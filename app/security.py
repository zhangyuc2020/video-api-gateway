"""会话/鉴权：管理后台用签名 cookie，反代接口用 Bearer key。"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from typing import Any

from fastapi import HTTPException, Request

from .config import settings

COOKIE_NAME = "pp_session"
MAX_AGE = 86400 * 7


def _sign(payload: bytes) -> str:
    return hmac.new(
        settings.session_secret.encode(), payload, hashlib.sha256
    ).hexdigest()


def make_session(user: str = "admin") -> str:
    """生成会话 token：`payload_b64.signature`。

    ⚠️ base64 要去掉 `=` padding（2026-10-25 修）。
    带 padding 时值里会出现 '='，Starlette 给这种 cookie 加双引号，
    而回传的引号会让 verify_session 解析失败 —— 表现为"登录成功但立刻失效"。
    去掉 padding 后值只剩 [A-Za-z0-9_-] 和 '.'，符合 cookie-value 的字面量
    规则，不会再被加引号。

    解码侧仍然用 urlsafe_b64decode，它对缺失 padding 的输入是容忍的
    （Python 的 base64 只在长度模 4 余 1 时才报错，我们这里是 108/109 字符，
    余 0/1... 实测可解；verify_session 里也有兜底补 padding）。
    """
    data = json.dumps({"u": user, "t": int(time.time())}).encode()
    b = base64.urlsafe_b64encode(data).decode().rstrip("=")
    return f"{b}.{_sign(data)}"


def verify_session(token: str) -> dict[str, Any] | None:
    # ⚠️ 先剥掉可能存在的双引号（2026-10-25 修，这是一个会让**所有登录失效**的 bug）。
    #
    # 现象：/api/login 返回 200，Set-Cookie 里也有 pp_session，但下一个请求
    # 就是 {"detail":"未登录"}。浏览器看起来"登录成功"但一刷新就掉出登录态。
    #
    # 根因链：
    #   1. make_session() 的 payload 是 base64.urlsafe_b64encode(...)，**带 = padding**
    #      （JSON 长度不是 3 的倍数时必然出现 '='）
    #   2. RFC 6265 规定 cookie-value 不能含 '=' 之外的分隔符，而 Starlette/
    #      SimpleCookie 遇到含特殊字符的值时会给整个值**加双引号**
    #   3. 浏览器（和 httpx）把带引号的值原样回传，Request.cookies 里就是
    #      '"eyJ1...==' 这种形式
    #   4. 这里 split(".", 1) 之后 b = '"eyJ1...'，base64 解码抛异常
    #      -> 走 except 返回 None -> require_admin 抛 401
    #
    # 修复只需一行 strip('"')：签名校验仍在，剥引号不降低任何安全性
    # （引号不是签名的一部分，攻击者剥不剥都能被 hmac 拦下）。
    if not token:
        return None
    token = token.strip().strip('"').strip()
    if not token:
        return None
    try:
        b, sig = token.split(".", 1)
        # 兼容三种形态（缺 padding / 带 padding / 旧版带引号）：
        # urlsafe_b64decode 要求长度是 4 的倍数，缺了要补回去。
        raw_b = b
        pad = len(raw_b) % 4
        if pad:
            raw_b += "=" * (4 - pad)
        data = base64.urlsafe_b64decode(raw_b.encode())
        if not hmac.compare_digest(sig, _sign(data)):
            return None
        d = json.loads(data)
        if time.time() - d.get("t", 0) > MAX_AGE:
            return None
        return d
    except Exception:
        return None


def current_user(request: Request) -> dict[str, Any] | None:
    tok = request.cookies.get(COOKIE_NAME)
    if not tok:
        return None
    return verify_session(tok)


def require_admin(request: Request) -> dict[str, Any]:
    """管理接口的鉴权。

    两段判据（2026-10-25 改）：

      ① 没设管理密码（`PICSART_POOL_ADMIN_PASSWORD` 为空）
         -> 只要**本部署已激活**就放行，不再要求登录。
            这是「客户自己部署」形态的默认行为：客户是这台实例的唯一主人，
            激活码就是他的身份凭证。再让他记一个管理密码，只是多一层
            他自己也会忘的门 —— 而忘了之后只能靠改配置文件恢复。

      ② 设了管理密码
         -> 必须登录（session cookie）。保留这条是因为有的客户会把链接
            发给同事，那时"同一实例内区分人"才有意义。

    ⚠️ 安全边界必须说清楚：
      ①的形态下，**任何拿到链接的人都能进管理页** —— 能加/删账号、
      能看任务、能改设置。所以：
        · 链接要当成密码来保管（客户各自部署，通常只给自己用）
        · 想收紧就把 PICSART_POOL_ADMIN_PASSWORD 设上，立刻回到②
        · 号池数据（账号 token）仍受激活门保护，未激活的实例连这个
          函数都到不了（guard 中间件先拦 403）

    实现上**不查 cookie、只查激活状态**，所以不引入 session，
    也不会因为 cookie 引号/padding 之类的问题失效（见 verify_session 的注释）。
    """
    u = current_user(request)
    if u:
        return u

    # 没设密码 -> 用"已激活"当身份
    if not settings.admin_password:
        try:
            from workbuddy.gate import OFFLINE_GRACE, gate as _gate

            g = _gate()
            if g.verify() and (time.time() - g._last_ok) <= OFFLINE_GRACE:
                return {"u": "owner", "via": "activation"}
        except Exception:
            # 授权门未装（本地开发/单测）-> 回落到原来的 401
            pass

    raise HTTPException(status_code=401, detail="未登录")


def check_admin_password(password: str) -> bool:
    if not settings.admin_password:
        return True          # 未设置密码 = 开放（仅内网）
    # compare_digest 对 str 要求两边都是纯 ASCII，否则抛 TypeError。
    # 之前直接用 str，配一个中文/emoji 密码就变成"登录必 500"。
    # 转成 bytes 后任意 Unicode 都能安全比较。
    return hmac.compare_digest(
        (password or "").encode("utf-8"), settings.admin_password.encode("utf-8")
    )


def check_api_key(authorization: str | None) -> bool:
    if not settings.api_keys:
        return True
    if not authorization:
        return False
    tok = authorization[7:].strip() if authorization.lower().startswith("bearer ") else authorization.strip()
    # 逐个常量时间比较，避免 `in list` 的短路差异（虽然收益有限，但没成本）
    ok = False
    for k in settings.api_keys:
        if hmac.compare_digest(tok.encode("utf-8"), k.encode("utf-8")):
            ok = True
    return ok


# ---------------------------------------------------------------- 登录限流

class LoginLimiter:
    """管理页登录失败限流。

    authlimit.py 那套是"别把上游 Cloudflare 打成 1015"，跟防人爆破后台是两件事 ——
    之前 /api/login 完全没接任何限流，可以无限次猜密码。

    规则（内存态，进程级）：
      · 同一来源 IP 连续失败 5 次 → 锁 5 分钟，期间即使密码正确也拒绝
      · 成功一次即清零
      · 来源 IP 取 X-Forwarded-For 第一段，没有就用 socket 地址
    """

    MAX_FAILS = 5
    WINDOW = 300.0        # 失败计数的观察窗口（秒）
    LOCK = 300.0          # 达到上限后锁多久

    def __init__(self) -> None:
        self._fails: dict[str, list[float]] = {}
        self._locked: dict[str, float] = {}

    @staticmethod
    def client_key(request: Request) -> str:
        xff = request.headers.get("x-forwarded-for") or ""
        if xff:
            return xff.split(",")[0].strip()
        return request.client.host if request.client else "unknown"

    def locked_for(self, key: str) -> int:
        until = self._locked.get(key, 0.0)
        remain = until - time.time()
        if remain <= 0:
            self._locked.pop(key, None)
            return 0
        return int(remain) + 1

    def _recent(self, key: str) -> list[float]:
        now = time.time()
        hits = [t for t in self._fails.get(key, []) if now - t < self.WINDOW]
        if hits:
            self._fails[key] = hits
        else:
            self._fails.pop(key, None)
        return hits

    def check(self, key: str) -> int:
        """返回还需锁多少秒；0 = 可以尝试。"""
        return self.locked_for(key)

    def fail(self, key: str) -> int:
        """记一次失败。返回触发的锁定时长（0 = 还没锁）。"""
        hits = self._recent(key)
        hits.append(time.time())
        self._fails[key] = hits
        if len(hits) >= self.MAX_FAILS:
            self._locked[key] = time.time() + self.LOCK
            self._fails.pop(key, None)
            return int(self.LOCK)
        return 0

    def reset(self, key: str) -> None:
        self._fails.pop(key, None)
        self._locked.pop(key, None)


_login_limiter = LoginLimiter()


def login_limiter() -> LoginLimiter:
    return _login_limiter
