# -*- coding: utf-8 -*-
"""
MIME 邮件解析：主题/发件人解码（兼容 GBK/Big5 等）、纯文本与 HTML 正文提取、
<a> 链接保留（供点击跳转）。借鉴 mailpilot 的 go-message 处理思路。
"""
import base64
import email
import html as html_mod
import re
from email.header import decode_header
from email.utils import parseaddr
from typing import List, Optional

_SCRIPT_RE = re.compile(r"<\s*(?:script|style)[^>]*>.*?<\s*/\s*(?:script|style)\s*>", re.I | re.S)
_ANCHOR_RE = re.compile(r"<\s*a\b[^>]*\bhref\s*=\s*[\"']([^\"']+)[\"'][^>]*>(.*?)<\s*/\s*a\s*>", re.I | re.S)
_BLOCK_RE = re.compile(r"<\s*(?:br\s*/?|/p|/div|/tr|/li|/h[1-6]|/blockquote|hr\s*/?)[^>]*>", re.I)
_TAG_RE = re.compile(r"<[^>]+>")
_MULTISPACE_RE = re.compile(r"[ \t\r\f\v]+")
_EMPTY_LINE_RE = re.compile(r"\n{3,}")

MAX_IMAGES = 3          # 最多喂给 AI 的图片数
MAX_IMAGE_BYTES = 4_000_000   # 单张不超过 4MB（base64 后再限制更稳妥）


def _decode_fallback(raw: bytes, charset: Optional[str]) -> str:
    for cs in [charset, "utf-8", "gb18030", "big5", "shift_jis"]:
        if not cs:
            continue
        try:
            return raw.decode(cs)
        except (LookupError, UnicodeDecodeError):
            continue
    return raw.decode("utf-8", errors="replace")


def decode_mime_words(raw) -> str:
    """解码 RFC2047 编码词：=?utf-8?b?...?= / =?gbk?q?...?= 等。"""
    if raw is None:
        return ""
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    out = []
    try:
        for data, charset in decode_header(raw):
            if isinstance(data, bytes):
                out.append(_decode_fallback(data, charset))
            else:
                out.append(str(data))
    except Exception:
        return str(raw)
    return "".join(out)


def decode_address_field(raw) -> "tuple[str, str, str]":
    """解析 From 头 → (显示名, 邮箱地址, 展示文本)。"""
    if raw is None:
        return "", "", ""
    try:
        name, addr = parseaddr(decode_mime_words(raw))
    except Exception:
        return "", "", ""
    if not addr:
        # 尝试逐个处理
        return "", "", decode_mime_words(raw)
    disp = f"{name} <{addr}>" if name and name != addr else addr
    return name, addr, disp


def _decode_part(part) -> str:
    payload = part.get_payload(decode=True)
    if payload is None:
        text = part.get_payload()
        return str(text) if isinstance(text, str) else ""
    return _decode_fallback(payload, part.get_content_charset())


def _clean_ws(s: str) -> str:
    lines = [_MULTISPACE_RE.sub(" ", ln).strip() for ln in s.splitlines()]
    lines = [ln for ln in lines if ln]
    return _EMPTY_LINE_RE.sub("\n", "\n".join(lines))


def html_to_text(h: str) -> str:
    """把 HTML 粗略转成可读文本，并把 <a href> 以 (url) 形式保留。"""
    h = _SCRIPT_RE.sub(" ", h or "")

    def _link(m):
        url = m.group(1).strip()
        text = _TAG_RE.sub(" ", m.group(2))
        text = _MULTISPACE_RE.sub(" ", text).strip()
        text = html_mod.unescape(text)
        if url.lower().startswith(("http://", "https://")):
            return f"{text} ({url})" if text else url
        return text
    h = _ANCHOR_RE.sub(_link, h)
    h = _BLOCK_RE.sub("\n", h)
    h = _TAG_RE.sub("", h)
    return _clean_ws(html_mod.unescape(h))


def html_links(h: str) -> List[str]:
    seen, out = set(), []
    for m in _ANCHOR_RE.finditer(h or ""):
        url = m.group(1).strip()
        if url.lower().startswith(("http://", "https://")) and url not in seen:
            seen.add(url)
            out.append(url)
    return out[:20]


def parse_rfc822(raw: bytes, want_images: bool = True) -> dict:
    """解析原始 RFC822 邮件字节，返回规整字段字典。

    want_images=False 时跳过内嵌图片提取（未启用视觉模型时省去 base64 的
    大额 CPU/内存开销）。
    """
    msg = email.message_from_bytes(raw)
    subject = decode_mime_words(msg.get("Subject", ""))
    name, addr, from_text = decode_address_field(msg.get("From", ""))
    message_id = str(msg.get("Message-ID", "")).strip()
    date_raw = str(msg.get("Date", "")).strip()

    plain_parts, html_parts = [], []
    images = []  # 内嵌/附件图片 → base64，供视觉 AI 识别
    total_img = 0
    for part in msg.walk():
        ctype = part.get_content_type()
        cd = str(part.get("Content-Disposition", "")).lower()
        if ctype == "text/plain" and "attachment" not in cd:
            try:
                plain_parts.append(_decode_part(part))
            except Exception:
                continue
        elif ctype == "text/html" and "attachment" not in cd:
            try:
                html_parts.append(_decode_part(part))
            except Exception:
                continue
        elif want_images and ctype.startswith("image/") and len(images) < MAX_IMAGES:
            try:
                payload = part.get_payload(decode=True)
            except Exception:
                payload = None
            if payload and len(payload) > 2048 and len(payload) <= MAX_IMAGE_BYTES:
                total_img += len(payload)
                if total_img <= MAX_IMAGE_BYTES * 2:
                    images.append({"mime": ctype,
                                   "b64": base64.b64encode(payload).decode("ascii")})
                else:
                    break


    body = "\n".join(p for p in plain_parts if p and p.strip()).strip()
    html_raw = "\n".join(html_parts)
    mode = "plain"
    if not body and html_raw.strip():
        body = html_to_text(html_raw)
        mode = "html"
    links = html_links(html_raw) if html_raw else []

    return {
        "subject": subject,
        "from_name": name,
        "from_addr": addr,
        "from_text": from_text,
        "date": date_raw,
        "message_id": message_id,
        "body": body,
        "body_mode": mode,
        "links": links,
        "images": images,
    }
