'use strict';

// ---------------------------------------------------------------- 基础

// 双击打开 index.html（file:// 协议）时，任何 /api 请求都会失败且报错难懂。
// 直接给出可操作的提示，别让人误以为是服务端坏了。
if (location.protocol === 'file:') {
  document.documentElement.innerHTML = `
    <body style="margin:0;display:grid;place-items:center;min-height:100vh;
                 background:#0d0d10;color:#e8e8ee;
                 font:15px/1.7 -apple-system,'Segoe UI','Microsoft YaHei',sans-serif">
      <div style="max-width:560px;padding:36px;background:#16161b;
                  border:1px solid #2a2a33;border-radius:14px">
        <h2 style="margin:0 0 12px;color:#ff3ea5">需要用服务器地址打开</h2>
        <p style="margin:0 0 14px;color:#8b8b99">
          你正在用 <code style="background:#000;padding:2px 6px;border-radius:5px">file://</code>
          协议打开这个文件，浏览器会去本地磁盘找 <code>style.css</code> 和
          <code>app.js</code>，所以页面没样式、按钮也没反应。
        </p>
        <p style="margin:0 0 14px;color:#8b8b99">正确做法：先启动服务，再访问 http 地址。</p>
        <pre style="background:#000;padding:14px;border-radius:9px;overflow:auto;
                    font-size:13px;color:#9fe8d5">python -m uvicorn app.main:app --port 8000

# 然后浏览器打开：
http://localhost:8000</pre>
        <p style="margin:14px 0 0;color:#8b8b99">
          Docker 部署的话是 <code style="background:#000;padding:2px 6px;border-radius:5px">
          http://localhost:8000</code>
        </p>
      </div>
    </body>`;
  throw new Error('opened via file://');
}

const $ = (s) => document.querySelector(s);
const $$ = (s) => Array.from(document.querySelectorAll(s));

function toast(msg, kind = '', ms = 3800) {
  const el = $('#toast');
  el.textContent = msg;
  el.className = 'toast ' + kind;
  clearTimeout(el._t);
  el._t = setTimeout(() => el.classList.add('hidden'), ms);
}

// 给 URL 补上 sharecode（分享模式下网关会拦掉没带它的请求）
//
// 为什么需要这个助手（2026-10-26，这是"登录完了主页面没反应"的真正根因）：
//
// WorkBuddy 部署出来的实例走分享链接模式，网关把关：
//     GET /api/accounts/oauth/result                -> 403（被网关拦，不到应用）
//     GET /api/accounts/oauth/result?sharecode=xxx  -> 200
//
// 而前端所有 fetch 都是裸路径。于是：
//   · OAuth 结果轮询每次 403 -> 代码里 `if (!r.ok) return` 静默跳过
//     -> 表现和 postMessage 失效一模一样，主页面永远等不到结果
//   · /api/docs 下载文档、/v1/files 上传素材同理，全都成了摆设
//
// 之前几个版本换着法子改通知机制（postMessage / BroadcastChannel /
// storage / 服务端轮询）都没用 —— **因为请求根本没到应用**。
//
// 做法：把当前页面的 search 原样保留，已带 sharecode 就不重复加。
// 非分享模式（自托管）时 search 为空，这个函数等于直接返回原 path。
function withShare(path) {
  try {
    const qs = location.search || '';
    if (!qs) return path;
    // 已经自带 query 的，用 & 连接
    const sep = path.includes('?') ? '&' : '?';
    // 去掉开头的 ?
    return path + sep + qs.replace(/^\?/, '');
  } catch (e) {
    return path;
  }
}

async function api(path, opts = {}) {
  const res = await fetch(withShare(path), {
    credentials: 'same-origin',
    headers: { 'content-type': 'application/json' },
    ...opts,
  });
  const text = await res.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch { data = { raw: text }; }
  if (!res.ok) {
    const msg = (data && (data.detail || data.error)) || `HTTP ${res.status}`;
    throw new Error(typeof msg === 'string' ? msg : JSON.stringify(msg));
  }
  return data;
}

function esc(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));
}

// ---------------------------------------------------------------- 首屏三态

function showActivate() {
  document.body.classList.remove('mode-login', 'mode-app');
  document.body.classList.add('mode-activate');
}

function showLogin() {
  document.body.classList.remove('mode-app', 'mode-activate');
  document.body.classList.add('mode-login');
}

function showApp() {
  document.body.classList.remove('mode-login', 'mode-activate');
  document.body.classList.add('mode-app');
  loadAll();
  // 账号/任务数据照旧全量拉一次（账号页的统计要用），但**落在哪个 tab**
  // 由 restoreTab 决定，不再固定回第一个。
  restoreTab();
  // 版本检查放最后：它要外呼 GitHub，不能拖慢首屏
  setTimeout(checkUpdate, 1200);
}

// 首屏判断顺序：激活 -> 主界面（不再要求登录）。
//
// 为什么去掉登录这一步（2026-10-25）：
//   客户各自部署自己的实例，"这台机器上只有主人一个人"是常态。
//   激活码本身就是他的身份凭证 —— 再让他记一个管理密码，只是多一层
//   他自己也会忘的门，忘了还得改配置文件恢复。
//
//   想让同一实例内区分人时，把 PICSART_POOL_ADMIN_PASSWORD 设上，
//   这里会自动回到"先登录"的流程（由后端 /api/me 的 need_login 告知）。
//
// 安全边界：激活后的管理页**任何拿到链接的人都能进**。链接要当密码保管。
// 未激活时仍然只有激活页 —— 那一步不会放松。
async function boot() {
  let lic = null;
  try {
    lic = await api('/api/license/status');
  } catch {
    // 拿不到授权状态（服务异常）时按"未激活"处理 —— 激活页至少能显示
    // 一个明确的错误，比抛一个空登录框有用。
    showActivate();
    showActivateError('无法读取授权状态，请刷新页面重试');
    return;
  }

  document.getElementById('act-hwid').textContent =
    lic.hwid || '（读取失败：请检查部署环境）';

  if (!lic.activated) {
    showActivate();
    return;
  }

  // 已激活。只有**设了管理密码**的实例才需要登录。
  try {
    const me = await api('/api/me');
    if (me.logged_in || me.need_login === false) showApp();
    else if (me.need_login) showLogin();
    else showApp();       // 旧后端没有 need_login 字段，按已授权处理
  } catch {
    // /api/me 出错不该把人挡在外面（已激活就是主人）
    showApp();
  }
}

function showActivateError(msg) {
  const el = document.getElementById('act-msg');
  if (el) { el.textContent = msg; el.className = 'err'; }
}

async function copyHwid() {
  const t = document.getElementById('act-hwid').textContent || '';
  if (!t || t.indexOf('读取失败') >= 0) return;
  try {
    await navigator.clipboard.writeText(t);
    showActivateError('');
    const el = document.getElementById('act-msg');
    if (el) { el.textContent = '部署标识已复制'; el.className = 'ok'; }
  } catch {
    // 非 https 或浏览器拒绝剪贴板权限时，退回"选中让用户自己复制"
    const r = document.createRange();
    r.selectNodeContents(document.getElementById('act-hwid'));
    const s = getSelection();
    s.removeAllRanges();
    s.addRange(r);
    showActivateError('已选中，请按 Ctrl+C 复制');
  }
}

// 激活表单提交。
// 成功后**不直接进主界面** —— 还要过登录（管理密码）那道门。
// 重走 boot() 是为了让"激活 -> 登录"的顺序判断只有一处，不重复实现。
async function doActivate(ev) {
  ev.preventDefault();
  const card = document.getElementById('act-card').value.trim();
  if (!card) { showActivateError('请先填激活码'); return; }

  const btn = document.getElementById('act-go');
  btn.disabled = true;
  btn.textContent = '激活中…';
  showActivateError('');

  try {
    const r = await fetch(withShare('/api/license/activate'), {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ card }),
    });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) {
      showActivateError(d.error || `激活失败（HTTP ${r.status}）`);
      btn.disabled = false;
      btn.textContent = '激活';
      return;
    }
    if (!d.config_ready) {
      // 激活成功但配置没下来 —— 说清楚，否则客户会以为能用
      showActivateError(
        '激活成功，但站点配置未下发（' + (d.error || '服务端可能版本过旧') +
        '）。请重启应用后重试。');
      btn.disabled = false;
      btn.textContent = '重试';
      return;
    }
    const el = document.getElementById('act-msg');
    if (el) {
      el.textContent = `激活成功（${d.tier || ''}，到期 ` +
        new Date((d.expire || 0) * 1000).toLocaleDateString() + '）';
      el.className = 'ok';
    }
    setTimeout(() => boot(), 900);
  } catch (e) {
    showActivateError('请求失败：' + e.message);
    btn.disabled = false;
    btn.textContent = '激活';
  }
}

async function doLogin(ev) {
  ev.preventDefault();
  const pw = $('#pw').value;
  try {
    await api('/api/login', { method: 'POST', body: JSON.stringify({ password: pw }) });
    showApp();
  } catch (e) {
    $('#login-err').textContent = e.message;
  }
}

async function doLogout() {
  await api('/api/logout', { method: 'POST' });
  location.reload();
}

// ---------------------------------------------------------------- tabs

// 之前每次刷新都掉回「账号」页：tab 只活在内存里，location.reload() 一按，
// index.html 里写死的 class="active" 就把它按回第一个。
// 现在两处都记住 —— localStorage 管「我上次在看哪」，URL hash 管「这一眼
// 要不要能分享/收藏」。hash 优先，这样从别的页面点 /#tasks 进来也落得对。
const TAB_KEY = 'pool.tab';

function tabs() { return $$('.tab'); }

// 有效的 tab 名。非法值（旧版本残留、手打错 hash）一律回落到默认。
function normTab(name) {
  return tabs().some((b) => b.dataset.tab === name) ? name : 'accounts';
}

