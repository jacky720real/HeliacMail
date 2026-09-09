# -*- coding: utf-8 -*-
"""邮件正文解析（HTML → 纯文本 / 简单清洗）。"""
import email
import html
import re


def get_body(msg) -> str:
    """返回邮件纯文本正文（优先 text/plain；否则 HTML 转文本）。"""
    if msg.is_multipart():
        for part in msg.walk():
            ct = (part.get_content_type() or "").lower()
            if ct == "text/plain":
                payload = _decode_payload(part)
                if payload:
                    return payload
        for part in msg.walk():
            ct = (part.get_content_type() or "").lower()
            if ct == "text/html":
                payload = _decode_payload(part)
                if payload:
                    return html_to_text(payload)
        return ""
    ct = (msg.get_content_type() or "").lower()
    payload = _decode_payload(msg)
    if not payload:
        return ""
    return html_to_text(payload) if ct == "text/html" else payload


def _decode_payload(part) -> str:
    try:
        payload = part.get_payload(decode=True)
        if payload is None:
            return ""
        charset = part.get_content_charset() or "utf-8"
        try:
            return payload.decode(charset, errors="replace")
        except LookupError:
            return payload.decode("utf-8", errors="replace")
    except Exception:
        return ""


def html_to_text(html_str: str) -> str:
    """极简 HTML → 纯文本：去 script/style、去标签、解实体。"""
    s = html_str or ""
    s = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", s)
    s = re.sub(r"(?is)<br\s*/?>", "\n", s)
    s = re.sub(r"(?is)</p>|</div>|</tr>|</li>", "\n", s)
    s = re.sub(r"(?s)<[^>]+>", " ", s)
    s = html.unescape(s)
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\n\s*\n+", "\n", s)
    return s.strip()


def normalize_subject(raw: str) -> str:
    """去掉主题里常见的前缀/编码残留。"""
    s = (raw or "").strip()
    s = re.sub(r"(?i)^\s*(re|fw|fwd|回复|转发|答复)\s*[:：]\s*", "", s)
    return s
