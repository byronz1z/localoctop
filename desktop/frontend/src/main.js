// main.js — M3 multi-page UI for localoctop-desktop (vanilla JS, no
// framework, no bundler required at runtime; vite just copies/points at src).
// Talks to the Go side exclusively through the Wails bindings
// (window.go.main.App.*) and the runtime events ("bridge:status",
// "bridge:audit"). Sidebar pages: 连接 (M2 form) / 活动 (audit stream) /
// 设置 (paths + confirm_exit).

import './style.css';

const $ = (id) => document.getElementById(id);

const state = {
    dirs: [],       // [{path, enabled}]
    existed: null,  // null until LoadConfig returns
    recent: [],     // [{url, token}] from file.recent_servers
    saved: false,   // a valid config was saved in this session
    page: 'conn',   // current sidebar page (conn | activity | settings)
    events: [],     // audit events, newest first, cap 500 (activity page)
    onlyErrors: false, // activity page filter
};

/* ---------- helpers ---------- */

function flash(el, text, ok) {
    el.textContent = text;
    el.className = 'msg ' + (ok ? 'ok' : 'err');
    clearTimeout(el._t);
    el._t = setTimeout(() => { el.className = 'msg'; }, ok ? 3500 : 8000);
}

function clearFlash(el) {
    clearTimeout(el._t);
    el.className = 'msg';
    el.textContent = '';
}

const goApp = () => window.go.main.App;

function fmtTime(ts) {
    if (!ts) return '';
    try { return new Date(ts).toLocaleTimeString(); } catch { return ts; }
}

/* ---------- sidebar navigation (M3) ---------- */

function showPage(name) {
    state.page = name;
    ['conn', 'activity', 'settings'].forEach((p) => {
        document.getElementById('page-' + p).hidden = p !== name;
        document.getElementById('nav-' + p).classList.toggle('active', p === name);
    });
}

function wireNav() {
    document.querySelectorAll('.nav-btn').forEach((btn) => {
        btn.addEventListener('click', () => showPage(btn.dataset.page));
    });
}

/* ---------- activity page: audit event stream (M3) ---------- */

const MAX_EVENTS = 500;

// DECISION maps an AuditEvent.decision ("allow" | "deny" | "error") to the
// row's pill class + label (protocol: localoctop.AuditEvent).
const DECISION = { allow: ['ok', '允许'], deny: ['err', '拒绝'], error: ['err', '错误'] };

function decisionOf(ev) { return DECISION[ev.decision] || ['', ev.decision || '—']; }

function isErr(ev) { return ev.decision === 'deny' || ev.decision === 'error'; }

function onAuditEvent(ev) {
    state.events.unshift(ev);
    if (state.events.length > MAX_EVENTS) state.events.length = MAX_EVENTS;
    if (state.page === 'activity') renderActivity();
}

function renderActivity() {
    const list = $('actList');
    const shown = state.onlyErrors ? state.events.filter(isErr) : state.events;
    $('actCount').textContent = shown.length + ' 条';

    list.innerHTML = '';
    if (shown.length === 0) {
        const empty = document.createElement('div');
        empty.className = 'act-empty muted';
        empty.textContent = state.onlyErrors ? '暂无错误记录' : '暂无操作记录';
        list.appendChild(empty);
        return;
    }
    shown.forEach((ev) => {
        const [cls, label] = decisionOf(ev);

        const row = document.createElement('div');
        row.className = 'act-row';

        const t = document.createElement('span');
        t.className = 'act-time mono';
        t.textContent = fmtTime(ev.ts);

        const m = document.createElement('span');
        m.className = 'act-method mono';
        m.textContent = ev.method || '—';

        const p = document.createElement('span');
        p.className = 'act-path mono';
        p.textContent = ev.path || '';
        p.title = ev.path || '';

        const d = document.createElement('span');
        d.className = 'pill ' + cls;
        d.textContent = label;

        row.append(t, m, p, d);
        list.appendChild(row);
    });
}

/* ---------- settings page (M3) ---------- */

