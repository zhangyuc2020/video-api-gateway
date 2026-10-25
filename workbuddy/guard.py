"""激活门中间件 + 激活接口。

它做两件事，边界很清楚：

  放行  —— 静态页面、激活接口、健康检查、`/__domain` 自检
  拦截  —— 其余一切。未激活时返回 403 和一个**能让客户自己操作**的 JSON，
           而不是 500 或者一个空页面

为什么不做成「装饰器加在每个路由上」
----------------------------------
现有 `app/` 有 40+ 个路由，`require_admin` 只挂在管理 API 上，`/v1/*` 反代
接口走的是 API Key。逐个加装饰器有两个风险：漏挂（新加的接口忘了）和重复挂
（激活门 + 管理密码两层都拒，客户看到的是「密码错误」而不是「未激活」）。

中间件是**默认拒绝**：新增路由不用改任何东西就自动受保护。要放行必须显式
列进 `_OPEN_PREFIX`，这个白名单一眼能看完，也就好审计。

激活门和管理密码是两层，不能互相替代
----------------------------------
  激活门   —— 拦住「没付钱的人」。过了这层才能碰号池。
  管理密码 —— 拦住「付了钱但共用同一台服务器的人」。同一个客户内部多个人用。

所以激活成功后仍然要登录管理页，这是刻意的：激活码是「买」，密码是「访问」。
"""
from __future__ import annotations

import json
import logging
import os

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse

log = logging.getLogger("picsart-pool.gate")

router = APIRouter()

# 不需要激活就能访问的路径前缀。
# 每一个都有明确理由，加新的之前先想清楚「未激活的客户为什么需要它」。
_OPEN_PREFIX = (
    "/api/license/",      # 激活本身：客户必须能拿到机器码、提交卡密
    "/healthz",           # 沙箱网关/负载均衡的存活探测
    "/__domain",          # 发布后自检：确认应用看到的是哪个公网域名
    "/__diag",            # 发布后自检：确认配置真的进了上游模块（只报有无）
    "/__probe",           # 网络自检：从本部署测上游可达性（目标写死，不是 SSRF）
    "/__version",         # 版本检查：首屏要显示更新提示，不能要求登录
    "/static/",           # CSS/JS：激活页也要有样式
    "/apple-touch-icon",  # 浏览器自动请求，拦截会在控制台刷红
    "/favicon",
    # 登录/登出/会话查询必须放行（2026-10-25 修）。
    #
    # 为什么：激活页激活成功后要过登录这道门，而登录接口如果被激活门挡住，
    # 就形成死锁 —— 未激活时登录 403，激活后（前端重新 boot）才放行。
    # 更麻烦的是一种常见路径：客户已经激活，但**数据卷被重置**导致
    # 重新变成未激活，此时他连管理页都进不去，看不到任何说明。
    #
    # 这三条本身不泄露号池数据：/api/me 只回一个布尔，/api/login 只验密码。
    # 真正的数据接口仍然在激活门之后。
    "/api/login",
    "/api/logout",
    "/api/me",
    # OAuth 回调必须放行（2026-10-25 修）。
    #
    # 为什么：用户是在**上游授权页**完成授权后被跨站回跳回来的。
    # 这一刻他可能刚好处于「激活门那一瞬间 verify() 没过」或
    # 「数据卷被重置变回未激活」的状态 —— 而他正在做的事情是
    # **添加账号**，不是访问号池数据。
    #
    # 没这条白名单的后果（客户实测反馈）：
    #   点「重新登录」-> 跳到上游授权 -> 回跳 /oauth/manual-callback
    #   -> guard 直接 403 -> 用户看到一片空白
    #   -> 描述成「不出现复制授权码的页面，直接被关掉了」
    #   而这条路是**唯一**能修好一个失效账号的路径。
    #
    # 这条不泄数据：callback 只渲染 URL 里的一次性 code；
    # logout-page 只是一个说明页。真正的写操作（exchange）仍要登录态。
    "/oauth/",
)

