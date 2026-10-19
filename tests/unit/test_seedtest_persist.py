"""SeedTest ``--persist NAME`` (round 7 WP-12): keep ONE named tenant for the walk.

The harness names every tenant it creates ``seed+<RUN_ID>-<role>@seed.vantagemind.ai``
with a per-run random password, and purges them all. The dev walk account
(e2e-walk@seed.vantagemind.ai) already exists and its password is not the harness's, so
``--persist`` ADOPTS it with a session minted in-process (the same minting the harness
already does for the verify link): nothing about the account changes. It is made walkable
(a Luciel + knowledge only if it has none) and never purged — the teardown now purges only
accounts THIS run minted, so neither the walk account nor another run's tenant can be
destroyed by a list mix-up. Only the seed domain can be adopted, and never outside dev.

Provisioning runs here against an ``httpx.MockTransport`` standing in for the API.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any

import httpx
import pytest

from tests.unit import seedtest_harness

H = seedtest_harness.load()
WALK = "e2e-walk@seed.vantagemind.ai"


@pytest.mark.parametrize(
    ("name", "email"),
    [
        ("e2e-walk", WALK),
        ("E2E-Walk", WALK),
        (WALK, WALK),
        (" e2e-walk ", WALK),
        ("walk.2+test", "walk.2+test@seed.vantagemind.ai"),
    ],
)
def test_persist_names_a_seed_domain_mailbox(name: str, email: str) -> None:
    assert H.persist_email(name) == email


@pytest.mark.parametrize(
    "name",
    [
        "owner@gmail.com",  # never a real customer's account
        "e2e-walk@vantagemind.ai",
        "e2e-walk@seed.vantagemind.ai.evil.test",
        "",
        "a b",
        "../e2e-walk",
        "-walk",
        "x" * 64,
    ],
)
def test_persist_refuses_anything_else(name: str) -> None:
    with pytest.raises(ValueError):
        H.persist_email(name)


@pytest.mark.parametrize(
    ("env_name", "allowed"),
    [("dev", True), ("", True), ("prod", False), ("PREPROD", False), ("production", False)],
)
def test_persist_is_a_dev_instrument(env_name: str, allowed: bool) -> None:
    assert H.persist_allowed(env_name) is allowed


def _tenant(email: str | None = None, *, persist: bool = False, role: str = "main") -> Any:
    t = H.Tenant(role, email=email)
    t.persist = persist
    t.embed_key = "vm_live_x"
    return t


def test_the_teardown_purges_only_what_this_run_minted() -> None:
    mine = _tenant(role="main")
    cap = _tenant(role="cap")
    walk_adopted = _tenant(WALK, persist=True, role="persist-e2e-walk")
    walk_by_mistake = _tenant(WALK, role="oops")  # not marked persist, still never purged
    other_run = _tenant("seed+1234567890-main@seed.vantagemind.ai", role="old")
    purge, keep = H.teardown_plan([mine, walk_adopted, cap, walk_by_mistake, other_run])
    assert [t.role for t in purge] == ["main", "cap"]
    assert {t.role for t in keep} == {"persist-e2e-walk", "oops", "old"}
    assert H.minted_by_this_run(mine.email)
    for t in (mine, cap, walk_adopted, walk_by_mistake, other_run):
        t.client.close()


def test_a_persisted_tenant_is_never_looked_up_for_purge(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import app.db.engine

    async def no_lookup(_email: str) -> uuid.UUID:
        raise AssertionError("the persisted tenant was looked up for purge")

    async def no_dispose() -> None:
        return None

    monkeypatch.setattr(H, "_admin_id_for", no_lookup)
    monkeypatch.setattr(app.db.engine, "dispose_engine", no_dispose)
    walk = _tenant(WALK, persist=True, role="persist-e2e-walk")
    asyncio.run(H._teardown([walk]))
    assert f"kept 'persist-e2e-walk' ({WALK})" in capsys.readouterr().out
    walk.client.close()


# --- provisioning against a stand-in API -------------------------------------- #
class FakeApi:
    def __init__(self, *, luciel: dict[str, Any] | None, sources: int = 1) -> None:
        self.luciel = luciel
        self.sources = sources
        self.calls: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls.append(f"{request.method} {path}")
        if path == "/api/v1/admin/luciel" and request.method == "GET":
            return httpx.Response(200, content=json.dumps(self.luciel).encode())
        if path == "/api/v1/admin/luciel" and request.method == "POST":
            self.luciel = {"state": "active", "embedKeyPublicId": "vm_live_new"}
            return httpx.Response(200, json=self.luciel)
        if path == "/api/v1/admin/knowledge/sources/paste":
            self.sources += 1
            return httpx.Response(200, json={})
        if path == "/api/v1/admin/knowledge/sources":
            return httpx.Response(200, json=[{"sourceId": str(i)} for i in range(self.sources)])
        if path == "/api/v1/chat-widget/bootstrap":
            return httpx.Response(200, json={"renderState": "active"})
        return httpx.Response(404, json={"message": f"unexpected {path}"})


def _account(**overrides: Any) -> dict[str, Any]:
    account = {
        "admin_id": uuid.uuid4(),
        "user_id": uuid.uuid4(),
        "epoch": 3,
        "state": "verified",
        "verified": True,
    }
    account.update(overrides)
    return account


@pytest.fixture
def walk_tenant(monkeypatch: pytest.MonkeyPatch) -> Any:
    t = H.Tenant("persist-e2e-walk", email=WALK)
    t.persist = True
    yield t
    t.client.close()


def _wire(
    monkeypatch: pytest.MonkeyPatch,
    tenant: Any,
    api: FakeApi,
    accounts: list[dict[str, Any] | None],
) -> list[dict[str, Any]]:
    tenant.client.close()
    tenant.client = httpx.Client(transport=httpx.MockTransport(api))
    adopted: list[dict[str, Any]] = []
    queue = list(accounts)

    async def account_for(_email: str) -> dict[str, Any] | None:
        return queue.pop(0)

    monkeypatch.setattr(H, "_account_for", account_for)
    monkeypatch.setattr(H, "adopt_session", lambda _t, account: adopted.append(account))
    return adopted


def test_an_existing_walk_account_is_adopted_and_left_as_it_is(
    monkeypatch: pytest.MonkeyPatch, walk_tenant: Any
) -> None:
    account = _account()
    api = FakeApi(luciel={"state": "active", "embedKeyPublicId": "vm_live_walk"}, sources=2)
    adopted = _wire(monkeypatch, walk_tenant, api, [account])
    obs = H.provision_persisted(walk_tenant)
    assert adopted == [account]
    assert not any(c.startswith("POST /api/v1/admin/") for c in api.calls)  # nothing created
    assert obs["adopted"] and not obs["created_luciel"]
    assert walk_tenant.embed_key == "vm_live_walk" and walk_tenant.admin_id == account["admin_id"]
    ok, evidence = H.judge_persist(obs)
    assert ok, evidence


def test_an_adopted_account_without_a_luciel_gets_one_with_knowledge(
    monkeypatch: pytest.MonkeyPatch, walk_tenant: Any
) -> None:
    api = FakeApi(luciel=None, sources=0)
    _wire(monkeypatch, walk_tenant, api, [_account()])
    obs = H.provision_persisted(walk_tenant)
    assert "POST /api/v1/admin/luciel" in api.calls
    assert "POST /api/v1/admin/knowledge/sources/paste" in api.calls
    assert obs["created_luciel"] and obs["sources"] == 1
    assert H.judge_persist(obs)[0]


def test_an_unverified_account_is_reported_not_touched(
    monkeypatch: pytest.MonkeyPatch, walk_tenant: Any
) -> None:
    api = FakeApi(luciel=None)
    adopted = _wire(monkeypatch, walk_tenant, api, [_account(state="unverified", verified=False)])
    obs = H.provision_persisted(walk_tenant)
    assert api.calls == [] and adopted == []
    ok, evidence = H.judge_persist(obs)
    assert not ok and "account_ready=FAIL[" in evidence


def test_a_missing_account_is_created_through_signup(
    monkeypatch: pytest.MonkeyPatch, walk_tenant: Any
) -> None:
    api = FakeApi(luciel={"state": "active", "embedKeyPublicId": "vm_live_seeded"})
    adopted = _wire(monkeypatch, walk_tenant, api, [None, _account()])
    seeded: list[str] = []
    monkeypatch.setattr(H.Tenant, "seed", lambda self: seeded.append(self.email))
    obs = H.provision_persisted(walk_tenant)
    assert seeded == [WALK] and adopted == []  # its own fresh session, no adoption needed
    assert obs["adopted"] is False
    assert H.judge_persist(obs)[0]


@pytest.mark.parametrize(
    ("changes", "label"),
    [
        ({"account_state": "closed"}, "account_ready"),
        ({"luciel_state": "paused"}, "luciel_active"),
        ({"sources": 0}, "knowledge_present"),
        ({"boot": "at_cap"}, "widget_serving"),
    ],
)
def test_judge_persist(changes: dict[str, Any], label: str) -> None:
    good = {
        "email": WALK,
        "adopted": True,
        "account_state": "verified",
        "verified": True,
        "created_luciel": False,
        "luciel_state": "active",
        "embed_key": "vm_live_walk",
        "sources": 1,
        "boot": "active",
    }
    assert H.judge_persist(good)[0]
    ok, evidence = H.judge_persist({**good, **changes})
    assert not ok and f"{label}=FAIL[" in evidence


def test_the_adopted_session_is_an_ordinary_one_at_the_accounts_epoch() -> None:
    from app.auth.tokens import decode_token
    from app.config import get_settings

    account = _account(epoch=7)
    t = H.Tenant("persist-e2e-walk", email=WALK)
    try:
        H.adopt_session(t, account)
        token = t.client.cookies.get(get_settings().session_cookie_name)
        assert token
        claims = decode_token(token)
        assert claims["sub"] == str(account["admin_id"])
        assert claims["uid"] == str(account["user_id"])
        assert claims["ep"] == 7 and claims["typ"] == "session"
    finally:
        t.client.close()


def test_persist_refused_in_prod_is_a_recorded_fail(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LUCIEL_ENV", "prod")
    monkeypatch.setattr(H, "provision_persisted", lambda _t: pytest.fail("provisioned in prod"))
    r = H.Results()
    H.run_persist(r, "e2e-walk")
    (row,) = r.rows
    assert row.name == f"P persist {WALK}" and row.status == "fail"
    assert "refused in LUCIEL_ENV=prod" in row.evidence


@pytest.mark.parametrize(
    ("argv", "cases", "persist"),
    [
        (["--persist", "e2e-walk"], False, "e2e-walk"),
        (["--persist", "e2e-walk", "--cases"], True, "e2e-walk"),
        (["--persist", "e2e-walk", "--matrix"], False, "e2e-walk"),
        ([], True, None),
    ],
)
def test_cli_persist(argv: list[str], cases: bool, persist: str | None) -> None:
    opts = H.parse_args(argv, {})
    assert (opts.cases, opts.persist) == (cases, persist)


def test_cli_refuses_a_non_seed_persist() -> None:
    with pytest.raises(SystemExit):
        H.parse_args(["--persist", "owner@gmail.com"], {})
