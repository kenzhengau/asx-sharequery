# Deploying to Streamlit Community Cloud

This guide gets the ASX share agent onto a public `*.streamlit.app` URL.

Three things are worth knowing up front, because they shape everything below:

1. **The agent was not a Streamlit app.** The original browser front-end was Microsoft
   Agent Framework's DevUI, a FastAPI/uvicorn server with its own bundled single-page app.
   Community Cloud only runs a Streamlit script, so `streamlit_app/app.py` is a new, thin
   front-end over the same agent - same tools, same instructions, same output. The CLI and
   the DevUI window are untouched.
2. **Document reading is switched off, twice over.** `read_sharepoint_library` is not
   attached to the agent, and the app deletes every `SHAREPOINT_*` variable from the
   environment before loading settings. A public URL cannot tell its visitors apart, and
   that tool reads an organisation's private document library using app-only credentials.
   Do not re-enable it on a public app.
3. **The app bills your model API key.** Anyone who finds the URL can send it questions.
   Set a spend limit on the provider account before you share the link - the in-app
   question cap is a speed bump, not a control.

## What gets deployed

| | |
| --- | --- |
| Entrypoint | `streamlit_app/app.py` |
| Dependencies | `streamlit_app/requirements.txt` |
| Secrets | The app's secret store on Community Cloud - never a committed file |
| Python | 3.12 (Community Cloud default; `agent-framework-core` needs 3.10 or newer) |

The dependency file sits **next to the entrypoint** on purpose. Community Cloud installs
from one dependency file only, searching the entrypoint's directory before the repo root,
and within a directory it prefers `uv.lock`, then `Pipfile`, `environment.yml`,
`requirements.txt`, and `pyproject.toml`. The repo root has a `uv.lock`, so a root-level
`requirements.txt` would be ignored in favour of the full development lockfile.

The project itself is not installed into the cloud environment; `app.py` puts `src/` on
`sys.path` instead, which avoids an editable install reaching out of the entrypoint folder
for the repo's `pyproject.toml`.

## Before you start