# 未激活也要能拿到 HTML —— 否则客户连激活表单都看不到，
# 更看不到自己的机器码，连「要发给发卡方什么」都不知道。
#
# ⚠️ `/activate` 必须在这里。它是激活页本身 —— 曾经漏掉，结果激活页被
# 自己的门拦成 403，客户打开链接看到的是「未激活」四个字，而唯一的入口
# 就是那个链接。这是一个完整死锁，且日志里完全看不出来（403 是"正常"行为）。
_OPEN_EXACT = (
    "/", "/index.html", "/style.css", "/app.js",
    "/activate", "/activate/",
)


def _is_open(path: str) -> bool:
    if path in _OPEN_EXACT:
        return True
    return any(path.startswith(p) for p in _OPEN_PREFIX)


def install_guard(app: FastAPI, gate_getter) -> None:
    """挂中间件。`gate_getter` 是返回 Gate 的可调用对象（延迟取，避免循环导入）。"""

    @app.middleware("http")
    async def _guard(request: Request, call_next):
        path = request.url.path
        if _is_open(path):
            return await call_next(request)

        g = gate_getter()
        if g is None:
            return JSONResponse(
                {"error": "授权系统未初始化", "code": "gate_unavailable"},
                status_code=503,
            )

        # 这里**不走心跳**。心跳有 12h 节流且会发网络请求，
        # 挂在每个请求上会让首次并发的一堆前端请求各自去打一次
        # （Go 侧记过这个事故：并发心跳互写 lic.ok，把文件写坏 →
        # 下次启动 lastOK=0 → 断网客户直接 403）。
        if not g.verify():
            return _blocked(g, path)

        # 验签过了还要看离线宽限。分两种状态给不同的话术：
        #   · 从没成功心跳过（last_ok=0）—— 从没联网成功
        #   · 心跳过期 —— 断网太久
        from workbuddy.gate import OFFLINE_GRACE, _now

        if _now() - g._last_ok > OFFLINE_GRACE:
            return _blocked(g, path, reason="offline_expired")

        g.maybe_heartbeat()

        # 心跳可能刚刚换上新配置（服务端抬了 cfg_ver）。那一刻只是把配置
        # 拉到了 gate._cfg 里，**还没有写进 app.* 模块** —— 而真正发请求的是
        # app.upstream。不补这一步，配置更新等于没生效：
        # 状态页显示"配置已更新"，但请求仍用旧端点。
        #
        # 放在这里而不是 heartbeat 内部，是为了让 gate.py 不依赖 app.*
        # （避免循环导入，也保持"gate 只管授权"的单一职责）。
        if getattr(g, "_cfg_dirty", False):
            try:
                _apply_remote_config(g)
                g._cfg_dirty = False
                log.info("配置已热更新并注入（v%s）", getattr(g, "_cfg_ver", "?"))
            except Exception as e:
                # 注入失败不该拦住这个请求（手上还有上一份可用配置），
                # 但要把标记留着，下个请求继续试。
                log.error("配置热更新注入失败：%s", e)

        return await call_next(request)


def _blocked(g, path: str, reason: str = "not_activated") -> JSONResponse:
    st = g.status()
    msg = (
        "尚未激活，请打开首页输入激活码"
        if reason == "not_activated"
        else "离线超过 72 小时，请让本应用联网一次以续期"
    )
    # /v1/* 是给程序调的：给机器可读的 OpenAI 风格错误，别塞 HTML。
    if path.startswith("/v1/"):
        return JSONResponse(
            {
                "error": {
                    "message": msg,
                    "type": "not_activated",
                    "code": reason,
                }
            },
            status_code=403,
        )
    return JSONResponse(
        {"error": msg, "code": reason, "hwid": st["hwid"]},
        status_code=403,
    )


# ---------------------------------------------------------------- 激活接口

@router.get("/api/license/status")
async def license_status():
    from workbuddy.gate import gate

    try:
        g = gate()
    except RuntimeError:
        return JSONResponse({"error": "gate 未初始化"}, status_code=503)
    return g.status()


