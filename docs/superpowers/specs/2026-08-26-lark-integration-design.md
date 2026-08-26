# Lark Integration Design

## Summary

Add Lark as a first-class Open SWE channel. Nas Company members can invoke the bot in a
group with `@openswe` or send it a direct message. Open SWE receives signed Lark webhooks,
maps the Lark member to GitHub, verifies active `Nas-Company` membership, requires a GitHub
repository or pull request link in the Lark conversation, and dispatches the existing
LangGraph agent. All bot output stays in a Lark thread beneath the invoking message.

The adapter runs in `agent.webapp:app` and reuses the current LangGraph threads, message
queue, GitHub authorization, dashboard, and Modal sandbox. It does not add a bridge service
or WebSocket worker. The official Lark Python SDK supports webhook transport, normalized
events, replies, media, and card callbacks:
<https://github.com/larksuite/oapi-sdk-python/blob/v2_main/doc/channel.md>.

## Requirements

- Accept structured `@openswe` mentions in group chats and every human message in DMs.
- Reply only in a thread beneath the invocation's root message.
- Require exactly one GitHub repository or PR link in the selected Lark thread context.
- Support attached images, with count, MIME, and byte limits.
- Map Lark identities to GitHub even when their emails differ.
- Allow only mapped GitHub users who are active `Nas-Company` organization members.
- Provide interactive Approve and Reject cards for protected actions.
- Make webhook retries idempotent and never silently ignore an actionable rejection.
- Preserve existing GitHub, Slack, Linear, dashboard, LangGraph, and Modal behavior.

There is no default-repository fallback and no arbitrary file support in the first release.

## User flows

### Invocation

1. A user sends `@openswe <request> <GitHub URL>` in a group, or the same request in a DM.
2. The bot immediately acknowledges in a Lark thread.
3. Open SWE sends questions, progress, approvals, and the final result to that thread.
4. The final result includes the PR link when work creates or updates one.

If the context has no repository, the bot asks for a GitHub repository or PR link. If it
contains multiple distinct repositories, it asks the user to choose one rather than guessing.

### Account linking

Open SWE first checks a tenant-scoped Lark identity mapping, then tries a unique verified work
email match. If neither resolves, it posts a Connect Lark account link. The user signs into the
dashboard with GitHub and completes Lark OAuth, proving ownership of both identities before a
mapping is stored.

## Architecture

### Components

1. **Webhook routes** receive URL verification, message events, and card callbacks; the
   official SDK verifies and decrypts them before any processing.
2. **Lark API client** caches tenant tokens, reads member/thread context, downloads images,
   and posts threaded messages and cards.
3. **Event adapter** normalizes Lark events, applies group/DM rules, selects context, resolves
   the repository and identity, and dispatches LangGraph.
4. **Identity linking** extends dashboard mappings with tenant-scoped Lark IDs and adds a Lark
   OAuth authorization-code flow.
5. **Agent tools** add `lark_thread_reply` and `lark_read_thread_messages` to the curated main
   agent tool list.
6. **Approval adapter** renders bounded Lark cards and validates callbacks against pending
   server-side approval state.

Focused modules:

- `agent/utils/lark.py`: SDK boundary, API calls, event parsing, formatting, and media.
- `agent/webhooks/lark.py`: invocation processing and LangGraph dispatch.
- `agent/tools/lark_thread_reply.py`: threaded replies and cards.
- `agent/tools/lark_read_thread_messages.py`: normalized context reads.
- `agent/dashboard/lark_oauth.py`: OAuth and verified Lark identity parsing.
- `agent/webapp.py` and `agent/dashboard/routes.py`: route wiring only.

### Communication protocol

| Sender | Receiver | Transport | Data |
|---|---|---|---|
| Lark user | Lark Platform | Lark IM | Mention/DM, GitHub URL, optional images |
| Lark | `POST /webhooks/lark` | HTTPS JSON webhook | Message event, tenant, sender, chat/message IDs, mentions, content |
| Open SWE | Lark APIs | HTTPS JSON/binary | Tenant token, member profile, thread messages, images |
| Open SWE | Store and GitHub | SDK/HTTPS | Mapping, idempotency, org membership, repo access |
| Open SWE | LangGraph server | LangGraph SDK/HTTPS | Thread metadata, prompt blocks, repo, GitHub identity, Lark context |
| Agent | Modal and GitHub | Modal API, Git HTTPS, GitHub REST | Sandbox and repository operations |
| Lark tools | Lark Reply API | HTTPS JSON | Threaded text, rich post, or interactive card |
| Lark | `POST /webhooks/lark/card` | HTTPS JSON callback | Actor, action, and bounded approval fingerprint |

The webhook acknowledges only after validation and a durable event record. Invalid signatures
or decryption failures never dispatch work. Long-running work continues as a LangGraph run.

### Thread identity and context

Derive a namespaced LangGraph thread ID from:

```text
lark:<tenant_key>:<chat_id>:<root_message_id>
```

The root is the Lark thread root when present, otherwise the invoking message. For each event,
read messages from the previous bot mention (or root) through the current message. Ignore later
messages, bot-authored messages, and unsupported types. Follow-ups arriving during a run enter
the existing LangGraph Store message queue with `source: "lark"`.

## Identity and authorization

Extend `user_mappings` records with optional `lark_tenant_key`, `lark_open_id`,
`lark_union_id`, and `lark_display_name`. The cache gains a `(tenant_key, open_id)` index;
an open ID is never considered globally unique.

Resolution order:

1. Active tenant/open-ID mapping.
2. Unique verified Lark email match to an active mapping.
3. Otherwise, self-service Lark OAuth and stop the current invocation.
4. Validate GitHub credentials using existing OAuth/App fallback behavior.
5. Check active `Nas-Company` membership.
6. Check the selected repository's Open SWE allowlist and GitHub App installation access.

