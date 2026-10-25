"""Picsart 官方 OAuth 登录 + Cookie/Token 导入。

实测要点（2026-10）：
- auth.picsart.com 是标准 Keycloak 风格 OIDC，authorization_code + PKCE(S256)
- /oauth2/register 开放 DCR，但 grant_types 只收 authorization_code + refresh_token
  （塞 device_code 会 400 Unsupported grant_type）
- authorize 无会话时 302 到 accounts.picsart.com 登录页；登录成功后回跳到
  redirect_uri?code=...
- token_endpoint_auth_method = none，无 client_secret
- access_token 是 JWT，TTL=1800s；refresh_token 可反复续期（长效关键）
"""
from __future__ import annotations

import base64
import hashlib
import os
import secrets
import time
import urllib.parse
from typing import Any

import httpx

from . import authlimit, store
from .config import (
    AUTHORIZE,
    DEFAULT_SCOPE,
    ME,
    REGISTER,
    REVOKE,
    TOKEN,
    settings,
)

# 真实 Chrome 版本号。之前用 Chrome/155 是编造的版本，
# Cloudflare 的 Browser Integrity Check 会把它当可疑特征。
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36"
)


class AuthError(RuntimeError):
    def __init__(self, error: str, detail: Any = None):
        self.error = error
        self.detail = detail
        super().__init__(f"{error}: {detail}" if detail else error)


def _json_or_text(r: httpx.Response) -> Any:
    """auth.picsart.com 前面有 Cloudflare。

    限流/被拦时返回的是 HTML 页面而不是 JSON，直接 r.json() 会抛
    "Expecting value: line 1 column 1" —— 那个异常会把真正的原因（429 限流）
    完全掩盖成一句看不懂的解析错误。所以统一走这里。
    """
    try:
        return r.json()
    except Exception:
        return {"_raw": r.text[:200], "_status": r.status_code}


# ---------------------------------------------------------------- PKCE

def new_verifier() -> str:
    return base64.urlsafe_b64encode(os.urandom(48)).decode().rstrip("=")


