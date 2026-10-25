# Wan3 视频生成 API

**Base URL**：`https://你的域名  （部署后访问 /api/docs 可拿到填好真实域名的版本）`

这是一个 **OpenAI 风格**的视频生成接口。提交任务拿 `id`，轮询拿结果。

> 激活码 / 版本更新 / 报障，请联系：**Zzyuc88**（微信）
>
> 本文档由服务在运行时生成，档位、默认值、时长范围都取自当前生效的配置，**与线上完全一致**。可以直接把整份文件交给 AI 让它写调用代码。

---

## 0. 30 秒跑通

```bash
curl -X POST "https://你的域名  （部署后访问 /api/docs 可拿到填好真实域名的版本）/v1/videos/generations" \
  -H "content-type: application/json" \
  -H "authorization: Bearer YOUR_KEY" \
  -d '{"prompt": "海边日落，镜头缓慢推进", "resolution": "720P", "duration": 5}'
```

### 最少可以只传 prompt

```bash
curl -X POST "https://你的域名  （部署后访问 /api/docs 可拿到填好真实域名的版本）/v1/videos/generations" \
  -H "content-type: application/json" \
  -d '{"prompt": "海边日落"}'
```

不传的字段会用这些默认值：

| 字段 | 默认值 |
|---|---|
| `model` | `wan3.0-video` |
| `resolution` | **`720P`** |
| `ratio` | **`adaptive`**（`adaptive` = 让模型按 prompt 决定画幅） |
| `duration` | `5` 秒 |
| `audio` | `false` |
| `watermark` | `false` |

> `720P` 和 480P **都是 0 积分**，所以默认给画质更好的那个。需要更快出片（减少排队尾延迟）就显式传 `480P`。

返回：

```json
{
  "id": "vtask_3e9318bbfe9f4e2a8654dff3",
  "object": "video.generation",
  "status": "queued",
  "poll": "/v1/videos/generations/vtask_3e9318bbfe9f4e2a8654dff3"
}
```

然后每 **10~15 秒**轮询一次：

```bash
curl "https://你的域名  （部署后访问 /api/docs 可拿到填好真实域名的版本）/v1/videos/generations/vtask_3e9318bbfe9f4e2a8654dff3" \
  -H "authorization: Bearer YOUR_KEY"
```

完成后返回成片 URL：

```json
{"id": "vtask_...", "status": "completed", "progress": 100,
 "url": "https://.../xxx.mp4", "elapsed": 91.1}
```

---

## 1. 鉴权

当前**未启用** API Key 校验（`/v1/*` 对外开放）。如需收紧，请联系服务提供方配置 `PICSART_POOL_API_KEYS`。

---

## 2. 必读：这个号池是怎么工作的

**不理解这一节，写出来的代码一定会出错。** 这不是一个普通的 API 网关。

### 2.1 任务是**异步 + 慢**的

| 事实 | 数值 | 对代码的要求 |
|---|---|---|
| 单个任务耗时 | **实测 91 秒 ~ 23 分钟** | 超时时间至少设 30 分钟 |
| 轮询建议间隔 | **10~15 秒** | 不要用 `sleep(1)`，会被限流 |
| 提交是同步还是异步 | 默认异步（`202`） | 必须实现轮询 |

> `wait: true` 可以同步等结果，但会**一直占着 HTTP 连接**几分钟到二十分钟。只建议单次调试时用，不要用在批量任务里。

### 2.2 并发受号池限制

每个上游账号同时只能跑 **1 个**任务。号池里有 N 个号，就能并发 N 个任务。

所以提交任务时可能遇到：

| HTTP | 含义 | **你该做什么** |
|---|---|---|
| `202` | 已入队 | 正常，开始轮询 |
| `503` | 号池全忙（没有空闲账号） | **等待 30~60 秒后重新提交**，不要立即重试 |
| `429` | 撞上并发占用（瞬时） | 退避后重试，遵守 `Retry-After` 头 |

**关键区别**：`503` 表示整个池子满了；`429` 表示这一次提交撞车了。两者都要退避，但 `503` 通常要等更久。

### 2.3 不要重复提交

接口**不保证幂等**。网络超时后盲目重发 `POST` 会真的创建两个任务，白占两次名额、出两份片。

正确做法：

- 提交成功后**立刻保存返回的 `id`**
- 网络异常时，先按 `id` 查一次，确认真没创建再重发
- 或者给 prompt 里带一个你自己的业务单号，便于事后对账

## 3. 请求参数

### 3.1 基本参数