@router.post("/api/license/activate")
async def license_activate(request: Request):
    from workbuddy.gate import gate

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "请求体不是合法 JSON"}, status_code=400)
    card = str((body or {}).get("card") or "").strip()
    if not card:
        return JSONResponse({"error": "激活码不能为空"}, status_code=400)

    try:
        g = gate()
        g.activate(card)
    except Exception as e:
        # 400 而不是 500：这是「输入错」或「服务端拒绝」，原文照搬给前端，
        # 客户才知道是卡密填错、被绑到别的机器、还是网络不通。
        return JSONResponse({"error": f"激活失败：{e}"}, status_code=400)

    # ⚠️ 必须在这里把配置**同步到已经 import 的 app.* 模块**。
    #
    # 为什么：客户在网页上激活时，app.main 早就 import 完了，
    # `app/upstream.py` 里的 `from .config import GW` 在 import 那一刻
    # 就把**空串**复制进了它自己的命名空间。只改 app.config 是改不到它的。
    #
    # 漏了这一步的症状：激活成功、状态显示「已激活」、配置也 ready，
    # 但每个生成请求都拼出 `/workflows//submit` 然后 400 ——
    # 而且日志里一切正常，最容易被误判成"上游改了接口"。
    try:
        _apply_remote_config(g)
    except Exception as e:
        log.error("激活后同步站点配置失败：%s", e)
        return JSONResponse(
            {"error": f"授权已写入，但配置同步失败：{e}。请重启应用后重试"},
            status_code=500,
        )

    st = g.status()
    return {
        "ok": True,
        "tier": st["tier"],
        "expire": st["expire"],
        "hwid": st["hwid"],
        "config_ready": st["config_ready"],
    }


def _apply_remote_config(g) -> None:
    """把 gate 里的已解密配置灌进 app.config 并传播到所有 app.* 模块。"""
    from app import config as app_config
    from workbuddy import remote

    cfg = g.config
    if not cfg:
        # 没配置不算致命（可能服务端只发了 license）。但要说清，
        # 否则客户会以为"激活成功=能用"。
        raise RuntimeError("服务端没有下发配置（可能是服务端版本过旧）")
    remote.apply_and_propagate(app_config, cfg)


@router.post("/api/license/refresh")
async def license_refresh():
    """手动拉一次配置。客户报「已激活但什么都用不了」时先点这个。"""
    from workbuddy.gate import gate

    g = gate()
    ok = g.refresh_config(raise_on_error=False)
    if ok:
        try:
            _apply_remote_config(g)
        except Exception as e:
            return {"ok": False, "config_ready": False, "error": str(e)}
    return {"ok": ok, "config_ready": g.config is not None, "error": g._cfg_err}


