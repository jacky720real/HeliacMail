# -*- coding: utf-8 -*-
"""
配置模块：默认值 + 旧版配置自动迁移 + ${ENV} 展开。

配置存为 config.yaml（YAML）。结构见 config.example.yaml。
旧版扁平结构（accounts 在顶层、imap_host/web_port/poll_interval/ntfy_topic
在顶层）会被自动迁移到新结构，不会丢数据。
"""
import copy
import logging
import os
import re
import threading
from typing import Any, Dict, List, Optional

import yaml

_ENV_RE = re.compile(r"\$\{([^}]+)\}")

_write_lock = threading.Lock()

_logger = logging.getLogger("mail_monitor.config")

# ---------- 基础工具 ----------

def expand_env_text(s: str) -> str:
    """把字符串里的 ${VAR} 展开为环境变量值。"""
    if not isinstance(s, str) or "${" not in s:
        return s
    return _ENV_RE.sub(lambda m: os.environ.get(m.group(1), m.group(0)), s)


def expand_env(obj):
    """递归展开 dict/list/str 里的 ${ENV}。"""
    if isinstance(obj, dict):
        return {k: expand_env(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [expand_env(v) for v in obj]
    if isinstance(obj, str):
        return expand_env_text(obj)
    return obj


def deep_merge(base: Dict, override: Dict) -> Dict:
    """override 递归覆盖 base。"""
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def _defaults() -> Dict[str, Any]:
    return {
        "server": {"host": "127.0.0.1", "port": 8090, "auth_token": ""},
        "monitor": {
            "poll_interval": 30,
            "max_per_run": 20,
            "mark_seen": False,
            "use_idle": True,
            "idle_timeout": 300,
            "lookback_days": 30,   # 新账户首轮只读最近 N 天内的邮件；0=不限
            "accounts": [],
        },
        "todo": {"keywords": ["TODO", "待办", "task", "action item"]},
        "ai": {
            "enabled": False,
            "timeout": 120,
            "language": "中文",
            "preferences": "",    # 用户对 AI 的“关注重点”说明（会注入提示词）
            "weights": {},        # 各邮件类别关注权重 1-5，如 {"工作":5,"验证码":5}
            "providers": [],
        },
        "notify": {
            "ntfy": {"server": "https://ntfy.sh", "topic": ""},
            "priority_urgent": 5,
            "skip_categories": ["垃圾", "营销推广"],
            "sync_todos": True,
            "first_batch_quiet": True,   # 首轮/存量邮件只进简报与待办，不逐封推送（避免轰炸）
        },
        "sync": {
            "enabled": False,
            "backend": "vikunja",   # vikunja | todoist(规划) | ms_todo(规划) | off
            "vikunja": {
                "url": "",          # 例如 http://192.168.1.10:3456
                "token": "",        # Settings>API Tokens 生成的 tk_ 令牌
                "project_id": "",   # 留空=按 project_title 自动查找/创建
                "project_title": "邮箱待办",
                "priority_high": 5,
                "priority_med": 3,
                "priority_low": 1,
                "adopt_remote": True,   # 把手机上手动新增的任务也同步回来
                "notify_only_new": True,  # 只在有新待办生成时发 ntfy 提醒
            },
        },
        "logging": {
            "level": "INFO",
            "format": "%(asctime)s - %(name)s - %(levelname)s - %(message)s",
            "backup_count": 5,
        },
    }


def detect_imap_host(email: str) -> str:
    e = (email or "").lower()
    if "@qq.com" in e or "@foxmail.com" in e:
        return "imap.qq.com"
    if "@163.com" in e:
        return "imap.163.com"
    if "@126.com" in e:
        return "imap.126.com"
    if "@outlook.com" in e or "@hotmail.com" in e or "@live.com" in e:
        return "outlook.office365.com"
    if "@sina.com" in e:
        return "imap.sina.com"
    return "imap.gmail.com"


def is_valid_account(acc: Optional[Dict]) -> bool:
    if not acc:
        return False
    email = (acc.get("email") or "").strip()
    pwd = (acc.get("password") or "").strip()
    if not email or not pwd:
        return False
    if "@example.com" in email.lower():
        return False
    low = pwd.lower()
    if "your_password" in low or "your_authorization_code" in low:
        return False
    return True


def normalize_accounts(cfg: Dict) -> List[Dict]:
    """返回有效账户列表，自动补齐 imap_host/imap_port/enabled/mark_seen。"""
    out: List[Dict] = []
    default_mark_seen = bool((cfg.get("monitor") or {}).get("mark_seen"))
    for acc in (cfg.get("monitor") or {}).get("accounts", []) or []:
        if not is_valid_account(acc):
            continue
        item = dict(acc)
        email = (item.get("email") or "").strip()
        item["email"] = email
        item["password"] = str(item.get("password") or "").strip()
        item.setdefault("imap_host", detect_imap_host(email))
        item.setdefault("imap_port", 993)
        item["imap_port"] = int(item.get("imap_port") or 993)
        item.setdefault("enabled", True)
        item.setdefault("mark_seen", bool(item.get("mark_seen", default_mark_seen)))
        out.append(item)
    return out

# ---------- 旧版配置迁移 ----------

def _migrate_legacy(raw: Dict) -> Dict:
    """把旧版扁平结构迁移到新版嵌套结构（并保留原字段以免信息丢失）。"""
    cfg = copy.deepcopy(raw)
    had_legacy = False

    # 顶层旧字段 → server / monitor
    if "server" not in cfg:
        cfg["server"] = {}
    srv = cfg["server"]
    if "web_port" in cfg and srv.get("port") in (None, ""):
        srv["port"] = cfg.get("web_port")
        had_legacy = True
    if isinstance(cfg.get("web"), dict):
        if srv.get("port") in (None, "") and cfg["web"].get("port"):
            srv["port"] = cfg["web"]["port"]
        if cfg["web"].get("host"):
            srv["host"] = cfg["web"]["host"]

    if "monitor" not in cfg:
        cfg["monitor"] = {}
    mon = cfg["monitor"]
    if mon.get("poll_interval") in (None, "") and cfg.get("poll_interval"):
        mon["poll_interval"] = cfg.get("poll_interval")
        had_legacy = True
    if not mon.get("accounts") and isinstance(cfg.get("accounts"), list):
        mon["accounts"] = cfg["accounts"]
        had_legacy = True
    # 旧账户在顶层持有 imap_host/imap_port 默认值，逐个补齐
    top_host = cfg.get("imap_host")
    top_port = cfg.get("imap_port")
    if top_host or top_port:
        had_legacy = True
    for acc in mon.get("accounts", []) or []:
        if isinstance(acc, dict):
            acc.setdefault("imap_host", top_host or detect_imap_host(acc.get("email", "")))
            acc.setdefault("imap_port", top_port or 993)

    if "todo" not in cfg and cfg.get("todo_keywords"):
        cfg["todo"] = {"keywords": cfg.get("todo_keywords")}
        had_legacy = True
    if "notify" not in cfg and cfg.get("ntfy_topic"):
        cfg["notify"] = {"ntfy": {"server": "https://ntfy.sh", "topic": cfg.get("ntfy_topic")}}
        had_legacy = True

    if had_legacy:
        cfg["_migrated_from_legacy"] = True

    # 迁移完成后清理旧版“顶层残留键”，避免反复写回 config.yaml 越堆越乱
    for _legacy_top in ("accounts", "imap_host", "imap_port", "ntfy_topic",
                        "poll_interval", "todo_keywords", "web", "web_port"):
        cfg.pop(_legacy_top, None)
    return cfg


def coerce(raw: Optional[Dict]) -> Dict:
    """迁移旧结构 + 填充默认值 + 返回结构规整的配置。"""
    raw = _migrate_legacy(dict(raw) if raw else {})
    cfg = deep_merge(_defaults(), raw)
    cfg["monitor"]["accounts"] = normalize_accounts(cfg)
    return cfg


# ---------- AI provider 辅助 ----------

PROVIDER_DEFAULTS = {
    "openai": "https://api.openai.com/v1",
    "gemini": "https://generativelanguage.googleapis.com/v1beta/openai",
    "ollama": "http://127.0.0.1:11434/v1",
    "deepseek": "https://api.deepseek.com/v1",
}


def provider_base_url(p: Dict) -> str:
    url = (p.get("base_url") or "").strip()
    if url:
        return url.rstrip("/")
    return PROVIDER_DEFAULTS.get((p.get("type") or "").lower(), PROVIDER_DEFAULTS["openai"])


# ---------- 读写 ----------

def load_config(path: str) -> Dict:
    """读取并规整 config.yaml。

    - 文件不存在：返回默认配置（coerce(None)），不抛异常；
    - 文件存在但解析失败：打警告日志并返回默认配置，避免 /api/* 全 500。
    """
    if not os.path.exists(path):
        return coerce(None)
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f)
    except Exception as e:
        _logger.warning("config.yaml 解析失败，回退默认配置：%s", e)
        return coerce(None)
    return coerce(raw)


def save_config(cfg: Dict, path: str) -> None:
    """原子写入 config.yaml（临时文件 + rename）。"""
    clean = {k: v for k, v in cfg.items() if k != "_migrated_from_legacy"}
    tmp = path + ".tmp"
    with _write_lock:
        with open(tmp, "w", encoding="utf-8") as f:
            yaml.safe_dump(clean, f, allow_unicode=True, sort_keys=False,
                           default_flow_style=False, indent=2)
        os.replace(tmp, path)


def update_section(cfg: Dict, section: str, data: Dict) -> Dict:
    """整段更新某个 section（ai / notify / todo / monitor / server）。"""
    if section == "monitor":
        merged = deep_merge(cfg.get("monitor", {}), data)
        merged["accounts"] = cfg.get("monitor", {}).get("accounts", [])
        cfg["monitor"] = merged
    else:
        cfg[section] = deep_merge(cfg.get(section, {}), data)
    return coerce(cfg)


def ensure_config_file(path: str) -> Dict:
    """当 config.yaml 不存在时，生成默认配置并落盘。"""
    if os.path.exists(path):
        return load_config(path)
    cfg = coerce(None)
    save_config(cfg, path)
    return cfg

