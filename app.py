#!/usr/bin/env python3
"""启动入口的等价包装。

用途：某些平台不认 `python workbuddy/entry.py` 这种带子目录的命令，
只接受根目录下的一个脚本（比如 `python app.py` 或 `python main.py`）。

这个文件**只做转发**，真正的逻辑仍在 workbuddy/entry.py —— 那里有
配置注入顺序的关键约束（必须在 import app.main 之前注入）。

⚠️ 不要把这个文件的内容改成 `uvicorn app.main:app`。
"""
from __future__ import annotations

import os
import runpy
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ENTRY = os.path.join(HERE, "workbuddy", "entry.py")

if not os.path.exists(ENTRY):
    sys.stderr.write(
        "找不到 workbuddy/entry.py。\n"
        "请确认整个目录都上传了（不只是 app/）。\n"
    )
    raise SystemExit(1)

# 把根目录加进 sys.path，让 entry.py 里的相对 import 正常解析
if HERE not in sys.path:
    sys.path.insert(0, HERE)

runpy.run_path(ENTRY, run_name="__main__")