| 字段 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `prompt` | string | — | 提示词。与素材至少给一个 |
| `model` | string | `wan3.0-video` | 模型名 |
| `resolution` | string | `720P` | **只支持 `480P / 720P`**（免费档） |
| `duration` | int | `5` | `2`~`30` 秒；`-1` 表示智能 |
| `ratio` | string | `adaptive` | `16:9` `9:16` `1:1` `4:3` `3:4` `adaptive` |
| `audio` | bool | `false` | 输出是否含音轨 |
| `negative_prompt` | string | — | 负向提示词 |
| `seed` | int | — | 随机种子，同种子+同参数结果接近 |
| `enable_thinking` | bool | `false` | 深度思考（更慢，复杂场景用） |
| `watermark` | bool | `false` | 是否加水印 |
| `wait` | bool | `false` | `true` = 同步等出片再返回 |

### 3.2 素材参数（图生视频 / 首尾帧 / 参考视频）

| 字段 | 类型 | 说明 |
|---|---|---|
| `start_frame` | string | 首帧图 URL |
| `end_frame` | string | 尾帧图 URL（**必须同时给 `start_frame`**） |
| `image_urls` | array | 参考图，元素是 URL 或 `{url, name}` |
| `video_urls` | array | 参考视频，同上 |
| `audio_urls` | array | 参考音频，同上 |
| `media` | array | 上游原生数组 `{type, url, name?}` |

**素材组合规则（违反会 400）：**

- ✅ 参考图 / 参考视频 / 参考音频：可自由组合
- ✅ 只有 `first_frame`，或 `first_frame` + `last_frame`
- ❌ 首尾帧**不能**和其他任何素材混用
- ❌ 只有 `end_frame` 没有 `start_frame`

数量上限：参考图 10 · 参考视频 5 段（总时长 ≤15s）· 参考音频 5 段（≤15s）·首/尾帧各 1。

### 3.3 素材在 prompt 里怎么引用

素材按**数组顺序**编号，**图片、视频、音频分别计数**：

```
image_urls: [图A, 图B, 图C]   ->   图1、图2、图3
```

两个及以上素材时，**必须在 prompt 里写明编号**，否则模型不知道你说的是哪张：

```json
{"prompt": "让图1里的人物穿上图2的衣服", "image_urls": ["...", "..."]}
```

也可以给素材起名字，用 `@名字` 引用（服务会自动换成 `图N`）：

```json
{"prompt": "@主角 拿起 @杯子 喝了一口",
 "image_urls": [{"url": "...", "name": "主角"}, {"url": "...", "name": "杯子"}]}
```

---

## 4. 完整可运行示例

### 4.1 Python（推荐，含退避和超时处理）

```python
"""Wan3 视频生成 —— 生产可用的调用封装。

关键点：
  · 提交要处理 503/429 的退避（号池满 / 撞并发）
  · 轮询间隔 10~15 秒，不要更密
  · 总超时给到 30 分钟（实测单任务最长 23 分钟）
"""
import time
import httpx

BASE = "https://你的域名  （部署后访问 /api/docs 可拿到填好真实域名的版本）"
KEY = "YOUR_KEY"          # 没有启用鉴权时随便填
HEADERS = {"authorization": f"Bearer {KEY}", "content-type": "application/json"}


def submit(prompt: str, **kw) -> str:
    """提交任务，返回 id。自动处理号池满 / 并发占用的题。"""
    body = {"prompt": prompt, "resolution": "720P", "duration": 5}
    body.update(kw)

    delay = 5
    for attempt in range(6):
        r = httpx.post(f"{BASE}/v1/videos/generations",
                       headers=HEADERS, json=body, timeout=60)

        if r.status_code in (200, 202):
            return r.json()["id"]

        if r.status_code == 429:
            # 撞并发：听 Retry-After
            wait = float(r.headers.get("retry-after") or delay)
            time.sleep(wait)
            delay = min(delay * 2, 60)
            continue

        if r.status_code == 503:
            # 号池全忙：等久一点，别死磕
            time.sleep(max(30, delay))
            delay = min(delay * 2, 120)
            continue

        if r.status_code == 400:
            # 参数错误，重试没用
            raise ValueError(r.json().get("detail", r.text))

        r.raise_for_status()

    raise RuntimeError("提交失败：重试 6 次仍被拒（号池可能一直满）")


def wait_done(tid: str, timeout: float = 1800) -> dict:
    """轮询到完成。返回最终结果（含 url）。"""
    deadline = time.time() + timeout
    interval = 10          # 起点 10 秒

    while time.time() < deadline:
        r = httpx.get(f"{BASE}/v1/videos/generations/{tid}",
                      headers=HEADERS, timeout=30)
        r.raise_for_status()
        d = r.json()

        if d["status"] == "completed":
            return d
        if d["status"] == "failed":
            raise RuntimeError(f"生成失败：{d.get('error')}")

        time.sleep(interval)
        interval = min(interval + 2, 15)   # 逐步放慢到 15 秒

    raise TimeoutError(f"任务 {tid} 超时未完成")


if __name__ == "__main__":
    tid = submit("一只金毛在海边奔跑，夕阳，电影感")
    print("已提交:", tid)
    result = wait_done(tid)
    print("完成:", result["url"])
```

