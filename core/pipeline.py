#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""邮件监控主流程：多账户并发轮询、UID 水位线去重、AI/关键词分析、
待办提取与推送、ntfy 手机通知、Vikunja 待办同步联动。"""
import base64
import email
import email.header
import email.utils
import hashlib
import json
import os
import re
import threading
import time
from datetime import datetime, timedelta

import requests

from core import ai as ai_mod
from core import imap_client as imap_mod
from core import mailparse as mp
from core import notifier as notify_mod
from core import recent as recent_mod
from core import state as state_mod
from core import storage as storage_mod
from core import tasksync as tasksync_mod
from core import todos as todos_mod


# ---------- 工具 ----------

def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _ts() -> float:
    return time.time()


def _log(log, msg):
    if log:
        try:
            log(msg)
        except Exception:
            pass


def _cfg_bool(cfg, *path, default=False):
    cur = cfg
    for k in path:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(k)
        if cur is None:
            return default
    return bool(cur)


# ---------- 邮件去重（UID 水位线） ----------

def _uid_key(uid_validity: int, uid: int) -> str:
    return f"{uid_validity}:{uid}"


def _seen_uids(state: dict) -> set:
    return set(state.get("seen_uids") or [])


def _record_seen(state: dict, uid_validity: int, uid: int) -> None:
    s = state.setdefault("seen_uids", [])
    s.append(_uid_key(uid_validity, uid))
    # 只保留最近 5000 条，避免 state.json 无限膨胀
    if len(s) > 5000:
        del s[: len(s) - 5000]


def _lookback_days(cfg) -> int:
    """0=不限；空/非法用 30。"""
    v = (cfg.get("monitor") or {}).get("lookback_days")
    if v is None or str(v).strip() == "":
        return 30
    try:
        return max(int(v), 0)
    except (TypeError, ValueError):
        return 30


# ---------- 主监控器 ----------

class Monitor:
    def __init__(self, config_file, state_file, todos_file, recent_file, log=None):
        self.config_file = config_file
        self.state_file = state_file
        self.todos_file = todos_file
        self.recent_file = recent_file
        self.log = log
        self._lock = threading.Lock()
        self._event = threading.Event()
        self._thread = None
        self._stop = False
        self._status = {
            "running": False,
            "checking": False,
            "last_cycle": "",
            "accounts": {},
            "last_error": "",
        }

    # ---------- 对外状态 ----------

    def status(self) -> dict:
        with self._lock:
            return json.loads(json.dumps(self._status))

    def start(self):
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop = False
            self._thread = threading.Thread(target=self._loop, daemon=True,
                                            name="MailMonitor")
            self._thread.start()

    def stop(self):
        self._stop = True
        self._event.set()

    def trigger(self):
        """网页点「立即检查」时唤醒一次。"""
        self._event.set()

    # ---------- 主循环 ----------

    def _loop(self):
        from core.config import load_config
        with self._lock:
            self._status["running"] = True
        _log(self.log, "监控线程已启动")
        first = True
        while not self._stop:
            try:
                cfg = load_config(self.config_file)
                interval = int((cfg.get("monitor") or {}).get("poll_interval", 30))
                interval = max(interval, 10)
                self._run_once(cfg, first_run=first)
            except Exception as e:
                _log(self.log, f"监控循环异常: {e}")
                with self._lock:
                    self._status["last_error"] = str(e)[:300]
            first = False
            self._event.wait(timeout=interval)
            self._event.clear()
        with self._lock:
            self._status["running"] = False
        _log(self.log, "监控线程已停止")

    def _run_once(self, cfg, first_run=False):
        accounts = []
        for a in (cfg.get("monitor") or {}).get("accounts") or []:
            if (a.get("email") or "").strip():
                accounts.append(a)
        if not accounts:
            return
        with self._lock:
            self._status["checking"] = True
        _log(self.log, f"开始检查 {len(accounts)} 个账户")
        state = state_mod.load(self.state_file)
        results = {}
        threads = []
        for acc in accounts:
            t = threading.Thread(target=self._check_one, args=(cfg, acc, state, results),
                                 daemon=True, name=f"Check-{acc['email'][:12]}")
            threads.append(t)
            t.start()
        for t in threads:
            t.join(timeout=180)
        state_mod.save(state, self.state_file)
        with self._lock:
            self._status["last_cycle"] = _now()
            self._status["checking"] = False
            self._status["accounts"] = results
        _log(self.log, f"本轮检查完成（{len(accounts)} 账户）")

    def _check_one(self, cfg, acc, state, results):
        email_addr = acc.get("email") or ""
        entry = {"email": email_addr, "ok": True, "new": 0, "error": ""}
        try:
            client = imap_mod.login(acc)
            if not client:
                entry.update({"ok": False, "error": "登录失败（检查授权码/密码）"})
                results[email_addr] = entry
                return
            try:
                uid_validity, seen = imap_mod.fetch_uids(client, lookback_days=_lookback_days(cfg))
                old_seen = _seen_uids(state)
                new_items = []
                for uid in seen:
                    key = _uid_key(uid_validity, uid)
                    if key in old_seen:
                        continue
                    new_items.append(uid)
                _log(self.log, f"[{email_addr}] 新邮件 {len(new_items)} 封")
                processed = 0
                for uid in new_items:
                    if self._stop:
                        break
                    try:
                        raw = imap_mod.fetch_raw(client, uid)
                        parsed = mp.parse_email(raw)
                        self._handle_one(cfg, acc, parsed)
                        _record_seen(state, uid_validity, uid)
                        processed += 1
                    except Exception as e:
                        _log(self.log, f"[{email_addr}] 处理 UID {uid} 失败: {e}")
                entry["new"] = processed
            finally:
                try:
                    client.logout()
                except Exception:
                    pass
        except Exception as e:
            entry.update({"ok": False, "error": str(e)[:200]})
        results[email_addr] = entry

    def _handle_one(self, cfg, acc, parsed):
        """单封邮件：AI/关键词分析 → 待办入库 → 推送 → 近期记录。"""
        subject = parsed.get("subject") or "（无主题）"
        sender = parsed.get("from") or ""
        body = parsed.get("body") or ""
        _log(self.log, f"处理邮件: {subject[:40]}")

        ai_cfg = cfg.get("ai") or {}
        result = None
        if ai_cfg.get("enabled") and ai_cfg.get("providers"):
            try:
                result = ai_mod.analyze_email(ai_cfg, sender, subject, body, log=self.log)
            except Exception as e:
                _log(self.log, f"AI 分析失败，走关键词兜底: {e}")
        if result is None:
            result = self._keyword_fallback(cfg, subject, body)

        # 待办入库（AI 或关键词提取）
        new_todos = []
        for t in (result.get("todos") or []):
            tid = todos_mod.add(self.todos_file, t, source=subject[:60])
            if tid:
                new_todos.append(t)

        # 推送
        self._notify(cfg, parsed, result, new_todos)

        # 近期记录
        recent_mod.push(self.recent_file, {
            "subject": subject,
            "sender": sender,
            "category": result.get("category"),
            "priority": result.get("priority"),
            "summary": result.get("summary"),
            "time": _now(),
        })

        # 待办同步联动
        if tasksync_mod.enabled(cfg) and new_todos:
            try:
                counts = tasksync_mod.sync_todos(cfg, self.todos_file, log=self.log)
                if counts.get("ok") and counts["created"]:
                    notify_mod.push_new_todo_notice(cfg, created=counts["created"],
                                                    project=counts.get("project", ""))
            except Exception as e:
                _log(self.log, f"待办同步失败: {e}")

    def _keyword_fallback(self, cfg, subject, body):
        keywords = (cfg.get("todo") or {}).get("keywords") or []
        hits = ai_mod.keyword_todos(subject, body, keywords)
        category = "其他"
        for kw in keywords:
            if kw and kw.lower() in f"{subject} {body}".lower():
                category = "工作" if any(k in kw for k in ("工作", "项目", "会议")) else "其他"
                break
        return {
            "priority": 3, "category": category,
            "summary": subject[:30], "action": bool(hits),
            "todos": hits, "reason": "关键词模式",
        }

    def _notify(self, cfg, parsed, result, new_todos):
        """按规则决定是否推送 ntfy 通知。"""
        notify_cfg = cfg.get("notify") or {}
        if not notify_mod.topic_configured(cfg):
            return
        if not new_todos and not (result or {}).get("action"):
            return
        skip = set(notify_cfg.get("skip_categories") or [])
        cat = (result or {}).get("category") or "其他"
        if cat in skip:
            return
        notify_mod.push_mail(cfg, parsed, result)


def sync_todos_to_phone(cfg, todos_file, log=None):
    """把本地待办清单发送到手机（ntfy 文本消息）。"""
    todos = todos_mod.load(todos_file)
    open_todos = [t for t in todos if not t.get("done")]
    if not open_todos:
        _log(log, "无待办，跳过手机推送")
        return True
    lines = ["📋 待办清单："]
    for i, t in enumerate(open_todos, 1):
        lines.append(f"{i}. {t.get('title')}")
    ok, msg = notify_mod.push_text(cfg, "\n".join(lines), title="HeliacMail 待办")
    _log(log, f"手机推送: {msg}")
    return ok
