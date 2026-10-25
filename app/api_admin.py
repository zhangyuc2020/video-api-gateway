"""管理 API：账号管理 / OAuth 登录 / 号池状态 / 任务。
"""
from __future__ import annotations

import html as _html
import json
import logging
import secrets
import time
import urllib.parse
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel

from . import oauth, pool, store, upstream

log = logging.getLogger("picsart-pool")
from .config import REVOKE_LOGOUT, settings
from .security import (
    COOKIE_NAME,
    MAX_AGE,
    check_admin_password,
    login_limiter,
    make_session,
    require_admin,
)

router = APIRouter()

# OAuth 临时态（state -> verifier），进程内即可，量小、短命
_PENDING: dict[str, dict[str, Any]] = {}


# ---------------------------------------------------------------- 登录

class LoginBody(BaseModel):
    password: str


@router.post("/api/login")
async def login(body: LoginBody, request: Request, response: Response):
    lim = login_limiter()
    key = lim.client_key(request)
    # 先看是否在锁定期。锁定期内即使密码正确也拒绝 —— 否则限流可被
    # "猜中即通过"绕过，等于没有。
    remain = lim.check(key)
    if remain:
        raise HTTPException(
            429, f"登录失败次数过多，请 {remain} 秒后再试"
        )
    if not check_admin_password(body.password):
        locked = lim.fail(key)
        if locked:
            raise HTTPException(429, f"登录失败次数过多，请 {locked} 秒后再试")
        raise HTTPException(401, "密码错误")
    lim.reset(key)
    response.set_cookie(
        COOKIE_NAME,
        make_session(),
        max_age=MAX_AGE,
        httponly=True,
        samesite="lax",
        secure=settings.public_url.startswith("https://"),
    )
    return {"ok": True}


@router.post("/api/logout")
async def logout(response: Response):
    response.delete_cookie(COOKIE_NAME)
    return {"ok": True}


@router.get("/api/me")
async def me(request: Request):
    """会话状态。前端据此决定首屏是"直接进主界面"还是"先登录"。

    `need_login` 的含义（2026-10-25 新增）：
      · False —— 没设管理密码，已激活即放行（客户自己部署的默认形态）
      · True  —— 设了密码，必须登录（同一实例多人用的场景）

    前端不能用 `logged_in == False` 来判断"要登录"，因为①形态下
    本来就是"没 session 但也不需要登录"。之前前端正是这么判断的，
    结果已激活的客户每次都先看到一个他根本不该填的密码框。
    """
    from .security import current_user

    u = current_user(request)
    return {
        "logged_in": bool(u),
        "need_login": bool(settings.admin_password),
    }


# ---------------------------------------------------------------- OAuth 登录账号

@router.get("/api/accounts/oauth/start")
async def oauth_start(name: str = "", manual: bool = False,
                      _: dict = Depends(require_admin)):
    """生成官方授权 URL。

    manual=True 时走"粘贴授权码"模式（参照 sub2api）：
      回调页只把 code 显示出来，由用户复制回表单。
    这样每次都是全新授权流程，不会被浏览器已有的 Picsart 会话静默复用。
    """
    url, verifier = await oauth.build_authorize_url(manual=manual)
    state = secrets.token_urlsafe(16)
    _PENDING[state] = {
        "verifier": verifier,
        "name": name.strip(),
        "manual": manual,
        "created": time.time(),
    }
    for k in [k for k, v in _PENDING.items() if time.time() - v["created"] > 900]:
        _PENDING.pop(k, None)
    return {"url": url.replace("&redirect_uri=", f"&state={state}&redirect_uri="),
            "state": state, "manual": manual}


@router.post("/api/accounts/oauth/exchange")
async def oauth_exchange(body: ExchangeBody, _: dict = Depends(require_admin)):
    """用粘贴回来的授权码换 token（manual 模式）。

    code 可能是整串回调 URL，也可能只是 code 本身，都能处理。
    """
    raw = (body.code or "").strip()
    if not raw:
        raise HTTPException(400, "授权码为空")

    code = raw
    state = (body.state or "").strip()
    if "code=" in raw:
        q = urllib.parse.parse_qs(urllib.parse.urlparse(raw).query)
        code = (q.get("code") or [""])[0]
        if not state:
            state = (q.get("state") or [""])[0]
    if not code:
        raise HTTPException(400, "无法从输入中解析出 code")

    pending = _PENDING.pop(state, None) if state else None
    if not pending:
        # 容忍 state 丢失：用最近一次未使用的 manual 会话
        cand = [(k, v) for k, v in _PENDING.items()
                if v.get("manual") and time.time() - v["created"] < 900]
        if not cand:
            raise HTTPException(400, "授权会话已过期，请重新点击授权")
        state, pending = cand[-1]
        _PENDING.pop(state, None)

    try:
        bundle = await oauth.exchange_code(code, pending["verifier"], manual=True)
    except oauth.AuthError as e:
        raise HTTPException(400, f"换取 token 失败：{e}")

    return await _store_oauth_account(
        bundle, pending.get("name", ""), pending.get("relogin", 0) or 0
    )


class ExchangeBody(BaseModel):
    code: str = ""
    state: str = ""


@router.get("/oauth/manual-callback")
async def oauth_manual_callback(code: str = "", state: str = "", error: str = ""):
    """手动模式下展示授权码，让用户复制。

    ⚠️ 这个端点**故意不要求登录态**（2026-10-25 修）。

    原因：用户是在上游授权页完成授权后**被跨站回跳**到这里的。
    跨站回跳时浏览器不一定带 cookie —— 如果客户端设了
    `PICSART_POOL_ADMIN_PASSWORD`、或者激活门那一瞬间 `verify()` 没过，
    `require_admin` 就会抛 401，于是用户看到的是一段裸 JSON
    `{"detail":"未登录"}`，或者干脆一片空白 ——
    他描述成「不出现复制授权码的页面，直接就被关掉了」。

    放开之后的最坏情况：别人看到你的一次性授权码。但他拿去也没用 ——
    兑换需要同一次授权流程里的 code_verifier，存在服务端
    `_PENDING[state]` 里，而 state 是 32 字节随机值。
    而用户拿不到这个页面的后果是**完全无法添加账号**。
    """
    if error:
        return _code_page("", state, f"授权被拒绝：{error}")
    if not code:
        # 没有 code 也没有 error：可能是用户在授权页直接关掉了，
        # 或者回跳参数被中间层吃掉。给一个能看懂、能重试的页面，
        # 而不是裸 400。
        return _code_page(
            "", state,
            "没有收到授权码。可能是授权过程被中断了 —— "
            "请回到管理页重新点「添加账号」再试一次。",
        )
    return _code_page(code, state, "")


