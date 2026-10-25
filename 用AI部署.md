# 让 WorkBuddy 帮你部署

把这份文件的内容整段复制，粘贴给你平台上的 AI 即可。

---

## 提示词（复制以下全部）

```
请帮我把这个项目发布成一个 Web 应用。

## 代码位置

我已经把代码下载/解压到：<在这里填你的实际路径>

（例如：D:\下载\picsart-pool-main  或  /home/user/picsart-pool）

这个目录里应该有这些文件和文件夹：
  app/            workbuddy/       requirements.txt
  app.py          README.md        部署说明.md

如果目录里没有这些，说明解压错了层级，请先确认路径。

## 发布参数（照抄，不要改）

language:    python
startCmd:    python workbuddy/entry.py
port:        3000
installCmd:  pip install -r requirements.txt

## 铁律：启动命令不要改成 uvicorn

错的：python -m uvicorn app.main:app
对的：python workbuddy/entry.py

原因：这个项目的配置是在 app.main 被 import **之前**由 entry.py 注入的。
用 uvicorn 直起会跳过注入，结果是**应用能打开但所有功能都坏** ——
每次调用都拼出不完整地址然后报 400，而日志里看不出任何异常。

（如果你只能用根目录脚本，可以用 `python app.py`，它等价于 entry.py。）

## 发布后请做这 4 项验证，把**原始输出**贴给我

1) 服务起来了
   curl -s -o /dev/null -w "%{http_code}\n" "https://<分享链接>/"
   期望：200

2) 公网地址推导正确
   curl -s "https://<分享链接>/__domain"
   期望：public_url 是 https:// 开头，**不是** localhost 或 127.0.0.1

3) 后端代码是新版（最关键）
   curl -s "https://<分享链接>/api/me"
   期望：{"logged_in":false,"need_login":false}
   如果只返回 {"logged_in":false}、没有 need_login 字段
   → 说明部署的是旧代码，请重新拉取并重发

4) 上游网络可达
   curl -s "https://<分享链接>/__probe"
   期望：JSON 里 verdict 是「上游可达，可以正常加号和出片」
   如果 verdict 说「上游完全不通」→ 这台机器访问不了 picsart.com，
   需要换一个网络环境（境外服务器/国际版沙箱）重新部署

## 现在的状态是正常的，别当故障

应用还没激活，所以：
  · 访问 /v1/* 返回 403 {"code":"not_activated"}  ← 设计如此
  · 管理接口返回 403                              ← 设计如此
  · 但 /、/activate、/healthz、/__domain、/__diag、/__probe
    这几个必须都是 200

## 完成后请给我

1. 分享链接（完整，含 ?sharecode= 的话不要截断）
2. 上面 4 项验证的原始输出
3. /api/license/status 里 hwid 的值（一串 32 位十六进制）
```

---

## 拿到链接之后

1. 把 `hwid` 发给发卡方，换激活码
2. 打开 `https://你的域名/activate`，填入激活码
3. 激活成功后**直接进管理页**，不需要登录密码
4. 进「账号」页 → 添加你自己的 Picsart 账号
5. 进「接入」页 → 拿到 Base URL 和接口文档，开始调用

---

## 如果卡住了

| 现象 | 先查什么 |
|---|---|
| 页面 502 / 白屏 | 启动命令是不是 `python workbuddy/entry.py` |
| `/api/me` 没有 `need_login` | 代码是旧版，重新下载 |
| `/__probe` 说"上游完全不通" | 换网络环境（境外服务器/国际版） |
| 点「添加账号」黑屏 | 偶发超时，等 1~2 分钟重试；持续失败看 `/__probe` |
| 激活后又要激活 | 重新部署导致 data/ 丢了，联系发卡方换绑 |

技术支持：**Zzyuc88**（微信）