def challenge_for(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")


# ---------------------------------------------------------------- DCR

async def register_client(redirect_uri: str, name: str = "picsart-pool") -> dict[str, Any]:
    """动态客户端注册（DCR）。第一次加号时调用。

    ⚠️ 这里必须自己捕获 httpx 的网络异常（2026-10-25 修）。

    为什么：`httpx.ConnectTimeout` 的 `str()` 是**空字符串**。以前让它直接
    往上抛，会一路走到 main.py 的全局 handler，客户在浏览器里看到的是一个
    纯黑页面 + `{"error":""}` —— 加点号一点就白屏，且没有任何线索。

    实际触发场景很常见：WorkBuddy 沙箱在境外，访问 auth.picsart.com
    （也在境外但不一定通）经常超时。这不是程序 bug，而是出网问题，
    但用户必须能看到"是网络，不是你操作错了"。
    """
    body = {
        "client_name": name,
        "redirect_uris": [redirect_uri],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
    }
    try:
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.post(REGISTER, json=body, headers={"user-agent": UA})
    except httpx.TimeoutException as e:
        raise AuthError(
            "upstream_timeout",
            "连接 Picsart 授权服务器超时（30 秒）。这是网络问题，不是操作错误。"
            "请稍后重试；若持续失败，说明当前部署环境访问不了 auth.picsart.com，"
            "需要检查出网策略。",
        ) from e
    except httpx.HTTPError as e:
        raise AuthError(
            "upstream_unreachable",
            f"无法连接 Picsart 授权服务器：{type(e).__name__}。"
            "请检查部署环境的网络。",
        ) from e

    data = _json_or_text(r)
    if r.status_code == 429:
        raise AuthError("rate_limited", "auth.picsart.com 限流（Cloudflare 429），稍后再试")
    if r.status_code not in (200, 201) or "client_id" not in data:
        raise AuthError("register_failed", data)
    store.save_client(data["client_id"], redirect_uri)
    return data


# ---------------------------------------------------------------- authorize

MANUAL_REDIRECT_PATH = "/oauth/manual-callback"


async def ensure_client(manual: bool = False) -> dict[str, Any]:
    """取/注册 client。

    manual=True 时用独立的 redirect_uri（/oauth/manual-callback），
    该页面只把 code 显示出来供复制 —— 这是 sub2api 的做法，
    能绕开 accounts.picsart.com 的静默 SSO 复用。

    ⚠️ 两种 redirect_uri 各自对应一个 client，分开存。
    绝不能因为"这次想用另一种模式"就覆盖掉另一种的 client_id：
    refresh_token 与签发它的 client_id 绑定，混用会 400 无效授权。
    """
    want = (
        f"{settings.public_url}{MANUAL_REDIRECT_PATH}"
        if manual
        else settings.redirect_uri
    )
    saved = store.get_client(want)
    if saved:
        return saved
    await register_client(want)
    saved = store.get_client(want)
    assert saved is not None
    return saved


async def build_authorize_url(
    scope: str = "", *, force_login: bool = True, manual: bool = False
) -> tuple[str, str]:
    """返回 (授权 URL, verifier)。verifier 要在兑换 code 时带上。

    两种模式：
      自动回调  -> redirect_uri = {public_url}/oauth/callback
                   本服务直接收 code、换 token、入库，全程无感。
                   缺点：浏览器已有 Picsart 会话时会被静默复用。
      手动粘贴  -> redirect_uri = {public_url}/oauth/manual-callback
                   只显示 code，用户复制回表单（sub2api 的做法）。
                   每次都是独立授权流程，天然支持多账号。

    force_login 会附加 prompt=login / max_age=0 / login_mode=standalone，
    实测会被透传，但 accounts.picsart.com 的静默探测仍可能绕过，
    因此多账号场景建议直接用 manual 模式。

    ⚠️ scope 的默认值**不能写成 `= DEFAULT_SCOPE`**（2026-10-25 修）。

    Python 的默认参数是在**函数定义那一刻**求值的。而配置注入发生在这之后
    （workbuddy/remote.py 在 app.main 被 import 前改写 app.config 的模块属性）。
    所以 `scope: str = DEFAULT_SCOPE` 会把**注入前的空串**永久固化进函数签名 ——
    之后无论怎么注入，oauth 拿到的永远是空 scope。

    症状：以空 scope 发起授权，Keycloak 用自己的默认值，Picsart 侧拒绝授权码，
    报 invalid_grant —— 而错误信息完全指不出「是 scope 空了」。
    两个调用点（api_admin.py:92/178）都不传 scope，所以**每一次加号都会中招**。

    改成空串 + 函数体内取当前值：这样拿到的是注入之后的真值。
    """
    scope = scope or DEFAULT_SCOPE
    if not scope:
        # 配置没加载就发授权请求只会拿到一句看不懂的 invalid_grant。
        # 这里直接说清楚，比让客户对着 Picsart 报错页猜强。
        raise AuthError(
            "config_missing",
            "授权 scope 为空（站点配置未加载）。"
            "请确认已激活并重启应用，然后重试",
        )
    client = await ensure_client(manual=manual)
    verifier = new_verifier()
    params = {
        "client_id": client["client_id"],
        "response_type": "code",
        "scope": scope,
        "code_challenge": challenge_for(verifier),
        "code_challenge_method": "S256",
        "redirect_uri": client["redirect_uri"],
    }
    if force_login:
        params["prompt"] = "login"
        params["max_age"] = "0"
        params["login_mode"] = "standalone"
    return f"{AUTHORIZE}?{urllib.parse.urlencode(params)}", verifier


# ---------------------------------------------------------------- token

async def exchange_code(
    code: str, verifier: str, *, manual: bool = False
) -> dict[str, Any]:
    """用 code 换 token。返回的 bundle 里补一个 client_id，
    因为 refresh_token 只能拿签发它的同一个 client 去刷新。"""
    client = await ensure_client(manual=manual)
    form = {
        "grant_type": "authorization_code",
        "code": code,
        "client_id": client["client_id"],
        "code_verifier": verifier,
        "redirect_uri": client["redirect_uri"],
    }
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(TOKEN, data=form, headers={"user-agent": UA})
    data = _json_or_text(r)
    if r.status_code == 429:
        raise AuthError(
            "rate_limited",
            "auth.picsart.com 限流（Cloudflare 429）。这是请求太频繁，不是 token 失效，"
            "等 1~5 分钟再试即可",
        )
    if r.status_code != 200 or "access_token" not in data:
        raise AuthError("code_exchange_failed", data)
    # client_id 以 **token claim** 为准，而不是我们刚刚发出去的那个。
    # 发放方写进 JWT 的值天然正确；我们自己填的可能是另一个 redirect_uri 对应的
    # client（两种授权模式各有一个），存错了第一次 refresh 就 401。
    data["client_id"] = (
        client_id_from_token(data.get("access_token") or "")
        or client["client_id"]
    )
    return data


async def _raw_refresh(refresh_token: str, client_id: str = "") -> dict[str, Any]:
    """**内部函数，不要直接调用。**

    只换 token 不落库。OAuth 的 refresh_token 是**单次使用**的：
    一旦刷新成功，服务端立刻作废旧值、下发新的。
    如果调用方拿到新 token 却没写库，旧的下次就用不了了 ——
    整个账号永久失效，只能人工重新授权。

    这正是本项目踩过的坑：临时脚本调了 refresh 却没落库，
    导致账号失效。所以对外一律用 refresh_and_persist()。

    client_id 必须是**当初签发这个 refresh_token 的那个 client**。

    全局限流：Cloudflare 对 token 端点的计数特征是「数据中心 ID + IP」，
    我们所有账号共用同一个计数器。所以 refresh 必须走全局令牌桶排队，
    不能各账号自己发 —— 100 个号同时刷新会直接触发 Error 1015。

    熔断期间不重试：官方文档明确「短时间内反复尝试会延长封禁」。
    """
    if not client_id:
        # 走到这里说明调用方没给 client_id（老数据 / add_by_token 没填）。
        # ⚠️ 不能随便取"全库最新一条"：两种授权模式各有一个 DCR client，
        # 取错了 refresh 必 401，然后账号被判 dead。
        #
        # refresh_token 是 opaque token，解不出客户端身份，所以这里**没有**
        # 可靠的兜底 —— 只能报错，让上层用 access_token 的 claim 去纠错
        # （见 refresh_and_persist）。宁可这次不刷，也不要拿错的 client
        # 去打一次注定失败的请求、白白浪费熔断配额。
        raise AuthError(
            "no_client",
            "该账号没有记录 client_id，无法确定用哪个 client 刷新。"
            "请重新授权（新的刷新逻辑会从 token claim 自动补上）。",
        )

    lim = authlimit.limiter()
    if lim.blocked:
        raise AuthError("rate_limited", f"auth 端点熔断中，约 {lim.blocked_for()}s 后恢复")

    # 全局排队取令牌（100 个号会在这里被摊平）
    try:
        await lim.acquire()
    except authlimit.TokenBlocked as e:
        raise AuthError("rate_limited", str(e)) from e

    form = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": client_id,
    }
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(TOKEN, data=form, headers={"user-agent": UA})

    if r.status_code == 429:
        # 按服务端 retry-after 熔断整个通道，**绝不重试**
        secs = lim.block_from_response(r.headers.get("retry-after"), r.status_code)
        # 不打上游域名：错误信息会进日志、也会回显给前端，
        # 而"哪个域名在被限流"本身就是站点知识。
        raise AuthError(
            "rate_limited",
            f"授权端点触发上游限流（Cloudflare Error 1015），"
            f"全局限流 {int(secs)}s。这不是 token 失效",
        )

    data = _json_or_text(r)
    if r.status_code != 200 or "access_token" not in data:
        raise AuthError("refresh_failed", data)
    # 刷新成功 -> 清掉指数退避阶梯（2026-10-25 加）。
    #
    # 不调用的话，一整天零星的限流会累积成"连续第 8 次"，
    # 退避永远停在 900s 封顶 —— 而实际上中间成功过很多次，
    # 早该从头按 30s 起了。
    lim.reset_streak()
    return data


