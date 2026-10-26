"""SQLite 存储。零 ORM，纯 sqlite3，容器里最省事。

表：
  clients    OAuth DCR 注册的 client（client_id / redirect_uri）
  accounts   号池账号（token / cookie / 状态 / 配额）
  features   account × feature 的可用性缓存
  tasks      生成任务
  settings   键值杂项
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from typing import Any

from .config import settings

_LOCK = threading.RLock()
_conn: sqlite3.Connection | None = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS clients (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id     TEXT NOT NULL,
    redirect_uri  TEXT NOT NULL UNIQUE,
    created_at    REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS accounts (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT NOT NULL UNIQUE,
    notes         TEXT NOT NULL DEFAULT '',
    kind          TEXT NOT NULL DEFAULT 'oauth',   -- oauth | cookie
    access_token  TEXT NOT NULL DEFAULT '',
    refresh_token TEXT NOT NULL DEFAULT '',
    cookie        TEXT NOT NULL DEFAULT '',
    device_id     TEXT NOT NULL DEFAULT '',
    sub           TEXT NOT NULL DEFAULT '',
    username      TEXT NOT NULL DEFAULT '',
    email         TEXT NOT NULL DEFAULT '',
    package_id    TEXT NOT NULL DEFAULT '',
    credits       REAL NOT NULL DEFAULT 0,
    tier_credits  REAL NOT NULL DEFAULT 0,
    renew_at      TEXT NOT NULL DEFAULT '',
    expires_at    REAL NOT NULL DEFAULT 0,
    status        TEXT NOT NULL DEFAULT 'ok',      -- ok | cooling | dead | no_credits
    cooldown_until REAL NOT NULL DEFAULT 0,
    fails         INTEGER NOT NULL DEFAULT 0,
    last_error    TEXT NOT NULL DEFAULT '',
    created_at    REAL NOT NULL,
    updated_at    REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS features (
    account_id    INTEGER NOT NULL,
    feature_id    TEXT NOT NULL,
    available     INTEGER NOT NULL DEFAULT 1,
    checked_at    REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (account_id, feature_id),
    FOREIGN KEY (account_id) REFERENCES accounts(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS tasks (
    id            TEXT PRIMARY KEY,
    account_id    INTEGER,
    account_name  TEXT NOT NULL DEFAULT '',
    model         TEXT NOT NULL DEFAULT '',
    resolution    TEXT NOT NULL DEFAULT '',
    prompt        TEXT NOT NULL DEFAULT '',
    status        TEXT NOT NULL DEFAULT 'queued',
    upstream_id   TEXT NOT NULL DEFAULT '',
    url           TEXT NOT NULL DEFAULT '',
    error         TEXT NOT NULL DEFAULT '',
    elapsed       REAL NOT NULL DEFAULT 0,
    created_at    REAL NOT NULL,
    updated_at    REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS kv (
    k TEXT PRIMARY KEY,
    v TEXT NOT NULL
);

-- 下游调用方（API Key）。
--
-- 为什么单独建表而不用环境变量里的逗号分隔：环境变量没法按 key
-- 计量、没法单独禁用、没法看谁在滥用。号池是要给多个下游用的，
-- 这些信息必须有地方落。
CREATE TABLE IF NOT EXISTS api_keys (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    key           TEXT NOT NULL UNIQUE,        -- 明文 key（sk-xxx）
    name          TEXT NOT NULL DEFAULT '',    -- 调用方备注
    enabled       INTEGER NOT NULL DEFAULT 1,  -- 1=启用 0=禁用
    daily_limit   INTEGER NOT NULL DEFAULT 0,  -- 每日提交上限，0=不限
    created_at    REAL NOT NULL,
    last_used_at  REAL NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_tasks_created ON tasks(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_accounts_status ON accounts(status);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_api_keys_key ON api_keys(key);
-- 注意：idx_tasks_apikey 不在这里建 —— api_key_id 是后加的列（见 _MIGRATIONS），
-- 建表时它还不存在，索引会失败。放到 _migrate 之后建。
"""


