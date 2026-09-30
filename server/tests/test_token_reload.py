"""v0.6.0 T3 令牌热加载测试.

覆盖（任务书 §4）:
  * 轮询重载: env 注入新令牌 → reload → 新令牌通过、被移除的旧令牌拒绝
  * reload API: admin 鉴权通过/失败两路径; 响应含用户列表且不含令牌值
  * 并发: reload 与 authenticate 并发无竞态（大量交替 + 断言不变量）
  * 轮询器: lifespan 启动后台 task, 到期重读 env, 关停时取消
  * 运行时签发令牌不受 env 轮询影响
"""

from __future__ import annotations

import asyncio
import threading

import httpx
import pytest

from localoctop.app import create_app
from localoctop.config import Settings, load_settings, reload_tokens_from_env
from localoctop.errors import CODE_AUTH_FAILED
from localoctop.tokens import KIND_BRIDGE, KIND_MCP, TokenStore

from conftest import make_token_store


def _settings(**over) -> Settings:
    base = dict(
        token_store=make_token_store(),
        allow_write=False,
        bridge_timeout=2.0,
        idle_timeout=5.0,
        audit_log_path="",
        log_level="WARNING",
        # poller off by default in unit tests; specific tests enable it
        token_poll_interval=0.0,
    )
    base.update(over)
    return Settings(**base)


async def _mk(settings: Settings):
    app = create_app(settings)
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test"), app


# ------------------------------------------------------------- 轮询重载逻辑

def test_reload_adds_new_env_token():
    """env 出现新用户 → reload 后新令牌即可认证, 变更记录含 user_id."""
    s = _settings()
    env = {"LOCALOCTOP_MCP_TOKENS": "alice:tok-alice,bob:tok-bob",
           "LOCALOCTOP_CLIENT_TOKENS": "alice:btok-alice,bob:btok-bob"}
    diff = reload_tokens_from_env(s, env)
    assert diff["mcp"] == {"added": [], "removed": []}
    assert diff["bridge"] == {"added": [], "removed": []}

    env2 = {**env, "LOCALOCTOP_MCP_TOKENS": "alice:tok-alice,bob:tok-bob,carol:tok-carol"}
    diff = reload_tokens_from_env(s, env2)
    assert diff["mcp"]["added"] == ["carol"]
    assert s.authenticate_mcp("tok-carol") == "carol"
    # bridge side unchanged → no diff there
    assert diff["bridge"] == {"added": [], "removed": []}


def test_reload_removes_missing_env_token():
    """env 移除用户 → reload 后旧令牌按语义处理（移除即拒绝）."""
    s = _settings()
    env = {"LOCALOCTOP_MCP_TOKENS": "alice:tok-alice,bob:tok-bob",
           "LOCALOCTOP_CLIENT_TOKENS": "alice:btok-alice,bob:btok-bob"}
    reload_tokens_from_env(s, env)
    env2 = {"LOCALOCTOP_MCP_TOKENS": "alice:tok-alice",
            "LOCALOCTOP_CLIENT_TOKENS": "alice:btok-alice"}
    diff = reload_tokens_from_env(s, env2)
    assert diff["mcp"]["removed"] == ["bob"]
    assert diff["bridge"]["removed"] == ["bob"]
    with pytest.raises(Exception) as ei:
        s.authenticate_mcp("tok-bob")
    assert getattr(ei.value, "code", None) == CODE_AUTH_FAILED
    with pytest.raises(Exception):
        s.authenticate_bridge("btok-bob")
    # alice keeps working on both sides
    assert s.authenticate_mcp("tok-alice") == "alice"
    assert s.authenticate_bridge("btok-alice") == "alice"


def test_reload_rotated_token_replaces_old():
    """同用户换令牌 → 新令牌通过, 旧令牌立即失效（移除语义）."""
    s = _settings()
    env = {"LOCALOCTOP_MCP_TOKENS": "alice:tok-alice"}
    reload_tokens_from_env(s, env)
    env2 = {"LOCALOCTOP_MCP_TOKENS": "alice:tok-alice-v2"}
    diff = reload_tokens_from_env(s, env2)
    assert s.authenticate_mcp("tok-alice-v2") == "alice"
    with pytest.raises(Exception):
        s.authenticate_mcp("tok-alice")


