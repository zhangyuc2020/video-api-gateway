"""版本号与「检查更新」。

设计取舍
--------
**为什么版本号写死在代码里**
  它是构建产物的一部分。从 Git tag 读需要 subprocess + 仓库存在，
  而客户拿的是 zip（没有 .git）。

**为什么要查 GitHub Releases**
  客户各自部署，我们改不了他们的代码。唯一能通知「该更新了」的通道，
  就是让他们主动去看。而这个动作必须由客户端发起。

**关键约束：这个检查必须完全非阻塞、可失败**
  · GitHub API 在国内经常超时/被墙
  · 查不到就静默忽略，绝不能影响启动或任何功能
  · 缓存 24 小时，避免每次请求都外呼

**为什么只提示、不自动更新**
  代码在客户机器的项目目录里。自动替换等于我们获得了远程执行代码的能力 ——
  一旦私钥泄露或被入侵，所有客户机器失守。收益远小于风险。
  所以只提示，由客户自己重新发布。
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any

log = logging.getLogger("picsart-pool.version")

# 每次打 Release 时改这里
__version__ = "2026.11.05"

# 版本检查的目标。允许用环境变量覆盖（自托管/内网镜像）。
# 更新源：GitHub Releases（2026-10-25 从自建分发站改回）
#
# 演进过程值得记一下，免得以后又来回搬：
#   1. 最初查 GitHub —— 账号被封，失效
#   2. 迁到自建 dl.nuwa-ai.cc —— 能用，但要单独维护一个子域 + 证书 + 静态目录
#   3. 新账号通了 —— 改回 GitHub，少一个要维护的东西
#
# 自建分发站的代价：多一个域名、多一张证书（Let's Encrypt 90 天要续）、
# 多一套 nginx 配置、多一个 /dl/ 目录要手动更新。GitHub 一个 API 就够。
#
# 可用环境变量覆盖（自建镜像 / 内网部署时指向内部 release 清单）。
REPO = os.environ.get("PICSART_POOL_UPDATE_REPO", "zhangyuc2020/video-api-gateway")
API = os.environ.get(
    "PICSART_POOL_UPDATE_API", f"https://api.github.com/repos/{REPO}/releases/latest"
)
SOURCE = f"https://github.com/{REPO}"

# 缓存：24 小时。查一次就够，没必要每个请求都问。
_TTL = 24 * 3600
_cache: dict[str, Any] = {"at": 0.0, "latest": "", "url": "", "err": "", "page": ""}

# 单次查询超时。给短一点 —— 查不到无所谓，不能拖慢任何东西。
_TIMEOUT = 6.0


def current() -> str:
    return __version__


def _ver_tuple(v: str) -> tuple[int, ...]:
    """把 "2026.10.25" 变成 (2026, 10, 25) 便于比较。

    容忍 "v2026.10.25" 和 "2026.10.25-beta" 这类写法：
    只取开头的数字段，后面的丢弃。比较不需要精确语义，
    只要"新版本能判出来"就够。
    """
    out: list[int] = []
    for part in v.lstrip("vV").split("-")[0].split("."):
        try:
            out.append(int(part))
        except ValueError:
            break
    return tuple(out) or (0,)


def is_newer(latest: str, mine: str = "") -> bool:
    mine = mine or __version__
    return _ver_tuple(latest) > _ver_tuple(mine)


async def check_async(force: bool = False) -> dict[str, Any]:
    """查最新版本。永远不抛异常。

    返回 {"current", "latest", "has_update", "url", "checked_at", "error"}。
    error 非空表示这次没查成（网络问题），此时 has_update 恒为 False ——
    不能因为查不到就误报"有新版本"。
    """
    now = time.time()
    if not force and _cache["at"] and now - _cache["at"] < _TTL:
        return _result(_cache["latest"], _cache["url"], _cache["page"],
                       _cache["err"], _cache["at"])

    import httpx

    latest = ""
    url = ""
    page = ""
    err = ""
    try:
        # trust_env=False：不蹭系统代理，和业务请求保持一致。
        # 也不跟随重定向到奇怪的地方。
        async with httpx.AsyncClient(trust_env=False, follow_redirects=False) as c:
            r = await c.get(
                API,
                timeout=httpx.Timeout(_TIMEOUT, connect=4.0),
                headers={
                    "accept": "application/vnd.github+json",
                    "user-agent": "video-api-gateway-update-check",
                },
            )
        if r.status_code == 200:
            d = r.json()
            # GitHub Releases 的字段名和我们的清单不同：
            #   tag_name  -> 版本号（可能带前缀 v）
            #   html_url  -> Release 页面
            # 下载地址要从 assets 里找 zip
            latest = str(d.get("tag_name") or "").strip()
            page = str(d.get("html_url") or "").strip()
            url = ""
            for a in (d.get("assets") or []):
                n = str(a.get("name") or "")
                if n.endswith(".zip"):
                    url = str(a.get("browser_download_url") or "")
                    break
        elif r.status_code == 404:
            err = "release_not_found"
        else:
            err = f"http_{r.status_code}"
    except Exception as e:
        # 超时/DNS/被墙 都走这里。静默。
        err = type(e).__name__
        log.debug("update check failed: %s", err)

    _cache.update({"at": now, "latest": latest, "url": url,
                   "page": page, "err": err})
    return _result(latest, url, page, err, now)


def _result(latest: str, url: str, page: str, err: str,
            at: float) -> dict[str, Any]:
    has = bool(latest) and not err and is_newer(latest)
    return {
        "current": __version__,
        "latest": latest,
        "has_update": has,
        # 查不到时也给个页面地址，方便用户自己去看
        "url": url or f"https://github.com/{REPO}/releases",
        "page": page or f"https://github.com/{REPO}/releases",
        "source": SOURCE,
        "checked_at": int(at) if at else 0,
        "error": err,
    }
