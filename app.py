#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
HeliacMail · 多邮箱 AI 助手（一体化版 v2）
= 保留：浏览器配置/仪表盘、多邮箱、关键词待办提取（AI 不可用时兜底）
= 新增：AI 结构化分析（OpenAI 兼容多 provider 自动降级）、ntfy 手机推送、
        待办同步到手机、UID 水位线去重、多账户并发轮询、原子化落盘。

运行: py app.py （或双击 run.bat）
"""
import hmac
import json
import os
import signal
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ---------- 环境自检（未安装依赖时给出友好提示，而不是一堆 Traceback） ----------
def _require_deps():
    missing = []
    for _name in ("requests", "yaml", "psutil"):
        try:
            __import__(_name)
        except Exception:
            missing.append(_name)
    if missing:
        print("=" * 60)
        print("  缺少运行依赖：" + ", ".join(missing))
        print("  首次使用请先安装依赖（或直接双击 run.bat，会自动安装）：")
        print("      py -m pip install -r requirements.txt")
        print("=" * 60)
        raise SystemExit(1)


_require_deps()

from core import ai as ai_mod
from core import cloudflare_tunnel as cf_mod
from core import config as config_mod
from core import notifier as notify_mod
from core import pipeline as pipeline_mod
from core import recent as recent_mod
from core import tasksync as tasksync_mod
from core import todos as todos_mod
from core import vikunja_setup as vk_mod
from core.config import load_config, normalize_accounts, save_config
from utils.logger import setup_logger

if getattr(sys, "frozen", False):
    # PyInstaller 打包运行：配置/数据/日志放在 exe 同目录（持久化），页面模板从打包资源读取
    BASE_DIR = os.path.dirname(os.path.abspath(sys.executable))
    RES_DIR = getattr(sys, "_MEIPASS", BASE_DIR)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    RES_DIR = BASE_DIR

CONFIG_FILE = os.path.join(BASE_DIR, "config.yaml")
STATE_FILE = os.path.join(BASE_DIR, "state.json")
TODOS_FILE = os.path.join(BASE_DIR, "todos.json")
RECENT_FILE = os.path.join(BASE_DIR, "recent.json")

logger = setup_logger("mail_monitor", log_dir=os.path.join(BASE_DIR, "logs"))
monitor = pipeline_mod.Monitor(CONFIG_FILE, STATE_FILE, TODOS_FILE, RECENT_FILE, logger.info)
_cfg_lock = threading.Lock()


def log(msg: str) -> None:
    logger.info(msg)


def _read_page(name: str) -> str:
    with open(os.path.join(RES_DIR, name), "r", encoding="utf-8") as f:
        return f.read()


# ---------- 邮箱账户 ----------

def _email_format_error(email: str) -> str:
    e = email.strip().lower()
    if not e:
        return "请填写邮箱地址"
    if " " in e:
        return "邮箱地址中不能包含空格"
    if e.count("@") != 1:
        return f"邮箱地址格式不正确（应含一个 @）：{email}"
    local, _, domain = e.partition("@")
    if not local:
        return "邮箱地址缺少 @ 前面的用户名部分"
    if "." not in domain:
        return f"域名不完整，请检查是否漏点了，如 qq.com、163.com（你输入的是：{domain}）"
    if "qqcom" in domain or domain in ("qq", "qq.co", "qq.cm"):
        return "疑似 QQ 邮箱域名笔误：应为 @qq.com（qq 和 com 之间有小数点）"
    return ""


def _save_account(email: str, pwd: str, poll_interval: int = 0) -> None:
    """新增或更新邮箱账户；密码留空时保留原密码。"""
    with _cfg_lock:
        cfg = load_config(CONFIG_FILE)
        mon = cfg.get("monitor") or {}
        if poll_interval:
            mon["poll_interval"] = max(int(poll_interval), 10)
        accounts = mon.get("accounts") or []
        found = False
        for acc in accounts:
            if (acc.get("email") or "").lower() == email.lower():
                if pwd:
                    acc["password"] = pwd
                acc["imap_host"] = config_mod.detect_imap_host(email)
                acc["imap_port"] = 993
                acc["mark_seen"] = bool(mon.get("mark_seen"))
                found = True
                break
        if not found:
            accounts.append({
                "email": email, "password": pwd,
                "imap_host": config_mod.detect_imap_host(email),
                "imap_port": 993,
                "mark_seen": bool(mon.get("mark_seen")),
            })
        mon["accounts"] = accounts
        cfg["monitor"] = mon
        save_config(cfg, CONFIG_FILE)


# ---------- 设置（AI / 推送 / 关键词）序列化 ----------

def _public_provider(p: dict) -> dict:
    return {
        "name": (p.get("name") or "").strip(),
        "type": (p.get("type") or "openai").strip(),
        "base_url": (p.get("base_url") or "").strip(),
        "model": (p.get("model") or "").strip(),
        "api_key": "",
        "has_key": bool((p.get("api_key") or "").strip()),
        "vision": bool(p.get("vision")),
    }


def settings_public() -> dict:
    """返回给网页的设置（API Key 一律隐藏）。"""
    cfg = load_config(CONFIG_FILE)
    mon = cfg.get("monitor") or {}
    return {
        "monitor": {
            "poll_interval": mon.get("poll_interval", 30),
            "max_per_run": mon.get("max_per_run", 20),
            "mark_seen": bool(mon.get("mark_seen")),
            "use_idle": bool(mon.get("use_idle", True)),
            "idle_timeout": int(mon.get("idle_timeout") or 300),
            "lookback_days": int(mon.get("lookback_days", 30)),
        },
        "ai": {
            "enabled": bool((cfg.get("ai") or {}).get("enabled")),
            "timeout": (cfg.get("ai") or {}).get("timeout", 120),
            "language": (cfg.get("ai") or {}).get("language", "中文"),
            "preferences": str(((cfg.get("ai") or {}).get("preferences") or "")),
            "weights": dict(((cfg.get("ai") or {}).get("weights") or {})),
            "providers": [_public_provider(p) for p in ((cfg.get("ai") or {}).get("providers") or [])],
        },
        "notify": {
            "ntfy": {
                "server": ((cfg.get("notify") or {}).get("ntfy") or {}).get("server", "https://ntfy.sh"),
                "topic": ((cfg.get("notify") or {}).get("ntfy") or {}).get("topic", ""),
            },
            "priority_urgent": (cfg.get("notify") or {}).get("priority_urgent", 5),
            "skip_categories": (cfg.get("notify") or {}).get("skip_categories", []),
            "sync_todos": bool((cfg.get("notify") or {}).get("sync_todos", True)),
            "first_batch_quiet": bool((cfg.get("notify") or {}).get("first_batch_quiet", True)),
        },
        "sync": _public_sync(cfg),
        "todo": {"keywords": (cfg.get("todo") or {}).get("keywords", [])},
    }


def _public_sync(cfg: dict) -> dict:
    sy = cfg.get("sync") or {}
    vk = sy.get("vikunja") or {}
    return {
        "enabled": bool(sy.get("enabled")),
        "backend": (sy.get("backend") or "vikunja"),
        "vikunja": {
            "url": (vk.get("url") or "").strip(),
            "token": "",
            "has_token": bool((vk.get("token") or "").strip()),
            "username": (vk.get("username") or "").strip(),
            "password": "",
            "has_password": bool((vk.get("password") or "").strip()),
            "project_id": str(vk.get("project_id") or ""),
            "project_title": (vk.get("project_title") or "邮箱待办").strip(),
            "priority_high": int(vk.get("priority_high") or 5),
            "priority_med": int(vk.get("priority_med") or 3),
            "priority_low": int(vk.get("priority_low") or 1),
            "adopt_remote": bool(vk.get("adopt_remote", True)),
        },
    }


def _lookback(value) -> int:
    """lookback_days：0=不限；空/非法则用默认 30。"""
    if value is None or str(value).strip() == "":
        return 30
    try:
        return max(int(value), 0)
    except (TypeError, ValueError):
        return 30


def _weights(raw) -> dict:
    """只保留已知类别、归一化到 1-5 的权重表。"""
    allowed = set(ai_mod.CATEGORIES)
    out = {}
    for k, v in (raw or {}).items():
        if k not in allowed:
            continue
        try:
            n = int(v)
        except (TypeError, ValueError):
            continue
        out[k] = max(1, min(5, n))
    return out


def apply_settings(data: dict) -> str:
    """应用设置；返回错误文本（空=成功）。API Key 留空时保留旧值，clear_key=true 时清空。"""
    with _cfg_lock:
        cfg = load_config(CONFIG_FILE)
        old = settings_public()

        if "ai" in data:
            na = data["ai"]
            providers = []
            old_provs = ((cfg.get("ai") or {}).get("providers") or [])
            # 按「provider name」建立旧配置的 api_key 索引；name 缺失时回退按位置匹配，
            # 避免前端删除中间 provider 后按 DOM 顺序提交导致 key 错位。
            old_key_by_name = {}
            for op in old_provs:
                _n = (op.get("name") or "").strip()
                if _n:
                    old_key_by_name[_n] = (op.get("api_key") or "")
            for i, p in enumerate(na.get("providers") or []):
                pp = dict(p)
                pp["name"] = (pp.get("name") or "").strip()
                pp["type"] = (pp.get("type") or "openai").strip()
                pp["base_url"] = (pp.get("base_url") or "").strip()
                pp["model"] = (pp.get("model") or "").strip()
                if not pp["model"]:
                    return "AI provider 的 model 不能为空"
                old_key = old_key_by_name.get(pp["name"], "")
                if not old_key and i < len(old_provs):
                    old_key = (old_provs[i].get("api_key") or "")
                if pp.pop("clear_key", False):
                    pp["api_key"] = ""
                elif not (pp.get("api_key") or "").strip():
                    pp["api_key"] = old_key  # 留空=保留
                pp["vision"] = bool(pp.pop("vision", False))
                pp.pop("has_key", None)
                providers.append(pp)
            ai_upd = {
                "enabled": bool(na.get("enabled")),
                "timeout": max(int(na.get("timeout") or 120), 10),
                "language": (na.get("language") or "中文").strip() or "中文",
                "providers": providers,
            }
            # 偏好/权重：仅在本次请求显式带上时才更新（避免别的保存动作误清空）
            if "preferences" in na:
                ai_upd["preferences"] = str(na.get("preferences") or "").strip()[:2000]
            if "weights" in na:
                ai_upd["weights"] = _weights(na.get("weights"))
            cfg = config_mod.update_section(cfg, "ai", ai_upd)

        if "notify" in data:
            nn = data["notify"]
            ntfy = nn.get("ntfy") or {}
            cfg = config_mod.update_section(cfg, "notify", {
                "ntfy": {
                    "server": (ntfy.get("server") or "https://ntfy.sh").strip() or "https://ntfy.sh",
                    "topic": (ntfy.get("topic") or "").strip(),
                },
                "priority_urgent": int(nn.get("priority_urgent", 5) or 5),
                "skip_categories": [str(x) for x in (nn.get("skip_categories") or [])],
                "sync_todos": bool(nn.get("sync_todos", True)),
                "first_batch_quiet": bool(nn.get("first_batch_quiet", True)),
            })

        if "sync" in data:
            ns = data["sync"]
            old_vk = ((cfg.get("sync") or {}).get("vikunja") or {})
            nv = ns.get("vikunja") or {}
            new_token = (nv.get("token") or "").strip()
            if not new_token and not nv.get("clear_token"):
                new_token = old_vk.get("token", "")
            new_user = (nv.get("username") or "").strip() or old_vk.get("username", "")
            new_pass = (nv.get("password") or "").strip()
            if nv.get("clear_password"):
                new_pass = ""
            elif not new_pass:
                new_pass = old_vk.get("password", "")
            cfg = config_mod.update_section(cfg, "sync", {
                "enabled": bool(ns.get("enabled")),
                "backend": (ns.get("backend") or "vikunja"),
                "vikunja": {
                    "url": (nv.get("url") or "").strip(),
                    "token": "" if nv.get("clear_token") else new_token,
                    "username": new_user,
                    "password": new_pass,
                    "project_id": str(nv.get("project_id") or "").strip(),
                    "project_title": (nv.get("project_title") or "邮箱待办").strip(),
                    "priority_high": int(nv.get("priority_high") or 5),
                    "priority_med": int(nv.get("priority_med") or 3),
                    "priority_low": int(nv.get("priority_low") or 1),
                    "adopt_remote": bool(nv.get("adopt_remote", True)),
                },
            })

        if "todo" in data:
            cfg = config_mod.update_section(cfg, "todo", {
                "keywords": [str(x).strip() for x in (data["todo"].get("keywords") or []) if str(x).strip()],
            })

        if "monitor" in data:
            nm = data["monitor"]
            cfg = config_mod.update_section(cfg, "monitor", {
                "poll_interval": max(int(nm.get("poll_interval") or 30), 10),
                "max_per_run": max(int(nm.get("max_per_run") or 20), 1),
                "mark_seen": bool(nm.get("mark_seen")),
                "use_idle": bool(nm.get("use_idle", True)),
                "idle_timeout": max(int(nm.get("idle_timeout") or 300), 30),
                "lookback_days": _lookback(nm.get("lookback_days")),
            })
        save_config(cfg, CONFIG_FILE)
    return ""


# ---------- HTTP Handler ----------

def _status_json() -> dict:
    cfg = load_config(CONFIG_FILE)
    accounts = normalize_accounts(cfg)
    mon = monitor.status()
    errors = []
    for r in (mon.get("accounts") or {}).values():
        if isinstance(r, dict) and not r.get("ok", True) and r.get("error"):
            errors.append(f"{r.get('email')}: {r.get('error')}")
    ntfy = notify_mod.ntfy_settings(cfg)
    server = cfg.get("server") or {}
    host = (server.get("host") or "127.0.0.1").strip().lower()
    auth_token = str(server.get("auth_token") or "").strip()
    auth_warning = bool(not auth_token and host not in ("127.0.0.1", "localhost", "::1"))
    return {
        "success": True,
        "account_count": len(accounts),
        "running": mon.get("running", False),
        "checking": mon.get("checking", False),
        "last_cycle": mon.get("last_cycle", ""),
        "poll_interval": int((cfg.get("monitor") or {}).get("poll_interval", 30)),
        "ntfy_topic": (ntfy.get("topic") or "").strip(),
        "ai_enabled": bool((cfg.get("ai") or {}).get("enabled")),
        "ai_providers": len((cfg.get("ai") or {}).get("providers") or []),
        "sync_enabled": bool((cfg.get("sync") or {}).get("enabled")),
        "sync_backend": (cfg.get("sync") or {}).get("backend", ""),
        "auth_warning": auth_warning,
        "errors": errors,
    }


def _api_auth_ok(handler) -> bool:
    """校验 /api/* 请求的 X-Auth-Token（config.server.auth_token 配置后启用）。"""
    token = str((load_config(CONFIG_FILE).get("server") or {}).get("auth_token") or "").strip()
    if not token:
        return True  # 未配置 token → 不启用鉴权
    supplied = (handler.headers.get("X-Auth-Token") or "").strip()
    return hmac.compare_digest(supplied, token)


class Handler(BaseHTTPRequestHandler):
    server_version = "HeliacMail/2.0"

    # 高频“轮询型”GET（仪表板/设置页每 10s 自动刷新），成功时记 DEBUG，
    # 不刷屏日志文件；真正的操作/报错仍记 INFO。
    _QUIET_GET = frozenset(("/", "/dashboard", "/setup", "/api/status",
                            "/api/recent", "/api/todos", "/api/settings"))

    def log_message(self, fmt, *args):  # 使用统一日志
        quiet = False
        if self.command == "GET" and args and len(args) >= 2:
            try:
                quiet = (int(args[1]) == 200
                         and self.path.split("?", 1)[0] in self._QUIET_GET)
            except (TypeError, ValueError):
                quiet = False
        msg = f"[HTTP] {self.client_address[0]} - {fmt % args}"
        if quiet:
            logger.debug(msg)
        else:
            log(msg)

    # ---------- 响应工具 ----------

    def _send(self, content: bytes, ctype: str, code: int = 200, cache: bool = False):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(content)))
        if not cache:
            self.send_header("Cache-Control", "no-store")
        try:
            self.end_headers()
            self.wfile.write(content)
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError, OSError):
            # 浏览器中途断开（如用户关闭页面）不必再补发 500 响应，
            # 避免 do_GET/do_POST 的 except 里二次 _json 连环刷屏。
            self.close_connection = True

    def _html(self, content: str, code: int = 200):
        self._send(content.encode("utf-8"), "text/html; charset=utf-8", code)

    def _json(self, obj, code: int = 200):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self._send(data, "application/json; charset=utf-8", code)

    def _go(self, path: str):
        self.send_response(302)
        self.send_header("Location", path)
        self.end_headers()

    def _post_data(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        length = min(length, 2 * 1024 * 1024)
        raw = self.rfile.read(length) if length else b""
        if not raw:
            return {}
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return {}

    # ---------- GET ----------

    def do_GET(self):
        try:
            p = self.path.split("?")[0]
            if p.startswith("/api/") and not _api_auth_ok(self):
                self._json({"success": False, "error": "未授权：缺少或错误的 X-Auth-Token"}, 401)
                return
            if p in ("/", ""):
                if normalize_accounts(load_config(CONFIG_FILE)):
                    self._go("/dashboard")
                else:
                    self._go("/setup")
            elif p == "/setup":
                self._html(_read_page("setup.html"))
            elif p == "/dashboard":
                self._html(_read_page("dashboard.html"))
            elif p == "/api/status":
                self._json(_status_json())
            elif p == "/api/accounts":
                accs = [{"email": a["email"], "imap_host": a.get("imap_host", "")}
                        for a in normalize_accounts(load_config(CONFIG_FILE))]
                self._json({"success": True, "accounts": accs})
            elif p == "/api/todos":
                self._json({"success": True, "todos": todos_mod.load(TODOS_FILE)})
            elif p == "/api/recent":
                self._json({"success": True, "items": recent_mod.load(RECENT_FILE)})
            elif p == "/api/vikunja/status":
                st = vk_mod.status(BASE_DIR)
                st["success"] = True
                self._json(st)
            elif p == "/api/vikunja/public-url":
                self._json({"success": True,
                            "public_url": vk_mod.effective_public_url(vk_mod.local_dir(BASE_DIR))})
            elif p == "/api/cloudflare/status":
                st = cf_mod.status(BASE_DIR)
                st["success"] = True
                self._json(st)
            elif p == "/api/settings":
                self._json({"success": True, "settings": settings_public()})
            else:
                self._json({"success": False, "error": "not found"}, 404)
        except Exception as e:
            logger.error(f"GET 错误: {e}", exc_info=True)
            self._json({"success": False, "error": str(e)}, 500)

    # ---------- POST ----------

    def do_POST(self):
        try:
            p = self.path.split("?")[0]
            if p.startswith("/api/") and not _api_auth_ok(self):
                self._json({"success": False, "error": "未授权：缺少或错误的 X-Auth-Token"}, 401)
                return
            data = self._post_data()
            if p == "/api/save":
                self._api_save(data)
            elif p == "/api/remove":
                self._api_remove(data)
            elif p == "/api/settings":
                self._api_settings(data)
            elif p == "/api/todo/sync":
                self._api_todo_sync()
            elif p.startswith("/api/todo/") and p.endswith("/toggle"):
                self._api_todo_toggle(p)
            elif p.startswith("/api/todo/") and p.endswith("/del"):
                self._api_todo_del(p)
            elif p == "/api/notify/test":
                self._api_notify_test()
            elif p == "/api/sync/test":
                self._api_sync_test()
            elif p == "/api/vikunja/status":
                st = vk_mod.status(BASE_DIR)
                st["success"] = True
                self._json(st)
            elif p == "/api/vikunja/stop":
                msg = vk_mod.stop(BASE_DIR)
                self._json({"success": True, "message": msg})
            elif p == "/api/vikunja/deploy":
                self._api_vikunja_deploy()
            elif p == "/api/vikunja/stop-port":
                self._api_vikunja_stop_port(data)
            elif p == "/api/vikunja/public-url":
                self._api_vikunja_public_url(data)
            elif p == "/api/cloudflare/deploy":
                self._api_cf_deploy()
            elif p == "/api/cloudflare/stop":
                self._api_cf_stop()
            elif p == "/api/ai/test":
                self._api_ai_test()
            elif p == "/api/poll":
                monitor.trigger()
                self._json({"success": True, "message": "已触发立即检查"})
            else:
                self._json({"success": False, "error": "not found"}, 404)
        except Exception as e:
            logger.error(f"POST 错误: {e}", exc_info=True)
            self._json({"success": False, "error": str(e)}, 500)

    # ---------- 各 API ----------

    def _api_save(self, data):
        email = (data.get("email") or "").strip()
        pwd = (data.get("password") or "").strip()
        interval = int(data.get("poll_interval") or 0)
        if not email:
            self._json({"success": False, "error": "请填写邮箱地址"}, 400)
            return
        err = _email_format_error(email)
        if err:
            self._json({"success": False, "error": err}, 400)
            return
        if "@example.com" in email.lower():
            self._json({"success": False, "error": "不能使用示例邮箱"}, 400)
            return
        # 判断是否新增：新增必须填密码
        existing = [a for a in normalize_accounts(load_config(CONFIG_FILE))
                    if a["email"].lower() == email.lower()]
        if not existing and not pwd:
            self._json({"success": False, "error": "新账户必须填写授权码/密码"}, 400)
            return
        _save_account(email, pwd, interval)
        monitor.trigger()
        logger.info(f"配置已保存: {email}（监控已触发）")
        self._json({"success": True, "message": "保存成功，正在开始监控"})

    def _api_remove(self, data):
        email = (data.get("email") or "").strip()
        with _cfg_lock:
            cfg = load_config(CONFIG_FILE)
            mon = cfg.get("monitor") or {}
            mon["accounts"] = [a for a in mon.get("accounts", [])
                               if (a.get("email") or "").lower() != email.lower()]
            cfg["monitor"] = mon
            save_config(cfg, CONFIG_FILE)
        logger.info(f"已删除账户: {email}")
        self._json({"success": True})

    def _api_settings(self, data):
        err = apply_settings(data or {})
        if err:
            self._json({"success": False, "error": err}, 400)
            return
        monitor.trigger()
        logger.info("设置已保存")
        self._json({"success": True, "message": "设置已保存"})

    def _api_todo_toggle(self, path):
        tid = path.split("/")[-2]
        if todos_mod.toggle(TODOS_FILE, tid):
            self._sync_after_change()
            self._json({"success": True})
        else:
            self._json({"success": False, "error": "未找到该待办"}, 404)

    def _api_todo_del(self, path):
        tid = path.split("/")[-2]
        cfg = load_config(CONFIG_FILE)
        err = tasksync_mod.delete_local(cfg, TODOS_FILE, tid, log=log)
        if err:
            self._json({"success": False, "error": err}, 404)
            return
        self._sync_after_change()
        self._json({"success": True})

    def _api_todo_sync(self):
        cfg = load_config(CONFIG_FILE)
        if tasksync_mod.enabled(cfg):
            counts = tasksync_mod.sync_todos(cfg, TODOS_FILE, log=log)
            if counts.get("ok"):
                changed = bool(counts["created"] or counts["done_local"] or counts["adopted"])
                msg = f"已同步：新建 {counts['created']}，远端完成 {counts['done_local']}，纳入 {counts['adopted']}"
                if not changed:
                    msg = "已同步：两边一致，无变更（未发通知）"
                else:
                    notify_mod.push_new_todo_notice(
                        cfg, counts["created"], counts["adopted"], counts["done_local"],
                        counts.get("project", ""))
                self._json({"success": True, "message": msg})
                return
            self._json({"success": False, "error": counts.get("error") or "同步失败"}, 400)
            return
        open_n = sum(1 for t in todos_mod.load(TODOS_FILE) if not t.get("done"))
        if not open_n:
            self._json({"success": False, "error": "暂无待办，无需同步"}, 400)
            return
        ok = pipeline_mod.sync_todos_to_phone(cfg, TODOS_FILE, log=log)
        self._json({"success": ok,
                    "message": "待办清单已发送到手机(ntfy)" if ok else "未同步（请先配置 ntfy 主题）"})

    def _sync_after_change(self):
        """本地待办变更后的联动：接任务软件→双向同步；否则→按需推 ntfy 清单。"""
        try:
            cfg = load_config(CONFIG_FILE)
            if tasksync_mod.enabled(cfg):
                counts = tasksync_mod.sync_todos(cfg, TODOS_FILE, log=log)
                if counts["ok"] and (counts["created"] or counts["done_local"]):
                    notify_mod.push_new_todo_notice(
                        cfg, created=counts["created"], done_local=counts["done_local"],
                        project=counts.get("project", ""))
            elif (cfg.get("notify") or {}).get("sync_todos"):
                pipeline_mod.sync_todos_to_phone(cfg, TODOS_FILE, log=log)
        except Exception as e:
            log(f"待办变更同步失败: {e}")

    def _api_sync_test(self):
        cfg = load_config(CONFIG_FILE)
        ok, msg = tasksync_mod.test_connection(cfg)
        self._json({"success": ok, "message": msg})

    def _api_vikunja_deploy(self):
        def _vklog(m):
            logger.info(f"[Vikunja] {m}")
        self._json({"success": True, "message": "正在部署/重新配置本地 Vikunja，请稍候…（进度见运行日志）"})
        # 部署可能耗时较久，放独立线程执行，避免卡住页面
        def _work():
            try:
                ok, msg = vk_mod.deploy(BASE_DIR, CONFIG_FILE, _vklog)
                logger.info(f"[Vikunja] 部署结果: {'成功' if ok else '失败'} {msg[-300:]}")
                vk_mod.LAST_DEPLOY.update({"ts": int(time.time()), "ok": ok, "msg": msg[-800:]})
                if ok:
                    monitor.trigger()
            except Exception as e:
                logger.error(f"[Vikunja] 部署异常: {e}", exc_info=True)
                vk_mod.LAST_DEPLOY.update({"ts": int(time.time()), "ok": False,
                                           "msg": f"部署异常：{e}"[:800]})
        threading.Thread(target=_work, daemon=True, name="VikunjaDeploy").start()

    def _api_vikunja_stop_port(self, data=None):
        """结束占用 3456 端口的外部进程（如旧目录残留的 vikunja.exe）。"""
        if data is None:
            data = self._post_data()
        try:
            pid = int(data.get("pid") or 0)
        except (TypeError, ValueError):
            pid = 0
        if pid <= 0:
            self._json({"success": False, "error": "缺少进程 PID"}, 400)
            return
        ok, msg = vk_mod.stop_port_process(pid)
        self._json({"success": ok, "message": msg}, 200 if ok else 400)

    def _api_vikunja_public_url(self, data=None):
        if data is None:
            data = self._post_data()
        url = (data.get("url") or "").strip()
        if not url:
            logger.warning("[Vikunja] 应用外部访问地址失败：未收到 url 参数（POST body 为空）")
            self._json({"success": False, "error": "请填写外部访问地址（http://…）"}, 400)
            return
        ok, msg = vk_mod.set_public_url(
            BASE_DIR, url, lambda m: logger.info(f"[Vikunja] {m}"))
        if not ok:
            logger.warning(f"[Vikunja] 应用外部访问地址失败（{url}）：{msg}")
        self._json({"success": ok, "message": msg}, 200 if ok else 400)

    def _api_cf_deploy(self):
        """☁️ 一键安装并启动 Cloudflare Tunnel（后台线程，网页轮询 /api/cloudflare/status）。"""
        def _cf_log(m):
            logger.info(f"[Cloudflare] {m}")

        def _work():
            try:
                ok, msg = cf_mod.start(BASE_DIR, _cf_log)
                cf_mod.LAST.update({"ts": int(time.time()), "ok": bool(ok),
                                    "msg": (msg or "")[-500:]})
                logger.info(f"[Cloudflare] 启动结果: {'成功' if ok else '失败'} {str(msg)[:200]}")
            except Exception as e:
                logger.error(f"[Cloudflare] 部署异常: {e}", exc_info=True)
                cf_mod.LAST.update({"ts": int(time.time()), "ok": False,
                                    "msg": f"部署异常：{e}"[:500]})

        threading.Thread(target=_work, daemon=True, name="CloudflareDeploy").start()
        self._json({"success": True,
                    "message": "已开始安装/启动 Cloudflare 隧道（首次下载约 60MB，之后约 5~20 秒取得地址，请稍候）"})

    def _api_cf_stop(self):
        msg = cf_mod.stop(BASE_DIR, lambda m: logger.info(f"[Cloudflare] {m}"))
        self._json({"success": True, "message": msg})

    def _api_notify_test(self):
        cfg = load_config(CONFIG_FILE)
        try:
            msg = notify_mod.test_push(cfg)
            self._json({"success": True, "message": msg})
        except Exception as e:
            self._json({"success": False, "error": str(e)}, 400)

    def _api_ai_test(self):
        cfg = load_config(CONFIG_FILE)
        ai_cfg = cfg.get("ai") or {}
        if not ai_cfg.get("enabled") or not ai_cfg.get("providers"):
            self._json({"success": False,
                        "error": "请先在 设置→AI 分析 里启用并添加 provider"}, 400)
            return
        ok, result = ai_mod.quick_test(ai_cfg, log=log)
        self._json({"success": ok,
                    "message": "AI 测试成功，示例邮件分析结果如下" if ok else str(result),
                    "analysis": result if ok else None})


# ---------- 启动 ----------

def _open_browser(port: int) -> None:
    time.sleep(1.2)
    url = f"http://127.0.0.1:{port}"
    log(f"打开浏览器: {url}")
    try:
        webbrowser.open(url)
    except Exception as e:
        log(f"自动打开浏览器失败，请手动访问 {url}: {e}")


def main() -> None:
    print("=" * 56)
    print("  HeliacMail v2 · 多邮箱 AI 助手（邮件分析 · 待办 · ntfy 手机推送）")
    print("  保留关键词提取；配置 AI 后自动升级为智能分析")
    print("=" * 56)

    # 首次运行生成默认配置
    cfg = config_mod.ensure_config_file(CONFIG_FILE)
    # 已装过本地 Vikunja 且配置指向本机 → 自动拉起，无需每次手动部署
    vk_mod.auto_start_local(cfg, BASE_DIR, logger.info)
    server_cfg = cfg.get("server") or {}
    host = (server_cfg.get("host") or "127.0.0.1").strip()
    port = int(server_cfg.get("port") or 8090)
    auth_token = str(server_cfg.get("auth_token") or "").strip()
    if host not in ("127.0.0.1", "localhost", "::1") and not auth_token:
        print("⚠ 警告：Web 服务绑定到非本机地址（%s）且未配置 server.auth_token，" % host)
        print("   局域网内任何设备都可能访问并修改配置。请在 config.yaml 的 server 段")
        print("   新增 auth_token（任意长字符串）作为访问令牌后再开放局域网访问。")

    accounts = normalize_accounts(cfg)
    if accounts:
        print(f"✔ 检测到 {len(accounts)} 个邮箱账户")
    else:
        print("✔ 请在浏览器中添加邮箱账户（保存后立即生效）")
    if notify_mod.topic_configured(cfg):
        print("✔ ntfy 推送已启用（主题: "
              f"{((cfg.get('notify') or {}).get('ntfy') or {}).get('topic')}）")
    else:
        print("○ 未配置 ntfy 主题：手机推送关闭，仍可网页查看待办/摘要")
    ai_cfg = cfg.get("ai") or {}
    if ai_cfg.get("enabled") and ai_cfg.get("providers"):
        print(f"✔ AI 分析已启用（{len(ai_cfg['providers'])} 个 provider）")
    else:
        print("○ AI 未启用：当前按关键词模式提取待办")

    monitor.start()

    try:
        httpd = ThreadingHTTPServer((host, port), Handler)
    except OSError as e:
        print(f"✗ 启动 Web 服务失败（端口 {port} 可能被占用）: {e}")
        raise SystemExit(1)
    threading.Thread(target=httpd.serve_forever, daemon=True, name="Web").start()
    print(f"✔ Web 界面: http://localhost:{port}")
    threading.Thread(target=_open_browser, args=(port,), daemon=True).start()
    print("按 Ctrl+C 停止程序\n")

    def _stop(*_):
        raise SystemExit(0)
    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)
    try:
        while True:
            time.sleep(1)
    except (KeyboardInterrupt, SystemExit):
        print("\n程序已停止。")
        monitor.stop()
        # 先停掉本工具拉起的 Vikunja 子进程，避免关窗后留下孤儿进程占住控制台/端口
        vk_mod.stop_if_owned(BASE_DIR)
        try:
            httpd.shutdown()
        except Exception:
            pass
        # 兜底强制退出：即使仍有非 daemon 线程/子进程残留，也要保证黑框窗口能被关闭
        os._exit(0)


if __name__ == "__main__":
    main()
