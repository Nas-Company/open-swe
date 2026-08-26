from __future__ import annotations

from typing import Any

import pytest

from agent.dashboard import user_mappings as um


class _FakeStore:
    def __init__(self) -> None:
        self.items: dict[tuple[tuple[str, ...], str], dict[str, Any]] = {}

    async def get_item(self, namespace: list[str], key: str) -> dict[str, Any] | None:
        value = self.items.get((tuple(namespace), key))
        return {"value": dict(value)} if value is not None else None

    async def put_item(
        self,
        namespace: list[str],
        key: str,
        value: dict[str, Any],
    ) -> None:
        self.items[(tuple(namespace), key)] = dict(value)

    async def search_items(self, namespace: list[str], *, limit: int = 1000) -> dict[str, Any]:
        items = [
            {"value": dict(value)}
            for (stored_namespace, _), value in self.items.items()
            if stored_namespace == tuple(namespace)
        ]
        return {"items": items[:limit]}


class _FakeClient:
    def __init__(self, store: _FakeStore) -> None:
        self.store = store


@pytest.fixture
def fake_store(monkeypatch: pytest.MonkeyPatch) -> _FakeStore:
    store = _FakeStore()
    monkeypatch.setattr(um, "_client", lambda: _FakeClient(store))
    um.clear_cache()
    return store


def test_lark_open_id_is_tenant_scoped() -> None:
    um.prime_cache(
        [
            {
                "github_login": "alice",
                "work_email": "alice@nas.io",
                "lark_tenant_key": "tenant-a",
                "lark_open_id": "ou-1",
                "status": "active",
            },
            {
                "github_login": "bob",
                "work_email": "bob@nas.io",
                "lark_tenant_key": "tenant-b",
                "lark_open_id": "ou-1",
                "status": "active",
            },
        ]
    )

    assert um.cached_login_for_lark_id("tenant-a", "ou-1") == "alice"
    assert um.cached_login_for_lark_id("tenant-b", "ou-1") == "bob"
    assert um.cached_login_for_lark_id("tenant-c", "ou-1") is None


@pytest.mark.asyncio
async def test_upsert_preserves_existing_slack_mapping(fake_store: _FakeStore) -> None:
    await um.upsert_mapping(
        github_login="alice",
        work_email="alice@nas.io",
        slack_user_id="U1",
        source="slack_oauth",
    )

    record = await um.upsert_mapping(
        github_login="alice",
        work_email="alice@nas.io",
        lark_tenant_key="tenant-a",
        lark_open_id="ou-1",
        lark_union_id="on-1",
        lark_display_name="Alice",
        source="lark_oauth",
    )

    assert record["slack_user_id"] == "U1"
    assert record["lark_union_id"] == "on-1"
    assert await um.login_for_lark_id("tenant-a", "ou-1") == "alice"


@pytest.mark.asyncio
async def test_lark_lookup_loads_a_cold_cache(fake_store: _FakeStore) -> None:
    await um.upsert_mapping(
        github_login="alice",
        work_email="alice@nas.io",
        lark_tenant_key="tenant-a",
        lark_open_id="ou-1",
        source="lark_oauth",
    )
    um.clear_cache()

    assert await um.login_for_lark_id("tenant-a", "ou-1") == "alice"


@pytest.mark.asyncio
async def test_lark_update_deindexes_old_tenant_identity(fake_store: _FakeStore) -> None:
    await um.upsert_mapping(
        github_login="alice",
        work_email="alice@nas.io",
        lark_tenant_key="tenant-a",
        lark_open_id="ou-old",
        source="lark_oauth",
    )
    await um.upsert_mapping(
        github_login="alice",
        work_email="alice@nas.io",
        lark_tenant_key="tenant-a",
        lark_open_id="ou-new",
        source="lark_oauth",
    )

    assert um.cached_login_for_lark_id("tenant-a", "ou-old") is None
    assert um.cached_login_for_lark_id("tenant-a", "ou-new") == "alice"
