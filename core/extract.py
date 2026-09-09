# -*- coding: utf-8 -*-
"""待办关键词提取（AI 不可用时兜底）。"""
import re

KEYWORDS = ["待办", "需要", "请", "务必", "记得"]


def extract_keyword_todos(subject: str, body_text: str, keywords=None) -> list:
    """从主题/正文里按关键词提取待办句子。返回 [{title, from}]。"""
    kws = keywords or KEYWORDS
    if not kws:
        return []
    text = (body_text or "").strip()
    lines = [ln.strip() for ln in re.split(r"[\n\r；;。！!？?]", text) if ln.strip()]
    todos = []
    for ln in lines:
        if any(k in ln for k in kws) and 2 <= len(ln) <= 80:
            todos.append({"title": ln, "from": "body"})
    # 主题也算一条候选（放最前）
    subj = (subject or "").strip()
    if subj and any(k in subj for k in kws):
        todos.insert(0, {"title": subj, "from": "subject"})
    return todos[:5]