@router.get("/oauth/begin")
async def oauth_begin(name: str = "", relogin: int = 0,
                     _: dict = Depends(require_admin)):
    """授权入口页：一个真实 URL，标签页直接打开它，不会白屏。

    页面自身负责：
      1. 立刻渲染（本地响应，几十毫秒）
      2. iframe 静默清 Picsart 会话
      3. 约 900ms 后 location.replace 到 Picsart 授权页

    这样就绕开了「window.open('about:blank') 再等异步」导致的长时间空白。

    relogin=<账号id>：从账号列表点「重新登录」时带上。
    授权成功后按 Picsart 的 sub 匹配到原记录并覆盖令牌，
    不会新增重复账号（_find_by_sub 已经做了这个匹配）。
    """
    try:
        url, verifier = await oauth.build_authorize_url(manual=True)
    except oauth.AuthError as e:
        # 这一页是用户**新开标签页**打开的，出错时如果走全局 500，
        # 他会看到一个纯黑页面 + 一行 JSON（`{"error":""}`），完全无从下手。
        # 所以这里就地渲染一张说明页：说清是网络问题、让他重试。
        return HTMLResponse(_auth_fail_page(e), status_code=503)
    except Exception as e:      # 兜底：任何意外的空消息异常也不让它变成黑屏
        log.warning("oauth/begin 异常：%s: %s", type(e).__name__, e)
        return HTMLResponse(
            _auth_fail_page(
                oauth.AuthError(
                    "internal",
                    f"打开登录页失败（{type(e).__name__}）。请返回上一页重试；"
                    f"若持续失败请联系技术支持。",
                )
            ),
            status_code=500,
        )

    state = secrets.token_urlsafe(16)
    _PENDING[state] = {
        "verifier": verifier,
        "name": name.strip(),
        "manual": True,
        "relogin": int(relogin or 0),
        "created": time.time(),
    }
    for k in [k for k, v in _PENDING.items() if time.time() - v["created"] > 900]:
        _PENDING.pop(k, None)

    # 把 redirect_uri 记进 state，供回跳时比对（2026-10-25 加）。
    #
    # 为什么需要：用户反馈「授权完成后网页直接关闭」，服务端日志里什么都没有。
    # 最可能的解释是**回跳目标不可达** —— 而不可达的原因是 redirect_uri
    # 用的是请求级推导的 public_url，两次请求之间域名可能变了。
    #
    # 有了这个记录，回跳页就能自证：
    #   · 实际收到的 Host 和当初注册用的不一致 -> 明确告诉用户
    #   · 而不是白屏让他猜
    try:
        _cli = store.get_client(settings.public_url + oauth.MANUAL_REDIRECT_PATH)
        _PENDING[state]["redirect_uri"] = (_cli or {}).get("redirect_uri", "")
    except Exception:
        pass

    auth_url = url.replace("&redirect_uri=", f"&state={state}&redirect_uri=")
    html = f"""<!doctype html><html><head><meta charset="utf-8">
<title>正在打开登录页…</title>
<style>
  html,body{{margin:0;height:100%;background:#0d0d10;color:#8b8b99;
    font:14px/1.7 -apple-system,'Segoe UI','Microsoft YaHei',sans-serif}}
  .wrap{{height:100vh;display:flex;flex-direction:column;
    align-items:center;justify-content:center;gap:16px}}
  .spinner{{width:26px;height:26px;border:2px solid #2a2a33;
    border-top-color:#ff3ea5;border-radius:50%;
    animation:spin .7s linear infinite}}
  @keyframes spin{{to{{transform:rotate(360deg)}}}}
  .txt{{font-size:13px}}
  iframe{{position:absolute;width:1px;height:1px;border:0;opacity:0}}
</style></head>
<body>
<div class="wrap">
  <div class="spinner"></div>
  <div class="txt">正在打开登录页…</div>
</div>
<script>
  const AUTH_URL = {_js_json(auth_url)};
  // 会话清理是内部实现，用户无需感知
  try {{
    const f = document.createElement('iframe');
    f.src = '/oauth/logout-page?embed=1';
    document.body.appendChild(f);
  }} catch (e) {{}}
  setTimeout(() => {{ location.replace(AUTH_URL); }}, 900);
</script>
</body></html>"""
    return HTMLResponse(html)


@router.get("/oauth/logout")
async def oauth_logout_redirect(request: Request, _: dict = Depends(require_admin)):
    """登出 Picsart，落地到一个本地说明页。

    实测：/api/connect/logout 会 307 回到自身且**丢弃**
    post_logout_redirect_uri，直接打开会停在空白页。
    所以这里先跳出去清会话，用户看到的是本地这一页（下面 /oauth/logout-page）。
    """
    client = store.get_client()
    if not client:
        return RedirectResponse("/oauth/logout-page?e=noclient")

    # 端点来自配置（`endpoints.revoke_logout`），不写域名。
    if not REVOKE_LOGOUT:
        return RedirectResponse("/oauth/logout-page?e=noconfig")
    target = f"{REVOKE_LOGOUT}?client_id={client['client_id']}"
    # 前端用 iframe 静默清会话，同时给出说明页
    return RedirectResponse(target)


