"""v0.6.1 T1 令牌一对一配对模型测试.

覆盖（任务书 docs/tasks/v061/T1-SERVER-PAIRING.md 验收标准）:
  * 配对路由命中: MCP 令牌的 tools/call 只下发到同配对的桥会话
  * 配对桥离线: 返回明确错误 pair-bridge-offline（不做用户名兜底）
  * 多对并存: 同用户名两对（byron/byron2），令牌互不串线
  * 未配对 MCP 令牌: 401 + CODE_MCP_UNPAIRED，明确指出无配对、无路由目标
  * 成对签发/成对吊销（issue pair / revoke pair）
  * env 兼容: 两行式 .env 同名条目即一对；单令牌兼容模式自配对
  * admin 接口: /admin/tokens 与 /admin/sessions 显示配对关系与在线状态
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from localoctop.app import create_app
from localoctop.config import Settings, load_settings, reload_tokens_from_env
from localoctop.errors import (
    CODE_AUTH_FAILED,
    CODE_MCP_UNPAIRED,
    CODE_NO_BRIDGE,
)
from localoctop.tokens import KIND_BRIDGE, KIND_MCP, TokenStore

from conftest import FakeWebSocket, bridge_pair_id, make_session, make_token_store

INIT_PARAMS = {
    "protocolVersion": "2024-11-05",
    "capabilities": {},
    "clientInfo": {"name": "octop", "version": "1"},
}

JSON_HEADERS = {"Accept": "application/json", "Content-Type": "application/json"}


def _settings(**over) -> Settings:
    base = dict(
        token_store=make_token_store(),
        allow_write=False,
        bridge_timeout=2.0,
        idle_timeout=30.0,
        audit_log_path="",
        log_level="WARNING",
        token_poll_interval=0.0,
    )
    base.update(over)
    return Settings(**base)


def _multi_pair_store() -> TokenStore:
    """The production shape from the task doc: byron holds two strict pairs
    (byron = first machine, byron2 = second machine), each pair's mcp and
    bridge tokens bound 1:1."""
    store = TokenStore()
    store.seed_pair("byron", "tok-byron", "btok-byron")
    store.seed_pair("byron2", "tok-byron2", "btok-byron2")
    return store


async def _mk(settings: Settings):
    app = create_app(settings)
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test"), app


def _auth(token: str) -> dict:
    return {**JSON_HEADERS, "Authorization": f"Bearer {token}"}


async def _handshake(client: httpx.AsyncClient, token: str) -> dict:
    r = await client.post("/mcp/localoctop/", headers=_auth(token), json={
        "jsonrpc": "2.0", "id": 1, "method": "initialize", "params": INIT_PARAMS,
    })
    assert r.status_code == 200, r.text
    sid = r.headers.get("mcp-session-id")
    assert sid
    headers = {**_auth(token), "Mcp-Session-Id": sid}
    r = await client.post("/mcp/localoctop/", headers=headers, json={
        "jsonrpc": "2.0", "method": "notifications/initialized",
    })
    assert r.status_code == 202
    return headers


def _pair_of(app, user: str, bridge_token: str) -> str:
    return bridge_pair_id(app.state.settings.token_store, user, bridge_token)


async def _call(client, headers, req_id, name="read_file", arguments=None):
    r = await client.post("/mcp/localoctop/", headers=headers, json={
        "jsonrpc": "2.0", "id": req_id, "method": "tools/call",
        "params": {"name": name, "arguments": arguments or {"path": "a.txt"}},
    })
    return r.json()


def _store_of(app):
    return app.state.settings.token_store


async def _answer(registry, ws, user_id):
    """Wait for one forwarded request on ws and answer it with an ok text."""
    for _ in range(100):
        if ws.outbox:
            break
        await asyncio.sleep(0.02)
    req = ws.outbox[0]
    registry.deliver(user_id, req["id"], {"id": req["id"], "result": {
        "path": "a.txt", "text": "ok", "encoding": "utf-8", "size": 2,
        "truncated": False,
    }})


# ------------------------------------------------------------- 配对路由命中

@pytest.mark.asyncio
async def test_paired_route_hits_only_the_paired_bridge():
    """两对并存（byron/byron2），两台桥都在线：byron 的 MCP 令牌调用只打到
    byron 那台桥的会话，绝不串到 byron2 的桥——即使两个会话都在线."""
    client, app = await _mk(_settings(token_store=_multi_pair_store()))
    registry = app.state.registry

    ws1, ws2 = FakeWebSocket(), FakeWebSocket()
    await registry.register(make_session("byron", ws1, client_id="machine-1",
                                        pair_id=_pair_of(app, "byron", "btok-byron")))
    await registry.register(make_session("byron2", ws2, client_id="machine-2",
                                         pair_id=_pair_of(app, "byron2", "btok-byron2")))

    async with client:
        headers = await _handshake(client, "tok-byron")
        task = asyncio.create_task(_answer(registry, ws1, "byron"))
        body = await _call(client, headers, 10)
        await task
        assert body["result"]["isError"] is False
        # The request landed on machine-1's socket and NOWHERE else.
        assert len(ws1.outbox) == 1
        assert ws2.outbox == []


@pytest.mark.asyncio
async def test_second_pair_routes_to_second_machine():
    """byron2 的 MCP 令牌调用打到第二台桥——多对并存时配对就是路由依据."""
    client, app = await _mk(_settings(token_store=_multi_pair_store()))
    registry = app.state.registry

    ws1, ws2 = FakeWebSocket(), FakeWebSocket()
    await registry.register(make_session("byron", ws1, pair_id=_pair_of(app, "byron", "btok-byron")))
    await registry.register(make_session("byron2", ws2, pair_id=_pair_of(app, "byron2", "btok-byron2")))

    async with client:
        headers = await _handshake(client, "tok-byron2")
        task = asyncio.create_task(_answer(registry, ws2, "byron2"))
        body = await _call(client, headers, 11)
        await task
        assert body["result"]["isError"] is False
        assert ws1.outbox == []
        assert len(ws2.outbox) == 1


# ------------------------------------------------------------- 配对桥离线

@pytest.mark.asyncio
async def test_paired_bridge_offline_returns_explicit_error():
    """配对桥不在线（用户名下没有任何会话）→ 明确错误 pair-bridge-offline，
    码 4101，绝不兜底."""
    client, app = await _mk(_settings(token_store=_multi_pair_store()))
    async with client:
        headers = await _handshake(client, "tok-byron")
        body = await _call(client, headers, 12)
        assert body["error"]["code"] == CODE_NO_BRIDGE
        assert "pair-bridge-offline" in body["error"]["message"]


@pytest.mark.asyncio
async def test_no_user_name_fallback_to_online_other_pair():
    """关键反例：byron 的配对桥离线，但 byron2 的桥在线（老模型会按用户名
    兜底打到它）→ 新模型必须报错，绝不把调用串到另一对的桥."""
    client, app = await _mk(_settings(token_store=_multi_pair_store()))
    registry = app.state.registry

    ws2 = FakeWebSocket()
    await registry.register(make_session("byron2", ws2,
                                         pair_id=_pair_of(app, "byron2", "btok-byron2")))

    async with client:
        headers = await _handshake(client, "tok-byron")
        body = await _call(client, headers, 13)
        assert body["error"]["code"] == CODE_NO_BRIDGE
        assert "pair-bridge-offline" in body["error"]["message"]
        # The online bridge of the OTHER pair was never touched.
        assert ws2.outbox == []


@pytest.mark.asyncio
async def test_same_name_session_wrong_pair_is_offline():
    """同用户名、在线、但配对不符（如换了桥令牌重新注册）→ 按配对桥离线处理，
    不命中."""
    client, app = await _mk(_settings(token_store=_multi_pair_store()))
    registry = app.state.registry

    # A session for user "byron" but registered by byron2's bridge token.
    ws = FakeWebSocket()
    await registry.register(make_session("byron", ws,
                                         pair_id=_pair_of(app, "byron2", "btok-byron2")))

    async with client:
        headers = await _handshake(client, "tok-byron")
        body = await _call(client, headers, 14)
        assert body["error"]["code"] == CODE_NO_BRIDGE
        assert "pair-bridge-offline" in body["error"]["message"]
        assert ws.outbox == []


# ------------------------------------------------------------- 未配对 MCP 令牌

@pytest.mark.asyncio
async def test_unpaired_mcp_token_rejected_with_explicit_code():
    """运行时单独签发、未配对的 MCP 令牌（如 lfs-mcp-1847 那类临时令牌）：
    鉴权身份有效但无路由目标 → CODE_MCP_UNPAIRED 4103，HTTP 401，
    消息明确指出"未配对".（吊销由中枢经 admin 接口操作，代码不特判.）"""
    store = _multi_pair_store()
    stray = store.issue(KIND_MCP, "byron")  # runtime-issued, unpaired
    client, app = await _mk(_settings(token_store=store))
    async with client:
        # Rejected at the ASGI entry (401) with the dedicated code.
        r = await client.post("/mcp/localoctop/", headers=_auth(stray), json={
            "jsonrpc": "2.0", "id": 1, "method": "initialize", "params": INIT_PARAMS,
        })
        assert r.status_code == 401
        assert r.json()["error"]["code"] == CODE_MCP_UNPAIRED
        assert "unpaired" in r.json()["error"]["message"].lower()
        # The paired token still works on the same store.
        r = await client.post("/mcp/localoctop/", headers=_auth("tok-byron"), json={
            "jsonrpc": "2.0", "id": 2, "method": "initialize", "params": INIT_PARAMS,
        })
        assert r.status_code == 200


def test_pair_route_mcp_distinguishes_auth_from_pairing():
    """路由解析的三种失败形态彼此独立：未知令牌→4102，未配对→4103，正常→返回对."""
    s = _settings(token_store=_multi_pair_store())
    user, pair = s.pair_route_mcp("tok-byron")
    assert user == "byron" and pair
    with pytest.raises(Exception) as ei:
        s.pair_route_mcp("no-such-token")
    assert ei.value.code == CODE_AUTH_FAILED
    stray = s.token_store.issue(KIND_MCP, "byron")
    with pytest.raises(Exception) as ei:
        s.pair_route_mcp(stray)
    assert ei.value.code == CODE_MCP_UNPAIRED


# ------------------------------------------------------------- 成对签发/吊销

def test_issue_pair_binds_both_tokens():
    """/admin/tokens/issue kind=pair: 两枚令牌共用一个 pair_id，互相为唯一配对."""
    store = TokenStore()
    mcp_tok, bridge_tok = store.issue_pair("byron")
    assert mcp_tok.startswith("lfs-mcp-") and bridge_tok.startswith("lfs-bridge-")
    mcp_pair = store.pair_of(KIND_MCP, mcp_tok)
    bridge_pair = store.pair_of(KIND_BRIDGE, bridge_tok)
    assert mcp_pair["pair_id"] == bridge_pair["pair_id"]
    # Strict 1:1: exactly one pair in the table for this user.
    assert [p["user_id"] for p in store.list_pairs()] == ["byron"]


def test_revoke_pair_revokes_both_halves():
    """成对吊销: 吊销 byron2 一对，两枚令牌同时失效；byron 一对不受影响."""
    store = _multi_pair_store()
    pair2 = store.pair_of(KIND_BRIDGE, "btok-byron2")["pair_id"]
    assert store.revoke_pair(pair2) is True
    with pytest.raises(Exception) as e1:
        store.authenticate(KIND_MCP, "tok-byron2")
    with pytest.raises(Exception) as e2:
        store.authenticate(KIND_BRIDGE, "btok-byron2")
    assert e1.value.code == CODE_AUTH_FAILED and e2.value.code == CODE_AUTH_FAILED
    # The other pair keeps working end to end.
    assert store.authenticate(KIND_MCP, "tok-byron") == "byron"
    assert store.pair_of(KIND_MCP, "tok-byron")["pair_id"]
    # Revoking a revoked pair is a no-op.
    assert store.revoke_pair(pair2) is False


def test_revoked_bridge_half_makes_mcp_token_unroutable():
    """只吊销配对中的桥一半（老的单向吊销仍在）→ MCP 令牌按未配对处理，
    不许路由到任何桥."""
    store = _multi_pair_store()
    assert store.revoke_user(KIND_BRIDGE, "byron") == 1
    # pair_of drops the revoked bridge side; the mcp token has no routing
    # target anymore.
    pair = store.pair_of(KIND_MCP, "tok-byron")
    assert pair is not None and KIND_BRIDGE not in pair
    s = _settings(token_store=store)
    with pytest.raises(Exception) as ei:
        s.pair_route_mcp("tok-byron")
    assert ei.value.code == CODE_MCP_UNPAIRED
    assert "unpaired" in ei.value.message.lower()


# ------------------------------------------------------------- env 兼容性

def test_env_two_line_form_is_one_pair():
    """现有 .env 两行式（byron.bridge=xxx / byron.mcp=yyy，即两个 env 变量里的
    同名条目）不改格式即映射为一对；byron2 天然是第二对."""
    s = load_settings({
        "LOCALOCTOP_MCP_TOKENS": "byron:tok-byron,byron2:tok-byron2",
        "LOCALOCTOP_CLIENT_TOKENS": "byron:btok-byron,byron2:btok-byron2",
    })
    user, pair = s.pair_route_mcp("tok-byron")
    assert user == "byron"
    # Same human, two distinct pairs, no cross-binding.
    _, pair2 = s.pair_route_mcp("tok-byron2")
    assert pair != pair2
    # Bridge side resolves to the same pair ids.
    u, bp = s.pair_route_bridge("btok-byron2")
    assert u == "byron2" and bp == pair2


def test_env_single_token_mode_self_pairs():
    """单令牌兼容模式（只设 LOCALOCTOP_MCP_TOKENS）: 每个用户的同一令牌自成一对，
    两个方向都能路由."""
    s = load_settings({"LOCALOCTOP_MCP_TOKENS": "byron:legacy-tok"})
    user, pair = s.pair_route_mcp("legacy-tok")
    assert user == "byron"
    u2, pair2 = s.pair_route_bridge("legacy-tok")
    assert u2 == "byron" and pair2 == pair


def test_env_reload_preserves_pairing():
    """热加载后配对关系随 env 重建：换令牌=换一对，移除用户=删一对；运行时签发的
    对不受 env 轮询影响."""
    env = {"LOCALOCTOP_MCP_TOKENS": "byron:tok-byron",
           "LOCALOCTOP_CLIENT_TOKENS": "byron:btok-byron"}
    s = load_settings(env)
    _, pair = s.pair_route_mcp("tok-byron")

    # Rotate byron's pair entirely.
    env2 = {"LOCALOCTOP_MCP_TOKENS": "byron:tok-byron-v2",
            "LOCALOCTOP_CLIENT_TOKENS": "byron:btok-byron-v2"}
    reload_tokens_from_env(s, env2)
    user, pair2 = s.pair_route_mcp("tok-byron-v2")
    assert user == "byron"
    assert pair2 != pair
    u2, bp2 = s.pair_route_bridge("btok-byron-v2")
    assert (u2, bp2) == ("byron", pair2)
    with pytest.raises(Exception):
        s.pair_route_mcp("tok-byron")

    # Runtime-issued pair for a user absent from env survives the polling.
    m, b = s.token_store.issue_pair("dave")
    reload_tokens_from_env(s, env2)
    assert s.pair_route_mcp(m)[0] == "dave"
    assert s.pair_route_bridge(b)[0] == "dave"


# ------------------------------------------------------------- 桥注册记录配对

@pytest.mark.asyncio
async def test_bridge_ws_registration_records_pair():
    """桥用桥令牌认证时，会话记录其所属配对标识（不是设备绑定——只是"这个桥
    由哪对令牌的桥令牌注册"）."""
    from localoctop.bridge_ws import BridgeWSEndpoint

    client, app = await _mk(_settings(token_store=_multi_pair_store()))
    registry = app.state.registry
    ep = BridgeWSEndpoint(app.state.settings, registry)

    ws = FakeWebSocket(query={"token": "btok-byron"})
    ws.feed({"type": "register", "token": "btok-byron", "client_id": "m-1",
             "hostname": "host-1", "allowed_dirs": ["/work"], "write_enabled": False,
             "version": "1.0.0"})
    handle = asyncio.create_task(ep.handle(ws))
    for _ in range(100):
        if registry.get("byron"):
            break
        await asyncio.sleep(0.01)
    session = registry.get("byron")
    assert session is not None
    assert session.pair_id == _pair_of(app, "byron", "btok-byron")

    ws.feed_disconnect()
    await asyncio.wait_for(handle, timeout=2)