function switchTab(name, { push = true } = {}) {
  const tab = normTab(name);
  tabs().forEach((b) => b.classList.toggle('active', b.dataset.tab === tab));
  $$('.tab-panel').forEach((p) =>
    p.classList.toggle('active', p.id === 'tab-' + tab));

  try { localStorage.setItem(TAB_KEY, tab); } catch { /* 隐私模式/存储满 */ }

  // replaceState 不触发 hashchange，所以这里不需要防重入标志。
  // 用 replaceState 而不是直接赋 hash：直接赋值会往 history 里堆记录，
  // 切几个 tab 之后得按好几次返回才退得出页面。
  if (push && location.hash.slice(1) !== tab) {
    history.replaceState(null, '', '#' + tab);
  }

  if (tab === 'tasks') loadTasks();
  if (tab === 'settings') loadHealth();
}

// 用户/别的页面改了 hash（后退、收藏夹、手打地址）—— 同步状态，
// push:false 避免又把 hash 写回去。
window.addEventListener('hashchange', () => {
  switchTab(location.hash.slice(1), { push: false });
});

// 登录后 restoreTab() 决定落在哪一页；没登录时不碰 hash（登录页在前）。
// 没有历史记录且 hash 为空时，保持 URL 干净 —— 别白写一个 #accounts 上去。
function restoreTab() {
  let want = location.hash.slice(1);
  if (!want) {
    try { want = localStorage.getItem(TAB_KEY) || ''; } catch { want = ''; }
  }
  switchTab(want, { push: Boolean(want) });
}

// 按钮点击 -> 切 tab。数据加载在 switchTab 里按 tab 名触发，这里只管转发。
tabs().forEach((btn) => { btn.onclick = () => switchTab(btn.dataset.tab); });

// ---------------------------------------------------------------- 数据加载

async function loadAll() {
  await Promise.all([loadStats(), loadAccounts(), loadHealth()]);
  buildCurl();
  buildMediaDemo();
}

async function loadStats() {
  try {
    const s = await api('/api/stats');
    const ok = s.accounts_ok || 0;
    const total = s.accounts_total || 0;
    const dead = s.accounts_dead || 0;
    // free 档：状态健康但**没有 wan3 权限**（2026-10-26 加）。
    // 它不计入"可用"，所以必须单独显示 —— 否则用户会觉得"我明明加了
    // 3 个号，怎么只有 0 个可用"，而且完全不知道原因。
    const freeTier = s.accounts_free || 0;
    // 「冷却中」已经不存在了（队列制不用冷却），改成「可用/总数」的直观表达
    $('#stats').innerHTML = `
      <div class="stat ${ok ? 'ok' : 'bad'}"><div class="n">${ok}<span class="of">/${total}</span></div><div class="l">可用账号</div></div>
      <div class="stat ${freeTier ? 'warn' : ''}" title="free 档账号没有 wan3 权限，需要升级订阅"><div class="n">${freeTier}</div><div class="l">free 无权限</div></div>
      <div class="stat ${dead ? 'bad' : ''}"><div class="n">${dead}</div><div class="l">失效</div></div>
      <div class="stat"><div class="n">${s.tasks_24h || 0}</div><div class="l">24h 任务</div></div>
      <div class="stat ok"><div class="n">${s.tasks_completed || 0}</div><div class="l">成功</div></div>
      <div class="stat ${s.tasks_failed ? 'warn' : ''}"><div class="n">${s.tasks_failed || 0}</div><div class="l">失败</div></div>
    `;
  } catch (e) { toast(e.message, 'err'); }
}

async function loadAccounts() {
  let data;
  try { data = await api('/api/accounts'); } catch (e) { return toast(e.message, 'err'); }
  const tb = $('#acc-table tbody');
  if (!data.accounts.length) {
    // 这个表是 5 列，colspan 必须一致，否则空态会把表格撑歪
    tb.innerHTML = `<tr><td colspan="5" class="dim" style="text-align:center;padding:26px">
      还没有账号，点右上角「添加账号」开始</td></tr>`;
    return;
  }
  tb.innerHTML = data.accounts.map((a) => {
    // 状态与令牌现在都由后端推导（a.health），避免前端拼出矛盾显示。
    // 之前出现过「状态：正常」+「令牌：待续期」同时出现。
    const HEALTH = {
      ready:     ['可用',   'ok'],
      renewable: ['待续期', 'cooling'],
      relogin:   ['需重登', 'dead'],
    };
    const [hTxt, hCls] = HEALTH[a.health] || [a.status, 'cooling'];

    // tier 徽章。free 档要醒目 —— 它的号状态健康但**不能跑 wan3**
  // （2026-10-26）。不加区分的话用户会以为这号能用，提交后莫名失败。
  const _tier = String(a.tier || '').toLowerCase();
  const tierBadge = a.tier
    ? `<span class="tier-badge${_tier === 'free' ? ' tier-free' : ''}"
        title="${_tier === 'free' ? 'free 档没有 wan3 权限，需要升级订阅' : esc(a.tier)}"
       >${esc(a.tier)}${_tier === 'free' ? ' · 无权限' : ''}</span>`
    : '';
    const uname = a.username ? `<b>${esc(a.username)}</b>` : '<span class="dim">未知</span>';

    // 令牌：直接说还剩多久；已过期就说明靠什么续
    let tok;
    if (a.health === 'relogin') {
      tok = '<span class="dim">—</span>';
    } else if (a.ttl > 0) {
      tok = `<span class="badge ok">${fmtTtl(a.ttl)}</span>`;
    } else if (a.has_refresh) {
      tok = '<span class="badge cooling">自动续期</span>';
    } else {
      tok = '<span class="badge cooling">Cookie</span>';
    }

    const err = a.last_error
      ? `<div class="dim err-line" title="${esc(a.last_error)}">${esc(a.last_error.slice(0, 28))}</div>` : '';

    return `<tr>
      <td>
        <b>${esc(a.name)}</b>
        ${a.notes ? `<div class="dim" style="font-size:11px">${esc(a.notes)}</div>` : ''}
      </td>
      <td>${uname} ${tierBadge}</td>
      <td><span class="badge ${hCls}">${hTxt}</span>${err}</td>
      <td>${tok}</td>
      <td class="nowrap" data-acc="${a.id}" data-name="${esc(a.name)}">
        <button class="ghost mini" data-act="check">检查</button>
        <button class="ghost mini${a.health === 'relogin' ? ' attention' : ''}"
                data-act="relogin"
                title="用同一个 Picsart 账号重新授权，刷新失效的令牌">重新登录</button>
        <button class="ghost mini" data-act="reset"
                title="清除失败计数，立即重试">重置</button>
        <button class="ghost mini danger" data-act="del"
                title="作废 token 并从号池移除">删除</button>
      </td>
    </tr>`;
  }).join('');
}

// 账号表格的按钮用事件委托，不把名字拼进 onclick。
//
// 为什么必须这样：以前写的是 onclick="relogin(${a.id},'${esc(a.name)}')"。
// esc() 把 ' 转成 &#39;，看起来安全 —— 但**属性值里的 HTML 实体会被浏览器
// 先解码再交给 JS 解析**，于是 &#39; 又变回 '，账号名 a');alert(1)// 就能
// 直接执行。而账号名来自 Picsart 用户名，配合不需要鉴权的 /oauth/callback
// 就能写进库 → 管理员一打开「账号」页即中招（存储型 XSS）。
//
// data-name 走的是"属性值 → dataset"路径，浏览器不会把它当代码解析，
// 无论内容是什么都只是字符串。
const ACC_ACTIONS = {
  check:   (id) => checkAcc(id),
  relogin: (id, name) => relogin(id, name),
  reset:   (id, name) => resetAcc(id, name),
  del:     (id, name) => delAcc(id, name),
};

$('#acc-table').addEventListener('click', (ev) => {
  const btn = ev.target.closest('button[data-act]');
  if (!btn) return;
  const cell = btn.closest('td[data-acc]');
  if (!cell) return;
  const fn = ACC_ACTIONS[btn.dataset.act];
  if (!fn) return;
  fn(Number(cell.dataset.acc), cell.dataset.name || '');
});

function fmtTtl(sec) {
  const s = Math.max(0, Math.floor(sec));
  if (s >= 3600) return `${Math.floor(s / 3600)}h${Math.floor((s % 3600) / 60)}m`;
  if (s >= 60) return `${Math.floor(s / 60)}m`;
  return `${s}s`;
}

