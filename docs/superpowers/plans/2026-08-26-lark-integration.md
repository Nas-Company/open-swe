# First-Class Lark Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let authorized Nas Company members invoke `@openswe` from Lark group chats or DMs and receive threaded, image-aware agent work and approval cards.

**Architecture:** Add a Lark-specific adapter to the existing FastAPI/LangGraph deployment. The adapter verifies webhook events, resolves tenant-scoped user identity and a repository URL, dispatches the existing agent, and exposes Lark reply/read tools; GitHub authorization and Modal sandbox execution remain unchanged.

**Tech Stack:** Python 3.11+, FastAPI, `lark-oapi`, httpx, LangGraph SDK/Store, pytest, pytest-asyncio, ruff

**Spec:** `docs/superpowers/specs/2026-08-26-lark-integration-design.md`

## Global Constraints

- Bot display name is exactly `openswe`.
- Group messages require a structured bot mention; direct messages do not.
- Every bot response is a reply beneath the invocation's Lark root message.
- A GitHub repository or PR URL is mandatory; no profile/team default fallback is allowed.
- Only active `Nas-Company` GitHub organization members may invoke the bot.
- Only raster image attachments are ingested in the first release.
- Lark credentials and tenant tokens never enter Modal or an LLM prompt.
- Existing GitHub, Slack, Linear, dashboard, LangGraph, and Modal behavior must remain intact.
- Every task follows red-green-refactor and ends with focused tests plus a commit.

---

## File structure

New production files:

- `agent/utils/lark.py` — configuration, normalized event/message types, Lark SDK/HTTP boundary, token cache, replies, thread reads, and image downloads.
- `agent/utils/lark_events.py` — persistent event-id idempotency state machine.
- `agent/dashboard/lark_oauth.py` — Lark authorization-code flow and verified identity model.
- `agent/webhooks/lark.py` — invocation normalization, authorization, context/repo/image resolution, queue/dispatch behavior.
- `agent/tools/lark_thread_reply.py` — Lark reply and approval-card agent tool.
- `agent/tools/lark_read_thread_messages.py` — Lark thread-reading agent tool.

Modified production files:

- `pyproject.toml`, `uv.lock` — add the official SDK.
- `agent/dashboard/user_mappings.py` — tenant-scoped Lark identity index.
- `agent/dashboard/routes.py` — Lark OAuth routes and mapping status.
- `agent/webapp.py` — thin webhook/card routes and thread helpers.
- `agent/tools/__init__.py`, `agent/server.py` — curated Lark tools and source prompt wiring.
- `agent/middleware/check_message_queue.py` — Lark follow-up handoff wording where needed.
- `agent/middleware/workflow_push_guard.py`, `agent/middleware/plan_mode.py` — Lark approval delivery.
- `agent/middleware/notify_step_limit.py`, `agent/utils/auth.py` — explicit terminal Lark failures.
- `README.md`, `INSTALLATION.md` — exact Lark console, permission, URL, and environment setup.

New tests:

- `tests/test_lark_utils.py`
- `tests/test_lark_events.py`
- `tests/test_lark_user_mappings.py`
- `tests/test_lark_oauth.py`
- `tests/test_lark_webhook.py`
- `tests/test_lark_invocation.py`
- `tests/test_lark_tools.py`
- `tests/test_lark_approvals.py`
- `tests/test_lark_integration.py`

---

### Task 1: Lark SDK boundary and normalized types

**Files:**
- Modify: `pyproject.toml`
- Modify: `uv.lock`
- Create: `agent/utils/lark.py`
- Create: `tests/test_lark_utils.py`

**Interfaces:**
- Consumes: `agent.utils.http.DEFAULT_HTTP_TIMEOUT`.
- Produces: `LarkConfig`, `LarkSender`, `LarkMessage`, `LarkEvent`, `LarkApiError`, `lark_configured()`, `parse_lark_event(body: bytes)`, `get_lark_tenant_token()`, `get_lark_user(open_id: str)`, `fetch_lark_thread(chat_id: str, root_message_id: str)`, `download_lark_image(message_id: str, image_key: str)`, and `reply_to_lark_message(root_message_id: str, content: dict[str, object], msg_type: str = "text")`.

- [ ] **Step 1: Add failing normalized-event tests**