def client_id_from_token(access_token: str) -> str:
    """从 access_token 的 claim 里取出签发它的 client_id。

    为什么以 claim 为准：`bundle["client_id"]` 是**我们自己**在兑换后补进去的
    （见 exchange_code），填错了不会有人拦；而 JWT 里的 `client_id` / `aud`
    是**签发方**写进去的，天然正确。

    refresh_token 是 opaque token（`rt:xxx.yyy`，不是 JWT），解不出东西 ——
    所以纠错源只能是 access_token。这一点很容易想当然搞错。
    """
    claims = parse_jwt(access_token or "")
    cid = str(claims.get("client_id") or "").strip()
    if cid:
        return cid
    # 有些 token 只把 client 放在 aud 里
    aud = claims.get("aud")
    if isinstance(aud, str) and aud.startswith("dcr-"):
        return aud
    if isinstance(aud, list):
        for a in aud:
            if isinstance(a, str) and a.startswith("dcr-"):
                return a
    return ""


async def refresh_and_persist(account: dict[str, Any]) -> dict[str, Any]:
    """刷新 token **并立刻落库**。这是唯一的对外刷新入口。

    为什么必须原子：refresh_token 单次使用，刷新成功即作废旧值。
    只要"刷新成功"和"写库成功"之间有任何缺口（异常、崩溃、
    调用方自己调了内部函数），账号就永久失效。

    这里做两件事：
      1) 自愈 client_id：库里记的和 token claim 里的不一致时，
         先用 claim 的值。refresh_token 只能被**签发它的那个 client** 刷新，
         存错了第一次就会 401 invalid_client，然后账号被判 dead。
         实测 invalid_client / invalid_grant **不消耗** refresh_token，
         所以用 claim 的值重试是免费的、无损的。
      2) 回读校验：确认新 token 真的写进库了，
         否则抛错 —— 宁可这次刷新失败，也不让 token 在黑盒里丢失。
    """
    aid = account["id"]
    rt = account.get("refresh_token") or ""
    if not rt:
        raise AuthError("no_refresh_token", f"{account.get('name','?')} 无 refresh_token")

    stored_cid = (account.get("client_id") or "").strip()
    # token 自带的 client_id —— 用来纠错和校验
    claimed_cid = client_id_from_token(account.get("access_token") or "")

    # 优先用"可信"的那个：claim > 库里的
    use_cid = claimed_cid or stored_cid

    try:
        bundle = await _raw_refresh(rt, use_cid)
    except AuthError as e:
        # 第一次失败 + 我们还有另一个候选 client → 换一个重试一次。
        # 只在**明确是 client 不匹配**时重试，别把真正的 token 失效也重试成风暴。
        other = stored_cid if use_cid == claimed_cid else claimed_cid
        retriable = e.error in ("refresh_failed",) and isinstance(e.detail, dict) \
            and e.detail.get("error") in ("invalid_client", "invalid_grant")
        if not (other and other != use_cid and retriable):
            raise
        bundle = await _raw_refresh(rt, other)
        use_cid = other

    new_at = bundle["access_token"]
    new_rt = bundle.get("refresh_token") or rt
    # 新 token 的 claim 才是最终的真相（如果解得出）
    final_cid = client_id_from_token(new_at) or use_cid or stored_cid

    fields = {
        "access_token": new_at,
        "refresh_token": new_rt,
        "client_id": final_cid,
        "expires_at": token_expiry(bundle),
        "status": "ok",
        "fails": 0,
        "last_error": "",
    }
    store.update_account(aid, **fields)

    # 回读校验：refresh_token 与 expires_at 必须真的变了
    persisted = store.get_account(aid) or {}
    if persisted.get("refresh_token") != new_rt:
        raise AuthError(
            "persist_failed",
            f"{account.get('name','?')} 新 refresh_token 未写进库，已中止以免丢号",
        )
    if persisted.get("access_token") != new_at:
        raise AuthError(
            "persist_failed",
            f"{account.get('name','?')} 新 access_token 未写进库，已中止以免丢号",
        )
    if final_cid and persisted.get("client_id") != final_cid:
        raise AuthError(
            "persist_failed",
            f"{account.get('name','?')} 新 client_id 未写进库（refresh 会再失败）",
        )

    return {**persisted, **fields}


