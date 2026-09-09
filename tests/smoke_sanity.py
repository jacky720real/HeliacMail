# -*- coding: utf-8 -*-
"""轻量冒烟：state/todos/config 主链路（不经 unittest，输出简短）。"""
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from core import state, todos
from core.config import coerce  # noqa: E402

d = tempfile.mkdtemp()
sp = os.path.join(d, "state.json")
state.set_watermark(sp, "a@qq.com", 1, 5)
assert state.get_watermark(sp, "a@qq.com") == (1, 5), "watermark"
state.mark_pushed(sp, "a@qq.com", 5)
assert state.was_pushed(sp, "a@qq.com", 5), "pushed"

tp = os.path.join(d, "todos.json")
assert todos.add(tp, "task-A", "a@qq.com", "subj"), "add"
assert not todos.add(tp, "task-A", "a@qq.com", "subj"), "dedupe"
assert todos.load(tp)[0]["text"] == "task-A", "load"
assert todos.toggle(tp, todos.load(tp)[0]["id"]), "toggle"

c = coerce(None)
assert c["server"]["host"] == "127.0.0.1", "default host"
with open(os.path.join(ROOT, "_smoke_ok.txt"), "w", encoding="utf-8") as f:
    f.write("SMOKE_ALL_OK")
print("SMOKE_ALL_OK")
