# Deployment Runbook

> **Note:** The Python package was renamed from `gdrive-mcp` to `gsuite-mcp`
> (module: `gsuite_mcp`). GCP infrastructure names (project ID
> `gdrive-mcp-492818`, Cloud Run service `gdrive-mcp`, Secret Manager keys,
> service URL) are **not** renamed — changing them would require downtime and
> client reconfiguration.

This document describes how `gsuite-mcp` is deployed and how to operate it.
**Do not commit secret values to this file.** Use the placeholder names as
written and rely on Secret Manager for the real values.

## Topology

```
┌──────────────────┐    HTTPS + Bearer    ┌──────────────────────────────┐
│ MCP client       │ ───────────────────► │ Cloud Run service            │
│ (Claude Desktop  │                      │ gdrive-mcp (us-central1)     │
│  via mcp-remote) │                      │ project: gdrive-mcp-492818   │
└──────────────────┘                      └──────────────┬───────────────┘
                                                         │ OAuth user creds
                                                         ▼
                                            ┌─────────────────────────┐
                                            │ Google Drive / Docs /   │
                                            │ Sheets APIs as          │
                                            │ josh@josh.is            │
                                            └─────────────────────────┘
```

- **GCP project:** `gdrive-mcp-492818`
- **Region:** `us-central1`
- **Cloud Run service:** `gdrive-mcp`
- **Service URL:** `https://gdrive-mcp-1055579418514.us-central1.run.app`
- **MCP endpoint:** `POST /mcp` (no trailing slash — `/mcp/` 307-redirects and most curl-style clients drop the body on redirect)
- **Runtime service account:** `1055579418514-compute@developer.gserviceaccount.com` (default Compute SA, granted `secretAccessor` on each secret below)
- **Identity the server acts as:** `josh@josh.is` (Workspace user — the OAuth refresh token was minted by this account)

## Auth model

Two layers, both required on every request:

1. **Caller → server:** shared-secret API key, accepted via either `Authorization: Bearer <api-key>` header or `?key=<api-key>` query parameter. Enforced by `APIKeyMiddleware` (`src/gsuite_mcp/api_key_middleware.py`). Constant-time comparison via `hmac.compare_digest`. The header is checked first; the query param is a fallback for clients (like Claude.ai) that can't set custom headers. Missing/wrong key → `401 {"error": "unauthorized"}`.
2. **Server → Google APIs:** OAuth user credentials (client id + client secret + long-lived refresh token). On every Drive call the server exchanges the refresh token for a short-lived access token via the standard OAuth refresh flow.

The server **refuses to start** if neither `GSUITE_MCP_API_KEY` nor `GDRIVE_MCP_API_KEY` is set (see `server.py:main`). There is no mode that runs unauthenticated.

## Secret Manager secrets

All four are in project `gdrive-mcp-492818` with automatic replication. The runtime SA has `roles/secretmanager.secretAccessor` on each.

| Secret name                  | Mounted as env var            | Purpose                                               |
| ---------------------------- | ----------------------------- | ----------------------------------------------------- |
| `gdrive-oauth-client-id`     | `GOOGLE_OAUTH_CLIENT_ID`      | OAuth Desktop client ID                                |
| `gdrive-oauth-client-secret` | `GOOGLE_OAUTH_CLIENT_SECRET`  | OAuth Desktop client secret                            |
| `gdrive-oauth-refresh-token` | `GOOGLE_OAUTH_REFRESH_TOKEN`  | Long-lived refresh token for josh@josh.is              |
| `gdrive-mcp-api-key`         | `GDRIVE_MCP_API_KEY`          | Shared secret for the bearer-token middleware (server also accepts `GSUITE_MCP_API_KEY`) |

The legacy `gdrive-sa` service-account secret is **kept** for rollback only — it is no longer mounted by the current revision.

## Initial deployment (already done — for reference)

