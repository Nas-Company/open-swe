# Nas Company Deployment Runbook

This runbook documents the current Nas Company production deployment for Open SWE.
It is intentionally secret-free: keep API keys, private keys, tokens, webhook
secrets, JWT secrets, and OAuth client secrets in the provider dashboards or local
environment only.

## Current Production Shape

Open SWE runs as two deployed pieces:

1. Backend: LangGraph/LangSmith deployment
   - LangGraph backend URL: `https://nascompany-open-swe-5786464fff6d52fdb4f32c80d541067d.aws.us.langgraph.app`
   - Deployment ID: `fcbdc143-a1ac-4eb0-a8d0-bedbcc49e3ed`
   - Defined by `langgraph.json`
   - Serves the graphs: `agent`, `reviewer`, `analyzer`, `chat`, `scheduler`
   - Serves the FastAPI app: `agent.webapp:app`
   - Owns `/dashboard/api/*`, `/webhooks/*`, `/health`, and graph run execution

2. Dashboard frontend: Vercel deployment
   - Production URL: `https://nascompany-open-swe-indol.vercel.app`
   - Source directory: `ui/`
   - Vercel config: `ui/vercel.json`
   - Build command: `bun run build`
   - Output directory: `.output/public`

Production routing:

```text
Browser
  -> https://nascompany-open-swe-indol.vercel.app
  -> /dashboard/api/*
  -> Vercel rewrite
  -> https://nascompany-open-swe-5786464fff6d52fdb4f32c80d541067d.aws.us.langgraph.app/dashboard/api/*
  -> FastAPI dashboard API
  -> LangGraph SDK
  -> LangGraph run/thread/store/checkpointer
  -> Modal sandbox
```

Thread state and conversation history live in the LangGraph backend, not in the
Vercel frontend.

Last verified successful backend deployment:

```text
Date: 2026-07-11
Revision: f01ad801-4453-4335-93af-e98348b8f450
Commit: f84df52b4cede1e43ae04c36ff2cd38fb9d20d5b
Verification: /info host_revision_id matched the revision, /health returned healthy,
and a production agent run executed successfully in the nascompany Modal workspace
```

## Backend Environment

Set backend environment variables in the LangGraph/LangSmith deployment. Do not
commit values.

Required categories:

```text
LangSmith/LangGraph:
  LANGSMITH_API_KEY_PROD
  LANGSMITH_TENANT_ID_PROD
  LANGSMITH_TRACING_PROJECT_ID_PROD
  LANGSMITH_URL_PROD
  LANGSMITH_ENDPOINT
  LANGSMITH_HOST_API_URL
  LANGGRAPH_HOST_URL

LLM:
  CODEX_PROXY_BASE_URL / CODEX_PROXY_API_KEY
  ANTHROPIC_API_KEY or ANTHROPIC_AUTH_TOKEN / ANTHROPIC_BASE_URL
  OPENAI_API_KEY (only for direct OpenAI fallback outside Codex Proxy)
  GOOGLE_API_KEY
  FIREWORKS_API_KEY
  LLM_FALLBACK_MODEL_ID

GitHub:
  GITHUB_APP_ID
  GITHUB_APP_PRIVATE_KEY
  GITHUB_APP_INSTALLATION_ID
  GITHUB_WEBHOOK_SECRET
  GITHUB_APP_CLIENT_ID
  GITHUB_APP_CLIENT_SECRET
  GITHUB_OAUTH_PROVIDER_ID
  X_SERVICE_AUTH_JWT_SECRET

Dashboard:
  DASHBOARD_API_BASE_URL
  DASHBOARD_BASE_URL
  DASHBOARD_JWT_SECRET
  DASHBOARD_ALLOWED_ORIGINS
  CONFIGURED_ADMINS
  LANGGRAPH_URL

Access control:
  ALLOWED_GITHUB_ORGS
  ALLOWED_GITHUB_REPOS
  DEFAULT_REPO_OWNER
  DEFAULT_REPO_NAME

Sandbox:
  SANDBOX_TYPE
  MODAL_APP_NAME
  MODAL_TOKEN_ID
  MODAL_TOKEN_SECRET
  MODAL_SANDBOX_TIMEOUT_SECONDS (optional, default 14400)
  MODAL_SANDBOX_IDLE_TIMEOUT_SECONDS (optional, default 1800)
  MODAL_SANDBOX_WORKDIR (optional, default /workspace)
  MODAL_SANDBOX_CPU (optional, default 2)
  MODAL_SANDBOX_MEMORY_MIB (optional, default 4096)
  SANDBOX_CREATION_TIMEOUT_SECONDS (optional; Modal default 900)

Optional integrations:
  LINEAR_API_KEY
  LINEAR_WEBHOOK_SECRET
  SLACK_BOT_TOKEN
  SLACK_SIGNING_SECRET
  SLACK_CLIENT_ID
  SLACK_CLIENT_SECRET
  EXA_API_KEY
```