```python
def test_parse_lark_event_normalizes_message() -> None:
    event = parse_lark_event(_message_event(chat_type="group"))
    assert event.event_id == "evt-1"
    assert event.tenant_key == "tenant-1"
    assert event.message.root_message_id == "om-root"
    assert event.message.mentions == ("ou_bot",)


def test_lark_configured_requires_every_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(lark, "LARK_APP_SECRET", "")
    assert lark.lark_configured() is False
```

- [ ] **Step 2: Run tests and verify the missing module failure**

Run: `uv run pytest -vvv tests/test_lark_utils.py`

Expected: collection fails with `ModuleNotFoundError: No module named 'agent.utils.lark'`.

- [ ] **Step 3: Add the official SDK and normalized immutable models**

Add `"lark-oapi>=1.6.9"` to dependencies and run `uv lock`. Implement frozen dataclasses and strict payload parsing:

```python
@dataclass(frozen=True)
class LarkMessage:
    message_id: str
    root_message_id: str
    parent_message_id: str
    chat_id: str
    chat_type: str
    message_type: str
    text: str
    mentions: tuple[str, ...]
    image_keys: tuple[str, ...]


@dataclass(frozen=True)
class LarkEvent:
    event_id: str
    tenant_key: str
    sender: LarkSender
    message: LarkMessage
```

Use the SDK event dispatcher for validation/decryption, but keep the rest of Open SWE dependent on normalized dataclasses rather than SDK-generated classes. Build one async client boundary with tenant-token single-flight refresh and bounded 401/429/5xx retries.

- [ ] **Step 4: Add API boundary tests**

```python
async def test_reply_uses_root_message_and_tenant_token(monkeypatch: pytest.MonkeyPatch) -> None:
    transport = MockTransport([token_response(), reply_response("om-reply")])
    monkeypatch.setattr(lark, "_http_client", lambda: AsyncClient(transport=transport))
    result = await reply_to_lark_message("om-root", {"text": "Working"})
    assert result.message_id == "om-reply"
    assert transport.requests[-1].url.path.endswith("/om-root/reply")


async def test_429_honors_retry_after(monkeypatch: pytest.MonkeyPatch) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr(lark.asyncio, "sleep", sleeps.append)
    # first reply is 429, second succeeds
    assert (await reply_to_lark_message("om-root", {"text": "ok"})).ok
    assert sleeps == [1.0]
```

- [ ] **Step 5: Run focused tests and lint**

Run: `uv run pytest -vvv tests/test_lark_utils.py && uv run ruff check agent/utils/lark.py tests/test_lark_utils.py`

Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml uv.lock agent/utils/lark.py tests/test_lark_utils.py
git commit -m "feat: add Lark API boundary"
```

---

### Task 2: Persistent Lark event idempotency

**Files:**
- Create: `agent/utils/lark_events.py`
- Create: `tests/test_lark_events.py`

**Interfaces:**
- Consumes: `langgraph_sdk.get_client` and Lark event IDs.
- Produces: `LarkEventRecord`, `claim_lark_event(event_id: str, thread_id: str) -> ClaimResult`, `mark_lark_event_dispatched(event_id: str, run_id: str)`, and `mark_lark_event_failed(event_id: str, error_code: str)`.

- [ ] **Step 1: Write state-machine tests**

```python
async def test_first_delivery_claims_and_second_delivery_skips(fake_store) -> None:
    first = await claim_lark_event("evt-1", "thread-1")
    second = await claim_lark_event("evt-1", "thread-1")
    assert first.status == "claimed"
    assert second.status == "in_progress"


async def test_dispatched_event_is_idempotent(fake_store) -> None:
    await claim_lark_event("evt-1", "thread-1")
    await mark_lark_event_dispatched("evt-1", "run-1")
    assert (await claim_lark_event("evt-1", "thread-1")).status == "dispatched"
```

- [ ] **Step 2: Verify red**

Run: `uv run pytest -vvv tests/test_lark_events.py`

Expected: missing module failure.

- [ ] **Step 3: Implement the event record and stale-claim behavior**

Use namespace `("lark_events",)` and keys equal to event IDs. Records include status,
thread ID, run ID, attempts, `claimed_at`, `updated_at`, and safe `error_code`. Protect
same-process races with a per-event `asyncio.Lock`; re-read after acquiring the lock. Reclaim
`dispatching` records older than `LARK_EVENT_CLAIM_TIMEOUT_SECONDS` and failed records with
attempts below `LARK_EVENT_MAX_ATTEMPTS`.

- [ ] **Step 4: Add concurrency and reclaim tests**

```python
async def test_concurrent_claim_has_one_owner(fake_store) -> None:
    results = await asyncio.gather(*[claim_lark_event("evt-1", "t") for _ in range(10)])
    assert [r.status for r in results].count("claimed") == 1


