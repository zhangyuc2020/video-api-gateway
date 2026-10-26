"""素材上传：把本地文件/内联数据传到上游云存储，换成公网链接。

为什么需要
----------
`media[].url` 必须是**上游能抓取的公网地址**。之前完全靠调用方自备 ——
客户想做图生视频，得先自己找图床、上传、复制链接，再粘进 API。
这一步劝退了大部分人。

`picsart` 自己的编辑器和 AI Playground 都往 `upload.picsart.com/v2/files`
传文件，返回一个 `gcdn.picsart.com/editing-temp/<uuid>.<ext>` 的公开地址。
我们也走这条路。

实测结论（2026-10-26，用网页端 HAR 里的头直接打真端点）
----------------------------------------------------
    POST https://upload.picsart.com/v2/files
    multipart/form-data:
        file = <二进制>
        type = "editing-temp"
    头: x-app-authorization(Bearer) + deviceid + x-touchpoint

    -> 200  216 字节
       {"status":"success",
        "response":{"url":"https://gcdn.picsart.com/editing-temp/<uuid>.png",
                    "download_url":"...（同上）"}}

**不需要用户 token**。这一点很关键：上传和号池完全解耦 ——
不占账号的授权额度、不碰 auth 端点、不受号池熔断影响。
只要配置里的应用令牌有效就能传。

为什么不放进号池流程
--------------------
上传是一次性的、与账号无关的 HTTP 调用。放进 `upstream` 会让那个模块
依赖"当前用哪个账号"，而上传压根不需要账号。

已知限制
--------
· 文件落在 `editing-temp`（临时区），上游可能定期清理 ——
  所以**只适合当"提交前的跳板"**，不适合长期托管。
  客户如果自己要留档，还是得自己存。
· 没有 Content-MD5 / 分片，大文件是整包上传（受 C.UPLOAD_MAX_BYTES 限制）。
"""
from __future__ import annotations

import logging
import time
import uuid
from typing import Any, AsyncIterator

import httpx

from . import config as C

# ⚠️ 为什么用 rom . import config as C 而不是 rom .config import X
# （2026-10-26）
#
# rom .config import X 在**模块导入那一刻**就把值复制进本模块的命名空间。
# 而激活/热更新发生在导入之后（workbuddy/remote.py 改写 app.config 的模块属性），
# 于是本模块拿到的永远是空串 —— 上传永远报 not_configured，
# 而 app.config 里明明有值。
#
# 这和 upstream.py 里那个 /workflows//submit 的 bug 是同一类：
# Python 的 import 绑定是快照，不是引用。
# 所以这里全部走 C.XXX 实时读。

log = logging.getLogger("picsart-pool.upload")


class UploadError(RuntimeError):
    """上传失败。message 直接给用户看。"""

    def __init__(self, msg: str, *, code: str = "upload_failed",
                 status: int | None = None):
        self.code = code
        self.status = status
        super().__init__(msg)


def configured() -> bool:
    """上传端点配置齐了吗。没齐时调用方应给出明确提示而不是拼空 URL。"""
    return bool(C.UPLOAD_BASE and C.UPLOAD_PATH and C.UPLOAD_TYPE)


def _guess_mime(name: str, data: bytes) -> str:
    """按扩展名 + 魔数猜 MIME。上游靠它决定怎么处理。"""
    low = (name or "").lower()
    table = (
        ((".png",), "image/png"),
        ((".jpg", ".jpeg"), "image/jpeg"),
        ((".webp",), "image/webp"),
        ((".gif",), "image/gif"),
        ((".mp4",), "video/mp4"),
        ((".mov",), "video/quicktime"),
        ((".webm",), "video/webm"),
        ((".mp3",), "audio/mpeg"),
        ((".wav",), "audio/wav"),
        ((".m4a",), "audio/mp4"),
    )
    for exts, mime in table:
        if low.endswith(exts):
            return mime
    # 魔数兜底：扩展名不可信时看文件头
    if data.startswith(b"\x89PNG"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"GIF8"):
        return "image/gif"
    if len(data) > 12 and data[4:8] == b"ftyp":
        return "video/mp4"
    return "application/octet-stream"


