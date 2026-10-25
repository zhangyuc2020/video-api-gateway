"""激活门 —— Go 客户端 `client/license*.go` + `client/cfgload.go` 的 Python 等价物。

为什么必须有这一层
------------------
现在交付给客户的只有 Go 桌面版带激活门；Python 这套（`app/`）**完全没有授权校验**
——`security.py` 里只有管理密码。所以「客户自己用 WorkBuddy 发布」如果直接发
Python 源码，等于把号池白送：任何人拿到链接就能加自己的账号、白嫖整套上游知识。

这里把 Go 侧那套判据原样搬过来，**协议字节级一致**（服务端一行都不用改）：

  激活     POST /activate        {card, hwid}            -> {license}
  心跳     POST /heartbeat       {hwid, sig}             -> {min_lic_ver, cfg_ver}
  配置     POST /config          {hwid, sig}             -> {version, body, salt, sig_hint}

三个必须逐字节对齐的地方（对不上就是「卡密正确但激活失败」这种查不出原因的状态）：

  1. 请求签名：X-Sign = HMAC-SHA256(sha256(SIGN_SALT|hwid), body ‖ "{ts}|{path}" ‖ nonce)
  2. license 信封：base64(payload_json ‖ "." ‖ sig(64))，分隔符位置**算**出来，不搜
  3. 配置密钥：HKDF-SHA256(ikm=lic_sig, salt=本次随机, info="cfg-v{n}")

hwid 的语义在这里变了，这是**唯一**一处刻意的偏离
--------------------------------------------------
桌面版用 `sha256(MachineGuid|卷序列号)`；Linux 沙箱里既没有注册表也没有固定盘符，
容器每次重新发布还可能换主机名。继续绑机器 = 客户每次重新部署都要找你要解绑。

所以改用**部署标识**（`deploy.id`，首次运行生成，落盘）：
  · 服务端对 `hwid` 是**不透明字符串**，不校验格式 —— 服务端零改动
  · 重新发布会保留数据卷，标识不变 → 激活持续有效
  · 数据卷被清 → 标识丢失 → 必须重新激活（这是要提前跟客户讲清的运维代价，
    见 workbuddy/README.md「数据卷」一节）

诚实的边界（和 Go 侧注释同样的取舍）
------------------------------------
客户激活成功之后，配置解出来的明文就在他自己的机器上，内存 dump 拦不住。
本方案保证的是「没付钱的人拿不到」，不是「付了钱的人拿不走」。
这也是为什么源码公开在这个模型下是安全的 —— 机密不在源码里。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import ssl
import time
from pathlib import Path
from typing import Any

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

log = logging.getLogger("picsart-pool.gate")

# ---------------------------------------------------------------- 常量

# 内置公钥（只用于验签，没有私钥就造不出 license）。拆两段存放，
# 与 client/license.go 的 pkPart1/pkPart2 完全一致 —— 已实测与 server/keys/sk.pem 配对。
_PK1 = "9iVcGX7jcW43LFVM"
_PK2 = "tS7SdY0WIO8JNSghCMwtY1rWzo4="

# 请求签名盐。它**不是**授权凭据：没有有效卡密照样拿不到 license，
# 拿不到 license 就派生不出配置密钥。真正的作用是让「录包重放」失效。
_SIGN_SALT = "d9fe0ab17cc16dc5091a4d1cec82d1b8c1caa923a1272c5cb1188c9ec7a07e48"

# 授权服务器地址。可用环境变量覆盖（本地联调 / 换域名），默认线上地址。
_DEFAULT_BASE = "https://lic.nuwa-ai.cc"

# 证书公钥 pin（base64 SPKI sha256）。与 Go 版 `client/transport.go:pinSPKI`
# **必须是同一个值** —— 两边打的是同一台服务器、同一张证书。
#
# 作用：中间设备（企业网关/杀软 HTTPS 扫描）换掉证书时，请求会被拒而不是
# 把 license 和配置交给一个冒充者。留空则退化成"只验系统 CA"，
# 那种拦截完全看不出来。
#
# ⚠️ 换证书 = 所有已发出的客户端连不上（pin 不匹配）。换之前先想清楚
# 怎么让存量客户升级 —— Go 版早就有这个约束了（README 第四节）。
# 重算方法：
#   openssl x509 -in certs/lic.pem -pubkey -noout \
#     | openssl pkey -pubin -outform DER | openssl dgst -sha256 -binary | base64
_PIN_SPKI = "vMbp5Ewbh6gQTD8TOBVJY7otwnsuYXVn2w3BthMTYDc="

LIC_FILE = "license.dat"
VER_FILE = "lic.ver"
OK_FILE = "lic.ok"
DEPLOY_FILE = "deploy.id"

HEARTBEAT_INTERVAL = 12 * 3600     # 12h 一次心跳
OFFLINE_GRACE = 72 * 3600          # 离线宽限 72h
HEARTBEAT_RETRY_GAP = 60           # 失败后 60s 内不重试


def _now() -> int:
    return int(time.time())


# ---------------------------------------------------------------- 密码学小工具

def _pubkey() -> Ed25519PublicKey:
    raw = base64.b64decode(_PK1 + _PK2)
    if len(raw) != 32:
        raise ValueError("内置公钥长度异常")
    return Ed25519PublicKey.from_public_bytes(raw)


def _hkdf(ikm: bytes, salt: bytes, info: bytes, length: int = 32) -> bytes:
    return HKDF(
        algorithm=hashes.SHA256(), length=length, salt=salt, info=info
    ).derive(ikm)


def split_envelope(raw: bytes) -> tuple[bytes, bytes] | tuple[None, None]:
    """切 `payload ‖ '.' ‖ sig(64)`。

    签名定长，所以位置是算出来的（len-64-1），不是搜出来的。
    搜分隔符会有 1-(255/256)^63 ≈ 21.85% 的概率切错 —— 表现成「卡密正确但
    激活失败」，而且每次服务端重新签名都是独立抽样，重试一次可能就好了，
    客服永远复现不了。Go 侧踩过这个坑，这里必须用同一份算法。
    """
    if len(raw) < 64 + 1:
        return None, None
    dot = len(raw) - 64 - 1
    if raw[dot:dot + 1] != b".":
        return None, None
    return raw[:dot], raw[dot + 1:]


def _xor(b: bytes) -> bytes:
    key = bytes([0x5A, 0x9C, 0x31, 0xF7, 0x0E, 0x44, 0xB2, 0x6D])
    return bytes(x ^ key[i % len(key)] for i, x in enumerate(b))


# ---------------------------------------------------------------- 部署标识

def deploy_id(data_dir: Path) -> str:
    """本部署的稳定标识。首次运行生成并落盘。

    换掉机器码的理由见模块头。这里额外做一件事：**格式自检**。
    空串会让服务端建出一条永远对不上的绑定（Go 侧记过这个事故：
    三路因子全失败退化成常量，多台机器撞同一个码，授权白送）。
    """
    f = data_dir / DEPLOY_FILE
    try:
        v = f.read_text("utf-8").strip()
    except OSError:
        v = ""
    if not re.fullmatch(r"[0-9a-f]{32}", v):
        v = secrets.token_hex(16)
        data_dir.mkdir(parents=True, exist_ok=True)
        f.write_text(v, encoding="utf-8")
        try:
            f.chmod(0o600)
        except OSError:
            pass
    return v


# ---------------------------------------------------------------- 传输层

def _spki_digest(der: bytes) -> bytes:
    """证书 DER -> SPKI 的 sha256。与 Go 的 x509.MarshalPKIXPublicKey 同义。

    ⚠️ `x509` 是独立的顶层模块，不在 `serialization` 下面：
    `serialization.x509` → AttributeError。这个错法很自然（两者都在
    cryptography 里），但只在真正走到 pin 校验时才暴露 —— 而那条路径
    平时不走，所以能一直藏着。
    """
    from cryptography import x509

    cert = x509.load_der_x509_certificate(der)
    spki = cert.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return hashlib.sha256(spki).digest()


class _PinnedContext(ssl.SSLContext):
    """握手后额外比对证书公钥的 SSLContext。

    为什么这么做（而不是"启动时探一次"）
    ------------------------------------
    启动时探一次只能发现"那一刻"的中间人。真正的威胁是**持续的** TLS 拦截
    （企业网关、杀软 HTTPS 扫描）—— 它不会在开机那一刻刚好可用然后就消失。
    只探一次等于只在开机时验票。

    为什么必须关掉链校验
    -------------------
    Python 的 ssl 没有 Go 的 `VerifyPeerCertificate` 回调。要做"只认这张证书"
    就必须关掉链校验，改为自己比对 SPKI —— 这正是 Go 版的做法
    （`InsecureSkipVerify: true` + `VerifyPeerCertificate` 里做 pin）。
    两边语义一致，不是这里偷懒。

    安全性没有降低：pin 比"信任任何 CA"更强。任何 CA 签的证书都过不了这一关，
    只有持那把私钥的服务器能过。
    """

    # ⚠️ 必须用工厂函数构造，不能 `super().__init__(PROTOCOL_TLS_CLIENT)` ——
    # ssl.SSLContext 是 C 类型，子类直接调 __init__ 会报
    # `TypeError: 'bytes' object cannot be interpreted as an integer`。
    # 正确做法是让工厂造出标准实例，再把它 __class__ 改成我们的子类
    # （CPython 允许改，前提是布局一致 —— SSLContext 是普通 C 扩展类型，
    # 没有 __slots__ 限制，实测可行）。
    _expected: bytes

    @classmethod
    def create(cls, expected: bytes) -> "ssl.SSLContext":
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.__class__ = cls                  # type: ignore[assignment]
        ctx._expected = expected
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx

    def wrap_socket(self, sock, *args, **kwargs):  # type: ignore[override]
        ss = super().wrap_socket(sock, *args, **kwargs)
        der = ss.getpeercert(binary_form=True)
        if not der:
            ss.close()
            raise ssl.SSLError("授权服务器没有出示证书")
        if _spki_digest(der) != self._expected:
            ss.close()
            raise ssl.SSLError(
                "授权服务器证书公钥不匹配：中间设备在做 TLS 拦截，或服务端换过证书"
            )
        return ss


class LicenseTransport:
    """往授权服务器发**带签名**的请求。

    三处刻意的设计：

    · 不走环境变量代理（`trust_env=False`）。Go 侧为这件事写了长注释：
      软件同时需要「上游走代理」和「授权直连」，混在一起会让挂着梯子的
      客户连激活都做不到。沙箱里也一样。
    · 证书 pin 挂在 SSLContext 上，**每次连接都验**，不是启动时探一次。
    · 请求带 HMAC 签名（时间戳 + nonce + 路径），录包重放无效。
    """

    def __init__(self, base: str | None = None) -> None:
        self.base = (base or os.environ.get("LICENSE_BASE") or _DEFAULT_BASE).rstrip("/")
        self._client: httpx.Client | None = None

    def client(self) -> httpx.Client:
        if self._client is None:
            kwargs: dict[str, Any] = dict(
                timeout=httpx.Timeout(15.0),
                trust_env=False,
                follow_redirects=False,
            )
            # 只有 https 且配了 pin 才启用。留空 = 走系统 CA（开发/本地联调）。
            if _PIN_SPKI and self.base.startswith("https://"):
                kwargs["verify"] = _PinnedContext.create(base64.b64decode(_PIN_SPKI))
            self._client = httpx.Client(**kwargs)
        return self._client

    def post_signed(self, path: str, body: dict[str, Any], hwid: str) -> tuple[bytes, int]:
        payload = json.dumps(body, separators=(",", ":"), sort_keys=True).encode()
        ts = _now()
        nonce = secrets.token_bytes(12)
        secret = hashlib.sha256((_SIGN_SALT + "|" + hwid).encode()).digest()
        mac = hmac.new(secret, digestmod=hashlib.sha256)
        mac.update(payload)
        mac.update(f"{ts}|{path}".encode())
        mac.update(nonce)

        headers = {
            "content-type": "application/json",
            "x-ts": str(ts),
            "x-nonce": nonce.hex(),
            "x-sign": mac.hexdigest(),
        }
        r = self.client().post(self.base + path, content=payload, headers=headers)
        return r.content, r.status_code


def _decode_err(b: bytes, code: int) -> str:
    try:
        d = json.loads(b)
        if isinstance(d, dict) and d.get("err"):
            return str(d["err"])
    except Exception:
        pass
    return f"服务器返回 {code}"


# ---------------------------------------------------------------- 配置解密

def open_config(body: str, sig_hint_b64: str, salt_b64: str, version: int) -> dict[str, Any]:
    """验签 + 解密一份下发的配置。

    和 `client/cfgload.go:loadRemoteConfigV2` 一一对应。三步缺一不可：
      1. 用内置公钥验签 —— 所以改不了规则（把 1080P 塞进免费档之类）
      2. 用 lic_sig 派生密钥解密 —— 所以换台机器解不开
      3. 检查 exp —— 到期后拒绝使用，必须靠心跳续期
    """
    raw = base64.b64decode(body)
    blob_b64, sig = split_envelope(raw)
    if blob_b64 is None:
        raise ValueError("配置信封格式非法（分隔符位置不对）")

    _pubkey().verify(sig, blob_b64)      # 失败抛 InvalidSignature

    blob = base64.b64decode(blob_b64)
    if len(blob) < 12 + 16:
        raise ValueError("配置密文过短")
    nonce, ct = blob[:12], blob[12:]

    salt = base64.b64decode(salt_b64) if salt_b64 else b"picsart-pool-cfg"
    key = _hkdf(base64.b64decode(sig_hint_b64), salt, f"cfg-v{version}".encode(), 32)
    plain = AESGCM(key).decrypt(nonce, ct, None)
    cfg = json.loads(plain)

    if int(cfg.get("v", 0)) < 1:
        raise ValueError("配置缺少版本号")
    exp = int(cfg.get("exp") or 0)
    if exp and exp < _now():
        raise ValueError("配置已过期")
    return cfg


# ---------------------------------------------------------------- 授权门

class Gate:
    """授权门。判据只有一处：`Verify()`。

    每个受保护入口都问它，patch 单点无效 —— 这是 Go 侧的原话，这里保持同样的形态。
    """

    def __init__(self, data_dir: Path) -> None:
        self.dir = data_dir
        self.dir.mkdir(parents=True, exist_ok=True)
        self.hwid = deploy_id(self.dir)

        self._payload: dict[str, Any] = {}
        self._raw_payload: bytes = b""
        self._sig = b""
        self._last_ok = 0
        self._last_try = 0
        self._server_ver = 1
        self._cfg: dict[str, Any] | None = None
        # 当前生效的配置版本号。心跳拿它和服务的 cfg_ver 比对，
        # 不一致就自动重拉 —— 这是「不改客户代码也能改行为」的唯一通道。
        self._cfg_ver = 0
        # 「配置已换新，但还没注入到 app.* 模块」的标记。
        #
        # 为什么不在这里直接注入：gate.py 不该 import app.*
        # （它会引入循环依赖，而且 gate 要保持"只负责授权"的单一职责）。
        # 改成留标记，由 guard.py 的中间件在下一个请求到来时执行注入 ——
        # 那时 app.* 一定已经加载完毕，时机是安全的。
        self._cfg_dirty = False
        self._cfg_err = ""

    # ---- 落盘 ----

    def _disk_key(self) -> bytes:
        # 与 Go 的 diskKey 同一算式：换部署标识就解不开旧文件。
        return hashlib.sha256(("licwrap-v1|" + self.hwid).encode()).digest()

    def _save(self) -> None:
        plain = json.dumps(
            {
                "p": base64.b64encode(self._raw_payload).decode(),
                "s": base64.b64encode(self._sig).decode(),
            },
            separators=(",", ":"),
        ).encode()
        nonce = secrets.token_bytes(12)
        ct = AESGCM(self._disk_key()).encrypt(nonce, plain, None)
        (self.dir / LIC_FILE).write_bytes(_xor(nonce + ct))
        (self.dir / VER_FILE).write_text(str(self._server_ver), encoding="utf-8")

    def _load(self) -> bool:
        try:
            raw = _xor((self.dir / LIC_FILE).read_bytes())
        except OSError:
            return False
        if len(raw) < 12 + 16:
            return False
        try:
            plain = AESGCM(self._disk_key()).decrypt(raw[:12], raw[12:], None)
            d = json.loads(plain)
            self._raw_payload = base64.b64decode(d["p"])
            self._sig = base64.b64decode(d["s"])
            self._payload = json.loads(self._raw_payload)
        except Exception:
            return False

        try:
            self._server_ver = max(1, int((self.dir / VER_FILE).read_text("utf-8").strip()))
        except (OSError, ValueError):
            self._server_ver = 1

        # last_ok 必须落盘：不读的话进程每次启动都是 0，
        # `now - 0 <= 72h` 恒不成立 —— 授权服务器稍有不通，软件就打不开。
        # （Go 侧这个 bug 有过完整记录：界面显示「已激活」，接口全 403。）
        try:
            v = int((self.dir / OK_FILE).read_text("utf-8").strip())
            if 0 < v <= _now() + 86400:
                self._last_ok = min(v, _now())
        except (OSError, ValueError):
            pass
        return True

    _raw_payload: bytes = b""

    # ---- 判据 ----

    def verify(self) -> bool:
        if not self._sig or not self._raw_payload:
            return False
        try:
            _pubkey().verify(self._sig, self._raw_payload)
        except Exception:
            return False
        if not isinstance(self._payload, dict):
            return False
        floor = max(1, self._server_ver)
        exp = int(self._payload.get("exp") or 0)
        ver = int(self._payload.get("ver") or 0)
        return (
            self._payload.get("hwid") == self.hwid
            and ver >= floor
            and exp > _now()
        )

    def allowed(self) -> bool:
        """功能入口的统一判据：验签通过 + 在离线宽限内。

        宽限从**最后一次成功心跳**算起，不是从启动算起 —— 否则长期离线的
        客户重启一次就白拿 72 小时。
        """
        if not self.verify():
            return False
        self.maybe_heartbeat()
        return _now() - self._last_ok <= OFFLINE_GRACE

    def maybe_heartbeat(self) -> None:
        now = _now()
        if now - self._last_try <= HEARTBEAT_RETRY_GAP:
            return
        self._last_try = now
        try:
            self.heartbeat()
        except Exception as e:
            log.debug("心跳失败（不影响本地判定）：%s", e)

    def status(self) -> dict[str, Any]:
        ok = self.verify()
        return {
            "activated": ok and _now() - self._last_ok <= OFFLINE_GRACE,
            "hwid": self.hwid,
            "tier": self._payload.get("tier", "") if ok else "",
            "expire": int(self._payload.get("exp") or 0) if ok else 0,
            "config_ready": self._cfg is not None,
            "config_error": self._cfg_err,
            "last_ok": self._last_ok,
        }

    # ---- 动作 ----

    def activate(self, card: str) -> None:
        card = card.strip().upper()
        if not card:
            raise ValueError("激活码不能为空")

        body = {"card": card, "hwid": self.hwid}
        b, code = self._post("/activate", body)
        if code != 200:
            raise ValueError(_decode_err(b, code))

        lic_b64 = (json.loads(b) or {}).get("license") or ""
        if not lic_b64:
            raise ValueError("服务器未返回 license")
        payload, sig = split_envelope(base64.b64decode(lic_b64))
        if payload is None:
            raise ValueError("license 格式非法")

        self._raw_payload = payload
        self._sig = sig
        self._payload = json.loads(payload)
        if not self.verify():
            raise ValueError(
                f"license 本地校验未通过（hwid={self.hwid} "
                f"exp={self._payload.get('exp')} ver={self._payload.get('ver')}）"
            )
        self._mark_ok()
        self._save()
        # 激活成功必然和服务端通过话，顺手把版本下限和配置一起同步。
        try:
            self.heartbeat()
        except Exception:
            pass
        self.refresh_config(raise_on_error=False)

    def heartbeat(self) -> int:
        digest = hashlib.sha256(self._sig).hexdigest()[:32]
        b, code = self._post("/heartbeat", {"hwid": self.hwid, "sig": digest})
        if code != 200:
            raise RuntimeError(_decode_err(b, code))
        try:
            d = json.loads(b)
            self._server_ver = max(self._server_ver, int(d.get("min_lic_ver") or 1))
        except Exception:
            d = {}

        # ⚠️ cfg_ver 变了就立刻重拉配置（2026-10-25 补）。
        #
        # 为什么需要：客户各自发布自己的部署，我们**改不了他们机器上的代码**。
        # 唯一能在不发版的情况下生效的，就是服务端下发的配置
        # （端点、头名、包 ID、免费档规则、默认值）。
        #
        # 而在此之前，客户端只在**启动时**拉一次配置，之后一直用缓存 ——
        # 你在服务端改了什么，客户不重启就永远看不到。心跳明明收到了
        # cfg_ver，却只读了 min_lic_ver 就把它丢了。
        #
        # 现在：服务端把 CFG_VER 加一，所有在线客户端在下一次心跳
        # （最长 12 小时，通常更早因为 Allowed() 会节流触发）自动换上新配置。
        # 这是「不改客户代码也能改行为」的唯一通道，必须接上。
        new_ver = int((d or {}).get("cfg_ver") or 0)
        if new_ver and new_ver != self._cfg_ver:
            log.info(
                "服务端配置版本变化 %s -> %s，重新拉取",
                self._cfg_ver, new_ver,
            )
            if self.refresh_config(raise_on_error=False):
                log.info("配置已自动更新到 v%s", new_ver)

        self._mark_ok()
        return code

    def refresh_config(self, raise_on_error: bool = True) -> bool:
        """拉一份新配置并解密。失败时保留上一份，不让服务变砖。"""
        digest = hashlib.sha256(self.hwid.encode()).hexdigest()[:32]
        try:
            b, code = self._post("/config", {"hwid": self.hwid, "sig": digest})
            if code != 200:
                raise RuntimeError(_decode_err(b, code))
            d = json.loads(b)
            cfg = open_config(
                d["body"], d.get("sig_hint", ""), d.get("salt", ""), int(d.get("version") or 1)
            )
            self._cfg = cfg
            self._cfg_ver = int(d.get("version") or 0)
            self._cfg_dirty = True       # 交给 guard 中间件做注入
            self._cfg_err = ""
            return True
        except Exception as e:
            self._cfg_err = f"{type(e).__name__}: {e}"
            log.warning("配置加载失败：%s", self._cfg_err)
            if raise_on_error:
                raise
            return False

    @property
    def config(self) -> dict[str, Any] | None:
        return self._cfg

    def _mark_ok(self) -> None:
        self._last_ok = _now()
        self._last_try = self._last_ok
        try:
            (self.dir / OK_FILE).write_text(str(self._last_ok), encoding="utf-8")
        except OSError:
            pass

    def _post(self, path: str, body: dict[str, Any]) -> tuple[bytes, int]:
        if self._tx is None:
            self._tx = LicenseTransport()
        return self._tx.post_signed(path, body, self.hwid)

    _tx: LicenseTransport | None = None


# ---------------------------------------------------------------- 单例

_GATE: Gate | None = None


def gate() -> Gate:
    if _GATE is None:
        raise RuntimeError("授权门尚未初始化")
    return _GATE


def init_gate(data_dir: Path) -> Gate:
    global _GATE
    g = Gate(data_dir)
    g._load()
    _GATE = g
    log.info(
        "gate: hwid=%s activated=%s exp=%s",
        g.hwid, g.verify(), g._payload.get("exp", 0),
    )
    return g