### 4.2 Node.js

```javascript
const BASE = "https://你的域名  （部署后访问 /api/docs 可拿到填好真实域名的版本）";
const KEY = "YOUR_KEY";
const HEADERS = {
  "authorization": `Bearer ${KEY}`,
  "content-type": "application/json",
};

async function submit(prompt, extra = {}) {
  const body = { prompt, resolution: "720P", duration: 5, ...extra };

  let delay = 5000;
  for (let i = 0; i < 6; i++) {
    const r = await fetch(`${BASE}/v1/videos/generations`, {
      method: "POST", headers: HEADERS, body: JSON.stringify(body),
    });

    if (r.ok) return (await r.json()).id;

    if (r.status === 429) {
      const ra = Number(r.headers.get("retry-after")) || delay / 1000;
      await new Promise(s => setTimeout(s, ra * 1000));
      delay = Math.min(delay * 2, 60000);
      continue;
    }
    if (r.status === 503) {
      await new Promise(s => setTimeout(s, Math.max(30000, delay)));
      delay = Math.min(delay * 2, 120000);
      continue;
    }
    if (r.status === 400) {
      const d = await r.json();
      throw new Error(d.detail || "参数错误");
    }
    throw new Error(`提交失败 HTTP ${r.status}`);
  }
  throw new Error("重试 6 次仍被拒");
}

async function waitDone(tid, timeoutMs = 1800000) {
  const deadline = Date.now() + timeoutMs;
  let interval = 10000;
  while (Date.now() < deadline) {
    const r = await fetch(`${BASE}/v1/videos/generations/${tid}`, { headers: HEADERS });
    const d = await r.json();
    if (d.status === "completed") return d;
    if (d.status === "failed") throw new Error(d.error || "生成失败");
    await new Promise(s => setTimeout(s, interval));
    interval = Math.min(interval + 2000, 15000);
  }
  throw new Error("超时未完成");
}

// 用法
const id = await submit("一只金毛在海边奔跑");
const result = await waitDone(id);
console.log(result.url);
```

---

## 5. 端点一览