async def revoke(
    token: str, kind: str = "refresh_token", client_id: str = ""
) -> bool:
    """作废 token。返回是否**确认**作废成功。

    以前是：不传 client_id 就去取"全库最新一条"，然后 `except: pass`，
    永远返回 None。于是上层（删号/登出）无论如何都报告"已作废"，
    实际上多半没作废 —— 凭证还活着，只是我们记录没了。

    现在：没有 client_id 就直接返回 False（作废不了就如实说），
    并且检查状态码。上层据此如实上报。
    """
    if not client_id:
        # 同 _raw_refresh：两种授权模式各有一个 client，猜不得
        return False
    form = {"token": token, "client_id": client_id, "token_type_hint": kind}
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.post(REVOKE, data=form, headers={"user-agent": UA})
        return r.status_code in (200, 204)
    except Exception:
        return False


# ---------------------------------------------------------------- jwt / device

def parse_jwt(token: str) -> dict[str, Any]:
    import json

    try:
        part = token.split(".")[1]
        part += "=" * (-len(part) % 4)
        return json.loads(base64.urlsafe_b64decode(part).decode("utf-8", "replace"))
    except Exception:
        return {}


def new_device_id() -> str:
    rnd = base64.b32encode(os.urandom(6)).decode().lower().rstrip("=")
    return f"a.c.{rnd}.{secrets.token_hex(4)}-{secrets.token_hex(2)}-{secrets.token_hex(2)}-{secrets.token_hex(2)}-{secrets.token_hex(6)}"