@pytest.mark.asyncio
async def test_unpaired_bridge_token_registers_without_pair():
    """未配对的桥令牌仍可拨入（保活/升级场景），但其会话 pair_id 为空，永远不会
    成为任何 MCP 调用的路由目标."""
    from localoctop.bridge_ws import BridgeWSEndpoint

    store = _multi_pair_store()
    stray_bridge = store.issue(KIND_BRIDGE, "byron")  # unpaired bridge token
    client, app = await _mk(_settings(token_store=store))
    registry = app.state.registry
    ep = BridgeWSEndpoint(app.state.settings, registry)

    ws = FakeWebSocket(query={"token": stray_bridge})
    ws.feed({"type": "register", "client_id": "m-0", "allowed_dirs": ["/work"]})
    handle = asyncio.create_task(ep.handle(ws))
    for _ in range(100):
        if registry.get("byron"):
            break
        await asyncio.sleep(0.01)
    session = registry.get("byron")
    assert session is not None and session.pair_id == ""

    # The paired mcp token must NOT route to this unpaired session.
    async with client:
        headers = await _handshake(client, "tok-byron")
        body = await _call(client, headers, 20)
        assert body["error"]["code"] == CODE_NO_BRIDGE
        assert "pair-bridge-offline" in body["error"]["message"]
        assert ws.outbox == []

    ws.feed_disconnect()
    await asyncio.wait_for(handle, timeout=2)


