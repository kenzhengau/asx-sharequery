"""Offline checks for the SharePoint reader.

The Graph calls need real credentials, but the parsers and configuration handling do not,
so this verifies those without any Azure setup. It builds a .docx and a PDF in memory.

Usage:
    uv run python scripts/check_sharepoint.py
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import sys
import zipfile

from asx_sharequery.sharepoint_client import (
    DocumentError,
    extract_docx_text,
    extract_pdf_text,
    extract_text,
    site_key_from_url,
)

_W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"

_DOCUMENT_XML = f"""<?xml version="1.0" encoding="UTF-8"?>
<w:document xmlns:w="{_W_NS}">
  <w:body>
    <w:p><w:r><w:t>Quarterly inspection summary</w:t></w:r></w:p>
    <w:p><w:r><w:t>Defects fell by</w:t></w:r><w:r><w:t> 12 percent</w:t></w:r></w:p>
    <w:p></w:p>
    <w:p><w:r><w:t>Second line of the report.</w:t></w:r></w:p>
  </w:body>
</w:document>
"""

failures: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    """Record a single assertion result."""
    if condition:
        print(f"   PASS  {label}")
    else:
        print(f"   FAIL  {label}{f' -> {detail}' if detail else ''}")
        failures.append(label)


def build_docx() -> bytes:
    """Create a minimal .docx in memory, including a run split across two <w:t> nodes."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("word/document.xml", _DOCUMENT_XML)
    return buffer.getvalue()


def build_pdf(text: str) -> bytes:
    """Assemble a tiny but structurally valid single-page PDF with a text object."""
    content = f"BT /F1 24 Tf 72 700 Td ({text}) Tj ET".encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n" + content + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]

    out = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for index, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{index} 0 obj\n".encode() + body + b"\nendobj\n"

    xref_at = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode() + b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref_at}\n%%EOF\n"
    ).encode()
    return bytes(out)


def check_docx() -> bytes:
    print("\n== .docx extraction ==")
    data = build_docx()
    text = extract_docx_text(data)
    print(f"   text: {text!r}")
    check("paragraph text extracted", "Quarterly inspection summary" in text)
    check("runs within a paragraph are joined", "Defects fell by 12 percent" in text)
    check("empty paragraphs dropped", "\n\n" not in text)
    return data


def check_pdf() -> None:
    print("\n== .pdf extraction ==")
    text = extract_pdf_text(build_pdf("Hello SharePoint"))
    print(f"   text: {text!r}")
    check("text layer read", "Hello SharePoint" in text)


def check_dispatch(docx: bytes) -> None:
    print("\n== extractor dispatch ==")
    check("extension match is case-insensitive", extract_text("R.DOCX", docx) == extract_docx_text(docx))
    try:
        extract_text("sheet.xlsx", b"x")
        check("unsupported type rejected", False, "no error raised")
    except DocumentError as exc:
        check("unsupported type rejected", True)
        print(f"         -> {exc}")


def check_corrupt() -> None:
    print("\n== corrupt input handling ==")
    for label, func in (("bad docx", extract_docx_text), ("bad pdf", extract_pdf_text)):
        try:
            func(b"definitely not a document")
            check(f"{label} raises DocumentError", False, "no error raised")
        except DocumentError as exc:
            check(f"{label} raises DocumentError", True)
            print(f"         -> {exc}")


def check_site_urls() -> None:
    print("\n== SharePoint site URL parsing ==")
    for url, expected in (
        ("https://contoso.sharepoint.com/sites/Research", "contoso.sharepoint.com:/sites/Research"),
        ("https://contoso.sharepoint.com/sites/Research/", "contoso.sharepoint.com:/sites/Research"),
        ("contoso.sharepoint.com/sites/Research", "contoso.sharepoint.com:/sites/Research"),
        ("https://contoso.sharepoint.com", "contoso.sharepoint.com:"),
    ):
        check(f"{url} -> {expected}", site_key_from_url(url) == expected, site_key_from_url(url))


def check_partial_config() -> None:
    print("\n== partly configured credentials are non-fatal ==")
    os.environ["SHAREPOINT_TENANT_ID"] = "tenant-only-for-this-check"
    for name in (
        "SHAREPOINT_CLIENT_ID",
        "SHAREPOINT_CLIENT_SECRET",
        "SHAREPOINT_SITE_URL",
        "SHAREPOINT_LIBRARY",
    ):
        os.environ.pop(name, None)

    from asx_sharequery.config import load_settings

    settings = load_settings()
    check("agent settings still load", settings.api_key != "")
    check("sharepoint left disabled", settings.sharepoint is None)
    check(
        "the problem is described",
        bool(settings.sharepoint_error and "SHAREPOINT_CLIENT_ID" in settings.sharepoint_error),
        str(settings.sharepoint_error),
    )

    from asx_sharequery.tools import AGENT_TOOLS

    tool = {registered.name: registered for registered in AGENT_TOOLS}["read_sharepoint_library"]
    payload = json.loads(asyncio.run(tool.func()))
    check("tool returns an error payload, not a traceback", "error" in payload)
    print(f"         -> {str(payload.get('error'))[:110]}")


def main() -> int:
    docx = check_docx()
    check_pdf()
    check_dispatch(docx)
    check_corrupt()
    check_site_urls()
    check_partial_config()

    print()
    if failures:
        print(f"{len(failures)} check(s) FAILED: {', '.join(failures)}")
        return 1
    print("All offline SharePoint checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
