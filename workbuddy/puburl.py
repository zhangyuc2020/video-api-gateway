"""把「对外地址」从写死的环境变量改成「按请求实时推导」。

为什么要这个模块
----------------
现有工程靠 `PICSART_POOL_PUBLIC_URL` 静态环境变量决定两件事：

  1. OAuth 的 redirect_uri（`{public_url}/oauth/callback`）
  2. 登录 cookie 的 Secure 属性

在自有服务器上没问题。**在 WorkBuddy 沙箱里行不通**，三条原因都不是
"改个环境变量"能解决的：

  · 域名是发布时才生成的（`https://<随机前缀>.app.workbuddy.link`），
    写进环境变量意味着每次发布都要手改，忘了就"加号一直失败"。
  · 沙箱**不提供自定义环境变量注入** —— 想传也传不进去。
  · DCR 注册的 client 是按 redirect_uri 分行存的，地址一变就得重新注册。

实现方式（以及为什么不用"直接改 settings.public_url"）
----------------------------------------------------
一开始的写法是中间件里 `settings.public_url = 推导值`。这在**并发下是错的**，
而且有粘性：一旦某个请求带了 `X-Forwarded-Host`，之后所有不带头的请求
（本地自检、健康探针）也会看到那个公网域名 —— 自检结果因此变成假数据。

所以改成 `ContextVar`：每个请求一份，请求结束自动失效，互不污染。
`app/config.py` 里 `Settings.public_url` 变成一个 property，先看
ContextVar 有没有覆盖值，没有才回落到环境变量 —— 这样
`oauth.ensure_client()`、登录 cookie、`/api/health` 三处调用点
**一行都不用改**，自动就正确了。

安全边界
--------
· 推导只用于**拼 URL**，不参与任何授权判断。
· 取不到转发头就保持环境变量原值，绝不猜 —— 猜错的表现是 OAuth 回调
  跳到一个不存在的地址，比不猜更难查。
"""
from __future__ import annotations

from contextvars import ContextVar

from fastapi import FastAPI, Request

# 每个请求一份。默认空串 = 没有覆盖，用环境变量。
_override: ContextVar[str] = ContextVar("public_url_override", default="")


def current_public_url() -> str:
    """当前请求上下文里的对外地址；没有就返回空串（调用方回落到环境变量）。"""
    return _override.get()


def _pick(headers) -> tuple[str, str]:
    """从转发头取 (scheme, host)。任一缺失就返回空串，不猜。"""
    host = (headers.get("x-forwarded-host") or "").split(",")[0].strip()
    proto = (headers.get("x-forwarded-proto") or "").split(",")[0].strip().lower()

    if not host or proto not in ("http", "https"):
        return "", ""
    # 去掉端口：网关到应用是一跳，公网端口由网关决定（443/80）。
    if ":" in host and not host.startswith("["):
        host = host.split(":")[0]
    if not host:
        return "", ""
    return proto, host


def install_public_url(app: FastAPI) -> None:
    """挂中间件。必须在 uvicorn.run 之前调用。

    只认 `X-Forwarded-Host`，**不认 `Host`**。
    理由：Host 是客户端可以直接伪造的头，用它拼 OAuth 回调地址等于让攻击者
    指定 redirect_uri。而 X-Forwarded-* 在沙箱里是网关写入的。
    自有服务器（Nginx/Caddy）需要时自己配 `proxy_set_header X-Forwarded-Host $host;`。
    """

    @app.middleware("http")
    async def _derive(request: Request, call_next):
        proto, host = _pick(request.headers)
        token = None
        if proto and host:
            token = _override.set(f"{proto}://{host}")
        try:
            return await call_next(request)
        finally:
            if token is not None:
                _override.reset(token)

    @app.get("/__domain")
    async def _domain():
        """发布后自检用：确认应用真的看到了公网域名。

        不带授权门的刻意的 —— 它只回几个地址，不含任何号池信息，
        却能在"打开白屏/加号失败"时一眼区分「服务没起来」和「URL 算错了」。
        """
        from app.config import settings

        return {
            "public_url": settings.public_url,
            "redirect_uri": settings.redirect_uri,
            "from_env": bool(settings._env_public_url),
        }
