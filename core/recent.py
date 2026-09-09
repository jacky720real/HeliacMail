# -*- coding: utf-8 -*-
"""
最近邮件简报：recent.json（原子写盘，最新在前，最多保留 60 条），
供仪表板展示“AI/关键词摘要历史”。
"""
from datetime import datetime
from typing import List, Optional

from core.storage import read_json, update_json

DEFAULT_PATH = "recent.json"
MAX_ITEMS = 60


def load(path: str = DEFAULT_PATH) -> List[dict]:
    data = read_json(path, [])
    items = data if isinstance(data, list) else data.get("items", [])
    return [x for x in items if isinstance(x, dict)]


def add(path: str, email: str, subject: str, category: str, urgency: str,
        summary: str, key_points: List[str], mode: str, url: str = "",
        message_id: str = "", uid: Optional[int] = None,
        mail_time: str = "") -> None:
    """新增一条最近简报（per-file 锁内完成，最新在前，最多保留 MAX_ITEMS 条）。

    mail_time：邮件本身的发送时间（本地 ISO 文本），供界面优先展示。
    """
    def _fn(data):
        if isinstance(data, list):
            items = data
        elif isinstance(data, dict):
            items = data.get("items", [])
        else:
            items = []
        items = [x for x in items if isinstance(x, dict)]
        # 同账户同 UID 只保留一条（崩溃重试时不重复堆积）
        if uid is not None:
            items = [x for x in items
                     if not (x.get("source_email") == email and x.get("uid") == uid)]
        items.insert(0, {
            "uid": uid,
            "source_email": email,
            "subject": subject,
            "category": category,
            "urgency": urgency,
            "summary": summary,
            "key_points": key_points or [],
            "mode": mode,          # ai | keyword
            "url": url,
            "message_id": message_id,
            "mail_time": mail_time or "",   # 邮件本身发送时间（可空）
            "time": datetime.now().isoformat(timespec="seconds"),  # 兜底/处理时间
        })
        return items[:MAX_ITEMS]
    update_json(path, _fn)