async function loadSettingsPage() {
    try {
        const info = await goApp().GetSettingsInfo();
        $('setVersion').textContent = 'v' + (info.version || '');
        $('setConfigPath').textContent = info.config_path || '—';
        $('setAuditPath').textContent = info.audit_log_path || '—';
    } catch { /* keep placeholders */ }
    try {
        $('confirmExit').checked = await goApp().GetConfirmExit();
    } catch { /* default checked */ }
    $('confirmExit').addEventListener('change', async () => {
        try {
            await goApp().SetConfirmExit($('confirmExit').checked);
        } catch (err) {
            $('confirmExit').checked = !$('confirmExit').checked; // revert on failure
            flash($('saveMsg'), '保存设置失败：' + err, false);
            showPage('conn'); // the msg slot lives on the connection page
        }
    });

    // 开机自启 (0.6.0 T2): registry is truth; warning surfaces as hint text.
    try {
        const [on, warn] = await goApp().GetAutostart();
        $('autostartChk').checked = on;
        if (warn) {
            $('autostartHint').textContent = '无法读取自启状态：' + warn;
            $('autostartHint').hidden = false;
        }
    } catch { /* keep unchecked */ }
    $('autostartChk').addEventListener('change', async () => {
        $('autostartHint').hidden = true;
        try {
            await goApp().SetAutostart($('autostartChk').checked);
        } catch (err) {
            $('autostartChk').checked = !$('autostartChk').checked; // revert on failure
            $('autostartHint').textContent = '设置开机自启失败：' + err;
            $('autostartHint').hidden = false;
        }
    });
}

/* ---------- status card + header dot (five states, 0.6.0 T2) ---------- */

// STALE_PONG: connected but last pong older than this (ms) → 心跳异常.
// Mirrors the Go side's stalePongAfter (90s). Evaluated at render time so
// the warning appears on the 3s poll tick even without a push event.
const STALE_PONG_MS = 90 * 1000;

// elapsedAgo formats "X 秒前" for the heartbeat row (0 → 刚刚).
function elapsedAgo(ts) {
    if (!ts) return '';
    const ms = Date.now() - new Date(ts).getTime();
    if (ms < 0 || Number.isNaN(ms)) return '';
    const sec = Math.floor(ms / 1000);
    if (sec < 1) return '刚刚';
    return sec + ' 秒前';
}

// classify maps one Status snapshot to the five states. Priority matches
// app.go's applyDetail: 已断开 (manual) > 已连接 > 重连中 > 连接中 > 未配置.
function classify(s) {
    switch (s.conn_state) {
    case 'disconnected': return 'disconnected';
    case 'connected':
        return s.connected ? 'connected' : 'reconnecting'; // detail-lag guard
    case 'reconnecting': return 'reconnecting';
    case 'connecting': return 'connecting';
    case 'unconfigured': return 'unconfigured';
    default:
        // pre-startup snapshot: infer from identity/config fields
        if (s.connected) return 'connected';
        if (s.reconnect_attempt > 0 || s.last_error) return 'reconnecting';
        if (s.client_id) return 'connecting';
        return 'unconfigured';
    }
}

