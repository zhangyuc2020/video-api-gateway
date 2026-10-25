"""每个账号一套**稳定且自洽**的浏览器指纹。

为什么需要
----------
号池里所有账号共用同一个出口 IP，这本身已经是一个关联信号，没办法避免。
但如果连"浏览器"都完全一样，上:游看到的特征就是：

    同一个 IP + 同一个 UA + 同一组 Client Hints + 同一个 accept-language
    -> N 个账号

而真实世界里，同一个家庭/办公室的多台设备，UA 和语言偏好通常不同。
所以给每个账号派生一套自己的指纹，让它们看起来是**不同设备**，
是成本最低、收益最直接的隔离手段。

关键设计：**自洽（self-consistent）**
------------------------------------
这是最容易做错的地方。UA 和 Client Hints 是**互相印证**的一对：

    UA:                Chrome/130
    sec-ch-ua:         "Chromium";v="138", "Google Chrome";v="138"   <- 打架
    sec-ch-ua-platform: "Windows"                                     <- UA 说 macOS

这种矛盾比"所有号一样"更可疑 —— 它直接证明请求是伪造的。
所以指纹必须**成套生成**：浏览器大版本、操作系统、平台提示、语言，
全部由同一个哈希派生，彼此一致。

关键设计：**稳定（stable）**
---------------------------
同一个账号每次请求必须拿到**完全相同**的指纹。如果每次随机，
上游会看到"同一个账号在不同浏览器之间跳来跳去"，那是更强的异常信号。

所以种子必须是账号的**持久标识**（device_id，注册时生成一次，存库），
而不是每次调用现算的随机数。

为什么不改配置模板
----------------
`UA` / `BROWSER_HEADERS` 来自服务端下发的配置，是所有账号共享的默认值。
这里做的是**覆盖**：有账号级指纹时用它，没有时回落到配置值
（老数据 / cookie 模式账号可能没有 device_id）。
"""
from __future__ import annotations

import hashlib
from typing import Any

# 浏览器大版本池。
#
# 取 126~140：这是 2024 末到 2026 初的 Chrome 版本区间。
# 不能取太老（上游会对过旧版本起疑）也不能取太新（可能超出真实发布范围）。
# 数量 15 个足够让每个账号落到不同版本，又不会出现"没人用"的罕见值。
_CHROME_VERSIONS = tuple(range(126, 141))

# 操作系统池 + 对应的 Client Hints。
#
# 权重是**刻意不均匀**的：Windows 用户占绝大多数，macOS 次之，
# Linux 极少见（一个号池里出现 Linux 反而可疑）。
# 用哈希取模时会自然按这个比例分布。
_PLATFORMS = (
    # (UA 里的平台串,        sec-ch-ua-platform 值,  权重)
    ("Windows NT 10.0; Win64; x64", '"Windows"', 6),
    ("Macintosh; Intel Mac OS X 10_15_7", '"macOS"', 3),
)

# 语言偏好池。中文用户为主，但因为出口 IP 在境外，
# 混一部分英文偏好更自然（境外机器装中文系统很常见，反之亦然）。
_LANGUAGES = (
    ("zh-CN,zh;q=0.9,en;q=0.8", "zh-CN"),
    ("zh-CN,zh;q=0.9,en-US;q=0.8,en;q=0.7", "zh-CN"),
    ("en-US,en;q=0.9", "en-US"),
    ("en-US,en;q=0.9,zh-CN;q=0.8", "en-US"),
    ("zh-TW,zh;q=0.9,en;q=0.8", "zh-TW"),
)


def _seed(account: dict[str, Any]) -> str:
    """取账号的稳定种子。

    优先级：device_id > sub > name。
    device_id 是最可靠的（注册时生成、存库、之后不变）。
    回落到 sub（上游用户 ID）和 name 是为了兼容老数据 ——
    只要它们也不变，指纹就稳定。
    """
    for key in ("device_id", "sub", "name"):
        v = str(account.get(key) or "").strip()
        if v:
            return v
    # 什么都没有：用一个固定串，至少保证同一次进程内稳定
    return "anonymous"


def _pick(seq, h: bytes, offset: int):
    """从序列里按哈希字节选一个（线性同余，够均匀）。"""
    return seq[h[offset] % len(seq)]


def _weighted_platform(h: bytes, offset: int):
    """按权重选平台。"""
    total = sum(w for _, _, w in _PLATFORMS)
    n = h[offset] % total
    acc = 0
    for ua_part, hint, w in _PLATFORMS:
        acc += w
        if n < acc:
            return ua_part, hint
    return _PLATFORMS[0][0], _PLATFORMS[0][1]


def fingerprint(account: dict[str, Any]) -> dict[str, str]:
    """这个账号的指纹。返回可直接 merge 进请求头的字典。

    返回的键是**标准头名**（不是配置里的动态头名）——
    调用方负责只在配置允许时才应用。
    """
    seed = _seed(account)
    h = hashlib.sha256(f"fp-v1|{seed}".encode()).digest()

    version = _pick(_CHROME_VERSIONS, h, 0)
    ua_platform, hint_platform = _weighted_platform(h, 1)
    lang, lang_code = _pick(_LANGUAGES, h, 2)

    # sec-ch-ua 的格式：品牌列表随大版本略有差异，但结构固定。
    # "Not)A;Brand" 是 Chrome 从 105 起塞的噪声品牌，值固定在 v="99"。
    ua_full = (
        f"Mozilla/5.0 ({ua_platform}) AppleWebKit/537.36 "
        f"(KHTML, like Gecko) Chrome/{version}.0.0.0 Safari/537.36"
    )

    return {
        "user-agent": ua_full,
        "sec-ch-ua": (
            f'"Chromium";v="{version}", "Google Chrome";v="{version}", '
            f'"Not)A;Brand";v="99"'
        ),
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": hint_platform,
        "accept-language": lang,
        "language-code": lang_code.split("-")[0],
    }


def describe(account: dict[str, Any]) -> str:
    """人看的摘要，用于诊断面板确认隔离生效。"""
    fp = fingerprint(account)
    m = fp["user-agent"]
    # 只取关键片段，别把整串刷进日志
    ver = m.split("Chrome/")[-1].split(".")[0] if "Chrome/" in m else "?"
    plat = fp["sec-ch-ua-platform"].strip('"')
    return f"Chrome/{ver} on {plat}, lang={fp['accept-language'].split(',')[0]}"