```bash
# 1. Create OAuth Desktop client in Cloud Console
#    https://console.cloud.google.com/apis/credentials?project=gdrive-mcp-492818
#    → Internal consent screen, Desktop application type
#    → Download JSON to ~/.config/gdrive-mcp/oauth-client.json (chmod 600)

# 2. Mint the refresh token (interactive — opens browser)
GOOGLE_OAUTH_CLIENT_ID='<client-id>' \
GOOGLE_OAUTH_CLIENT_SECRET='<client-secret>' \
  uv run python -m gsuite_mcp.auth_setup
# → Sign in as josh@josh.is, allow Drive/Docs/Sheets/Gmail scopes
# → Captures GOOGLE_OAUTH_REFRESH_TOKEN

# 3. Generate the API key
python3 -c "import secrets; print(secrets.token_urlsafe(32))"

# 4. Create the four secrets
printf '%s' '<client-id>'      | gcloud secrets create gdrive-oauth-client-id     --project=gdrive-mcp-492818 --data-file=- --replication-policy=automatic
printf '%s' '<client-secret>'  | gcloud secrets create gdrive-oauth-client-secret --project=gdrive-mcp-492818 --data-file=- --replication-policy=automatic
printf '%s' '<refresh-token>'  | gcloud secrets create gdrive-oauth-refresh-token --project=gdrive-mcp-492818 --data-file=- --replication-policy=automatic
printf '%s' '<api-key>'        | gcloud secrets create gdrive-mcp-api-key         --project=gdrive-mcp-492818 --data-file=- --replication-policy=automatic

# 5. Grant runtime SA accessor on each
for SECRET in gdrive-oauth-client-id gdrive-oauth-client-secret gdrive-oauth-refresh-token gdrive-mcp-api-key; do
  gcloud secrets add-iam-policy-binding "$SECRET" \
    --member='serviceAccount:1055579418514-compute@developer.gserviceaccount.com' \
    --role='roles/secretmanager.secretAccessor' \
    --project=gdrive-mcp-492818
done

# 6. Enable required APIs
gcloud services enable drive.googleapis.com docs.googleapis.com sheets.googleapis.com gmail.googleapis.com \
  --project=gdrive-mcp-492818

# 7. Deploy
gcloud run deploy gdrive-mcp \
  --source=. \
  --region=us-central1 \
  --project=gdrive-mcp-492818 \
  --update-secrets=GOOGLE_OAUTH_CLIENT_ID=gdrive-oauth-client-id:latest,GOOGLE_OAUTH_CLIENT_SECRET=gdrive-oauth-client-secret:latest,GOOGLE_OAUTH_REFRESH_TOKEN=gdrive-oauth-refresh-token:latest,GDRIVE_MCP_API_KEY=gdrive-mcp-api-key:latest
```

## Redeploying after a code change

```bash
cd /Users/josh/Desktop/CODING/gdrive-mcp
gcloud run deploy gdrive-mcp \
  --source=. \
  --region=us-central1 \
  --project=gdrive-mcp-492818 \
  --quiet
```

The `--update-secrets` flags from the initial deploy persist on the service, so you don't need to repeat them. Cloud Build picks up `Dockerfile`, builds the image, and rolls a new revision with `:latest` of every secret.

## Rotating the API key

```bash
# 1. Generate a new key
NEW_KEY=$(python3 -c "import secrets; print(secrets.token_urlsafe(32))")

# 2. Add a new version to the secret
printf '%s' "$NEW_KEY" | gcloud secrets versions add gdrive-mcp-api-key \
  --project=gdrive-mcp-492818 --data-file=-

# 3. Trigger a redeploy so the new version is picked up
gcloud run deploy gdrive-mcp --source=. --region=us-central1 \
  --project=gdrive-mcp-492818 --quiet

# 4. Update Claude Desktop config (and any other clients)
#    ~/Library/Application Support/Claude/claude_desktop_config.json
#    → replace the bearer token in the gdrive-mcp entry's --header arg
#    → Cmd+Q Claude Desktop and reopen
```

## Rotating the OAuth refresh token

Refresh tokens for **Internal** Workspace OAuth clients don't expire on a fixed schedule, but they can be invalidated by the user (revoking access in account settings) or by Google for inactivity. To mint a fresh one:

```bash
# 1. Reuse the existing client id + secret (or create a new client)
GOOGLE_OAUTH_CLIENT_ID='<from-secret-or-~/.config/gdrive-mcp/oauth-client.json>' \
GOOGLE_OAUTH_CLIENT_SECRET='<from-secret-or-~/.config/gdrive-mcp/oauth-client.json>' \
  uv run python -m gsuite_mcp.auth_setup

# 2. Add a new version to the secret with the printed token
printf '%s' '<new-refresh-token>' | gcloud secrets versions add gdrive-oauth-refresh-token \
  --project=gdrive-mcp-492818 --data-file=-

# 3. Redeploy
gcloud run deploy gdrive-mcp --source=. --region=us-central1 \
  --project=gdrive-mcp-492818 --quiet
```

The local OAuth client JSON lives at `~/.config/gdrive-mcp/oauth-client.json` (mode 600, outside the repo, defended by `.gitignore` patterns: `oauth-client.json`, `client_secret*.json`).

## Smoke testing the deployed service

