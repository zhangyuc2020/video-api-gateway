"""auth 端点的全局限流与退避。

为什么单独抽一个模块
--------------------
`auth.picsart.com/api/oauth2/token` 在 Cloudflare 后面，限流规则的计数特征是
**数据中心 ID + IP**（见 Cloudflare 文档「Request rate calculation」：
每条规则强制带 `cf.colo.id`）。

这意味着：**我们所有账号共用同一个计数器**。1 个号刷 token 和 100 个号刷 token，
在 CF 眼里是同一个配额。所以 refresh 必须全局排队，不能各账号自己发。

实测过的教训
------------
· 一晚上对 token 端点打了约 70 次 -> Cloudflare Error 1015，IP 被临时封
· 官方文档明确：**不要在短时间内反复尝试，这会延长封禁**
  （所以我之前那个 `for attempt in range(30)` 是纯粹的错误）
· 响应头给 `retry-after: 60`，必须遵守
"""
from __future__ import annotations

import asyncio
import random
import time
from typing import Any


class TokenRateLimiter:
    """全局令牌桶 + 熔断。

    · 令牌桶：限制每秒 refresh 次数，100 个号排队而不是同时打
    · 熔断：一旦收到 1015/429，整个 auth 通道关闭一段时间（按 retry-after），
      期间所有 refresh 直接失败，**绝不重试** —— 重试会延长封禁
    """

    def __init__(self, rate: float = 1.0, burst: int = 2) -> None:
        # rate: 每秒允许的次数。60 次/分钟 = 1/s，对 100 号够用且安全
        self.rate = rate
        self.capacity = max(1, burst)
        self._tokens = float(self.capacity)
        self._last = time.monotonic()
        self._lock = asyncio.Lock()

        # 熔断状态：_blocked_until 之前，所有 refresh 直接拒绝
        self._blocked_until = 0.0
        self._block_reason = ""

    # ------------------------------------------------------------ 熔断

    @property
    def blocked(self) -> bool:
        return time.monotonic() < self._blocked_until

    def blocked_for(self) -> int:
        return max(0, int(self._blocked_until - time.monotonic()))

    def block(self, seconds: float, reason: str = "") -> None:
        """熔断。按服务端给的 retry-after 精确等待。"""
        until = time.monotonic() + max(1.0, seconds)
        if until > self._blocked_until:
            self._blocked_until = until
            self._block_reason = reason or "auth 端点限流"

    def block_from_response(self, retry_after: str | None, status: int) -> float:
        """从 429 响应里解析 retry-after 并熔断。

        Cloudflare 的 1015 页面固定带 `retry-after`，没有就保守按 60s。
        """
        secs = 60.0
        if retry_after:
            try:
                secs = float(str(retry_after).strip())
            except (TypeError, ValueError):
                secs = 60.0
        secs = max(5.0, min(secs, 3600.0))
        self.block(secs, f"HTTP {status}")
        return secs

    # ------------------------------------------------------------ 令牌桶

    async def acquire(self) -> None:
        """取一个令牌；桶空就等。熔断期间直接抛。"""
        if self.blocked:
            raise TokenBlocked(self.blocked_for())

        async with self._lock:
            while True:
                if self.blocked:
                    raise TokenBlocked(self.blocked_for())
                now = time.monotonic()
                elapsed = now - self._last
                self._last = now
                self._tokens = min(
                    float(self.capacity), self._tokens + elapsed * self.rate
                )
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                # 不够一个令牌，等够为止
                need = (1.0 - self._tokens) / self.rate
                await asyncio.sleep(min(need, 5.0))


class TokenBlocked(RuntimeError):
    """auth 通道处于熔断期。"""

    def __init__(self, retry_in: int):
        self.retry_in = retry_in
        super().__init__(f"auth 端点限流中，约 {retry_in}s 后恢复")


# 进程级单例
_limiter = TokenRateLimiter()


def limiter() -> TokenRateLimiter:
    return _limiter


def jitter(ahead: int = 300, spread: int = 600) -> int:
    """给刷新时机加随机抖动，避免所有账号同时到期。

    100 个号如果都在启动后同一秒到期，就会同一秒发 100 个 refresh，
    直接撞上同一个 CF 计数器。抖动把到期时间摊开。

    返回：提前多少秒刷新（ahead + 0~spread 之间的随机值）
    """
    return ahead + random.randint(0, max(0, spread))
