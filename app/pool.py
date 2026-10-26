"""号池调度 —— 队列制 + SSE，面向几十到上百个账号。

核心规则
--------
上游每账号并发上限 = 1（limit:1, label:user_concurrency，按 JWT sub 绑死）。
所以只需要一条规则：

    **一个账号在它手上的任务真正结束之前，不接下一个任务。**

实现：每账号一把 asyncio.Lock；任务结束才释放。锁被占时新任务排队等锁。

为什么没有"冷却"，也没有"后台探测"
----------------------------------
两条都是基于错误假设加进来的，已经删掉：

· 冷却 —— 猜"等 90 秒大概够了"。猜错就误杀可用账号。
  队列是确定的：锁在 = 忙，锁空 = 闲。

· 后台全量探测 —— 当初是为了发现"网页端占用名额"。
  但号池里的账号**不会上网页端跑任务**，所以上游不存在
  "本地看不见的第三方"。名额只可能被我们自己的任务占着，
  而本地锁精确记录着这一点。探测纯属多余。

  唯一的例外是"我们自己的任务在上游卡死了"。这种情况交给
  ① 任务生命周期上限（TASK_MAX_LIFETIME）
  ② SSE 连接断开即感知
  两个机制处理，比轮询戳探测干净。

并发模型（重要）
----------------
HTTP 轮询模式下，常驻连接数 ≈ 0（每次请求 200ms 就断）。
改用 SSE 后，一个任务 = 一条持续数分钟的长连接，
100 个任务跑满 = 100 条常驻连接。

实测（连 api.picsart.com）：单连接约 232 KB 内存 + 1 个句柄，
100 条约 23 MB / 100 句柄。内存不是问题，**句柄上限**才是，
容器里需要 ulimit -n 调高（参考：账号数 × 5）。
"""
from __future__ import annotations

import asyncio
import collections
import time
from typing import Any

from . import authlimit, oauth, store, upstream
from .config import FREE_RESOLUTIONS, PAID_RESOLUTIONS, settings

REFRESH_AHEAD = 300          # 基础提前量：token 剩这么久时该续了
# 抖动范围。**不能太大**：access_token 只有 3600s 寿命，
# 如果抖动上限也接近 900s，阈值会到 1200s，等于 token 一到手就该刷，
# 反而增加了 auth 端点压力。500s 足够把 100 个号摊开（分布见下），
# 又不至于让阈值超过寿命的 1/3。
REFRESH_SPREAD = 500
RATE_LIMIT_COOLDOWN = 300    # auth 熔断期间的兜底冷却（实际由 authlimit 控制）

# 跟任务的方式。**默认轮询，不用 SSE。**
#
# 为什么放弃 SSE（实测结论，不是猜）：
#   · 上游任务要 15~23 分钟才出片
#   · SSE 是单个 HTTP 请求，Cloudflare 等 100 秒没响应头就返回 524 掐断
#   · 被掐后任务仍在上游跑、占着名额，但我们**拿不到它的 id**，
#     既查不了状态也取消不了 —— 名额被一个看不见的任务锁死
#
# 轮询没有这个问题：submit 立刻返回 id，之后用 id 查状态，等多久都行。
# 请求数是唯一代价，但实测 100 条并发连接只占 23MB 内存，不是瓶颈。
USE_SSE = False

# 单任务最长生命周期：2 小时。
# 超过就判失败并释放锁 —— 否则一个卡死的任务会永久占住账号。
TASK_MAX_LIFETIME = 7200.0

# 429 之后的重试退避（秒）。指数增长，上限 60 秒。
RETRY_BACKOFF_BASE = 2.0
RETRY_BACKOFF_MAX = 60.0
# 当上游通过 retry-after 明确要求更长的等待时，允许超过 RETRY_BACKOFF_MAX，
# 但仍兜一个硬上限 —— 防止解析异常把某个号锁死一整天。
RETRY_AFTER_CAP = 1800.0

# 「等上游名额」的上限。
#
# 必须**远小于** TASK_MAX_LIFETIME。串行等名额时任务抱着账号锁，
# 这段时间账号干不了别的；等太久等于用一把锁把整个号锁死，
# 而外部只能看到「排队中」，完全看不出是在空转。
#
# 45s / 最多 3 次 submit：实测 429 退避 7 次要 182 秒，每次都真的打到
# Cloudflare —— 那只会把 IP 往 1015 上推。3 次足够判定"这个号此刻不能用"，
# 再多一次也不会改变结论。放弃后把号交给 _probe_loop 重新探测，
# 比让任务抱着锁反复撞干净得多。
SUBMIT_WAIT_MAX = 45.0
SUBMIT_MAX_TRIES = 3

# ---- 「号主动宣告可用」的时序参数 ----
#
# PROBE_INTERVAL：后台多久扫一轮空闲号。
# PROBE_TTL     ：同一个号多久内不重复打上游 options（节流）。
#
# ⚠️ PROBE_TTL 从 10s 改成 60s（2026-10-25）。
#
# 为什么改 —— 这是 Cloudflare 1015 的真正来源，不是 token 续期：
#
#   auth 续期：3 个号 × 每 25 分钟一次 ≈ 8.9 次/小时
#   options 探测：3 个号 × 每 10 秒一次 ≈ 1080 次/小时   ← 差 121 倍
#
# 客户现场就是 3 个号，一眼看去"待续期"以为是 token 问题，
# 实际是探测把 Cloudflare 的 IP 计数器打满了，然后续期被连带限流。
#
# 为什么调大 TTL 不影响派发速度（这一点必须确认过才敢改）：
#   任务真正要拿号时走的是 `probe_slot(force=True)`（见 _try_claim），
#   它**绕过 TTL 直接探测**。所以 TTL 只影响"后台预热"的刷新频率，
#   不影响"有任务来了"的响应速度。
#
# 代价：号从"忙"变"闲"时，后台最多晚 60s 才发现（以前 10s）。
# 但上游名额释放本身就要几秒到几十秒，而且排队的任务会 force 探测，
# 所以实际感知差异很小。
PROBE_INTERVAL = 5.0
PROBE_TTL = 60.0

# RELEASE_GRACE：任务结束后多久内**不许**宣告这个号可用。
#
# 这是必须的：本地锁一释放，上游并发名额往往还没放开（实测晚几秒到几十秒）。
# 没有这个宽限，队列头任务会被立刻唤醒、抢号、submit → 429 ——
# 白排队 20 分钟后拿到号却失败，实测撞了好几次。
# 宽限内即使探测说"空着"也不宣告，等它真的放干净。
RELEASE_GRACE = 8.0


def _credential_dead(account: dict[str, Any]) -> bool:
    """这个号的 refresh_token 是不是已经**确认失效**了。

    判据：status=dead 且 last_error 指向 invalid_grant / invalid_client。
    这两种错误意味着 refresh_token 本身废了（或被别的 client 作废），
    再刷也不会好 —— 必须停手等人工重新授权，否则就是拿 auth 端点
    当沙袋打，迟早把 IP 打进 Cloudflare 1015。
    """
    if account.get("status") != "dead":
        return False
    err = str(account.get("last_error") or "")
    return "invalid_grant" in err or "invalid_client" in err


class NoCapacity(RuntimeError):
    pass


