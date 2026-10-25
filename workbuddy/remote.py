"""把站点知识从源码搬进「服务端加密下发的配置」。

为什么需要这一层
----------------
`app/config.py` 里现在硬编码了整套上游契约：`api.picsart.com`、`gw-v2` 前缀、
`x-touchpoint` 的确切值、`x-sub-package-id` 的包名、免费档矩阵、duration 枚举、
素材类型与上限…… 加起来 60+ 处。

自有服务器部署时这无所谓（源码不出去）。**但客户用 WorkBuddy 发布时，
源码就在客户机器上** —— 谁都能看到「哦，原来免费档的诀窍是这两个头」。

而 Go 客户端早就解决了这件事：`client/config.go` 文件头写着
「端点、头名与头值、参数校验规则、定价矩阵、媒体类型一律不写死在代码里，
全部由服务端下发的加密配置注入」——二进制里零站点知识。

这个模块就是把那套机制搬给 Python 版。**服务端一行都不用改**，
协议完全相同（`/config` 返回 `{version, body, salt, sig_hint}`）。

注入时机是整件事的关键
----------------------
`app/upstream.py` 写的是 `from .config import GW` —— 这会在 **import 那一刻**
把值绑进 upstream 模块自己的命名空间。之后再改 `app.config.GW` 是改不到的。

所以必须在 `app.main` 被 import **之前**完成注入：

    import app.config                 # 1. 只加载模块，不碰上游
    remote.apply(app.config)          # 2. 把解密值写进它的命名空间
    import app.main                   # 3. 此时 `from .config import GW`
                                      #    才拿到真值

这个顺序由 `workbuddy/entry.py` 保证。代价是入口必须唯一 ——
不能用 `uvicorn app.main:app` 直接起（那会跳过注入）。

默认值必须被清空
----------------
只做「注入」不够：没激活时 `app/config.py` 里那些硬编码常量仍然是真值，
客户读源码照样能看到。所以 `app/config.py` 的站点常量默认改成空值/占位，
真值只在解密后才有。这笔账算得清：

  · 未激活 → 常量是空的 → 读源码也拿不到端点
  · 已激活 → 注入真值 → 一切照常

未激活状态下不会有任何代码用到这些常量，因为授权门在前面拦着。
"""
from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger("picsart-pool.remote")

# 解密出的配置，供诊断/自省用。
_active: dict[str, Any] = {}


def config_get(path: str, default=None):
    """按点号路径读配置，如 `config_get("vars.GW")`。"""
    cur: Any = _active
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


def _resolve(tpl: str, var: dict[str, str]) -> str:
    """展开端点模板里的 `{API}` / `{GW}` / `{AUTH}` / `{SELF}`。

    服务端下发的端点长这样：`"{GW}/workflows/{wf}/submit"`。
    必须在这里展开成完整 URL —— 上一版直接把它赋给 `GW`，结果
    `AUTHORIZE` 等 8 个常量拿到的是带花括号的模板原文，
    请求会发到字面量 `{AUTH}/oauth2/authorize`（DNS 失败）。

    `{wf}` / `{id}` / `{uid}` 这类**运行时**占位符必须保留 —— 它们由
    调用点用 str.format 填，提前展开会 KeyError。
    """
    if not tpl:
        return ""
    out = tpl
    for k, v in var.items():
        out = out.replace("{" + k + "}", v)
    return out


