# -*- coding: utf-8 -*-
"""
推送模块：ntfy（免费、支持手机订阅）。用户只需填一个主题名，
在 ntfy 手机 App 里订阅该主题即可收到推送（借鉴 mailpilot 的通知能力）。
"""
import logging
import time
from typing import Dict, List, Optional

import requests

from core.config import expand_env_text

logger = logging.getLogger("mail_monitor.notifier")

# 网络请求超时：15 秒太短（ntfy.sh 偶发慢响应 → ReadTimeout），统一放宽到 30 秒。
REQUEST_TIMEOUT_SECONDS = 30
# 真实业务推送（邮件摘要）正文最多 300 字符；需要完整正文的推送可显式传 max_len。
MESSAGE_DEFAULT_LIMIT = 300
# 需要较长正文的场景（如把整份待办清单推到手机）允许放宽到原 4000 上限。
MESSAGE_FULL_LIMIT = 4000

# 最近一次成功发送的内容（主题+标题+正文+优先级），用于防“循环/重复推送”
_last_pub = {"key": None, "ts": 0.0}
PUB_DEDUP_SECONDS = 20.0


def ntfy_settings(cfg: dict) -> dict:
    return (cfg.get("notify") or {}).get("ntfy") or {}


def topic_configured(cfg: dict) -> bool:
    return bool(((cfg.get("notify") or {}).get("ntfy") or {}).get("topic", "").strip())


def publish(cfg: dict, title: str, message: str,
            tags: Optional[List[str]] = None, priority: int = 3,
            click: str = "", max_len: int = MESSAGE_DEFAULT_LIMIT) -> bool:
    """发送 ntfy 推送。未配置主题返回 False；网络失败抛可读的 RuntimeError。

    使用 ntfy 的标准 JSON 发布接口：POST 到服务器根路径（https://ntfy.sh/），
    把 topic/title/message/priority/click 放进 JSON body（UTF-8）。
    几点约定：
    1) 不用“HTTP Header 传中文标题”——HTTP Header 只允许 latin-1，中文会报错；
    2) 不 POST JSON 到 /topic 路径——那样 JSON 会被当成消息正文文字；
    3) 正文默认按 300 字符截断（max_len 可调），超时统一为 30 秒；
    4) ReadTimeout / ConnectionError 等网络异常转为通俗日志 + RuntimeError，
       不把 urllib3/requests 的原始堆栈直接抛给上层界面。
    tags 参数已弃用（不再下发 emoji 标签，保持普通文字通知）。
    """
    ntfy = ntfy_settings(cfg)
    server = expand_env_text(str(ntfy.get("server") or "https://ntfy.sh")).strip().rstrip("/")
    topic = expand_env_text(str(ntfy.get("topic") or "")).strip()
    if not topic:
        return False
    body = (message or "")[:max_len]
    title = (title or "").strip()[:200]

    key = (topic, title, body, int(priority))
    now = time.monotonic()
    if key == _last_pub["key"] and now - _last_pub["ts"] < PUB_DEDUP_SECONDS:
        return True  # 与上一条完全相同的推送短时间内刚发过 → 去重，防循环

    payload: Dict[str, object] = {
        "topic": topic,
        "message": body,
        "priority": int(priority),
    }
    if title:
        payload["title"] = title
    if click:
        payload["click"] = (click or "")[:500]

    auth = None
    root = server
    if server.startswith(("http://", "https://")):
        scheme, rest = server.split("://", 1)
        if "@" in rest:
            userinfo, host = rest.rsplit("@", 1)
            if ":" in userinfo:
                u, pw = userinfo.split(":", 1)
                auth = (u, pw)
            root = f"{scheme}://{host}"
    url = root + "/"   # ntfy 根路径 = JSON 发布接口
    try:
        resp = requests.post(url, json=payload, auth=auth,
                             timeout=REQUEST_TIMEOUT_SECONDS)
        resp.raise_for_status()
    except requests.exceptions.ReadTimeout:
        logger.warning("ntfy 推送超时：%s 在 %d 秒内未响应（ReadTimeout），请稍后重试。",
                       server, REQUEST_TIMEOUT_SECONDS)
        raise RuntimeError(
            f"推送超时：连接 ntfy 服务器（{server}）超过 "
            f"{REQUEST_TIMEOUT_SECONDS} 秒没有响应，请检查网络后重试。") from None
    except requests.exceptions.ConnectionError:
        logger.warning("ntfy 连接失败：无法连接到 %s（ConnectionError），请检查网络。",
                       server)
        raise RuntimeError(
            "推送失败：无法连接到 ntfy 服务器，请检查本机网络或 "
            "「服务器地址」是否填写正确。") from None
    except requests.exceptions.HTTPError:
        logger.warning("ntfy 服务器返回错误：HTTP %s（%s）", resp.status_code, server)
        raise RuntimeError(
            f"推送失败：ntfy 服务器返回 HTTP {resp.status_code}（{server}），"
            "请检查主题名与服务器配置。") from None
    except requests.exceptions.RequestException as e:
        logger.warning("ntfy 推送请求异常：%s", e)
        raise RuntimeError(f"推送失败：{e}") from e
    _last_pub["key"] = key
    _last_pub["ts"] = now
    return True


