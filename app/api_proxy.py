"""OpenAI 兼容反代接口 + 异步任务。

  POST /v1/videos/generations     同步或异步生成
  GET  /v1/videos/generations/{id} 查任务
  GET  /v1/models                  模型列表
  GET  /v1/credits                 号池总余额
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from . import pool, store, upstream
from .config import (
    DURATION_AUTO,
    DURATION_MAX,
    DURATION_MIN,
    FREE_RESOLUTIONS,
    PAID_RESOLUTIONS,
    RATIOS,
    settings,
)
from .security import check_api_key

log = logging.getLogger("picsart-pool")

router = APIRouter()

# 跑着的异步任务引用。
# asyncio.create_task 的返回值**必须**有人持有 —— 事件循环对 task 只持弱引用，
# 不保存的话它可能在跑到一半就被 GC 掉，而 tasks 表里那行会永远停在
# queued/in_progress（谁也不会去收尾，见 store.reset_unfinished_tasks 的说明）。
_TASKS: dict[str, asyncio.Task] = {}


def _guard(authorization: str | None) -> None:
    if not check_api_key(authorization):
        raise HTTPException(401, "invalid api key")


# ---------------------------------------------------------------- 模型

@router.get("/v1/models")
async def models(authorization: str | None = Header(default=None)):
    """只暴露 0 积分档位。

    这个服务只做免费 wan3：480P / 720P。1080P 和 -prime 都会扣积分，
    所以不列出来 —— 客户端看不到，也就不会调。
    """
    _guard(authorization)
    return {
        "object": "list",
        "data": [
            {
                "id": "wan-3.0-video",
                "object": "model",
                "owned_by": "wan",
                "resolutions": list(settings.free_fallback),
                "input_types": ["text-to-video", "image-to-video", "video-to-video"],
                "duration": {"min": DURATION_MIN, "max": DURATION_MAX, "auto": DURATION_AUTO},
                "ratios": list(RATIOS),
                "credits": 0,
                "note": "免费档：480P / 720P，2~30 秒，不消耗积分",
            },
        ],
    }


@router.get("/v1/credits")
async def credits(authorization: str | None = Header(default=None)):
    _guard(authorization)
    accounts = [a for a in store.list_accounts() if a["status"] == "ok"]
    return {
        "accounts_ok": len(accounts),
        "credits_total": sum(a["credits"] or 0 for a in accounts),
        # 不给账号名：那是号池内部信息。反代接口的调用方只需要知道
        # "有多少号可用、总额度多少"，不需要知道每个号叫什么 ——
        # 暴露内部命名等于把号池结构白送出去。
        "details": [
            {"credits": a["credits"], "tier": a["tier_credits"]}
            for a in accounts
        ],
    }


# ---------------------------------------------------------------- 生成

@router.post("/v1/videos/generations")
async def generate(
    request: Request,
    authorization: str | None = Header(default=None),
):
    """生成视频。

    默认 `wait: true` 同步返回（单任务约 5 分钟）。
    批量场景建议 `wait: false`，拿 id 去轮询，避免 HTTP 连接被长任务拖死。
    """
    _guard(authorization)
    body = await request.json()

    params = _build_params(body)
    # 视频任务实测 5 分钟左右，同步会一直占着连接；
    # 需要同步就显式传 wait:true。
    wait = bool(body.get("wait", False))

    if not wait:
        tid = await _spawn_async(params)
        return JSONResponse(
            status_code=202,
            content={
                "id": tid,
                "object": "video.generation",
                "status": "queued",
                "poll": f"/v1/videos/generations/{tid}",
            },
        )

    try:
        result = await pool.pool().generate(params)
    except pool.NoCapacity as e:
        raise HTTPException(503, str(e))
    except upstream.UpstreamError as e:
        code = {401: 401, 402: 402, 429: 429, 400: 400}.get(e.status, 502)
        detail = _explain(e)
        if e.reason == "insufficient_credits":
            detail = (
                f"额度不足（402）。免费档只能用 480P / 720P，"
                f"1080P 需要积分。当前请求 {params.get('resolution')}"
            )
        raise HTTPException(code, detail)

    status = result.get("status")
    if status != "COMPLETED":
        return {
            "id": result.get("upstream_id"),
            "object": "video.generation",
            "status": str(status).lower(),
            "error": result.get("error") or status,
            "account": result.get("account_name"),
        }
    return {
        "id": result.get("upstream_id"),
        "object": "video.generation",
        "status": "completed",
        "url": result.get("url"),
        "resolution": result.get("resolution"),
        "model": result.get("model"),
        "credits_used": result.get("credits_used", 0),
        "account": result.get("account_name"),
    }


@router.get("/v1/videos/generations/{tid}")
async def get_generation(tid: str, authorization: str | None = Header(default=None)):
    _guard(authorization)
    t = store.get_task(tid)
    if not t:
        raise HTTPException(404, f"task {tid} 不存在")
    out: dict[str, Any] = {
        "id": t["id"],
        "object": "video.generation",
        "status": t["status"],
        "model": t["model"],
        "resolution": t["resolution"],
        "created": int(t["created_at"]),
        "account": t["account_name"],
    }
    if t.get("percent"):
        out["progress"] = t["percent"]
    if t["url"]:
        out["url"] = t["url"]
    if t["error"]:
        out["error"] = t["error"]
    if t["elapsed"]:
        out["elapsed"] = t["elapsed"]
    return out


# ---------------------------------------------------------------- 异步

async def _spawn_async(params: dict[str, Any]) -> str:
    """入队一个异步任务。

    ⚠️ 队列上限必须用**任务行数**判，不能用 pool.queue_depth()
    （2026-10-25 修，对齐 Go 版 apiproxy.go 的同一处修复）。

    为什么 queue_depth 不行：它是 `len(self._waiters)`，而 _waiters
    只在 acquire() **阻塞等待**时才加入。判满发生在**提交那一刻** ——
    那时任务刚 create_task，还没跑进 acquire，_waiters 必然是 0。

    实测后果：上限设 200，连提 300 个任务，**一个都不会被拒**。
    号池只有 N 个号时，剩下 300-N 个任务在内存里堆着，
    每个都占着 params（中文 prompt 可能上万字节）—— 客户把它当队列用，
    内存一路涨到 OOM，而日志里看不出任何异常。

    count_running_tasks() 数的是 queued + in_progress 的行，**提交即计入**，
    这才是"队列有多深"的正确答案。
    """
    depth = store.count_running_tasks()
    limit = settings.max_concurrent_tasks
    if limit > 0 and depth >= limit:
        raise HTTPException(
            429,
            f"任务队列已满（在跑+排队 {depth} / 上限 {limit}），请稍后重试",
        )

    tid = "vtask_" + uuid.uuid4().hex[:24]
    # 完整参数存下来：任务页要能查看"当时到底发了什么"，
    # 也方便复现问题。剥掉内部标记（_named_media 之类）。
    snapshot = {k: v for k, v in params.items() if not k.startswith("_")}
    # ⚠️ 不要截断 params（2026-10-25 修，对齐 Go 版同一处修复）。
    #
    # 以前是 [:4000]，而中文 prompt 是按字节切的 —— 1 个汉字 3 字节，
    # 切在字符中间就产生非法 UTF-8，json.loads 直接失败。
    # 后果链：params 读不出来 → 续跟时判"参数已丢失" → 重新排队拿 nil
    # → 任务卡死。实测 10880 字节的 prompt 必然踩到。
    # readJSONBody 已经限了请求体大小，这里没必要再限。
    store.create_task(
        tid,
        model=params.get("model", ""),
        resolution=params.get("resolution", ""),
        prompt=str(params.get("prompt", ""))[:200],
        status="queued",
        params=json.dumps(snapshot, ensure_ascii=False, default=str),
        queue_pos=depth,
    )
    # 强引用存起来，跑完再摘掉（done_callback 两条路径都覆盖）
    job = asyncio.create_task(_run_task(tid, params))
    _TASKS[tid] = job

    def _discard(_t: asyncio.Task, _tid: str = tid) -> None:
        _TASKS.pop(_tid, None)

    job.add_done_callback(_discard)
    return tid


def resume_tasks(rows: list[dict[str, Any]]) -> int:
    """把上次进程遗留、但**已经拿到 upstream_id** 的任务重新挂起来续跟。

    这是"重启不丢任务"的另一半：
      · 提交成功立刻把 upstream_id 落库（见 _on_submitted）
      · 启动时把这些行捞出来，直接 GET .../{id}/result 接着跟
    两者合起来，进程重启就不再制造孤儿任务了。

    返回挂起的条数。
    """
    n = 0
    for t in rows:
        tid = t["id"]
        if tid in _TASKS:
            continue
        job = asyncio.create_task(_resume_task(t))
        _TASKS[tid] = job

        def _discard(_t: asyncio.Task, _tid: str = tid) -> None:
            _TASKS.pop(_tid, None)

        job.add_done_callback(_discard)
        n += 1
    return n


async def _resume_task(t: dict[str, Any]) -> None:
    """恢复一个上个进程遗留的任务。

    两种情况：
      · 有 upstream_id —— 续跟：直接 GET .../{id}/result 接着等，
        **不重新 submit**（重提交会白占名额、可能重复出片）
      · 没有 upstream_id 但参数还在 —— 重新排队：这种连号都没拿到过，
        重启对它只是"还没轮到"，重新走一遍完整流程即可
    """
    tid = t["id"]
    upstream_id = t.get("upstream_id") or ""
    aid = t.get("account_id")
    t_start = time.time()

    # 没提交过：重新入队跑完整流程
    if not upstream_id:
        params = _params_of(t)
        if not params:
            store.update_task(
                tid, status="failed",
                error="无法恢复：任务参数已丢失",
                finished_at=time.time(),
            )
            return
        log.info("resume %s：未提交过，重新排队", tid)
        await _run_task(tid, params)
        return

    log.info("resume %s：续跟上游任务 %s", tid, upstream_id[:16])

    def _progress(pct: int, status: str) -> None:
        try:
            store.update_task(
                tid,
                percent=int(pct),
                upstream_status=status[:20],
                upstream_waiting=1 if status == "ACCEPTED" else 0,
            )
        except Exception:
            pass

    try:
        # 找回账号：先按 account_id，缺失时回退按 account_name。
        # 老数据（_on_account 还没写 account_id 的那批）只有 name，
        # 不兼容的话续跟必然失败 —— 上游任务明明还活着却被扔了。
        acct = store.get_account(aid) if aid else None
        if not acct and t.get("account_name"):
            acct = store.get_account_by_name(t["account_name"])
        if acct and not aid:
            # 补记 account_id，下次就不用再猜
            store.update_task(tid, account_id=acct["id"])
        if not acct:
            # 号真的没了：问不了上游。**不标 failed** ——
            # 上游任务可能还在跑，标 failed 会让下游以为彻底结束。
            store.update_task(
                tid, status="unknown",
                error=(
                    f"续跟失败：账号已不存在，无法从上游恢复。"
                    f"上游任务 {upstream_id} 可能仍在运行，可凭上游 ID 查询。"
                ),
                finished_at=time.time(),
            )
            return
        result = await pool.pool().resume(acct, upstream_id, on_progress=_progress)
        now = time.time()
        if result is None:
            # 账号正被别的任务占着 —— 不判死，标回 queued 让它在下次启动/手动重试时再来
            store.update_task(
                tid, status="unknown",
                error="续跟跳过：该账号当前被其他任务占用",
                finished_at=now,
            )
            return
        if result.get("status") == "COMPLETED":
            store.update_task(
                tid, status="completed",
                url=result.get("url") or "",
                percent=100,
                upstream_status="COMPLETED",
                upstream_waiting=0,
                # error 必须清空：续跟前它挂着"续跟中断/失联"之类的文案，
                # 任务明明成功了还带个错误，前端会显示成"成功但有错"，自相矛盾。
                error="",
                finished_at=now,
                elapsed=round(now - (t.get("created_at") or t_start), 1),
            )
        else:
            # 上游即真相：没 COMPLETED 就不算完。UNKNOWN/TIMEOUT 是"还没答案"，
            # FAILED 是上游真的判失败 —— 只有后者才写 failed。
            st = str(result.get("status") or "")
            store.update_task(
                tid,
                status="failed" if st == "FAILED" else "unknown",
                error=(
                    f"上游任务失败：{str(result.get('error'))[:400]}" if st == "FAILED"
                    else f"上游状态 {st or '未知'}，任务可能仍在运行"
                ),
                upstream_status=st[:20],
                finished_at=now,
            )
    except pool.NoCapacity as e:
        # 账号暂时占不到：不是任务失败，别判死。留给下一轮或下次启动续跟。
        store.update_task(tid, status="unknown", error=f"续跟暂不可用：{e}"[:500],
                          finished_at=time.time())
    except Exception as e:
        # 续跟过程中也断了：同样不是任务失败。上游 ID 还在库里，
        # 下次启动会再续。这里只如实标 unknown，绝不标 failed。
        store.update_task(tid, status="unknown",
                          error=f"续跟中断（{e}），上游任务 {upstream_id} 可能仍在运行"[:500],
                          finished_at=time.time())


async def _run_task(tid: str, params: dict[str, Any]) -> None:
    """异步任务执行体。

    状态语义严格区分"排队"和"在跑"：
      queued      -> 等号池分配账号
      in_progress -> 已占住账号，上游在生成

    耗时拆成三段分别记录，方便定位到底慢在哪：
      wait_s      排队等账号
      schedule_s  上游受理 -> 真正开始出片（调度延迟）
      gen_s       开始出片 -> 完成（真实生成）
    """
    t_enqueue = time.time()
    marks: dict[str, float] = {}

    def _progress(pct: int, status: str) -> None:
        # status 是上游**真实**状态：
        #   ACCEPTED    = 已受理，在等调度（此时上游不给 percent，就是 0）
        #   IN_PROGRESS = 正在执行，pct 是真实进度
        # 不做任何伪造：等待阶段进度就是 0，用户看"已等 X 分钟"和状态更诚实。
        try:
            store.update_task(
                tid,
                percent=int(pct),
                upstream_status=status[:20],
                upstream_waiting=1 if status == "ACCEPTED" else 0,
            )
        except Exception:
            pass

    def _on_account(acct: dict[str, Any]) -> None:
        # 拿到账号只代表"排到号了"，不代表上游已受理。
        # 状态仍留在 queued（前端显示"已分配账号，正在提交"），
        # 真正拿到上游 id 由 _on_submitted 切成 in_progress。
        # 之前这里直接写 in_progress，导致 429 退避期间界面显示"生成中"，
        # 实际上连 submit 都没成功。
        marks["account"] = time.time()
        try:
            store.update_task(
                tid,
                # account_id 必须一起落库：续跟（_resume_task）是按它找回账号的。
                # 只写 account_name 的话，号一改名/删除就再也接不回上游任务。
                account_id=acct.get("id"),
                account_name=acct.get("name") or "",
                started_at=marks["account"],
                queue_pos=0,
                wait_s=round(marks["account"] - t_enqueue, 2),
                upstream_status="SUBMITTING",
            )
        except Exception:
            pass

    def _on_submitted(upstream_id: str = "") -> None:
        # 拿到上游 id = 上游真的受理了。这时才切成 in_progress。
        marks["submitted"] = time.time()
        try:
            store.update_task(
                tid,
                status="in_progress",
                upstream_id=upstream_id or "",
                submitted_at=marks["submitted"],
                upstream_status="ACCEPTED",
                upstream_waiting=1,
                schedule_s=round(marks["submitted"] - marks.get("account", t_enqueue), 2),
            )
        except Exception:
            pass

    def _on_upstream() -> None:
        marks["upstream"] = time.time()
        try:
            store.update_task(
                tid,
                upstream_at=marks["upstream"],
                gen_s=round(marks["upstream"] - marks.get("submitted", t_enqueue), 2),
            )
        except Exception:
            pass

    try:
        result = await pool.pool().generate(
            params,
            on_progress=_progress,
            on_account=_on_account,
            on_submitted=_on_submitted,
            on_upstream=_on_upstream,
        )
        now = time.time()
        # gen_s 若还没写（上游一直没报进度就直接完成），用完成-受理补上
        gen = marks["upstream"] if "upstream" in marks else marks.get("submitted", t_enqueue)
        store.update_task(tid, gen_s=round(now - gen, 2), finished_at=now)
        elapsed = round(now - t_enqueue, 1)

        if result.get("status") == "COMPLETED":
            store.update_task(
                tid,
                status="completed",
                url=result.get("url") or "",
                upstream_id=result.get("upstream_id") or "",
                account_name=result.get("account_name") or "",
                percent=100,
                elapsed=elapsed,
            )
        elif str(result.get("status") or "") == "STALLED":
            # 上游没调度这个任务。**重新提交一次**（新 upstream_id）通常
            # 立刻就跑；旧任务留在上游等它自己超时，我们管不了。
            await _requeue(
                tid, params,
                f"上游未调度（{result.get('error') or 'STALLED'}），重新提交",
            )
        else:
            st = str(result.get("status") or "")
            store.update_task(
                tid,
                # UNKNOWN = 上游超时但任务可能还在跑。不能用 failed 表示，
                # 否则调用方会去重试，重复创建任务还会占名额。
                status="unknown" if st == "UNKNOWN" else "failed",
                error=str(result.get("error") or st)[:500],
                account_name=result.get("account_name") or "",
                elapsed=elapsed,
            )
    except pool.NoCapacity as e:
        # 拿不到号：**大概率**请求没提交出去（没有 upstream_id），真失败。
        # 但保险起见还是查一下 —— 万一 submit 其实成功了才拿不到号。
        await _on_failure(tid, e)
    except upstream.UpstreamError as e:
        await _on_failure(tid, e)
    except Exception as e:
        # 断流 / 读超时 / 进程抖动。upstream_id 早在 _on_submitted 就落库了，
        # 所以这些一律视为"失联"，接回来继续跟，不判失败。
        await _on_failure(tid, e)
    finally:
        # 最后一层保险：不论走到哪条路都不许留在非终态。
        #
        # 上面几个 except 都是 Exception；而 CancelledError 属于 BaseException，
        # 进程退出 / task 被取消时它会把上面全部绕过，行就永久停在
        # queued 或 in_progress —— 再也没人收尾，只会一直吃 count_running_tasks()
        # 的配额（见 store.reset_unfinished_tasks）。所以这里同步读一次状态兜底。
        #
        # ⚠️ 但有一个例外：**已经提交到上游的任务不许在这里判失败**。
        # 进程被取消/退出时上游任务还在跑，本地一句"任务已丢失"会把
        # 一个其实能出片的任务埋掉。那种情况留给启动续跟（resume_tasks）
        # 拿 upstream_id 去接回来，这里只处理"从没提交过"的。
        try:
            t = store.get_task(tid)
            if not t or t["status"] not in ("queued", "in_progress"):
                pass
            elif t["status"] == "cancelled":
                # 用户主动取消（2026-10-25 加）。
                # cancel_task 已经把状态写成 cancelled 了，这里**不许覆盖**
                # 成 failed —— "我点了取消"和"任务失败了"是两回事，
                # 混在一起会让统计和客户认知都出错。
                pass
            elif (t.get("upstream_id") or "").strip():
                # 提交过 → 交给启动续跟，这里不动它
                store.update_task(
                    tid, status="reviving",
                    error="本地执行中断（服务退出或任务被取消），将从上游恢复",
                )
            else:
                store.update_task(
                    tid,
                    status="failed",
                    error="任务被中断（服务退出或任务被取消），且尚未提交到上游",
                    finished_at=time.time(),
                )
        except Exception:
            pass


# ---------------------------------------------------------------- 上游即真相
#
# 原则（2026-10-24 定）：**任务一旦提交到上游，它的状态就只由上游说了算。**
#
# 本地会断：Server disconnected、读超时、进程重启、SSE 断流……
# 但这些都是"我们看不见了"，不是"任务失败了"。
# 实测：一个任务报 "Server disconnected without sending a response"，
# 上游其实 status=IN_PROGRESS、progress=68%，最后能出片。
#
# 所以失败分两种，处理完全不同：
#
#   没拿到 upstream_id   → 请求根本没提交，真失败，可以标 failed
#   拿到过 upstream_id   → 上游已受理，本地的一切异常都只是"失联"，
#                          处理 = 重新连上继续跟，**永远不标 failed**
#
# 这也让下游不必为了重试去重复创建任务（重复创建会白占并发名额）。
REVIVE_GRACE = 90.0     # 失联多久内才值得立刻重连（更老的交给启动续跟）


def _params_of(t: dict[str, Any]) -> dict[str, Any]:
    """从任务行里取回当初提交的参数（回队列重试用）。

    提交时把完整参数存进了 `params` 列（见 _spawn_async），
    剥掉了 `_xxx` 内部标记。回队列只需要这些公开参数就够了 ——
    内部标记（_named_media 之类）会在 _build_params 重新生成。
    """
    raw = t.get("params") or ""
    if not raw:
        return {}
    try:
        d = json.loads(raw)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _is_concurrency_busy(exc: Exception) -> bool:
    """这个异常是不是"号此刻被占"（429 concurrency_limit）。

    429 的含义是**号的名额被别的任务占着**，可能是：
      · 上游还有我们没跟完的旧任务
      · 上游名额释放比本地慢
      · 外部（网页端）在跑
    无论哪种，都不是"这个任务失败了" —— 等一会儿重试就好。
    """
    if isinstance(exc, upstream.UpstreamError):
        return exc.reason == "concurrency_limit" or exc.status == 429
    return False


# 429 回队列的最大次数。超过就真判失败 ——
# 无限回队列会让一个永远被占的号把任务挂到天荒地老，
# 而且每次回队列都会重新跑一遍 _run_task，白耗资源。
REQUEUE_MAX = 5


async def _requeue(tid: str, params: dict[str, Any], reason: str) -> None:
    """把任务放回队列等下次机会（不重新 submit 参数、不判失败）。

    用于"号此刻不可用"这类**临时**状况：429 名额被占。
    放进 queued 让号池重新派发 —— 池子会等号宣告可用，
    那时候再试。这样单个号的短暂占用不会把整批任务全部判死。

    有次数上限（REQUEUE_MAX）：超了就判 failed 并说明原因，
    不让任务无限回队列空转。
    """
    t = store.get_task(tid) or {}
    tries = int(t.get("requeued") or 0) + 1     # 专门的重排计数
    if tries > REQUEUE_MAX:
        store.update_task(
            tid, status="failed",
            error=f"已重排 {tries - 1} 次仍未成功，放弃：{reason}"[:500],
            finished_at=time.time(),
        )
        return
    store.update_task(
        tid, status="queued", account_id=None, account_name="",
        upstream_status="", error=f"{reason}（第 {tries}/{REQUEUE_MAX} 次重排）"[:500],
        finished_at=0, percent=0, requeued=tries,
    )
    job = asyncio.create_task(_run_task(tid, params))
    _TASKS[tid] = job

    def _discard(_t: asyncio.Task, _tid: str = tid) -> None:
        _TASKS.pop(_tid, None)

    job.add_done_callback(_discard)


async def _on_failure(tid: str, exc: Exception) -> None:
    """本地出错了。按"上游即真相"决定怎么收尾。

    判据（按优先级）：

      1) 429 名额被占 + 没提交成功 → **回队列重试**，不是失败
      2) 拿到过 upstream_id       → 失联，接回来继续跟，永不判 failed
      3) 从没拿到 upstream_id     → 真失败，标 failed

    第 1 条是重点：429 是"号此刻被占"，等一会儿就好。
    以前直接判 failed，于是一个号的短暂占用会把整批任务全部打死 ——
    实测 8 条任务里 2 条这么死的，剩下 6 条白等 20 分钟。
    """
    t = store.get_task(tid) or {}
    up_id = (t.get("upstream_id") or "").strip()
    aid = t.get("account_id")
    acct = store.get_account(aid) if aid else None
    if not acct and t.get("account_name"):
        acct = store.get_account_by_name(t["account_name"])

    if not up_id and _is_concurrency_busy(exc):
        # 号被占：回队列，让池子重新派发
        await _requeue(
            tid, _params_of(t),
            f"账号名额被占，已重新排队：{_explain(exc) if isinstance(exc, upstream.UpstreamError) else exc}",
        )
        return

    if not up_id:
        # 没提交过：这才是真失败
        detail = f"无可用账号：{exc}" if isinstance(exc, pool.NoCapacity) else str(exc)
        store.update_task(
            tid, status="failed",
            error=detail[:500], finished_at=time.time(),
        )
        return

    if acct is None:
        # 号没了，问不了上游：如实标 unknown，至少留下上游 ID
        store.update_task(
            tid, status="unknown",
            error=(
                f"本地连接中断（{type(exc).__name__}），且账号已不存在，无法从上游恢复。"
                f"上游任务 ID {up_id}，可手动查询。"
            ),
            finished_at=time.time(),
        )
        return

    # 有号、有上游 ID → 接回来继续跟。以上游状态为准。
    store.update_task(
        tid, status="in_progress",
        error=f"本地连接中断（{type(exc).__name__}），正在从上游恢复",
    )
    await _follow_upstream(tid, acct, up_id, on_progress=_progress)


async def _follow_upstream(
    tid: str,
    acct: dict[str, Any],
    up_id: str,
    on_progress: Any = None,
) -> None:
    """以上游为准，把任务跟到终态。

    任何异常都视为"失联"，重连继续跟；只有上游给出终态才写终态。
    调用方保证：只在**已经拿到 upstream_id** 时调用。

    ⚠️ on_progress 必须传进来。以前没传，重连后进度和上游状态就再也不
    更新了 —— 前端一直卡在"ACCEPTED / 0%"，而上游其实早就 IN_PROGRESS
    99%。排查时完全被误导（实测卡了半小时显示 0%）。

    停止条件：**时间**，不是次数。超过 TASK_MAX_LIFETIME 还没拿到终态
    就标 unknown 退出（上游任务本身也不可能活过这个时长）——
    "永不放弃"不等于"无限重试"，否则一次持续失联就能把协程
    永远锁死在这里。
    """
    revived = 0
    started = time.time()
    reconnected = False          # 重连成功后要把"正在恢复"的文案清掉
    while True:
        try:
            result = await upstream.wait_result(acct, up_id, on_progress=on_progress)
            # 重连成功且拿到了上游真实状态 —— 之前那句
            # "本地连接中断，正在从上游恢复" 该撤了。
            # 不清的话任务明明在正常跑，前端却一直显示"恢复中"，
            # 排查时会被误导（实测就这么显示了几十分钟）。
            if not reconnected:
                reconnected = True
                store.update_task(tid, error="", upstream_status="")
            st = str(result.get("status") or "")
            if st == "COMPLETED":
                store.update_task(
                    tid, status="completed",
                    url=result.get("url") or "",
                    percent=100,
                    upstream_status="COMPLETED",
                    upstream_waiting=0,
                    error="",
                    finished_at=time.time(),
                    revived=revived,
                )
                if revived:
                    log.info("task %s 失联 %d 次后复活成功，url=%s",
                             tid, revived, result.get("url"))
                return
            if st == "FAILED":
                # 上游自己说失败 —— 这才是真的失败
                store.update_task(
                    tid, status="failed",
                    error=f"上游任务失败：{str(result.get('error'))[:400]}",
                    upstream_status="FAILED",
                    finished_at=time.time(),
                    revived=revived,
                )
                return
            # 其他（TIMEOUT / UNKNOWN）：上游也没说清，如实标 unknown，
            # 不标 failed —— 否则下游会重试，重复创建还占名额。
            store.update_task(
                tid, status="unknown",
                error=(
                    f"上游状态 {st or '未知'}，任务 {up_id} 可能仍在运行，"
                    "可凭上游 ID 查询。"
                ),
                upstream_status=st[:20],
                finished_at=time.time(),
                revived=revived,
            )
            return
        except asyncio.CancelledError:
            raise
        except Exception as e:
            # 失联：重连继续跟。**只要上游没给终态，就不算完。**
            #
            # 但"永不放弃"不等于"无限重试"—— 必须有硬性停止条件，
            # 否则一次持续失联就会让这个协程永远转下去，白占内存、
            # 把账号锁抱死、日志刷爆。停止条件按**时间**算，不按次数：
            # 上游任务本身有生命周期（TASK_MAX_LIFETIME），超过它任务
            # 在上游也不可能还活着了，再跟没意义。
            revived += 1
            if time.time() - started > pool.TASK_MAX_LIFETIME:
                store.update_task(
                    tid, status="unknown",
                    error=(
                        f"失联重连超过 {int(pool.TASK_MAX_LIFETIME)}s 仍未拿到终态"
                        f"（已重连 {revived} 次）。上游任务 {up_id} 可能仍在运行，"
                        "可凭上游 ID 查询。"
                    ),
                    finished_at=time.time(),
                    revived=revived,
                )
                log.error("task %s 失联重连超时放弃（%d 次），上游 %s",
                          tid, revived, up_id)
                return
            # 重连前刷新一下账号（token 可能刚过期）
            fresh = store.get_account(acct["id"]) or acct
            acct = fresh
            log.warning("task %s 失联（%s），重连继续跟上游 %s（第 %d 次）",
                        tid, type(e).__name__, up_id, revived)
            store.update_task(
                tid, status="in_progress",
                error=f"本地连接中断，正在重新连接（第 {revived} 次）",
                revived=revived,
            )
            # 指数退避，上限 30s，**下限 2s**。
            # 下限很重要：wait_result 立即抛错时（DNS 失败、连接被拒），
            # 没有下限的话一秒能重试几十次 —— 那不是重连，是 DDoS 上游。
            await asyncio.sleep(min(30.0, max(2.0, 2.0 * revived)))


async def _adopt_upstream(tid: str, acct: dict[str, Any], up_id: str) -> None:
    """本地已经失败/中断，但上游可能还活着 —— 接回来继续跟。

    和 _follow_upstream 的区别：这个用于**已知失联过一次**的场合
    （比如异常处理、启动续跟），先快速问一次状态再决定跟不跟。
    但判据和 _follow_upstream 一样：以上游为准，不自己猜失败。
    """
    try:
        result = await upstream.wait_result(acct, up_id, timeout=REVIVE_GRACE)
    except Exception:
        # 问也问不到：不能判失败，只能如实说"状态未知"
        store.update_task(
            tid, status="unknown",
            error=(
                f"本地连接中断且暂时无法重新连接。上游任务 {up_id} 可能仍在运行，"
                "可凭上游 ID 查询，或稍后重试。"
            ),
            finished_at=time.time(),
        )
        return
    st = str(result.get("status") or "")
    if st == "COMPLETED":
        store.update_task(
            tid, status="completed",
            url=result.get("url") or "",
            percent=100,
            upstream_status="COMPLETED",
            upstream_waiting=0,
            error="",
            finished_at=time.time(),
        )
    else:
        # 还没出结果：继续跟
        await _follow_upstream(tid, acct, up_id)


def _explain(e: upstream.UpstreamError) -> str:
    """把上游错误翻成人话，别让用户看 reason code。"""
    m = {
        "insufficient_credits": "额度不足（402）。免费档只能用 480P / 720P",
        "concurrency_limit": "该账号并发被占用（同时只能跑 1 个任务）",
        "unauthorized": "token 失效（401），请重新授权该账号",
        "upstream_blocked": "上游被拦截（Cloudflare 返回了 HTML，通常是限流）",
        "invalid_request": "参数不合法",
        "rate_limited": "auth 限流，稍后自动重试",
    }
    if e.reason in m:
        return m[e.reason]
    return f"{e.reason} (HTTP {e.status})"


# ---------------------------------------------------------------- 映射

def _closest_ratio(ar: float) -> str:
    table = {"16:9": 16 / 9, "9:16": 9 / 16, "1:1": 1.0, "4:3": 4 / 3, "3:4": 3 / 4}
    return min(table, key=lambda k: abs(table[k] - ar))


def _build_params(body: dict[str, Any]) -> dict[str, Any]:
    """把 OpenAI 风格入参映射到 wan3 的真实字段。

    真实字段（从上游 400 报错逐个确认）：
      model / prompt / negative_prompt / resolution / ratio / duration /
      audio / seed / media / enable_thinking / watermark

    坑点：
      · 字段名是 ratio，不是 aspect_ratio（后者不报错但被忽略）
      · duration 只有 -1 或 2~30，没有 0/1
      · media 元素必须 {type, url}，type 见 config.MEDIA_TYPES
      · prompt 和 media 至少给一个
    """
    model = upstream.normalize_model(body.get("model") or settings.default_model)
    ratio = body.get("ratio") or body.get("aspect_ratio") or settings.default_ratio

    size = body.get("size")
    if size and "x" in str(size):
        try:
            w, h = (int(x) for x in str(size).lower().split("x"))
            if w and h:
                ratio = _closest_ratio(w / h)
        except Exception:
            pass
    if ratio not in RATIOS:
        ratio = settings.default_ratio

    resolution = str(body.get("resolution") or settings.default_resolution).upper()

    # 只做 0 积分档。收费档**明确拒绝**，不静默改参数 ——
    # 静默降级会让调用方以为自己拿到的是 1080P，是之前一堆坑的根源。
    if model.endswith("-prime"):
        raise HTTPException(
            400,
            "这个服务只跑免费档 wan3.0-video。"
            "wan3.0-video-prime 会扣积分（480P 就要 15 积分），不提供。",
        )
    if resolution in PAID_RESOLUTIONS:
        raise HTTPException(
            400,
            f"{resolution} 要扣积分（5 秒 30 积分）。"
            f"免费只有 {' / '.join(FREE_RESOLUTIONS)}，请改 resolution。",
        )
    if resolution not in FREE_RESOLUTIONS:
        raise HTTPException(
            400,
            f"resolution 只能是 {' / '.join(FREE_RESOLUTIONS)}，收到 {resolution!r}",
        )

    duration = upstream.clamp_duration(
        body.get("duration", settings.default_duration)
    )

    media: list[dict[str, Any]] = []
    named: dict[str, int] = {}

    def _add(mtype: str, url: Any, name: str = "") -> None:
        """收集一个素材。空白 URL 直接丢弃。

        ⚠️ 必须 strip 之后再判空（2026-10-25 修，Go 版同样有这个缺陷）。

        只判 `not url` 会漏掉全是空白的情况：`"  "` / `"\\t"` / `"\\n"` 都是
        truthy，会被原样送进 media，然后上游回 400「素材格式不对」。

        那个错误信息把排查方向完全带偏 —— 客户以为是图片格式/尺寸问题，
        反复换图，而真实原因是他在 JSON 里多打了一个空格。
        实测：7 种输入里 5 种（空格/tab/换行/混合）都会泄漏。

        strip 之后还要把**干净的值**存进去：`"  https://a/1.png  "` 这种
        首尾带空白的 URL，上游也可能判非法。
        """
        if url is None:
            return
        s = str(url).strip()
        if not s:
            return
        if mtype:
            mtype = str(mtype).strip()
        if not mtype:
            return
        if name:
            named[str(name).strip()] = len(media)
        media.append({"type": mtype, "url": s})

    # 也允许一次性传完整 media 定义（最灵活，支持 name）
    for m in (body.get("media") or []):
        if isinstance(m, dict):
            _add(m.get("type"), m.get("url"), m.get("name") or "")

    _add("first_frame", body.get("start_frame"), body.get("start_frame_name") or "首帧")
    _add("last_frame", body.get("end_frame"), body.get("end_frame_name") or "末帧")

    # 参考素材支持两种写法：
    #   "image_urls": ["url1", "url2"]
    #   "image_urls": [{"url":"url1","name":"主角"}, {"url":"url2","name":"产品"}]
    for key, mtype in (("image_urls", "reference_image"),
                       ("video_urls", "reference_video"),
                       ("audio_urls", "reference_audio")):
        for item in _as_list_full(body.get(key)):
            if isinstance(item, str):
                _add(mtype, item)
            elif isinstance(item, dict):
                _add(mtype, item.get("url"), item.get("name") or "")
    # 单数别名
    _add("reference_image", body.get("image"), body.get("image_name") or "")
    _add("reference_video", body.get("video"), body.get("video_name") or "")
    _add("reference_audio", body.get("audio_url"), body.get("audio_name") or "")

    try:
        media = upstream.validate_media(media)
    except upstream.UpstreamError as e:
        raise HTTPException(400, str(e.detail))

    # prompt 也要 strip（同 _add 的理由）：全是空白的 prompt 等于是空的，
    # 但如果残留空格就送到上游，会变成"有空 prompt 但没有实际内容"，
    # 上游可能接受并生成一个随机结果 —— 客户会觉得"我明明没写内容它却出片了"。
    prompt = str(body.get("prompt") or "").strip()
    if not prompt and not media:
        raise HTTPException(400, "prompt 或至少一个媒体输入必填")

    params: dict[str, Any] = {
        "model": model,
        "resolution": resolution,
        "ratio": ratio,
        "duration": duration,
        "audio": bool(body.get("audio", settings.default_audio)),
        "enable_thinking": bool(body.get("enable_thinking", False)),
        "watermark": bool(body.get("watermark", False)),
    }
    if prompt:
        params["prompt"] = prompt
    if body.get("negative_prompt"):
        params["negative_prompt"] = str(body["negative_prompt"])
    if body.get("prompt_extend") is not None:
        params["prompt_extend"] = bool(body["prompt_extend"])
    if media:
        params["media"] = media
    if body.get("seed") is not None:
        # Picsart 网关：seed 必须 0~2147483647，传 -1 会 400
        # （阿里官方说 -1 = 随机，但这边不认）
        try:
            params["seed"] = max(0, min(2147483647, int(body["seed"])))
        except (TypeError, ValueError):
            pass
    if named:
        params["_named_media"] = named
    return params


def _as_list_full(v: Any) -> list[Any]:
    """素材列表：元素可以是 URL 字符串，也可以是 {url, name} 对象。"""
    if not v:
        return []
    if isinstance(v, (str, dict)):
        return [v]
    if isinstance(v, (list, tuple)):
        return [x for x in v if x]
    return []
