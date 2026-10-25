# 用 WorkBuddy 把本应用发布成你自己的应用

这份文档是给**客户**看的。按它做，你可以把本项目发布成 `https://xxx.app.workbuddy.link`，
用你自己的域名访问、自己管理。源码是公开的，没有关系 —— 真正的关键（上游接口的
调用方式和参数规则）不在源码里，而在你的激活码背后。

---

## 一、先说清楚：为什么源码公开也没问题

这套程序分两层：

| 层 | 在哪 | 内容 |
|---|---|---|
| **程序逻辑** | 你手上的源码 | FastAPI 路由、SQLite 存储、HTTP 调用、任务队列 |
| **站点知识** | 只在我的授权服务器上 | 上游端点、必须携带的请求头、计费参数、免费档规则 |

第一层任何人都能写，没有价值。**第二层是有人花时间逆向出来的结论**，它不在源码里。

你翻 `app/config.py` 会看到：

```python
GW = ""
TOUCHPOINT = ""
PACKAGE_ID_BY_TIER = {}
FREE_RESOLUTIONS = ()
```

全是空的。真值只有在**你的激活码验证通过之后**，才会从服务器加密下发、
并在内存里填进这些变量。没有激活码，这份源码和一个普通的 FastAPI 脚手架没有区别。

---

## 二、发布前必须知道的四个约束

### 1. 端口必须读 `PORT`

平台会注入 `PORT` 环境变量，然后把公网请求转给它。**不要写死端口**。

本项目的 `workbuddy/entry.py` 已经处理好了（优先级：命令行 `--port` > `PORT` >
`PICSART_POOL_PORT` > 3000）。所以你只需要用对启动命令，见第四节。

### 2. 只暴露一个端口，没有数据库服务

平台只给一个 HTTP 端口，没有 MySQL / Redis / PostgreSQL。
本项目用 SQLite（随包一起跑），**不需要任何外部数据库**。

### 3. ⚠️ 数据卷一定要持久化

**这是最容易被忽略、后果最严重的一条。**

平台**重新部署会重置项目目录**。如果你把数据放在项目里的 `./data`，
那么每次更新代码 = 账号没了 + 激活失效。

`data/` 里存着：
- `pool.db` —— 你添加的 Picsart 账号（token）
- `license.dat` —— 激活凭证（绑这台部署）
- `lic.ok` / `deploy.id` —— 离线宽限和部署标识

**做法**：把 `DATA_DIR` 指向平台的持久卷路径（例如 `/data`），或在发布配置里
把持久卷挂到项目外的目录。具体路径按你的平台版本确认。

> 如果确实丢了：账号要重新授权、激活要联系发卡方重新绑定。不难，但很烦。

### 4. 沙箱不提供自定义环境变量注入

所以管理密码**不能**通过环境变量传。首次启动后：

1. 打开首页 → 如果提示未激活，先去 `/activate` 输入激活码
2. 激活成功后进管理页登录
3. 管理密码默认**为空**（任何人拿到链接都能进）——**务必在设置里改掉**

---

## 三、发布步骤

### 第 1 步：确认依赖

目录里需要有 `requirements.txt`（已有）。平台会执行 `pip install -r requirements.txt`。

如果平台从境外拉包不稳定，可以先本地 `pip download` 再把 wheels 一起上传，
或把 `installCmd` 置空并预装依赖。

### 第 2 步：启动命令

```
python workbuddy/entry.py
```

**不要**用 `uvicorn app.main:app`。原因见下：

> `app/config.py` 里的站点常量默认是空的，真值由 `entry.py` 在
> `app.main` 被 import **之前**注入。直接 `uvicorn` 起会跳过注入，
> 所有端点为空白 —— 表现是每个请求都拼出 `/workflows//submit` 然后 400，
> 而日志里看不出任何异常。

### 第 3 步：发布参数

| 参数 | 值 |
|---|---|
| `--dir` | 项目根目录（含 `workbuddy/` 的那个） |
| `--port` | 与 `PORT` 一致的端口（默认 3000） |
| `--language` | `python` |
| `startCmd` | `python workbuddy/entry.py` |

### 第 4 步：发布后自检（三步）

```bash
LINK="https://xxxx.app.workbuddy.link"

# 1. 服务起来了？
curl -s -o /dev/null -w "首页: %{http_code}\n" --max-time 20 "$LINK"
# 期望 200（未激活也是 200 —— 首页要能打开才能看到激活入口）

# 2. 域名推导对不对？
curl -s --max-time 20 "$LINK/__domain"
# 期望 public_url 是 https://xxxx.app.workbuddy.link，不是 localhost

# 3. 配置真的进了上游模块？
curl -s --max-time 20 "$LINK/__diag"
# 未激活：site_config_loaded=false   ← 正常
# 已激活：site_config_loaded=true 且所有字段非空
```

