"""上游客户端：wan3 提交 / 轮询 / 计费 / 余额 / 身份。

实测契约（2026-10，真实出片验证过）：
  POST {GW}/workflows/wan/v3/video/submit  {"params": {...}}  -> 201 {"response":{"id":...}}
  GET  {GW}/workflows/wan/v3/video/{id}/result                -> IN_PROGRESS -> COMPLETED
        COMPLETED 时 response.result.url = gcdn.picsart.com/...mp4，usage.credits=0
  POST {GW}/workflows/wan/v3/video/options {"params":{"model":...},"resolution":...}
        -> toolId / credits / price，用来判断某档位是否收费

**根因记录**：workflows 必须走 /gw-v2 网关前缀。漏掉前缀会落到另一套路由，
返回 429 concurrency_limit_exceeded（其实根本没有任务在跑）或 402，
让人误判成"并发被占"或"额度不足"。

免费档：480P / 720P（0 积分，与时长无关，5/10/15/30s 都免费）。
1080P 与 -prime 收费：1080P = 30 积分 / 5s。credits=0 的号会 402。

**resolution 必须显式传**：不传时服务端按 1080P 计价直接 402，
不是"没额度"，是这个字段缺失。

media 元素 schema（实测反推）：
  {"type": "reference_image|reference_video|reference_audio|first_frame|last_frame|file|link",
   "url": "https://..."}
  数组 1~20 个。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import Any

import httpx

from . import store

log = logging.getLogger("picsart-pool")

from .config import (
    API_BASE,
    APP_AUTHORIZATION,
    AUTH_PREFIX,
    BROWSER_HEADERS,
    CATALOG,
    CREDITS,
    DEFAULTS,
    DRIVE_HEADERS,
    DRIVE_OPTIONS,
    DURATION_AUTO,
    DURATION_MAX,
    DURATION_MIN,
    DURATION_WITH_VIDEO_REF,
    FEATURE_ID_FORMAT,
    FEATURES,
    FRAME_TYPES,
    FREE_RESOLUTIONS,
    GW,
    H_ACCEPT,
    H_APP_AUTH,
    H_AUTH,
    H_COOKIE,
    H_DEVICE,
    H_PACKAGE,
    H_TOUCH,
    H_TOUCH_REF,
    H_UA,
    MEDIA_KINDS,
    MEDIA_LIMITS,
    MEDIA_MAX,
    MEDIA_TYPES,
    MESSAGES,
    MODEL_ALIASES,
    MODEL_SLUG,
    PACKAGE_ID_BY_TIER,
    PAID_RESOLUTIONS,
    PENDING_USES,
    PRIME_SUFFIX,
    RATIOS,
    REFERENCE_TYPES,
    REF_VIDEO_AUDIO_TOTAL_MAX,
    STATIC_HEADERS,
    STATUS_VALUES,
    SUCCESS_CODES,
    TOUCHPOINT,
    TOUCHPOINT_REFERRER,
    UA,
    USER_SHOW,
    WAN3_WORKFLOW,
    WORKFLOW_KIND,
    settings,
)

# ⚠️ UA / BROWSER_HEADERS 从 config 导入，**不要在这里再赋值**。
#
# 上一版这里写了 `UA = "..."` 和 `BROWSER_HEADERS = {...}` 字面量 ——
# 那会覆盖掉从 config 导入的值，等于把「配置外置」的努力全部作废：
# 客户翻源码照样能看到完整的 UA 版本号和 Client Hints 组合。
#
# 现在它们必须保持"从 config 来的引用"身份，这样 propagate() 改写
# app.config.UA 之后，upstream 里的 UA 会跟着变（因为它俩是同一个模块
# 对象上的属性，propagate 会同时改两个模块）。

SUCCESS = (200, 201, 202)


# submit 允许的字段（其余一律剥掉，避免把内部标记 _xxx 传上去）
ALLOWED_PARAMS = (
    "model",
    "prompt",
    "negative_prompt",
    "resolution",
    "ratio",
    "duration",
    "audio",
    "seed",
    "media",
    "enable_thinking",
    "watermark",
    # Picsart 网关对 prompt_extend 不做类型校验（传 "BAD" 也 429 通过），
    # 说明它很可能只是被透传/忽略。保留转发，不承诺效果。
    "prompt_extend",
    # 网页端 submit 会带 options（drive 落盘配置）。
    # 不带的话上游可能当裸调用处理、走低优先级队列。
    "options",
)


def _retry_after(r: httpx.Response) -> float | None:
    """读上游 429 响应里的 `retry-after`，单位秒。

    为什么必须读（2026-10-25 补，Go 版也没读，两边同缺）：
      Cloudflare 的限流响应会明确给出 retry-after（实测 60）。我们过去一律
      用自己拍的指数退避，**无视上游的指示**。后果有两个：

        · 退避短了 → 到点又打，Cloudflare 视为持续滥用，**延长封禁窗口**
          （authlimit.py 顶部记着实测：一晚上约 70 次就 1015，且"不要在短时间
          内反复尝试，这会延长封禁"）
        · 退避长了 → 号明明 60 秒后就能用，我们干等 4 分钟

    retry-after 也可能是 HTTP-date 格式（RFC 7231 允许），这里一并处理。
    解析不出来就返回 None，让调用方回落到自己的退避。
    """
    v = (r.headers.get("retry-after") or "").strip()
    if not v:
        return None
    try:
        secs = float(v)
    except ValueError:
        # HTTP-date 形式，例如 "Wed, 21 Oct 2026 07:28:00 GMT"
        try:
            from email.utils import parsedate_to_datetime

            dt = parsedate_to_datetime(v)
            secs = dt.timestamp() - time.time()
        except Exception:
            return None
    # 夹一下：太小的值（0/负数）没意义，太大（>1h）多半是解析错，别把号锁死
    if secs <= 0:
        return None
    return min(secs, 3600.0)


class UpstreamError(RuntimeError):
    def __init__(
        self,
        reason: str,
        status: int = 0,
        detail: Any = None,
        retry_after: float | None = None,
    ):
        self.reason = reason
        self.status = status
        self.detail = detail
        # 上游明确要求的重试间隔（秒）。None = 上游没给，调用方自行退避。
        self.retry_after = retry_after
        super().__init__(f"{reason} (http {status})")


def package_id_for(account: dict[str, Any]) -> str:
    """按账号**真实订阅档位**决定该声明哪个 x-sub-package-id。

    为什么不能写死一个全局常量：
      这个头不是"我要优惠"，而是"我的账号订的是这个 SKU"。
      实测（见 config.PACKAGE_ID_BY_TIER 注释）服务端会拿它去查 entitlement：
      pro_monthly / pro_yearly 能对上一个真 pro 号 → 0 积分；
      gold / pro_annual 对不上 → 15 积分。

      写死一个 pro 包 ID，等于**让池子里所有号都宣称自己是 pro**。
      号池里全是 pro 时看不出问题，一旦混进非 pro 号：
        · 轻则白跑一趟（服务端不认，按原价 402）
        · 重则是在替一个非 pro 账号谎报订阅 —— 这不是我们该做的事，
          而且服务端一旦开始校验，整个池子会被判风险。

    优先级：
      1) accounts.package_id —— 显式配置过就尊重它（迁移/特殊套餐用）
      2) 由 accounts.tier_id 映射（pro -> pro_monthly）
      3) 都没有 → 返回空串，**不发送这个头**。宁可按原价跑 / 被 402 拒绝，
         也不要谎报。
    """
    explicit = (account.get("package_id") or "").strip()
    if explicit:
        return explicit
    tier = str(account.get("tier_id") or account.get("tier") or "").strip().lower()
    return PACKAGE_ID_BY_TIER.get(tier, "")


def headers_for(account: dict[str, Any]) -> dict[str, str]:
    """按配置拼上游请求头。

    ⚠️ 这里**不能**有字面量。头名（`x-touchpoint` 之类）和头值
    （`ai-playground` 之类）就是"免费档怎么做到的"这个答案本身。
    真值全部来自配置（`app.config` 的 H_* / TOUCHPOINT / STATIC_HEADERS 等），
    由 workbuddy/remote.py 在启动或激活时注入。

    未注入时这里会拼出空头名的字典 —— `require_site_config()` 会先一步
    给出明确错误，不会走到这里发一个残缺请求。
    """
    h: dict[str, str] = {}
    # 头名本身也是知识，全部来自配置；空则不发这个头。
    if H_ACCEPT:
        h[H_ACCEPT] = "application/json"
    if H_DEVICE:
        h[H_DEVICE] = account.get("device_id") or ""
    # 静态头（platform / market / origin / referer …）整块来自配置
    h.update(STATIC_HEADERS)
    if H_TOUCH and TOUCHPOINT:
        h[H_TOUCH] = TOUCHPOINT
    if H_TOUCH_REF and TOUCHPOINT_REFERRER:
        h[H_TOUCH_REF] = TOUCHPOINT_REFERRER
    if H_UA and UA:
        h[H_UA] = UA

    # 只在账号真有订阅时才声明。空值不进 headers（httpx 也不会替我们删掉空的头）。
    pkg = package_id_for(account)
    if pkg and H_PACKAGE:
        h[H_PACKAGE] = pkg

    h.update(BROWSER_HEADERS)

    # ---- 每号一套稳定的设备指纹（覆盖上面的全局默认值）----
    #
    # 为什么要盖：所有账号共用出口 IP 已经是关联信号，如果连 UA 和
    # Client Hints 都完全一样，上游看到的特征就是"同一个 IP 上有一模一样
    # 的 N 个浏览器"。给每个号派生自己的指纹，是成本最低的隔离手段。
    #
    # ⚠️ 必须**整套覆盖**，不能只改 UA：
    #      UA 说 Chrome/130 而 sec-ch-ua 说 138 是自相矛盾，
    #      比不做隔离更可疑 —— 那等于自证伪造。
    #      fingerprint.py 保证版本/平台/语言同源派生，天然自洽。
    #
    # 老数据 / cookie 账号可能没有 device_id，此时 fingerprint 会用
    # sub 或 name 做种子，仍然稳定；实在没有就回落到上面的配置值。
    try:
        from . import fingerprint as _fp

        fp = _fp.fingerprint(account)
        if H_UA and fp.get("user-agent"):
            h[H_UA] = fp["user-agent"]
        # Client Hints 只在配置里有这些头时才盖（配置没发就别自己加）
        for k in ("sec-ch-ua", "sec-ch-ua-mobile", "sec-ch-ua-platform",
                  "accept-language", "language-code"):
            if k in BROWSER_HEADERS and fp.get(k):
                h[k] = fp[k]
    except Exception:
        # 指纹是增强，不是必需。派生失败就用配置的默认值，别让请求挂掉。
        pass

    if APP_AUTHORIZATION and H_APP_AUTH:
        # 网页端会带这个应用级令牌。实测不带也能提交，但带上更贴近真实客户端。
        h[H_APP_AUTH] = APP_AUTHORIZATION
    if account.get("access_token") and H_AUTH:
        h[H_AUTH] = f"{AUTH_PREFIX}{account['access_token']}"
    elif account.get("cookie") and H_COOKIE:
        h[H_COOKIE] = account["cookie"]
    return h


def drive_options(params: dict[str, Any]) -> dict[str, Any]:
    """构造网页端同款的 options.drive 落盘配置。

    网页端抓包（HAR）里 submit 的 body 带这一段，我们之前完全没发：

        "options": {
          "inputs_transformation": {"downscale_oversized_images": true},
          "drive": {
            "name": "<ms>_<prompt前几字>-...-<ms>.mp4",
            "attributes": {
              "model": "...", "aiSDKPayload": "...",
              "appId": "...", "appType": "..."
            },
            "folder": {"path": "..."}
          }
        }

    为什么重要：这是「AI Playground 应用的用户生成」的完整标识。
    缺了它，上游可能把请求当成裸调用，走低优先级队列 ——
    这正是我们观测到"每个任务固定等 20 分钟"的可疑原因。

    ⚠️ 模板全部来自配置（`rules.drive_options` + `headers.drive`）。
    这里只负责填占位符，不写字面量 —— appId / appType / folder
    这些都是能让人直接复现请求的知识。
    """
    tmpl = DRIVE_OPTIONS or {}
    drv_hdr = DRIVE_HEADERS or {}
    ms = int(time.time() * 1000)
    prompt = str(params.get("prompt") or "")
    slug = "".join(ch for ch in prompt[:16] if ch.isalnum()) or MODEL_SLUG or "wan"
    sdk = {
        "prompt": prompt,
        "duration": params.get("duration", settings.default_duration),
        "resolution": params.get("resolution", settings.default_resolution),
        "aspectRatio": params.get("ratio", settings.default_ratio),
        "generateAudio": bool(params.get("audio", False)),
        "enableThinking": bool(params.get("enable_thinking", False)),
        "watermark": bool(params.get("watermark", False)),
    }
    subs = {
        "ms": str(ms),
        "slug": slug,
        "prompt": prompt,
        "duration": str(sdk["duration"]),
        "resolution": str(sdk["resolution"]),
        "ratio": str(sdk["aspectRatio"]),
        "audio": str(sdk["generateAudio"]).lower(),
        "enable_thinking": str(sdk["enableThinking"]).lower(),
        "watermark": str(sdk["watermark"]).lower(),
        "model": normalize_model(str(params.get("model") or "")),
        "json:sdk_payload": json.dumps(sdk, ensure_ascii=False),
        "app_id": drv_hdr.get("appId", ""),
        "app_type": drv_hdr.get("appType", ""),
        "folder": drv_hdr.get("folder", ""),
    }
    return _fill(tmpl, subs)


def _fill(obj: Any, subs: dict[str, str]) -> Any:
    """递归替换模板里的 `{name}` 占位符。

    只替换单层花括号且名字在 subs 里的；`{json:sdk_payload}` 这种带冒号的
    特殊占位符也支持（网页端 HAR 里 aiSDKPayload 是个 JSON 字符串，
    直接 format 会把花括号吃掉）。
    """
    if isinstance(obj, dict):
        return {k: _fill(v, subs) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_fill(v, subs) for v in obj]
    if not isinstance(obj, str):
        return obj
    out = obj
    # 先处理 json: 前缀的特殊占位符（它的值本身含花括号）
    for k, v in subs.items():
        out = out.replace("{" + k + "}", str(v))
    return out



# ---------------------------------------------------------------- 免费档

def normalize_model(model: str) -> str:
    """把用户传入的模型名归一成上游认的形式。

    映射表来自配置（`rules.model_aliases`）—— 里面出现的每个模型名
    （含 `-prime` 这种付费标识）都是站点知识，不能硬编码。
    映射表为空时退化为恒等映射（未注入状态，反正也发不出请求）。
    """
    m = (model or "").strip()
    if not m:
        return ""
    return MODEL_ALIASES.get(m, m)


def is_free_tier(resolution: str, model: str = "") -> bool:
    model = model or settings.default_model
    if PRIME_SUFFIX and normalize_model(model).endswith(PRIME_SUFFIX):
        return False
    return str(resolution).upper() in FREE_RESOLUTIONS


def feature_id_for(
    resolution: str,
    model: str = "",
    kind: str = "",
) -> str:
    """拼出计费 feature_id，例如 `text-to-video.<model>.720p`。

    kind 由输入类型决定（实测）：
      纯文本 -> text-to-video / 有图或音频 -> image-to-video / 有视频 -> video-to-video

    模板来自配置（`rules.feature_id_format`），含 `{kind}` `{model}`
    `{resolution_lower}` 三个占位符。
    """
    kind = kind or (WORKFLOW_KIND.get("text") or "")
    model = normalize_model(model or settings.default_model)
    tpl = FEATURE_ID_FORMAT or "{kind}.{model}.{resolution_lower}"
    return tpl.format(kind=kind, model=model, resolution_lower=str(resolution).lower())


def kind_for_media(media: list[dict[str, Any]] | None) -> str:
    """按 media 内容判断工作流类型。

    判据用 `MEDIA_KINDS`（media type -> 归类）而不是写死的类型名 ——
    `reference_video` / `first_frame` 这些字符串本身也是知识。
    """
    text = WORKFLOW_KIND.get("text", "")
    if not media:
        return text
    kinds = {
        MEDIA_KINDS.get(str(m.get("type") or ""), "")
        for m in media
        if isinstance(m, dict)
    }
    if "video" in kinds:
        return WORKFLOW_KIND.get("video", text)
    if kinds & {"image", "frame"}:
        return WORKFLOW_KIND.get("image", text)
    if "audio" in kinds:
        # 实测纯音频也归 image-to-video
        return WORKFLOW_KIND.get("image", text)
    return text


# ---------------------------------------------------------------- 连接池

# 几十个账号 × 各自并发任务，如果每次请求都新建 AsyncClient，
# 每个请求都要重做 TLS 握手（约 100~300ms），而且 fd 会爆。
# 这里用一个进程级共享 client，连接复用、限流、超时都统一管理。
_CLIENT: httpx.AsyncClient | None = None
_CLIENT_LOCK = asyncio.Lock()


def pool_limits() -> dict[str, int]:
    """连接池大小按账号规模动态推算。

    **关键：SSE 是长连接。** 一个任务跑 5 分钟 = 一条连接被占 5 分钟。
    100 个任务跑满 = 100 条连接被同时占住，不是"瞬时复用"。

    而 submit/options/轮询这些短请求也要从这个池里借连接。
    如果池子刚好等于账号数，短请求就会被 SSE 饿死 ——
    明明账号空闲，却因为没连接可用而卡住。

    所以按账号数的 3 倍给，给短请求留出余量：
      账号数    池大小
      1         10（下限，避免小池子抖动）
      10        30
      100       300
      200       600

    实测单连接约 232 KB 内存，300 条约 70 MB，可接受。
    真正的约束是**文件句柄上限**（Linux 默认 1024），
    容器部署时务必调 ulimit -n（建议 = 池大小 × 2）。
    """
    try:
        n = max(1, len(store.list_accounts()))
    except Exception:
        n = 1
    size = max(10, min(n * 3, 800))
    return {
        "max_connections": size,
        "max_keepalive_connections": max(5, size // 2),
        "keepalive_expiry": 30.0,
    }


async def client() -> httpx.AsyncClient:
    """取共享 AsyncClient（懒初始化）。

    注意：SSE 长连接和短请求共用一个池。这没问题（池够大），
    但不能给 SSE 单独一个 client —— 那样连接数会翻倍。

    ⚠️ 代理行为必须显式指定（2026-10-25 补，对齐 Go 版 proxy.go）。

    在此之前这里没写 `proxy=` 也没写 `trust_env=`，httpx 的默认是
    **读环境变量**（HTTP_PROXY / HTTPS_PROXY）。看似合理，实际有两个坑：

      1. **和授权请求不一致**：gate.py 里是 `trust_env=False`（强制直连）。
         于是同一个进程里，授权请求直连、上游请求走代理 ——
         排查时看到的现象是"能激活但出不了片"，很难联想到代理。

      2. **服务器环境里的环境变量常是给别的程序配的**（比如 Docker、
         CI、或系统装的全局梯子）。客户部署到云上时，这些变量可能指向
         一个到不了 Picsart 的代理，而我们又"看起来正常地"用了它。

    现在的策略和 Go 版一致：

      · `PICSART_POOL_UPSTREAM_PROXY` 显式设置 -> 用它（客户自备代理时）
      · 否则 -> `trust_env=True`，沿用环境变量（保持和以前一致的行为，
        不给已有部署带来意外变化）
      · 授权请求永远直连（gate.py 已如此，理由见那里的注释）

    为什么不干脆默认直连：有的客户就是靠环境变量里的代理才能出网，
    默认忽略它们会让本来能用的部署突然坏掉。所以保留 env 作为默认，
    但提供显式开关。
    """
    global _CLIENT
    if _CLIENT is not None and not _CLIENT.is_closed:
        return _CLIENT
    async with _CLIENT_LOCK:
        if _CLIENT is None or _CLIENT.is_closed:
            limits = pool_limits()
            explicit = (os.environ.get("PICSART_POOL_UPSTREAM_PROXY") or "").strip()
            kw: dict[str, Any] = {
                "limits": httpx.Limits(**limits),
                "timeout": httpx.Timeout(90.0, connect=10.0),
                "follow_redirects": False,
            }
            if explicit:
                # 显式代理：同时关掉 trust_env，避免两套配置打架
                kw["proxy"] = explicit
                kw["trust_env"] = False
                log.info("upstream proxy: %s（显式配置）", explicit)
            else:
                kw["trust_env"] = True
                log.info("upstream proxy: 沿用环境变量（PICSART_POOL_UPSTREAM_PROXY 未设置）")
            _CLIENT = httpx.AsyncClient(**kw)
            log.info(
                "http pool: max_connections=%s keepalive=%s",
                limits["max_connections"], limits["max_keepalive_connections"],
            )
    return _CLIENT


async def aclose() -> None:
    """应用关闭时释放连接。"""
    global _CLIENT
    if _CLIENT is not None and not _CLIENT.is_closed:
        await _CLIENT.aclose()
    _CLIENT = None


# ---------------------------------------------------------------- 查询

async def fetch_credits(account: dict[str, Any]) -> dict[str, Any]:
    c = await client()
    r = await c.get(CREDITS, headers=headers_for(account))
    if r.status_code != 200:
        raise UpstreamError("credits_failed", r.status_code, _safe(r))
    return (_body(r) or {}).get("response") or {}


# 公开资料接口（用 OAuth token 可调，返回订阅档位）。
# 端点来自配置（`endpoints.user`），这里不再重复定义 —— 上一版在模块里
# 又写了一份 `USER_SHOW = "https://..."`，把 config 注入的值覆盖掉了。


async def fetch_tier(account: dict[str, Any]) -> str:
    """取该账号**真实订阅档位**（tier_id）。

    为什么要单独查：x-sub-package-id 必须和真实订阅对得上，否则就是谎报。
    之前是硬编码一个 pro 包 ID 发给所有号，等于假设池子里每个号都是 pro ——
    混进一个非 pro 号就会替它谎报订阅。

    返回值：'pro' / 'free' / 空串（查不到）。
    查不到时**不猜**，交给 package_id_for() 走"不发送该头"的分支。

    ⚠️ `granted=False`（订阅失效）也归为 'free' —— 这是有意的：
      没有有效订阅就是 free 档，两者在"能不能跑 wan3"这件事上没区别。
      不要试图拆成 'expired' 之类的细分值：那个区分来自推测，
      上游并没有这样定义，拆出来只会让判断逻辑变复杂而无收益。
    """
    uid = str(account.get("sub") or "").strip()
    if not uid:
        return ""
    try:
        c = await client()
        r = await c.get(
            USER_SHOW.format(uid=uid), headers=headers_for(account), timeout=20
        )
    except Exception:
        return ""
    if r.status_code != 200:
        return ""
    body = _body(r)
    if not isinstance(body, dict):
        return ""
    sub = body.get("subscription") or {}
    if not isinstance(sub, dict):
        return ""
    # granted=False（订阅失效）等同于 free：没有有效订阅就享受不到 pro 权限。
    if sub.get("granted") is False:
        return "free"
    return str(sub.get("tier_id") or "").strip().lower()


async def fetch_features_catalog(account: dict[str, Any]) -> dict[str, int]:
    """全量 feature -> credit_price。用于判断某档位是否标价 0。"""
    c = await client()
    r = await c.get(FEATURES, headers=headers_for(account), timeout=40)
    if r.status_code != 200:
        raise UpstreamError("features_failed", r.status_code, _safe(r))
    items = (_body(r) or {}).get("response") or []
    return {i["id"]: int(i.get("credit_price") or 0) for i in items if "id" in i}


async def fetch_wan_models(account: dict[str, Any]) -> list[dict[str, Any]]:
    url = f"{CATALOG}?mode=video&search=wan&include=schema,constraints,routing"
    c = await client()
    r = await c.get(url, headers=headers_for(account), timeout=30)
    if r.status_code != 200:
        raise UpstreamError("catalog_failed", r.status_code, _safe(r))
    return ((_body(r) or {}).get("response") or {}).get("models") or []


async def fetch_options(
    account: dict[str, Any],
    model: str = "",
    *,
    resolution: str = "",
    duration: int = 0,
) -> dict[str, Any]:
    model = model or settings.default_model
    resolution = resolution or settings.default_resolution
    duration = duration or settings.default_duration
    """查某档位的真实计价。

    返回示例（免费）：
      {"toolId":"text-to-video.wan3.0-video.720p","credits":0,"price":0,"usageAmount":5}
    收费（1080P 5s）：
      {"toolId":"text-to-video.wan3.0-video.1080p","credits":30,"price":6}
    """
    url = f"{GW}/workflows/{WAN3_WORKFLOW}/options"
    body = {"params": {"model": model, "resolution": resolution, "duration": duration}}
    c = await client()
    r = await c.post(url, headers=headers_for(account), json=body, timeout=25)
    if r.status_code not in SUCCESS:
        raise UpstreamError("options_failed", r.status_code, _safe(r))
    return (_body(r) or {}).get("response") or {}


async def is_free_now(
    account: dict[str, Any], resolution: str = "", duration: int = 5
) -> bool | None:
    """用 options 判断这个档位此刻是否 0 积分。None = 判断不了。"""
    try:
        o = await fetch_options(account, resolution=resolution, duration=duration)
    except Exception:
        return None
    if "credits" not in o:
        return None
    return int(o.get("credits") or 0) == 0


async def probe_feature(account: dict[str, Any], feature_id: str) -> bool | None:
    """用 pending-uses 探测该 feature 是否还有免费次数（不真扣）。

    返回：
      True  -> 有额度（200/201）
      False -> 明确没额度（400 + not-enough-available-feature-uses）
      None  -> 无法判断（网络错误 / 401 / 其他）——调用方不应据此屏蔽账号

    之前把异常一律当 False，导致可用账号被误标成「次数用尽」。
    """
    url = PENDING_USES
    if not url:
        # 未配置探测端点 = 这个功能没开（配置里 probe.enabled=False）。
        # 不猜端点，直接返回"无法判断"，让调用方不要据此屏蔽账号。
        return None
    try:
        c = await client()
        r = await c.post(
            url,
            headers=headers_for(account),
            json={"feature_id": feature_id, "used": 1},
            timeout=15,
        )
    except Exception:
        return None

    if r.status_code in (200, 201):
        return True

    if r.status_code == 400:
        reason = ""
        try:
            reason = str((_body(r) or {}).get("reason") or "")
        except Exception:
            reason = r.text
        if "not-enough-available-feature-uses" in reason:
            return False
        return None

    return None


# ---------------------------------------------------------------- 生成

# 主入口（已实测出片）：POST {GW}/workflows/wan/v3/video/submit
# 注意必须带 /gw-v2；不带前缀会落到旧路由，返回假的 429 concurrency_limit。
MODELS_WORKFLOW = "v1/models"


def clamp_duration(d: Any) -> int:
    """把 duration 收进合法范围。

    上游真实枚举：-1（自适应）、2~30。没有 0 和 1。
    """
    try:
        d = int(d)
    except (TypeError, ValueError):
        return 5
    if d == DURATION_AUTO:
        return DURATION_AUTO
    return max(DURATION_MIN, min(DURATION_MAX, d))


def validate_media(media: Any) -> list[dict[str, Any]]:
    """校验 media 数组，非法直接抛错（本地就拦住，不用等上游 400）。

    实测 schema：
      media: 1~20 个元素
      每个元素 {"type": <MEDIA_TYPES 之一>, "url": "https://..."}
    """
    if not media:
        return []
    if not isinstance(media, list):
        raise UpstreamError("invalid_request", 400, "media 必须是数组")
    if len(media) > MEDIA_MAX:
        raise UpstreamError("invalid_request", 400, f"media 最多 {MEDIA_MAX} 个")
    out = []
    for i, m in enumerate(media):
        if not isinstance(m, dict):
            raise UpstreamError("invalid_request", 400, f"media.{i} 必须是对象")
        t = str(m.get("type") or "")
        u = str(m.get("url") or "")
        if t not in MEDIA_TYPES:
            raise UpstreamError(
                "invalid_request", 400,
                f"media.{i}.type 必须是 {'/'.join(MEDIA_TYPES)} 之一，收到 {t!r}",
            )
        if not u:
            raise UpstreamError("invalid_request", 400, f"media.{i}.url 不能为空")
        out.append({"type": t, "url": u})
    return check_media_rules(out)


def check_media_rules(media: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """检查素材组合与数量上限（官方「素材组合」章节原文）。

    规则：
      1. first_frame / last_frame 与 reference_* / file / link 互斥
      2. file 与 link 二选一（但都可以和 reference_* 组合）
      3. reference_image ≤10、reference_video ≤5、reference_audio ≤5
      4. last_frame 必须配 first_frame
    """
    counts: dict[str, int] = {}
    for m in media:
        counts[m["type"]] = counts.get(m["type"], 0) + 1

    for t, n in counts.items():
        limit = MEDIA_LIMITS.get(t)
        if limit is not None and n > limit:
            raise UpstreamError(
                "invalid_request", 400, f"{t} 最多 {limit} 个，收到 {n} 个"
            )

    has_frame = any(t in counts for t in FRAME_TYPES)
    # 注意：file / link 也算「非帧」素材，官方明确它们与帧互斥
    has_non_frame = any(
        t != "first_frame" and t != "last_frame" for t in counts
    )
    if has_frame and has_non_frame:
        raise UpstreamError(
            "invalid_request", 400,
            "首尾帧（first_frame / last_frame）不能和其他任何素材"
            "（reference_image / video / audio、file、link）混用",
        )

    if counts.get("last_frame") and not counts.get("first_frame"):
        raise UpstreamError(
            "invalid_request", 400, "last_frame 必须配合 first_frame 使用"
        )

    if counts.get("file") and counts.get("link"):
        raise UpstreamError(
            "invalid_request", 400, "file 和 link 只能二选一"
        )
    return media


def apply_media_tags(prompt: str, media: list[dict[str, Any]]) -> str:
    """把 media 的命名补进 prompt。

    为什么要做这件事：上游 media 元素**没有名字或编号字段**
    （name/alias/index/label 都实测被忽略），模型只能靠数组顺序
    去对应"第几张图"。而每条 media 都可能在 prefix 里占了位置。

    做法：给每个元素算一个稳定编号（参考图叫「图片1/2」，视频「视频1」，
    音频「音频1」，帧不加编号），在 prompt 末尾追加一行图例。
    这样"用@某张图"就有确定含义，模型也能精确对上号。
    """
    if not media or not prompt:
        return prompt

    labels: list[str] = []
    seen: dict[str, int] = {}
    for m in media:
        t = m["type"]
        if t in FRAME_TYPES:
            labels.append("首帧" if t == "first_frame" else "末帧")
            continue
        seen[t] = seen.get(t, 0) + 1
        n = seen[t]
        base = {
            "reference_image": "图片",
            "reference_video": "视频",
            "reference_audio": "音频",
            "file": "文件",
            "link": "链接",
        }.get(t, "素材")
        labels.append(f"{base}{n}")

    legend = "；".join(
        f"{lab} = 第{i + 1}个素材" for i, lab in enumerate(labels)
    )
    return f"{prompt}\n\n[素材顺序：{legend}]"


def media_label(mtype: str, n: int) -> str:
    """上游官方的素材引用名：图N / 视频N / 音频N。

    来源：阿里云百炼《万相3.0 视频生成 Prompt 指南》原文——
    「素材按上传顺序编号，图片、视频、音频分别计数：第 1 张图是图1、
      第 2 张图是图2，视频、音频同理写视频N、音频N。
      因此图1与视频1可以同时存在。」

    注意：是「图」，不是「图片」。我一开始自造成「图片1」，模型不认。
    """
    base = {
        "reference_image": "图",
        "reference_video": "视频",
        "reference_audio": "音频",
        "file": "文件",
        "link": "链接",
    }.get(mtype)
    if base is None:            # 帧不加编号，官方用「图1」指首帧
        return ""
    return f"{base}{n}"


def label_media(media: list[dict[str, Any]]) -> dict[int, str]:
    """给每个 media 元素算出官方引用名。帧返回空串（不参与编号计数）。"""
    out: dict[int, str] = {}
    seen: dict[str, int] = {}
    for i, m in enumerate(media):
        t = m["type"]
        if t in FRAME_TYPES:
            out[i] = ""
            continue
        seen[t] = seen.get(t, 0) + 1
        out[i] = media_label(t, seen[t])
    return out


def resolve_mentions(
    prompt: str, media: list[dict[str, Any]], named: dict[str, int]
) -> str:
    """把 @自定义名 换成上游官方的 图N / 视频N / 音频N。

    上游 schema 里 media 元素**没有 name 字段**（实测 name/alias/label/index
    全部被忽略），但**模型侧的 prompt 规范支持用 图N/视频N/音频N 指代**。
    所以正确做法不是自造语法，而是：

      @主角（第 1 张图） → 图1

    编号严格按 media 数组里同类素材的出现顺序，和官方"按上传顺序编号"一致。
    """
    if not media or not prompt or not named:
        return prompt
    labels = label_media(media)
    for name in sorted(named, key=len, reverse=True):
        idx = named[name]
        lab = labels.get(idx, "")
        if lab:
            prompt = prompt.replace(f"@{name}", lab)
    return prompt


def build_media_legend(media: list[dict[str, Any]], named: dict[str, int]) -> str:
    """素材清单：告诉模型哪个名字对应哪张图。只在用了 @名字 时附上。"""
    if not media or not named:
        return ""
    labels = label_media(media)
    rev = {v: k for k, v in named.items()}
    parts = []
    for i in sorted(labels):
        lab = labels[i]
        if not lab:
            continue
        nm = rev.get(i) or _name_of(named, i)
        parts.append(f"{lab}是{nm}" if nm else lab)
    return "、".join(parts)


def _name_of(named: dict[str, int], idx: int) -> str:
    for k, v in named.items():
        if v == idx:
            return k
    return ""


def _clean(params: dict[str, Any]) -> dict[str, Any]:
    """只留 submit 认识的字段，剥掉内部标记 _xxx。

    认识的字段（从上游 400 报错逐个反推确认）：
      model / prompt / negative_prompt / resolution / ratio / duration /
      audio / seed / media / enable_thinking / watermark
    注意字段名是 ratio，不是 aspect_ratio —— 后者不报错但会被忽略。
    """
    return {k: v for k, v in params.items() if k in ALLOWED_PARAMS and v is not None}


def _raise_for(r: httpx.Response, data: dict[str, Any]) -> None:
    """把上游状态码翻译成带语义的异常。

    顺序有讲究：先判 Cloudflare HTML。CF 限流也会返回 429，
    但它不是"账号并发满了"——混为一谈会让调度器把好账号冷却掉。
    """
    # Cloudflare 介入时返回 HTML（不是 JSON），状态码可能是 403/429/503/520 等
    if isinstance(data, dict) and "_raw" in data:
        raise UpstreamError("upstream_blocked", r.status_code, data)
    if r.status_code == 429:
        raise UpstreamError("concurrency_limit", 429, data.get("details"),
                            retry_after=_retry_after(r))
    if r.status_code == 401:
        raise UpstreamError("unauthorized", 401, data)
    if r.status_code == 402:
        raise UpstreamError("insufficient_credits", 402, data)
    if r.status_code == 400:
        raise UpstreamError(data.get("reason") or "invalid_request", 400, data)


async def submit(account: dict[str, Any], params: dict[str, Any]) -> tuple[str, str]:
    """提交任务，返回 (task_id, workflow)。

    参数处理有三条硬约束，全部实测：
      1) resolution 必须显式传 —— 缺了服务端按 1080P 计价直接 402
      2) duration 只有 -1 或 2~30，没有 0/1
      3) media 每个元素必须 {type, url}，type 在 MEDIA_TYPES 里
    """
    body_params = _clean(params)
    body_params.setdefault("model", settings.default_model)
    body_params.setdefault("resolution", settings.default_resolution)
    body_params.setdefault("ratio", settings.default_ratio)

    if "duration" in body_params:
        body_params["duration"] = clamp_duration(body_params["duration"])
    else:
        body_params["duration"] = 5

    if "media" in body_params:
        media = validate_media(body_params["media"])
        if media:
            body_params["media"] = media
        else:
            body_params.pop("media", None)

    # 有参考视频时，上游只允许 -1/5/10/15
    if body_params.get("media") and body_params["duration"] not in DURATION_WITH_VIDEO_REF:
        if any(m["type"] == "reference_video" for m in body_params["media"]):
            body_params["duration"] = min(
                (d for d in DURATION_WITH_VIDEO_REF if d > 0),
                key=lambda d: abs(d - body_params["duration"]),
            )

    # 把 @名字 解析成上游官方的 图N/视频N/音频N，并附素材清单
    named = params.get("_named_media") or {}
    if body_params.get("prompt") and body_params.get("media") and named:
        prompt = resolve_mentions(body_params["prompt"], body_params["media"], named)
        legend = build_media_legend(body_params["media"], named)
        if legend:
            prompt = f"{prompt}\n\n【素材对应】{legend}"
        body_params["prompt"] = prompt

    # prompt 和 media 至少要有一个，否则上游会拒
    if not body_params.get("prompt") and not body_params.get("media"):
        raise UpstreamError("invalid_request", 400, "prompt 和 media 至少要有一个")

    url = f"{GW}/workflows/{WAN3_WORKFLOW}/submit"
    c = await client()
    # 带上网页端同款的 options.drive 落盘配置：
    # 缺了它上游可能把请求当裸调用、走低优先级队列。
    body_params.setdefault("options", drive_options(body_params))
    r = await c.post(url, headers=headers_for(account), json={"params": body_params}, timeout=90)
    data = _body(r)

    _raise_for(r, data)
    if r.status_code not in SUCCESS or data.get("status") != "success":
        raise UpstreamError(data.get("reason", "submit_failed"), r.status_code, data)

    tid = (data.get("response") or {}).get("id")
    if not tid:
        raise UpstreamError("invalid_response", r.status_code, data)
    return tid, WAN3_WORKFLOW


async def _slot_occupied(account: dict[str, Any]) -> bool:
    """探测该账号的并发名额是否被占用（旧接口，保留兼容）。

    新代码请用 probe_slot_free() —— 它区分"确定被占"和"探不了"，
    而这个函数把两者都返回 False，会让人误以为名额空着。
    """
    free = await probe_slot_free(account, duration=5)
    return free is False


async def probe_slot_free(
    account: dict[str, Any],
    *,
    resolution: str = "",
    duration: int = 0,
) -> bool | None:
    resolution = resolution or settings.default_resolution
    duration = duration or 2
    """问上游：这个号的并发名额现在空着吗？

    用 options 的 rateLimitVerdict —— 只判定、不真提交任务，
    所以**无副作用**：不会白开任务、不占名额、不扣积分。

    三态，这点很重要：
      True  -> overLimit == False，明确空闲 → 可以派任务
      False -> overLimit == True，明确被占 → 别派
      None  -> 探不了（网络错误 / 429 / 上游抽风）→ **不要据此下任何结论**

    以前只返回 bool，把"探不了"也当成 False（空闲），于是调度器
    会往一个名额其实被占着的号上派任务，白撞一次 429。
    """
    try:
        o = await fetch_options(
            account, resolution=resolution, duration=duration
        )
    except Exception:
        return None
    if not isinstance(o, dict):
        return None
    v = o.get("rateLimitVerdict")
    if not isinstance(v, dict) or "overLimit" not in v:
        return None
    return not bool(v.get("overLimit"))


# 每个任务的 ETag 缓存：(account_id, task_id) -> etag
# 网页端就是这么做的：第一次拿 200 + etag，之后带 if-none-match，
# 没变化时上游返回 304（无 body，约 350 字节）。
# 100 个号轮询时这个差别很可观。
_ETAGS: dict[tuple[int, str], str] = {}


async def get_result(
    account: dict[str, Any], task_id: str, workflow: str | None = None
) -> dict[str, Any]:
    """查任务状态。带 ETag 条件请求，未变化时上游返回 304。

    304 时返回 {"_not_modified": True}，调用方沿用上次状态。
    这也是「上游确实没动静」的准确信号 —— 比比对 updated 时间戳可靠，
    因为上游在 ACCEPTED 阶段 updated 本来就不变。

    ⚠️ workflow 的默认值**不能**写成 `= WAN3_WORKFLOW`（2026-10-25 修）：
    Python 默认参数在 import 时求值，会把注入前的空串固化进签名，
    导致运行时激活后仍拼出 `/workflows//{id}/result`（双斜杠 → 上游 404
    → 被当成 task_not_found → 任务卡死）。改用 None + 函数体内兜底。
    """
    workflow = workflow or WAN3_WORKFLOW
    url = f"{GW}/workflows/{workflow}/{task_id}/result"
    c = await client()
    h = headers_for(account)
    key = (account.get("id") or 0, task_id)
    etag = _ETAGS.get(key)
    if etag:
        h["if-none-match"] = etag

    r = await c.get(url, headers=h, timeout=45)

    if r.status_code == 304:
        return {"_not_modified": True}
    if r.status_code == 404:
        _ETAGS.pop(key, None)
        raise UpstreamError("task_not_found", 404, None)
    if r.status_code == 429:
        raise UpstreamError("concurrency_limit", 429, None, retry_after=_retry_after(r))

    new_etag = r.headers.get("etag")
    if new_etag:
        _ETAGS[key] = new_etag
    else:
        _ETAGS.pop(key, None)

    data = _body(r)
    if r.status_code not in SUCCESS:
        reason = "result_failed"
        if isinstance(data, dict):
            reason = data.get("reason") or reason
        raise UpstreamError(reason, r.status_code, data)
    resp = (data.get("response") or {}) if isinstance(data, dict) else {}
    return resp


async def stream_submit(
    account: dict[str, Any],
    params: dict[str, Any],
    *,
    workflow: str | None = None,
    timeout: float | None = None,
    on_progress: Any = None,
) -> dict[str, Any]:
    """**提交 + 流式跟随**，一个请求走到底。

    实测端点语义（这点踩过坑，必须写清楚）：

        POST {GW}/workflows/{workflow}/stream     ← 提交并流式返回
        POST {GW}/workflows/{workflow}/{id}/stream ← 不存在（404 workflow_not_found）

    也就是说 `/stream` **不接受 task_id**，它自己就是一个"提交"动作：
    参数和 submit 完全一样，但响应不是 201+id，而是 SSE 流，
    最后用 `task.completed` 事件把 result 直接给你。

    所以它替代的是「submit + 轮询」两步，不是「只替代轮询」。
    拿不到 task_id 是正常的 —— 上游 id 只在流内部出现。

    协议来自前端 SDK 的 decodeSSE/readSSE：
      · Accept: text/event-stream
      · 帧以空行分隔，每帧取 `data:` 后的 JSON
      · `data: [DONE]` 结束
      · 事件 type: event.* / task.partial-result / task.completed / task.failed
    """
    c = await client()
    workflow = workflow or WAN3_WORKFLOW
    url = f"{GW}/workflows/{workflow}/stream"
    h = headers_for(account)
    h["accept"] = "text/event-stream"

    body_params = _clean(params)
    body_params.setdefault("model", settings.default_model)
    body_params.setdefault("resolution", settings.default_resolution)
    body_params.setdefault("ratio", settings.default_ratio)
    if "duration" in body_params:
        body_params["duration"] = clamp_duration(body_params["duration"])
    else:
        body_params["duration"] = 5
    if "media" in body_params:
        media = validate_media(body_params["media"])
        if media:
            body_params["media"] = media
        else:
            body_params.pop("media", None)
    named = params.get("_named_media") or {}
    if body_params.get("prompt") and body_params.get("media") and named:
        prompt = resolve_mentions(body_params["prompt"], body_params["media"], named)
        legend = build_media_legend(body_params["media"], named)
        if legend:
            prompt = f"{prompt}\n\n【素材对应】{legend}"
        body_params["prompt"] = prompt

    deadline = asyncio.get_event_loop().time() + (timeout or settings.task_timeout)

    try:
        body_params.setdefault("options", drive_options(body_params))
        async with c.stream(
            "POST", url, headers=h, json={"params": body_params},
            timeout=httpx.Timeout(None, connect=15.0),
        ) as r:
            if r.status_code == 429:
                raise UpstreamError("concurrency_limit", 429, None,
                                    retry_after=_retry_after(r))
            if r.status_code == 401:
                raise UpstreamError("unauthorized", 401, None)
            if r.status_code == 402:
                raise UpstreamError("insufficient_credits", 402, None)
            if r.status_code == 400:
                raw = await r.aread()
                raise UpstreamError(
                    "invalid_request", 400, _safe_bytes(raw))
            # Cloudflare 网关超时。含义很特殊：
            #   源站收到了请求（任务可能已经创建并通过），但 100 秒内没回响应头，
            #   CF 就把连接掐了。
            # 对我们的后果是：**任务可能已经在上游跑着，但我们没拿到 task_id**。
            # 不能简单重试（会重复创建、重复占名额），也不能当"没发生"。
            if r.status_code == 524:
                raw = await r.aread()
                raise UpstreamError("cf_timeout", 524, _safe_bytes(raw))
            if r.status_code not in SUCCESS:
                raw = await r.aread()
                raise UpstreamError("stream_failed", r.status_code, _safe_bytes(raw))

            buf = ""
            result_payload: dict[str, Any] = {}
            upstream_id = ""
            async for chunk in r.aiter_text():
                if asyncio.get_event_loop().time() > deadline:
                    return {"status": "TIMEOUT", "url": None,
                            "error": "超过任务生命周期上限"}
                buf += chunk
                while True:
                    idx = _find_frame_end(buf)
                    if idx < 0:
                        break
                    frame, buf = buf[:idx], buf[idx:]
                    evt = _parse_sse_frame(frame)
                    if evt is None:
                        continue
                    if evt == "[DONE]":
                        return _finish_stream(result_payload, upstream_id)
                    etype = str(evt.get("type") or "")
                    # 流里可能带 id，尽量捞出来给上层记录
                    if not upstream_id:
                        for k in ("id", "taskId", "task_id"):
                            v = evt.get(k)
                            if isinstance(v, str) and v:
                                upstream_id = v
                                break
                        if not upstream_id:
                            res = evt.get("result")
                            if isinstance(res, dict):
                                for k in ("id", "taskId", "task_id"):
                                    v = res.get(k)
                                    if isinstance(v, str) and v:
                                        upstream_id = v
                                        break
                    if etype == "task.partial-result":
                        pct = ((evt.get("result") or {}).get("progress") or {}).get("percent")
                        if on_progress and pct is not None:
                            try:
                                on_progress(int(pct), "IN_PROGRESS")
                            except Exception:
                                pass
                    elif etype == "task.completed":
                        result_payload = evt
                        if on_progress:
                            try:
                                on_progress(100, "COMPLETED")
                            except Exception:
                                pass
                        return _finish_stream(result_payload, upstream_id)
                    elif etype == "task.failed":
                        return {
                            "status": "FAILED", "url": None,
                            "error": str(evt.get("result") or evt)[:500],
                            "task_id": upstream_id,
                            "raw": evt,
                        }
            return _finish_stream(result_payload, upstream_id)
    except (httpx.RemoteProtocolError, httpx.ReadError) as e:
        raise UpstreamError("stream_broken", 0, str(e)[:200]) from e
    except httpx.TimeoutException as e:
        raise UpstreamError("stream_timeout", 0, str(e)[:200]) from e


def _finish_stream(payload: dict[str, Any], upstream_id: str) -> dict[str, Any]:
    """把 task.completed 事件整理成统一结构。"""
    if not payload:
        return {
            "status": "FAILED", "url": None, "task_id": upstream_id,
            "error": "SSE 流结束但没有收到 task.completed",
        }
    res = payload.get("result") or {}
    usage = payload.get("usage") or {}
    return {
        "status": "COMPLETED",
        "url": res.get("url") if isinstance(res, dict) else None,
        "mime": (res or {}).get("mimeType") if isinstance(res, dict) else None,
        "credits_used": usage.get("credits", 0),
        "percent": 100,
        "task_id": upstream_id,
        "raw": payload,
    }


def _find_frame_end(buf: str) -> int:
    """找 SSE 帧边界：\\n\\n / \\r\\r / \\r\\n\\r\\n。返回切分位置（含分隔符）。"""
    for i in range(len(buf) - 1):
        if buf[i] == "\n" and buf[i + 1] == "\n":
            return i + 2
        if buf[i] == "\r" and buf[i + 1] == "\r":
            return i + 2
        if (
            buf[i] == "\r" and i + 3 < len(buf)
            and buf[i + 1] == "\n" and buf[i + 2] == "\r" and buf[i + 3] == "\n"
        ):
            return i + 4
    return -1


def _parse_sse_frame(frame: str) -> Any:
    """从一帧里取出 data: 后面的 JSON。没有 data 行返回 None。"""
    for line in frame.split("\n"):
        line = line.rstrip("\r")
        if not line.startswith("data:"):
            continue
        raw = line[5:].strip()
        if not raw:
            continue
        if raw == "[DONE]":
            return "[DONE]"
        try:
            return json.loads(raw)
        except ValueError:
            # 只吞 JSON 解析错误。之前写成 except Exception，
            # 把 NameError（忘了 import json）也吞了，导致所有帧静默返回 None ——
            # 这种静默失败比崩溃难查得多。
            return None
    return None


def _finish(payload: dict[str, Any], task_id: str) -> dict[str, Any]:
    """把 task.completed 事件整理成统一结构。"""
    if not payload:
        return {
            "status": "FAILED", "url": None,
            "error": "SSE 流结束但没有收到 task.completed",
            "task_id": task_id,
        }
    res = payload.get("result") or {}
    usage = payload.get("usage") or {}
    return {
        "status": "COMPLETED",
        "url": res.get("url") if isinstance(res, dict) else None,
        "mime": (res or {}).get("mimeType") if isinstance(res, dict) else None,
        "credits_used": usage.get("credits", 0),
        "percent": 100,
        "task_id": task_id,
        "raw": payload,
    }


def _safe_bytes(b: bytes) -> Any:
    try:
        return json.loads(b.decode("utf-8", "replace"))
    except Exception:
        return b[:300].decode("utf-8", "replace")


async def wait_result(
    account: dict[str, Any],
    task_id: str,
    *,
    workflow: str | None = None,
    poll_interval: float | None = None,
    timeout: float | None = None,
    on_progress: Any = None,
    estimate_total: float | None = None,
) -> dict[str, Any]:
    """轮询到任务完成。

    **实测时序（已经和网页端做过同参数对比，两边一致）**：

        submit 201
          ↓
        ACCEPTED + progress=null      ← 上游排队，实测 15~23 分钟，updated 完全不变
          ↓
        IN_PROGRESS progress 77%+     ← 真正开始出片
          ↓
        COMPLETED                     ← 出片本身只要 30~60 秒

    对比数据（同账号同参数）：
      · 我们 API：总 1157s，其中排队 1125s，生成 32s
      · 网页端 ：总 1377s，其中排队 1335s，生成 42s

    上游的 status 字段本身就区分了两个阶段，直接透出就行，不需要造数：

      ACCEPTED     = 已受理，**等待调度**（progress 为 null）
      IN_PROGRESS  = **正在执行**，progress.percent 是真实进度
      COMPLETED    = 完成

    所以 on_progress 会把（真实 percent, 上游 status）原样传上去，
    由上层决定怎么展示。ACCEPTED 阶段的 percent 恒为 0 —— 因为上游就是没给。
    想知道"等了多久"，看时间戳；不需要编一个百分比出来。

    对比数据（同账号同参数）：
      · 我们 API：总 1157s，其中等待调度 1125s，执行 32s
      · 网页端 ：总 1377s，等待调度 1335s，执行 42s

    网页端那个一路爬升的百分比是假的 —— 它拿「已用时间 ÷ 估算总时长」倒推，
    所以会出现"显示 87%、上游实际 77%、最后完成 85%"这种对不上的情况。
    我们不这么干。

    404 = 刚提交还没落库，短暂重试即可。
    """
    # 同 get_result：默认值不能固化 WAN3_WORKFLOW（import 时求值，注入前是空串）
    workflow = workflow or WAN3_WORKFLOW
    poll = poll_interval or settings.poll_interval
    start = asyncio.get_event_loop().time()
    deadline = start + (timeout or settings.task_timeout)
    notfound_since: float | None = None
    last_resp: dict[str, Any] = {}
    last_status = ""
    # ACCEPTED 卡死检测的状态。**必须在循环外初始化** ——
    # 放进循环体内每轮都会重置，"第一次看到 ACCEPTED" 永远记不住，
    # 卡死检测等于不存在（第一版就犯了这个错）。
    #
    # 判据用上游的 `updated` 字段（它自己的心跳），不是 elapsed：
    # elapsed 会随正常排队增长，拿它当判据必然误杀；
    # updated 不变才说明上游侧这个任务彻底静止。
    accepted_since: float | None = None
    accepted_updated = ""
    ACCEPTED_STALL_MAX = 900.0     # 15 分钟：> 实测正常排队时长（15~23 分钟）
    # 连续撞 429 的次数，用于指数退避。成功一次即清零。
    rate_429 = 0

    while True:
        now = asyncio.get_event_loop().time()
        if now > deadline:
            return {"status": "TIMEOUT", "url": None, "task_id": task_id}

        try:
            resp = await get_result(account, task_id, workflow)
        except UpstreamError as e:
            if e.reason == "task_not_found":
                if notfound_since is None:
                    notfound_since = now
                elif now - notfound_since > 120:
                    return {
                        "status": "FAILED",
                        "url": None,
                        "error": "上游查不到该任务（可能已被清理）",
                        "task_id": task_id,
                    }
                await asyncio.sleep(poll)
                continue
            # ⚠️ 轮询时撞 429 必须**继续等**，不能把整个任务判失败
            # （2026-10-25 修，对齐 FIXES.md 的 P2-1）。
            #
            # 为什么：查询接口也在 Cloudflare 后面，号池跑满时
            # GET .../result 会被限流。这是**常态**，不是任务出错。
            # 以前这里直接 raise → 整条任务 FAILED，而视频其实还在上游跑着，
            # 客户白白损失一次生成（还可能重复提交、白占名额）。
            #
            # 用指数退避而不是固定 poll：连续 429 说明我们打太快了，
            # 固定间隔会一直撞、还会延长 Cloudflare 的封禁窗口
            # （authlimit.py 顶部记的 1015 教训）。
            if e.reason == "concurrency_limit" or e.status == 429:
                rate_429 += 1
                # 优先听上游的 retry-after。它比我们自己拍的数字权威：
                # Cloudflare 说 60 秒就是 60 秒，早打一次会延长封禁窗口。
                if e.retry_after:
                    back = e.retry_after
                else:
                    back = min(poll * (2 ** min(rate_429, 4)), 60.0)
                log.warning(
                    "轮询 %s 撞 429（第 %d 次），退避 %.0fs 后继续%s",
                    task_id, rate_429, back,
                    "（上游要求）" if e.retry_after else "（本地估算）",
                )
                await asyncio.sleep(back)
                continue
            raise

        rate_429 = 0        # 成功一次就重置退避

        notfound_since = None

        # 304：上游内容没变（状态没推进）。沿用上次的状态继续等。
        if isinstance(resp, dict) and resp.get("_not_modified"):
            resp = last_resp
        else:
            last_resp = resp

        status = (resp.get("status") or "").upper()
        progress = resp.get("progress") or {}
        real_pct = progress.get("percent")
        updated = str(resp.get("updated") or "")

        # 原样上报真实状态和真实进度。ACCEPTED 阶段 percent 就是 0/None。
        if on_progress and status != last_status:
            try:
                on_progress(int(real_pct or 0), status)
            except Exception:
                pass
            last_status = status
        elif on_progress and real_pct is not None:
            try:
                on_progress(int(real_pct), status)
            except Exception:
                pass

        # ACCEPTED 且长时间不动 → 判定卡死。
        # 用 updated 字段而不是 elapsed：updated 是上游自己的心跳，
        # 它不变说明上游侧这个任务彻底静止；elapsed 会随正常排队增长，
        # 拿它当判据必然误杀。
        if status == "ACCEPTED":
            if accepted_since is None or updated != accepted_updated:
                accepted_since = now
                accepted_updated = updated
            elif now - accepted_since > ACCEPTED_STALL_MAX:
                return {
                    "status": "STALLED",
                    "url": None,
                    "task_id": task_id,
                    "error": (
                        f"上游任务 {task_id} 卡在 ACCEPTED 已 "
                        f"{int(now - accepted_since)}s（updated 无变化），"
                        "判定未被调度"
                    ),
                    "upstream_status": status,
                    "raw": resp,
                }
        else:
            accepted_since = None
            accepted_updated = ""

        # 注意：**不要**在 ACCEPTED 阶段做"卡死判定"。
        #
        # 曾经写过：ACCEPTED 停 60 秒就查 options 的 overLimit，占着就判 BLOCKED。
        # 这是错的 —— overLimit=true 恰恰可能是**我们这个任务自己**占的名额，
        # 等于用自己的任务把自己判死。

        if status == "COMPLETED":
            result = resp.get("result") or {}
            usage = resp.get("usage") or {}
            return {
                "status": "COMPLETED",
                "url": result.get("url") if isinstance(result, dict) else None,
                "mime": (result or {}).get("mimeType") if isinstance(result, dict) else None,
                "drive_file": (result or {}).get("driveFile") if isinstance(result, dict) else None,
                "credits_used": usage.get("credits", 0),
                "percent": real_pct,
                "task_id": task_id,
                "upstream_status": status,
                "raw": resp,
            }
        if status == "FAILED":
            return {
                "status": "FAILED",
                "url": None,
                "error": resp.get("result"),
                "task_id": task_id,
                "upstream_status": status,
                "raw": resp,
            }
        await asyncio.sleep(poll)


def _safe(r: httpx.Response) -> Any:
    try:
        return r.json()
    except Exception:
        return r.text[:300]


def _body(r: httpx.Response) -> Any:
    """安全解析响应体。

    api.picsart.com 前面有 Cloudflare，被限流/拦截时返回 HTML，
    直接 r.json() 会抛 "Expecting value: line 1 column 1 (char 0)"，
    把真实原因（429 限流）完全掩盖成一句看不懂的解析错误。
    """
    if not r.content:
        return {}
    try:
        return r.json()
    except Exception:
        return {"_raw": r.text[:300], "_status": r.status_code}