// renderStatus maps one bridge:status snapshot onto the status card.
// The five states come from the Go side's conn_state (fed by the bridge
// core's StatusDetail pushes + 3s poll); only the 心跳异常 sub-warning is
// derived here, from last_pong_at vs wall clock.
function renderStatus(s) {
    const card = $('statusCard');
    const big = $('statusBig');
    const sub = $('statusSub');
    const tag = $('statusTag');
    const ico = $('statusIco');
    const ICO = {
        connected: '✔', reconnecting: '✕', connecting: '', disconnected: '⏏', unconfigured: '…',
    };

    const st = classify(s);
    let cls, tagTxt;
    if (st === 'connected') {
        // >90s without a pong = heartbeat trouble: still "connected" on the
        // wire, but warn in yellow and say why.
        const pongAge = s.last_pong_at ? (Date.now() - new Date(s.last_pong_at).getTime()) : Infinity;
        const stale = pongAge > STALE_PONG_MS;
        cls = stale ? 'warn' : 'ok';
        tagTxt = stale ? '心跳异常' : '已连接';
        big.textContent = stale ? '已连接（心跳异常）' : '已连接';
        sub.textContent = stale
            ? '超过 90 秒未收到服务器心跳回应，连接可能已失效，等待自动重连…'
            : (s.client_id ? '云端 AI 可访问本机白名单目录 · 心跳 ' + (elapsedAgo(s.last_pong_at) || '从未') : '');
    } else if (st === 'reconnecting') {
        cls = 'err'; tagTxt = '重连中';
        big.textContent = '重连中（第 ' + (s.reconnect_attempt || 1) + ' 次）';
        sub.textContent = s.last_error || '连接中断，正在自动重连…';
    } else if (st === 'connecting') {
        cls = 'conn'; tagTxt = '连接中';
        big.textContent = '连接中…';
        sub.textContent = '正在与服务器建立连接…';
    } else if (st === 'disconnected') {
        cls = 'off'; tagTxt = '已断开';
        big.textContent = '已断开';
        sub.textContent = '已手动断开，不会自动重连；点「连接」或保存配置可恢复。';
    } else { // unconfigured
        cls = 'wait'; tagTxt = '未配置';
        big.textContent = '未配置';
        sub.textContent = '填写服务器地址与令牌并保存后开始连接。';
    }
    card.className = 'card status-card ' + cls;
    ico.textContent = ICO[st];
    tag.className = 'pill ' + (cls === 'ok' || cls === 'warn' ? 'ok' : cls === 'err' ? 'err' : '');
    tag.textContent = tagTxt;

    $('stClient').textContent = s.client_id || '—';
    $('stAttempt').textContent = String(s.reconnect_attempt || 0);
    $('stReconn').textContent = String(s.reconnects || 0);
    $('stPong').textContent = st === 'connected' ? (elapsedAgo(s.last_pong_at) || '从未') : '—';
    $('stServer').textContent = s.server_url || '—';
    $('stAudit').textContent = s.last_audit_ts ? fmtTime(s.last_audit_ts) : '—';

    $('connDot').className = 'dot ' + (st === 'connected' ? 'on' : 'off');
    $('connText').textContent = tagTxt;

    updateConnButtons(st);
}

// updateConnButtons: 连接 enabled unless already trying/online; 断开
// enabled only while a bridge exists (connecting/connected/reconnecting).
function updateConnButtons(st) {
    $('connectBtn').disabled = (st === 'connecting' || st === 'connected');
    $('disconnectBtn').disabled = !(st === 'connecting' || st === 'connected' || st === 'reconnecting');
}

/* ---------- manual connect / disconnect (0.6.0 T2) ---------- */

async function doConnect() {
    const btn = $('connectBtn');
    btn.disabled = true;
    try {
        await goApp().Connect();
        try { renderStatus(await goApp().GetStatus()); } catch { /* push follows */ }
    } catch (err) {
        flash($('saveMsg'), '连接失败：' + err, false);
        btn.disabled = false;
    }
}

async function doDisconnect() {
    const btn = $('disconnectBtn');
    btn.disabled = true;
    try {
        await goApp().Disconnect();
        try { renderStatus(await goApp().GetStatus()); } catch { /* push follows */ }
    } catch (err) {
        flash($('saveMsg'), '断开失败：' + err, false);
        btn.disabled = false;
    }
}

/* ---------- dir list rendering ---------- */

function renderDirs() {
    const ul = $('dirList');
    ul.innerHTML = '';
    if (state.dirs.length === 0) {
        const li = document.createElement('li');
        li.className = 'muted empty';
        li.textContent = '（尚未添加任何目录）';
        ul.appendChild(li);
        return;
    }
    state.dirs.forEach((d, i) => {
        const li = document.createElement('li');

        const chk = document.createElement('input');
        chk.type = 'checkbox';
        chk.checked = d.enabled;
        chk.title = '启用 / 禁用该目录';
        chk.addEventListener('change', () => { state.dirs[i].enabled = chk.checked; });

        const code = document.createElement('code');
        code.textContent = d.path;
        code.title = d.path;

        const del = document.createElement('button');
        del.type = 'button';
        del.className = 'btn danger';
        del.textContent = '删除';
        del.addEventListener('click', () => { state.dirs.splice(i, 1); renderDirs(); });

        li.append(chk, code, del);
        ul.appendChild(li);
    });
}