async function loadTasks() {
  let data;
  try { data = await api('/api/tasks?limit=50'); } catch (e) { return toast(e.message, 'err'); }
  const ts = data.tasks || [];

  // 顶部统计：一眼看出堵在哪
  const n = (s) => ts.filter((t) => t.status === s).length;

  // 「并发能力」= 真实可用账号数（2026-10-26 修）。
  //
  // 原来用 `data.capacity || 1`，两个问题叠加：
  //   1. 后端 capacity = max(1, 有凭证账号数)，**不看 status**
  //      -> 账号全 dead 时也显示 2
  //   2. 前端的 `|| 1` 又会把 0 变成 1
  // 结果就是客户看到的「可用账号 0/2」+「并发能力 2」自相矛盾。
  //
  // 后端现在额外给 `live`（未钳制的真实可用数），展示用它；
  // `capacity` 仍参与"还要等几轮"的除法，所以保留并兜底兼容旧后端。
  const liveCap = data.live ?? data.capacity ?? 0;

  $('#task-stats').innerHTML = `
    <div class="stat ok"><div class="n">${n('in_progress')}</div><div class="l">生成中</div></div>
    <div class="stat ${n('queued') ? 'warn' : ''}"><div class="n">${n('queued')}</div><div class="l">排队中</div></div>
    <div class="stat ok"><div class="n">${n('completed')}</div><div class="l">完成</div></div>
    <div class="stat ${n('failed') ? 'bad' : ''}"><div class="n">${n('failed')}</div><div class="l">失败</div></div>
    <div class="stat ${n('unknown') ? 'warn' : ''}"><div class="n">${n('unknown')}</div><div class="l">状态未知</div></div>
    <div class="stat ${liveCap ? '' : 'bad'}"><div class="n">${liveCap}</div><div class="l">并发能力</div></div>
  `;

  const tb = $('#task-table tbody');
  if (!ts.length) {
    tb.innerHTML = `<tr><td colspan="8" class="dim" style="text-align:center;padding:26px">暂无任务</td></tr>`;
    return;
  }

  tb.innerHTML = ts.map((t) => {
    const LABEL = {
      queued: ['排队中', 'cooling'],
      in_progress: ['生成中', 'ok'],
      completed: ['完成', 'ok'],
      failed: ['失败', 'dead'],
      // 用户主动取消。**不是 failed** —— 「我点了取消」和「任务失败了」
      // 是两回事，混成同一个颜色会让人以为出了故障。
      cancelled: ['已取消', 'dim'],
      unknown: ['状态未知', 'cooling'],
    };
    const [txt, cls] = LABEL[t.status] || [t.status, 'cooling'];

    // 进度条：只显示上游给的真实进度，不造数
    let prog = '—';
    if (t.status === 'in_progress' && t.upstream_waiting) {
      // 上游状态 ACCEPTED = 已受理、等调度中，此时上游不给 percent。
      // 直接展示状态 + 已等时长，比编一个百分比诚实也有用。
      // 秒数内嵌在同一个标签里，不再拆成「上面一个框 + 下面一行小字」两块。
      const waited = t.started_at
        ? Math.round(Date.now() / 1000 - t.started_at) : (t.age || 0);
      prog = `<div class="waiting-tag">上游排队等调度` +
             `<span class="wt-sec">已等 ${fmtDur(waited)}</span></div>`;
    } else if (t.status === 'in_progress') {
      const p = Math.max(0, Math.min(100, t.percent || 0));
      prog = `<div class="prog" title="上游真实进度 ${p}%"><i style="width:${p}%"></i><span>${p}%</span></div>`;
    } else if (t.status === 'completed') {
      prog = `<div class="prog done"><i style="width:100%"></i><span>100%</span></div>`;
    } else if (t.status === 'queued') {
      prog = '<span class="dim">等待中</span>';
    }

    // 排队位置：前面还有几个 + 预计第几轮
    let qinfo = '';
    if (t.status === 'queued' && t.queue_pos) {
      qinfo = `<div class="qbadge">前面还有 ${t.queue_pos - 1} 个${
        t.rounds > 1 ? ` · 约第 ${t.rounds} 轮` : ''}</div>`;
    }

    // 耗时拆解：排队 / 上游调度 / 真实生成，合成一个块显示
    let timing = '—';
    const short = (v) => (v == null ? '—' : fmtDur(v));
    if (t.status === 'queued') {
      timing = `<span class="dim">已等 ${fmtDur(t.age)}</span>`;
    } else if (t.status === 'in_progress') {
      const run = t.started_at ? Math.round(Date.now() / 1000 - t.started_at) : 0;
      timing = `<span title="已占账号 ${fmtDur(run)}">${fmtDur(run)}</span>`;
      if (t.wait_s != null) {
        timing += `<span class="dim"> · 排队 ${short(t.wait_s)}</span>`;
      }
    } else {
      // 完成/失败：三段拆解
      const parts = [];
      if (t.wait_s != null) parts.push(`排队 <b>${short(t.wait_s)}</b>`);
      if (t.schedule_s != null) parts.push(`调度 <b>${short(t.schedule_s)}</b>`);
      if (t.gen_s != null) parts.push(`生成 <b>${short(t.gen_s)}</b>`);
      if (parts.length) {
        timing = `<div class="segs">${parts.join(' · ')}` +
                 (t.elapsed ? `<span class="dim"> · 总 ${short(t.elapsed)}</span>` : '') + `</div>`;
      } else {
        timing = short(t.elapsed);
      }
    }

    const res = t.url
      ? `<a href="${esc(t.url)}" target="_blank" rel="noopener" class="link">打开</a>`
      : (t.error
          ? `<span class="dim err-line" title="${esc(t.error)}">${esc(t.error.slice(0, 30))}</span>`
          : '—');

    // 重试 / 取消按钮。两个都只在**后端会答应**的情况下出现，
    // 免得用户点到一个必然报错的按钮。
    //
    // 重试（/retry 的拒绝条件）：
    //   · 有 upstream_id -> 上游可能还在跑/已出片，重提会重复出片
    //   · queued/in_progress -> 正在处理
    //   · completed -> 已完成
    //
    // 取消（/cancel 的拒绝条件，2026-10-25 加）：
    //   · 有 upstream_id -> 上游没取消接口，本地取消只会让账号被
    //     一个看不见的任务锁住（每号并发=1）。后端会 409 说明原因。
    const canRetry = t.status === 'failed' && !t.upstream_id;
    const canCancel = (t.status === 'queued' || t.status === 'in_progress')
                      && !t.upstream_id;

    const btns = [];
    if (canCancel) {
      btns.push(`<button class="ghost mini" data-cancel="${esc(t.id)}"
                 title="取消这个还在排队的任务（还没提交到上游）">取消</button>`);
    }
    if (canRetry) {
      btns.push(`<button class="ghost mini" data-retry="${esc(t.id)}"
                 title="用原参数重新跑一次（不用重填）">重试</button>`);
    }
    const act = btns.length ? btns.join(' ') : '<span class="dim">—</span>';

    // 同理：任务 id 不拼进 onclick，走 data-* + 事件委托（见下面的监听器）。
    return `<tr class="task-row" data-tid="${esc(t.id)}">
      <td class="mono dim nowrap">${fmtTime(t.created_at)}</td>
      <td class="nowrap"><span class="badge ${cls}">${txt}</span>${qinfo}</td>
      <td>${prog}</td>
      <td class="param-cell" title="${esc(t.summary)}">${esc(t.summary)}</td>
      <td class="nowrap">${esc(t.account_name || '—')}</td>
      <td class="nowrap">${timing}</td>
      <td>${res}</td>
      <td class="nowrap">${act}</td>
    </tr>`;
  }).join('');
}

// 任务重试：用 tasks.params 里的原始快照重跑，用户不用重填。
// 取消一个还没提交到上游的任务。
//
// 后端只允许这一种（/api/tasks/{tid}/cancel）：上游没有取消接口，
// 已提交的任务本地取消只会让该账号被一个"看不见的任务"占着 ——
// 每号并发=1，那样队列会卡住。所以后端会 409 并说清原因，
// 这里把原因原样显示给用户。
async function cancelTask(tid) {
  if (!confirm('取消这个任务？\n\n它还在排队，尚未提交到上游，取消不会有任何副作用。')) {
    return;
  }
  try {
    await api(`/api/tasks/${encodeURIComponent(tid)}/cancel`, { method: 'POST' });
    toast('已取消', 'ok');
    loadTasks();
    loadStats();
  } catch (e) {
    // 409 的文案本来就是给用户看的（说明为什么不能取消），原样显示
    toast(e.message, 'err', 8000);
  }
}

async function retryTask(tid) {
  try {
    const r = await api(`/api/tasks/${encodeURIComponent(tid)}/retry`,
                        { method: 'POST' });
    toast(r.status === 'queued' ? '已重新入队' : '已提交重试', 'ok');
    loadTasks();
    loadStats();
  } catch (e) {
    // 409 的文案本来就是给用户看的（说明为什么不能重试），原样显示
    toast(e.message, 'err', 7000);
  }
}

// 任务行点击（事件委托，替代原来的 onclick="openTask('id')"）
$('#task-table').addEventListener('click', (ev) => {
  // 按钮优先于"打开详情"：否则点按钮会同时弹出任务详情
  const rb = ev.target.closest('button[data-retry]');
  if (rb) {
    ev.stopPropagation();
    retryTask(rb.dataset.retry);
    return;
  }
  const cb = ev.target.closest('button[data-cancel]');
  if (cb) {
    ev.stopPropagation();
    cancelTask(cb.dataset.cancel);
    return;
  }
  // 行内的链接（打开成片）要照常跳转，不要被这里吞掉
  if (ev.target.closest('a')) return;
  const row = ev.target.closest('tr[data-tid]');
  if (row) openTask(row.dataset.tid);
});

// 相对时间：3s / 2m / 1h
function fmtDur(sec) {
  const s = Math.max(0, Math.round(Number(sec) || 0));
  if (s < 60) return s + 's';
  if (s < 3600) return Math.floor(s / 60) + 'm' + (s % 60 ? (s % 60) + 's' : '');
  return Math.floor(s / 3600) + 'h' + Math.floor((s % 3600) / 60) + 'm';
}

