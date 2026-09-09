# -*- coding: utf-8 -*-
"""最近邮件记录（原子化落盘，保留最近 200 条）。"""
import json
import os
import tempfile
import time

MAX_ITEMS = 200


def load(path: str) -> list:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def save(items: list, path: str) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix="recent_", suffix=".tmp", dir=os.path.dirname(path) or ".")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(items[-MAX_ITEMS:], f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def append(item: dict, path: str) -> list:
    items = load(path)
    items.append(item)
    save(items, path)
    return items