# ------------------------------------------------------------- admin 接口

async def _admin_client(settings: Settings):
    import os
    os.environ["LOCALOCTOP_ADMIN_TOKEN"] = ""  # loopback-only, like other tests
    client, app = await _mk(settings)
    return client, app


@pytest.mark.asyncio
async def test_admin_tokens_shows_pairing():
    """/admin/tokens: 每个 pair_id 只出现在自己的记录里，配对关系可见，无明文."""
    client, app = await _admin_client(_settings(token_store=_multi_pair_store()))
    async with client:
        r = await client.get("/admin/tokens")
        assert r.status_code == 200
        body = r.json()
        tokens = {t["hint"]: t for t in body["tokens"]}
        assert tokens["tok-byron"]["pair_id"] == tokens["btok-byron"]["pair_id"]
        assert tokens["tok-byron"]["pair_id"] != tokens["btok-byron2"]["pair_id"]
        assert tokens["tok-byron"]["paired"] is True
        # Pairing overview: two complete pairs for the same human.
        users = sorted(p["user_id"] for p in body["pairs"])
        assert users == ["byron", "byron2"]
        assert all(p["complete"] for p in body["pairs"])
        # No plaintext beyond the 16-char hint: the full secrets never
        # appear as-is in a search for token+suffix.
        for secret in ("tok-byron2X", "btok-byron2X", "lfs-mcp-", "lfs-bridge-"):
            assert secret not in r.text