// 绝对时间：今天显示时分秒，更早显示月日
function fmtTime(ts) {
  if (!ts) return '—';
  const d = new Date(ts * 1000);
  const now = new Date();
  const same = d.toDateString() === now.toDateString();
  const p = (x) => String(x).padStart(2, '0');
  const hm = `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
  return same ? hm : `${d.getMonth() + 1}/${d.getDate()} ${hm}`;
}

// 任务详情：完整提交参数 + 上下游 id + 时间线
async function openTask(tid) {
  try {
    const t = await api('/api/tasks/' + encodeURIComponent(tid));
    $('#task-title').textContent = '任务 ' + tid.slice(0, 16);

    const rows = [
      ['本地 ID', t.id],
      ['上游 ID', t.upstream_id || '—（SSE 模式下可能拿不到）'],
      ['状态', t.status],
      ['账号', t.account_name || '—'],
      ['模型 / 档位', `${t.model || '—'} / ${t.resolution || '—'}`],
      ['错误', t.error || '—'],
      ['结果', t.url ? `<a href="${esc(t.url)}" target="_blank" class="link">${esc(t.url)}</a>` : '—'],
    ];

    // 时间线：五个节点 + 三段耗时
    const tl = t.timeline || {};
    const abs = (v) => (v ? new Date(v * 1000).toLocaleString() : '—');
    const nodes = [
      ['① 用户发起', tl.created, ''],
      ['② 拿到账号', tl.started, t.wait_s != null ? `排队 ${fmtDur(t.wait_s)}` : ''],
      ['③ 上游受理', tl.submitted, t.schedule_s != null ? `受理耗时 ${fmtDur(t.schedule_s)}` : ''],
      ['④ 开始出片', tl.upstream, t.gen_s != null ? `出片 ${fmtDur(t.gen_s)}` : ''],
      ['⑤ 完成', tl.finished, t.elapsed ? `总计 ${fmtDur(t.elapsed)}` : ''],
    ];
    const tlHtml = `<h3 style="margin:18px 0 8px;font-size:13px">时间线</h3>
      <table class="kv">${nodes.map(([k, v, note]) =>
        `<tr><td style="width:110px">${esc(k)}</td>
             <td>${v ? esc(abs(v)) : '<span class="dim">—</span>'}
                 ${note ? `<span class="dim" style="font-size:11px"> · ${esc(note)}</span>` : ''}</td></tr>`
      ).join('')}</table>`;

    $('#task-body').innerHTML =
      `<table class="kv">${rows.map(([k, v]) =>
        `<tr><td style="width:110px">${esc(k)}</td><td>${k === '结果' ? v : esc(String(v))}</td></tr>`
      ).join('')}</table>
      ${tlHtml}
      <h3 style="margin:18px 0 8px;font-size:13px">提交参数</h3>
      <pre class="detail-pre">${esc(JSON.stringify(t.params_obj || {}, null, 2))}</pre>`;
    $('#task-modal').classList.remove('hidden');
  } catch (e) { toast(e.message, 'err'); }
}

function closeTask() {
  $('#task-modal').classList.add('hidden');
}


// 任务页自动刷新（排队中的任务状态会变）
let autoTimer = null;
function toggleAuto() {
  const on = $('#auto-refresh').checked;
  clearInterval(autoTimer);
  if (on) autoTimer = setInterval(() => {
    if (document.querySelector('#tab-tasks.active')) loadTasks();
  }, 3000);
}

// 离开任务页时暂停，省得在别的页签空转
document.addEventListener('visibilitychange', () => {
  if (document.hidden) clearInterval(autoTimer);
  else if ($('#auto-refresh') && $('#auto-refresh').checked) toggleAuto();
});

async function loadHealth() {
  let h;
  try { h = await api('/api/health'); } catch (e) { return; }
  $('#cfg-table').innerHTML = `
    <tr><td>默认分辨率</td><td><code>${esc(h.default_resolution)}</code>（只跑免费档：480P / 720P）</td></tr>
    <tr><td>默认画幅</td><td><code>${esc(h.default_ratio)}</code></td></tr>
    <tr><td>默认时长</td><td><code>${esc(h.default_duration)}</code> 秒</td></tr>
    <tr><td>收费档自动降级</td><td>${h.free_fallback.map((x) => `<code>${esc(x)}</code>`).join(' → ')}</td></tr>
    <tr><td>对外地址</td><td><code>${esc(h.public_url)}</code></td></tr>
    <tr><td>OAuth 回调</td><td><code>${esc(h.redirect_uri)}</code></td></tr>
    <tr><td>OAuth Client</td><td>${h.client ? `<code>${esc(h.client.client_id)}</code>` : '<span class="dim">尚未注册（首次登录时自动创建）</span>'}</td></tr>
    <tr><td>API Key 校验</td><td>${h.api_keys_enabled ? '已开启' : '<span class="dim">未设置（开放访问）</span>'}</td></tr>
    <tr><td>管理密码</td><td>${h.admin_password_set ? '已设置' : '<span class="warn">未设置</span>'}</td></tr>
  `;
}

function buildCurl() {
  const origin = location.origin;
  if ($('#usage-base')) $('#usage-base').textContent = origin;
  // 带文件的示例单独一块（见 renderUploadDemo）—— 它和文生视频
  // 的调用形状差别大（multipart vs JSON），混在一个代码块里会难读。
  renderUploadDemo(origin);
  $('#curl-demo').textContent =
`# 1) 文生视频（默认异步，立即返回 task id）
curl -X POST ${origin}/v1/videos/generations \\
  -H "content-type: application/json" \\
  -H "authorization: Bearer YOUR_KEY" \\
  -d '{
    "prompt": "一只金毛在海边沙滩上奔跑，夕阳，电影感",
    "resolution": "720P",
    "ratio": "16:9",
    "duration": 5
  }'

# 2) 拿上面返回的 id 轮询（间隔 10~15 秒）
curl ${origin}/v1/videos/generations/vtask_xxxx \\
  -H "authorization: Bearer YOUR_KEY"

# 3) 图生视频：首帧 + @名字 引用
curl -X POST ${origin}/v1/videos/generations \\
  -H "content-type: application/json" \\
  -H "authorization: Bearer YOUR_KEY" \\
  -d '{
    "prompt": "@主角 拿起 @杯子 喝了一口",
    "resolution": "720P",
    "duration": 5,
    "image_urls": [
      {"url": "https://.../person.png", "name": "主角"},
      {"url": "https://.../cup.png",    "name": "杯子"}
    ]
  }'

# 4) 首尾帧（不能和其他素材混用）
curl -X POST ${origin}/v1/videos/generations \\
  -H "content-type: application/json" \\
  -H "authorization: Bearer YOUR_KEY" \\
  -d '{
    "prompt": "花瓣从花苞到盛开",
    "resolution": "720P",
    "ratio": "9:16",
    "duration": 5,
    "start_frame": "https://.../start.png",
    "end_frame":   "https://.../end.png"
  }'

# 5) 号池状态 / 模型列表
curl ${origin}/v1/credits -H "authorization: Bearer YOUR_KEY"
curl ${origin}/v1/models  -H "authorization: Bearer YOUR_KEY"

# 枚举值：resolution 480P|720P · duration -1 或 2~30
#         ratio 16:9 9:16 1:1 4:3 3:4 adaptive
# 提示：wait:true 可同步等出片，但会占用连接数分钟，生产环境建议异步`;
}

// 素材编号示例（接入页那张 pre）
function buildMediaDemo() {
  const el = $('#usage-media-demo');
  if (!el) return;
  el.textContent =
`{
  "prompt": "@主角 拿起 @杯子 喝了一口，露出满意的表情",
  "resolution": "720P",
  "duration": 5,
  "image_urls": [
    {"url": "https://.../person.png", "name": "主角"},
    {"url": "https://.../cup.png",    "name": "杯子"}
  ]
}`;
}

function copyCurl() {
  navigator.clipboard.writeText($('#curl-demo').textContent)
    .then(() => toast('已复制', 'ok'))
    .catch(() => toast('复制失败', 'err'));
}

// 带本地文件的调用示例。
//
// 用 -F 而不是 JSON + base64：文件原样流式转发，不膨胀、
// 不会把整个文件读进内存。这一点对 20MB 的视频参考差别很大。
function renderUploadDemo(origin) {
  const el = $('#upload-demo');
  if (!el) return;
  el.textContent =
`# 图生视频：图片随请求带上（一步完成）
curl -X POST ${origin}/v1/videos/generations \\
  -H "authorization: Bearer YOUR_KEY" \\
  -F "prompt=让它动起来" \\
  -F "image=@photo.jpg" \\
  -F "resolution=720P" \\
  -F "duration=5"

# 多个素材：同名传多次即可
  -F "image=@a.jpg" -F "image=@b.jpg"
# prompt 里写 图1 / 图2，或给 -F "image=@a.jpg;..." 配名字（用 JSON 时更直观）

# 首尾帧
  -F "start_frame=@first.jpg" -F "end_frame=@last.jpg"

# 只想先拿链接（同一素材复用多次时）
curl -X POST ${origin}/v1/files \\
  -H "authorization: Bearer YOUR_KEY" \\
  -F "file=@photo.jpg"
# 返回 {"url": "https://..."}，把它填进 image_urls / video_urls / audio_urls`;
}

function copyUploadCurl() {
  const t = $('#upload-demo')?.textContent || '';
  if (!t) { toast('示例还没渲染出来', 'err'); return; }
  navigator.clipboard.writeText(t)
    .then(() => toast('已复制', 'ok'))
    .catch(() => toast('复制失败', 'err'));
}

// ---------------------------------------------------------------- 接口文档

// 文档由后端 `/api/docs` **现生成**（见 app/docs.py），不是静态文件。
// 原因：文档里的 Base URL、免费档、默认值都来自运行期配置。
// 写成静态文件必然和实际部署漂移 —— 客户照文档写的代码跑不通。
//
// 两个动作打同一个接口，只差 ?download=1（Content-Disposition）：
//   复制全文 -> fetch 后进剪贴板（给 AI 用，或贴进聊天窗口）
//   下载 .md -> 走浏览器下载（文件名带日期，存多份能区分新旧）

async function fetchDocs(download) {
  // 用 fetch 而不是直接 window.open：需要拿到正文才能判断成功/失败，
  // 也才能做"复制"。直接 open 一个失败响应只会给用户一个空白标签页。
  const res = await fetch(withShare('/api/docs' + (download ? '?download=1' : '')), {
    credentials: 'same-origin',
  });
  if (!res.ok) {
    // 401（未登录）/ 403（未激活）都走这里。给出可读原因，
    // 而不是让用户对着一个裸的 HTTP 状态码猜。
    let msg = `HTTP ${res.status}`;
    try {
      const d = await res.json();
      msg = d.detail || d.error || msg;
    } catch { /* 非 JSON 响应，保留状态码 */ }
    throw new Error(msg);
  }
  return await res.text();
}

async function copyDocs() {
  try {
    const md = await fetchDocs(false);
    await navigator.clipboard.writeText(md);
    toast(`已复制 ${md.length} 字符的文档`, 'ok');
  } catch (e) {
    toast('复制失败：' + e.message, 'err', 6000);
  }
}