def _ext_for(mime: str) -> str:
    return {
        "image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp",
        "image/gif": ".gif", "video/mp4": ".mp4", "video/quicktime": ".mov",
        "video/webm": ".webm", "audio/mpeg": ".mp3", "audio/wav": ".wav",
        "audio/mp4": ".m4a",
    }.get(mime, ".bin")


def _headers(device_id: str = "") -> dict[str, str]:
    """上传请求的头。全部来自配置 —— 和业务请求用的是同一套来源。"""
    h: dict[str, str] = dict(C.STATIC_HEADERS or {})
    if C.H_UA and C.UA:
        h[C.H_UA] = C.UA
    if C.H_DEVICE and device_id:
        h[C.H_DEVICE] = device_id
    if C.H_TOUCH and C.TOUCHPOINT:
        h[C.H_TOUCH] = C.TOUCHPOINT
    if C.H_APP_AUTH and C.APP_AUTHORIZATION:
        h[C.H_APP_AUTH] = C.APP_AUTHORIZATION
    return h



async def upload_stream(chunks: AsyncIterator[bytes], filename: str,
                        size_hint: int = 0,
                        device_id: str = "") -> dict[str, Any]:
    """流式上传：边收边发，不把整个文件读进内存。

    为什么必须有这条路径（2026-10-26）
    ----------------------------------
    参考视频可以到 20MB+。用 base64 传会膨胀 33%（27MB），
    而且 `await request.json()` 要先完整解析整个 body ——
    内存峰值翻倍，首字节延迟等于收完整个文件的耗时。
    multipart 流式则是不膨胀、边收边发。

    实现要点
    --------
    手工拼 multipart，但用**生成器**产出：
        开头 boundary + 文件头
        -> 文件内容（逐块转发）
        -> type 字段 + 结束 boundary

    这样 `httpx` 会用 chunked transfer 边发边收，
    内存里最多只有一个 chunk（默认 64KB）。

    size_hint 只用于预检查上限（Content-Length 未知时也能拦）。
    """
    if not C.UPLOAD_BASE or not C.UPLOAD_PATH or not C.UPLOAD_TYPE:
        raise UploadError(
            "素材上传功能未启用（服务端配置缺少 upload 端点）。"
            "请改用公网可访问的素材链接。",
            code="not_configured",
        )
    if C.UPLOAD_MAX_BYTES and size_hint and size_hint > C.UPLOAD_MAX_BYTES:
        raise UploadError(
            f"文件太大（{size_hint / 1024 / 1024:.1f} MB）。"
            f"上限 {C.UPLOAD_MAX_BYTES / 1024 / 1024:.0f} MB。",
            code="too_large",
        )

    name = filename or f"upload-{int(time.time() * 1000)}.bin"
    mime = _guess_mime(name, b"")
    url = f"{C.UPLOAD_BASE.rstrip('/')}{C.UPLOAD_PATH}"
    boundary = "----dsh" + uuid.uuid4().hex[:16]

    head = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="{C.UPLOAD_FILE_FIELD}"; '
        f'filename="{name}"\r\n'
        f"Content-Type: {mime}\r\n"
        f"\r\n"
    ).encode()
    tail = (
        f"\r\n--{boundary}\r\n"
        f'Content-Disposition: form-data; name="{C.UPLOAD_TYPE_FIELD}"\r\n'
        f"\r\n"
        f"{C.UPLOAD_TYPE}"
        f"\r\n--{boundary}--\r\n"
    ).encode()

    sent = {"n": 0}

    async def body() -> AsyncIterator[bytes]:
        yield head
        async for chunk in chunks:
            if not chunk:
                continue
            sent["n"] += len(chunk)
            if C.UPLOAD_MAX_BYTES and sent["n"] > C.UPLOAD_MAX_BYTES:
                # 中途超限：抛出会让 httpx 中断上传，不会把半个文件传完
                raise UploadError(
                    f"文件超过上限（{C.UPLOAD_MAX_BYTES / 1024 / 1024:.0f} MB），"
                    f"已中断上传",
                    code="too_large",
                )
            yield chunk
        yield tail

    h = _headers(device_id)
    h["content-type"] = f"multipart/form-data; boundary={boundary}"
    if size_hint:
        # 给了就发 Content-Length，上游能直接判定是否超限；
        # 没给就让 httpx 用 chunked（也是合法的）。
        h["content-length"] = str(
            len(head) + size_hint + len(tail)
        )

    t0 = time.monotonic()
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(300.0, connect=15.0, write=120.0),
            follow_redirects=False,
        ) as c:
            r = await c.post(url, content=body(), headers=h)
    except UploadError:
        raise
    except httpx.TimeoutException:
        raise UploadError(
            "上传超时。文件较大时请重试，或改用外部链接。",
            code="timeout",
        ) from None
    except httpx.HTTPError as e:
        raise UploadError(
            f"上传请求失败（{type(e).__name__}）。请检查部署环境的网络。",
            code="network",
        ) from e

    return _parse_result(r, name, mime, sent["n"],
                         round(time.monotonic() - t0, 2))


