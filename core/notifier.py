# -*- coding: utf-8 -*-
"""ntfy 手机推送。"""
import json
import time
import requests


def ntfy_settings(cfg: dict) -> dict:
    n = (cfg.get("notify") or {}).get("ntfy") or {}
    return {"server": (n.get("server") or "https://ntfy.sh").rstrip("/"), "topic": (n.get("topic") or "").strip()}


def topic_configured(cfg: dict) -> bool:
    return bool(ntfy_settings(cfg)["topic"])


def push(cfg: dict, title: str, message: str, priority: int = 3, tags=None) -> bool:
    ns = ntfy_settings(cfg)
    if not ns["topic"]:
        return False
    headers = {"Title": title[:200], "Priority": str(priority), "Tags": ",".join(tags or [])}
    try:
        r = requests.post(f"{ns['server']}/{ns['topic']}", data=(message or "")[:4000].encode("utf-8"),
                          headers=headers, timeout=10)
        return r.status_code < 300
    except Exception:
        return False


def push_new_todo_notice(cfg: dict, created: int = 0, adopted: int = 0, done_local: int = 0,
                         project: str = "") -> bool:
    if not topic_configured(cfg):
        return False
    parts = []
    if created:
        parts.append(f"新增 {created} 条待办")
    if adopted:
        parts.append(f"纳入 {adopted} 条远端任务")
    if done_local:
        parts.append(f"完成 {done_local} 条")
    if not parts:
        return False
    title = f"📋 待办更新 {project or ''}".strip()
    return push(cfg, title, "；".join(parts), priority=3, tags=["tada"])


def test_push(cfg: dict) -> str:
    ns = ntfy_settings(cfg)
    if not ns["topic"]:
        raise ValueError("请先填写 ntfy 主题")
    ok = push(cfg, "HeliacMail 测试", "如果你看到这条，说明推送正常 ✅", priority=3, tags=["white_check_mark"])
    if not ok:
        raise ValueError("推送失败：请检查服务器地址与网络")
    return "测试推送已发送，请查看手机"