async function downloadDocs() {
  // 先确认接口通（否则点下载会得到一个内容是错误页的 .md 文件），
  // 再用 blob 触发下载 —— 这样文件名和内容都由我们控制。
  try {
    const md = await fetchDocs(false);
    const day = new Date().toISOString().slice(0, 10).replace(/-/g, '');
    const blob = new Blob([md], { type: 'text/markdown;charset=utf-8' });
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = `wan3-api-${day}.md`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    // 释放 object URL，否则这份内容会在内存里留到页面刷新
    setTimeout(() => URL.revokeObjectURL(a.href), 1000);
    toast('已下载', 'ok');
  } catch (e) {
    toast('下载失败：' + e.message, 'err', 6000);
  }
}

// ---------------------------------------------------------------- 调用测试
//
// 布局照 fal playground / Recraft Studio 的工作台形态：
//   左栏调参（prompt 突出 + 高级设置折叠 + Form/JSON 切换）
//   右栏看结果（Preview/JSON 切换，播放器是主视图）
//   底部历史记录，点一下把参数灌回左栏（Recraft 的 Reuse）
//
// 为什么不再用「接口下拉」：四种语义挤在一个面板里，字段时隐时现，
// 用起来像调试器不像工具。现在一屏一个动作 —— 生成是主流程，
// models/credits 是辅助，做成右上角小按钮。

const PG = {
  timer: null,     // 轮询定时器
  tid: '',         // 正在轮询的任务
  sending: false,
  startedAt: 0,
  hist: [],        // 最近调用（只留本次会话）
};

// ---------- 视图切换 ----------

// 左栏：表单 / JSON
function pgView(which) {
  $('#pg-tab-form').classList.toggle('active', which === 'form');
  $('#pg-tab-json').classList.toggle('active', which === 'json');
  $('#pg-form').classList.toggle('hidden', which !== 'form');
  $('#pg-json-pane').classList.toggle('hidden', which !== 'json');
  if (which === 'json') pgSyncUp();
}

// 右栏：预览 / 响应 JSON
function pgOut(which) {
  $('#pg-tab-preview').classList.toggle('active', which === 'preview');
  $('#pg-tab-resp').classList.toggle('active', which === 'resp');
  $('#pg-preview').classList.toggle('hidden', which !== 'preview');
  $('#pg-resp-pane').classList.toggle('hidden', which !== 'resp');
}

// ---------- 参数收集 / 回填 ----------

function pgCollect() {
  const b = {};
  const prompt = $('#pg-prompt').value.trim();
  if (prompt) b.prompt = prompt;
  b.resolution = $('#pg-resolution').value;
  b.ratio = $('#pg-ratio').value;
  const dur = parseInt($('#pg-duration').value, 10);
  if (!Number.isNaN(dur)) b.duration = dur;
  const seed = $('#pg-seed').value.trim();
  if (seed !== '') b.seed = parseInt(seed, 10);
  const neg = $('#pg-negative').value.trim();
  if (neg) b.negative_prompt = neg;
  const model = $('#pg-model').value.trim();
  if (model) b.model = model;
  if ($('#pg-start').value.trim()) b.start_frame = $('#pg-start').value.trim();
  if ($('#pg-end').value.trim()) b.end_frame = $('#pg-end').value.trim();
  if ($('#pg-wait').checked) b.wait = true;
  if ($('#pg-audio').checked) b.audio = true;
  if ($('#pg-watermark').checked) b.watermark = true;
  if ($('#pg-thinking').checked) b.enable_thinking = true;
  if ($('#pg-extend').checked) b.prompt_extend = true;

  // 素材按类型分组。裸 URL -> 字符串；带名字 -> {url,name}（上游只认 图N）
  const buckets = {};
  $$('#pg-media-list .pg-card').forEach((row) => {
    const type = row.querySelector('.pg-c-type').value;
    const url = row.querySelector('.pg-c-url').value.trim();
    const name = row.querySelector('.pg-c-name').value.trim();
    if (!url) return;
    (buckets[type] ??= []).push({ url, name });
  });
  const KEY = { image: 'image_urls', video: 'video_urls', audio: 'audio_urls' };
  for (const [type, items] of Object.entries(buckets)) {
    b[KEY[type]] = items.map((x) => (x.name ? { url: x.url, name: x.name } : x.url));
  }
  return b;
}

function pgFillForm(b) {
  $('#pg-prompt').value = b.prompt || '';
  $('#pg-resolution').value = b.resolution || '720P';
  $('#pg-ratio').value = b.ratio || 'adaptive';
  // input.value 只吃字符串。数字字段显式转，不依赖浏览器隐式转型。
  $('#pg-duration').value = b.duration == null ? '5' : String(b.duration);
  $('#pg-seed').value = b.seed == null ? '' : String(b.seed);
  $('#pg-negative').value = b.negative_prompt || '';
  $('#pg-model').value = b.model || 'wan-3.0-video';
  $('#pg-start').value = b.start_frame || '';
  $('#pg-end').value = b.end_frame || '';
  $('#pg-wait').checked = !!b.wait;
  $('#pg-audio').checked = !!b.audio;
  $('#pg-watermark').checked = !!b.watermark;
  $('#pg-thinking').checked = !!b.enable_thinking;
  $('#pg-extend').checked = !!b.prompt_extend;
  $('#pg-media-list').innerHTML = '';
  const T = { image_urls: 'image', video_urls: 'video', audio_urls: 'audio' };
  let n = 0;
  for (const [key, type] of Object.entries(T)) {
    for (const item of (b[key] || [])) {
      pgAddMedia(type, typeof item === 'string' ? item : item.url,
                 typeof item === 'string' ? '' : (item.name || ''));
      n++;
    }
  }
  if (!n) pgAddMedia();
  // 有高级参数就自动展开，省得用户猜
  const adv = b.negative_prompt || b.seed != null || b.start_frame ||
              b.end_frame || b.audio || b.watermark || b.enable_thinking ||
              b.prompt_extend || b.wait;
  const det = $('.pg-more');
  if (det) det.open = !!adv;
}

// 表单 -> JSON 框
function pgSyncUp() {
  $('#pg-json').value = JSON.stringify(pgCollect(), null, 2);
}

// JSON 框 -> 表单
function pgSyncDown() {
  let b;
  try { b = JSON.parse($('#pg-json').value); }
  catch (e) { return toast('JSON 解析失败：' + e.message, 'err'); }
  if (!b || typeof b !== 'object' || Array.isArray(b)) {
    return toast('JSON 得是个对象', 'err');
  }
  pgFillForm(b);
  pgView('form');
  toast('已同步到表单', 'ok');
}

// ---------- 素材卡片 ----------

// 一张卡片 = 类型下拉 + URL + 名字 + 删除。比一行三输入框好认，
// 类型用图标和颜色区分，不会看错行。
const PG_ICON = { image: '图', video: '视', audio: '音' };
function pgAddMedia(type = 'image', url = '', name = '') {
  const card = document.createElement('div');
  card.className = 'pg-card pg-card-' + type;
  card.innerHTML = `
    <div class="pg-c-head">
      <span class="pg-c-ico">${PG_ICON[type] || '图'}</span>
      <select class="pg-c-type">
        <option value="image"${type === 'image' ? ' selected' : ''}>图片</option>
        <option value="video"${type === 'video' ? ' selected' : ''}>视频</option>
        <option value="audio"${type === 'audio' ? ' selected' : ''}>音频</option>
      </select>
      <button class="pg-c-del" title="移除" onclick="this.closest('.pg-card').remove()">✕</button>
    </div>
    <input class="pg-c-url" placeholder="https://...  或点下面的「上传文件」" value="${esc(url)}" spellcheck="false">
    <input class="pg-c-name" placeholder="名字（可选，prompt 里 @名字 引用）" value="${esc(name)}">
    <div class="pg-c-up">
      <input type="file" class="pg-c-file" hidden onchange="pgUploadTo(this)">
      <button class="ghost mini pg-c-upbtn"
              onclick="this.parentNode.querySelector('.pg-c-file').click()">上传文件</button>
      <span class="pg-c-upmsg"></span>
    </div>`;
  $('#pg-media-list').appendChild(card);
  return card;
}

// 上传一个文件到 /v1/files，把返回的 url 填进本卡输入框。
//
// 为什么调用测试页要支持（2026-10-26）：
//   以前只能粘公网链接 —— 想在图生视频里用一张本地图片，
//   得先去别处传一遍拿链接。而 /v1/files 正是为了消除这一步。
//   调用测试页如果不能传文件，"测试"和"真实用法"就对不上。
async function pgUploadTo(fileInput) {
  const card = fileInput.closest('.pg-card');
  if (!card) return;
  const file = fileInput.files && fileInput.files[0];
  if (!file) return;

  const urlIn = card.querySelector('.pg-c-url');
  const msg = card.querySelector('.pg-c-upmsg');
  const btn = card.querySelector('.pg-c-upbtn');

  // 前端只做友好提示。真正的上限在服务端（配置项），
  // 改配置后这里不需要同步改。
  if (file.size > 20 * 1024 * 1024) {
    msg.textContent = `太大（${(file.size / 1048576).toFixed(1)} MB，上限 20 MB）`;
    msg.className = 'pg-c-upmsg err';
    return;
  }

  btn.disabled = true;
  msg.className = 'pg-c-upmsg';
  msg.textContent = `上传中… ${(file.size / 1024).toFixed(0)} KB`;

  try {
    const fd = new FormData();
    fd.append('file', file, file.name);
    const r = await fetch(withShare('/v1/files'), {
      method: 'POST',
      // ⚠️ 不要设 content-type：浏览器要自己加 multipart 的 boundary。
      // 手动设了反而会丢 boundary，服务端解析失败。
      headers: pgAuthOnly(),
      body: fd,
    });
    const txt = await r.text();
    let d = {};
    try { d = JSON.parse(txt); } catch { /* 非 JSON 响应 */ }
    if (!r.ok) {
      throw new Error(d.detail || d.error || txt.slice(0, 120) || ('HTTP ' + r.status));
    }
    if (!d.url) throw new Error('返回里没有 url');
    urlIn.value = d.url;
    msg.textContent = '已上传，链接已填入';
    msg.className = 'pg-c-upmsg ok';
  } catch (e) {
    msg.textContent = '失败：' + (e.message || e);
    msg.className = 'pg-c-upmsg err';
  } finally {
    btn.disabled = false;
    fileInput.value = '';      // 允许重选同一个文件（重试）
  }
}