@router.get("/oauth/logout-page")
async def oauth_logout_page(request: Request, embed: int = 0,
                            _: dict = Depends(require_admin)):
    """登出页。

    embed=1 时只做静默清理（供前端 iframe 调用），不渲染界面。
    """
    client = store.get_client()
    cid = client["client_id"] if client else ""
    # 登出端点来自配置（`endpoints.revoke_logout`），不在这里写域名。
    logout_target = (
        f"{REVOKE_LOGOUT}?client_id={cid}"
        if cid and REVOKE_LOGOUT else ""
    )

    if embed:
        html = f"""<!doctype html><html><head><meta charset="utf-8"></head><body>
<script>
  const T = {_js_json(logout_target)};
  if (T) {{
    try {{
      const f = document.createElement('iframe');
      f.src = T; f.style.cssText='width:1px;height:1px;border:0;opacity:0';
      document.body.appendChild(f);
    }} catch (e) {{}}
    // 也尝试一次导航（同源时可清 cookie）
    try {{ fetch('/oauth/logout', {{redirect:'follow'}}); }} catch (e) {{}}
  }}
</script></body></html>"""
        return HTMLResponse(html)

    html = f"""<!doctype html><html><head><meta charset="utf-8">
<title>登出 Picsart</title>
<style>
  body{{margin:0;min-height:100vh;display:grid;place-items:center;
       background:#0d0d10;color:#e8e8ee;
       font:14px/1.7 -apple-system,'Segoe UI','Microsoft YaHei',sans-serif}}
  .box{{padding:32px;background:#16161b;border:1px solid #2a2a33;
       border-radius:14px;max-width:440px;width:100%;margin:20px}}
  h2{{margin:0 0 12px;font-size:17px}}
  p{{color:#8b8b99;font-size:13px;margin:0 0 14px}}
  .btn{{display:block;width:100%;text-align:center;text-decoration:none;
       background:#ff3ea5;color:#fff;border:0;border-radius:9px;
       padding:11px;font:inherit;font-weight:600;cursor:pointer;margin-bottom:10px}}
  .btn.ghost{{background:transparent;border:1px solid #2a2a33;color:#8b8b99}}
</style></head><body>
<div class="box">
  <h2>登出 Picsart</h2>
  <p>点击下面的按钮清掉 Picsart 的登录会话。完成后回到管理页，再打开登录页就会要求输账号密码。</p>
  <button class="btn" onclick="doLogout()">清掉 Picsart 会话</button>
  <a class="btn ghost" href="/">返回管理页</a>
  <p id="st" style="margin-top:14px;font-size:12px"></p>
</div>
<script>
  const TARGET = {_js_json(logout_target)};
  function doLogout() {{
    document.getElementById('st').textContent = '正在清理…';
    try {{
      const f = document.createElement('iframe');
      f.src = TARGET;
      document.body.appendChild(f);
    }} catch (e) {{}}
    setTimeout(() => {{
      document.getElementById('st').textContent =
        '已发送登出请求。如果没生效，请手动打开：' + TARGET;
    }}, 2500);
  }}
  if (TARGET) doLogout();
</script>
</body></html>"""
    return HTMLResponse(html)


@router.get("/oauth/logged-out")
async def oauth_logged_out():
    """兼容旧回跳地址。"""
    return RedirectResponse("/oauth/logout-page")


@router.get("/api/accounts/oauth/signout")
async def oauth_signout(_: dict = Depends(require_admin)):
    """返回登出入口，前端用来做「切换账号」。"""
    client = store.get_client()
    cid = client["client_id"] if client else ""
    back = f"{settings.public_url}/"
    # 拼接用的端点同样来自配置
    end_session = (
        f"{REVOKE_LOGOUT}?client_id={cid}"
        f"&post_logout_redirect_uri={urllib.parse.quote(back, safe='')}"
        if REVOKE_LOGOUT else ""
    )
    return {
        "logout_url": "/oauth/logout",
        "end_session": end_session,
        "hint": "点「切换账号」会登出上游会话并自动回到本页，再授权就会要求输密码。",
    }


@router.get("/oauth/callback")
async def oauth_callback(request: Request, code: str = "", state: str = "", error: str = "",
                        _: dict = Depends(require_admin)):
    """Picsart 授权后回跳。换 token、入库，然后给一个「可以关闭」的提示页。

    弹窗场景下不直接跳首页——那样用户会以为打开了新网站。
    改为渲染简洁结果页：成功后自动关窗，并把结果 postMessage 给主窗口。
    """
    pending = _PENDING.pop(state, None) if state else None

    if error:
        return _callback_page(False, f"授权被拒绝：{error}")
    if not code:
        return _callback_page(False, "回调缺少 code 参数")
    if not pending:
        return _callback_page(False, "授权状态已过期，请回到管理页重新发起")

    try:
        bundle = await oauth.exchange_code(code, pending["verifier"])
    except oauth.AuthError as e:
        return _callback_page(False, f"换取 token 失败：{e}")

    access = bundle["access_token"]
    device_id = oauth.new_device_id()
    ident = await oauth.fetch_identity(access, device_id)

    # 同一 Picsart 账号（sub 相同）重复授权时，更新原记录而不是新增
    existing = _find_by_sub(ident.get("sub", ""))
    base_name = (
        pending.get("name")
        or (existing["name"] if existing else "")
        or ident.get("username")
        or f"acct{int(time.time())}"
    )
    name = existing["name"] if existing else _unique_name(base_name)

    aid = store.upsert_account({
        "name": name,
        "kind": "oauth",
        "access_token": access,
        "refresh_token": bundle.get("refresh_token") or "",
        "client_id": bundle.get("client_id") or "",
        "device_id": device_id,
        "sub": ident.get("sub", ""),
        "username": ident.get("username", ""),
        "email": ident.get("email", ""),
        "tier": ident.get("tier", ""),
        "photo": ident.get("photo", ""),
        "expires_at": oauth.token_expiry(bundle),
        "status": "ok",
        "cooldown_until": 0,
        "fails": 0,
        "last_error": "",
    })

    token_kind = ident.get("token_kind", "")
    extra = ""
    if token_kind == "oauth":
        extra = "账号已授权，可直接出片（免费档 480P / 720P）。"
    try:
        await pool.pool().refresh_account_info(aid)
    except Exception:
        pass

    if existing:
        msg = f"账号 {name} 已更新（同一 Picsart 账号，未重复添加）"
    else:
        msg = f"账号 {name} 已添加"
    return _callback_page(True, msg, extra)


def _auth_fail_page(err: "oauth.AuthError") -> str:
    """授权链路失败时的说明页。

    为什么不用 JSON 而是 HTML：这一页是用户**新开标签页**打开的
    （点「添加账号」→ window.open('/oauth/begin')）。返回 JSON 他只会看到
    一屏黑底白字，既不知道怎么回去，也看不出是不是自己操作错了。

    页面必须回答三个问题：
      · 发生了什么（人话）
      · 是不是我的错（多数是网络，不是）
      · 我该怎么办（重试 / 找谁）
    """
    import html as _html

    detail = _html.escape(str(err.detail) if err.detail else str(err))
    # detail 可能是 dict（上游返回的 JSON），转成可读文本
    if err.detail and not isinstance(err.detail, str):
        try:
            import json as _j

            detail = _html.escape(_j.dumps(err.detail, ensure_ascii=False)[:600])
        except Exception:
            detail = _html.escape(str(err.detail)[:600])

    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>无法打开登录页</title>