### 5.1 给程序调用的（`/v1/*`，需要 API Key）

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/v1/videos/generations` | 提交生成任务 |
| GET | `/v1/videos/generations/{id}` | 查询任务状态与结果 |
| GET | `/v1/models` | 可用模型与档位 |
| GET | `/v1/credits` | 号池状态（可用账号数） |

### 5.2 管理页用的（`/api/*`，需要管理会话）

这些是给人点的，不是给程序调的。列在这里只为让你知道它们存在。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/tasks` | 任务列表 |
| GET | `/api/tasks/{id}` | 单任务详情（含完整参数） |
| POST | `/api/tasks/{id}/retry` | 失败任务重跑（参数不用重填） |
| POST | `/api/tasks/{id}/cancel` | 取消未提交的任务 |
| GET | `/api/accounts` | 号池账号列表 |
| GET | `/api/stats` | 统计 |
| GET | `/api/docs` | 本文档（`?download=1` 触发下载） |

### 5.3 自检端点（不需要登录）

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/__probe` | 从部署环境实测上游可达性 |
| GET | `/__diag` | 确认配置真的注入了上游模块 |
| GET | `/__version` | 版本与更新检查 |
| GET | `/__domain` | 确认公网地址推导正确 |

`/v1/credits` 可以用来判断现在忙不忙：

```json
{"accounts_ok": 3, "credits_total": 0, "details": [...]}
```

`accounts_ok` 是当前可用账号数。如果它长期低于你的并发需求，说明号池需要加号。

---

## 6. 任务状态

| `status` | 含义 | 你的代码该做什么 |
|---|---|---|
| `queued` | 已入队，等号池分配账号 | 继续轮询（间隔 10~15s） |
| `in_progress` | 正在生成 | 继续轮询；`progress` 是 0~100 |
| `completed` | 完成 | 取 `url` 下载 |
| `failed` | 失败 | 读 `error`，多数是参数或额度问题 |
| `cancelled` | **已取消**（用户通过管理页取消） | 不要再轮询，按终态处理 |

> `cancelled` 与 `failed` 是**两个不同的终态**。「我主动取消了」和「任务失败了」不该混在一起统计 ——如果你做成功率/失败率报表，把 `cancelled` 单独算一类。

> `progress` 来自上游真实进度。**不要用它判断还要多久** ——上游到 80% 就算完成，这里已归一到 100。

---

## 6.5 取消任务（仅限管理页）

```http
POST /api/tasks/{id}/cancel
```

**这个接口只在管理页用，`/v1/*` 调用方不需要它。**它要求管理会话，不是 API Key。

### 只有「还没提交到上游」的任务能取消

| 任务当前状态 | 能否取消 | 结果 |
|---|---|---|
| `queued`，且从未提交到上游 | ✅ 可以 | 立即取消，队列位置释放 |
| `queued`/`in_progress`，已提交到上游 | ❌ 返回 `409` | 上游会继续执行 |
| `completed` / `failed` / `cancelled` | ❌ 返回 `409` | 已是终态 |

**为什么已提交的不能取消**：上游没有公开的取消接口。本地「取消」只能让我们的代码不再管它，但那个任务仍在 Picsart 那边跑着、仍占着该账号的并发名额（每账号并发 = 1）。结果就是账号被一个「本地看不见的任务」锁住，队列里后面的任务干等 ——比不取消更糟。所以这里明确拒绝并说明原因。

返回：

```json
{"ok": true, "id": "vtask_...", "status": "cancelled"}
```

---

## 6.6 版本检查

```http
GET /__version
```

不需要登录/激活即可访问（管理页首屏要显示更新提示）。返回：

```json
{"current": "2026.10.25", "latest": "2026.11.01", "has_update": true,
 "url": "https://github.com/.../releases", "repo": "...",
 "checked_at": 1792912345, "error": ""}
```

| 字段 | 说明 |
|---|---|
| `current` | 当前运行的版本 |
| `latest` | 最新发布版本（查不到时为空） |
| `has_update` | 是否有更新。**查不到时恒为 false**，不会误报 |
| `error` | 非空表示这次没查成（网络问题），不影响任何功能 |

> 这只是**提示**，不会自动更新。代码在你的部署目录里，自动替换等于远程执行代码 —— 收益远小于风险。

---

## 7. 错误码与处置

| HTTP | 典型 `detail` | 原因 | **处置** |
|---|---|---|---|
| `400` | `resolution 只能是 480P / 720P` | 参数不合法 | 按文案改，**重试无用** |
| `400` | `首尾帧不能和其他任何素材混用` | 素材组合冲突 | 二选一 |
| `400` | `prompt 或至少一个媒体输入必填` | 内容为空 | 补内容 |
| `401` | `invalid api key` | Key 错 | 检查 `Authorization` |
| `402` | `额度不足` | 用了收费档或号没额度 | 改用免费档 |
| `403` | `不是已激活状态` | 部署未激活 | 联系服务提供方 |
| `429` | `该账号并发被占用` | 瞬时撞车 | **按 `Retry-After` 退避重试** |
| `503` | `号池无可用账号` | 池子全忙 | **等 30~60 秒重新提交** |
| `502` | 上游异常 | Picsart 侧问题 | 指数退避重试 |

错误体统一格式：

```json
{"detail": "中文人话说明"}
```

---

## 8. 免费档与收费

- **免费**：`480P / 720P` —— 不消耗积分，本服务主要面向这一档
- **收费**：`1080P` —— 会扣账号积分，额度不足返回 `402`

免费与**时长无关**：同样的分辨率下，5 秒和 30 秒都是 0 积分。

---

## 9. 容易踩的坑

1. **轮询太快会被限流**。10~15 秒是实测安全值。写 `sleep(1)` 会撞 429。
2. **不传 `resolution` 会按最高档计费**。务必显式传 `720P`。
3. **成片 URL 是临时的**。指向上游 CDN，可能被清理 —— **务必自行转存**。
4. **`wait: true` 不要用于批量**。它会一直占 HTTP 连接，10 个并发就把连接池吃光。
5. **重复提交不幂等**。网络异常先按 `id` 查，别盲目重发。
6. **素材要写编号**。两个以上素材时不写「图1/图2」，模型不知道你说哪个。
7. **`cancelled` 是独立终态**。做统计时别把它并进 `failed` —— 用户主动取消不是故障。
8. **不要试图取消已提交的任务**。上游没有取消接口，本地取消只会让账号被隐形任务占着。见 §6.5。

---

有问题联系：**Zzyuc88**（微信）