Current production intent:

```text
SANDBOX_TYPE=modal
MODAL_APP_NAME=open-swe
DASHBOARD_BASE_URL=https://nascompany-open-swe-indol.vercel.app
DASHBOARD_API_BASE_URL=https://nascompany-open-swe-indol.vercel.app
LANGGRAPH_URL=https://nascompany-open-swe-5786464fff6d52fdb4f32c80d541067d.aws.us.langgraph.app
DASHBOARD_ALLOWED_ORIGINS=https://nascompany-open-swe-indol.vercel.app
ALLOWED_GITHUB_ORGS=Nas-Company
DEFAULT_REPO_OWNER=Nas-Company
DEFAULT_REPO_NAME=open-swe
```

Model routing intent:

```text
OpenAI-compatible endpoint: Codex Proxy
Default agent model: openai:gpt-5.6-sol (medium)
Default agent subagent model: openai:gpt-5.6-sol (medium)
LLM_FALLBACK_MODEL_ID=openai:gpt-5.6-sol
```

The fallback secret intentionally matches the default so a failed SOL request
does not silently fall back to the retired GPT-5.5 route. MiniMax remains a
selectable cross-provider option in the dashboard.

Modal ownership:

```text
Workspace: nascompany
Environment: main
App: open-swe
Workspace owners: chux@nas.com, techshare@nas.io
```

Production originally used a Modal token and `open-swe` app from the personal
`chux` workspace. On 2026-07-11, a workspace-scoped token and app were created in
the shareable `nascompany` workspace, the LangGraph deployment secrets were
rotated, and revision `f01ad801-4453-4335-93af-e98348b8f450` became active. A
production `agent` run then created sandbox `sb-WI8eOU2bJZuvDh9bevP0cM` and
successfully executed a command. The legacy personal app remains deployed but
had no active tasks at verification time; do not delete it until its token and
rollback value have been reviewed explicitly.

Because production uses Modal as the command sandbox provider, LangSmith sandbox
snapshot variables may be empty. If `SANDBOX_TYPE` changes back to `langsmith`,
set `DEFAULT_SANDBOX_SNAPSHOT_ID` and the related snapshot resource settings.

The Modal adapter builds a pinned Playwright/Chromium image with Python 3.12,
Node, Bun, GitHub CLI, git, and ripgrep. Each thread's selected repository scopes
a short-lived GitHub App installation token. The token stays in backend memory
and is exposed only to a one-shot GitHub broker sandbox; the long-lived thread
sandbox never receives it. Clone/fetch and fixed-SHA push data cross that boundary
as Git bundles, and every broker is terminated after the command. Package
installs, builds, tests, and browser renders therefore do not inherit GitHub
credentials. Dashboard-user GitHub OAuth stays on the trusted server for identity
and server-side API calls and is never forwarded to either sandbox. A private
repository must be granted to the GitHub App installation before the broker can
clone it; missing App access fails closed during sandbox creation.

Every new Modal thread sandbox is also tagged with the configured app name and
the Modal control-plane app ID. Reconnect verifies the exact sandbox ID through
an app-ID-scoped lookup before any command or file operation. Sandboxes created
before this ownership marker was introduced fail closed and are replaced on the
next run, so their ephemeral `/workspace` contents do not migrate automatically.
Download any required artifacts from an older thread before deploying this
change.

Dashboard messages can attach UTF-8 `.md`, `.html`, `.json`, `.csv`, and `.txt`
files. The limits are five files, 2 MiB per file, and 10 MiB combined. The
backend validates them, writes them under `/workspace/.open-swe/attachments/`,
and replaces base64 blocks with exact sandbox paths before the model call.

### Rendered-page visual QA

Playwright supplies deterministic rendering and objective browser diagnostics;
the model supplies the independent pixel-level review. For HTML and report work,
the agent renders desktop and mobile views, captures a full-page overview plus
legible viewport/section tiles for long pages, and calls `read_file` on each
final screenshot. It records image observations separately from console, page,
network, overflow, and accessibility diagnostics, then fixes and rerenders until
both evidence sets pass.