def conn() -> sqlite3.Connection:
    global _conn
    with _LOCK:
        if _conn is None:
            _conn = sqlite3.connect(
                settings.db_path, check_same_thread=False, timeout=30
            )
            _conn.row_factory = sqlite3.Row
            _conn.execute("PRAGMA journal_mode=WAL")
            _conn.execute("PRAGMA foreign_keys=ON")
            _conn.executescript(SCHEMA)
            _migrate(_conn)
            _conn.commit()
        return _conn


# 轻量迁移：给老库补列，避免 SQLite 不支持 ADD COLUMN IF NOT EXISTS 的问题
_MIGRATIONS = [
    ("accounts", "notes", "TEXT NOT NULL DEFAULT ''"),
    ("accounts", "tier", "TEXT NOT NULL DEFAULT ''"),
    ("accounts", "photo", "TEXT NOT NULL DEFAULT ''"),
    # refresh_token 必须用签发它的 client_id 去刷新，所以要记住来源
    ("accounts", "client_id", "TEXT NOT NULL DEFAULT ''"),
    # 每个账号一个独立 OAuth client 的槽位（拼进 redirect_uri 的 query）。
    # 重新登录时必须沿用，否则换了 client_id -> refresh_token 失效。
    ("accounts", "client_slot", "TEXT NOT NULL DEFAULT ''"),
    # 任务进度：上游 progress.percent，方便前端显示"跑到 60% 了"
    ("tasks", "percent", "INTEGER NOT NULL DEFAULT 0"),
    # 提交的完整参数（JSON），任务页要展示"当时到底发了什么"
    ("tasks", "params", "TEXT NOT NULL DEFAULT ''"),
    # 队列序号：提交时排在第几个。0 = 已在跑或已完成
    ("tasks", "queue_pos", "INTEGER NOT NULL DEFAULT 0"),
    # 开始执行的时间戳（拿到账号那一刻），用于区分"排队等了多久"和"跑了多久"
    ("tasks", "started_at", "REAL NOT NULL DEFAULT 0"),
    # 上游受理时刻（submit 返回 201 的那一刻）
    ("tasks", "submitted_at", "REAL NOT NULL DEFAULT 0"),
    # 上游真正开始出片的时刻（首次离开 ACCEPTED）
    ("tasks", "upstream_at", "REAL NOT NULL DEFAULT 0"),
    # 完成时刻
    ("tasks", "finished_at", "REAL NOT NULL DEFAULT 0"),
    # 排队耗时 / 上游调度耗时 / 生成耗时，三个分开存，方便排查到底卡在哪
    ("tasks", "wait_s", "REAL NOT NULL DEFAULT 0"),
    ("tasks", "schedule_s", "REAL NOT NULL DEFAULT 0"),
    ("tasks", "gen_s", "REAL NOT NULL DEFAULT 0"),
    # 1 = 上游已受理但还在排队（ACCEPTED）
    ("tasks", "upstream_waiting", "INTEGER NOT NULL DEFAULT 0"),
    # 上游原始状态字符串（ACCEPTED / IN_PROGRESS / ...），原样透出让前端展示
    ("tasks", "upstream_status", "TEXT NOT NULL DEFAULT ''"),
    # 复活次数。本地连接断了但上游任务还活着时，接回来继续跟的次数。
    # 记次数只为观测和防失控，**不**用于决定能不能复活 ——
    # 只要上游还没给终态，就该一直跟下去（上游是唯一真相源）。
    ("tasks", "revived", "INTEGER NOT NULL DEFAULT 0"),
    # 重新排队次数。429（号被占）/ STALLED（上游没调度）时重新入队的次数。
    # 和 revived 分开：两者含义不同，混在一个列里就看不出
    # "到底是失联重连多，还是反复重排多"。
    ("tasks", "requeued", "INTEGER NOT NULL DEFAULT 0"),
    # 提交这个任务的 API Key。按调用方计量（提交量/成功率/耗时）用。
    # 老任务为 0，表示"环境变量 key 或未知来源"。
    ("tasks", "api_key_id", "INTEGER NOT NULL DEFAULT 0"),
]