@router.get("/__probe")
async def probe():
    """从**本部署所在的网络**出发，测上游可达性。

    为什么必须从部署内部测（2026-10-25 加）：

      客户报「添加账号白屏」，我在自己机器上测 Picsart 也超时，
      但那不能证明**客户沙箱**也不通 —— 两地的出网策略完全不同。
      反过来也一样：沙箱能连 pypi / Supabase，不代表能连 Picsart。

      所以必须从跑应用的那台机器发请求。这个端点就是干这个的：
      用与应用相同的方式（trust_env=False，不蹭系统代理）去连几个目标，
      把每个的耗时和结果都报出来。

    安全考虑：
      · 不带授权门 —— 但它只报"通/不通/耗时"，不报任何站点知识
      · 目标地址写死在服务端代码里（这里），不接受调用方传入 ——
        否则它就是个 SSRF 跳板
    """
    import time as _t

    import httpx as _httpx

    # 探测目标**不写死上游路径**（2026-10-25 修）。
    #
    # 第一版直接把 `https://api.picsart.com/gw-v2/v1/models-catalog?...`
    # 写在这里 —— 那等于把「网关前缀 + 模型目录路径」这个逆向结论
    # 明文放进源码，推翻了整个外置设计（客户翻源码就看到了）。
    #
    # 现在：激活后从**已解密的配置**里拼（配置本来就有这些地址）；
    # 未激活时没有配置，就只测域名级别的连通性（HEAD 首页），不暴露路径。
    from app import config as _cfg

    def _targets() -> list[tuple[str, str]]:
        """拼出探测目标。优先用**已解密配置**里的地址。

        未激活时配置是空的，此时只测公共站点（cloudflare/pypi）——
        那足以回答"这台机器有没有出网能力"，
        而"能不能连 Picsart"要等激活后才有意义（也没地址可拼）。
        """
        gw = (_cfg.GW or "").rstrip("/")
        api = (_cfg.API_BASE or "").rstrip("/")
        auth = (_cfg.AUTH_BASE or "").rstrip("/")

        out_t: list[tuple[str, str]] = []
        # auth：OIDC discovery 是公开标准路径，不算站点私有知识
        if auth:
            out_t.append(("auth", auth + "/.well-known/openid-configuration"))
        # api：只打域名根，不拼任何业务路径
        base = gw or api
        if base:
            out_t.append(("api", base + "/"))

        out_t.append(("cloudflare", "https://www.cloudflare.com/cdn-cgi/trace"))
        out_t.append(("pypi", "https://pypi.org/pypi/httpx/json"))
        return out_t

    targets = _targets()

    out = []

    # 代理策略：**两种都测**，而不是只跟一种配置走。
    #
    # 为什么（2026-10-25 修）：第一版只测一种，结果把排查方向带偏了。
    #   · 只测直连 -> 应用其实靠代理才能出网时，探测误报"不通"
    #   · 只跟环境变量 -> 部署环境里残留着给别的程序配的代理时，
    #     探测自己就走进那个坏代理，超时，同样误报
    #
    # 所以对每个目标测两次：
    #   direct —— trust_env=False，强制直连
    #   env    —— trust_env=True（有显式代理则用它），沿用环境
    #
    # 两个都不通 => 确定是网络/地域问题
    # 只有 env 通   => **代理配置是关键**，这本身就是要告诉客户的信息
    async def _one(c, name: str, url: str, mode: str) -> dict:
        t0 = _t.monotonic()
        try:
            async with c.stream("GET", url, follow_redirects=False,
                                timeout=_httpx.Timeout(8.0, connect=6.0)) as r:
                # 拿到响应头就够判断了，不读 body（pypi /simple 是 47MB）
                return {"target": name, "mode": mode, "ok": True,
                        "status": r.status_code,
                        "secs": round(_t.monotonic() - t0, 2)}
        except _httpx.TimeoutException:
            return {"target": name, "mode": mode, "ok": False, "err": "TIMEOUT",
                    "secs": round(_t.monotonic() - t0, 2)}
        except Exception as e:
            return {"target": name, "mode": mode, "ok": False,
                    "err": f"{type(e).__name__}: {str(e)[:60]}",
                    "secs": round(_t.monotonic() - t0, 2)}

    for mode, trust in (("direct", False), ("env", True)):
        kw: dict = {"trust_env": trust}
        if mode == "env":
            explicit = (os.environ.get("PICSART_POOL_UPSTREAM_PROXY") or "").strip()
            if explicit:
                kw["proxy"] = explicit
        try:
            async with _httpx.AsyncClient(**kw) as c:
                for name, url in targets:
                    out.append(await _one(c, name, url, mode))
        except Exception as e:
            out.append({"target": "client", "mode": mode, "ok": False,
                        "err": f"{type(e).__name__}: {str(e)[:60]}", "secs": 0})

    # 一句话结论，省得人自己读表
    def _ok(n: str, mode: str = "direct") -> bool:
        return any(x["target"] == n and x["mode"] == mode and x.get("ok")
                   for x in out)

    auth_any = _ok("auth", "direct") or _ok("auth", "env")
    api_any = _ok("api", "direct") or _ok("api", "env")
    proxy_matters = ((_ok("auth", "env") and not _ok("auth", "direct"))
                     or (_ok("api", "env") and not _ok("api", "direct")))

    # 未激活时配置为空 -> 根本没测 auth/api。这时候说"上游完全不通"是**错的**：
    # 我们没测过（也没有地址可测），只是公共站点通了/没通。
    # 第一版就在这里误报过，会引导客户去换网络环境 —— 而他真正该做的是先激活。
    has_upstream_target = any(x["target"] in ("auth", "api") for x in out)

    if not has_upstream_target:
        pub_ok = _ok("cloudflare", "direct") or _ok("pypi", "direct")
        if pub_ok:
            verdict = ("这台机器出网正常，但**应用还没激活**，"
                       "所以还没法测 Picsart 是否可达。请先激活，再回来重测。")
        else:
            verdict = ("这台机器**完全没有出网能力**（公共站点也不通）。"
                       "请检查网络/代理设置，或换一个部署环境。")
    elif auth_any and api_any:
        verdict = "上游可达，可以正常加号和出片"
        if proxy_matters:
            verdict += ("（注意：必须走代理才通，重新部署时别丢掉代理环境变量）")
    elif api_any and not auth_any:
        verdict = ("api 通但 auth 不通：跑任务没问题，但**加不了新账号**"
                   "（OAuth 需要 auth 域）")
    elif not auth_any and not api_any:
        verdict = ("上游完全不通（直连和走代理都不行）。这台机器**无法使用本应用** —— "
                   "需要在能访问境外网络的部署环境里跑（例如香港/境外服务器）。")
    else:
        verdict = "auth 通但 api 不通：能加号但无法出片，请检查上游 api 域可达性"

    return {
        "verdict": verdict,
        "results": out,
        "proxy_matters": proxy_matters,
        "note": "每个目标测两次：direct=强制直连，env=沿用环境变量/显式代理",
    }