async def test_stale_dispatch_is_reclaimed(fake_store, frozen_clock) -> None:
    await claim_lark_event("evt-1", "t")
    frozen_clock.advance(seconds=LARK_EVENT_CLAIM_TIMEOUT_SECONDS + 1)
    assert (await claim_lark_event("evt-1", "t")).status == "claimed"
```

- [ ] **Step 5: Run and commit**

Run: `uv run pytest -vvv tests/test_lark_events.py`

```bash
git add agent/utils/lark_events.py tests/test_lark_events.py
git commit -m "feat: deduplicate Lark webhook events"
```

---

### Task 3: Tenant-scoped Lark user mappings

**Files:**
- Modify: `agent/dashboard/user_mappings.py`
- Create: `tests/test_lark_user_mappings.py`

**Interfaces:**
- Consumes: existing mapping records keyed by GitHub login.
- Produces: `cached_login_for_lark_id(tenant_key: str | None, open_id: str | None)`, `login_for_lark_id(...)`, and new optional arguments on `upsert_mapping`: `lark_tenant_key`, `lark_open_id`, `lark_union_id`, `lark_display_name`.

- [ ] **Step 1: Add tenant-isolation and compatibility tests**

```python
def test_lark_open_id_is_tenant_scoped() -> None:
    prime_cache([mapping("alice", "tenant-a", "ou-1"), mapping("bob", "tenant-b", "ou-1")])
    assert cached_login_for_lark_id("tenant-a", "ou-1") == "alice"
    assert cached_login_for_lark_id("tenant-b", "ou-1") == "bob"


async def test_upsert_preserves_existing_slack_mapping(fake_store) -> None:
    record = await upsert_mapping(
        github_login="alice", work_email="alice@nas.io",
        lark_tenant_key="tenant-a", lark_open_id="ou-1", source="lark_oauth",
    )
    assert record["slack_user_id"] == "U1"
```

- [ ] **Step 2: Verify red**

Run: `uv run pytest -vvv tests/test_lark_user_mappings.py`

Expected: `cached_login_for_lark_id` import failure.

- [ ] **Step 3: Extend indexes and record types**

Add `_by_lark_id: dict[tuple[str, str], dict[str, Any]]`, normalize both fields, index/deindex
them with existing email/Slack indexes, and expand `MappingSource` with `"lark_oauth"`. Preserve
all existing fields during partial upserts.

- [ ] **Step 4: Run mapping regression tests and commit**

Run: `uv run pytest -vvv tests/test_lark_user_mappings.py tests/test_dashboard_github_oauth_mapping.py tests/test_slack_oauth.py`

```bash
git add agent/dashboard/user_mappings.py tests/test_lark_user_mappings.py
git commit -m "feat: map Lark users to GitHub identities"
```

---

### Task 4: Self-service Lark OAuth

**Files:**
- Create: `agent/dashboard/lark_oauth.py`
- Modify: `agent/dashboard/routes.py`
- Create: `tests/test_lark_oauth.py`

**Interfaces:**
- Consumes: dashboard GitHub session, `upsert_mapping`, dashboard base URL.
- Produces: `LarkIdentity`, `lark_oauth_configured()`, `build_lark_authorize_url()`, `exchange_lark_code()`, `fetch_lark_identity()`, `verify_lark_tenant()`, `GET /dashboard/api/lark/login`, and `GET /dashboard/api/lark/callback`.

- [ ] **Step 1: Add OAuth state and tenant tests**

```python
def test_build_authorize_url_has_state_and_redirect(monkeypatch: pytest.MonkeyPatch) -> None:
    url = build_lark_authorize_url(redirect_uri="https://api.example/lark/callback", state="nonce")
    assert parse_qs(urlparse(url).query)["state"] == ["nonce"]


def test_verify_lark_tenant_rejects_other_tenant() -> None:
    with pytest.raises(HTTPException) as exc:
        verify_lark_tenant(LarkIdentity(open_id="ou-1", tenant_key="other", email=None, name=None))
    assert exc.value.status_code == 403
