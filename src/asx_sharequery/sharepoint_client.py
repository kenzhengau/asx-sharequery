"""Read documents from a SharePoint library using Microsoft Graph.

Authentication is app-only (OAuth 2.0 client credentials) against the Microsoft identity
platform, and the Graph calls are made with ``httpx`` rather than the Microsoft SDKs.
That is deliberate: ``azure-identity``, ``msal`` and ``msgraph-sdk`` all depend on
``cryptography``, whose recent releases publish no CPython wheels for Intel macOS, so
installing them here means a Rust source build. The client-credentials flow needs no
cryptography, only HTTPS form posts.

Text extraction avoids dependencies too: ``.docx`` is parsed from its underlying zip with
the standard library, and only ``.pdf`` needs a package (``pypdf``, which is pure Python).
"""

from __future__ import annotations

import asyncio
import io
import logging
import time
import zipfile
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import urlparse
from xml.etree import ElementTree

import httpx

from .config import SharePointSettings

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
LOGIN_BASE = "https://login.microsoftonline.com"
GRAPH_SCOPE = "https://graph.microsoft.com/.default"

# Word documents store visible text in these namespaces.
_W_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"

# pypdf logs parse problems ("invalid pdf header", "EOF marker not found") straight to the
# console, which interleaves with this project's own output. Failures are reported as
# DocumentError instead, so the library's logging is turned down to errors only.
logging.getLogger("pypdf").setLevel(logging.ERROR)

# Refresh a little before the token actually expires.
_TOKEN_SKEW_SECONDS = 120

SUPPORTED_EXTENSIONS = (".pdf", ".docx")


class SharePointError(RuntimeError):
    """Raised when a SharePoint or Graph operation fails."""


class SharePointAuthError(SharePointError):
    """Raised when Microsoft rejects the app credentials."""


class DocumentError(SharePointError):
    """Raised when a downloaded document cannot be read."""