def _migrate(c: sqlite3.Connection) -> None:
    for table, col, ddl in _MIGRATIONS:
        cols = {r["name"] for r in c.execute(f"PRAGMA table_info({table})")}
        if col not in cols:
            c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")

    # 依赖后加列的索引，必须等列建好再建。
    # api_key_id 就是后加的（见 _MIGRATIONS），建表时的 SCHEMA 里没有它。
    try:
        c.execute(
            "CREATE INDEX IF NOT EXISTS idx_tasks_apikey ON tasks(api_key_id)"
        )
    except sqlite3.OperationalError:
        pass

    # 老版 clients 表是单行表（CHECK id = 1），要重建成按 redirect_uri 多行
    ddl = c.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='clients'"
    ).fetchone()
    if ddl and "CHECK (id = 1)" in (ddl["sql"] or ""):
        c.execute("ALTER TABLE clients RENAME TO clients_old")
        c.execute(
            "CREATE TABLE clients ("
            " id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " client_id TEXT NOT NULL,"
            " redirect_uri TEXT NOT NULL UNIQUE,"
            " created_at REAL NOT NULL)"
        )
        c.execute(
            "INSERT INTO clients (client_id, redirect_uri, created_at) "
            "SELECT client_id, redirect_uri, created_at FROM clients_old"
        )
        c.execute("DROP TABLE clients_old")


def init() -> None:
    conn()


# ---------------------------------------------------------------- clients

def save_client(client_id: str, redirect_uri: str) -> None:
    """按 redirect_uri 分别存 client。

    ⚠️ 必须按 redirect_uri 区分，不能全库只用一条：
    refresh_token 是和签发它的 client_id 绑定的，如果用 A 流程（/oauth/callback）
    签发的 refresh_token 拿 B 流程（/oauth/manual-callback）的 client_id 去刷新，
    auth 会直接 400 无效授权 —— 表现为"账号莫名其妙续期失败"。
    """
    with _LOCK:
        c = conn()
        c.execute(
            "INSERT INTO clients (client_id, redirect_uri, created_at) VALUES (?,?,?) "
            "ON CONFLICT(redirect_uri) DO UPDATE SET client_id=excluded.client_id, "
            "created_at=excluded.created_at",
            (client_id, redirect_uri, time.time()),
        )
        c.commit()


def get_client(redirect_uri: str | None = None) -> dict[str, Any] | None:
    """取指定 redirect_uri 的 client；不传则取任一条（兼容旧调用）。"""
    with _LOCK:
        c = conn()
        if redirect_uri:
            row = c.execute(
                "SELECT * FROM clients WHERE redirect_uri=?", (redirect_uri,)
            ).fetchone()
        else:
            row = c.execute("SELECT * FROM clients ORDER BY id DESC LIMIT 1").fetchone()
        return dict(row) if row else None


# ---------------------------------------------------------------- accounts

_ACCOUNT_FIELDS = (
    "name", "notes", "kind", "access_token", "refresh_token", "cookie", "device_id",
    "sub", "username", "email", "tier", "photo", "package_id", "credits", "tier_credits",
    "renew_at", "expires_at", "status", "cooldown_until", "fails", "last_error",
    # ⚠️ client_id 必须在这里。refresh_token 是**单次使用**且绑定签发它的 client，
    # 少这一列 → upsert 静默丢弃 → 走 manual 模式重新登录后库里还是旧 client
    # → refresh 拿错 client 打 → invalid_grant → fails 3 → 账号被判 dead。
    # 这个字段曾经在 _MIGRATIONS 里加过列却没进白名单，等于白加。
    "client_id",
    # 同上：不在白名单里的字段会被 upsert 静默丢弃。
    "client_slot",
)