```bash
URL="https://gdrive-mcp-1055579418514.us-central1.run.app/mcp"
KEY=$(gcloud secrets versions access latest --secret=gdrive-mcp-api-key --project=gdrive-mcp-492818)

# 1. No header → expect 401
curl -s -X POST "$URL" \
  -H "Content-Type: application/json" -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"smoke","version":"0.0"}}}' \
  -w "\nHTTP %{http_code}\n"

# 2. Correct key → expect 200 + initialize response
curl -s -X POST "$URL" \
  -H "Content-Type: application/json" -H "Accept: application/json, text/event-stream" \
  -H "Authorization: Bearer $KEY" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"smoke","version":"0.0"}}}' \
  -w "\nHTTP %{http_code}\n"

# 3. tools/list → expect 9 tools
curl -s -X POST "$URL" \
  -H "Content-Type: application/json" -H "Accept: application/json, text/event-stream" \
  -H "Authorization: Bearer $KEY" \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}' \
  | sed -n 's/^data: //p' \
  | python3 -c "import sys, json; r=json.loads(sys.stdin.read()); print(len(r['result']['tools']), 'tools')"
```

Expected output of (3): `9 tools`.

## Connecting an MCP client

Any MCP client that supports streamable HTTP with custom headers will work. The reference client setup uses `mcp-remote` (a stdio↔HTTP bridge that ships via npx), which is what Claude Desktop uses.

### Claude Desktop

Edit `~/Library/Application Support/Claude/claude_desktop_config.json` and add (or update) the `gdrive-mcp` entry:

```json
{
  "mcpServers": {
    "gdrive-mcp": {
      "command": "npx",
      "args": [
        "-y",
        "mcp-remote",
        "https://gdrive-mcp-1055579418514.us-central1.run.app/mcp",
        "--header",
        "Authorization:Bearer <api-key>"
      ]
    }
  }
}
```

Note the `Header-Name:Header-Value` format with **no space** around the colon — that is `mcp-remote`'s required syntax. The literal value passed to the server is `Bearer <api-key>` (the space between `Bearer` and the token is part of the value).

After editing, fully quit Claude Desktop (Cmd+Q) and reopen it. The config is only read at startup.

### Claude.ai

Add a remote MCP integration in **Settings → Integrations → Add More** with this URL:

```
https://gdrive-mcp-1055579418514.us-central1.run.app/mcp?key=<api-key>
```

Claude.ai doesn't support custom auth headers on MCP servers, so the API key is passed as a query parameter instead.

### Other MCP clients

Point them at `https://gdrive-mcp-1055579418514.us-central1.run.app/mcp` and inject `Authorization: Bearer <api-key>` on every request. If the client doesn't natively support headers, either:
- Append `?key=<api-key>` to the URL, or
- Wrap with `npx mcp-remote ... --header Authorization:Bearer <api-key>`

## Logs

```bash
# Tail last 50 lines
gcloud run services logs read gdrive-mcp --region=us-central1 \
  --project=gdrive-mcp-492818 --limit=50

# Stream live
gcloud run services logs tail gdrive-mcp --region=us-central1 \
  --project=gdrive-mcp-492818
```

## Rollback

```bash
# List recent revisions
gcloud run revisions list --service=gdrive-mcp --region=us-central1 \
  --project=gdrive-mcp-492818 --limit=10

# Roll traffic back to a previous revision
gcloud run services update-traffic gdrive-mcp --region=us-central1 \
  --project=gdrive-mcp-492818 --to-revisions=gdrive-mcp-00005-zp5=100
```

The pre-OAuth-expansion revision (`gdrive-mcp-00004-2sw`) and the pre-API-key revision (`gdrive-mcp-00005-zp5`) still exist and can be served if needed. The legacy `gdrive-sa` secret is also still in Secret Manager for the same reason.

## Local development

```bash
uv sync --all-extras
uv run pytest -q              # 42 tests
uv run ruff check .

# Run the server locally (requires all 4 env vars)
GDRIVE_MCP_API_KEY=$(gcloud secrets versions access latest --secret=gdrive-mcp-api-key --project=gdrive-mcp-492818) \
GOOGLE_OAUTH_CLIENT_ID=$(gcloud secrets versions access latest --secret=gdrive-oauth-client-id --project=gdrive-mcp-492818) \
GOOGLE_OAUTH_CLIENT_SECRET=$(gcloud secrets versions access latest --secret=gdrive-oauth-client-secret --project=gdrive-mcp-492818) \
GOOGLE_OAUTH_REFRESH_TOKEN=$(gcloud secrets versions access latest --secret=gdrive-oauth-refresh-token --project=gdrive-mcp-492818) \
  uv run python -m gdrive_mcp
```