@router.get("/__version")
async def version_info():
    """版本 + 是否有新版本。

    放在 guard 里而不是 api_admin，因为它必须**不需要登录**就能访问 ——
    更新提示条要在首屏显示，那时用户可能还没激活/没登录。

    安全上无风险：只回版本号和公开的 Release 地址。
    """
    from workbuddy import version as V

    try:
        return await V.check_async()
    except Exception as e:
        # check_async 本身不抛，这是最后一道保险：任何意外都不能让
        # 版本检查变成 500（前端会以为整个应用坏了）。
        log.warning("version check 异常：%s", e)
        return {
            "current": V.current(),
            "latest": "",
            "has_update": False,
            "url": f"https://github.com/{V.REPO}/releases",
            "page": f"https://github.com/{V.REPO}/releases",
            "source": V.SOURCE,
            "checked_at": 0,
            "error": type(e).__name__,
        }


@router.get("/__diag")
async def diag():
    """诊断：确认配置真的进了**上游模块**，而不只是 app.config。

    这条接口存在的理由：`apply()` 和 `propagate()` 是两件事，只做前者时
    一切看起来正常（状态页显示已激活、配置 ready），但 `upstream.py`
    里那 20 多个 `from .config import X` 仍然是空的 —— 表现为每个请求
    拼出 `/workflows//submit` 然后 400，日志里完全看不出原因。

    所以这里直接读 `app.upstream` 模块自己的全局量，而不是 `app.config`。
    只报"有没有"，不报具体值（这个接口不带授权门，不能泄漏站点知识）。
    """
    import sys

    up = sys.modules.get("app.upstream")
    cfg = sys.modules.get("app.config")
    names = ("GW", "API_BASE", "TOUCHPOINT", "H_TOUCH", "H_PACKAGE",
             "WAN3_WORKFLOW", "FREE_RESOLUTIONS", "DEFAULT_SCOPE")
    out_up = {}
    out_cfg = {}
    for n in names:
        v = getattr(up, n, None) if up else None
        out_up[n] = bool(v) if not isinstance(v, (list, tuple)) else len(v)
        v2 = getattr(cfg, n, None) if cfg else None
        out_cfg[n] = bool(v2) if not isinstance(v2, (list, tuple)) else len(v2)

    # 空头名会让 httpx 抛 TypeError，或让上游看到一个名字为空的头
    bad = []
    if up:
        try:
            h = up.headers_for({"access_token": "x", "device_id": "d"})
            bad = [k for k in h if not k or not k.strip()]
        except Exception as e:
            bad = [f"headers_for 抛错: {type(e).__name__}: {e}"]

    return {
        "upstream": out_up,
        "config": out_cfg,
        "bad_headers": bad,
        "site_config_loaded": any(out_up.values()) if out_up else False,
    }


# ---------------------------------------------------------------- 激活页