def token_expiry(bundle: dict[str, Any]) -> float:
    if bundle.get("expires_in"):
        return time.time() + float(bundle["expires_in"])
    claims = parse_jwt(bundle.get("access_token", ""))
    return float(claims.get("exp", 0) or 0)


# ---------------------------------------------------------------- identity

# 公开资料接口：OAuth token 也能调，能拿到 username / 头像 / 订阅档位
USER_SHOW = "https://api.picsart.com/users/show/{uid}.json"


async def fetch_identity(
    access_token: str, device_id: str, *, strict: bool = False
) -> dict[str, Any]:
    """拿 username / sub / 订阅档位，用于管理界面显示。

    Picsart 两套 token，能调的接口不同（实测）：

      OAuth token (aud=dcr-xxx)           浏览器 token (aud=auth-service-web)
      ├ userinfo            ✅ 只有 sub    ├ me.json        ✅ 含 email
      ├ users/show/{uid}    ✅ username    ├ models-catalog ✅ 200
      ├ guard/credits       ✅ 200         └ workflows      ✅ 200
      └ models-catalog      ❌ 401

    邮箱只有 web token 的 me.json 会给；OAuth 的 scopes_supported 里
    根本没有 email scope，所以第三方授权拿不到邮箱——用 username 代替。
    """
    headers = {
        "accept": "application/json",
        "authorization": f"Bearer {access_token}",
        "deviceid": device_id,
        "market": "website",
        "platform": "website",
        "user-agent": UA,
        "referer": "https://picsart.com/",
    }

    claims = parse_jwt(access_token)
    aud = str(claims.get("aud") or "")
    is_oauth_token = aud.startswith("dcr-")
    uid = str(claims.get("sub") or "")

    # 1) web 会话 token -> me.json 有 email，优先用它
    if not is_oauth_token:
        try:
            async with httpx.AsyncClient(timeout=20) as c:
                r = await c.get(ME, headers=headers)
            if r.status_code == 200:
                d = r.json() or {}
                return {
                    "sub": str(d.get("id") or uid),
                    "username": d.get("username") or "",
                    "email": d.get("email") or "",
                    "tier": "",
                                "aud": aud,
                    "token_kind": "web",
                }
            if strict and r.status_code == 401:
                raise AuthError(
                    "token_invalid", "token 无效或已过期（me.json 返回 401）"
                )
        except AuthError:
            raise
        except Exception as e:
            if strict:
                raise AuthError("identity_failed", str(e))

    # 2) OAuth token：先用 userinfo 确认 token 有效并校验 sub
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.get("https://auth.picsart.com/api/userinfo", headers=headers)
        if r.status_code == 200:
            d = r.json() or {}
            sub = str(d.get("sub") or uid)
            if not sub:
                raise AuthError("token_invalid", "userinfo 未返回 sub")

            # 2.1) 公开资料接口补 username / 订阅档位
            username, tier = "", ""
            try:
                async with httpx.AsyncClient(timeout=20) as c:
                    r2 = await c.get(USER_SHOW.format(uid=sub), headers=headers)
                if r2.status_code == 200:
                    p = r2.json() or {}
                    if p.get("status") == "success":
                        username = p.get("username") or ""
                        tier = ((p.get("subscription") or {}).get("tier_id")) or ""
            except Exception:
                pass

            return {
                "sub": sub,
                "username": username,
                "email": "",          # OAuth 拿不到，见函数注释
                "tier": tier,
                "aud": aud,
                "token_kind": "oauth",
            }
        if strict:
            raise AuthError("token_invalid", f"userinfo 返回 {r.status_code}")
    except AuthError:
        raise
    except Exception as e:
        if strict:
            raise AuthError("identity_failed", str(e))

    if strict and not uid:
        raise AuthError("token_invalid", "无法解析 token")
    return {
        "sub": uid,
        "username": "",
        "email": "",
        "tier": "",
        "aud": aud,
        "token_kind": "unknown",
    }


