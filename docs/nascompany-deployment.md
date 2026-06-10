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
   - Serves the graphs: `agent`, `reviewer`, `analyzer`, `scheduler`
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
Date: 2026-06-10
Revision: 36342154-2bfc-4213-9662-8d643ca0d72e
Commit: 5b6b2334376f463e27e4e1fc23916653cab58fbd
Verification: /info host_revision_id matched the revision, /health returned healthy
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
  ANTHROPIC_API_KEY or ANTHROPIC_AUTH_TOKEN / ANTHROPIC_BASE_URL
  OPENAI_API_KEY
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

Because production uses Modal as the command sandbox provider, LangSmith sandbox
snapshot variables may be empty. If `SANDBOX_TYPE` changes back to `langsmith`,
set `DEFAULT_SANDBOX_SNAPSHOT_ID` and the related snapshot resource settings.

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
6. Confirm the public backend reports the new host revision:

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