Codex Proxy uses Chat Completions. That API path accepts user-message images but
can silently ignore image content carried directly by a tool-role message. The
`CodexProxyToolImageMiddleware` therefore mirrors the latest image results from
`read_file`, plus a bounded pending batch when intervening tool calls contain no
durable visual observations, into one transient user-role multimodal message.
The original thread messages and dashboard artifacts remain unchanged, all
parallel tool results still precede the mirrored user message, and acknowledged
or out-of-window screenshots are stripped from later provider requests. Visual
read batches are capped at eight images and roughly 6 MiB of encoded payload.

### Durable generated files

Final user-facing files are copied out of the sandbox with the agent's
`publish_artifact` tool and stored as chunked, checksummed records in the
LangGraph Store. The dashboard lists them under the task's `Files` workspace
tab and downloads them through authenticated `/dashboard/api/threads/*`
endpoints. Downloads never reconnect to the sandbox, and HTML is always served
as an attachment with content sniffing disabled.

Artifact delivery has a server-side terminal-response guard as well as the
model-facing tool instruction. If a normal completion forgets to call the tool,
the guard injects auditable `publish_artifact` calls for eligible paths listed
under an explicit `Deliverables` heading or staged in the controlled
`.open-swe/deliverables/` outbox. It does not scan the repository, and it rejects
attachments, hidden paths, credential-like names, symlinks, paths outside the
sandbox workspace, and non-deliverable source extensions.

Artifacts are retained for 30 days without refresh-on-read. Limits are 20 MiB
per file, 10 files per task, and 50 MiB total per task. `langgraph.json` enables
the Store TTL sweeper but intentionally does not set a global Store default TTL,
because OAuth credentials, profiles, and team settings share the same Store.
Manual task deletion removes its artifacts best-effort; per-item TTL bounds any
orphaned data left by automatic thread expiry or a partial cleanup.

## Frontend Environment

Production uses same-origin dashboard API requests. In Vercel,
`VITE_DASHBOARD_API_BASE_URL` should generally be empty or unset.

`ui/vercel.json` must rewrite API requests to the current backend:

```json
{
  "rewrites": [
    {
      "source": "/dashboard/api/:path*",
      "destination": "https://nascompany-open-swe-5786464fff6d52fdb4f32c80d541067d.aws.us.langgraph.app/dashboard/api/:path*"
    },
    {
      "source": "/(.*)",
      "destination": "/_shell.html"
    }
  ]
}
```

The GitHub App dashboard callback must include:

```text
https://nascompany-open-swe-indol.vercel.app/dashboard/api/auth/callback
```

This callback hits Vercel first, then the Vercel rewrite forwards it to the
backend. That lets the backend set the `osw_session` cookie on the dashboard
host and lets later browser requests send the cookie same-origin.

## Normal Deployment Flow

Backend:

1. Push code to `Nas-Company/open-swe`.
2. Trigger a LangGraph deployment from the connected GitHub source.
3. Confirm the new revision builds.
4. Wait for rollout to reach `DEPLOYED`.
5. Confirm the deployment active/latest revision points to the new revision.
6. Confirm the public backend reports the new host revision.
7. For sandbox-runtime changes, run a real production agent smoke test that
   clones a private repo and checks `bun`, `playwright`, Chromium rendering,
   screenshot reading, and sandbox reconnect persistence.

```bash
curl -sS https://nascompany-open-swe-5786464fff6d52fdb4f32c80d541067d.aws.us.langgraph.app/info
curl -sS https://nascompany-open-swe-5786464fff6d52fdb4f32c80d541067d.aws.us.langgraph.app/health
```

Frontend:

1. Commit any `ui/` changes.
2. Push to the branch Vercel deploys from.
3. Confirm Vercel builds `ui/` with `bun run build`.
4. Confirm `/dashboard/api/*` requests from the deployed dashboard reach the
   backend and carry the session cookie.

## LangGraph CLI Limitation

On 2026-06-09 and 2026-06-10, these direct CLI paths failed before rollout:

```text
langgraph deploy --remote     -> POST /v2/deployments/.../upload-url returned 500
langgraph deploy --no-remote  -> POST /v2/deployments/.../push-token returned 500
```

When this happens, use the GitHub-source Host API path instead of source archive
upload or local image push.

The Host API expects `X-Api-Key` and, when applicable, `X-Tenant-ID` headers.
Do not use `Authorization: Bearer ...` for this deployment API path.

Example shape:

```bash
set -a
source .env >/dev/null 2>&1
set +a

curl -sS -X PATCH \
  "$LANGGRAPH_HOST_URL/v2/deployments/fcbdc143-a1ac-4eb0-a8d0-bedbcc49e3ed" \
  -H "X-Api-Key: $LANGSMITH_API_KEY_PROD" \
  -H "X-Tenant-ID: $LANGSMITH_TENANT_ID_PROD" \
  -H "Content-Type: application/json" \
  --data @payload.json
```

Use the LangGraph Host API payload fields for a GitHub-source revision, including
`revision_source=github` and the target repository ref or commit SHA. Keep the
payload in a local temporary file if it includes any environment update.

## Rollout Failure Pattern

If a revision builds but later fails with:

```text
Timeout: Queue Deployment is not ready after 600 seconds
```

check deploy logs for queue, DNS, and Postgres readiness symptoms such as:

```text
failed to resolve host ...svc.cluster.local
failed to get next run from queue
failed to begin transaction for next crons
PoolTimeout: couldn't get a connection after 15.00 sec
AdminShutdown: terminating connection due to administrator command
```

This failure usually indicates the LangGraph hosted rollout or queue deployment
did not become ready. It is different from a Python import error or app startup
exception. The old active revision should continue serving traffic unless the
platform has already promoted the failed revision.

Useful checks:

```bash
set -a
source .env >/dev/null 2>&1
set +a

LANGSMITH_API_KEY="$LANGSMITH_API_KEY_PROD" \
  uv run langgraph deploy logs \
  --deployment-id fcbdc143-a1ac-4eb0-a8d0-bedbcc49e3ed \
  --limit 100

LANGSMITH_API_KEY="$LANGSMITH_API_KEY_PROD" \
  uv run langgraph deploy logs \
  --deployment-id fcbdc143-a1ac-4eb0-a8d0-bedbcc49e3ed \
  --level error \
  --limit 100
```

If the rollout fails with no application error logs and local import checks pass,
collect the deployment ID, revision ID, commit SHA, and log excerpts before
contacting LangChain/LangSmith support.

## Inspecting Threads And Runs

Use the deployed LangGraph SDK endpoint to inspect production thread state:

```bash
set -a
source .env >/dev/null 2>&1
set +a

uv run python - <<'PY'
import asyncio
import os
from langgraph_sdk import get_client

THREAD_ID = "replace-with-thread-id"
URL = "https://nascompany-open-swe-5786464fff6d52fdb4f32c80d541067d.aws.us.langgraph.app"
API_KEY = os.environ["LANGSMITH_API_KEY_PROD"]

async def main():
    client = get_client(url=URL, api_key=API_KEY)
    thread = await client.threads.get(THREAD_ID)
    runs = await client.runs.list(THREAD_ID, limit=10)
    state = await client.threads.get_state(THREAD_ID)
    print("thread status:", thread.get("status"))
    print("thread error:", thread.get("error"))
    print("latest runs:", [(r.get("run_id"), r.get("status")) for r in runs])
    print("state keys:", list(state.keys()))

asyncio.run(main())
PY
```

Useful thread metadata keys:

```text
sandbox_id
sandbox_creating_at
latest_run_id
latest_run_status
```

## What To Update When URLs Move

If the backend deployment URL changes:

1. Update the `ui/vercel.json` rewrite destination.
2. Redeploy Vercel.
3. Update backend `LANGGRAPH_URL` if it points at the old backend URL.
4. Re-check webhook URLs if any production webhook points directly at the old
   backend URL.

If the frontend Vercel URL changes:

1. Update backend `DASHBOARD_BASE_URL`.
2. Update backend `DASHBOARD_API_BASE_URL` when using same-origin rewrite mode.
3. Update backend `DASHBOARD_ALLOWED_ORIGINS`.
4. Update the GitHub App dashboard callback URL.
5. Redeploy or restart the backend.

If the GitHub org gate changes:

1. Update `ALLOWED_GITHUB_ORGS`.
2. Confirm the GitHub App has Organization Members read permission.
3. Confirm the installation has approved that permission.
4. Redeploy or restart the backend.

If the sandbox provider changes:

1. Update `SANDBOX_TYPE`.
2. Add provider-specific credentials.
3. For `langsmith`, set `DEFAULT_SANDBOX_SNAPSHOT_ID`.
4. For `modal`, set `MODAL_TOKEN_ID`, `MODAL_TOKEN_SECRET`, and `MODAL_APP_NAME`.
5. Re-check `agent/utils/sandbox.py` and the provider adapter in `agent/integrations/`.

## Local Notes

Local-only deployment notes may exist under `.local-deployment-notes/`. They can
include incident-specific details, temporary scripts, or copied provider output,
but they must stay untracked and must not contain secrets.