// 只要 Authorization，不设 content-type（见上面 boundary 的说明）
function pgAuthOnly() {
  const h = {};
  const key = ($('#pg-key-on').checked ? $('#pg-key').value || '' : '').trim();
  if (key) h.authorization = 'Bearer ' + key;
  return h;
}

// ---------- 发送 ----------

// /v1/* 只认 Authorization header，不认 cookie，后台登录态帮不上忙。
function pgHeaders() {
  const h = { 'content-type': 'application/json' };
  const key = ($('#pg-key-on').checked ? $('#pg-key').value || '' : '').trim();
  if (key) h.authorization = 'Bearer ' + key;
  return h;
}

function pgToggleKey() {
  $('#pg-key').classList.toggle('hidden', !$('#pg-key-on').checked);
  if (!$('#pg-key-on').checked) $('#pg-key').value = '';
}

// 快捷：打 GET 类接口
function pgQuick(which) {
  const path = which === 'models' ? '/v1/models' : '/v1/credits';
  pgBusy(true, which === 'models' ? '正在获取模型列表…' : '正在获取号池状态…');
  api(path, { headers: pgHeaders() })
    .then((r) => { pgShow(r); pgBusy(false); })
    .catch((e) => { pgShow({ error: e.message }, false); pgBusy(false); });
}

function pgBusy(on, text) {
  PG.sending = on;
  const btn = $('#pg-send');
  if (btn) { btn.disabled = on; btn.textContent = on ? (text || '提交中…') : '生成视频'; }
  if (on) {
    $('#pg-idle').classList.add('hidden');
    $('#pg-run').classList.remove('hidden');
    $('#pg-fail').classList.add('hidden');
    $('#pg-video').classList.add('hidden');
    pgBar(-1);
    $('#pg-status').textContent = text || '提交中…';
    $('#pg-status').className = 'dim';
  }
}

async function pgSend() {
  if (PG.sending) return;
  pgStop();

  // JSON 视图下以文本框为准，否则用表单
  let body;
  if (!$('#pg-json-pane').classList.contains('hidden')) {
    try { body = JSON.parse($('#pg-json').value || '{}'); }
    catch (e) { toast('请求体不是合法 JSON：' + e.message, 'err'); return; }
  } else {
    body = pgCollect();
    pgSyncUp();
  }
  if (!body.prompt && !body.media && !body.image_urls && !body.video_urls &&
      !body.audio_urls && !body.start_frame) {
    toast('prompt 是必填的', 'err');
    return;
  }

  pgBusy(true, '提交中…');
  const t0 = Date.now();
  try {
    const r = await api('/v1/videos/generations', {
      method: 'POST', headers: pgHeaders(), body: JSON.stringify(body),
    });
    pgShow(r);
    pgHist(body, r);

    if (r && r.id && r.status === 'queued') {
      PG.tid = r.id;
      PG.startedAt = t0;
      $('#pg-cancel').classList.remove('hidden');
      $('#pg-status').textContent = '已入队，等号池分配账号';
      $('#pg-status').className = 'warn';
      pgBar(0);
      pgPoll(r.id);
      return;                        // 轮询结束后再复位按钮
    }
    // 同步模式：直接就是终态
    pgDone(r, t0);
  } catch (e) {
    // 失败的调用也要进历史 —— 401/503 恰恰是最该留着复盘的。
    // （以前只在 api() 成功后 pgHist，报错的那次就凭空消失了。）
    pgShow({ error: e.message }, false);
    pgHist(body, { status: 'error', error: e.message });
    pgFail(e.message, t0);
  }
}

// 轮询节奏：前几次密一点（刚提交，状态变得快），之后稳定 12 秒。
// 文档建议 10~15s，视频任务 5 分钟起步，更急没意义。
const PG_STEPS = [3000, 5000, 8000, 12000];
function pgPoll(tid) {
  PG._n = (PG._n || 0) + 1;
  const delay = PG_STEPS[Math.min(PG._n, PG_STEPS.length) - 1];
  PG.timer = setTimeout(async () => {
    if (PG.tid !== tid) return;
    let r;
    try {
      r = await api('/v1/videos/generations/' + encodeURIComponent(tid),
                    { headers: pgHeaders() });
    } catch (e) {
      pgFail('轮询出错：' + e.message, PG.startedAt);
      return;
    }
    pgShow(r);
    if (r.status === 'completed') return pgDone(r, PG.startedAt);
    if (r.status === 'failed') return pgFail(r.error || '未知原因', PG.startedAt);

    const el = Math.round((Date.now() - PG.startedAt) / 1000);
    const pct = r.progress != null ? ` ${r.progress}%` : '';
    $('#pg-status').textContent = `${PG_LABEL[r.status] || r.status}${pct} · 已等 ${fmtDur(el)}`;
    $('#pg-status').className = 'warn';
    pgBar(r.progress);
    pgPoll(tid);
  }, delay);
}
const PG_LABEL = { queued: '排队中', in_progress: '生成中', unknown: '状态未知' };

function pgDone(r, t0) {
  pgStop();
  const el = Math.round((Date.now() - t0) / 1000);
  $('#pg-status').textContent = `完成 · 总耗时 ${fmtDur(el)}`;
  $('#pg-status').className = 'ok';
  $('#pg-elapsed').textContent = fmtDur(el);
  pgBar(100);
  if (r.url) {
    $('#pg-player').src = r.url;
    $('#pg-dl').href = r.url;
    $('#pg-video').classList.remove('hidden');
  }
}

function pgFail(msg, t0) {
  pgStop();
  const el = t0 ? Math.round((Date.now() - t0) / 1000) : 0;
  $('#pg-status').textContent = '失败' + (el ? ` · ${fmtDur(el)}` : '');
  $('#pg-status').className = 'err';
  $('#pg-fail').textContent = msg;
  $('#pg-fail').classList.remove('hidden');
  pgBar(-1);
}

// 进度条。上游排队阶段没有百分比，用条纹动画表示「在等」。
function pgBar(pct) {
  const i = $('#pg-bar-i');
  const stage = $('#pg-preview');
  if (pct < 0) { i.style.width = '0%'; stage.classList.remove('pg-waiting'); return; }
  if (pct === 0 || pct == null) {
    i.style.width = '100%';
    stage.classList.add('pg-waiting');
    return;
  }
  stage.classList.remove('pg-waiting');
  i.style.width = Math.max(0, Math.min(100, pct)) + '%';
}

function pgStop() {
  clearTimeout(PG.timer);
  PG.timer = null;
  PG.tid = '';
  PG._n = 0;
  $('#pg-cancel').classList.add('hidden');
  const btn = $('#pg-send');
  if (btn) { btn.disabled = false; btn.textContent = '生成视频'; }
  PG.sending = false;
}

function pgCancel() {
  pgStop();
  $('#pg-status').textContent = '已停止轮询（任务仍在号池里跑）';
  $('#pg-status').className = 'dim';
  pgBar(-1);
}

function pgShow(obj, ok = true) {
  $('#pg-resp').textContent = typeof obj === 'string' ? obj : JSON.stringify(obj, null, 2);
}

// ---------- 历史记录 + Reuse ----------

function pgHist(body, resp) {
  PG.hist.unshift({
    t: Date.now(),
    body,
    ok: !!(resp && (resp.status === 'queued' || resp.url || resp.status === 'completed')),
    status: (resp && resp.status) || 'error',
    err: resp && resp.error,
    id: resp && resp.id,
  });
  PG.hist = PG.hist.slice(0, 8);
  pgRenderHist();
}

function pgRenderHist() {
  const box = $('#pg-hist');
  if (!PG.hist.length) {
    box.innerHTML = '<p class="dim" style="margin:0">还没有记录。发一次请求就会出现在这里。</p>';
    return;
  }
  box.innerHTML = PG.hist.map((h, i) => {
    const p = (h.body.prompt || '').slice(0, 42) || '（无 prompt）';
    const media = ['image_urls', 'video_urls', 'audio_urls']
      .reduce((n, k) => n + (h.body[k] || []).length, 0);
    const bits = [h.body.resolution, h.body.ratio, `${h.body.duration}s`];
    if (media) bits.push(`${media} 个素材`);
    if (h.body.start_frame) bits.push('首帧');
    const cls = h.status === 'error' || h.status === 'failed' ? 'bad'
              : h.status === 'completed' ? 'ok' : 'cooling';
    // 失败的把原因也带上 —— 点开就知道当时为什么被拒
    const meta = h.err ? `✕ ${h.err.slice(0, 40)}` : bits.join(' · ');
    return `<div class="pg-hitem" onclick="pgReuse(${i})" title="点击把这套参数灌回左栏">
      <span class="badge ${cls}">${esc(h.status)}</span>
      <span class="pg-htext">${esc(p)}</span>
      <span class="dim pg-hmeta">${esc(meta)}</span>
    </div>`;
  }).join('');
}

function pgReuse(i) {
  const h = PG.hist[i];
  if (!h) return;
  pgFillForm(h.body);
  pgView('form');
  toast('参数已灌回左栏', 'ok');
}

function pgClearHist() {
  PG.hist = [];
  pgRenderHist();
}

// ---------- 示例 / 重置 ----------