@pytest.mark.asyncio
async def test_admin_sessions_shows_pair_and_online_status():
    """/admin/sessions: 会话带 pair_id；/admin/tokens 的 pairs 标注配对桥是否在线.
    （报告中附实际 JSON —— 这两个断言就是对着真实输出结构写的.）"""
    client, app = await _admin_client(_settings(token_store=_multi_pair_store()))
    registry = app.state.registry
    pair1 = _pair_of(app, "byron", "btok-byron")
    pair2 = _pair_of(app, "byron2", "btok-byron2")

    ws1 = FakeWebSocket()
    await registry.register(make_session("byron", ws1, pair_id=pair1))

    async with client:
        r = await client.get("/admin/sessions")
        assert r.status_code == 200
        body = r.json()
        assert body["sessions"][0]["pair_id"] == pair1
        by_pair = {p["pair_id"]: p for p in body["pairs"]}
        assert by_pair[pair1]["user_id"] == "byron"
        assert by_pair[pair2]["user_id"] == "byron2"

        r = await client.get("/admin/tokens")
        tokens_body = r.json()
        online = {p["pair_id"]: p["bridge_online"] for p in tokens_body["pairs"]}
        assert online[pair1] is True    # byron's bridge is connected
        assert online[pair2] is False  # byron2's bridge is not

        # After the paired bridge disconnects, its pair shows offline.
        await registry.unregister("byron", registry.get("byron"))
        r = await client.get("/admin/tokens")
        online = {p["pair_id"]: p["bridge_online"] for p in r.json()["pairs"]}
        assert online[pair1] is False
        assert online[pair2] is False