```

- [ ] **Step 2: Verify red**

Run: `uv run pytest -vvv tests/test_lark_oauth.py`

- [ ] **Step 3: Implement OAuth using the Slack route pattern**

Use a dedicated `osw_lark_oauth_state` HttpOnly cookie scoped to `/dashboard/api/lark`, the
existing signed state helpers, and the logged-in dashboard GitHub session. Callback order:
cookie/state check, code exchange, verified identity fetch, tenant enforcement, mapping upsert,
cookie clear, dashboard redirect. Never accept an asserted open ID from query parameters.

- [ ] **Step 4: Add route tests**

```python
async def test_callback_maps_verified_lark_identity(client, github_session, monkeypatch) -> None:
    monkeypatch.setattr(routes, "fetch_lark_identity", AsyncMock(return_value=LARK_ALICE))
    response = await client.get("/dashboard/api/lark/callback?code=ok&state=nonce", cookies=state_cookie())
    assert response.status_code == 307
    assert (await get_mapping("alice"))["lark_open_id"] == "ou-alice"
```

- [ ] **Step 5: Run and commit**

Run: `uv run pytest -vvv tests/test_lark_oauth.py tests/test_slack_oauth.py`

```bash
git add agent/dashboard/lark_oauth.py agent/dashboard/routes.py tests/test_lark_oauth.py
git commit -m "feat: add self-service Lark account linking"
```

---

### Task 5: Lark webhook routing and acceptance policy

**Files:**
- Modify: `agent/webapp.py`
- Create: `tests/test_lark_webhook.py`

**Interfaces:**
- Consumes: `parse_lark_event` and `claim_lark_event`.
- Produces: `generate_thread_id_from_lark(tenant_key, chat_id, root_message_id)`, `_process_lark_event(event: LarkEvent)`, `POST /webhooks/lark`, `GET /webhooks/lark`, and the authenticated `POST /webhooks/lark/card` intake route.

- [ ] **Step 1: Add route-policy tests**

```python
async def test_group_requires_structured_bot_mention(client, monkeypatch) -> None:
    response = await client.post("/webhooks/lark", content=group_event(text="@openswe hi", mentions=[]))
    assert response.json()["status"] == "ignored"


async def test_dm_without_mention_is_accepted(client, process_mock) -> None:
    response = await client.post("/webhooks/lark", content=dm_event("check repo"))
    assert response.json()["status"] == "accepted"
    process_mock.assert_called_once()
```

- [ ] **Step 2: Verify red**

Run: `uv run pytest -vvv tests/test_lark_webhook.py`

- [ ] **Step 3: Implement thin routes**

Handle URL verification before message normalization. Reject unconfigured service, invalid
envelope, wrong tenant, missing fields, bot authors, non-message events, and group messages
without the bot open ID in structured mentions. Compute the deterministic thread ID, claim the
event, and schedule `_process_lark_event(event)` only for a claimed event. Implement that helper
as a lazy import of `agent.webhooks.lark.process_lark_mention`; this keeps the intake boundary
independently testable before Task 6 creates the processor module. Route tests patch the helper
with an `AsyncMock`. Return success for already-dispatched or in-progress duplicates.

- [ ] **Step 4: Add dedupe and deterministic-ID tests**

```python
def test_lark_thread_id_is_stable_and_tenant_scoped() -> None:
    assert generate_thread_id_from_lark("t1", "c1", "m1") == generate_thread_id_from_lark("t1", "c1", "m1")
    assert generate_thread_id_from_lark("t1", "c1", "m1") != generate_thread_id_from_lark("t2", "c1", "m1")


async def test_replayed_event_does_not_schedule_twice(client, process_mock) -> None:
    await client.post("/webhooks/lark", content=valid_event("evt-1"))
    await client.post("/webhooks/lark", content=valid_event("evt-1"))
    assert process_mock.call_count == 1
