# -*- coding: utf-8 -*-
"""
非 AI 关键词待办提取（保留原程序功能，AI 不可用时兜底）。

改进：去掉行内 URL 与列表符号、过滤营销/系统噪声行、按行去重。
"""
import re
from typing import List

URL_PATTERN = re.compile(r"https?://\S+|www\.\S+", re.IGNORECASE)
# 命中则忽略的行（营销/系统邮件常见噪声），提升待办质量
NOISE_SUBSTR = (
    "unsubscribe", "退订", "隐私", "privacy", "版权", "©",
    "点击查看", "立即查看", "查看全部", "此邮件", "请勿回复",
    "noreply", "登录网页版", "关注我们", "follow us",
)

MAX_LINES = 50
MAX_LINE = 200
PREFIX_RE = re.compile(r"^[\s\-*•·\d\.\)、]+")


def _strip_line(raw: str) -> str:
    line = raw.strip()
    if not line:
        return ""
    # 去掉行内 URL（跟踪链接/广告链接）
    cleaned = URL_PATTERN.sub(" ", line).strip()
    if not cleaned:
        return ""
    plain = PREFIX_RE.sub("", cleaned).strip()
    if len(plain) < 4:
        return ""
    low = plain.lower()
    if any(n in low for n in NOISE_SUBSTR):
        return ""
    return plain[:MAX_LINE]


def extract_todos(subject: str, body: str, keywords: List[str]) -> List[str]:
    """从主题+正文中提取含关键词的行作为待办（主题不附加“主题:”前缀）。"""
    if not keywords:
        return []
    kws = [str(k).strip() for k in keywords if str(k).strip()]
    out: List[str] = []
    texts: List[str] = []
    if subject and subject.strip():
        texts.append(subject)
    if body:
        texts.extend(body.splitlines())
    for raw in texts:
        plain = _strip_line(raw)
        if not plain:
            continue
        low = plain.lower()
        for kw in kws:
            if kw.lower() in low:
                if plain not in out:
                    out.append(plain)
                break
        if len(out) >= MAX_LINES:
            break
    return out