function addDir(path, silentDup) {
    const p = (path || '').trim();
    if (!p) return false;
    if (state.dirs.some((d) => d.path.toLowerCase() === p.toLowerCase())) {
        if (!silentDup) flash($('saveMsg'), '该目录已在列表中', false);
        return false;
    }
    state.dirs.push({ path: p, enabled: true });
    renderDirs();
    return true;
}

/* ---------- native directory picker (Wails runtime dialog) ---------- */

async function pickDirectory() {
    const btn = $('pickDirBtn');
    btn.disabled = true;
    const prev = btn.innerHTML;
    btn.textContent = '选择中…';
    try {
        const path = await goApp().PickDirectory();
        if (path) {
            addDir(path); // picked: appears in the list immediately
        }
        // cancelled (""): no change, no error
    } catch (err) {
        flash($('saveMsg'), '打开目录选择器失败：' + err, false);
    } finally {
        btn.disabled = false;
        btn.innerHTML = prev;
    }
}

/* ---------- common locations (one-click add, chip icons) ---------- */

// per-location chip icons (folder-ish glyphs, no icon library)
const CHIP_ICONS = {
    '用户目录': '🏠',
    '桌面': '🖥',
    '文档': '📄',
    '下载': '⬇',
};

async function loadCommonDirs() {
    try {
        const dirs = await goApp().GetCommonDirs();
        const box = $('commonChips');
        box.innerHTML = '';
        (dirs || []).forEach((d) => {
            const b = document.createElement('button');
            b.type = 'button';
            b.className = 'chip';
            b.title = d.path;
            const ico = document.createElement('span');
            ico.className = 'bi';
            ico.setAttribute('aria-hidden', 'true');
            ico.textContent = CHIP_ICONS[d.name] || '＋';
            const label = document.createElement('span');
            label.textContent = d.name;
            b.append(ico, label);
            b.addEventListener('click', () => addDir(d.path));
            box.appendChild(b);
        });
    } catch { /* no common locations available */ }
}

/* ---------- recent servers (dropdown + token refill) ---------- */

function renderRecent() {
    const dl = $('recentServers');
    dl.innerHTML = '';
    state.recent.forEach((r) => {
        const o = document.createElement('option');
        o.value = r.url;
        dl.appendChild(o);
    });
    $('recentHint').hidden = state.recent.length === 0;
}

// On pick: fill the URL and refill the remembered token for that entry.
function onServerPick() {
    const v = $('serverURL').value.trim();
    if (!v) return;
    const hit = state.recent.find((r) => r.url.toLowerCase() === v.toLowerCase());
    if (hit && hit.token) $('token').value = hit.token;
}

/* ---------- token eye toggle ---------- */

function wireTokenToggle() {
    const btn = $('toggleToken');
    const input = $('token');
    btn.addEventListener('click', () => {
        const show = input.type === 'password';
        input.type = show ? 'text' : 'password';
        btn.className = 'btn eye' + (show ? ' show' : '');
        btn.title = show ? '隐藏令牌' : '显示令牌';
        btn.setAttribute('aria-label', btn.title);
        input.focus();
    });
}

/* ---------- form validation (before SaveConfig is called) ---------- */

function validate() {
    const problems = [];
    const url = $('serverURL').value.trim();
    const token = $('token').value;
    const enabledDirs = state.dirs.filter((d) => d.enabled);

    if (!url) {
        problems.push('服务器地址不能为空');
    } else if (!/^wss?:\/\//i.test(url)) {
        problems.push('服务器地址必须以 wss:// 或 ws:// 开头');
    }
    if (!token.trim()) problems.push('访问令牌不能为空');
    if (enabledDirs.length === 0) {
        problems.push(state.dirs.length === 0
            ? '请至少添加一个白名单目录'
            : '请至少启用一个白名单目录（当前全部被禁用）');
    }
    return problems;
}