def apply(mod, cfg: dict[str, Any]) -> None:
    """把解密后的配置写进 `app.config` 的模块命名空间。

    `mod` 是 `app.config` 模块对象。写的是**模块属性**，不是 Settings 实例 ——
    因为所有调用点读的都是 `from .config import GW` 这种模块级名字。
    """
    global _active
    _active = cfg

    var: dict[str, str] = cfg.get("vars") or {}
    raw_eps: dict[str, str] = cfg.get("endpoints") or {}
    eps = {k: _resolve(v, var) for k, v in raw_eps.items()}
    hdr: dict[str, Any] = cfg.get("headers") or {}
    rules: dict[str, Any] = cfg.get("rules") or {}
    sched: dict[str, Any] = cfg.get("schedule") or {}
    probe: dict[str, Any] = cfg.get("probe") or {}

    # ---- 基址 ----
    mod.API_BASE = var.get("API", "")
    mod.GW = var.get("GW", "")
    mod.AUTH_BASE = var.get("AUTH", "")
    mod.OIDC_DISCOVERY = (
        f"{mod.AUTH_BASE}/.well-known/openid-configuration" if mod.AUTH_BASE else ""
    )

    # ---- 具体端点（模板已展开）----
    mod.CATALOG = eps.get("catalog", "")
    mod.CREDITS = eps.get("credits", "")
    mod.FEATURES = eps.get("features", "")
    mod.ME = eps.get("me", "")

    mod.AUTHORIZE = eps.get("authorize", "")
    mod.TOKEN = eps.get("token", "")
    mod.REGISTER = eps.get("register", "")
    mod.REVOKE = eps.get("revoke", "")
    mod.REVOKE_LOGOUT = eps.get("revoke_logout", "")
    mod.USERINFO = eps.get("userinfo", "")
    mod.USER_SHOW = eps.get("user", "")
    mod.PENDING_USES = _resolve(probe.get("endpoint", ""), var)

    # ---- 工作流名 ----
    wfs: dict[str, str] = cfg.get("workflows") or {}
    mod.WAN3_WORKFLOW = wfs.get(cfg.get("default_workflow") or "wan3", "")
    mod.MODELS_WORKFLOW = "v1/models"

    # ---- 请求头契约 ----
    mod.TOUCHPOINT = hdr.get("touchpoint", "")
    mod.TOUCHPOINT_REFERRER = hdr.get("touchpoint_referrer", "")
    mod.PACKAGE_ID_BY_TIER = rules.get("package_by_tier") or {}
    mod.UA = hdr.get("ua", "")
    mod.BROWSER_HEADERS = hdr.get("browser") or {}
    mod.STATIC_HEADERS = hdr.get("static") or {}
    mod.DRIVE_HEADERS = hdr.get("drive") or {}
    # 头**名**（不是值）：`x-touchpoint` 这类名字本身也是知识
    mod.H_ACCEPT = hdr.get("accept_header", "")
    mod.H_DEVICE = hdr.get("device_header", "")
    mod.H_PACKAGE = hdr.get("package_header", "")
    mod.H_TOUCH = hdr.get("touch_header", "")
    mod.H_TOUCH_REF = hdr.get("touch_ref_header", "")
    mod.H_UA = hdr.get("ua_header", "")
    mod.H_APP_AUTH = hdr.get("app_auth_header", "")
    mod.H_AUTH = hdr.get("auth_header", "")
    mod.AUTH_PREFIX = hdr.get("auth_prefix", "")
    mod.H_COOKIE = hdr.get("cookie_header", "")

    # ---- 参数规则 / 定价矩阵 ----
    mod.FREE_RESOLUTIONS = tuple(rules.get("free_resolutions") or ())
    mod.PAID_RESOLUTIONS = tuple(rules.get("paid_resolutions") or ())
    mod.RATIOS = tuple(rules.get("ratios") or ())
    dur = rules.get("duration") or {}
    mod.DURATION_MIN = int(dur.get("min") or 2)
    mod.DURATION_MAX = int(dur.get("max") or 30)
    mod.DURATION_AUTO = int(dur.get("auto") or -1)
    mod.DURATIONS = tuple(range(mod.DURATION_MIN, mod.DURATION_MAX + 1))
    mod.DURATION_WITH_VIDEO_REF = tuple(dur.get("with_video_ref") or ())
    mod.MEDIA_TYPES = tuple(rules.get("media_types") or ())
    mod.MEDIA_MAX = int(rules.get("media_max") or 20)
    mod.MEDIA_LIMITS = rules.get("media_limits") or {}
    mod.FRAME_TYPES = tuple(rules.get("frame_types") or ())
    mod.FRAME_EXCLUSIVE = bool(rules.get("frame_exclusive"))
    mod.MEDIA_KINDS = rules.get("media_kinds") or {}
    mod.WORKFLOW_KIND = rules.get("workflow_kinds") or {}
    mod.PRIME_SUFFIX = rules.get("prime_suffix", "")
    mod.MODEL_ALIASES = rules.get("model_aliases") or {}
    mod.MODEL_SLUG = rules.get("slug_fallback", "")
    mod.MESSAGES = rules.get("messages") or {}
    mod.STATUS_VALUES = rules.get("status_values") or {}
    mod.FEATURE_ID_FORMAT = rules.get("feature_id_format", "")
    mod.DRIVE_OPTIONS = rules.get("drive_options") or {}
    mod.SUCCESS_CODES = tuple(rules.get("success_codes") or (200, 201, 202))
    mod.DEFAULTS = rules.get("defaults") or {}

    # ---- 作用域 / 应用级令牌 ----
    mod.DEFAULT_SCOPE = eps.get("scope_default", "")

    # ---- 调度参数 ----
    for src, dst in (
        ("probe_interval", "PROBE_INTERVAL_S"),
        ("task_max_life", "TASK_MAX_LIFE_S"),
        ("poll_interval", "POLL_INTERVAL_S"),
        ("refresh_ahead", "REFRESH_AHEAD_S"),
    ):
        if src in sched:
            setattr(mod, dst, sched[src])

    # Settings 实例上的几个默认值也要跟着改 —— `/v1/models` 直接读它。
    # ⚠️ 这里必须 `from app import config` 再取 config.settings。
    # 上一版写的是 `from . import settings` —— 在那个上下文里 `.` 是
    # workbuddy 包，于是去找 workbuddy.settings，永远 ImportError，
    # 而这行包在 try/except 里，只留下一条 warning 就悄悄跳过了。
    try:
        from app import config as _cfg

        _s = _cfg.settings
        if rules.get("defaults"):
            d = rules["defaults"]
            _s.default_resolution = str(d.get("resolution") or _s.default_resolution)
            _s.default_ratio = str(d.get("ratio") or _s.default_ratio)
            _s.default_duration = int(d.get("duration") or _s.default_duration)
            _s.default_model = str(d.get("model") or _s.default_model)
        if rules.get("free_resolutions"):
            _s.free_fallback = list(rules["free_resolutions"])
        # 同时把模块级默认值也写一份，供不走 settings 的调用点使用
        mod.DEFAULTS = rules.get("defaults") or {}
    except Exception as e:
        log.warning("注入 Settings 默认值失败（不影响端点）：%s", e)

    log.info(
        "remote config applied: v=%s endpoints=%d workflows=%d",
        cfg.get("v"), len([v for v in eps.values() if v]), len(wfs),
    )