def upsert_account(data: dict[str, Any]) -> int:
    now = time.time()
    with _LOCK:
        c = conn()
        name = data["name"]
        row = c.execute("SELECT id FROM accounts WHERE name=?", (name,)).fetchone()
        if row:
            fields = [f for f in _ACCOUNT_FIELDS if f in data]
            sets = ", ".join(f"{f}=?" for f in fields)
            c.execute(
                f"UPDATE accounts SET {sets}, updated_at=? WHERE id=?",
                [data[f] for f in fields] + [now, row["id"]],
            )
            aid = row["id"]
        else:
            fields = [f for f in _ACCOUNT_FIELDS if f in data]
            cols = ", ".join(["created_at", "updated_at"] + fields)
            marks = ", ".join(["?"] * (2 + len(fields)))
            cur = c.execute(
                f"INSERT INTO accounts ({cols}) VALUES ({marks})",
                [now, now] + [data[f] for f in fields],
            )
            aid = cur.lastrowid
        c.commit()
        return int(aid)


def list_accounts() -> list[dict[str, Any]]:
    with _LOCK:
        rows = conn().execute("SELECT * FROM accounts ORDER BY id").fetchall()
        return [dict(r) for r in rows]


# ---------------------------------------------------------------- 账号可用性
#
# 为什么需要这个（2026-10-26）
# ----------------------------
# 「这个号能不能跑 wan3」原本散在 6 个地方各写一遍，而且**都只看有没有
# 凭证**，不看账号状态、也不看订阅档位。于是：
#
#   · status=dead 的失效号被算进"并发能力"
#   · tier=free 的号被当成能用 —— 但 free 账号没有 wan3 权限，
#     提交上去必然失败，等于浪费一次调度
#
# 现在收敛成一个函数，所有判断都调它，口径不可能再漂。

# 明确**不能**跑 wan3 的档位。其余（含空串）都算可用。
#
# ⚠️ 空串必须算可用：`fetch_tier()` 查不到时返回 ""，而且新加的号
# 在 refresh_account_info 跑完之前 tier 就是空的。如果空串算不可用，
# 刚授权的号会立刻显示"不可用"，用户会以为授权失败。
#
# 也就是说：**只有明确知道它是 free 才排除**，不知道就放行
# （让上游去拒绝，错误信息比我们自己猜更准确）。
_BLOCKED_TIERS = ("free",)


def tier_blocked(account: dict[str, Any]) -> bool:
    """这个账号的订阅档位是否明确不支持 wan3。"""
    tier = str(account.get("tier") or "").strip().lower()
    return tier in _BLOCKED_TIERS


def account_usable(account: dict[str, Any]) -> bool:
    """这个账号现在能不能接任务。

    三个条件全部满足才算可用：
      1. 有凭证（access_token 或 cookie）
      2. status 是 ok（dead / cooling 等都不行）
      3. 档位不是明确受限的 free

    和 `pool._credential_dead()` 的判据一致 —— 那边也是先看 status，
    所以两处不会出现"列表说可用、调度说不行"的矛盾。
    """
    if not (account.get("access_token") or account.get("cookie")):
        return False
    if account.get("status") != "ok":
        return False
    return not tier_blocked(account)


def usable_accounts() -> list[dict[str, Any]]:
    """所有现在能接任务的账号。"""
    return [a for a in list_accounts() if account_usable(a)]



def get_account(aid: int) -> dict[str, Any] | None:
    with _LOCK:
        row = conn().execute("SELECT * FROM accounts WHERE id=?", (aid,)).fetchone()
        return dict(row) if row else None