function markInvalid(problems) {
    const urlBad = problems.some((p) => p.indexOf('服务器地址') === 0);
    const tokenBad = problems.some((p) => p.indexOf('访问令牌') === 0);
    $('serverURL').classList.toggle('invalid', urlBad);
    $('token').classList.toggle('invalid', tokenBad);
}

function clearInvalidMarks() {
    $('serverURL').classList.remove('invalid');
    $('token').classList.remove('invalid');
}

/* ---------- form save ---------- */

async function saveConfig(e) {
    e.preventDefault();
    clearInvalidMarks();

    // Front-end gate: invalid input never reaches SaveConfig.
    const problems = validate();
    if (problems.length > 0) {
        markInvalid(problems);
        flash($('saveMsg'), '无法保存：' + problems.join('；'), false);
        return;
    }

    const next = {
        server_url: $('serverURL').value.trim(),
        token: $('token').value,
        allowed_dirs: state.dirs,
        allow_write: $('allowWrite').checked,
    };
    const btn = $('saveBtn');
    btn.disabled = true;
    try {
        await goApp().SaveConfig(next);
        state.saved = true;
        flash($('saveMsg'), '已保存，正在连接…', true);
        // the status card updates itself via the bridge:status event;
        // pull once now so "连接中" shows even before the first push
        try { renderStatus(await goApp().GetStatus()); } catch { /* keep card */ }
        state.existed = true;
        updateFormMode();
    } catch (err) {
        flash($('saveMsg'), String(err), false);
    } finally {
        btn.disabled = false;
    }
}

/* ---------- onboarding vs. settings mode ---------- */

function updateFormMode() {
    const first = state.existed === false;
    $('formTitle').textContent = first ? '欢迎使用 Octop 本地文件桥' : '连接设置';
    $('formHint').textContent = first
        ? '首次使用需要完成三步：添加白名单目录 → 填写服务器地址与令牌 → 保存并连接。'
        : '修改后保存即可热重连，无需重启应用。';
}

/* ---------- boot ---------- */

async function boot() {
    // Live status: event push + an initial pull (covers the pre-connect gap).
    window.runtime.EventsOn('bridge:status', renderStatus);
    window.runtime.EventsOn('bridge:audit', onAuditEvent); // activity page feed (M3)
    try { renderStatus(await goApp().GetStatus()); } catch { /* keep card text */ }

    // Sidebar navigation + per-page data.
    wireNav();
    showPage('conn');
    renderActivity();
    $('onlyErrors').addEventListener('change', () => {
        state.onlyErrors = $('onlyErrors').checked;
        renderActivity();
    });
    loadSettingsPage();

    // Version: sidebar footer badge (M3 moved it from the bottom-right corner).
    let ver = '';
    try { ver = 'v' + (await goApp().GetVersion()); } catch { /* omit */ }
    $('verCorner').textContent = ver;

    // Settings.
    try {
        const r = await goApp().LoadConfig();
        state.existed = r.existed;
        state.dirs = (r.file && r.file.allowed_dirs) || [];
        state.recent = (r.file && r.file.recent_servers) || [];
        $('serverURL').value = (r.file && r.file.server_url) || '';
        $('token').value = (r.file && r.file.token) || '';
        $('allowWrite').checked = r.file ? !!r.file.allow_write : true;
    } catch (err) {
        flash($('saveMsg'), '读取配置失败：' + err, false);
    }
    renderDirs();
    renderRecent();
    updateFormMode();
    loadCommonDirs();

    wireTokenToggle();
    $('serverURL').addEventListener('change', onServerPick);
    $('cfgForm').addEventListener('submit', saveConfig);
    $('connectBtn').addEventListener('click', doConnect);
    $('disconnectBtn').addEventListener('click', doDisconnect);
    $('pickDirBtn').addEventListener('click', pickDirectory);
    $('addDirBtn').addEventListener('click', () => {
        if (addDir($('newDir').value)) $('newDir').value = '';
    });
    // live re-validate once a problem has been shown
    $('serverURL').addEventListener('input', clearInvalidMarks);
    $('token').addEventListener('input', clearInvalidMarks);
}

boot();
