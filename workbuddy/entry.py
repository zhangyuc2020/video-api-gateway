"""WorkBuddy 沙箱入口。**唯一**正确的启动方式。

为什么不能用 `uvicorn app.main:app` 直接起
------------------------------------------
`app/config.py` 里已经**没有任何站点字面量**（客户发布 = 源码在客户机器上，
留字面量等于白送）。真值由本文件在 `app.main` 被 import **之前**注入：

    import app.config        # 1. 只加载模块
    remote.apply(app.config) # 2. 写进它的命名空间
    import app.main          # 3. 此时 `from .config import GW` 拿到真值

顺序反了的表现是「所有端点为空」：请求会拼成 `/workflows//submit`，
然后拿到看不懂的 400。`uvicorn app.main:app` 就是这样。

这个入口做四件事
----------------
  1. 端口从 `PORT` 取（回落 PICSART_POOL_PORT → 3000）
  2. 绑定 `0.0.0.0`（沙箱要从外面回源）
  3. 开 proxy_headers —— 否则 request 看到的永远是 `http://127.0.0.1:PORT`，
     OAuth 回调地址会算错
  4. 按顺序注入站点配置 + 装授权门

两种数据来源，同一个 `apply()`
---------------------------
  客户模式（默认）  授权服务器 `/config` 解密 → apply
  自托管/开发      `_site/site-config.json`  → apply（用 `--standalone`）

没有站点配置时不会崩：授权门会把所有功能请求挡在 403，首页和 `/activate`
仍然能打开 —— 客户看得到机器码，这才是正确的首屏。
"""
from __future__ import annotations

import os
import sys

# 允许从任意 CWD 启动。路径必须是「本文件所在目录的父目录」——
# 沙箱里 CWD 不一定是项目根，用相对路径 import 会直接 ModuleNotFoundError。
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for p in (ROOT, HERE):
    if p not in sys.path:
        sys.path.insert(0, p)


def resolve_port(argv: list[str]) -> int:
    """端口优先级：命令行 > PORT（平台注入）> PICSART_POOL_PORT > 3000。

    平台注入的 PORT 必须是第一顺位 —— 写死或读错变量的后果是白屏/502，
    而这两种故障在日志里都不明显（进程活着，只是没人能连上）。
    """
    for i, a in enumerate(argv):
        if a == "--port" and i + 1 < len(argv):
            return int(argv[i + 1])
        if a.startswith("--port="):
            return int(a.split("=", 1)[1])
    for name in ("PORT", "PICSART_POOL_PORT"):
        v = (os.environ.get(name) or "").strip()
        if v.isdigit():
            return int(v)
    return 3000


def main() -> None:
    argv = sys.argv[1:]
    standalone = "--standalone" in argv
    port = resolve_port([a for a in argv if a != "--standalone"])
    host = os.environ.get("PICSART_POOL_HOST", "0.0.0.0")

    # 数据目录：平台重新部署会重置项目目录，放 ./data 里的账号和 license
    # 会跟着没。README 要求映射到平台的持久卷；这里只保证路径确定。
    os.environ.setdefault(
        "PICSART_POOL_DATA", os.environ.get("DATA_DIR") or os.path.join(ROOT, "data")
    )

    # ---- 第 1 步：只加载 config，不碰 app.main ----
    from app import config
    from workbuddy import remote

    g = None
    applied = False

    if standalone:
        # 自托管/开发：明文 sidecar。它必须在发布目录之外（.gitignore 已忽略）。
        cfg = remote.load_sidecar(ROOT)
        if cfg:
            remote.apply(config, cfg)
            applied = True
            print("[workbuddy] 站点配置来自 _site/site-config.json（自托管模式）", flush=True)
        else:
            print(
                "[workbuddy] 自托管模式但没找到 _site/site-config.json —— "
                "先跑 python tools/gen_site_config.py",
                flush=True,
            )
    else:
        # 客户模式：真值只能来自服务端解密。
        from app.config import settings
        from workbuddy.gate import init_gate

        g = init_gate(settings.data_dir)
        if g.verify():
            if g.refresh_config(raise_on_error=False) and g.config:
                remote.apply(config, g.config)
                applied = True
        else:
            print("[workbuddy] 未激活：只开放首页/激活页，号池功能全部 403", flush=True)

    # 默认值在注入之后才能确定（分辨率/时长/ratio 都来自规则）
    from app.config import settings as s2

    if applied:
        print(
            f"[workbuddy] 站点配置已注入：wf={config.WAN3_WORKFLOW!r} "
            f"free={list(config.FREE_RESOLUTIONS)} auth={bool(config.TOKEN)}",
            flush=True,
        )

    # ---- 第 2 步：现在才 import app.main ----
    # 此时 app/upstream.py 等模块执行 `from .config import GW`，
    # 拿到的是**注入后**的值。
    from app.main import app
    from workbuddy.guard import install_guard, router as license_router
    from workbuddy.puburl import install_public_url

    install_public_url(app)
    app.include_router(license_router)
    install_guard(app, lambda: g)

    import uvicorn

    print(f"[workbuddy] listening on {host}:{port}  data={s2.data_dir}", flush=True)
    uvicorn.run(
        app,
        host=host,
        port=port,
        proxy_headers=True,
        forwarded_allow_ips="*",
        log_level="info",
        # 长任务（视频实测 91s~23min）走 wait:true 时不能被 server 超时掐断。
        timeout_keep_alive=75,
    )


if __name__ == "__main__":
    main()