def get_account_by_name(name: str) -> dict[str, Any] | None:
    with _LOCK:
        row = conn().execute("SELECT * FROM accounts WHERE name=?", (name,)).fetchone()
        return dict(row) if row else None


def update_account(aid: int, **fields: Any) -> None:
    if not fields:
        return
    with _LOCK:
        c = conn()
        sets = ", ".join(f"{k}=?" for k in fields)
        c.execute(
            f"UPDATE accounts SET {sets}, updated_at=? WHERE id=?",
            list(fields.values()) + [time.time(), aid],
        )
        c.commit()


def delete_account(aid: int) -> None:
    with _LOCK:
        c = conn()
        c.execute("DELETE FROM features WHERE account_id=?", (aid,))
        c.execute("DELETE FROM accounts WHERE id=?", (aid,))
        c.commit()


# ---------------------------------------------------------------- features

def set_feature(aid: int, feature_id: str, available: bool) -> None:
    with _LOCK:
        c = conn()
        c.execute(
            "INSERT INTO features (account_id, feature_id, available, checked_at) "
            "VALUES (?,?,?,?) ON CONFLICT(account_id, feature_id) DO UPDATE SET "
            "available=excluded.available, checked_at=excluded.checked_at",
            (aid, feature_id, 1 if available else 0, time.time()),
        )
        c.commit()


def get_features(aid: int) -> dict[str, bool]:
    with _LOCK:
        rows = conn().execute(
            "SELECT feature_id, available FROM features WHERE account_id=?", (aid,)
        ).fetchall()
        return {r["feature_id"]: bool(r["available"]) for r in rows}


def clear_features(aid: int) -> None:
    with _LOCK:
        c = conn()
        c.execute("DELETE FROM features WHERE account_id=?", (aid,))
        c.commit()


# ---------------------------------------------------------------- tasks

def create_task(tid: str, **fields: Any) -> None:
    """插入一条任务。

    ⚠️ created_at / updated_at 会自动补，但**允许调用方覆盖**
    （续跟、补录、测试都要指定真实创建时间）。以前是无条件写 now，
    如果 fields 里也带了 created_at，就会拼出重复列名 → SQL 报错；
    更糟的是静默覆盖了调用方给的时间，导致「按创建时间判断是否超龄」
    的逻辑永远拿到 now —— 超龄任务会被误当成新任务续跟。

    id 同理：以参数 tid 为准，fields 里带 id 会被忽略。
    """
    now = time.time()
    fields = {k: v for k, v in fields.items() if k != "id"}
    fields.setdefault("created_at", now)
    fields.setdefault("updated_at", now)
    with _LOCK:
        c = conn()
        cols = ["id"] + list(fields.keys())
        marks = ", ".join(["?"] * len(cols))
        c.execute(
            f"INSERT INTO tasks ({', '.join(cols)}) VALUES ({marks})",
            [tid] + list(fields.values()),
        )
        c.commit()


def update_task(tid: str, **fields: Any) -> None:
    if not fields:
        return
    with _LOCK:
        c = conn()
        sets = ", ".join(f"{k}=?" for k in fields)
        c.execute(
            f"UPDATE tasks SET {sets}, updated_at=? WHERE id=?",
            list(fields.values()) + [time.time(), tid],
        )
        c.commit()


def get_task(tid: str) -> dict[str, Any] | None:
    with _LOCK:
        row = conn().execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
        return dict(row) if row else None