class Pool:
    _instance: "Pool | None" = None

    def __init__(self) -> None:
        self._locks: dict[int, asyncio.Lock] = {}
        self._guard = asyncio.Lock()
        self._refresh_locks: dict[int, asyncio.Lock] = {}
        self._busy: dict[int, int] = {}
        # 累计完成任务数，用于轮转（防止单个号被反复复用）
        self._done: dict[int, int] = {}
        # 429 后的冷却：只针对"这个号刚发现名额不可用"，
        # 是任务级的短退避，不是账号级封禁。
        self._retry_after: dict[int, float] = {}

        # ---- 「号主动宣告可用」机制 ----
        #
        # 旧模型：任务抢到账号锁 → 撞 429 → 抱着锁退避重试。问题是一个被上游
        # 占着名额的号会把任务连人带锁一起拖住，外面还只能看到「排队中」。
        #
        # 新模型：**账号自己负责宣告**。空闲的号定期探一下上游名额
        # （options 的 rateLimitVerdict，只判定不真提交），确认真的能接活，
        # 才把自己挂到 ready 集合里，并唤醒等待者。任务只从 ready 里取号 ——
        # 拿到的号是**已经验证过能提交**的，所以几乎不会再撞 429，
        # 也就不需要抱着锁退避。
        self._ready: set[int] = set()
        self._ready_at: dict[int, float] = {}      # 上次确认可用的时刻
        self._slot_probe: dict[int, float] = {}     # 上次探测上游名额的时刻
        self._slot_free: dict[int, bool] = {}        # 上次探测的**真实结论**
        self._released_at: dict[int, float] = {}     # 上次任务结束的时刻（宽限用）

        # ---- pending_ready：已确认空闲、但还在宽限期内的号 ----
        #
        # 结构：aid -> 可以宣告的时刻（released_at + RELEASE_GRACE）
        #
        # 为什么不直接丢（对齐 Go 版 client/pool.go:pendingReady）：
        #   探测一次要打上游 options，**消耗 Cloudflare 配额**。好不容易探到
        #   "号空了"，只因为还差几秒宽限就把结论扔掉、等下一轮重新探一遍，
        #   既浪费一次请求又让任务白等一个 PROBE_INTERVAL。
        #
        # 现实代价：authlimit.py 顶部记着实测教训——"一晚上约 70 次
        # 就 Cloudflare 1015 封 IP"。号池密集时每轮多探一次，
        # 正好会把配额推过那条线。
        self._pending_ready: dict[int, float] = {}

        # ---- slot_verdict：每号最近一次探测的结论（诊断用）----
        #
        # {aid: {"state": "free"|"busy"|"unknown", "at": ts, "err": str}}
        #
        # 为什么单独存一份（对齐 Go 版 client/pool.go:slotVerdict）：
        #   probe_slot 的返回值是**瞬时**的（当场用掉就没了），而排查
        #   "这个号为什么一直不能用"时，需要的恰恰是"它现在被判成什么、
        #   什么时候判的、为什么探不了"。
        #   没有它，界面上只能看到 ready 为空，看不出原因——
        #   而「探不了（本地网络）」和「上游说满」的处理方式完全不同。
        self._slot_verdict: dict[int, dict[str, Any]] = {}

        # 唤醒用的 Event。**延迟到首次使用时创建**：
        # asyncio.Event 一创建就绑定当时的事件循环，之后跨循环复用会抛
        # "is bound to a different event loop"。Pool 是进程级单例，
        # 在 uvicorn --reload / 多 worker / 测试里会被几个循环先后碰到，
        # 提前在模块导入期建好必然踩这个坑。
        self._ready_event: asyncio.Event | None = None
        self._ready_event_loop: Any = None

        # ---- FIFO 等待队列（号池按 100 号、上千排队任务设计）----
        #
        # 每个等待 acquire 的协程挂一个 Future，按到达顺序排队。
        # 号宣告可用时 **只唤醒队首**（按序派发），其余继续睡着。
        #
        # 为什么必须这样：如果每个等待者都自己轮询 ready 集合，1000 个协程
        # 会同时被唤醒、同时发现同一个可用号、同时去抢锁 —— 999 个失败方
        # 各自再循环一轮，CPU 直接空转（惊群）。排队后只有一个协程被唤醒，
        # 且顺序公平，不会出现"排在后面的号永远抢不到"。
        self._waiters: "asyncio.Deque[tuple[asyncio.Future, float]]" = collections.deque()

    def _event(self) -> asyncio.Event:
        """取与本事件循环绑定的唤醒 Event（必要时重建）。

        跨循环时旧 Event 不可用 —— 重建一个即可，因为 Event 只承担
        "有新号可用"这一个信号，丢掉一次唤醒没有副作用（等待方超时后会
        重新扫一遍 ready 集合）。
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if self._ready_event is None or self._ready_event_loop is not loop:
            self._ready_event = asyncio.Event()
            self._ready_event_loop = loop
        return self._ready_event

    @classmethod
    def get(cls) -> "Pool":
        if cls._instance is None:
            cls._instance = Pool()
        return cls._instance

    async def _lock_for(self, aid: int) -> asyncio.Lock:
        async with self._guard:
            if aid not in self._locks:
                self._locks[aid] = asyncio.Lock()
            return self._locks[aid]

    async def _refresh_lock_for(self, aid: int) -> asyncio.Lock:
        async with self._guard:
            if aid not in self._refresh_locks:
                self._refresh_locks[aid] = asyncio.Lock()
            return self._refresh_locks[aid]

    # ---------------------------------------------------------- 观测

    def stats(self) -> dict[str, Any]:
        now = time.time()
        return {
            "busy": {k: v for k, v in self._busy.items() if v},
            "accounts": len(self._locks),
            "total_done": sum(self._done.values()),
            "in_backoff": [aid for aid, t in self._retry_after.items() if t > now],
        }

    # ---------------------------------------------------------- token 续期

    @staticmethod
    def _need_refresh(aid: int, ttl: float) -> bool:
        """判断是否该刷新。ttl = token 剩余秒数。

        规则（由两个真实事故倒推出来）：

        · 抖动：基础阈值 = 300 + (id 派生偏移 0~900)，100 个号会落在
          300~1200s 的不同时刻，不会同时打 auth 端点。

        · 不误判：阈值固定，不跟 ttl 联动。
          曾经错写成 min(want, ttl*0.5)，结果阈值随 ttl 一起缩，
          ttl 再小也永远 > 阈值 —— 永远不刷，一直用到彻底过期。
          更早还错写成 REFRESH_AHEAD + offset 不设上限，导致 token 还剩
          922 秒（明明够用）就被判"快过期"去刷，把 IP 打成 429。

        · 兜底：ttl 小于 60 秒时无条件刷，避免卡在过期边缘。
        """
        if ttl <= 60:
            return True
        offset = (aid * 137) % max(1, REFRESH_SPREAD)
        return ttl <= REFRESH_AHEAD + offset

    async def ensure_fresh(self, account: dict[str, Any]) -> dict[str, Any]:
        """access_token 快过期就用 refresh_token 续。

        只做 token 刷新，不持有账号的执行锁——避免"刷新要几秒，
        期间该账号被锁死、其他请求排队"的问题。

        刷新时机带随机抖动：100 个号如果都在同一秒到期，
        就会同一秒发 100 个 refresh，撞上同一朵 Cloudflare 计数器。
        抖动把到期时间摊开到 REFRESH_SPREAD 秒内。

        ⚠️ 抖动只该「往后延」，不该「往前提前」：
        之前算成 `REFRESH_AHEAD + offset`（offset 最大 900），
        阈值就变成 300+822=1122 秒 —— token 还剩 922 秒明明够用，
        却被判成"快过期"去刷新，白打 auth 端点，还把 IP 打成 429。
        现在改成：抖动只从 REFRESH_AHEAD 到 REFRESH_AHEAD+spread 里取，
        且**绝不能超过 token 实际寿命的一半**。
        """
        aid = account["id"]
        ttl = account.get("expires_at", 0) - time.time()
        if ttl > 0 and not self._need_refresh(aid, ttl):
            return account

        rt = account.get("refresh_token") or ""
        if not rt:
            if account.get("cookie"):
                return account
            store.update_account(aid, status="dead", last_error="no refresh_token")
            raise NoCapacity(f"{account['name']}: 无 refresh_token，需重新登录")

        # 凭证已确认失效：不再打 auth 端点。
        # 后台循环和任务路径都可能调到这，没有这个判断的话一个死号
        # 会每 6 秒刷一次、永远 400，纯粹是把 IP 往 1015 上推。
        if _credential_dead(account):
            raise NoCapacity(
                f"{account['name']}: refresh_token 已失效（invalid_grant），需重新授权"
            )

        lock = await self._refresh_lock_for(aid)
        async with lock:
            fresh = store.get_account(aid)
            if fresh:
                fttl = fresh.get("expires_at", 0) - time.time()
                if fttl > 0 and not self._need_refresh(aid, fttl):
                    return fresh      # 等锁期间已被别人刷过
            try:
                # refresh_and_persist 内部保证「刷新成功 = 已落库」，
                # 因为 refresh_token 是单次使用的，漏写就等于丢号。
                return await oauth.refresh_and_persist(fresh or account)
            except oauth.AuthError as e:
                if e.error == "rate_limited":
                    # 熔断由 authlimit 统一管理，这里只标冷却，不算账号失败
                    blocked = authlimit.limiter().blocked_for()
                    store.update_account(
                        aid,
                        cooldown_until=time.time() + max(60, blocked or RATE_LIMIT_COOLDOWN),
                        last_error=str(e.detail or "auth 限流"),
                    )
                    raise NoCapacity(f"{account['name']}: auth 限流，约 {blocked}s 后恢复") from e

                if e.error == "persist_failed":
                    # 写库失败：token 可能已经轮换掉了，标记需要人工处理
                    store.update_account(
                        aid, status="dead",
                        last_error="刷新成功但未落库，需重新授权",
                    )
                    raise NoCapacity(f"{account['name']}: {e.detail}") from e

                fails = int((fresh or account).get("fails", 0)) + 1
                # 只有 invalid_grant 这类"凭证真失效"才快速判死。
                # 网络抖动 / 5xx / 限流都会走到这里，但它们**不是**凭证问题 ——
                # 3 次就判死的话，一次 CF 抖动就能把好号埋了，
                # 而号其实还好着，只是暂时问不通。
                credential_dead = e.error in ("refresh_failed",) and isinstance(
                    e.detail, dict
                ) and e.detail.get("error") in ("invalid_grant", "invalid_client")
                limit = 3 if credential_dead else 20
                store.update_account(
                    aid,
                    fails=fails,
                    last_error=f"refresh: {e.error}"
                               + (f" ({e.detail.get('error')})" if credential_dead else ""),
                    status="dead" if fails >= limit else (fresh or account).get("status", "ok"),
                )
                raise NoCapacity(f"{account['name']}: refresh 失败 {e.error}") from e

    # ---------------------------------------------------------- 调度

    def _candidates(self) -> list[dict[str, Any]]:
        """**真正可用的**账号，按"该谁干活"排序。

        先过滤，再排序。之前只排序不过滤，`dead` 和冷却中的号照样每轮被
        `_try_claim` 试一次，每次都触发一次 refresh —— 吃的是全局令牌桶
        （1 次/秒），10 个坏号就能把整池的续期通道堵死，正是 authlimit 注释里
        那条「越刷越封、越封越刷」的 1015 死亡螺旋。

        过滤条件（任一命中即排除）：
          · 无 access_token 且无 cookie —— 没有任何凭证
          · status == 'dead' —— 已经被判死（refresh 连续失败 / 人工登出）
          · cooldown_until > now —— auth 限流冷却期（以前写了却从没人读）

        排序键（依次）：
          1. 现在是否空闲 —— 忙的排后面
          2. 是否在 429 退避中 —— 退避的排后面
          3. 累计完成任务数 —— **少的优先**（轮转，防止单号被反复复用）
          4. 有效期（越晚越好）

        第 3 条是关键：只按"空闲"排序时，先排到的账号一旦空出来就又被抓去干活，
        导致 10 个号里 9 个各跑 1 次、剩下 1 个跑 41 次。加计数后自然轮转。
        """
        now = time.time()
        out = []
        for a in store.list_accounts():
            if not (a.get("access_token") or a.get("cookie")):
                continue
            if a.get("status") == "dead":
                continue
            # free 档账号没有 wan3 权限（2026-10-26 加）。
            #
            # 之前不筛档位：free 号会被选中、提交、然后被上游拒绝，
            # 白白浪费一次调度，而且错误信息是上游的（看起来像"任务失败"
            # 而不是"这个号没权限"）。空 tier 不放行 —— 理由见
            # store.account_usable 的注释（查不到 ≠ free）。
            if store.tier_blocked(a):
                continue
            if (a.get("cooldown_until") or 0) > now:
                continue
            out.append(a)

        def key(a: dict[str, Any]) -> tuple:
            aid = a["id"]
            lock = self._locks.get(aid)
            busy = bool(lock and lock.locked())
            cooling = self._retry_after.get(aid, 0) > now
            return (
                busy,
                cooling,
                self._done.get(aid, 0),
                -(a.get("expires_at") or 0),
            )

        out.sort(key=key)
        return out

    # ---------------------------------------------------- 号主动宣告"我能接活"

    def _announce_ready(self, aid: int) -> None:
        """账号确认可接活 —— 挂进 ready 集合，并**按序唤醒一个**等待者。

        这是「号主动告知」的落点。只有两种情况会调用：
          · 后台探测器探到 overLimit=False（_probe_loop）
          · acquire 里的即时探测确认空闲
        没被宣告的号，任务不会去拿 —— 这样任务就不会拿着锁去撞 429。

        ⚠️ 只唤醒队首，不是所有等待者。
        1000 个协程同时被 set() 唤醒会同时发现同一个可用号、同时抢锁，
        999 个失败方各跑一轮循环 —— CPU 空转（惊群），且长任务会被
        反复排在后面。按 FIFO 唤醒队首，既省 CPU 又保证顺序公平。

        ⚠️ 释放宽限（RELEASE_GRACE）：任务刚结束的号，即使探测说空闲
        也不宣告 —— 上游名额释放比本地锁慢，立刻宣告必撞 429。

        ⚠️ 但**结论不丢**：记进 _pending_ready，到点由
        _flush_pending_ready 兑现。丢掉的话，下一轮还得重新探一遍上游，
        白烧一次 Cloudflare 配额（对齐 Go 版 pool.go:announceReady）。
        """
        now = time.time()
        released = self._released_at.get(aid, 0)
        if released and now - released < RELEASE_GRACE:
            # 探测结论有效，只是还早。记下来，到点再宣告。
            self._pending_ready[aid] = released + RELEASE_GRACE
            return
        self._pending_ready.pop(aid, None)
        self._ready.add(aid)
        self._ready_at[aid] = now
        self._dispatch()

    def _flush_pending_ready(self) -> int:
        """把"宽限期已到"的待宣告号真正放进 ready 并派活。

        由 _probe_loop 每轮调用（跟着 PROBE_INTERVAL 走）。
        注意它的作用不是"更频繁地探测"，而是"把已经探到的结论及时兑现" ——
        号在宽限期内已经确认空闲，到点就该立刻可用，没理由再等一轮探测。

        返回本次兑现了几个号（供日志/测试观察）。
        """
        if not self._pending_ready:
            return 0
        now = time.time()
        n = 0
        for aid in list(self._pending_ready):
            at = self._pending_ready[aid]
            if now < at:
                continue                    # 还没到点
            del self._pending_ready[aid]
            # 号可能在这期间又被占上了（锁被拿）或变 dead ——
            # 那些情况由 _withdraw_ready / _candidates 过滤处理，
            # 这里只负责"兑现"。
            self._ready.add(aid)
            self._ready_at[aid] = now
            n += 1
        if n:
            # 用 _dispatch_all 而不是 _dispatch（2026-10-25 修）：
            # 这里可能一次兑现多个号，而 _dispatch 一次只唤醒一个等待者。
            # 只调一次的话，N 个号里只有 1 个被派出去，其余留在 ready 里
            # 干等下一轮探测 —— 客户看到的就是"有号却派不出去"。
            self._dispatch_all()
        return n

    def _next_pending_wake(self) -> float:
        """最近一个待宣告号还要等多久（秒）。0 = 没有待宣告的号。

        为什么要它：PROBE_INTERVAL 是 5 秒，RELEASE_GRACE 是 8 秒 ——
        如果只在每轮探测时 flush，宽限期到点最多还要再等 5 秒才兑现。
        拿这个值把 sleep 调短，就能在"宽限期刚过"那一刻准时宣告，
        而不是等下一个节拍（对齐 Go 版 pool.go:nextPendingWake）。
        """
        if not self._pending_ready:
            return 0.0
        earliest = min(self._pending_ready.values())
        d = earliest - time.time()
        return max(d, 0.001)          # 已到点也要给个极小值，别返回 0


    def _dispatch(self) -> None:
        """把可用号按 FIFO 顺序派给等待者。

        **每调一次只唤醒一个等待者**（这是刻意的，见下），但会先清掉
        所有已失效的等待者。

        为什么一次只唤醒一个：
          1000 个协程同时被 set() 唤醒会同时发现同一个可用号、同时抢锁，
          999 个失败方各跑一轮循环 —— CPU 空转（惊群），且长任务会被
          反复排在后面。按 FIFO 只唤醒队首，既省 CPU 又保证顺序公平。

        ⚠️ 那"多个号 + 多个等待者"怎么办（2026-10-25 修）：
          一次只派一个，剩下的号留在 ready 里等下一次 _dispatch。
          所以**调用方必须能可靠地反复调用**，否则就会出现
          「ready 里有号 + 有任务在排，但没人派」的死锁。

          现在的保障有三层：
            · _probe_loop 每轮开头无条件调一次
            · _flush_pending_ready 兑现后调一次
            · _announce_ready 宣告后调一次
          而每轮的探测周期是 PROBE_INTERVAL(5s)，所以最坏情况
          多等 5 秒就能再派出一个 —— 不会无限期卡住。
        """
        while self._waiters:
            fut, deadline = self._waiters[0]
            if fut.done():
                # 等待方已经不等了（超时/取消），摘掉继续看下一个
                self._waiters.popleft()
                continue
            if time.time() >= deadline:
                # 已超时：摘掉，让它在自己的协程里报错
                self._waiters.popleft()
                continue
            if not self._ready:
                return              # 没有可用号，等待者继续排队
            fut.set_result(True)
            self._waiters.popleft()
            return

    def _dispatch_all(self) -> int:
        """反复派发直到派不动为止。返回本次派出去几个。

        专供"一次涌入多个号"的场景（比如 _flush_pending_ready 兑现了
        一批待宣告号）。普通路径用 _dispatch 就够 —— 一次一个更公平。

        ⚠️ 不能简单地在 while 里调 _dispatch()：_dispatch 在
        `not self._ready` 时直接 return，没有"派不动了"的信号，
        循环会退化成无限重试同一个失败。所以这里显式判断条件。
        """
        n = 0
        while self._ready and self._waiters:
            before = len(self._waiters)
            self._dispatch()
            if len(self._waiters) == before:
                break               # 没有进展（全部失效/超时被摘除）
            n += 1
        return n

    def _withdraw_ready(self, aid: int, why: str = "") -> None:
        """把号从可用集合里撤下来（占用 / 名额被占 / 出错）。"""
        self._ready.discard(aid)
        self._ready_at.pop(aid, None)

    def ready_snapshot(self) -> dict[str, Any]:
        """给管理页看：号池实时状态（可用号 / 排队深度 / 忙碌号 / 探测结论）。

        多出的两列（对齐 Go 版 Pool.Stats）是排障的分水岭：

          pending_ready  已经探到空闲、正在等宽限期到点的号。
                         这个表非空说明"号其实是空的，只是还没到宣告时刻" ——
                         和"所有号都被占着"是完全不同的两种处境。

          verdict        每号最近一次探测的结论（free/busy/unknown + 时刻 + 原因）。
                         没有它，客户报"任务排队不动"时只能看到 ready 为空，
                         分不清是「上游满了」「本地网络探不了」还是「号 dead 了」。
        """
        now = time.time()
        return {
            "ready": sorted(self._ready),
            "ready_age": {
                aid: round(now - self._ready_at.get(aid, now), 1)
                for aid in self._ready
            },
            "busy": {k: v for k, v in self._busy.items() if v},
            "queue_depth": len(self._waiters),
            "pending_ready": {
                aid: round(at - now, 1)
                for aid, at in self._pending_ready.items()
            },
            "verdict": {
                aid: {
                    "state": v.get("state", "unknown"),
                    "at": round(now - float(v.get("at") or now), 1),
                    "err": v.get("err", ""),
                }
                for aid, v in self._slot_verdict.items()
            },
        }

    async def probe_slot(self, acct: dict[str, Any], *, force: bool = False) -> bool | None:
        """探一下这个号的上游并发名额是否空闲。

        `options` 端点的 rateLimitVerdict 只做判定、不真提交任务，
        所以探测是**无副作用**的 —— 不会白开任务、不占名额。

        返回：
          True  -> 名额空闲，可以接活
          False -> 名额被占，别派任务给它
          None  -> 探不了（网络/限流），**不要据此下结论**

        节流：同一个号 PROBE_TTL 内不重复打上游，直接返回**上次探测的
        真实结论**。

        ⚠️ 这里以前返回的是 `aid in self._ready` —— 看着像"沿用上次结论"，
        其实是循环论证：号因为探测说忙而被撤下 ready，于是节流期内的
        探测又返回"不在 ready"→ 继续撤 → 永远不重新问上游。
        号释放了也发现不了，队列永久卡死（实测 3 条任务排 10 分钟没派发）。
        所以必须缓存**探测结果本身**，而不是 ready 成员身份。
        """
        aid = acct["id"]
        now = time.time()
        last = self._slot_probe.get(aid, 0)
        if not force and now - last < PROBE_TTL:
            # 上次探测的真实结论。从没探过 = 不知道，不是"空闲"。
            #
            # 缓存命中时也要写一次 verdict：诊断面板读的是 _slot_verdict，
            # 而 PROBE_TTL(10s) 比 PROBE_INTERVAL(5s) 长，意味着**一半的轮次
            # 走的是这条缓存路径**。不写的话，界面上的"上游探测"列会在
            # free/busy 和 never 之间跳，看起来像探测没工作。
            cached = self._slot_free.get(aid)
            if cached is None:
                return None
            self._record_verdict(aid, "free" if cached else "busy", "")
            return cached
        self._slot_probe[aid] = now
        free = await upstream.probe_slot_free(acct)
        if free is not None:
            self._slot_free[aid] = free
        # 探不了（None）：不写结论、不改 _slot_free，但**要记下为什么** ——
        # 网络错误和"上游说满"是两回事，界面上必须能区分。
        if free is None:
            self._record_verdict(aid, "unknown", "探测失败（本地网络或凭证问题，与上游负载无关）")
        else:
            self._record_verdict(aid, "free" if free else "busy", "")
        return free

    def _record_verdict(self, aid: int, state: str, err: str) -> None:
        """记一次探测结论。state: free / busy / unknown。"""
        self._slot_verdict[aid] = {"state": state, "at": time.time(), "err": err}


    async def _probe_loop(self) -> None:
        """后台探测器：空闲的号定期确认名额，可用就宣告。

        为什么需要它：上游名额释放没有回调，只能靠问。但**问的人应该是号，
        不是任务** —— 任务是队列里的等待方，不该为了"我到底能不能跑"
        去反复打扰上游、还顺手把账号锁抱死。

        只探「当前没在跑任务」的号：跑着任务的号名额一定被自己占着，
        探了也是 False，纯浪费。

        ⚠️ 定时器**不是**固定节拍：有待宣告的号（宽限期快到了）时会提前醒，
        让号一到点就能派出去，不必等到下一个 PROBE_INTERVAL。
        （对齐 Go 版 client/pool.go:probeLoop）
        """
        while True:
            try:
                # 下一次什么时候醒：有待宣告的就按它到点的时刻，
                # 否则等一个节拍。这是"精准唤醒"的全部实现 ——
                # 没有它，RELEASE_GRACE(8s) 到点后最多还要多等一个
                # PROBE_INTERVAL(5s)，队列尾延迟白涨 5 秒。
                wait = PROBE_INTERVAL
                d = self._next_pending_wake()
                if d and d < wait:
                    wait = d
                await asyncio.sleep(wait)

                # ---- 每轮先补派一次（2026-10-25 修死锁）----
                #
                # 放在最前面，不受下面任何 continue 影响。原因见循环末尾那段
                # 长注释：`_announce_ready` 只在号**第一次**进 ready 时调
                # _dispatch，而 _dispatch 一次只唤醒一个等待者 —— 于是
                # 「ready 里还有号 + 还有任务在排」这个组合没人负责派出去，
                # 任务会一直等到超时。客户看到的是"有号却派不出去"。
                #
                # 成本：_dispatch 开头就是 `while self._ready and self._waiters`，
                # 任一为空立即返回，几乎零开销。
                self._dispatch()

                # ⚠️ 未激活时**不碰上游**（对齐 Go 版 pool.go:1226）。
                #
                # 为什么必须这道闸：
                #   下面的探测会给每个空闲号打一次 options，renewTokens 会刷新
                #   access_token —— 两者都消耗上游配额、都打 Cloudflare 计数器。
                #   而客户端的号是**手工加进去的**（客户自己的账号）。
                #   没有这道闸，客户装上软件但还没激活时，后台已经在
                #   替他白白续期、白探名额，然后撞上 1015 封 IP ——
                #   等他一小时后回来激活，号已经被限流了，而且完全查不出原因。
                #
                # 授权门没装（本地开发/测试）时不拦，否则那些场景跑不起来。
                try:
                    from workbuddy.gate import gate as _gate, OFFLINE_GRACE as _og

                    _g = _gate()
                    # 用 time.time() 而不是 gate 内部的 _now()：
                    # 那是私有函数，跨包依赖私有名会在重构时静默坏掉。
                    if _g.verify() and (time.time() - _g._last_ok) <= _og:
                        pass                      # 已激活，继续
                    else:
                        self._flush_pending_ready()
                        continue                  # 未激活：只兑现本地状态，不打上游
                except (RuntimeError, ImportError):
                    pass                          # 授权门未装：放行（开发/测试）

                for a in store.list_accounts():
                    if not (a.get("access_token") or a.get("cookie")):
                        continue
                    # dead 的号不探测名额（它现在不能跑），但**下面照样续期** ——
                    # 续期成功就能自愈，这是"补了新 token 后自动恢复"的一半。
                    if a.get("status") == "dead":
                        continue
                    # free 档也不探测（2026-10-26）：它没有 wan3 权限，
                    # 探名额只会白打一次上游。注意这里**不影响续期** ——
                    # 续期在下面独立进行，free 号如果后来升级成 pro，
                    # token 仍是新的，下次就会正常参与。
                    skip_probe = store.tier_blocked(a)
                    aid = a["id"]
                    lock = self._locks.get(aid)
                    if lock and lock.locked():
                        continue          # 自己在跑，没必要探
                    if (a.get("cooldown_until") or 0) > time.time():
                        continue
                    if skip_probe:
                        # free 档：不探测，也不进 ready。
                        # 如果它之前被记成 ready（升级/降级导致），撤掉 ——
                        # 否则调度会把它当可用号派任务，然后被上游拒。
                        self._withdraw_ready(aid, "tier_free")
                        continue
                    free = await self.probe_slot(a)
                    if free is True:
                        if aid not in self._ready:
                            self._announce_ready(aid)
                    elif free is False:
                        self._withdraw_ready(aid, "slot_busy")
                    # None（探不了）保持原状，不瞎猜

                # 兑现"宽限期已到"的待宣告号。
                # 必须在上面那轮探测之后：它可能刚把号记进 _pending_ready，
                # 同一轮就兑现掉，不用再等一个节拍。
                # （_flush_pending_ready 内部会自己调 _dispatch，这里不重复调。）
                self._flush_pending_ready()
            except asyncio.CancelledError:
                raise
            except Exception:
                # 探测循环绝不能因为单次异常就退出
                await asyncio.sleep(PROBE_INTERVAL)


            # ---- token 续期（独立的生命线，不依赖有没有任务）----
            #
            # 以前续期只发生在 `_try_claim` / `resume` 里 —— 也就是**有任务
            # 来抢号的时候才顺带检查**。没任务就不检查，token 静静过期，
            # 等提交时才发现号早死了。号池的价值就在"随时能跑"，
            # 所以续期必须是一个主动的后台行为，和有没有任务无关。
            try:
                await self._renew_tokens()
            except asyncio.CancelledError:
                raise
            except Exception:
                # 续期失败不该影响探测循环
                pass

    async def _renew_tokens(self) -> None:
        """把所有"快过期"的号的 access_token 续上。

        只处理**有 refresh_token** 的号；冷却中的号跳过（auth 限流期）。
        单个号失败不影响其他号。

        ⚠️ dead 的号也要续 —— 续期成功就能自愈（refresh_and_persist 会写
        status=ok / fails=0）。但**凭证已确认失效**（invalid_grant）的号
        必须停手：它的 refresh_token 已经废了，再刷一万次也不会好，
        只会反复打 auth 端点、把 IP 往 Cloudflare 1015 上推。
        """
        now = time.time()

        # ⚠️ 熔断期间**整轮跳过**（2026-10-25 修）。
        #
        # 以前没有这个判断，后果是：熔断一触发，这一轮会把每个待续期的号
        # 都走一遍 ensure_fresh -> oauth.refresh -> 立刻抛 rate_limited ->
        # 回写 cooldown_until。于是：
        #
        #   1. 每个号都被写上一次 cooldown，UI 上变成「三行都是待续期」，
        #      用户以为三个号全坏了，实际是**一个全局冷却**。
        #   2. cooldown 的秒数取的是 blocked_for()，而它随时间递减 ——
        #      三个号会显示 284s / 283s / 283s 这种"几乎一样但不同"的数字，
        #      更像"每个号各自出了问题"。
        #   3. 每次循环都白跑一遍全表。
        #
        # 熔断是全局状态，就该在循环入口一次性判断掉。
        if authlimit.limiter().blocked:
            return

        for a in store.list_accounts():
            if not a.get("refresh_token"):
                continue
            if (a.get("cooldown_until") or 0) > now:
                continue
            # 凭证已确认失效：停手，等人工重新授权
            if a.get("status") == "dead" and _credential_dead(a):
                continue
            ttl = (a.get("expires_at") or 0) - now
            if ttl > 0 and not self._need_refresh(a["id"], ttl):
                continue          # 还早，不用管
            try:
                await self.ensure_fresh(a)
            except NoCapacity:
                # 单个号续期失败（限流/失效）：记在账号上，别中断整轮
                pass
            except Exception:
                pass

    def start_probes(self) -> asyncio.Task:
        """启动后台探测循环（startup 调一次）。"""
        return asyncio.create_task(self._probe_loop())

    def queue_depth(self) -> int:
        """当前有多少任务在 acquire 里排队等号。"""
        return len(self._waiters)

    def _peek_ready(self) -> dict[str, Any] | None:
        """从 ready 里挑一个**当前确实没在跑**的号。

        不阻塞、不做上游请求。挑完成少的那个，让轮转保持均匀。
        """
        accounts = self._candidates()
        by_id = {a["id"]: a for a in accounts}
        for aid in sorted(list(self._ready), key=lambda x: (self._done.get(x, 0), x)):
            a = by_id.get(aid)
            if not a:
                self._withdraw_ready(aid)      # 号已失效/被删
                continue
            lock = self._locks.get(aid)
            if lock and lock.locked():
                continue
            return a
        return None

    async def wait_ready(self, timeout: float) -> dict[str, Any] | None:
        """等一个**已宣告可用**的号，超时返回 None。

        timeout <= 0 表示"只看一眼，没有就立刻返回"（不阻塞、不排队）。

        实现：加入 FIFO 等待队列，由 `_dispatch()` 在号宣告可用时按序唤醒。
        **不轮询、不持有锁** —— 1000 个排队协程里同一时刻只有 1 个在跑，
        不会出现 1000 个协程同时被唤醒抢同一个号的惊群。
        """
        # 1) 先看有没有现成的
        acct = self._peek_ready()
        if acct:
            return acct
        if timeout <= 0:
            return None

        loop = asyncio.get_running_loop()
        deadline = time.time() + timeout
        fut: asyncio.Future = loop.create_future()
        entry = (fut, deadline)
        self._waiters.append(entry)
        try:
            # 2) 排队等唤醒。dispatch 会 set_result；也可能因为超时/被摘除而返回。
            #    注意：醒来后**重新挑一次** —— 派给我们的那个号可能刚好被别的
            #    路径抢走（比如 resume 任务），挑不到就继续排下一轮。
            while True:
                remain = deadline - time.time()
                if remain <= 0:
                    return None
                try:
                    await asyncio.wait_for(asyncio.shield(fut), timeout=remain)
                except asyncio.TimeoutError:
                    return None
                except asyncio.CancelledError:
                    raise
                # 被唤醒了：这里不能复用 fut（Future 只能 set 一次），
                # 换一个新的再排到队尾，保证顺序公平
                try:
                    self._waiters.remove(entry)
                except ValueError:
                    pass
                acct = self._peek_ready()
                if acct:
                    return acct
                entry = (fut := loop.create_future(), deadline)
                self._waiters.append(entry)
        finally:
            try:
                self._waiters.remove(entry)
            except ValueError:
                pass

    async def _try_claim(
        self, acct: dict[str, Any]
    ) -> tuple[dict[str, Any], asyncio.Lock] | None:
        """尝试立刻占用一个账号；占不到返回 None。

        不做任何上游请求 —— 锁空即视为可用。若实际提交时拿到 429，
        说明该号上游名额仍被我们自己的旧任务占着，那时再退避重试。

        ⚠️ acquire 之后的**所有**退出路径都必须还锁。
        以前只 catch NoCapacity，但 ensure_fresh 底下会走 oauth._raw_refresh，
        那里的 httpx.ConnectError / TimeoutException、以及 sqlite3.OperationalError
        都会原样穿出去 —— 锁已 acquire 却没人 release，这个号就永久"忙"，
        直到 7200s deadline 才被放出来。一次网络抖动废掉一个号两小时，
        抖得多一点整个池子就瘫了。所以这里用 BaseException 兜底（含 CancelledError）。
        """
        aid = acct["id"]
        lock = await self._lock_for(aid)
        if lock.locked():
            return None

        await lock.acquire()
        try:
            fresh = store.get_account(aid)
            if not fresh or not (fresh.get("access_token") or fresh.get("cookie")):
                return None
            try:
                fresh = await self.ensure_fresh(fresh)
            except NoCapacity:
                return None
            async with self._guard:
                self._busy[aid] = self._busy.get(aid, 0) + 1
            return fresh, lock
        except BaseException:
            # 走到这里 = 没成功交出去，锁必须还给池子。
            # _busy 只在上面那个 async with 里加过，加完就 return 了，
            # 所以这里不需要回滚计数。
            if lock.locked():
                lock.release()
            raise

    async def acquire(
        self, timeout: float = TASK_MAX_LIFETIME
    ) -> tuple[dict[str, Any], asyncio.Lock]:
        """分配账号 —— 等号自己宣告可用后再取。

        和老版本的区别（这是这次重写的核心）：

        老版本：任务轮着扫所有账号，逐个 _try_claim，撞上 429 就抱着账号锁
                指数退避，最长 SUBMIT_WAIT_MAX。号被上游占着时，任务连人带锁
                一起卡住，外面还只显示「排队中」。

        新版本：**号负责宣告，任务排队等**。
          · 空闲号由后台 _probe_loop 定期问上游 options.rateLimitVerdict
            （无副作用，只判定不提交），确认空闲才 _announce_ready
          · 任务挂进 FIFO 等待队列（_waiters），由 _dispatch() 按序唤醒
          · 拿到的是**已验证可提交**的号，所以 submit 基本不撞 429

        队列语义（按 100 号 / 上千排队任务设计）：
          · 严格 FIFO：号宣告可用时只唤醒队首，不会惊群
          · 不轮询、不占锁：1000 个排队协程里同一时刻只有 1 个在跑
          · 顺序公平：被唤醒但号已被抢走的，重新排到队尾（不会饿死）
          · 立刻报错：全 dead / 全无凭证 —— 等也没用

        `queue_depth()` 暴露当前排队数，管理页可观测。
        """
        deadline = time.time() + timeout

        while time.time() < deadline:
            accounts = self._candidates()
            if not accounts:
                # 分清三种"看起来空"的原因，别一律说成"号池为空"
                all_accs = store.list_accounts()
                if not all_accs:
                    raise NoCapacity("号池为空，请先在管理页添加账号")
                now = time.time()
                no_cred = [a for a in all_accs
                           if not (a.get("access_token") or a.get("cookie"))]
                dead = [a for a in all_accs if a.get("status") == "dead"]
                cooling = [a for a in all_accs
                           if (a.get("cooldown_until") or 0) > now]
                if len(dead) + len(cooling) >= len(all_accs):
                    detail = []
                    if dead:
                        detail.append(f"{len(dead)} 个已失效（需重新授权）")
                    if cooling:
                        detail.append(
                            f"{len(cooling)} 个在限流冷却中（约 "
                            f"{int(max(a.get('cooldown_until', 0) for a in cooling) - now)}s 后恢复）"
                        )
                    raise NoCapacity("号池内没有可用账号：" + "，".join(detail))
                if no_cred:
                    raise NoCapacity("号池内没有带凭证的账号，请重新授权")
                raise NoCapacity("号池内没有可用账号")

            # ---- 1) 先看有没有现成可用的号 ----
            acct = self._peek_ready()
            if acct:
                got = await self._try_claim(acct)
                if got:
                    return got
                self._withdraw_ready(acct["id"], "claim_failed")
                # 号还在但 claim 失败（refresh 挂了），继续往下看别的
                continue

            # ---- 2) 没有 ready 的：先做一次即时探测，确认真实状态 ----
            # 后台循环 PROBE_INTERVAL 才跑一轮，任务刚来时可能还没探过。
            # 这里主动问一次，避免"明明空着却要等 5 秒"。
            announced = False
            for a in accounts:
                free = await self.probe_slot(a, force=True)
                if free is True:
                    self._announce_ready(a["id"])
                    announced = True
                elif free is False:
                    self._withdraw_ready(a["id"], "slot_busy")
            if announced:
                continue        # 有新号了，回循环顶部去取

            # ---- 3) 确实没有可用的：进 FIFO 队列等 ----
            # 到这里说明：没有 ready 号、且探测确认都不可用（忙 or 名额被占）。
            # 剩下唯一能做的就是等 —— 等 release() 或 _probe_loop 宣告新号。
            # 这里**只挂一个 Future**，不轮询、不占锁，由 _dispatch 按序唤醒。
            #
            # 判错要小心：只有"等也不会好"的才立刻报错。
            # 名额被占/号在跑 = 等一会儿会好，必须继续等。
            # 凭证全废/全 dead = 等也没用 —— 那个已经在上面
            # accounts 为空的判断里报过了，走不到这里。
            got = await self.wait_ready(deadline - time.time())
            if got is None:
                break          # 超时，原因由下面统一说明
            got = await self._try_claim(got)
            if got:
                return got
            self._withdraw_ready(got["id"], "claim_failed")

        # 到点了还没等到。给出**能定位原因**的说明，而不是一句"超时"
        detail = []
        now = time.time()
        for a in store.list_accounts()[:5]:
            aid = a["id"]
            if a.get("status") == "dead":
                detail.append(f"{a['name']}(已失效)")
            elif (a.get("cooldown_until") or 0) > now:
                detail.append(f"{a['name']}(限流冷却中)")
            elif aid in self._slot_probe and aid not in self._ready:
                detail.append(f"{a['name']}(上游名额被占)")
            else:
                detail.append(f"{a['name']}(忙)")
        raise NoCapacity(
            "等待账号空闲超时"
            + ("：" + "；".join(detail) if detail else "")
        )


    # ---------------------------------------------------------- 结算

    def release(self, account: dict[str, Any], lock: asyncio.Lock) -> None:
        """任务结束，释放账号（无论成败）。这是队列的核心动作。

        ⚠️ 关键：**这里绝不直接宣告可用**。

        本地锁空 ≠ 上游名额空。Picsart 那边任务结束了，并发名额通常要晚
        几秒到几十秒才真正放开。以前这里直接 `_announce_ready`，队列头
        任务被立刻唤醒、抢到号、submit —— 必然 429（实测撞了好几次，
        白等 20 分钟排队后拿到号却失败）。

        正确做法：只把「这个号该重新探了」的标记清掉，让 _probe_loop
        下一轮去问上游。问出来是空的才宣告可用。代价是慢一个 PROBE_INTERVAL
        （5 秒），比白白撞一次 429、还把任务判失败划算得多。
        """
        aid = account["id"]
        n = self._busy.get(aid, 1) - 1
        if n <= 0:
            self._busy.pop(aid, None)
        else:
            self._busy[aid] = n
        if lock.locked():
            lock.release()

        # 让下一次探测必须重新确认（时间戳清零，绕过 PROBE_TTL 节流）。
        # 注意：**不宣告**。上游名额是否真释放了，只有问过才知道。
        # _slot_free 也必须清 —— 否则节流期内的探测会复用旧的"被占"结论，
        # 号明明空了却永远发现不了（那正是队列卡死的直接原因）。
        # 同时记下释放时刻，_announce_ready 会据此加宽限（RELEASE_GRACE），
        # 避免"锁刚放、名额还没放"就去 submit 撞 429。
        self._slot_probe.pop(aid, None)
        self._slot_free.pop(aid, None)
        self._released_at[aid] = time.time()
        # 待宣告表也要清：这个号刚跑完，之前的 pending 记录已经过时，
        # 留着会让 _flush_pending_ready 在错误的时刻宣告它。
        self._pending_ready.pop(aid, None)
        self._withdraw_ready(aid, "released_pending_probe")

    def _count_done(self, aid: int) -> None:
        self._done[aid] = self._done.get(aid, 0) + 1

    def _backoff(self, aid: int, seconds: float) -> None:
        """给单个号设一个短退避。不是账号封禁，只是让它在队列里排后面。

        上限的取法有讲究：
          · 普通退避（我们自己算的指数退避）→ 夹到 RETRY_BACKOFF_MAX(60s)
          · 上游通过 retry-after 明确要求的 → 允许超过 60s，但仍然兜一个
            硬上限 RETRY_AFTER_CAP(1800s)，防止一个解析异常把号锁死一整天

        之前一律 hard clamp 到 60 秒，等于**无视上游的 retry-after=120**、
        提前再打一次 —— Cloudflare 会因此延长封禁窗口（1015 的教训）。
        """
        cap = RETRY_BACKOFF_MAX if seconds <= RETRY_BACKOFF_MAX else RETRY_AFTER_CAP
        self._retry_after[aid] = time.time() + max(1.0, min(float(seconds), cap))

    # 只让"凭证类"失败给账号扣分。业务失败不该算在账号头上。
    #
    # 之前是所有失败都 +1，20 次就 dead —— 于是 prompt 审核拒绝、参数 400、
    # 上游 502 全在给账号记账。实测就是这样把一个 token 还剩 10 分钟的号
    # 判成 dead 的，然后池子 503，重试还是 503。
    #
    # 分类依据：这次失败换了别的 prompt / 别的号会不会好？
    #   会 → 是业务问题，账号无罪，不计数
    #   不会 → 是账号问题，计数
    AUTH_FAIL_STATUS = (401, 403)      # token 失效 / 账号被限制
    ACCOUNT_FAULT_LIMIT = 20

    def settle(
        self,
        account: dict[str, Any],
        *,
        ok: bool,
        status_code: int = 0,
        reason: str = "",
    ) -> None:
        """记录结果。不设账号级冷却 —— 冷却会让账号被无辜跳过。"""
        aid = account["id"]
        if ok:
            self._retry_after.pop(aid, None)
            store.update_account(
                aid, fails=0, status="ok", cooldown_until=0, last_error=""
            )
            return

        reason = reason or ""

        if status_code == 429:
            # 名额被占：退避 + 说明，**不扣分**（不是账号的错）
            self._backoff(aid, RETRY_BACKOFF_BASE * 5)
            store.update_account(aid, last_error="上游名额被占用（等待放开）")
            return

        if status_code == 402:
            # 额度/收费档：不扣分。换个 free 档位就可能好了。
            store.update_account(aid, last_error=reason or "402 额度")
            return

        if status_code in self.AUTH_FAIL_STATUS:
            # 凭证问题：清 expires_at 触发 refresh；这一条才该计数
            fails = int(account.get("fails", 0)) + 1
            store.update_account(
                aid,
                expires_at=0,
                fails=fails,
                status="dead" if fails >= self.ACCOUNT_FAULT_LIMIT else account.get("status", "ok"),
                last_error=f"token 失效({status_code})",
            )
            return

        # 其余（400 参数、403 审核、404、5xx、超时……）一律视为业务/上游问题：
        # 只在任务上体现，不动账号状态、不累加 fails。
        store.update_account(aid, last_error=(reason or f"上游 {status_code or '异常'}")[:300])

    # ---------------------------------------------------------- 主流程

    async def generate(
        self,
        params: dict[str, Any],
        *,
        acquire_timeout: float = TASK_MAX_LIFETIME,
        on_progress: Any = None,
        on_account: Any = None,
        on_submitted: Any = None,
        on_upstream: Any = None,
    ) -> dict[str, Any]:
        """跑一次生成。

        队列语义：先排队拿到账号（锁），任务真正结束后才释放。

        三个时间钩子，用来把「总耗时」拆开归因：
          on_account    拿到账号的瞬间      -> started_at
          on_submitted  上游受理（submit 201）-> submitted_at
          on_upstream   上游真正开始出片      -> upstream_at
        这样能一眼看出慢在排队、慢在调度、还是慢在生成。
        """
        params = dict(params or {})
        model = upstream.normalize_model(params.get("model") or settings.default_model)
        resolution = str(params.get("resolution") or settings.default_resolution).upper()

        if model.endswith("-prime") or resolution in PAID_RESOLUTIONS:
            raise NoCapacity(f"只跑 0 积分档，拒绝 {model} / {resolution}")
        if resolution not in FREE_RESOLUTIONS:
            resolution = settings.default_resolution

        params["model"] = model
        params["resolution"] = resolution
        params.setdefault("duration", settings.default_duration)
        params.setdefault("ratio", settings.default_ratio)

        acct, lock = await self.acquire(timeout=acquire_timeout)
        if on_account:
            try:
                on_account(acct)
            except Exception:
                pass

        # 进度回调外面再包一层：第一次出现非零进度就说明上游开始跑了
        upstarted = {"hit": False}
        def _prog(pct: int, st: str) -> None:
            if not upstarted["hit"] and (pct or 0) > 0:
                upstarted["hit"] = True
                if on_upstream:
                    try:
                        on_upstream()
                    except Exception:
                        pass
            if on_progress:
                try:
                    on_progress(pct, st)
                except Exception:
                    pass

        try:
            if USE_SSE:
                result = await self._stream(acct, params, _prog)
                upstream_id = result.get("task_id") or ""
                workflow = upstream.WAN3_WORKFLOW
            else:
                upstream_id, workflow = await self._submit_with_queue(acct, params)
                if on_submitted:
                    # ⚠️ 必须把 upstream_id 一起交出去，而且要**立刻落库**。
                    # 之前 on_submitted() 不带参数，upstream_id 只在函数末尾的
                    # 返回值里 —— 一旦进程在中途死掉/重启，这个 id 就彻底丢了：
                    # 上游任务还在跑、还占着并发名额，我们既查不到也取消不了，
                    # 只能干等它自己超时（实测会占 20 分钟到数小时）。
                    # 这次就是这么丢了一个任务的。
                    try:
                        on_submitted(upstream_id)
                    except TypeError:
                        # 兼容旧签名（不收参数的回调）
                        on_submitted()
                    except Exception:
                        pass
                result = await upstream.wait_result(
                    acct, upstream_id, workflow=workflow, on_progress=_prog
                )
            ok = result.get("status") == "COMPLETED"
            st = str(result.get("status") or "")
            if st == "UNKNOWN":
                # 上游超时、任务可能还在跑。不叫失败，也不算成功。
                # 不调 settle()，因为它会把 last_error 覆盖掉，
                # 而这里写下的说明比"UNKNOWN"这个词有用得多。
                pass
            elif st == "TIMEOUT":
                # 我们等太久了，**不是**并发名额被占。
                # 以前和 BLOCKED 一起塞进 429 分支，settle 里就把 last_error
                # 写成"上游名额被占用（等待放开）"—— 那是把"我们自己放弃"
                # 误报成"上游在抢名额"，排查时会被带偏。
                store.update_account(
                    acct["id"],
                    last_error=f"等待出片超时（{int(TASK_MAX_LIFETIME)}s 上限），任务可能仍在上游运行",
                )
                # 不放锁就走：上游可能还占着名额，立刻放锁会让下一个任务撞上去。
                self._backoff(acct["id"], RETRY_BACKOFF_MAX)
            elif st == "STALLED":
                # 上游卡在 ACCEPTED 不动（updated 长期无变化）= 没被调度。
                # 不是账号问题，也不该冷却账号 —— 换一个任务重提通常立刻
                # 就能跑。让上层重新入队，账号状态保持不变。
                store.update_account(
                    acct["id"],
                    last_error=f"上游任务未被调度（{result.get('error') or 'STALLED'}）",
                )
            else:
                sc = 0 if ok else (429 if st == "BLOCKED" else 502)
                self.settle(acct, ok=ok, status_code=sc, reason=st)
            return {
                **result,
                "upstream_id": upstream_id,
                "workflow": workflow,
                "account_id": acct["id"],
                "account_name": acct["name"],
                "resolution": resolution,
                "model": model,
            }
        except upstream.UpstreamError as e:
            self.settle(acct, ok=False, status_code=e.status, reason=e.reason)
            raise
        finally:
            self._count_done(acct["id"])
            self.release(acct, lock)

    async def _stream(
        self,
        acct: dict[str, Any],
        params: dict[str, Any],
        on_progress: Any,
    ) -> dict[str, Any]:
        """SSE 提交+跟随。

        三种特殊情况的处理（都踩过）：

        1) 429 concurrency_limit
           名额被占（多半是上游还挂着我们之前的旧任务）。指数退避重试。

        2) 524 Cloudflare 超时
           请求已到达源站，但 100 秒内没回响应头。**任务很可能已经在上游跑着**，
           只是我们没拿到 task_id。这时候：
             · 不能重试 —— 会重复创建任务、重复占名额
             · 不能当"没发生" —— 那个任务已经在消耗名额了
           正确做法：标记该账号"可能有孤儿任务"，退避一段时间再让其他任务试。

        3) stream_broken / stream_timeout
           连接中途断了。同上，任务状态未知。
        """
        deadline = time.time() + TASK_MAX_LIFETIME
        delay = RETRY_BACKOFF_BASE
        last: Exception | None = None

        while time.time() < deadline:
            try:
                return await asyncio.wait_for(
                    upstream.stream_submit(acct, params, on_progress=on_progress),
                    timeout=TASK_MAX_LIFETIME,
                )
            except asyncio.TimeoutError:
                return {"status": "TIMEOUT", "url": None,
                        "error": f"超过 {int(TASK_MAX_LIFETIME/3600)} 小时未完成"}
            except upstream.UpstreamError as e:
                if e.reason == "concurrency_limit":
                    last = e
                    # 上游给了 retry-after 就照它等（Cloudflare 的指示比我们
                    # 自己拍的指数退避权威：早了会延长封禁，晚了白等）。
                    # 没给才用本地退避。
                    wait = e.retry_after or delay
                    self._backoff(acct["id"], wait)
                    await asyncio.sleep(wait)
                    delay = min(delay * 2, RETRY_BACKOFF_MAX)
                    fresh = store.get_account(acct["id"])
                    if fresh:
                        acct = fresh
                    continue

                if e.reason in ("cf_timeout", "stream_broken", "stream_timeout"):
                    # 任务可能已在上游创建并占着名额，但 id 拿不到。
                    # 不重试（避免重复创建），退避让后续任务晚点再试，
                    # 并把这个情况如实告诉上层 —— 用户看到的是"状态未知"，
                    # 而不是一个假的"失败"。
                    self._backoff(acct["id"], RETRY_BACKOFF_MAX)
                    store.update_account(
                        acct["id"],
                        last_error="上游响应超时，任务可能仍在跑（状态未知）",
                    )
                    return {
                        "status": "UNKNOWN",
                        "url": None,
                        "error": (
                            "上游响应超时（Cloudflare 524），任务可能已创建并正在运行，"
                            "但我们拿不到它的 ID。稍后可以在上游网页端确认是否出片。"
                        ),
                    }
                raise
        raise last or NoCapacity("等上游名额超时")

    # ---------------------------------------------------------- 检查账号

    async def refresh_account_info(self, aid: int) -> dict[str, Any]:
        """刷新余额 / 身份 / 计价，管理页「检查」按钮用。

        不碰业务状态，只做只读探测：
          · guard/credits          余额与档位
          · users/show/{uid}.json  用户名 / 头像
          · options × 3 档位        真实计价（判断哪些是 0 积分）
        """
        acct = store.get_account(aid)
        if not acct:
            raise NoCapacity("账号不存在")

        info: dict[str, Any] = {}
        try:
            acct = await self.ensure_fresh(acct)
        except NoCapacity as e:
            info["warning"] = str(e)
            return info

        # 余额
        try:
            c = await upstream.fetch_credits(acct)
            credits = c.get("credits")
            tier_credits = c.get("tierCredits")
            store.update_account(
                aid,
                credits=float(credits or 0),
                tier_credits=float(tier_credits or 0),
            )
            info["credits"] = credits
            info["tier_credits"] = tier_credits
        except Exception as e:
            info["credits_error"] = str(e)[:120]

        # 真实订阅档位。这个必须查，因为 x-sub-package-id 是要声明"我订的哪个 SKU"，
        # 声明错了就是在替账号谎报订阅。查到就落库，后续 headers_for() 直接用。
        try:
            tier = await upstream.fetch_tier(acct)
            if tier:
                store.update_account(aid, tier=tier)
                info["tier"] = tier
                acct = {**acct, "tier": tier}
        except Exception as e:
            info["tier_error"] = str(e)[:120]

        # 计价：用 options 端点查每个档位此刻的真实 credits
        pricing: dict[str, Any] = {}
        for res in ("480P", "720P", "1080P"):
            try:
                o = await upstream.fetch_options(acct, resolution=res, duration=5)
                pricing[res] = {
                    "toolId": o.get("toolId"),
                    "credits": o.get("credits"),
                    "price": o.get("price"),
                }
            except Exception as e:
                pricing[res] = {"error": str(e)[:80]}
        info["pricing"] = pricing

        # OAuth token 能力边界，如实告知
        token_kind = acct.get("kind")
        if token_kind == "oauth":
            info["token_kind"] = "oauth"
        return info

    async def resume(
        self,
        account: dict[str, Any],
        upstream_id: str,
        *,
        workflow: str | None = None,
        on_progress: Any = None,
    ) -> dict[str, Any] | None:
        """接着跟一个**已经提交过**的上游任务（进程重启后续跟）。

        为什么不重新 submit：提交过就占着名额、也可能已经在出片了。
        重新提交 = 白占一次名额 + 可能重复出片 + 重复计费风险。
        上游任务的真实身份就是 upstream_id，拿着它 GET result 就够了。

        返回 None 表示这个号正被别的任务占着，跳过（不排队等，
        因为续跟不该阻塞新任务）。
        """
        aid = account["id"]
        lock = await self._lock_for(aid)
        if lock.locked():
            return None
        await lock.acquire()
        try:
            async with self._guard:
                self._busy[aid] = self._busy.get(aid, 0) + 1
            try:
                fresh = store.get_account(aid) or account
                try:
                    fresh = await self.ensure_fresh(fresh)
                except NoCapacity:
                    # 续期失败也先试着跟，401 会由 settle 处理
                    pass
                result = await upstream.wait_result(
                    fresh, upstream_id,
                    workflow=workflow or upstream.WAN3_WORKFLOW,
                    on_progress=on_progress,
                )
            except upstream.UpstreamError as e:
                self.settle(account, ok=False, status_code=e.status, reason=e.reason)
                raise

            ok = result.get("status") == "COMPLETED"
            st = str(result.get("status") or "")
            if st == "UNKNOWN":
                pass
            elif st == "TIMEOUT":
                store.update_account(
                    account["id"],
                    last_error="续跟超时，任务可能仍在上游运行",
                )
                self._backoff(account["id"], RETRY_BACKOFF_MAX)
            else:
                sc = 0 if ok else (429 if st == "BLOCKED" else 502)
                self.settle(account, ok=ok, status_code=sc, reason=st)
            return {
                **result,
                "upstream_id": upstream_id,
                "workflow": workflow or upstream.WAN3_WORKFLOW,
                "account_id": aid,
                "account_name": account.get("name") or "",
            }
        finally:
            self.release(account, lock)

    async def _submit_with_queue(
        self, acct: dict[str, Any], params: dict[str, Any]
    ) -> tuple[str, str]:
        """submit，遇到 429 就按指数退避重试。

        429 说明这个号的上游名额被别的任务占着（多半是我们自己的、
        或者上游遗留的孤儿任务）。用指数退避等它自然释放，而不是猛打。

        ⚠️ 两处必须克制：
        · 退避总时长封顶 SUBMIT_WAIT_MAX，不能用 TASK_MAX_LIFETIME。
          否则一个号被占着，这个任务就抱着账号锁干等 2 小时 ——
          从外面看只是「排队中」，实际上是卡死，而且把整个号也占住了。
          等不到就该尽快把锁交回去，让别的任务（或别的号）去跑。
        · 每次重试前把等待事实写进 last_error，别让用户只能看到
          「正在提交」这种什么信息都没有的状态。
        """
        deadline = time.time() + SUBMIT_WAIT_MAX
        delay = RETRY_BACKOFF_BASE
        last: Exception | None = None
        attempts = 0
        while time.time() < deadline and attempts < SUBMIT_MAX_TRIES:
            try:
                return await upstream.submit(acct, params)
            except upstream.UpstreamError as e:
                if e.reason != "concurrency_limit":
                    raise
                last = e
                attempts += 1
                self._backoff(acct["id"], delay)
                store.update_account(
                    acct["id"],
                    last_error=f"上游名额被占，第 {attempts}/{SUBMIT_MAX_TRIES} 次退避 {int(delay)}s",
                )
                await asyncio.sleep(delay)
                delay = min(delay * 2, RETRY_BACKOFF_MAX)
                fresh = store.get_account(acct["id"])
                if fresh:
                    acct = fresh
        # 等不到名额就**明确失败**，并把号从 ready 撤下 ——
        # 否则队列头下一个任务会被立刻唤醒、又去撞一次 429，纯属浪费。
        # 探测缓存也一起清：让 _probe_loop 重新问一次，
        # 确认名额真放了再宣告（沿用旧的"被占"结论会让队列卡死）。
        self._withdraw_ready(acct["id"], "submit_exhausted")
        self._slot_probe.pop(acct["id"], None)
        self._slot_free.pop(acct["id"], None)
        raise upstream.UpstreamError(
            "concurrency_limit", 429,
            f"上游名额被占用（{attempts} 次尝试 / {int(SUBMIT_WAIT_MAX)}s 内未成功），"
            "已放弃本次提交，稍后会自动重试",
        )


def pool() -> Pool:
    return Pool.get()