```

- [ ] **Step 5: Run and commit**

Run: `uv run pytest -vvv tests/test_lark_webhook.py`

```bash
git add agent/webapp.py tests/test_lark_webhook.py
git commit -m "feat: receive signed Lark webhooks"
```

---

### Task 6: Invocation context, authorization, images, queue, and dispatch

**Files:**
- Create: `agent/webhooks/lark.py`
- Modify: `agent/middleware/check_message_queue.py`
- Create: `tests/test_lark_invocation.py`

**Interfaces:**
- Consumes: Tasks 1-5, existing `_is_repo_allowed`, GitHub org checks, `dispatch_agent_run`, `upsert_agent_thread_owner_metadata`, and queue behavior.
- Produces: `process_lark_mention(event: LarkEvent)`, `select_lark_context()`, `extract_lark_repo_refs()`, and `build_lark_prompt()`.

- [ ] **Step 1: Add repository/context tests**

```python
def test_no_repo_never_uses_default() -> None:
    result = extract_lark_repo_refs([message("please fix it")])
    assert result.status == "missing"


def test_pr_url_selects_parent_repo() -> None:
    result = extract_lark_repo_refs([message("https://github.com/Nas-Company/nas-e2e/pull/44")])
    assert result.repo == {"owner": "Nas-Company", "name": "nas-e2e"}
```

- [ ] **Step 2: Add dispatch and mapping tests**

```python
async def test_mapped_member_dispatches_one_lark_run(harness) -> None:
    await process_lark_mention(harness.event(images=("img-1",)))
    run = harness.runs.single()
    assert run.configurable["source"] == "lark"
    assert run.configurable["github_login"] == "alice"
    assert run.configurable["repo"] == {"owner": "Nas-Company", "name": "repo"}
    assert run.content_has_image()


async def test_unmapped_member_gets_connect_link_and_no_run(harness) -> None:
    await process_lark_mention(harness.event())
    assert "Connect Lark account" in harness.lark_replies.single().text
    assert harness.runs.count == 0
```

- [ ] **Step 3: Verify red**

Run: `uv run pytest -vvv tests/test_lark_invocation.py`

- [ ] **Step 4: Implement the full invocation pipeline**

Fetch sender and bounded thread context; resolve tenant/open-ID then unique email; validate a
usable GitHub principal, active org membership, exactly one repo reference, allowlist, and App
installation access; download and validate images; build Lark-specific prompt blocks; upsert
thread repo/owner/source metadata; queue a follow-up when a run is active or dispatch exactly one
new run; mark the event dispatched only after receiving its run ID. Every stopped branch posts a
specific Lark reply.

- [ ] **Step 5: Add image and active-run queue tests**

```python
async def test_oversized_image_is_skipped_with_warning(harness) -> None:
    harness.images["img-1"] = b"x" * (LARK_IMAGE_MAX_BYTES + 1)
    await process_lark_mention(harness.event(images=("img-1",)))
    assert "too large" in harness.lark_replies.last().text


async def test_followup_queues_into_active_thread(harness) -> None:
    harness.active_run = "run-1"
    await process_lark_mention(harness.followup_event())
    assert harness.queued.single()["content"]["source"] == "lark"
    assert harness.runs.count == 0
```

- [ ] **Step 6: Run and commit**

Run: `uv run pytest -vvv tests/test_lark_invocation.py tests/test_check_message_queue.py`

```bash
git add agent/webhooks/lark.py agent/middleware/check_message_queue.py tests/test_lark_invocation.py
git commit -m "feat: dispatch Open SWE from Lark"
```

---

### Task 7: Lark agent tools and source-aware failures

**Files:**
- Create: `agent/tools/lark_thread_reply.py`
- Create: `agent/tools/lark_read_thread_messages.py`
- Modify: `agent/tools/__init__.py`
- Modify: `agent/server.py`
- Modify: `agent/utils/auth.py`
- Modify: `agent/middleware/notify_step_limit.py`
- Create: `tests/test_lark_tools.py`

**Interfaces:**
- Consumes: `configurable.lark_thread` and Task 1 API functions.
- Produces: curated `lark_thread_reply(message: str, options: list[str] | None = None)` and `lark_read_thread_messages()` tools plus source-aware failure delivery.

- [ ] **Step 1: Add tool tests**

```python
async def test_reply_uses_configured_root(runtime_config, post_mock) -> None:
    result = await lark_thread_reply.ainvoke({"message": "Done"}, runtime_config)
    assert result["success"] is True
    post_mock.assert_awaited_once_with("om-root", {"text": "Done"}, msg_type="text")