def test_push(cfg: dict) -> str:
    """发送一条固定文本的测试推送，返回结果文本（供设置页测试按钮）。

    重要约束：本函数是「测试推送」的唯一处理函数，只做 ntfy 推送。
    它绝不调用任何读取邮箱 / 解析邮件 / 触发监控扫描的代码，无论配置了
    多少个邮箱账户，都只发下面这一条固定测试消息。
    """
    topic = expand_env_text(str(ntfy_settings(cfg).get("topic") or "")).strip()
    if not topic:
        return "请先填写 ntfy 主题名"
    # 每次点击都真实发出（清除与业务推送共用的去重指纹，避免“刚发过就不发”）
    _last_pub.update({"key": None, "ts": 0.0})
    publish(cfg, "测试推送", "这是一条测试消息", priority=3)
    return f"已发送测试推送，请检查手机通知（主题 {topic}）"


def priority_of(analysis, cfg: dict) -> int:
    """紧急度 → ntfy priority。"""
    urgent = int((cfg.get("notify") or {}).get("priority_urgent") or 5)
    urgency = getattr(analysis, "urgency", "中")
    if urgency == "高":
        return urgent
    if urgency == "低":
        return 2
    return 3


def category_emoji(category: str) -> str:
    return {
        "工作": "💼", "财务": "💰", "账单": "🧾", "营销推广": "📢",
        "通知": "🔔", "个人": "👤", "验证码": "🔑", "垃圾": "🗑️", "其他": "📩",
    }.get(category or "", "📩")


def is_skipped_category(category: str, cfg: dict) -> bool:
    skip = (cfg.get("notify") or {}).get("skip_categories") or []
    return category in skip


def first_batch_quiet(cfg: dict) -> bool:
    """首轮（存量未读）默认不逐封推送到手机，只进网页简报/待办，避免轰炸。"""
    return bool((cfg.get("notify") or {}).get("first_batch_quiet", True))


def build_todo_snapshot(todos: List[dict]) -> tuple:
    """把待办清单格式化成纯文本推送。返回 (title, message)。"""
    open_items = [t for t in todos if not t.get("done")]
    done_items = [t for t in todos if t.get("done")]
    lines = []
    if open_items:
        lines.append(f"未完成 {len(open_items)} 条：")
        lines.extend(f"- {t.get('text')}" for t in open_items[:15])
        if len(open_items) > 15:
            lines.append(f"（还有 {len(open_items) - 15} 条未列出）")
    if done_items:
        if lines:
            lines.append("")
        lines.append("已完成：")
        lines.extend(f"- {t.get('text')}" for t in done_items[:5])
    if not lines:
        lines = ["暂无待办。"]
    body = "\n".join(lines)
    title = f"待办 {len(open_items)} 条未完成"
    if done_items:
        title += f"（已完成 {len(done_items)}）"
    return title, body


def push_new_todo_notice(cfg: dict, created: int = 0, adopted: int = 0,
                         done_local: int = 0, project: str = "") -> bool:
    """真实任务后端启用时：ntfy 只提醒“生成了新待办”，不再发整份清单。"""
    if not topic_configured(cfg):
        return False
    parts = []
    if created:
        parts.append(f"新增 {created} 条待办")
    if adopted:
        parts.append(f"纳入手机新增 {adopted} 条")
    if done_local:
        parts.append(f"手机已完成 {done_local} 条")
    if not parts:
        parts.append("待办已同步")
    body = "；".join(parts)
    if project:
        body += f"\n项目：{project}"
    body += "\n打开手机 App 查看，勾选完成后会自动同步。"
    try:
        return publish(cfg, "邮箱助手 · 待办已同步", body, priority=3)
    except Exception:
        return False