@dataclass(frozen=True)
class LibraryFile:
    """A file in a SharePoint document library."""

    name: str
    item_id: str
    size: int
    modified: datetime | None
    web_url: str | None

    @property
    def extension(self) -> str:
        """Lower-cased file extension, including the dot."""
        _, _, suffix = self.name.rpartition(".")
        return f".{suffix.lower()}" if suffix else ""

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable representation."""
        return {
            "name": self.name,
            "size_bytes": self.size,
            "modified": self.modified.isoformat() if self.modified else None,
            "web_url": self.web_url,
        }


def _parse_modified(raw: Any) -> datetime | None:
    """Parse a Graph ISO-8601 timestamp."""
    if not isinstance(raw, str):
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def site_key_from_url(site_url: str) -> str:
    """Turn a SharePoint URL into the ``hostname:/path`` key Graph expects.

    ``https://contoso.sharepoint.com/sites/Research`` becomes
    ``contoso.sharepoint.com:/sites/Research``.
    """
    parsed = urlparse(site_url if "://" in site_url else f"https://{site_url}")
    if not parsed.netloc:
        raise SharePointError(f"'{site_url}' is not a valid SharePoint site URL")
    return f"{parsed.netloc}:{parsed.path.rstrip('/')}"


def extract_docx_text(data: bytes) -> str:
    """Extract visible text from a .docx using only the standard library.

    A .docx is a zip archive; paragraph text lives in ``<w:t>`` elements inside
    ``word/document.xml``.
    """
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            document = archive.read("word/document.xml")
    except KeyError as exc:
        raise DocumentError("the .docx has no word/document.xml") from exc
    except zipfile.BadZipFile as exc:
        raise DocumentError("the .docx is not a valid zip archive") from exc

    try:
        root = ElementTree.fromstring(document)
    except ElementTree.ParseError as exc:
        raise DocumentError("the .docx document.xml could not be parsed") from exc

    paragraphs: list[str] = []
    for paragraph in root.iter(f"{_W_NS}p"):
        text = "".join(node.text or "" for node in paragraph.iter(f"{_W_NS}t"))
        text = " ".join(text.split())
        if text:
            paragraphs.append(text)
    return "\n".join(paragraphs)


def extract_pdf_text(data: bytes) -> str:
    """Extract text from a PDF using pypdf."""
    from pypdf import PdfReader  # imported lazily so the module loads without it
    from pypdf.errors import PdfReadError

    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            # Try an empty password, which many "protected" PDFs accept.
            try:
                reader.decrypt("")
            except Exception as exc:  # noqa: BLE001 - pypdf raises varied types
                raise DocumentError("the PDF is encrypted") from exc

        pages: list[str] = []
        for page in reader.pages:
            try:
                pages.append(page.extract_text() or "")
            except Exception:  # noqa: BLE001 - one bad page should not lose the document
                pages.append("")
    except PdfReadError as exc:
        raise DocumentError(f"the PDF could not be read ({exc})") from exc

    return "\n".join(pages).strip()


def extract_text(filename: str, data: bytes) -> str:
    """Extract text from a supported document.

    Args:
        filename: Used to pick the extractor by extension.
        data: The raw file bytes.

    Returns:
        The extracted text, which may be empty for a scanned PDF with no text layer.

    Raises:
        DocumentError: if the file type is unsupported or the file cannot be parsed.
    """
    lowered = filename.lower()
    if lowered.endswith(".docx"):
        return extract_docx_text(data)
    if lowered.endswith(".pdf"):
        return extract_pdf_text(data)
    _, _, suffix = filename.rpartition(".")
    raise DocumentError(f"unsupported file type '.{suffix}'")


class SharePointClient:
    """App-only Microsoft Graph client scoped to one SharePoint site library.

    Use as an async context manager::

        async with SharePointClient(settings) as client:
            files = await client.list_files()
    """

    def __init__(self, settings: SharePointSettings, *, timeout: float = 30.0) -> None:
        self._settings = settings
        self._http = httpx.AsyncClient(timeout=timeout, follow_redirects=True)
        self._token: str | None = None
        self._token_expires_at: float = 0.0

    async def __aenter__(self) -> SharePointClient:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Close the underlying HTTP connection pool."""
        await self._http.aclose()

    # ------------------------------------------------------------------ auth

    async def _access_token(self) -> str:
        """Get an app-only Graph token, cached until shortly before it expires."""
        if self._token and time.monotonic() < self._token_expires_at:
            return self._token

        url = f"{LOGIN_BASE}/{self._settings.tenant_id}/oauth2/v2.0/token"
        payload = {
            "client_id": self._settings.client_id,
            "client_secret": self._settings.client_secret,
            "scope": GRAPH_SCOPE,
            "grant_type": "client_credentials",
        }

        try:
            response = await self._http.post(
                url,
                data=payload,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        except httpx.HTTPError as exc:
            raise SharePointError(
                f"could not reach the Microsoft identity service ({exc.__class__.__name__})"
            ) from exc

        if response.status_code >= 400:
            detail = ""
            try:
                body = response.json()
                detail = body.get("error_description") or body.get("error") or ""
            except ValueError:
                detail = response.text[:200]
            raise SharePointAuthError(
                f"Microsoft rejected the app credentials (HTTP {response.status_code}): "
                f"{detail.strip() or 'no detail returned'}"
            )

        try:
            body = response.json()
        except ValueError as exc:
            raise SharePointAuthError("the identity service returned non-JSON") from exc

        token = body.get("access_token")
        if not token:
            raise SharePointAuthError("the identity service returned no access_token")

        self._token = str(token)
        self._token_expires_at = time.monotonic() + float(body.get("expires_in", 3600))
        self._token_expires_at -= _TOKEN_SKEW_SECONDS
        return self._token

    async def _graph_get(self, path: str, **params: Any) -> dict[str, Any]:
        """GET a Graph resource, returning the parsed JSON object."""
        token = await self._access_token()
        url = path if path.startswith("http") else f"{GRAPH_BASE}{path}"

        try:
            response = await self._http.get(
                url,
                params=params or None,
                headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            )
        except httpx.HTTPError as exc:
            raise SharePointError(
                f"could not reach Microsoft Graph ({exc.__class__.__name__})"
            ) from exc

        if response.status_code == 401:
            raise SharePointAuthError("Microsoft Graph rejected the token (HTTP 401)")
        if response.status_code == 403:
            raise SharePointError(
                "Microsoft Graph denied access (HTTP 403). The app registration needs the "
                "'Sites.Read.All' application permission with admin consent granted."
            )
        if response.status_code == 404:
            raise SharePointError(
                "Microsoft Graph returned 404. Check the site URL and library name."
            )
        if response.status_code >= 400:
            detail = ""
            try:
                detail = str(response.json().get("error", {}).get("message", ""))
            except ValueError:
                detail = response.text[:200]
            raise SharePointError(
                f"Microsoft Graph returned HTTP {response.status_code}: {detail.strip()}"
            )

        try:
            return response.json()
        except ValueError as exc:
            raise SharePointError("Microsoft Graph returned non-JSON content") from exc

    # ------------------------------------------------------- site and library

    async def resolve_site(self) -> dict[str, str]:
        """Resolve the configured site URL to its Graph site record."""
        key = site_key_from_url(self._settings.site_url)
        data = await self._graph_get(f"/sites/{key}", **{"$select": "id,displayName,webUrl"})
        return {
            "id": str(data.get("id", "")),
            "display_name": str(data.get("displayName") or data.get("name") or ""),
            "web_url": str(data.get("webUrl") or self._settings.site_url),
        }

    async def resolve_drive(self, site_id: str) -> dict[str, str]:
        """Find the document library by display name.

        If the name does not match, the error lists what is available, which is the
        quickest way for a user to fix a typo.
        """
        data = await self._graph_get(f"/sites/{site_id}/drives", **{"$select": "id,name,webUrl"})
        drives = data.get("value") or []
        if not drives:
            raise SharePointError("the site has no document libraries the app can see")

        wanted = self._settings.library.strip().lower()
        for drive in drives:
            name = str(drive.get("name", ""))
            if name.lower() == wanted:
                return {
                    "id": str(drive.get("id", "")),
                    "name": name,
                    "web_url": str(drive.get("webUrl", "")),
                }

        available = ", ".join(sorted(str(d.get("name", "?")) for d in drives))
        raise SharePointError(
            f"no library named '{self._settings.library}' on this site. "
            f"Available libraries: {available}"
        )

    # ------------------------------------------------------------ file access

    def _as_file(self, item: dict[str, Any]) -> LibraryFile:
        """Convert a Graph driveItem into a LibraryFile."""
        return LibraryFile(
            name=str(item.get("name", "")),
            item_id=str(item.get("id", "")),
            size=int(item.get("size") or 0),
            modified=_parse_modified(item.get("lastModifiedDateTime")),
            web_url=item.get("webUrl"),
        )

    async def list_files(
        self,
        drive_id: str,
        *,
        extensions: tuple[str, ...] = SUPPORTED_EXTENSIONS,
        max_files: int = 50,
    ) -> list[LibraryFile]:
        """List supported files in a library, recursing into folders.

        Args:
            drive_id: The drive (library) id.
            extensions: File extensions to keep.
            max_files: Stop after this many files to bound the work.

        Returns:
            The matching files, newest first.
        """
        found: list[LibraryFile] = []
        # (item id or 'root') pairs to walk breadth-first.
        pending: list[str] = ["root"]

        while pending and len(found) < max_files:
            folder_id = pending.pop(0)
            path = (
                f"/drives/{drive_id}/root/children"
                if folder_id == "root"
                else f"/drives/{drive_id}/items/{folder_id}/children"
            )
            data = await self._graph_get(
                path,
                **{"$select": "id,name,size,lastModifiedDateTime,webUrl,folder,file", "$top": 200},
            )

            for item in data.get("value") or []:
                if not isinstance(item, dict):
                    continue
                if "folder" in item:
                    pending.append(str(item.get("id", "")))
                    continue
                if "file" not in item:
                    continue
                candidate = self._as_file(item)
                if candidate.extension in extensions:
                    found.append(candidate)
                    if len(found) >= max_files:
                        break

        found.sort(key=lambda f: f.modified.timestamp() if f.modified else 0.0, reverse=True)
        return found

    async def download(self, drive_id: str, item_id: str) -> bytes:
        """Download a file's bytes.

        Graph answers ``/content`` with a redirect to a pre-authenticated download URL that
        lives on a different host. The redirect is followed manually and without the
        Authorization header, because sending our token to that host would leak it and can
        make the storage endpoint reject the request.
        """
        token = await self._access_token()
        url = f"{GRAPH_BASE}/drives/{drive_id}/items/{item_id}/content"

        try:
            response = await self._http.get(
                url, headers={"Authorization": f"Bearer {token}"}, follow_redirects=False
            )
        except httpx.HTTPError as exc:
            raise SharePointError(
                f"could not download the file ({exc.__class__.__name__})"
            ) from exc

        if response.status_code in (301, 302, 303, 307, 308):
            location = response.headers.get("location")
            if not location:
                raise SharePointError("the download redirect had no Location header")
            try:
                response = await self._http.get(location)  # no auth header on purpose
            except httpx.HTTPError as exc:
                raise SharePointError(
                    f"could not fetch the file from storage ({exc.__class__.__name__})"
                ) from exc

        if response.status_code >= 400:
            raise SharePointError(f"the download failed with HTTP {response.status_code}")

        return response.content

    async def read_document(self, drive_id: str, library_file: LibraryFile) -> str:
        """Download a file and extract its text off the event loop."""
        data = await self.download(drive_id, library_file.item_id)
        return await asyncio.to_thread(extract_text, library_file.name, data)