async def test_read_tool_returns_normalized_thread(runtime_config, fetch_mock) -> None:
    result = await lark_read_thread_messages.ainvoke({}, runtime_config)
    assert result["messages"] == [{"author": "Alice", "text": "Fix it", "message_id": "om-1"}]
```

- [ ] **Step 2: Verify red**

Run: `uv run pytest -vvv tests/test_lark_tools.py`

- [ ] **Step 3: Implement tools and curated wiring**

Read only IDs from `configurable.lark_thread`; never accept caller-supplied credentials. Add the
tools to exports and `get_agent`'s tool list. Build Lark-originated prompts that require Lark
tools and prohibit Slack/Linear delivery. Extend auth and step-limit failure switches with
`source == "lark"` and post to the configured root message.

- [ ] **Step 4: Run tool and source regression tests**

Run: `uv run pytest -vvv tests/test_lark_tools.py tests/test_slack_thread_reply_tool.py tests/test_schedule_thread_wakeup.py`

- [ ] **Step 5: Commit**

```bash
git add agent/tools/lark_thread_reply.py agent/tools/lark_read_thread_messages.py agent/tools/__init__.py agent/server.py agent/utils/auth.py agent/middleware/notify_step_limit.py tests/test_lark_tools.py
git commit -m "feat: add Lark agent communication tools"
```

---

### Task 8: Interactive plan and workflow approvals

**Files:**
- Modify: `agent/tools/lark_thread_reply.py`
- Modify: `agent/webapp.py`
- Modify: `agent/middleware/plan_mode.py`
- Modify: `agent/middleware/workflow_push_guard.py`
- Create: `tests/test_lark_approvals.py`

**Interfaces:**
- Consumes: existing plan/workflow approval stores and `lark_thread` owner metadata.
- Produces: `build_lark_approval_card()`, `process_lark_card_action()`, and Lark delivery branches in both approval middlewares.

- [ ] **Step 1: Add bounded-card and owner tests**

```python
def test_card_contains_fingerprint_but_no_secret() -> None:
    card = build_lark_approval_card("Approve workflow push?", "workflow_push", "fp-1")
    raw = json.dumps(card)
    assert "fp-1" in raw
    assert "LARK_APP_SECRET" not in raw


async def test_wrong_user_cannot_approve(harness) -> None:
    result = await process_lark_card_action(harness.action(actor="ou-other"))
    assert result.toast_type == "error"
    assert harness.approvals.decisions == []
```

- [ ] **Step 2: Verify red**

Run: `uv run pytest -vvv tests/test_lark_approvals.py`

- [ ] **Step 3: Implement approval delivery and atomic callbacks**

For Lark threads, post interactive cards with type, thread ID, fingerprint, and Approve/Reject
action only. On callback, verify the envelope, tenant, mapped actor, stored thread owner, pending
record, fingerprint, expiry, and unused state. Atomically decide, update the card terminal state,
and queue exactly one synthetic follow-up. Preserve existing Slack blocks unchanged.

- [ ] **Step 4: Add replay, expiry, mismatch, and happy-path tests**

```python
@pytest.mark.parametrize("case", ["expired", "replayed", "fingerprint_mismatch"])
async def test_invalid_approval_never_resumes(case: str, harness) -> None:
    assert (await process_lark_card_action(harness.action(case=case))).accepted is False
    assert harness.queued.count == 0


async def test_owner_approval_resumes_once(harness) -> None:
    assert (await process_lark_card_action(harness.action(actor="ou-owner"))).accepted
    assert harness.queued.count == 1
```

- [ ] **Step 5: Run and commit**

Run: `uv run pytest -vvv tests/test_lark_approvals.py tests/test_plan_mode.py tests/test_workflow_push_guard.py`

```bash
git add agent/tools/lark_thread_reply.py agent/webapp.py agent/middleware/plan_mode.py agent/middleware/workflow_push_guard.py tests/test_lark_approvals.py
git commit -m "feat: approve protected actions from Lark"
```

---

### Task 9: Health, installation documentation, and integration coverage

**Files:**
- Modify: `agent/webapp.py`
- Modify: `README.md`
- Modify: `INSTALLATION.md`
- Create: `tests/test_lark_integration.py`

**Interfaces:**
- Consumes: all previous tasks.
- Produces: non-secret `lark_configured` health field, complete console configuration guide, and a mocked end-to-end acceptance test.

- [ ] **Step 1: Add the full acceptance test**

```python
async def test_lark_event_to_agent_to_threaded_reply(lark_harness) -> None:
    response = await lark_harness.post_event(group_mention_with_image_and_pr())
    assert response.status_code == 200
    assert lark_harness.runs.count == 1
    assert lark_harness.runs.single().thread_id == expected_lark_thread_id()
    assert lark_harness.runs.single().has_image

    await lark_harness.invoke_agent_reply("PR opened: https://github.com/Nas-Company/repo/pull/1")
    assert lark_harness.replies.single().root_message_id == "om-root"

    await lark_harness.post_event(group_mention_with_image_and_pr(event_id="evt-1"))
    assert lark_harness.runs.count == 1
