"""FastAPI 入口。"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

from . import api_admin, api_proxy, store
from .config import settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("picsart-pool")

STATIC = Path(__file__).parent / "static"

# 后台探测循环的句柄（模块级，shutdown 时要用）
_probe_task: "asyncio.Task | None" = None

app = FastAPI(title="Picsart Wan3 号池反代", version="1.0.0", docs_url="/docs")

app.include_router(api_admin.router)
app.include_router(api_proxy.router)


@app.on_event("startup")
async def _startup() -> None:
    store.init()
    # 上个进程留下的非终态行，分三类处理：
    #
    #  A) 已经拿到 upstream_id 的 —— **续跟**。
    #     上游任务的真实身份就是那个 id，拿着它 GET .../{id}/result 就能接着轮询，
    #     跑完了把成片收回来。不需要重新 submit（重提交会白占名额、可能重复出片）。
    #     这类行**不能**判死，否则等于把还在上游跑的任务丢掉，
    #     而且它占着的并发名额对我们就变成看不见的黑洞。
    #
    #  B) queued 且没提交过，但**参数还在** —— **重新排队**。
    #     这种任务连号都没拿到，重启对它只是"还没轮到"。
    #     参数完整存在 params 列里，直接重新入队即可，不该判死 ——
    #     否则调用方明明提交成功了，重启一次就被告知失败。
    #
    #  C) 既没 upstream_id、参数也丢了 —— 真没法救，判失败。
    #     这类还会吃 count_running_tasks() 的配额，跑满 max_concurrent_tasks
    #     次崩溃后所有异步请求恒 429，且重启不恢复。必须清掉。
    try:
        resumable = store.list_resumable_tasks(api_proxy.pool.TASK_MAX_LIFETIME)
        keep = [t["id"] for t in resumable]
        n = store.reset_unfinished_tasks(keep_ids=keep)
        if n:
            log.warning("startup: 清理了 %s 条无法恢复的遗留任务", n)
    except Exception as e:      # 清理失败不该拦住启动
        log.error("startup: 清理遗留任务失败: %s", e)
        resumable = []
    s = settings
    log.info("data dir       : %s", s.data_dir)
    log.info("public url     : %s", s.public_url)
    log.info("redirect uri   : %s", s.redirect_uri)
    log.info("free tier      : %s", "/".join(s.free_fallback))
    log.info("admin password : %s", "set" if s.admin_password else "NOT SET (open)")
    log.info("api keys       : %s", len(s.api_keys) or "open")
    log.info("task lifetime  : %ss", int(api_proxy.pool.TASK_MAX_LIFETIME))
    log.info("follow mode    : %s", "SSE" if api_proxy.pool.USE_SSE else "polling")

    # 明确把"敞开着"这件事喊出来。以前只是 log 一行 INFO，夹在其它日志里
    # 根本看不见 —— 而这两个开关一旦没设，等于把后台和号池都挂在公网上。
    if not s.admin_password:
        log.warning(
            "!!! PICSART_POOL_ADMIN_PASSWORD 未设置：管理后台**无需登录**即可访问。"
            " 只要这个端口能被别人摸到，号池里的 token 就等于公开的。"
        )
    if not s.api_keys:
        log.warning(
            "!!! PICSART_POOL_API_KEYS 未设置：/v1/* 反代接口**不校验 key**，"
            " 任何人拿到地址都能白嫖你的号池。"
        )
    if not s.public_url.startswith("https://") and "localhost" not in s.public_url \
            and "127.0.0.1" not in s.public_url:
        log.warning(
            "!!! PICSART_POOL_PUBLIC_URL 不是 https：管理会话 cookie 无法加 Secure，"
            " OAuth 授权码也会明文过网。建议前置 Nginx/Caddy 上 TLS。"
        )

    # 预热共享连接池（按账号数自动算大小）
    await api_proxy.upstream.client()

    # 启动「号主动宣告可用」的后台探测循环。
    # 没有它，号池里就没有任何东西会去确认上游名额是否释放 ——
    # 任务只能靠撞 429 试出来，正是老的退避模型的病根。
    global _probe_task
    _probe_task = api_proxy.pool.pool().start_probes()
    log.info(
        "slot probe     : every %.0fs (throttle %.0fs/account)",
        api_proxy.pool.PROBE_INTERVAL, api_proxy.pool.PROBE_TTL,
    )

    # 把能续跟的任务重新挂起来。放在 client 预热之后，避免抢连接。
    if resumable:
        try:
            n = api_proxy.resume_tasks(resumable)
            log.warning("startup: 续跟 %s 条上次进程未完成的任务", n)
        except Exception as e:
            log.error("startup: 续跟失败: %s", e)
    else:
        log.info("startup: 没有需要续跟的任务")


@app.on_event("shutdown")
async def _shutdown() -> None:
    global _probe_task
    if _probe_task is not None:
        _probe_task.cancel()
        _probe_task = None
    await api_proxy.upstream.aclose()
    log.info("resources released")


@app.get("/healthz")
async def healthz():
    try:
        n = len(store.list_accounts())
    except Exception:
        n = -1
    return {"ok": True, "accounts": n}


@app.get("/", response_class=HTMLResponse)
async def index():
    """把 CSS/JS 内联进 HTML。

    原因：容器/反代/各种浏览器扩展环境下，外部静态资源经常加载失败，
    之前出现过「页面完全无样式、登录框和主界面同时显示」的问题。
    内联后一次请求拿到完整页面，彻底不依赖额外资源加载。
    """
    html_f = STATIC / "index.html"
    css_f = STATIC / "style.css"
    js_f = STATIC / "app.js"
    if not html_f.exists():
        return HTMLResponse("<h1>UI 未找到</h1>", status_code=500)

    html = html_f.read_text("utf-8")
    css = css_f.read_text("utf-8") if css_f.exists() else ""
    js = js_f.read_text("utf-8") if js_f.exists() else ""

    html = html.replace(
        '<link rel="stylesheet" href="/style.css">',
        f"<style>\n{css}\n</style>",
    )
    html = html.replace(
        '<script src="/app.js"></script>',
        f"<script>\n{js}\n</script>",
    )
    return HTMLResponse(html, headers={"cache-control": "no-store"})


@app.get("/app.js")
async def app_js():
    return FileResponse(STATIC / "app.js", media_type="application/javascript")


@app.get("/style.css")
async def style_css():
    return FileResponse(STATIC / "style.css", media_type="text/css")


@app.exception_handler(Exception)
async def _unhandled(request: Request, exc: Exception):
    """兜底异常处理。

    ⚠️ 必须保证 error 字段**永远不是空串**（2026-10-25 修）。

    以前是 `{"error": str(exc)}`。而 httpx 的超时异常（ConnectTimeout /
    ReadTimeout）`str()` 出来就是空字符串 —— 于是客户在浏览器里看到的是
    一个纯黑的页面、上面写着 `{"error":""}`，完全不知道发生了什么。

    实际触发场景：使用 WorkBuddy 沙箱部署时，沙箱出口访问
    `auth.picsart.com`（境外）经常超时。加号一点就白屏，而唯一的信息是
    一个空字符串。这种"错误信息丢失"比错误本身更难查。

    修法：空消息时按异常类型给出人能读懂的话。
    """
    log.exception("unhandled: %s", exc)

    msg = str(exc or "").strip()
    if not msg:
        name = type(exc).__name__
        low = name.lower()
        if "timeout" in low:
            msg = (
                "连接上游超时（不是本程序的故障）。"
                "当前部署环境访问 Picsart 授权服务器不通或很慢，"
                "请稍后重试；若一直失败，检查部署环境的出网策略。"
            )
        elif "connect" in low:
            msg = (
                "无法连接上游服务（不是本程序的故障）。"
                "部署环境的出网可能被限制，请检查网络策略。"
            )
        else:
            msg = f"服务内部错误（{name}），请把这条信息发给技术支持"

    return JSONResponse(
        {"error": msg, "type": type(exc).__name__},
        status_code=500,
    )