@router.get("/activate", response_class=HTMLResponse)
async def activate_page():
    """一个不依赖前端构建的激活页。

    为什么不复用 `app/static/index.html`：那个页面是号池管理界面，
    它的启动流程会在未激活时被 403 打断，客户看到的是「加载失败」
    而不是「请输入激活码」。独立页面把这两件事分开，也让排错变简单 ——
    打开 `/activate` 就能分清「服务没起来」和「没激活」。
    """
    return HTMLResponse(
        """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>激活 · Wan3 号池</title>
<style>
 html,body{margin:0;height:100%;background:#0d0d10;color:#e6e6ef;
   font:14px/1.7 -apple-system,'Segoe UI','Microsoft YaHei',sans-serif}
 .wrap{min-height:100vh;display:flex;flex-direction:column;
   align-items:center;justify-content:center;gap:14px;padding:24px}
 .card{width:min(460px,100%);background:#16161c;border:1px solid #26262f;
   border-radius:14px;padding:26px}
 h1{font-size:18px;margin:0 0 6px}
 p.sub{margin:0 0 18px;color:#8b8b99;font-size:13px}
 label{display:block;font-size:12px;color:#8b8b99;margin:14px 0 6px}
 code{display:block;background:#0d0d10;border:1px solid #26262f;border-radius:8px;
   padding:10px 12px;font-size:13px;word-break:break-all;color:#ff3ea5}
 input{width:100%;box-sizing:border-box;background:#0d0d10;color:#e6e6ef;
   border:1px solid #33333f;border-radius:8px;padding:11px 12px;font-size:14px}
 input:focus{outline:none;border-color:#ff3ea5}
 button{width:100%;margin-top:16px;background:#ff3ea5;color:#fff;border:0;
   border-radius:8px;padding:12px;font-size:14px;font-weight:600;cursor:pointer}
 button:disabled{opacity:.55;cursor:default}
 .msg{margin-top:14px;font-size:13px;min-height:20px}
 .err{color:#ff6b6b}.ok{color:#3ddc97}
 .hint{color:#6b6b7a;font-size:12px;margin-top:16px;line-height:1.8}
</style></head><body>
<div class="wrap"><div class="card">
  <h1>激活本应用</h1>
  <p class="sub">把下面的部署标识发给发卡方换取激活码，填进来即可。</p>
  <label>部署标识（发给发卡方）</label>
  <code id="hwid">读取中…</code>
  <label>激活码</label>
  <input id="card" placeholder="XXXX-XXXX-XXXX-XXXX" autocomplete="off">
  <button id="go" onclick="doActivate()">激活</button>
  <div class="msg" id="msg"></div>
  <div class="hint">
    激活后换绑/迁移：把同一个部署标识发给发卡方解绑即可。<br>
    数据卷被清空会换新标识，届时需要重新激活。
  </div>
</div></div>
<script>
async function load(){
  try{
    const r = await fetch('/api/license/status',{cache:'no-store'});
    const d = await r.json();
    document.getElementById('hwid').textContent = d.hwid || '（读取失败）';
    if(d.activated){
      msg('已激活（'+(d.tier||'')+'，到期 '+
        new Date((d.expire||0)*1000).toLocaleDateString()+'）','ok');
    }
  }catch(e){ msg('读取状态失败：'+e,'err'); }
}
function msg(t,cls){
  const m=document.getElementById('msg');
  m.textContent=t; m.className='msg '+(cls||'');
}
async function doActivate(){
  const card=document.getElementById('card').value.trim();
  if(!card){ msg('请先填激活码','err'); return; }
  const b=document.getElementById('go');
  b.disabled=true; b.textContent='激活中…'; msg('');
  try{
    const r=await fetch('/api/license/activate',{
      method:'POST', headers:{'content-type':'application/json'},
      body:JSON.stringify({card})});
    const d=await r.json();
    if(r.ok){
      msg('激活成功，正在进入…','ok');
      setTimeout(()=>location.replace('/'),700);
    }else{
      msg(d.error||'激活失败','err'); b.disabled=false; b.textContent='激活';
    }
  }catch(e){
    msg('请求失败：'+e,'err'); b.disabled=false; b.textContent='激活';
  }
}
load();
</script></body></html>""",
        headers={"cache-control": "no-store"},
    )
