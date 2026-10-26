"""配置：环境变量 + 站点知识（**源码里零字面量**）。

设计前提
--------
客户用 WorkBuddy 发布时，这个文件**就在客户机器上**。所以这里不能出现
任何上游域名、网关前缀、产品专有请求头名、订阅包 ID 的字面量 ——
出现了就等于写在客户能看到的文件里，加个 if 也拦不住
（`strings` 一把就出来，改个环境变量也能拿到）。

那么站点知识从哪来？两条路，都走 `workbuddy/remote.py:apply()`：

  客户模式（默认）  从授权服务器 `/config` 解密得到，密钥由本机 license 派生。
                    没激活 = 拿不到 = 常量保持空。
  自托管/开发       从 `_site/site-config.json` 读（**发布目录之外**，
                    由 `tools/gen_site_config.py` 生成，已加 .gitignore）。

两条路喂的是**同一个** `apply()`，所以两种模式下行为天然一致 ——
不会出现「自托管测试通过、客户那边某个头名拼错」这种分叉。

代价：入口必须唯一
------------------
不能用 `uvicorn app.main:app` 直接起 —— 那会跳过注入，所有端点为空。
统一走 `python workbuddy/entry.py`（见 workbuddy/README.md）。

未注入时会怎样
--------------
不会静默用一个空端点去发请求（那会得到一堆莫名其妙的 400/401），
`require_site_config()` 会给出明确的中文错误。这比"看起来能跑但全错"好得多。
"""
from __future__ import annotations

import os
import secrets
from functools import lru_cache
from pathlib import Path

# ---------------------------------------------------------------- 站点知识
# 默认**全空**。真值由 workbuddy/remote.py 在 app.main 加载前注入。
# 这里只声明名字，不留值 —— 这是整个方案的地基，不要为了让某个单测好写而填内容。

AUTH_BASE = ""
OIDC_DISCOVERY = ""
AUTHORIZE = ""
TOKEN = ""
REGISTER = ""
REVOKE = ""
REVOKE_LOGOUT = ""
USERINFO = ""

API_BASE = ""
GW = ""

CATALOG = ""
CREDITS = ""
FEATURES = ""
ME = ""
USER_SHOW = ""
PENDING_USES = ""

WAN3_WORKFLOW = ""
MODELS_WORKFLOW = ""

TOUCHPOINT = ""
TOUCHPOINT_REFERRER = ""
PACKAGE_ID_BY_TIER: dict[str, str] = {}

UA = ""
BROWSER_HEADERS: dict[str, str] = {}
STATIC_HEADERS: dict[str, str] = {}
DRIVE_HEADERS: dict[str, str] = {}

# 头名本身也是知识（`x-touchpoint` 这几个字就是答案的一半）
H_ACCEPT = ""
H_DEVICE = ""
H_PACKAGE = ""
H_TOUCH = ""
H_TOUCH_REF = ""
H_UA = ""
H_APP_AUTH = ""
H_AUTH = ""
AUTH_PREFIX = ""
H_COOKIE = ""

# 应用级令牌只能来自环境变量（它是个凭证，不是站点知识）
APP_AUTHORIZATION = os.environ.get("PICSART_POOL_APP_AUTH", "")

FREE_RESOLUTIONS: tuple[str, ...] = ()
PAID_RESOLUTIONS: tuple[str, ...] = ()
ALL_RESOLUTIONS: tuple[str, ...] = ()
RATIOS: tuple[str, ...] = ()

# duration 的结构（min/max/auto）在注入前给一个安全默认，避免 range() 之类炸掉。
# 注意：这些**不是**站点知识 —— 2~30 是任何视频模型都有的常识，不构成泄漏。
DURATION_MIN = 2
DURATION_MAX = 30
DURATION_AUTO = -1
DURATIONS = tuple(range(DURATION_MIN, DURATION_MAX + 1))
DURATION_WITH_VIDEO_REF: tuple[int, ...] = ()

MEDIA_TYPES: tuple[str, ...] = ()
MEDIA_MAX = 20
MEDIA_LIMITS: dict[str, int] = {}

# ---- 素材上传（2026-10-26 新增）----
#
# 背景：media[].url 必须是上游能抓取的公网地址。原来完全靠调用方自备，
# 客户做图生视频得先自己找图床，很难用。
#
# 实测（2026-10-26，用网页端 HAR 里的头直接打）：
#   POST <UPLOAD_BASE>/v2/files  multipart{file, type=editing-temp}
#   -> {"status":"success","response":{"url":"https://gcdn.../<uuid>.png"}}
#   0.35s，216 字节响应，**不需要用户 token**，只要应用令牌 + deviceid。
#
# 所以上传可以和号池完全解耦：不占用户授权额度、不碰 auth 端点。
#
# 端点与头全部来自配置 —— 和 login/media 一样，不在这里写字面量。
UPLOAD_BASE = ""            # 如 https://upload.picsart.com
UPLOAD_PATH = ""            # 如 /v2/files
UPLOAD_TYPE = ""            # 表单里的 type 字段，如 editing-temp
UPLOAD_FILE_FIELD = "file"  # 文件字段名
UPLOAD_TYPE_FIELD = "type"  # type 字段名
UPLOAD_MAX_BYTES = 0        # 单文件上限，0 = 不检查（由上游校验）
FRAME_TYPES: tuple[str, ...] = ()
REFERENCE_TYPES: tuple[str, ...] = ()
REF_VIDEO_AUDIO_TOTAL_MAX = 15

