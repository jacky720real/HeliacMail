# -*- coding: utf-8 -*-
"""
通用 JSON 存储工具：原子写盘（临时文件 + rename），避免崩溃损坏数据。
"""
import json
import os
import threading
from typing import Any, Dict, Optional

_locks: Dict[str, threading.Lock] = {}
_guard = threading.Lock()


def _lock_for(path: str) -> "threading.RLock":
    """每个文件一把锁。

    使用 RLock 而非常规 Lock：update_json() 持锁后内部还会调用同样
    加锁的 write_json()，普通 Lock 在同一线程会二次获取而永久死锁。
    """
    with _guard:
        if path not in _locks:
            _locks[path] = threading.RLock()
        return _locks[path]


def read_json(path: str, default=None):
    if default is None:
        default = {}
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def write_json(path: str, obj: Any) -> None:
    """原子写入 JSON。"""
    lock = _lock_for(path)
    with lock:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)


def update_json(path: str, fn) -> Any:
    """读-改-写（带锁）。fn(data) 返回新数据；返回新数据。"""
    lock = _lock_for(path)
    with lock:
        data = read_json(path)
        data = fn(data)
        write_json(path, data)
        return data