function pgFill(kind) {
  pgStop();
  if (kind === '图生视频') {
    pgFillForm({
      prompt: '镜头缓缓推进，人物微笑，发丝被风吹动',
      resolution: '720P', ratio: '16:9', duration: 5,
      image_urls: [{ url: 'https://example.com/a.png', name: '人物' }],
    });
  } else if (kind === '首尾帧') {
    pgFillForm({
      prompt: '花瓣从花苞到盛开的延时',
      resolution: '720P', ratio: '9:16', duration: 5,
      start_frame: 'https://example.com/start.png',
      end_frame: 'https://example.com/end.png',
    });
  } else if (kind === '多素材') {
    pgFillForm({
      prompt: '@主角 拿起 @杯子 喝了一口，露出满意的表情，风格参考 @海报',
      resolution: '720P', ratio: '1:1', duration: 5,
      image_urls: [
        { url: 'https://example.com/person.png', name: '主角' },
        { url: 'https://example.com/cup.png', name: '杯子' },
        { url: 'https://example.com/poster.png', name: '海报' },
      ],
    });
  }
  pgView('form');
}

function pgReset() {
  pgStop();
  pgFillForm({ prompt: '一只金毛在海边沙滩上奔跑，夕阳，电影感' });
  pgView('form');
  pgOut('preview');
  $('#pg-idle').classList.remove('hidden');
  $('#pg-run').classList.add('hidden');
  $('#pg-fail').classList.add('hidden');
  $('#pg-video').classList.add('hidden');
  $('#pg-resp').textContent = '还没有响应';
  pgBar(-1);
}

// 初始化工况
pgAddMedia();

pgAddMedia();

// ---------------------------------------------------------------- 添加账号

// ---------------------------------------------------------------- 添加账号向导
// 参照 sub2api：打开授权页 → 复制授权码 → 粘贴回表单兑换
// 这样每次都是独立授权流程，不会被浏览器已有的 Picsart 会话静默复用

let oauthState = '';
// 重新登录时的目标账号 id（0 = 新增账号）
let reloginId = 0;

function openCreate() {
  reloginId = 0;
  $('#f-name').value = '';
  $('#f-notes').value = '';
  $('#f-code').value = '';
  $('#create-err').textContent = '';
  $('#create-title').textContent = '添加 Picsart 账号';
  $('#btn-auth').textContent = '打开登录页';
  oauthState = '';
  goStep(1);
  $('#create-modal').classList.remove('hidden');
  setTimeout(() => $('#f-name').focus(), 50);
}

// 重新登录：打开同一个向导，但锁定为「覆盖指定账号」。
//
// 之前这里只 window.open 了授权页，主页面没有打开回填表单 ——
// 结果拿到 code 却没地方粘。现在复用向导，并直接进第 2 步。
function relogin(id, name) {
  reloginId = id;
  $('#f-name').value = name || '';
  $('#f-notes').value = '';
  $('#f-code').value = '';
  $('#create-err').textContent = '';
  $('#create-title').textContent = `重新登录：${name || '账号 ' + id}`;
  $('#btn-auth').textContent = '打开登录页';
  oauthState = '';
  goStep(2);
  $('#create-modal').classList.remove('hidden');
  toast(`请用「${name}」这个 Picsart 账号授权，登错账号会被拒绝`, '', 7000);
  // 自动帮他打开授权页，省一次点击
  startAuth();
}

function closeCreate() {
  reloginId = 0;
  $('#create-modal').classList.add('hidden');
}

function goStep(n) {
  $('#create-step-1').classList.toggle('hidden', n !== 1);
  $('#create-step-2').classList.toggle('hidden', n !== 2);
  $('#step-dot-1').classList.toggle('active', n >= 1);
  $('#step-dot-2').classList.toggle('active', n >= 2);
  $('#btn-back').classList.toggle('hidden', n === 1);
  $('#create-err').textContent = '';
}

function submitCreate() {
  // 第二步不再"提交"什么 —— 授权是自动完成的（回跳时后端就入库了）。
  // 点「完成」只是关闭弹窗。真正的新增/更新发生在 /oauth/callback。
  if (!$('#create-step-2').classList.contains('hidden')) {
    closeCreate();
    loadAll();
    return;
  }
  goStep(2);
}

// 单按钮：打开授权流程
//
// 不再用 window.open('about:blank') 占位——那会让新标签页长时间空白。
// 改为让标签页直接打开真实的 /oauth/begin 页面（本地响应极快），
// 该页自己负责清会话 + 跳转授权页。
//
// 2026-10-26：授权改成**自动回调**。用户登录完成后，上游跳回
// /oauth/callback，后端直接换 token 入库，再 postMessage 通知主页面。
// 用户不需要复制任何东西，标签页被关掉也不影响。
//
// ⚠️ 2026-10-26 第二次修：**不能用 window.open 开授权页**。
//
// 真实故障：客户登录完 Picsart，标签页**自己关了**，主页面 25 秒后弹
// "还没收到授权结果"。查下来不是通知机制的问题 —— 服务端结果表里
// 什么都没有，说明回调页从没被渲染过：
//   · 我们只在**成功**时自动关窗（`_callback_page` 里 `if ok: finish()`），
//     而成功分支在关窗前一定会先 upsert_account(status="ok") 写库。
//   · 库里那条账号始终是 dead / fails=3，说明成功页从没渲染过。
//   · 所以关窗的不是我们，是**上游授权页**在登录成功后直接 close()。
//
// 为什么它能关：`window.open()` 开出来的是「脚本打开的窗口」，
// 规范允许脚本关掉自己开的窗口（"Scripts may close only the windows
// that were opened by them"）。于是 code 还没回跳，标签页就没了。
//
// 这个坑从 manual 时代就存在，一直被误判成 slot / redirect_uri /
// postMessage 的问题，所以换了几版通知机制都没用。
//
// 修法：改成**真实 <a target="_blank"> 点击**打开。用户点击打开的标签页，
// 浏览器会忽略页面对 window.close() 的调用，标签页就能活到回跳那一步。
// rel=noopener 让上游彻底拿不到 opener —— 反正 COOP 已经把 opener 断了，
// 通知只走服务端轮询。
function startAuth() {
  const name = $('#f-name').value.trim();
  let url = '/oauth/begin?name=' + encodeURIComponent(name);
  if (reloginId) url += '&relogin=' + reloginId;

  // 用 <a> 而不是 window.open。必须在本函数（= 用户点击的事件处理里）
  // 同步触发，否则会被弹窗拦截。
  const a = document.createElement('a');
  a.href = url;
  a.target = '_blank';
  a.rel = 'noopener';
  a.style.display = 'none';
  document.body.appendChild(a);
  a.click();
  a.remove();

  $('#auth-status').textContent =
    '已打开登录页。登录完成后会自动完成授权，这个页面会自动刷新账号列表。';
  oauthState = '';

  // 重置状态并启动服务端轮询（这是唯一不受 COOP 影响的通知通道）
  authDone = false;
  startAuthPolling();

  // 兜底：25 秒还没结果就把手动入口露出来。
  //
  // 为什么是 25 秒：轮询每 2 秒一次，正常授权（登录 + 回跳 + 换 token）
  // 通常 5~15 秒。25 秒还没动静基本说明卡住了。
  // 注意这里**不停轮询** —— 用户手动粘贴后可能成功，但轮询也可能先到。
  setTimeout(() => {
    const box = $('#manual-exchange');
    if (box && box.classList.contains('hidden') && !authDone) {
      box.classList.remove('hidden');
      $('#auth-status').textContent =
        '还没收到授权结果。如果登录页已经跳转过了，可以把地址栏里的 URL 粘到下面手动完成。';
    }
  }, 25000);
}

// authDone 的声明在文件底部（OAuth 回跳提示那一节）。
// 它要在 startAuth 的 15 秒兜底里被读，所以用 `let` 声明在模块作用域
// 都能访问 —— 但**只能声明一次**，重复声明会 SyntaxError。

async function doExchange() {
  const code = $('#f-code').value.trim();
  if (!code) {
    $('#create-err').textContent = '请把登录后的完整地址粘进来';
    return;
  }
  const btn = $('#btn-next');
  btn.disabled = true;
  btn.textContent = '兑换中…';
  try {
    const r = await api('/api/accounts/oauth/exchange', {
      method: 'POST',
      body: JSON.stringify({ code, state: oauthState }),
    });
    toast(r.updated ? `账号 ${r.name} 已重新授权` : `账号 ${r.name} 已添加`, 'ok');
    closeCreate();
    loadAll();
  } catch (e) {
    $('#create-err').textContent = e.message;
  } finally {
    btn.disabled = false;
    btn.textContent = '完成';
  }
}

async function resetAcc(id, name) {
  try {
    await api(`/api/accounts/${id}/reset`, { method: 'POST' });
    toast(`${name} 已重置，可立即重试`, 'ok');
    loadAll();
  } catch (e) { toast(e.message, 'err'); }
}

async function checkAcc(id) {
  toast('检查中…');
  try {
    const r = await api(`/api/accounts/${id}/check`, { method: 'POST' });
    const info = r.info || {};

    // 现在用 options 端点查真实计价，不再是 pending-uses 探测
    const pricing = info.pricing || {};
    const free = Object.entries(pricing)
      .filter(([, v]) => v && v.credits === 0)
      .map(([k]) => k);
    const paid = Object.entries(pricing)
      .filter(([, v]) => v && typeof v.credits === 'number' && v.credits > 0)
      .map(([k, v]) => `${k}(${v.credits}积分)`);

    if (info.warning) {
      toast(info.warning, 'err', 9000);
    } else if (free.length) {
      const bits = [`免费档：${free.join(' / ')}`];
      if (paid.length) bits.push(`收费：${paid.join(', ')}`);
      toast(bits.join('　·　'), 'ok', 6000);
    } else {
      toast('已刷新账号信息', 'ok');
    }
    loadAccounts();
    loadStats();
  } catch (e) { toast(e.message, 'err'); }
}