def test_reload_bridge_fallback_to_mcp_env():
    """未设 CLIENT_TOKENS 时 bridge 沿用 MCP 令牌（单令牌向后兼容）."""
    s = _settings()
    env = {"LOCALOCTOP_MCP_TOKENS": "alice:tok-alice"}
    reload_tokens_from_env(s, env)
    assert s.authenticate_bridge("tok-alice") == "alice"


def test_reload_no_change_is_noop_and_logged_only_on_change():
    """无变化零动作: 第二次 reload 不产生变更, 日志只在有变化时记录."""
    s = _settings()
    env = {"LOCALOCTOP_MCP_TOKENS": "alice:tok-alice"}
    reload_tokens_from_env(s, env)
    log = _TestLogger()
    diff = reload_tokens_from_env(s, env, logger=log)
    assert diff["mcp"] == {"added": [], "removed": []}
    assert diff["bridge"] == {"added": [], "removed": []}
    assert not log.records  # no-change reload logs nothing


class _TestLogger:
    """Collects (level, formatted) records for assertions."""

    def __init__(self) -> None:
        self.records: list[str] = []

    def info(self, msg: str, *args) -> None:
        self.records.append(msg % args if args else msg)


def test_reload_log_contains_no_plaintext(caplog):
    """变更日志含 user_id 但绝不含令牌原文."""
    records: list[str] = []

    class _Log:
        def info(self, msg: str, *args):
            records.append(msg % args if args else msg)
        def exception(self, msg: str, *args):
            records.append(msg % args if args else msg)

    s = _settings()
    diff = reload_tokens_from_env(
        s,
        {"LOCALOCTOP_MCP_TOKENS": "alice:tok-alice,carol:tok-carol",
         "LOCALOCTOP_CLIENT_TOKENS": "alice:btok-alice,carol:btok-carol"},
        logger=_Log(),
    )
    assert diff["mcp"]["added"] == ["carol"]
    joined = "\n".join(records)
    assert "carol" in joined
    assert "tok-carol" not in joined
    assert "btok-carol" not in joined


def test_reload_preserves_runtime_issued_tokens():
    """运行时 /admin/tokens/issue 签发的令牌（用户不在 env 中）不被轮询清掉."""
    s = _settings()
    env = {"LOCALOCTOP_MCP_TOKENS": "alice:tok-alice"}
    reload_tokens_from_env(s, env)
    runtime_token = s.token_store.issue(KIND_MCP, "dave")
    reload_tokens_from_env(s, env)  # 无变化, dave 保留
    assert s.authenticate_mcp(runtime_token) == "dave"
    # dave 不在 env, 所以 env 再怎么变 dave 都保留
    reload_tokens_from_env(s, {"LOCALOCTOP_MCP_TOKENS": "alice:tok-alice,bob:tok-bob"})
    assert s.authenticate_mcp(runtime_token) == "dave"


def test_load_settings_reload_roundtrip():
    """load_settings 与 reload_tokens_from_env 解析同一 env 形态一致."""
    env = {"LOCALOCTOP_MCP_TOKENS": "alice:tok-alice,bob:tok-bob",
           "LOCALOCTOP_CLIENT_TOKENS": "alice:btok-alice,bob:btok-bob"}
    s1 = load_settings(env)
    s2 = _settings()
    reload_tokens_from_env(s2, env)
    assert s2.authenticate_mcp("tok-alice") == "alice"
    assert s2.authenticate_bridge("btok-bob") == "bob"
    assert sorted(u for u in s1.static_tokens) == ["alice", "bob"]


# ------------------------------------------------------------- reload API