def is_applied() -> bool:
    return bool(_active)


def propagate(values: dict[str, Any]) -> int:
    """把刚注入的值**同步到所有已经 import 的 `app.*` 模块**。

    为什么需要这一步：`app/upstream.py` 写的是
    `from .config import GW` —— 这会在 import 那一刻把值复制到 upstream
    自己的命名空间。之后只改 `app.config.GW`，upstream 里那份**不会变**。

    启动路径靠 entry.py 的顺序（先注入、后 import）已经解决。但还有一条
    更常见的路径：**客户在网页上激活**。那时 app.main 早就 import 完了，
    所有 `from .config import X` 的模块都绑着空值 —— 不补这一步，客户激活
    成功、状态显示「已激活」，然后用起来全是 400。

    做法是直接改写那些模块里同名全局量。它们本来就在模块级命名空间里
    （因为 `from X import` 就是赋值到模块全局），所以能改到。
    """
    import sys

    n = 0
    for name, mod in list(sys.modules.items()):
        if not name.startswith("app.") or mod is None:
            continue
        for k, v in values.items():
            if k in getattr(mod, "__dict__", {}):
                setattr(mod, k, v)
                n += 1
    log.info("propagate: 同步 %d 处常量到 app.* 模块", n)
    return n


def apply_and_propagate(mod, cfg: dict[str, Any]) -> None:
    """apply() + propagate()。运行时激活走这个。

    单独导出是因为两个动作必须成对出现 —— 只 apply 不改已 import 的模块，
    表现就是「激活成功但功能全坏」，而且日志里一切正常。
    """
    apply(mod, cfg)
    propagate({k: getattr(mod, k) for k in _INJECTED_NAMES})


