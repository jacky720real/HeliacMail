# -*- coding: utf-8 -*-
"""
待办存储：todos.json（原子写盘）。

字段：id / text / source_email / source_subject / category / urgency /
created_at / done / ai(bool：来自 AI 提取还是关键词规则)。
"""
import uuid
from datetime import datetime
from typing import Dict, List, Optional

from core.storage import read_json, update_json, write_json

DEFAULT_PATH = "todos.json"
MAX_TEXT = 300


def load(path: str = DEFAULT_PATH) -> List[dict]:
    data = read_json(path, {})
    todos = data.get("todos", []) if isinstance(data, dict) else []
    return [t for t in todos if isinstance(t, dict)]


def _save(path: str, todos: List[dict]) -> None:
    write_json(path, {
        "version": "2.0",
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "todos": todos,
    })


def add(path: str, text: str, email: str, subject: str,
        category: str = "", urgency: str = "", ai: bool = False,
        backend: str = "", remote_id=None) -> bool:
    """新增待办。同一邮箱+同一文本视为重复，返回 False。

    整个“读-改-写”在 per-file 锁内完成，避免多账户并发轮询时互相覆盖丢失。
    """
    text = (text or "").strip()
    if not text:
        return False
    text = text[:MAX_TEXT]
    email_l = (email or "").lower()
    result = {"added": False}

    def _fn(data):
        if not isinstance(data, dict):
            data = {}
        todos = [t for t in data.get("todos", []) if isinstance(t, dict)]
        for t in todos:
            if (t.get("source_email") or "").lower() == email_l and t.get("text") == text:
                return data  # 重复，保持原样
        todos.insert(0, {
            "id": uuid.uuid4().hex[:12],
            "text": text,
            "source_email": email,
            "source_subject": subject,
            "category": category,
            "urgency": urgency,
            "ai": bool(ai),
            "backend": backend,
            "remote_id": remote_id,
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "done": False,
        })
        data["version"] = "2.0"
        data["updated_at"] = datetime.now().isoformat(timespec="seconds")
        data["todos"] = todos
        result["added"] = True
        return data

    update_json(path, _fn)
    return result["added"]


def toggle(path: str, tid: str) -> bool:
    result = {"ok": False}

    def _fn(data):
        if not isinstance(data, dict):
            data = {}
        todos = [t for t in data.get("todos", []) if isinstance(t, dict)]
        for t in todos:
            if t.get("id") == tid:
                t["done"] = not t.get("done", False)
                result["ok"] = True
                break
        data["todos"] = todos
        data["updated_at"] = datetime.now().isoformat(timespec="seconds")
        return data

    update_json(path, _fn)
    return result["ok"]


def remove(path: str, tid: str) -> bool:
    result = {"ok": False}

    def _fn(data):
        if not isinstance(data, dict):
            data = {}
        todos = [t for t in data.get("todos", []) if isinstance(t, dict)]
        kept = [t for t in todos if t.get("id") != tid]
        if len(kept) != len(todos):
            result["ok"] = True
        data["todos"] = kept
        data["updated_at"] = datetime.now().isoformat(timespec="seconds")
        return data

    update_json(path, _fn)
    return result["ok"]


def count_open(path: str = DEFAULT_PATH) -> int:
    return sum(1 for t in load(path) if not t.get("done", False))


def save(path: str, todos: List[dict]) -> None:
    """整表写回（供同步批处理使用，原子落盘）。"""
    _save(path, todos)


def set_done(path: str, tid: str, done: bool) -> bool:
    result = {"ok": False}

    def _fn(data):
        if not isinstance(data, dict):
            data = {}
        todos = [t for t in data.get("todos", []) if isinstance(t, dict)]
        for t in todos:
            if t.get("id") == tid:
                t["done"] = bool(done)
                result["ok"] = True
                break
        data["todos"] = todos
        data["updated_at"] = datetime.now().isoformat(timespec="seconds")
        return data

    update_json(path, _fn)
    return result["ok"]


def set_remote_id(path: str, tid: str, backend: str, remote_id) -> bool:
    result = {"ok": False}

    def _fn(data):
        if not isinstance(data, dict):
            data = {}
        todos = [t for t in data.get("todos", []) if isinstance(t, dict)]
        for t in todos:
            if t.get("id") == tid:
                t["backend"] = backend
                t["remote_id"] = remote_id
                result["ok"] = True
                break
        data["todos"] = todos
        data["updated_at"] = datetime.now().isoformat(timespec="seconds")
        return data

    update_json(path, _fn)
    return result["ok"]


def find_by_remote(path: str, backend: str, remote_id) -> Optional[dict]:
    for t in load(path):
        if t.get("backend") == backend and str(t.get("remote_id")) == str(remote_id):
            return t
    return None