Membership and repository access are checked on every invocation.

Dashboard endpoints under `/dashboard/api/lark`:

- `GET /login`: requires the GitHub dashboard session, stores signed OAuth state in a
  short-lived HttpOnly cookie, and redirects to Lark.
- `GET /callback`: verifies state, exchanges the code, resolves verified Lark identity,
  enforces `LARK_TENANT_KEY`, and maps it to the logged-in GitHub user.

No user can manually submit a Lark ID.

## Webhook security and idempotency

- Use the official dispatcher with `LARK_VERIFICATION_TOKEN` and `LARK_ENCRYPT_KEY`.
- Accept only `LARK_TENANT_KEY`.
- In groups, rely on structured mention IDs, not `@openswe` text matching. In DMs, accept
  human messages without a mention. Ignore bot/application authors.
- Persist event records keyed by Lark event ID with states `received`, `dispatching`,
  `dispatched`, and `failed`, plus timestamps, attempt count, thread ID, and run ID.
- Use a single-owner transition so concurrent duplicate deliveries cannot dispatch twice.
- Reclaim stale `dispatching` events after a bounded timeout; acknowledge `dispatched` retries.
- Mark an event dispatched only after LangGraph returns a run ID.
- Redact app secrets, tokens, OAuth codes, encrypted envelopes, and image bytes from logs.

## Repository and image handling

Parse only canonical `github.com/<owner>/<repo>` and PR URLs from text or rich-post context.
A PR resolves to its repository. Zero references asks for a URL; multiple repositories asks for
disambiguation; one proceeds through existing access gates.

Download protected Lark image resources server-side with a tenant token. Permit supported raster
MIME types and enforce count, per-image, and aggregate limits. Convert valid bytes to existing
multimodal content blocks. Protected URLs and tokens never enter the prompt. If the selected
model lacks vision support, continue with text and the existing warning.

## Agent tools

`lark_thread_reply` reads `configurable.lark_thread`, replies to the root message, and supports
text, rich posts, and approval cards. It returns a Lark message ID or a structured recoverable
error. `lark_read_thread_messages` returns normalized current-thread messages without exposing
credentials.

Lark-originated prompts explicitly require these tools for questions, progress, and completion
and prohibit Slack/Linear reply tools. Authentication, sandbox, and step-limit failure paths are
extended so Lark receives an explicit terminal reply.

## Interactive approvals

Pending approval state remains server-side. The Lark card displays a summary and carries only
the approval type, thread reference, and random one-time fingerprint. A callback must pass:

- Lark authenticity and tenant validation,
- mapped identity validation,
- original thread-owner validation,
- exact fingerprint/action match,
- expiry and unused checks.

The decision is recorded atomically, the card is made terminal, and one bounded follow-up is
queued to the existing agent thread. Replays, wrong users, mismatches, and expired actions are
rejected with a clear toast or threaded reply.

## Failure handling and observability

- Invalid webhooks return an authentication error with only safe metadata logged.
- Duplicate events return success without duplicate work.
- Missing identity, membership, repo, or access produces a specific threaded response.
- Lark 429/5xx responses use bounded exponential backoff and retry metadata.
- Expired tenant tokens refresh once under a process-local single-flight lock.
- LangGraph dispatch failures leave the event reclaimable; image failures preserve valid input.
- Unknown errors after a reply target is known produce a generic Lark message with a
  correlation ID while details remain in logs.

Trace safe event, thread, run, chat, user, repository, and Lark request IDs. Add counters for
received, ignored, deduplicated, dispatched, failed, reply results, link prompts, authorization
denials, and card decisions. Health/config status reports whether Lark settings exist without
returning their values.

## Configuration and Lark setup

Required LangSmith environment variables:

- `LARK_APP_ID`
- `LARK_APP_SECRET`
- `LARK_VERIFICATION_TOKEN`
- `LARK_ENCRYPT_KEY`
- `LARK_TENANT_KEY`

Derive the OAuth redirect from `DASHBOARD_API_BASE_URL` unless Lark needs an explicit
`LARK_OAUTH_REDIRECT_URI` override.

Lark setup requires the `openswe` bot capability, message webhook, card callback, OAuth redirect,
and permissions for group/DM receive, bot sends/cards, verified member profile/email, and message
image downloads. The exact permission identifiers are verified in the current developer console
during implementation and recorded in installation documentation. The app must be published and
installed for intended Nas Company users and chats.

## Verification

Unit tests cover webhook verification/decryption, tenant and mention rules, bot-loop rejection,
thread IDs, concurrent dedupe/reclaim, repo selection, identity/OAuth, queue behavior, images,
Lark tools, token refresh/backoff, approvals, and terminal failure replies.

Integration tests use a mocked Lark boundary and fake LangGraph client to prove that one event
creates exactly one deterministic run with normalized text/images, replies to the correct Lark
root, queues follow-ups, and resumes an approval once.

Production smoke tests cover group and DM invocation, missing-repo response, differing-email
account linking, screenshot ingestion, threaded output, an approval card, a harmless PR in an
enabled Nas Company repository, and duplicate webhook replay. Rollout is complete only when the
LangSmith revision is healthy, the Lark app is published with required URLs/permissions, and all
smoke tests pass.

## Rollout order

1. Lark SDK/API boundary, normalized events, and persistent idempotency.
2. User mapping and dashboard OAuth.
3. Webhook dispatch, repository selection, images, and message queue.
4. Agent reply/read tools and terminal failure notifications.
5. Approval cards and callbacks.
6. Installation documentation and permission checklist.
7. LangSmith secrets and deployment; Lark console configuration and publication.
8. Production smoke tests and observation of the first real invocation.