async function delAcc(id, name) {
  if (!confirm(
    `删除 ${name}？\n\n` +
    `会先让该账号的 token 在 Picsart 端作废，再从号池移除。\n` +
    `之后需要重新授权才能再次使用。\n\n` +
    `（不会清除你浏览器里的 picsart.com 登录态）`
  )) return;
  try {
    const r = await api(`/api/accounts/${id}`, { method: 'DELETE' });
    toast(r.note || '已删除', 'ok', 5000);
    loadAll();
  } catch (e) { toast(e.message, 'err'); }
}

// ---------------------------------------------------------------- OAuth 回跳提示
//
// **三条道 + 服务端轮询**（2026-10-26，按业界标准做法改）
//
// 背景：上游授权页会发 `Cross-Origin-Opener-Policy: same-origin`，
// 浏览器因此把弹窗放进独立浏览上下文组，`window.opener` 被**永久置 null**，
// `postMessage` 静默失效 —— 不报错、不返回，主窗口永远等不到。
// 这是浏览器安全策略，应用无法覆盖（Microsoft 官方答复原话）。
//
// 所以真正可靠的是**服务端中转**：回调页把结果写进服务端，
// 主窗口轮询取。窗口关系完全不参与，COOP 对它没有影响。
//
// 现在四条道并行，任意一条到就算成功：
//   1. 服务端轮询（主力，唯一不受 COOP 影响的）
//   2. window.opener.postMessage（快，但常失效）
//   3. BroadcastChannel（同源广播）
//   4. localStorage storage 事件（最老的兼容）
//
// 用一个标记去重，处理逻辑只跑一次。

let authDone = false;
let _authPollTimer = null;

function onOauthResult(d) {
  if (!d || d.type !== 'oauth-result') return;
  if (authDone) return;          // 多条道都会发，只处理第一次
  authDone = true;

  // 停掉轮询和兜底计时器
  if (_authPollTimer) {
    clearInterval(_authPollTimer);
    _authPollTimer = null;
  }

  if (d.ok) {
    toast(d.message || '账号已添加', 'ok');
    // 自动模式下用户不用点「完成」，直接把弹窗收掉
    const modal = $('#create-modal');
    if (modal && !modal.classList.contains('hidden')) {
      closeCreate();
    }
    loadAll();
  } else {
    toast(d.message || '授权失败', 'err');
    // 失败时把手动入口露出来，让用户能补救
    const box = $('#manual-exchange');
    if (box) box.classList.remove('hidden');
    const st = $('#auth-status');
    if (st) st.textContent = '授权没有成功：' + (d.message || '未知原因');
  }
}

// 把轮询结果转成 onOauthResult 需要的形状
function _fromPoll(d) {
  if (!d || d.status === 'pending') return;
  onOauthResult({
    type: 'oauth-result',
    ok: d.status === 'ok',
    message: d.message || '',
  });
}

// 道 1（主力）：轮询服务端
//
// 每 2 秒一次，最多 5 分钟。为什么要停：用户可能再也不回来，
// 无限轮询会给服务器加无意义的负载。
function startAuthPolling() {
  if (_authPollTimer) clearInterval(_authPollTimer);
  const started = Date.now();
  const LIMIT = 5 * 60 * 1000;

  // ⚠️ 必须带上 sharecode（2026-10-26 修的**第二个**根因）。
  //
  // WorkBuddy 部署的分享链接模式下，网关会拦所有不带 sharecode 的请求：
  //     GET /api/accounts/oauth/result                -> 403
  //     GET /api/accounts/oauth/result?sharecode=xxx  -> 200
  //
  // 而第一版的 fetch 是 `fetch('/api/accounts/oauth/result')` ——
  // 每次轮询都被网关 403，代码里又是 `if (!r.ok) return;` 静默跳过，
  // 所以表现和"postMessage 失效"一模一样：主页面永远等不到结果。
  //
  // 这就是为什么换了几种通知机制都没用 —— 请求根本没到应用。
  // 用当前页面的 search 原样带过去，不管是不是分享模式都正确。
  const qs = location.search || '';

  _authPollTimer = setInterval(async () => {
    if (authDone || Date.now() - started > LIMIT) {
      clearInterval(_authPollTimer);
      _authPollTimer = null;
      return;
    }
    try {
      // 用 withShare 统一处理 sharecode，不再手动拼 qs
      const r = await fetch(withShare('/api/accounts/oauth/result'), {
        headers: { 'accept': 'application/json' },
        credentials: 'same-origin',
      });
      if (!r.ok) return;               // 401（登录过期）等：静默跳过
      _fromPoll(await r.json());
    } catch (e) {
      // 网络抖动：下一轮再试，不打断
    }
  }, 2000);
}

// 道 2：postMessage
window.addEventListener('message', (ev) => {
  onOauthResult(ev.data);
});

// 道 3：BroadcastChannel
try {
  const _bc = new BroadcastChannel('picsart-pool-oauth');
  _bc.addEventListener('message', (ev) => onOauthResult(ev.data));
} catch (e) {
  // 老浏览器没有 BroadcastChannel —— 还有另外几条道
}

// 道 4：localStorage 的 storage 事件
// 注意：storage 事件只在**其他**标签页触发，同页写不会触发 —— 正好。
window.addEventListener('storage', (ev) => {
  if (ev.key !== 'oauth-result' || !ev.newValue) return;
  try {
    onOauthResult(JSON.parse(ev.newValue));
  } catch (e) {
    /* 不是我们的数据，忽略 */
  }
});

(function handleOauthReturn() {
  const q = new URLSearchParams(location.search);
  const st = q.get('oauth');
  // 只清 oauth 那两个参数，保留 hash。
  // 以前这里直接 replaceState 到 '/'，hash 被一起抹掉 —— 刚授权完添加账号，
  //  tab 又被打回账号页，还得手动点回任务页。
  if (st === 'ok' || st === 'error') {
    if (st === 'ok') toast('账号已添加', 'ok');
    else toast('授权失败：' + (q.get('msg') || '未知'), 'err');
    q.delete('oauth');
    q.delete('msg');
    const qs = q.toString();
    history.replaceState({}, '', location.pathname + (qs ? '?' + qs : '') + location.hash);
  }
})();

boot();

// ---------------------------------------------------------------- 版本更新提示

// 注意：这是**纯提示**，不做自动更新。
//
// 代码在客户自己的项目目录里，自动替换等于我们获得远程执行任意代码的能力 ——
// 一旦私钥泄露所有客户机器失守。收益远小于风险，所以只提示、由客户自己
// 重新发布。
//
// 失败必须静默：GitHub 在部分网络下不可达，查不到就整条隐藏，
// 绝不因为版本检查失败而影响任何功能。
const UPDATE_HIDE_KEY = 'pool.update.hidden';

async function checkUpdate() {
  let d;
  try {
    const r = await fetch(withShare('/__version'), { cache: 'no-store' });
    if (!r.ok) return;
    d = await r.json();
  } catch {
    return;                       // 静默：查不到就当没有更新
  }
  if (!d || !d.has_update) return;

  // 用户点过「本次不再提示」且版本没变 -> 不再打扰
  let hidden = '';
  try { hidden = localStorage.getItem(UPDATE_HIDE_KEY) || ''; } catch { /* 隐私模式 */ }
  if (hidden === d.latest) return;

  const bar = document.getElementById('update-bar');
  if (!bar) return;
  const setTxt = (id, v) => {
    const el = document.getElementById(id);
    if (el) el.textContent = v;
  };
  setTxt('update-ver', d.latest);
  setTxt('update-cur', d.current);
  const link = document.getElementById('update-link');
  if (link && d.url) link.href = d.url;
  bar.dataset.latest = d.latest;
  bar.classList.remove('hidden');
}

function hideUpdate() {
  const bar = document.getElementById('update-bar');
  if (!bar) return;
  try { localStorage.setItem(UPDATE_HIDE_KEY, bar.dataset.latest || '1'); } catch { /* 忽略 */ }
  bar.classList.add('hidden');
}

// 复制一段能直接粘给 AI 的更新提示词。
//
// 为什么是提示词而不是下载链接：客户各自部署，更新要重新发布。
// 光给 zip 他还得自己替换文件再发布，而这一步他能让 WorkBuddy 的 AI 做 ——
// 所以直接把提示词准备好，复制即用。
async function copyUpdatePrompt() {
  const bar = document.getElementById('update-bar');
  const latest = (bar && bar.dataset.latest) || '';
  const cur = document.getElementById('update-cur')?.textContent || '';
  const url = document.getElementById('update-link')?.href || '';

  const prompt = [
    '请帮我把这个项目更新到最新版本并重新发布。',
    '',
    '## 当前情况',
    `- 当前版本：${cur}`,
    `- 目标版本：${latest}`,
    `- 发布页：${url}`,
    '',
    '## 请你做的事',
    '1. 从上面的发布页下载新版 zip 并解压',
    '2. 把解压出来的新代码，覆盖到我原来的项目目录',
    '   **重要：保留我原来的 data/ 目录** —— 里面是账号和激活凭证，删了要重新激活',
    '3. 重新发布，参数照旧：',
    '   language: python',
    '   startCmd: python workbuddy/entry.py',
    '   port: 3000',
    '   installCmd: pip install -r requirements.txt',
    '',
    '## 铁律',
    '启动命令不要改成 uvicorn app.main:app —— 会跳过配置注入，',
    '应用能打开但所有功能都是坏的。',
    '',
    '## 发布后请验证',
    'curl -s "https://我的域名/api/me"',
    '期望包含 logged_in 和 need_login 两个字段。',
  ].join('\n');

  try {
    await navigator.clipboard.writeText(prompt);
    toast('更新提示词已复制，粘给你的 AI 即可', 'ok', 6000);
  } catch {
    // 剪贴板被拒（非 https / 权限）：退回到弹窗让用户手抄
    window.prompt('复制下面这段给你的 AI：', prompt);
  }
}