def list_tasks(limit: int = 50) -> list[dict[str, Any]]:
    with _LOCK:
        rows = conn().execute(
            "SELECT * FROM tasks ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]


def count_running_tasks() -> int:
    with _LOCK:
        row = conn().execute(
            "SELECT COUNT(*) n FROM tasks WHERE status IN ('queued','in_progress')"
        ).fetchone()
        return int(row["n"])


def list_resumable_tasks(max_age: float) -> list[dict[str, Any]]:
    """找出「还能接着跟」的任务：**拿过 upstream_id** + 未超龄。

    **上游即真相**：只要拿过 upstream_id，无论本地现在标成什么
    （queued / in_progress / failed / unknown / reviving），都该接回来跟。
    本地那些"失败"只是失联，上游任务其实还在跑 ——
    实测：一个报 "Server disconnected" 的任务，上游 progress=68% 照样出片。

    第二类：**queued 且参数还在**。这种连号都没拿到，重启对它只是
    "还没轮到"，参数完整存在 params 列里，重新入队即可 —— 不该判死，
    否则调用方明明提交成功了，重启一次就被告知失败。

    只有既没 upstream_id、参数又丢了的才真没法救，交给
    reset_unfinished_tasks 判失败。

    超龄的不续：那种任务多半上游早就清了，续跟只会白占账号锁。
    """
    cutoff = time.time() - max_age
    with _LOCK:
        rows = conn().execute(
            "SELECT * FROM tasks WHERE created_at > ? AND ("
            "  (upstream_id != '' AND status IN "
            "    ('queued','in_progress','failed','unknown','reviving'))"
            "  OR (status = 'queued' AND upstream_id = '' AND params != '')"
            ") ORDER BY created_at",
            (cutoff,),
        ).fetchall()
        return [dict(r) for r in rows]


def reset_unfinished_tasks(
    reason: str = "进程重启，任务已丢失", keep_ids: list[str] | None = None
) -> int:
    """把所有**无法续跟**的非终态任务标成 failed，返回改动行数。

    为什么必须有这个：`_spawn_async` 的并发上限判定用的就是
    `count_running_tasks()`（数 queued/in_progress）。而进程重启后，
    上一个进程留下的 queued/in_progress 行**没有任何东西会去收尾** ——
    跑它们的 asyncio task 已经随进程灰飞烟灭了。

    于是这些"僵尸行"只增不减：跑够 max_concurrent_tasks 次崩溃/重启之后，
    所有 wait:false 请求恒返回 429「任务队列已满」，而且**重启也不恢复** ——
    越重启越严重。所以启动时必须清一次。

    keep_ids：这些任务会被续跟（见 list_resumable_tasks），不能判死。
    """
    with _LOCK:
        c = conn()
        now = time.time()
        if keep_ids:
            marks = ",".join("?" * len(keep_ids))
            cur = c.execute(
                "UPDATE tasks SET status='failed', error=?, updated_at=?, "
                "finished_at=? WHERE status IN ('queued','in_progress') "
                f"AND id NOT IN ({marks})",
                [reason, now, now] + list(keep_ids),
            )
        else:
            cur = c.execute(
                "UPDATE tasks SET status='failed', error=?, updated_at=?, "
                "finished_at=? WHERE status IN ('queued','in_progress')",
                (reason, now, now),
            )
        c.commit()
        return cur.rowcount


# ---------------------------------------------------------------- api_keys

def create_api_key(key: str, name: str = "", daily_limit: int = 0) -> int:
    """建一个下游调用方 key。返回 id。"""
    with _LOCK:
        c = conn()
        cur = c.execute(
            "INSERT INTO api_keys (key, name, enabled, daily_limit, created_at) "
            "VALUES (?,?,1,?,?)",
            (key, name, int(daily_limit or 0), time.time()),
        )
        c.commit()
        return int(cur.lastrowid)


def list_api_keys() -> list[dict[str, Any]]:
    """全部 key（含禁用的），按创建时间。"""
    with _LOCK:
        rows = conn().execute(
            "SELECT * FROM api_keys ORDER BY created_at"
        ).fetchall()
        return [dict(r) for r in rows]


def get_api_key(key: str) -> dict[str, Any] | None:
    """按 key 明文查。鉴权时用。"""
    with _LOCK:
        row = conn().execute(
            "SELECT * FROM api_keys WHERE key=?", (key,)
        ).fetchone()
        return dict(row) if row else None


def update_api_key(aid: int, **fields: Any) -> None:
    """改 key 的属性（name / enabled / daily_limit）。"""
    allowed = ("name", "enabled", "daily_limit")
    fields = {k: v for k, v in fields.items() if k in allowed}
    if not fields:
        return
    with _LOCK:
        c = conn()
        sets = ", ".join(f"{k}=?" for k in fields)
        c.execute(f"UPDATE api_keys SET {sets} WHERE id=?",
                  list(fields.values()) + [aid])
        c.commit()


def delete_api_key(aid: int) -> None:
    with _LOCK:
        c = conn()
        c.execute("DELETE FROM api_keys WHERE id=?", (aid,))
        c.commit()


def touch_api_key(aid: int) -> None:
    """记一次使用时间。节流写库，别每次请求都 commit。"""
    with _LOCK:
        c = conn()
        c.execute(
            "UPDATE api_keys SET last_used_at=? WHERE id=? AND last_used_at<?",
            (time.time(), aid, time.time() - 60),
        )
        c.commit()


# ---------------------------------------------------------------- kv

def kv_get(k: str, default: str = "") -> str:
    with _LOCK:
        row = conn().execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
        return row["v"] if row else default


def kv_set(k: str, v: str) -> None:
    with _LOCK:
        c = conn()
        c.execute(
            "INSERT INTO kv (k,v) VALUES (?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
            (k, v),
        )
        c.commit()


# ---------------------------------------------------------------- stats

def stats() -> dict[str, Any]:
    with _LOCK:
        c = conn()
        total = c.execute("SELECT COUNT(*) n FROM accounts").fetchone()["n"]
        # 「可用账号」必须和 account_usable() 同口径（2026-10-26）。
        #
        # 原来只数 status='ok'，但 tier=free 的号跑不了 wan3，把它算作
        # 可用会让客户以为"有号能用"，提交后必失败。
        #
        # 空 tier 算可用（理由见 account_usable 的注释：查不到不等于 free）。
        ok = c.execute(
            "SELECT COUNT(*) n FROM accounts "
            "WHERE status='ok' "
            "  AND (access_token != '' OR cookie != '') "
            "  AND LOWER(TRIM(COALESCE(tier,''))) NOT IN ('free')"
        ).fetchone()["n"]
        cooling = c.execute(
            "SELECT COUNT(*) n FROM accounts WHERE status='cooling'"
        ).fetchone()["n"]
        dead = c.execute(
            "SELECT COUNT(*) n FROM accounts WHERE status='dead'"
        ).fetchone()["n"]
        # free 档又没失效的号：单独给个数字，让用户知道"号是好的但没权限"。
        # 否则它们会凭空从「可用」里消失，用户不知道去哪儿了。
        free_tier = c.execute(
            "SELECT COUNT(*) n FROM accounts "
            "WHERE status='ok' "
            "  AND (access_token != '' OR cookie != '') "
            "  AND LOWER(TRIM(COALESCE(tier,''))) = 'free'"
        ).fetchone()["n"]
        tasks_today = c.execute(
            "SELECT COUNT(*) n FROM tasks WHERE created_at > ?",
            (time.time() - 86400,),
        ).fetchone()["n"]
        done = c.execute(
            "SELECT COUNT(*) n FROM tasks WHERE status='completed'"
        ).fetchone()["n"]
        failed = c.execute(
            "SELECT COUNT(*) n FROM tasks WHERE status='failed'"
        ).fetchone()["n"]
    return {
        "accounts_total": total,
        "accounts_ok": ok,
        "accounts_cooling": cooling,
        "accounts_dead": dead,
        # free 档且未失效的号数：状态健康但**没有 wan3 权限**。
        # 前端用它给一句解释，否则这些号会莫名从"可用"里消失。
        "accounts_free": free_tier,
        "tasks_24h": tasks_today,
        "tasks_completed": done,
        "tasks_failed": failed,
    }