@pytest.mark.asyncio
async def test_admin_issue_pair_and_revoke_pair_endpoints():
    """admin 签发一对 → 两枚令牌即刻可认证且互为配对；admin 按 pair_id 吊销 →
    两枚同时失效（成对签发、成对吊销的用户裁定在接口层兑现）."""
    client, app = await _admin_client(_settings(token_store=TokenStore()))
    async with client:
        r = await client.post("/admin/tokens/issue",
                              json={"user_id": "byron", "kind": "pair"})
        assert r.status_code == 200, r.text
        body = r.json()
        mcp_tok, bridge_tok = body["mcp_token"], body["bridge_token"]
        assert mcp_tok.startswith("lfs-mcp-") and bridge_tok.startswith("lfs-bridge-")

        user, pair = app.state.settings.pair_route_mcp(mcp_tok)
        assert user == "byron"
        u2, pair2 = app.state.settings.pair_route_bridge(bridge_tok)
        assert pair2 == pair

        r = await client.post("/admin/tokens/revoke", json={"pair_id": pair})
        assert r.status_code == 200
        assert r.json()["revoked"] is True
        with pytest.raises(Exception):
            app.state.settings.pair_route_mcp(mcp_tok)
        with pytest.raises(Exception) as ei:
            app.state.settings.authenticate_bridge(bridge_tok)
        assert ei.value.code == CODE_AUTH_FAILED
