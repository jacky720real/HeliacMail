# -*- coding: utf-8 -*-
"""配置加载/保存/默认值/邮箱域名识别。"""
import os
import re
import time
import uuid
import yaml

CONFIG_VERSION = 2

# 常见邮箱域名 → IMAP 服务器
IMAP_HOSTS = {
    "qq.com": "imap.qq.com",
    "foxmail.com": "imap.qq.com",
    "163.com": "imap.163.com",
    "126.com": "imap.126.com",
    "yeah.net": "imap.yeah.net",
    "gmail.com": "imap.gmail.com",
    "googlemail.com": "imap.gmail.com",
    "outlook.com": "outlook.office365.com",
    "hotmail.com": "outlook.office365.com",
    "live.com": "outlook.office365.com",
    "office365.com": "outlook.office365.com",
    "icloud.com": "imap.mail.me.com",
    "139.com": "imap.139.com",
    "189.cn": "imap.189.cn",
    "sohu.com": "imap.sohu.com",
    "sina.com": "imap.sina.com",
    "sina.cn": "imap.sina.cn",
    "aliyun.com": "imap.aliyun.com",
    "tom.com": "imap.tom.com",
    "21cn.com": "imap.21cn.com",
    "coremail.cn": "imap.coremail.cn",
    "zoho.com": "imap.zoho.com",
    "protonmail.com": "127.0.0.1",  # 需要 Proton Bridge，不自动配置
    "yandex.com": "imap.yandex.com",
    "mail.ru": "imap.mail.ru",
    "aol.com": "imap.aol.com",
    "yahoo.com": "imap.mail.yahoo.com",
    "gmx.com": "imap.gmx.com",
}


def _defaults() -> dict:
    return {
        "version": CONFIG_VERSION,
        "server": {"host": "127.0.0.1", "port": 8090, "auth_token": ""},
        "monitor": {
            "poll_interval": 30,
            "max_per_run": 20,
            "mark_seen": False,
            "use_idle": True,
            "idle_timeout": 300,
            "lookback_days": 30,
            "accounts": [],
        },
        "ai": {
            "enabled": False,
            "timeout": 120,
            "language": "中文",
            "preferences": "",
            "weights": {},
            "providers": [],
        },
        "notify": {
            "ntfy": {"server": "https://ntfy.sh", "topic": ""},
            "priority_urgent": 5,
            "skip_categories": [],
            "sync_todos": True,
            "first_batch_quiet": True,
        },
        "sync": {
            "enabled": False,
            "backend": "vikunja",
            "vikunja": {
                "url": "",
                "token": "",
                "username": "",
                "password": "",
                "project_id": "",
                "project_title": "邮箱待办",
                "priority_high": 5,
                "priority_med": 3,
                "priority_low": 1,
                "adopt_remote": True,
            },
        },
        "todo": {"keywords": ["待办", "需要", "请", "务必", "记得"]},
    }


def load_config(path: str) -> dict:
    """读取配置；文件缺失/损坏时返回默认配置。"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        if not isinstance(data, dict):
            return _defaults()
        return _merge(_defaults(), data)
    except Exception:
        return _defaults()


def _merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            out[k] = _merge(base[k], v)
        else:
            out[k] = v
    return out


def save_config(cfg: dict, path: str) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(cfg, f, allow_unicode=True, sort_keys=False)


def ensure_config_file(path: str) -> dict:
    """确保配置文件存在；不存在则写入默认配置并返回。"""
    if os.path.exists(path):
        return load_config(path)
    cfg = _defaults()
    cfg["server"]["auth_token"] = uuid.uuid4().hex[:16] if False else ""
    save_config(cfg, path)
    return cfg


def update_section(cfg: dict, section: str, data: dict) -> dict:
    cur = dict(cfg.get(section) or {})
    cur.update(data)
    cfg[section] = cur
    return cfg


def detect_imap_host(email: str) -> str:
    """根据邮箱域名返回 IMAP 服务器。"""
    e = (email or "").strip().lower()
    if "@" not in e:
        return ""
    domain = e.split("@", 1)[1]
    return IMAP_HOSTS.get(domain, "")


def normalize_accounts(cfg: dict) -> list:
    """返回规范化的账户列表（过滤空邮箱、补齐默认字段）。"""
    mon = cfg.get("monitor") or {}
    out = []
    for a in mon.get("accounts") or []:
        if not isinstance(a, dict):
            continue
        email = (a.get("email") or "").strip().lower()
        if not email or "@" not in email:
            continue
        out.append({
            "email": email,
            "password": a.get("password") or "",
            "imap_host": (a.get("imap_host") or "").strip() or detect_imap_host(email),
            "imap_port": int(a.get("imap_port") or 993),
            "mark_seen": bool(a.get("mark_seen")),
        })
    return out