<style>
 html,body{{margin:0;height:100%;background:#0d0d10;color:#e6e6ef;
   font:14px/1.75 -apple-system,'Segoe UI','Microsoft YaHei',sans-serif}}
 .wrap{{min-height:100vh;display:flex;align-items:center;justify-content:center;padding:24px}}
 .card{{width:min(560px,100%);background:#16161c;border:1px solid #26262f;
   border-radius:14px;padding:28px}}
 h1{{font-size:17px;margin:0 0 14px;color:#ff8a8a}}
 p{{margin:0 0 12px;color:#c9c9d4}}
 .why{{background:#0b0b0e;border:1px solid #26262f;border-radius:9px;
   padding:12px 14px;font-size:12.5px;color:#8b8b99;word-break:break-word;
   margin:0 0 14px}}
 ul{{margin:0 0 14px;padding-left:20px;color:#c9c9d4;font-size:13px}}
 li{{margin:5px 0}}
 .contact{{color:#8b8b99;font-size:12.5px;border-top:1px solid #26262f;
   padding-top:12px;margin:14px 0 0}}
 .contact b{{color:#ff3ea5;user-select:all}}
 button{{background:#ff3ea5;color:#fff;border:0;border-radius:9px;
   padding:10px 18px;font-size:13.5px;font-weight:600;cursor:pointer}}
</style></head><body>
<div class="wrap"><div class="card">
  <h1>没能打开 Picsart 登录页</h1>
  <p>这一步需要从服务器访问上游授权服务器。失败通常不是你的操作问题。</p>
  <div class="why">{detail}</div>
  <p style="margin:0 0 8px;color:#8b8b99;font-size:13px">常见原因：</p>
  <ul>
    <li><b>网络</b>：当前部署环境访问不了上游授权服务器（最常见）</li>
    <li><b>限流</b>：短时间内反复授权，被上游临时限制（等 1~5 分钟）</li>
  </ul>
  <p>可以先关掉这个标签页，回到主界面稍等一会儿再试。</p>
  <button onclick="window.close();history.length>1?history.back():null">关闭本页</button>
  <p class="contact">一直失败请联系：<b>Zzyuc88</b>（微信）</p>
</div></div>
</body></html>"""


def _js_json(obj: Any) -> str:
    """把对象序列化成可以安全嵌进 <script> 的 JSON 字面量。

    json.dumps 默认不转义 `<`，所以只要值里出现 `</script>` 就能提前闭合脚本块 ——
    后面紧跟的任意 HTML 就会被当标签解析（这正是本文件两条 XSS 的成因之一）。
    把 < > & 换成 \\u 转义后，字符串内容等价但无法闭合标签。
    """
    s = json.dumps(obj, ensure_ascii=False)
    return (
        s.replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
    )


def _callback_page(ok: bool, message: str, detail: str = "") -> HTMLResponse:
    color = "#31d0aa" if ok else "#ff5c5c"
    icon = "✓" if ok else "✕"
    payload = _js_json({"type": "oauth-result", "ok": ok, "message": message})
    # message / detail 可能来自 ?error= 或上游响应体，必须转义后再进 HTML
    msg_html = _html.escape(str(message))
    detail_html = (
        f'<p class="detail">{_html.escape(str(detail))}</p>' if detail else ""
    )
    html = f"""<!doctype html>
<html><head><meta charset="utf-8"><title>授权结果</title>
<style>
  body{{margin:0;min-height:100vh;display:grid;place-items:center;
       background:#0d0d10;color:#e8e8ee;
       font:15px/1.7 -apple-system,'Segoe UI','Microsoft YaHei',sans-serif}}
  .box{{max-width:440px;padding:34px;background:#16161b;
        border:1px solid #2a2a33;border-radius:14px;text-align:center}}
  .icon{{width:56px;height:56px;border-radius:50%;display:grid;place-items:center;
        margin:0 auto 16px;font-size:28px;font-weight:700;
        background:{color}22;color:{color}}}
  h2{{margin:0 0 10px;font-size:17px}}
  .msg{{color:#8b8b99;margin:0 0 8px}}
  .detail{{color:#ffb84d;font-size:13px;margin:12px 0 0;text-align:left;
          background:#000;padding:12px;border-radius:8px}}
  button{{margin-top:20px;background:{color};color:#0d0d10;border:0;
         border-radius:9px;padding:10px 22px;font:inherit;font-weight:600;cursor:pointer}}
</style></head>
<body>
  <div class="box">
    <div class="icon">{icon}</div>
    <h2>{'授权成功' if ok else '授权失败'}</h2>
    <p class="msg">{msg_html}</p>
    {detail_html}
    <button onclick="finish()">关闭窗口</button>
  </div>
<script>
  const PAYLOAD = {payload};
  function finish() {{
    try {{
      if (window.opener && !window.opener.closed) {{
        window.opener.postMessage(PAYLOAD, '*');
      }}
    }} catch (e) {{}}
    window.close();
    setTimeout(() => {{ document.querySelector('.msg').textContent =
      '可以手动关闭此标签页，并回到管理页刷新'; }}, 300);
  }}
  try {{
    if (window.opener && !window.opener.closed) {{
      window.opener.postMessage(PAYLOAD, '*');
    }}
  }} catch (e) {{}}
  // 成功时自动关闭，失败留给用户看原因
  if ({str(ok).lower()}) setTimeout(finish, 1200);
</script>
</body></html>"""
    return HTMLResponse(html)


def _code_page(code: str, state: str, error: str) -> HTMLResponse:
    """手动模式：把授权码显示出来供复制。

    ⚠️ code / state / error 全部来自 URL query，是攻击者可控输入。
    必须转义后再拼进 HTML —— 之前直接插进 <textarea>，
    `?code=</textarea><script>...</script>` 就能在同源下执行任意脚本
    （而这个页面正是管理员会打开的页面，等于后台被接管）。
    """
    safe_code = _html.escape(str(code))
    if error:
        body = f"""
        <h2 style="color:#ff5c5c">授权失败</h2>
        <p class="msg">{_html.escape(str(error))}</p>"""
    else:
        body = f"""
        <h2 style="color:#31d0aa">授权成功</h2>
        <p class="msg">把下面这段完整复制，粘回管理页的输入框：</p>
        <textarea readonly onclick="this.select()">{safe_code}</textarea>
        <button onclick="copyIt()">复制</button>
        <p class="hint2">可以直接点上面文本框（会自动全选）再 Ctrl+C</p>
        <script>
          function copyIt(){{
            const t=document.querySelector('textarea');
            t.select(); document.execCommand('copy');
            document.querySelector('button').textContent='已复制';
          }}
          setTimeout(()=>{{const t=document.querySelector('textarea'); if(t) t.select();}},300);
        </script>"""
    html = f"""<!doctype html><html><head><meta charset="utf-8">
<title>授权码</title></head>
<style>
  body{{margin:0;min-height:100vh;display:grid;place-items:center;
       background:#0d0d10;color:#e8e8ee;
       font:14px/1.7 -apple-system,'Segoe UI','Microsoft YaHei',sans-serif}}
  .box{{max-width:520px;width:100%;margin:20px;padding:28px;background:#16161b;
        border:1px solid #2a2a33;border-radius:14px}}
  h2{{margin:0 0 12px;font-size:16px}}
  .msg{{color:#8b8b99;margin:0 0 12px;font-size:13px}}
  textarea{{width:100%;height:110px;background:#000;color:#9fe8d5;
           border:1px solid #2a2a33;border-radius:8px;padding:11px;
           font:12px ui-monospace,Menlo,monospace;resize:vertical;
           word-break:break-all}}
  button{{margin-top:10px;background:#ff3ea5;color:#fff;border:0;
         border-radius:9px;padding:9px 20px;font:inherit;font-weight:600;cursor:pointer}}
  .hint2{{color:#8b8b99;font-size:12px;margin:10px 0 0}}
</style>
<body><div class="box">{body}</div></body></html>"""
    return HTMLResponse(html)


async def _store_oauth_account(
    bundle: dict[str, Any], want_name: str, relogin_id: int = 0
) -> dict[str, Any]:
    """把 OAuth token 落库（手动/自动回调共用）。

    relogin_id > 0 表示这是「重新登录」：授权成功后覆盖指定账号的令牌。
    会校验登录的 Picsart 账号和原记录是否同一个，防止授权错号。
    """
    access = bundle["access_token"]
    device_id = oauth.new_device_id()
    ident = await oauth.fetch_identity(access, device_id)

    # 优先按 Picsart 的 sub 匹配到已有记录（重新登录走这条），
    # 匹配不到再看 relogin 指定的 id —— 防止换了 Picsart 账号却覆盖错行。
    existing = _find_by_sub(ident.get("sub", ""))
    if not existing and relogin_id:
        row = store.get_account(relogin_id)
        if row:
            same = (row.get("sub") or "") == (ident.get("sub") or "")
            if same or not row.get("sub"):
                existing = row
            else:
                raise HTTPException(
                    400,
                    f"刚才登录的 Picsart 账号（{ident.get('username') or ident.get('sub')}）"
                    f"不是「{row.get('name')}」对应的那个账号。"
                    f"请确认后用正确的账号重新授权。",
                )

    base_name = (
        want_name
        or (existing["name"] if existing else "")
        or ident.get("username")
        or f"acct{int(time.time())}"
    )
    name = existing["name"] if existing else _unique_name(base_name)

    aid = store.upsert_account({
        "name": name,
        "kind": "oauth",
        "access_token": access,
        "refresh_token": bundle.get("refresh_token") or "",
        "client_id": bundle.get("client_id") or "",
        "device_id": device_id,
        "sub": ident.get("sub", ""),
        "username": ident.get("username", ""),
        "email": ident.get("email", ""),
        "tier": ident.get("tier", ""),
        "photo": ident.get("photo", ""),
        "expires_at": oauth.token_expiry(bundle),
        "status": "ok",
        "cooldown_until": 0,
        "fails": 0,
        "last_error": "",
    })
    info: dict[str, Any] = {}
    try:
        info = await pool.pool().refresh_account_info(aid)
    except Exception:
        pass
    return {
        "ok": True,
        "id": aid,
        "name": name,
        "updated": bool(existing),
        "credits": info.get("credits"),
        "tier_credits": info.get("tier_credits"),
        "pricing": info.get("pricing"),
        "warning": info.get("warning"),
    }


def _unique_name(base: str) -> str:
    base = base.strip() or "account"
    existing = {a["name"] for a in store.list_accounts()}
    if base not in existing:
        return base
    for i in range(2, 100):
        cand = f"{base}-{i}"
        if cand not in existing:
            return cand
    return f"{base}-{int(time.time())}"


def _find_by_sub(sub: str) -> dict[str, Any] | None:
    """按 Picsart 用户 id 查已有账号，用于重复授权时更新而不是新建。"""
    if not sub:
        return None
    for a in store.list_accounts():
        if a.get("sub") == sub:
            return a
    return None


# ---------------------------------------------------------------- 账号管理

class TokenBody(BaseModel):
    name: str = ""
    notes: str = ""
    access_token: str = ""
    refresh_token: str = ""
    device_id: str = ""
    package_id: str = ""


@router.post("/api/accounts/token")
async def add_by_token(body: TokenBody, _: dict = Depends(require_admin)):
    """导入现成 access_token（从浏览器 Network 里复制 Bearer）。"""
    tok = (body.access_token or "").strip()
    if tok.startswith("Bearer "):
        tok = tok[7:].strip()
    if not tok:
        raise HTTPException(400, "access_token 为空")

    device_id = body.device_id.strip() or oauth.new_device_id()
    try:
        ident = await oauth.fetch_identity(tok, device_id, strict=True)
    except oauth.AuthError as e:
        raise HTTPException(400, f"token 校验失败：{e}")
    name = _unique_name(body.name.strip() or ident.get("username") or "token")

    aid = store.upsert_account({
        "name": name,
        "kind": "oauth" if body.refresh_token else "cookie",
        "access_token": tok,
        "refresh_token": body.refresh_token.strip(),
        # 带 refresh_token 才有"用哪个 client 刷新"的问题。
        # 从 access_token 的 claim 解出来 —— 那是签发方写的，天然正确；
        # 留空的话 refresh 时会回退去"全库最新一条 client"，必然对不上。
        "client_id": oauth.client_id_from_token(tok) if body.refresh_token else "",
        "device_id": device_id,
        "sub": ident.get("sub", ""),
        "username": ident.get("username", ""),
        "email": ident.get("email", ""),
        "tier": ident.get("tier", ""),
        "photo": ident.get("photo", ""),
        "package_id": body.package_id.strip(),
        "expires_at": oauth.token_expiry({"access_token": tok}),
        "status": "ok",
        "cooldown_until": 0,
        "fails": 0,
        "last_error": "",
    })
    try:
        await pool.pool().refresh_account_info(aid)
    except Exception:
        pass
    return {"ok": True, "id": aid, "name": name}


class CookieBody(BaseModel):
    name: str = ""
    notes: str = ""
    cookie: str = ""


@router.post("/api/accounts/cookie")
async def add_by_cookie(body: CookieBody, _: dict = Depends(require_admin)):
    """用浏览器 Cookie 登录。

    两条路：
      1) cookie 里带 access_token -> 直接当 token 用
      2) 只有会话 cookie -> 带 cookie 调 me.json 验证，能过就存起来
         （后续请求直接用 cookie 头，靠会话自身续命）
    """
    raw = (body.cookie or "").strip()
    if not raw:
        raise HTTPException(400, "cookie 为空")

    bearer = oauth.cookie_to_bearer(raw)
    if bearer:
        # 探测和入库必须用同一个 device_id，否则 Picsart 侧看到的是两个设备
        device_id = oauth.new_device_id()
        try:
            ident = await oauth.fetch_identity(bearer, device_id, strict=True)
        except oauth.AuthError as e:
            raise HTTPException(400, f"Cookie 中的 token 无效：{e}")
        name = _unique_name(body.name.strip() or ident.get("username") or "cookie")
        aid = store.upsert_account({
            "name": name, "kind": "oauth", "access_token": bearer,
            "device_id": device_id, "sub": ident.get("sub", ""),
            "username": ident.get("username", ""), "email": ident.get("email", ""),
            "expires_at": oauth.token_expiry({"access_token": bearer}),
            "status": "ok", "cooldown_until": 0, "fails": 0, "last_error": "",
        })
        try:
            await pool.pool().refresh_account_info(aid)
        except Exception:
            pass
        return {"ok": True, "id": aid, "name": name, "mode": "cookie→bearer"}

    try:
        ident = await oauth.login_from_cookie(raw)
    except oauth.AuthError as e:
        raise HTTPException(400, str(e))

    name = _unique_name(body.name.strip() or ident.get("username") or "cookie")
    aid = store.upsert_account({
        "name": name,
        "kind": "cookie",
        "access_token": "",
        "refresh_token": "",
        "cookie": raw,
        "device_id": ident["device_id"],
        "sub": ident.get("sub", ""),
        "username": ident.get("username", ""),
        "email": ident.get("email", ""),
        "tier": ident.get("tier", ""),
        "photo": ident.get("photo", ""),
        "expires_at": time.time() + 86400 * 30,   # cookie 会话未知，给个宽期限
        "status": "ok",
        "cooldown_until": 0,
        "fails": 0,
        "last_error": "",
    })
    return {"ok": True, "id": aid, "name": name, "mode": "cookie"}


@router.post("/api/accounts/{aid}/reset")
async def reset_account(aid: int, _: dict = Depends(require_admin)):
    """重置账号状态：清掉冷却、失败计数、feature 屏蔽记录。

    用于「额度恢复后想立刻重试」的场景——402 留下的限制不该永久生效。
    """
    acct = store.get_account(aid)
    if not acct:
        raise HTTPException(404, "账号不存在")
    store.update_account(
        aid,
        status="ok" if (acct.get("access_token") or acct.get("cookie")) else "dead",
        cooldown_until=0,
        fails=0,
        last_error="",
    )
    store.clear_features(aid)
    return {"ok": True}


@router.get("/api/accounts")
async def list_accounts(_: dict = Depends(require_admin)):
    now = time.time()
    out = []
    for a in store.list_accounts():
        ttl = int(a["expires_at"] - now) if a["expires_at"] else 0
        has_refresh = bool(a["refresh_token"])
        has_cookie = bool(a["cookie"])

        # 推导「能不能用」——不能只看 status 字段。
        # status='ok' 但 access_token 已过期、refresh 又失败，实际是不可用的。
        # 前端不该自己拼这个逻辑，否则会出现「状态：正常 / 令牌：待续期」这种矛盾显示。
        err = (a.get("last_error") or "")
        # refresh 失败到一定程度说明 token 已经废了，自愈不了
        refresh_broken = (
            a["status"] == "dead"
            or "refresh_failed" in err
            or "invalid_grant" in err
            or a["fails"] >= 3
        )
        if not (has_refresh or has_cookie):
            health = "relogin"        # 没有任何续期手段，只能重新授权
        elif refresh_broken:
            health = "relogin"        # 续期链路已断，自动恢复不了
        elif ttl > 0:
            health = "ready"          # token 有效，随时可用
        elif has_refresh:
            health = "renewable"      # 过期了但能自动续
        else:
            health = "relogin"

        out.append({
            "id": a["id"],
            "name": a["name"],
            "notes": a.get("notes", ""),
            "kind": a["kind"],
            "username": a["username"],
            "email": a["email"],
            "tier": a.get("tier", ""),
            "photo": a.get("photo", ""),
            "sub": a["sub"],
            "status": a["status"],
            "health": health,
            "credits": a["credits"],
            "tier_credits": a["tier_credits"],
            "ttl": ttl,
            "has_refresh": has_refresh,
            "has_cookie": has_cookie,
            "cooldown": int(max(0, a["cooldown_until"] - now)),
            "fails": a["fails"],
            "last_error": a["last_error"],
        })
    return {"accounts": out}


@router.post("/api/accounts/{aid}/check")
async def check_account(aid: int, _: dict = Depends(require_admin)):
    try:
        info = await pool.pool().refresh_account_info(aid)
        return {"ok": True, "info": info}
    except Exception as e:
        raise HTTPException(400, str(e))


@router.post("/api/accounts/{aid}/refresh")
async def refresh_account(aid: int, _: dict = Depends(require_admin)):
    acct = store.get_account(aid)
    if not acct:
        raise HTTPException(404, "账号不存在")
    try:
        await pool.pool().ensure_fresh({**acct, "expires_at": 0})
        return {"ok": True}
    except Exception as e:
        raise HTTPException(400, str(e))


@router.delete("/api/accounts/{aid}")
async def delete_account(
    aid: int, keep_token: bool = False, _: dict = Depends(require_admin)
):
    """删除账号。

    默认行为 = 先作废 Picsart 端 token，再移除记录（等价于"登出并删除"）。
    理由：只删本地记录而把有效 token 留在外面，是个说不清的隐患。

    keep_token=true 可跳过 revoke，仅用于迁移场景
    （比如要把 token 搬到另一套部署里继续用）。
    """
    acct = store.get_account(aid)
    if not acct:
        raise HTTPException(404, "账号不存在")

    revoked: list[str] = []
    failed: list[str] = []
    if not keep_token:
        # 必须带上该账号自己的 client_id —— 两种授权模式各有一个 DCR client，
        # 拿错了 revoke 会失败，而失败却不报 = 凭证还在外面活着。
        cid = acct.get("client_id") or ""
        if acct.get("refresh_token"):
            if await oauth.revoke(acct["refresh_token"], "refresh_token", cid):
                revoked.append("refresh_token")
            else:
                failed.append("refresh_token")
        if acct.get("access_token"):
            if await oauth.revoke(acct["access_token"], "access_token", cid):
                revoked.append("access_token")
            else:
                failed.append("access_token")

    store.delete_account(aid)
    if keep_token:
        note = "已移除记录（保留 token）"
    elif revoked and not failed:
        note = "已作废 token 并移除记录"
    elif revoked and failed:
        note = f"记录已移除，但部分 token 未能作废（{','.join(failed)}），上游可能仍有效"
    elif failed:
        note = (
            f"记录已移除，但 token **未能作废**（{','.join(failed)}）。"
            "原因通常是该账号没有记录 client_id —— 这些凭证在上游仍然有效，请手动处理。"
        )
    else:
        note = "已移除记录"
    return {"ok": True, "revoked": revoked, "failed": failed, "note": note}


@router.post("/api/accounts/{aid}/revoke")
async def revoke_account(aid: int, _: dict = Depends(require_admin)):
    """登出：让 Picsart 服务端作废这个账号的 refresh_token。

    这才是"登出"——会真正使 token 失效。
    之后该账号需要重新授权才能继续使用。
    """
    acct = store.get_account(aid)
    if not acct:
        raise HTTPException(404, "账号不存在")

    revoked, failed = [], []
    cid = acct.get("client_id") or ""
    if acct.get("refresh_token"):
        (revoked if await oauth.revoke(acct["refresh_token"], "refresh_token", cid)
         else failed).append("refresh_token")
    if acct.get("access_token"):
        (revoked if await oauth.revoke(acct["access_token"], "access_token", cid)
         else failed).append("access_token")

    store.update_account(
        aid,
        access_token="",
        refresh_token="",
        cookie="",
        expires_at=0,
        status="dead",
        last_error=(
            "已登出（token 已作废）" if not failed
            else f"本地已登出，但上游作废失败：{','.join(failed)}"
        ),
    )
    note = (
        "Picsart 端的 token 已作废，该账号需重新授权" if not failed
        else (
            f"本地已清空登录态，但 **上游 token 未能作废**（{','.join(failed)}）。"
            "该凭证在 Picsart 侧可能仍然有效。"
        )
    )
    return {"ok": True, "revoked": revoked, "failed": failed, "note": note}


# ---------------------------------------------------------------- 概览

@router.get("/api/stats")
async def stats(_: dict = Depends(require_admin)):
    return store.stats()


@router.get("/api/tasks")
async def tasks(limit: int = 50, _: dict = Depends(require_admin)):
    """任务列表 + 队列信息。

    队列位置是**实时算的**：看它前面还有几个 queued 任务。
    写死在表里会立刻过期（前面那个跑完了，后面的位置就该往前挪）。
    """
    rows = store.list_tasks(limit)

    # 正在排队的一批，按提交时间排序 -> 位置
    queued = [t for t in rows if t["status"] == "queued"]
    queued.sort(key=lambda t: t["created_at"])
    pos = {t["id"]: i + 1 for i, t in enumerate(queued)}

    # 池子里同时能干几个（= 有凭证的账号数），用来判断"还要等几轮"
    try:
        accts = [a for a in store.list_accounts()
                 if a.get("access_token") or a.get("cookie")]
        capacity = max(1, len(accts))
    except Exception:
        capacity = 1

    now = time.time()
    out = []
    for t in rows:
        d = dict(t)
        d.pop("params", None)          # 完整参数单独给，列表里只给摘要
        ca = t.get("created_at") or now
        started = t.get("started_at") or 0
        d["age"] = int(now - ca)                       # 提交至今
        # 排队等了多久 = 开始执行时刻 - 提交时刻。负数说明数据异常，钳到 0。
        d["waited"] = max(0, int(started - ca)) if started else None
        d["queue_pos"] = pos.get(t["id"], 0)
        if d["queue_pos"]:
            d["rounds"] = max(1, (d["queue_pos"] + capacity - 1) // capacity)
        else:
            d["rounds"] = 0
        d["summary"] = _task_summary(t)
        # 上游原始状态，原样透出 —— 前端用它区分「等调度」和「正在跑」
        d["upstream_status"] = t.get("upstream_status") or ""
        d["upstream_waiting"] = bool(t.get("upstream_waiting"))

        # ---- 时间线：把总耗时拆成三段 ----
        # created  用户发起
        # started  拿到账号（排队结束）
        # submitted 上游受理
        # upstream 上游真正开始出片
        # finished 完成
        tl = {
            "created": t.get("created_at") or 0,
            "started": started,
            "submitted": t.get("submitted_at") or 0,
            "upstream": t.get("upstream_at") or 0,
            "finished": t.get("finished_at") or 0,
        }
        d["timeline"] = tl
        # 三段耗时（秒）。缺一段就给 None，前端显示 "—"
        def seg(a: str, b: str) -> float | None:
            if tl[a] and tl[b] and tl[b] >= tl[a]:
                return round(tl[b] - tl[a], 1)
            return None
        d["wait_s"] = seg("created", "started")        # 排队
        d["schedule_s"] = seg("started", "upstream")   # 上游调度
        d["gen_s"] = seg("upstream", "finished")       # 真实生成
        out.append(d)
    return {"tasks": out, "capacity": capacity, "queued": len(queued)}


def _task_summary(t: dict[str, Any]) -> str:
    """一句话摘要：让列表一眼看出这个任务要干什么。"""
    bits = []
    if t.get("resolution"):
        bits.append(str(t["resolution"]))
    p = str(t.get("prompt") or "").strip()
    if p:
        bits.append(p[:36] + ("…" if len(p) > 36 else ""))
    return " · ".join(bits) or "—"


@router.get("/api/tasks/{tid}")
async def task_detail(tid: str, _: dict = Depends(require_admin)):
    """单个任务详情，含完整提交参数。前端点开看。"""
    t = store.get_task(tid)
    if not t:
        raise HTTPException(404, "任务不存在")
    d = dict(t)
    try:
        d["params_obj"] = json.loads(t.get("params") or "{}")
    except Exception:
        d["params_obj"] = {}
    return d


@router.post("/api/tasks/{tid}/cancel")
async def cancel_task(tid: str, _: dict = Depends(require_admin)):
    """取消一个**尚未提交到上游**的任务。

    为什么只允许这一种（2026-10-25 定）：

      · upstream_id 为空 -> 上游根本不知道有这个任务，取消是干净的：
        本地 asyncio.Task.cancel()，队列位置释放，不占任何资源。

      · upstream_id 非空 -> **不能取消**。上游没有公开的取消接口，
        我们能做的只有"本地不理它"，但那个任务会继续在 Picsart 那边
        跑着、继续占着该账号的并发名额（每号并发=1）。
        于是账号被一个"本地看不见的任务"锁住，队列后面的任务干等 ——
        这正是 pool.py 顶部注释里记的那个坑。
        所以宁可明确拒绝并说清原因，也不做"假取消"。

    取消后的状态是 `cancelled`，**不是** `failed`。
    「我点了取消」和「任务失败了」是两回事，混在一起统计会失真。
    """
    from . import api_proxy

    t = store.get_task(tid)
    if not t:
        raise HTTPException(404, "任务不存在")

    up_id = (t.get("upstream_id") or "").strip()
    if up_id:
        raise HTTPException(
            409,
            f"任务已提交到上游（{up_id}），无法取消。"
            f"上游会继续执行，该账号的名额在它结束前不会释放。"
            f"你可以在这里等它完成，结果会自动回来。",
        )

    if t["status"] not in ("queued", "in_progress"):
        raise HTTPException(409, f"任务当前状态是 {t['status']}，不需要取消")

    # 先落库再 cancel：cancel() 会让 _run_task 的 finally 立刻执行，
    # 那时它会读库判断状态。顺序反了就会看到「cancelled 被覆盖成 failed」。
    store.update_task(
        tid,
        status="cancelled",
        error="已由用户取消",
        finished_at=time.time(),
        queue_pos=0,
    )

    job = api_proxy._TASKS.get(tid)
    if job is not None and not job.done():
        # 不 await —— cancel() 只发出请求，真正收尾在事件循环里。
        # await 的话，若 _run_task 卡在不可中断的 await 上会拖住这个 HTTP 请求。
        job.cancel()

    log.info("task %s 已被用户取消（未提交到上游）", tid)
    return {"ok": True, "id": tid, "status": "cancelled"}


@router.post("/api/tasks/{tid}/retry")
async def retry_task(tid: str, _: dict = Depends(require_admin)):
    """把一条失败任务重新跑一遍，**参数不用重填**（一直在 tasks.params 里）。

    移植自 Go 版 apiadmin.go:handleRetryTask，判据一字不改：

      · upstream_id 为空 -> 上游从不知道有这个任务，重放安全
      · upstream_id 非空 -> **绝不重放**。上游可能已在跑或已出片，
        重提会占第二次名额、可能重复扣费、出两份片。那种要走"续跟"。

    这个区分很重要：客户报"任务失败了帮我重试"，如果他点的是
    一条其实上游还在跑的任务，无脑重提会让同一个 prompt 生成两遍。
    """
    from . import api_proxy

    t = store.get_task(tid)
    if not t:
        raise HTTPException(404, "任务不存在")

    up_id = (t.get("upstream_id") or "").strip()
    if up_id:
        raise HTTPException(
            409,
            f"该任务已提交到上游（{up_id}），不能重新提交 —— "
            f"那样会重复占名额甚至重复出片。"
            f"请在任务详情里看它的当前状态，或等系统自动续跟。",
        )

    if t["status"] in ("queued", "in_progress"):
        raise HTTPException(409, f"任务当前状态是 {t['status']}，正在处理中，不需要重试")
    if t["status"] == "completed":
        raise HTTPException(409, "任务已完成，无需重试")

    params = api_proxy._params_of(t)
    if not params:
        raise HTTPException(
            400, "这条任务的参数快照已丢失，无法重试。请重新提交一个新的任务。"
        )

    # 重置成"刚入队"的样子。
    # 计数清零是刻意的：这是一次**人工发起的新尝试**，不该继承上次的
    # 重排/续跟计数，否则一个曾经重排过 5 次的任务一重试就又被判满。
    store.update_task(
        tid,
        status="queued",
        error="",
        account_id=0,
        account_name="",
        upstream_id="",
        upstream_status="",
        url="",
        percent=0,
        elapsed=0,
        finished_at=0,
        requeued=0,
        revived=0,
    )

    # 生成要几分钟，不能卡住这个 HTTP 响应。
    # 与 _spawn_async 用同一套引用管理（_TASKS 强引用，防止 asyncio 的
    # 弱引用让 task 跑到一半被 GC）。
    import asyncio

    if tid not in api_proxy._TASKS:
        job = asyncio.create_task(api_proxy._run_task(tid, params))
        api_proxy._TASKS[tid] = job
        job.add_done_callback(lambda _t: api_proxy._TASKS.pop(tid, None))

    return {"ok": True, "id": tid, "status": "queued"}


@router.get("/api/docs")
async def api_docs(download: int = 0, _: dict = Depends(require_admin)):
    """导出接口文档（Markdown）。

    两个用途，靠 `download` 区分（对齐 Go 版 docs.go）：
      /api/docs              -> 直接返回正文（前端"复制全文"用）
      /api/docs?download=1   -> 加 Content-Disposition（"下载 .md"用）

    为什么动态生成：文档里的 Base URL、免费档、默认值都来自运行期配置。
    写成静态文件必然和实际部署漂移 —— 客户照文档写的代码跑不通。
    """
    from .docs import build_api_docs

    body = build_api_docs(settings.public_url)
    headers = {"cache-control": "no-store"}
    if download:
        # 文件名带日期，下多份能区分新旧
        import time as _t

        day = _t.strftime("%Y%m%d", _t.localtime())
        headers["content-disposition"] = (
            f'attachment; filename="wan3-api-{day}.md"'
        )
    return Response(
        content=body,
        media_type="text/markdown; charset=utf-8",
        headers=headers,
    )


@router.get("/api/health")
async def health(_: dict = Depends(require_admin)):
    s = settings
    from workbuddy.version import current as _ver

    return {
        "version": _ver(),
        "default_resolution": s.default_resolution,
        "default_ratio": s.default_ratio,
        "default_duration": s.default_duration,
        "free_fallback": s.free_fallback,
        "client": store.get_client(),
        "public_url": s.public_url,
        "redirect_uri": s.redirect_uri,
        "api_keys_enabled": bool(s.api_keys),
        "admin_password_set": bool(s.admin_password),
        # 号池"主动宣告"的实时状态：哪些号自称可以接活、多久前宣告的
        "pool": pool.pool().ready_snapshot(),
        "probe_interval": pool.PROBE_INTERVAL,
        "submit_wait_max": pool.SUBMIT_WAIT_MAX,
    }


@router.get("/api/pool/ready")
async def pool_ready(_: dict = Depends(require_admin)):
    """号池就绪快照 —— 排查"为什么任务派不出去"时先看这个。

    ready 里是**号自己探测后宣告可用**的；为空说明所有号的上游名额都被占着，
    或凭证/状态有问题。配合 /api/tasks 的 upstream_status 一起看。
    """
    p = pool.pool()
    snap = p.ready_snapshot()
    accounts = {a["id"]: a for a in store.list_accounts()}
    snap["detail"] = [
        {
            "id": aid,
            "name": accounts.get(aid, {}).get("name", ""),
            "ready_seconds_ago": snap["ready_age"].get(aid),
        }
        for aid in snap["ready"]
    ]
    snap["all_accounts"] = [
        {
            "id": a["id"],
            "name": a["name"],
            "status": a["status"],
            "ready": a["id"] in snap["ready"],
            "last_error": a.get("last_error") or "",
        }
        for a in store.list_accounts()
    ]
    return snap