# apply() 会写的全部名字。propagate 用它决定要同步哪些。
_INJECTED_NAMES = (
    "AUTH_BASE", "OIDC_DISCOVERY", "AUTHORIZE", "TOKEN", "REGISTER", "REVOKE",
    "REVOKE_LOGOUT", "USERINFO", "API_BASE", "GW", "CATALOG", "CREDITS",
    "FEATURES", "ME", "USER_SHOW", "PENDING_USES", "WAN3_WORKFLOW",
    "MODELS_WORKFLOW", "TOUCHPOINT", "TOUCHPOINT_REFERRER",
    "PACKAGE_ID_BY_TIER", "UA", "BROWSER_HEADERS", "STATIC_HEADERS",
    "DRIVE_HEADERS", "H_ACCEPT", "H_DEVICE", "H_PACKAGE", "H_TOUCH",
    "H_TOUCH_REF", "H_UA", "H_APP_AUTH", "H_AUTH", "AUTH_PREFIX", "H_COOKIE",
    "FREE_RESOLUTIONS", "PAID_RESOLUTIONS", "ALL_RESOLUTIONS", "RATIOS",
    "DURATION_MIN", "DURATION_MAX", "DURATION_AUTO", "DURATIONS",
    "DURATION_WITH_VIDEO_REF", "MEDIA_TYPES", "MEDIA_MAX", "MEDIA_LIMITS",
    "FRAME_TYPES", "REFERENCE_TYPES", "REF_VIDEO_AUDIO_TOTAL_MAX",
    "WORKFLOW_KIND", "MEDIA_KINDS", "PRIME_SUFFIX", "MODEL_ALIASES",
    "MODEL_SLUG", "STATUS_VALUES", "FEATURE_ID_FORMAT", "DRIVE_OPTIONS",
    "MESSAGES", "SUCCESS_CODES", "FRAME_EXCLUSIVE", "DEFAULTS", "DEFAULT_SCOPE",
)


def load_sidecar(root) -> dict[str, Any] | None:
    """自托管/开发用：从 `_site/site-config.json` 读一份明文配置。

    这是**唯一**允许明文存在站点知识的地方，它必须：
      · 在发布目录之外，或在 .gitignore 里（`tools/gen_site_config.py` 会检查）
      · 绝不进入 WorkBuddy 发布包

    客户模式下这个函数永远不会被调用 —— 真值只能来自服务端解密。
    """
    import json
    from pathlib import Path

    p = Path(root) / "_site" / "site-config.json"
    if not p.exists():
        return None
    try:
        cfg = json.loads(p.read_text("utf-8"))
    except Exception as e:
        log.warning("_site/site-config.json 解析失败：%s", e)
        return None
    if not isinstance(cfg, dict) or not cfg.get("endpoints"):
        log.warning("_site/site-config.json 结构不对（缺 endpoints）")
        return None
    return cfg


def summary() -> dict[str, Any]:
    """给 `/api/license/status` 用的自省信息（**不含具体值**）。

    只报「有没有 / 有几条」，不报内容 —— 这个接口未激活时也能访问，
    把值印出来等于免费送。
    """
    return {
        "version": _active.get("v", 0),
        "endpoints": len([v for v in (_active.get("endpoints") or {}).values() if v]),
        "workflows": len(_active.get("workflows") or {}),
        "applied": is_applied(),
    }