WORKFLOW_KIND: dict[str, str] = {}
MEDIA_KINDS: dict[str, str] = {}
PRIME_SUFFIX = ""
MODEL_ALIASES: dict[str, str] = {}
# 文件名 slug 的兜底词（prompt 全是标点时用）。也是站点知识的一部分，
# 所以同样来自配置，不写死。
MODEL_SLUG = ""
STATUS_VALUES: dict[str, str] = {}
FEATURE_ID_FORMAT = ""
DRIVE_OPTIONS: dict = {}
MESSAGES: dict[str, str] = {}
SUCCESS_CODES: tuple[int, ...] = (200, 201, 202)
FRAME_EXCLUSIVE = True
DEFAULTS: dict = {}

DEFAULT_SCOPE = ""


class SiteConfigMissing(RuntimeError):
    """站点配置尚未注入 —— 通常意味着没激活，或启动方式绕过了 entry.py。"""


def require_site_config() -> None:
    """在真正要打上游之前调用，给出能照做的错误。

    为什么需要显式检查：常量都是空串时，代码不会崩 —— 它会拼出
    `"/workflows//submit"` 这种 URL，然后拿到一个看不懂的 400。
    那种错误会把排查方向带到"上游改了接口"，而不是"没激活"。
    """
    if not GW or not WAN3_WORKFLOW:
        raise SiteConfigMissing(
            "站点配置未加载：可能是尚未激活，或启动方式不对。\n"
            "  · 客户部署：打开首页输入激活码（或访问 /activate）\n"
            "  · 自托管：确认 _site/site-config.json 存在，且用 workbuddy/entry.py 启动"
        )


# ---------------------------------------------------------------- 运行时设置

def _bool(name: str, default: bool = False) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


class Settings:
    def __init__(self) -> None:
        self.data_dir = Path(os.environ.get("PICSART_POOL_DATA", "./data"))
        self.host = os.environ.get("PICSART_POOL_HOST", "0.0.0.0")
        self.port = int(os.environ.get("PICSART_POOL_PORT", "8000"))

        # 对外地址。**不要读这个字段做判断** —— 请用 property `public_url`，
        # 它会优先返回从 `X-Forwarded-*` 推导的请求级值。
        #
        # 为什么：WorkBuddy 的公网域名是发布时随机生成的，无法写进环境变量
        # （沙箱也不支持注入自定义环境变量）。写死的话 OAuth 的 redirect_uri
        # 会算成 localhost，加号必然失败。
        self._env_public_url = os.environ.get("PICSART_POOL_PUBLIC_URL", "").rstrip("/")

        self.admin_password = os.environ.get("PICSART_POOL_ADMIN_PASSWORD", "")
        self.session_secret = os.environ.get("PICSART_POOL_SESSION_SECRET", "")

        self.api_keys = [
            k.strip()
            for k in os.environ.get("PICSART_POOL_API_KEYS", "").split(",")
            if k.strip()
        ]

        # 以下默认值会被注入的配置覆盖（见 workbuddy/remote.py）。
        # 先给中性值，避免注入前 AttributeError。
        self.default_resolution = os.environ.get(
            "PICSART_POOL_DEFAULT_RESOLUTION", ""
        ).upper()
        self.default_ratio = os.environ.get("PICSART_POOL_DEFAULT_RATIO", "")
        self.default_duration = int(os.environ.get("PICSART_POOL_DEFAULT_DURATION", "5"))
        self.default_model = os.environ.get("PICSART_POOL_DEFAULT_MODEL", "")
        self.default_audio = _bool("PICSART_POOL_DEFAULT_AUDIO", False)

        self.poll_interval = float(os.environ.get("PICSART_POOL_POLL", "3"))
        # 单任务最长生命周期。实测 5s 视频可能跑 20 分钟以上（排队严重时见过 23 分钟），
        # 1200s（20 分钟）会把正常的长任务误判成超时。
        self.task_timeout = float(os.environ.get("PICSART_POOL_TIMEOUT", "7200"))
        self.max_concurrent_tasks = int(
            os.environ.get("PICSART_POOL_MAX_TASKS", "200")
        )

        # 免费档被 402 时自动尝试的降档顺序（会被注入的配置覆盖）
        self.free_fallback: list[str] = []

        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._ensure_secret()

    @property
    def db_path(self) -> Path:
        return self.data_dir / "pool.db"

    @property
    def public_url(self) -> str:
        """对外地址。优先请求上下文推导值，其次环境变量，最后 localhost。

        顺序不能反：沙箱里环境变量是空的（也无法注入），唯一来源就是
        `X-Forwarded-*`。自有服务器上两者一般一致，谁优先都行。
        """
        from workbuddy.puburl import current_public_url

        ov = current_public_url()
        if ov:
            return ov
        if self._env_public_url:
            return self._env_public_url
        return f"http://localhost:{self.port}"

    @property
    def secret_file(self) -> Path:
        return self.data_dir / "session.secret"

    def _ensure_secret(self) -> None:
        """session secret 落盘持久化，容器重启后登录态不失效。"""
        if self.session_secret:
            return
        if self.secret_file.exists():
            self.session_secret = self.secret_file.read_text("utf-8").strip()
            return
        self.session_secret = secrets.token_urlsafe(48)
        self.secret_file.write_text(self.session_secret, encoding="utf-8")
        try:
            self.secret_file.chmod(0o600)
        except Exception:
            pass

    @property
    def redirect_uri(self) -> str:
        return f"{self.public_url}/oauth/callback"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