@pytest.mark.asyncio
async def test_reload_api_requires_admin_auth(monkeypatch):
    """鉴权失败路径: 无 admin token 时回环外请求 → 403."""
    monkeypatch.setenv("LOCALOCTOP_ADMIN_TOKEN", "admin-secret")
    s = _settings()
    client, _app = await _mk(s)
    async with client:
        r = await client.post("/admin/tokens/reload")
        assert r.status_code == 403
        assert "error" in r.json()


@pytest.mark.asyncio
async def test_reload_api_with_admin_token(monkeypatch):
    """鉴权通过 + 响应结构: reloaded/mcp_users/bridge_users, 不含令牌值."""
    monkeypatch.setenv("LOCALOCTOP_ADMIN_TOKEN", "admin-secret")
    s = _settings()
    client, _app = await _mk(s)
    async with client:
        r = await client.post("/admin/tokens/reload",
                              headers={"Authorization": "Bearer admin-secret"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["reloaded"] is True
        # users present (settings store has alice+bob seeded by make_token_store,
        # reload with empty env removes them — the response reflects the *live*
        # table after reload)
        assert isinstance(body["mcp_users"], list)
        assert isinstance(body["bridge_users"], list)
        raw = r.text
        assert "tok-alice" not in raw
        assert "btok-alice" not in raw
        assert "admin-secret" not in raw


@pytest.mark.asyncio
async def test_reload_api_reloads_new_env_token(monkeypatch):
    """API 立即重读 env: 更新环境变量后调用 API, 新令牌马上可认证."""
    monkeypatch.setenv("LOCALOCTOP_ADMIN_TOKEN", "admin-secret")
    monkeypatch.setenv("LOCALOCTOP_MCP_TOKENS", "alice:tok-alice,bob:tok-bob")
    monkeypatch.setenv("LOCALOCTOP_CLIENT_TOKENS", "alice:btok-alice,bob:btok-bob")
    s = _settings()
    client, _app = await _mk(s)
    async with client:
        # simulate ops updating the in-container env before calling the API
        monkeypatch.setenv("LOCALOCTOP_MCP_TOKENS", "alice:tok-alice,carol:tok-carol")
        monkeypatch.setenv("LOCALOCTOP_CLIENT_TOKENS", "alice:btok-alice,carol:btok-carol")
        r = await client.post("/admin/tokens/reload",
                              headers={"Authorization": "Bearer admin-secret"})
        assert r.status_code == 200
        body = r.json()
        assert "carol" in body["mcp_users"]
        assert "carol" in body["bridge_users"]
        assert "carol" in body["added"]
        # new token authenticates immediately via the same app's settings
        assert s.authenticate_mcp("tok-carol") == "carol"
        assert s.authenticate_bridge("btok-carol") == "carol"
        # removed user is gone from the list and auth fails
        assert "bob" not in body["mcp_users"]
        assert "bob" in body["removed"]
        with pytest.raises(Exception):
            s.authenticate_mcp("tok-bob")


@pytest.mark.asyncio
async def test_reload_api_loopback_allowed_without_admin_token(monkeypatch):
    """无 admin token 时回环请求放行（与 /admin/sessions 同机制）."""
    monkeypatch.delenv("LOCALOCTOP_ADMIN_TOKEN", raising=False)
    s = _settings()
    client, _app = await _mk(s)
    async with client:
        # httpx ASGITransport client host is "testclient" (127.0.0.1-like);
        # starlette sets it to 127.0.0.1 for ASGITransport
        r = await client.post("/admin/tokens/reload")
        assert r.status_code == 200
        assert r.json()["reloaded"] is True


# ------------------------------------------------------------- 并发安全

@pytest.mark.asyncio
async def test_concurrent_reload_and_authenticate():
    """reload 与 authenticate 大量交替: 无竞态、无崩溃, 结果始终自洽
    (任一时刻 authenticate 的结果与某个完整的 env 状态一致)."""
    s = _settings()
    envs = [
        {"LOCALOCTOP_MCP_TOKENS": "alice:tok-alice,bob:tok-bob",
         "LOCALOCTOP_CLIENT_TOKENS": "alice:btok-alice,bob:btok-bob"},
        {"LOCALOCTOP_MCP_TOKENS": "alice:tok-alice,carol:tok-carol",
         "LOCALOCTOP_CLIENT_TOKENS": "alice:btok-alice,carol:btok-carol"},
        {"LOCALOCTOP_MCP_TOKENS": "alice:tok-alice",
         "LOCALOCTOP_CLIENT_TOKENS": "alice:btok-alice"},
    ]
    reload_tokens_from_env(s, envs[0])
    errors: list[Exception] = []

    def _reload_worker():
        for _ in range(300):
            for env in envs:
                reload_tokens_from_env(s, env)

    def _auth_worker():
        for _ in range(600):
            for token, expect_user in (("tok-alice", "alice"), ("tok-bob", None),
                                       ("tok-carol", None)):
                try:
                    user = s.authenticate_mcp(token)
                except Exception as exc:  # noqa: BLE001
                    if expect_user is not None:
                        # tok-alice is in every env state, so a failure here
                        # would be a real race (torn read)
                        errors.append(exc)
                    continue
                if expect_user is not None and user != expect_user:
                    errors.append(AssertionError(f"alice resolved to {user}"))
                elif expect_user is None and user not in ("bob", "carol"):
                    errors.append(AssertionError(f"{token} resolved to {user}"))

    threads = [threading.Thread(target=_reload_worker),
               threading.Thread(target=_auth_worker),
               threading.Thread(target=_auth_worker)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert not errors, errors[:5]


def test_token_store_apply_env_atomic_swap():
    """apply_env 换表原子: 换完后旧表引用与 getattr 读取一致性."""
    store = make_token_store()
    before = store._table_locked(KIND_MCP)
    store.apply_env(KIND_MCP, {"alice": "tok-alice", "bob": "tok-bob"})
    after = store._table_locked(KIND_MCP)
    assert before is not after
    assert set(after) == set(before)  # same content, new dict
    # revoked flag preserved through reload
    store.revoke_token(KIND_MCP, "tok-alice")
    store.apply_env(KIND_MCP, {"alice": "tok-alice", "bob": "tok-bob"})
    with pytest.raises(Exception):
        store.authenticate(KIND_MCP, "tok-alice")


# ------------------------------------------------------------- 轮询器

# ------------------------------------------------------------- 轮询器
# httpx's ASGITransport never emits ASGI lifespan events, so the poller
# tests drive the lifespan context explicitly.

@pytest.mark.asyncio
async def test_token_poller_reloads_on_change(monkeypatch):
    """lifespan 启动的后台 task 到期重读 env, 令牌变化生效, 退出时取消."""
    monkeypatch.setenv("LOCALOCTOP_MCP_TOKENS", "alice:tok-alice")
    s = _settings(token_poll_interval=0.05)
    app = create_app(s)
    async with app.router.lifespan_context(app):
        # let the poller run at least once with the initial env
        await asyncio.sleep(0.15)
        assert s.authenticate_mcp("tok-alice") == "alice"
        # change the env the poller will read
        monkeypatch.setenv("LOCALOCTOP_MCP_TOKENS", "alice:tok-alice,carol:tok-carol")
        await asyncio.sleep(0.2)
        assert s.authenticate_mcp("tok-carol") == "carol"
    # after lifespan shutdown the poll task is cancelled and gone
    assert app.state._token_poll_task.done() or app.state._token_poll_task.cancelled()


@pytest.mark.asyncio
async def test_token_poller_zero_interval_disabled(monkeypatch):
    """token_poll_interval=0 → 不启动后台 task."""
    monkeypatch.setenv("LOCALOCTOP_MCP_TOKENS", "alice:tok-alice")
    s = _settings(token_poll_interval=0.0)
    app = create_app(s)
    async with app.router.lifespan_context(app):
        assert getattr(app.state, "_token_poll_task", None) is None
        monkeypatch.setenv("LOCALOCTOP_MCP_TOKENS", "alice:tok-alice,carol:tok-carol")
        await asyncio.sleep(0.1)
        with pytest.raises(Exception):
            s.authenticate_mcp("tok-carol")