```

- [ ] **Step 2: Verify red**

Run: `uv run pytest -vvv tests/test_lark_integration.py`

- [ ] **Step 3: Add health and exact installation instructions**

Document environment variables, webhook URL, card callback URL, OAuth redirect URL, bot name,
event subscription, exact current permission identifiers, version publication, group install,
and group/DM smoke-test commands. Add health output like `{"status": "healthy", "lark_configured": true}` without secret values.

- [ ] **Step 4: Run the Lark suite and lint**

Run: `uv run pytest -vvv tests/test_lark_*.py && uv run ruff check agent tests`

Expected: all Lark tests pass and ruff reports no errors.

- [ ] **Step 5: Commit**

```bash
git add agent/webapp.py README.md INSTALLATION.md tests/test_lark_integration.py
git commit -m "docs: add Lark production setup and acceptance coverage"
```

---

### Task 10: Full regression, deployment, and live smoke test

**Files:**
- Modify only files required by failures found in this task.

**Interfaces:**
- Consumes: completed implementation and Nas Company Lark/LangSmith administration.
- Produces: a green repository, deployed LangSmith revision, configured/published Lark app, and evidence for every production smoke case.

- [ ] **Step 1: Run the complete local verification suite**

Run: `make test`

Expected: every repository unit test passes.

- [ ] **Step 2: Run formatting and lint verification**

Run: `make lint`

Expected: ruff check and format diff both pass.

- [ ] **Step 3: Audit spec coverage**

Run:

```bash
rg -n "LARK_|/webhooks/lark|lark_thread_reply|lark_read_thread_messages|lark_open_id" agent tests README.md INSTALLATION.md
git diff origin/main...HEAD --check
```

Inspect evidence for group/DM policy, mandatory repo selection, images, tenant-scoped mapping,
org gate, idempotency, tool wiring, failure replies, approvals, docs, and tests. Fix and retest any
missing requirement rather than narrowing the spec.

- [ ] **Step 4: Commit final local fixes**

```bash
git add agent tests README.md INSTALLATION.md pyproject.toml uv.lock
git commit -m "fix: complete Lark integration verification"
```

Skip the commit when verification required no changes.

- [ ] **Step 5: Configure LangSmith secrets and deploy**

Using the signed-in LangSmith UI, add the five required Lark environment variables and submit a
new revision. This is a persistent security-sensitive credential configuration, so request
action-time confirmation immediately before the final Submit. Wait for Currently deployed, then
verify `/ok` returns `{"ok": true}` and `/info` reports the new revision ID.

- [ ] **Step 6: Configure and publish the Lark app**

Using the signed-in Lark developer console, configure the documented webhook, callback, OAuth
redirect, events, and permissions; publish the app version and install it for the Nas Company
tenant. Request action-time confirmation before submitting new credentials/permissions or the
final publication when required by Computer Use policy.

- [ ] **Step 7: Run and record production smoke tests**

Verify in a staging group and DM:

1. Group `@openswe` and DM both dispatch.
2. Missing repository receives the required question.
3. A differing-email user can self-link.
4. A screenshot reaches a vision-capable run.
5. Progress and final messages stay threaded.
6. An approval card resumes only for its owner and only once.
7. A harmless request creates/updates a PR in an enabled Nas Company repository.
8. Replaying the same webhook event creates no duplicate run.

- [ ] **Step 8: Commit deployment documentation evidence**

Update `INSTALLATION.md` only if live setup revealed a correction, then commit:

```bash
git add INSTALLATION.md
git commit -m "docs: record verified Lark deployment setup"
```

The task is complete only when local verification, live deployment, Lark publication, and all
eight smoke cases have authoritative evidence.