async def upload_bytes(data: bytes, filename: str = "",
                       device_id: str = "") -> dict[str, Any]:
    """上传一段已在内存里的字节。

    内部转调 upload_stream —— 只有一条实现路径，避免"内存版和流式版
    行为不一致"这种最难查的 bug。

    什么时候用它：内容本来就在内存里（比如测试、小图标）。
    大文件请让调用方直接走 multipart —— 那条路径是流式的。
    """

    async def once() -> AsyncIterator[bytes]:
        yield data

    return await upload_stream(once(), filename, len(data), device_id)


def _parse_result(r: httpx.Response, name: str, mime: str,
                  size: int, elapsed: float) -> dict[str, Any]:
    """解析上传响应。上传与流式两条路径共用一个解析器。"""
    payload: dict[str, Any] = {}
    try:
        payload = r.json()
    except Exception:
        payload = {"_raw": r.text[:300]}

    if r.status_code == 413:
        raise UploadError(
            f"文件太大，上游拒绝（{size / 1024 / 1024:.1f} MB）。",
            code="too_large", status=413,
        )
    if r.status_code in (401, 403):
        raise UploadError(
            "上游拒绝了上传（应用令牌无效或过期）。"
            "请联系技术支持更新服务端配置。",
            code="unauthorized", status=r.status_code,
        )
    if r.status_code == 422:
        # 上游会**真的解析文件内容**（2026-10-26 实测）。
        # 外壳像 mp4 不够 —— 造过一个结构完整的假 mp4
        # （ftyp + moov + trak + mvhd + mdat 齐全）照样被拒，
        # 换成真实编码的 H.264/AAC 才通过。
        #
        # 所以 422 不是"不认这个类型"，而是"这个文件本身坏了或编码不对"。
        # 原始信息只有一句英文 `file_is_corrupted`，客户看不懂，
        # 这里按 reason 翻译成能指导操作的说明。
        reason = str(payload.get("reason") or "")
        msg = str(payload.get("message") or "")
        if "corrupt" in reason.lower() or "invalid" in msg.lower():
            raise UploadError(
                f"文件内容不完整或编码不受支持（{reason or msg}）。"
                f"如果是视频，请确认是标准 H.264 + AAC 编码的 MP4；"
                f"如果是图片，请确认是正常的 PNG/JPEG。"
                f"有些工具导出的文件头正常但数据被截断，也会触发这个错误。",
                code="corrupted", status=422,
            )
        raise UploadError(
            f"上游拒绝了文件内容（{reason or msg or '未说明原因'}）。"
            f"请换个文件重试。",
            code="rejected", status=422,
        )
    if r.status_code != 200:
        raise UploadError(
            f"上传失败（HTTP {r.status_code}）：{str(payload)[:200]}",
            code="http_error", status=r.status_code,
        )
    if str(payload.get("status") or "") != "success":
        raise UploadError(
            f"上传未成功：{str(payload)[:200]}", code="not_success",
            status=200,
        )

    resp = payload.get("response") or {}
    got = str(resp.get("url") or resp.get("download_url") or "")
    if not got:
        raise UploadError(
            f"上传成功但没拿到链接：{str(payload)[:200]}",
            code="no_url", status=200,
        )

    log.info("upload ok: %s (%d bytes, %ss)", got[-60:], size, elapsed)
    return {
        "url": got,
        "download_url": str(resp.get("download_url") or got),
        "name": name,
        "mime": mime,
        "size": size,
        "elapsed": elapsed,
    }