- A GitHub account, and a [Streamlit Community Cloud](https://share.streamlit.io) account
  (free, signed in with GitHub).
- An OpenAI Platform key from <https://platform.openai.com/api-keys>, or a
  [DeepSeek key](https://platform.deepseek.com/api_keys).
- **A spend limit set on that provider account.** This is the only real control on what a
  public app can cost you.

## Step 1 - put the repo on GitHub

The repository has already been initialised and committed locally. All that is left is the
remote:

```bash
cd /Users/kenzheng/Desktop/asx-sharequery

# Verify nothing secret is about to be published - .env, .venv and
# .streamlit/secrets.toml are all git-ignored, and this should show no .env entry.
git status --short

# Option A - GitHub CLI (creates the repo and pushes in one step)
gh repo create asx-sharequery --public --source=. --push

# Option B - create an empty repo in the browser, then:
git remote add origin git@github.com:<your-account>/asx-sharequery.git
git push -u origin main
```

## Step 2 - create the app

1. Go to <https://share.streamlit.io> and click **Create app**.
2. When asked "Do you already have an app?", click **Yup, I have an app**.
3. Fill in:
   - **Repository**: `<your-account>/asx-sharequery`
   - **Branch**: `main`
   - **Main file path**: `streamlit_app/app.py`
   - **App URL**: pick a subdomain you will remember
4. Click **Advanced settings** and set **Python version** to `3.12`.
5. Still in Advanced settings, paste this into **Secrets**:

   ```toml
   OPENAI_API_KEY = "sk-..."
   ```

   Optionally add `OPENAI_MODEL = "gpt-4o-mini"`. For DeepSeek instead:
   `DEEPSEEK_API_KEY = "sk-..."` and `LLM_PROVIDER = "deepseek"`.

   Do not add any `SHAREPOINT_*` entry. The app removes them, and they would only be a
   credential sitting in a store that does not need it.
6. Click **Save**, then **Deploy**.

## Step 3 - check the first run

The app is up when the sidebar shows the provider, model, endpoint and a redacted key. If
it shows "no usable model API key" instead, the secrets did not save - reopen **Manage
app -> Settings -> Secrets**, save again, and reboot the app from the same menu.

To test it: ask *"What's happening with BHP?"* and watch the tool panel name the tools as
they are called, then collapse to a summary line.

## Updating the deployed app

Any push to `main` redeploys automatically. Code changes land immediately; changes to
`streamlit_app/requirements.txt` trigger a rebuild, which takes a few minutes.

When you bump a dependency, bump it in both `streamlit_app/requirements.txt` and
`pyproject.toml` so local runs match the deployment.

## What I could not do for you

Everything above is prepared, but two steps need a human with a browser:

- **Authenticating to GitHub and Streamlit.** Creating the remote repository and linking
  it to Community Cloud runs through GitHub OAuth, which cannot be driven from here.
- **Clicking Deploy.** Creating the app on `share.streamlit.io` is a form in their
  workspace, not an API this project can call.

You also need to paste the API key into the secrets box yourself, and that is the right
way around: it should never travel through a chat, a commit, or a log.

## Security notes, in the order they matter

1. **The app is public.** An app deployed from a public repo is public by default and
   searchable. Private apps exist but require a private repo, and each workspace is allowed
   only one at a time.
2. **Your key pays for every question.** Set a hard spend limit on the provider account.
   `MAX_TURNS_PER_SESSION` in `app.py` caps a conversation at 25 questions, but a new
   browser session resets the count, so treat it as etiquette rather than protection.
3. **SharePoint stays off.** Both belts are needed and both are in place: the tool is not
   attached to the agent, and the credentials are deleted from the environment. Together
   they mean an accidental secret cannot turn document reading back on.
4. **Optional: gate the whole app behind a login.** Streamlit's built-in OIDC support
   works with Entra ID, which suits an organisation that already has one. Add to secrets:

   ```toml
   [auth]
   redirect_uri = "https://<your-subdomain>.streamlit.app/oauth2callback"
   cookie_secret = "<a long random string>"
   client_id = "<entra app registration id>"
   client_secret = "<entra client secret>"
   server_metadata_url = "https://login.microsoftonline.com/<tenant>/v2.0/.well-known/openid-configuration"
   ```

   then call `st.login()` at the top of `main()`. Do this *before* considering SharePoint:
   a login proves who a visitor is, it does not prove they may read a given library.
5. **Optional: let visitors bring their own key.** A `st.text_input(type="password")` in
   the sidebar, copied into the environment before `load_settings()`, moves the cost off
   your account entirely. It also removes the reason for the question cap.
6. **The data sources are public ones.** ASX's JSON API and Google News RSS. HotCopper and
   Reddit are deliberately not scraped - both block automated clients, and the former has
   explicitly opted out of AI ingestion.

## Troubleshooting

| Symptom | Cause |
| --- | --- |
| Build fails, or `No module named 'asx_sharequery'` | The entrypoint path is wrong, or `streamlit_app/requirements.txt` was renamed or moved. |
| Build installs the wrong stack | A dependency file was added at the repo root. Only one file is used, and `uv.lock` outranks `requirements.txt`. |
| `Configuration error: No model API token is set` | Secrets were not saved, or the app was not rebooted after saving. |
| `401` mentioning `rejected_by_access_enforcement` | A ChatGPT / Codex sign-in token was pasted instead of a Platform API key. Create one at the URL above. |
| The app is asleep | Community Cloud apps sleep when idle and wake on the next visit. |
| `AttributeError` on a Streamlit command after a dependency bump | `streamlit_app/requirements.txt` pins 1.64.0. Loosen the pin only if you retest the page. |

## The alternative, if you would rather not rewrite the front-end at all

The DevUI window is a FastAPI/uvicorn app, so it runs unchanged on any host that serves
Python web processes - Render, Railway, Fly.io. That keeps the live tool-call panel and
the OpenAI-compatible API endpoint, and it needs no Streamlit code. The trade-off is that
Community Cloud's free tier, one-click Git deploys and secret store have no direct
equivalent, and an exposed bind still needs `--auth` on.