# ---------------------------------------------------------------- cookie 导入

async def login_from_cookie(cookie: str) -> dict[str, Any]:
    """用浏览器 Cookie 直接换出可用身份。

    流程：带 Cookie 调 me.json -> 若 401，说明 cookie 里没有可用会话；
    若 200，则再尝试从常见位置确认。Picsart 网页的会话主要在 cookie 里，
    我们用 me.json 验证有效性并取身份。
    """
    cookie = cookie.strip()
    if not cookie:
        raise AuthError("empty_cookie", "cookie 为空")

    device_id = new_device_id()
    headers = {
        "accept": "application/json",
        "cookie": cookie,
        "deviceid": device_id,
        "market": "website",
        "platform": "website",
        "user-agent": UA,
        "referer": "https://picsart.com/",
        "origin": "https://picsart.com",
    }
    async with httpx.AsyncClient(timeout=25) as c:
        r = await c.get(ME, headers=headers)

    if r.status_code != 200:
        raise AuthError(
            "cookie_invalid",
            f"me.json 返回 {r.status_code}，Cookie 无效或已过期",
        )

    d = r.json() or {}
    return {
        "sub": str(d.get("id") or ""),
        "username": d.get("username") or "",
        "email": d.get("email") or "",
        "device_id": device_id,
    }


def cookie_to_bearer(cookie: str) -> str | None:
    """从 cookie 串里捞 access_token（部分站点会把它放进 cookie）。"""
    for part in cookie.split(";"):
        if "=" not in part:
            continue
        k, _, v = part.partition("=")
        k = k.strip().lower()
        if k in ("access_token", "access-token", "token", "jwt", "auth_token"):
            v = urllib.parse.unquote(v.strip())
            if v.count(".") == 2 and len(v) > 100:
                return v
    return None