**`/__diag` 是最关键的一步。** 它检查的不是"配置有没有下载"，而是
"配置有没有真的进到发请求的那个模块里"。这两件事会不一致，且不一致时
表面上一切正常（状态页显示已激活、配置 ready），只有真正发请求时才炸。

---

## 四、激活流程

1. 打开 `https://你的域名/activate`
2. 复制页面上的**部署标识**（32 位十六进制）
3. 发给发卡方换取激活码
4. 填入激活码 → 点激活
5. 成功后自动进入管理页

激活后：

- **72 小时离线宽限** —— 断网也能继续用，超过后需联网一次
- **绑这台部署** —— 不是绑机器硬件，所以重新部署（数据卷保留）不用重新激活
- **换部署** —— 把新标识发给发卡方解绑，或直接换一张卡

---

## 五、常见问题

### Q: 打开是白屏 / 502

| 检查 | 怎么查 |
|---|---|
| 端口没读对 | 日志里应有 `[workbuddy] listening on 0.0.0.0:PORT` |
| 绑定的不是 `0.0.0.0` | 同上，看那一行的地址 |
| 启动命令用了 `uvicorn app.main:app` | 改成 `python workbuddy/entry.py` |
| 依赖没装上 | 看平台构建日志里 `pip install` 那段 |

### Q: 激活成功，但生成视频报 400

先打 `/__diag`：

```
site_config_loaded: false  → 配置没进上游模块（见下面 Q: 显示已激活但什么都用不了）
site_config_loaded: true   → 配置正常，问题在上游（账号/token/额度）
```

### Q: 显示已激活，但什么都用不了

这是「配置没传播到上游模块」的典型症状。原因通常是：

- 用了 `uvicorn app.main:app` 启动（跳过注入）
- 或者激活后没有重启

处理：改成 `python workbuddy/entry.py` 重启；或点管理页的「重新拉取配置」
（调 `/api/license/refresh`）。

### Q: 重新部署后要重新激活

说明数据卷没配好，`deploy.id` 丢了。见第二节第 3 条。

### Q: 想改管理密码但没环境变量

登录管理页 → 设置 → 改密码。密码保存在 `data/pool.db` 里。
**首次部署后第一件事就是改它**，因为默认是空的。

---

## 六、安全提醒（给客户的）

1. **首页默认无密码**。发布后立刻改。
2. **链接是"知道就能看"**。子域名随机、不被搜索引擎收录，但不是鉴权。
   要真正限制访问，用平台的功能或自己加一层反代。
3. **你的 Picsart 账号 token 存在 `data/pool.db`**。这个文件等价于账号密码，
   不要分享、不要提交到 git。
4. **不要把你的激活码发给别人**。它绑你的部署标识，但也意味着一张卡。

---

## 七、文件清单

发布包里**必须**有的：

```
app/                 程序（源码，可公开）
workbuddy/           沙箱适配层（端口、域名、激活门、配置注入）
requirements.txt     依赖
```

发布包里**绝对不能**有的：

```
_site/               自托管用的明文站点配置 —— 含全部关键信息
data/                你的账号 token + 激活凭证
certs/ 或 *.pem      证书/私钥
*.db                 SQLite 数据库
server/              授权服务器（那是发卡方的东西）
client/              Go 桌面客户端（另一条产品线）
tools/gen_site_config.py   会生成 _site/，别把 _site/ 传上去
```

### 发布前跑一遍这个

```bash
python tools/preflight.py .
```

它会扫内容而不只是文件名 —— 按路径、按后缀、按**实际凭证特征**
（JWT / refresh_token / 私钥 PEM / 站点知识）判断，退出码非 0 就别发。

在项目根目录跑会报 500 多项（正常，根目录本来就不该整体上传）。
正确做法是建一个只含 `app/` + `workbuddy/` + `requirements.txt` 的目录，
对它跑：

```bash
python tools/preflight.py ./发布目录
```

实测输出：

```
扫描 22 个文件
[注意] 1 项要你确认一下：
   workbuddy/gate.py ← 内容含【请求签名盐】：防重放用，不是授权边界（可保留）
结论：可以发布
```

> 关于那条「请求签名盐」：客户端必须拿它给请求签名，所以它必然在源码里
> （Go 版 `signSalt` 同理）。已实测它**不能**绕过授权 —— 假卡密 400、
> 未激活 403、伪造 hwid 403，真正的边界是卡密 + HWID + Ed25519 签名。
> 详见 `_smoke/test_salt_boundary.py`。

---

## 八、给自托管（不用平台）的说明

如果你有自己的服务器（Docker / 裸机），不需要平台的激活门：

```bash
python tools/gen_site_config.py --self-url https://你的域名
docker compose up -d
```

`--standalone` 模式下配置来自 `_site/site-config.json`（明文，在项目外），
适合你自己完全可控的环境。但**这个文件绝不能跟源码一起外发**。
