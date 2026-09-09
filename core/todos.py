# -*- coding: utf-8 -*-
"""待办清单存取（原子化落盘）。"""
import json
import os
import tempfile
import time


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def load(path: str) -> list:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def save(todos: list, path: str) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix="todos_", suffix=".tmp", dir=os.path.dirname(path) or ".")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(todos, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def add(todos: list, title: str, category: str = "", mail_from: str = "", mail_subject: str = "",
        ts: str = "") -> dict:
    t = {
        "id": _new_id(todos),
        "title": title,
        "category": category,
        "mail_from": mail_from,
        "mail_subject": mail_subject,
        "ts": ts or _now(),
        "done": False,
        "done_ts": "",
    }
    todos.append(t)
    return t


def _new_id(todos: list) -> str:
    used = {t.get("id") for t in todos}
    n = 1
    while True:
        if str(n) not in used:
            return str(n)
        n += 1


def toggle(todos: list, tid: str) -> bool:
    for t in todos:
        if t.get("id") == tid:
            t["done"] = not t.get("done")
            t["done_ts"] = _now() if t["done"] else ""
            return True
    return False


def remove(todos: list, tid: str) -> bool:
    for i, t in enumerate(todos):
        if t.get("id") == tid:
            todos.pop(i)
            return True
    return False
