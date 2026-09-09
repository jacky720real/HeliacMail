#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AI 结构化邮件分析模块：调用 OpenAI 兼容接口（可配置多个 provider 自动降级），
把邮件正文/附件文本解析为结构化分析结果（优先级、类别、待办）。

多 provider 自动降级策略：按配置顺序依次尝试，全部失败才算失败；
每个 provider 失败时记录原因，便于在网页“AI 测试”中直接看到是哪一步出问题。
"""
import json
import re
import time
from datetime import datetime

import requests

# 邮件分类（与网页下拉、权重表保持一致）
CATEGORIES = [
    "工作", "财务", "生活", "订阅", "广告", "社交", "通知", "物流", "其他",
]


class AIError(Exception):
    """AI 调用失败（含降级失败）时抛出。"""


def _weight_of(weights, category, default=3):
    try:
        return max(1, min(5, int(weights.get(category, default))))
    except (TypeError, ValueError, AttributeError):
        return default


def _fmt_weight(weights, category, default=3):
    w = _weight_of(weights, category, default)
    return "高" if w >= 4 else ("中" if w >= 2 else "低")


def _summarize_body(body: str, limit: int = 600) -> str:
    """正文截断：保留开头（通常含关键信息），超出部分省略。"""
    body = (body or "").strip()
    if not body:
        return "（无正文）"
    body = re.sub(r"[ \t]+$", "", body, flags=re.M)
    if len(body) <= limit:
        return body
    return body[:limit] + "\n……（正文过长已截断）"


def _parse_float(value, default=None):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _parse_json_object(text: str) -> dict:
    """尽力从模型返回文本中解析出 JSON 对象（容忍 ```json 围栏与前后噪音）。"""
    if not text:
        raise AIError("模型返回为空")
    t = text.strip()
    # 去掉 ```json ... ``` 围栏
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", t, re.S)
    if m:
        t = m.group(1)
    m = re.search(r"\{.*\}", t, re.S)
    if m:
        t = m.group(0)
    try:
        obj = json.loads(t)
    except json.JSONDecodeError as e:
        raise AIError(f"模型返回不是合法 JSON：{e}")
    if not isinstance(obj, dict):
        raise AIError("模型返回 JSON 不是对象")
    return obj


def _extract_todos_from_text(text: str) -> list:
    """从任意文本中按行提取待办（`- [ ]`、`- `、`* ` 或编号行）。"""
    todos = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        m = re.match(r"^(?:[-*+]|\d+[.、)])\s+(.+)$", line)
        if m:
            todos.append(m.group(1).strip())
    return todos[:20]


def _normalize_category(raw: str, weights: dict) -> str:
    cat = (raw or "").strip()
    for c in CATEGORIES:
        if cat == c or cat == c.replace("其他", "其它"):
            return c
    if not cat or cat.lower() in ("unknown", "null", "none", "其他", "其它"):
        return "其他"
    return "其他"  # 未识别一律归“其他”，避免前端出现未定义颜色


def _build_system_prompt(ai_cfg: dict) -> str:
    weights = ai_cfg.get("weights") or {}
    prefs = str(ai_cfg.get("preferences") or "").strip()
    lines = [
        "你是一个严谨的邮件助理。请阅读下面邮件内容，输出严格的 JSON 对象（不要输出其他文字）：",
        "{",
        '  "priority": 1-5 的整数（5 最高），依据收件人视角的重要紧急程度',
        '  "category": 分类，只能从以下取值：' + ", ".join(CATEGORIES),
        '  "summary": 一句话中文摘要（≤30字）',
        '  "action": 是否需要处理（true/false）',
        '  "todos": 从中提取出的待办列表（数组，每项为字符串；没有则为空数组）',
        '  "reason": 一句话说明判断依据',
        "}",
        "",
        "分类权重参考（1-5，数字越大越重要）：",
    ]
    for c in CATEGORIES:
        lines.append(f'- {c}: {_weight_of(weights, c)}')
    if prefs:
        lines.append("")
        lines.append("用户附加偏好：" + prefs)
    lines.append("")
    lines.append("规则：priority 与分类权重一致；摘要必须中文；todos 只保留明确动词/任务的条目。")
    return "\n".join(lines)


def _call_provider(provider: dict, sys_prompt: str, user_content: str,
                   timeout: int = 120) -> dict:
    """调用单个 provider；失败抛 AIError（含原因）。"""
    name = provider.get("name") or "unnamed"
    ptype = (provider.get("type") or "openai").lower()
    base = (provider.get("base_url") or "").strip().rstrip("/")
    model = provider.get("model") or ""
    key = provider.get("api_key") or ""
    if not base:
        raise AIError(f"provider[{name}] 缺少 base_url")
    if not model:
        raise AIError(f"provider[{name}] 缺少 model")

    url = base + "/chat/completions"
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": user_content},
        ],
        "temperature": 0.2,
        "max_tokens": 800,
        "stream": False,
    }
    if ptype == "anthropic":
        # Anthropic 原生格式：URL 为 /v1/messages
        url = base + "/messages"
        payload = {
            "model": model,
            "max_tokens": 800,
            "system": sys_prompt,
            "messages": [{"role": "user", "content": user_content}],
        }
    try:
        resp = requests.post(url, json=payload, headers=headers, timeout=timeout)
    except requests.RequestException as e:
        raise AIError(f"provider[{name}] 网络错误: {e}")
    if resp.status_code != 200:
        raise AIError(f"provider[{name}] HTTP {resp.status_code}: {resp.text[:200]}")
    try:
        data = resp.json()
    except ValueError:
        raise AIError(f"provider[{name}] 返回非 JSON: {resp.text[:200]}")

    if ptype == "anthropic":
        try:
            content = data["content"][0]["text"]
        except (KeyError, IndexError, TypeError):
            raise AIError(f"provider[{name}] 响应缺少 content: {str(data)[:200]}")
    else:
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            raise AIError(f"provider[{name}] 响应缺少 choices[0].message.content: {str(data)[:200]}")
    return _parse_json_object(content)


def analyze_email(ai_cfg: dict, sender: str, subject: str, body: str,
                  log=None) -> dict:
    """分析一封邮件，返回结构化结果；所有 provider 失败时抛 AIError。"""
    if not ai_cfg.get("enabled"):
        raise AIError("AI 未启用")
    providers = ai_cfg.get("providers") or []
    if not providers:
        raise AIError("未配置 AI provider")

    sys_prompt = _build_system_prompt(ai_cfg)
    user_content = (
        f"发件人: {sender}\n主题: {subject}\n\n正文:\n{_summarize_body(body)}"
    )
    timeout = max(int(ai_cfg.get("timeout") or 120), 10)
    errors = []
    for p in providers:
        try:
            obj = _call_provider(p, sys_prompt, user_content, timeout=timeout)
            if log:
                log(f"AI provider[{p.get('name')}] 分析成功")
            return normalize_result(obj, ai_cfg.get("weights") or {})
        except AIError as e:
            errors.append(str(e))
            if log:
                log(f"AI provider[{p.get('name')}] 失败: {e}")
        except Exception as e:
            errors.append(f"{p.get('name')}: {e}")
            if log:
                log(f"AI provider[{p.get('name')}] 异常: {e}")
    raise AIError("；".join(errors) or "所有 provider 均失败")


def normalize_result(obj: dict, weights: dict) -> dict:
    """把模型返回对象规范化为固定结构（缺字段给默认值，类型强转）。"""
    category = _normalize_category(obj.get("category"), weights)
    try:
        priority = max(1, min(5, int(obj.get("priority") or 3)))
    except (TypeError, ValueError):
        priority = 3
    summary = str(obj.get("summary") or "").strip()[:80]
    if not summary:
        summary = "（模型未给出摘要）"
    reason = str(obj.get("reason") or "").strip()[:200]
    action = bool(obj.get("action"))
    todos = []
    for t in (obj.get("todos") or []):
        if isinstance(t, str) and t.strip():
            todos.append(t.strip()[:120])
    return {
        "priority": priority,
        "category": category,
        "summary": summary,
        "action": action,
        "todos": todos[:20],
        "reason": reason,
        "model": (obj.get("model") or ""),
        "analyzed_at": datetime.now().isoformat(timespec="seconds"),
    }


def quick_test(ai_cfg: dict, log=None) -> tuple:
    """网页“AI 测试”：用示例邮件跑一次完整链路。返回 (ok, 结果)。"""
    sample = (
        "发件人: boss@example.com\n"
        "主题: 明天上午10点项目评审会\n\n"
        "各位，明天上午 10 点在三楼会议室召开项目评审会，请提前准备好材料。\n"
        "会后请把会议纪要发给全体成员。另外记得提醒财务下周一前提交预算表。\n"
    )
    try:
        result = analyze_email(ai_cfg, "boss@example.com", "明天上午10点项目评审会",
                               sample, log=log)
        return True, result
    except AIError as e:
        return False, str(e)


def keyword_todos(subject: str, body: str, keywords: list) -> list:
    """关键词兜底：AI 不可用时按关键词提取待办。"""
    kw = [k.strip().lower() for k in (keywords or []) if k and k.strip()]
    if not kw:
        return []
    text = f"{subject}\n{body}".lower()
    hits = []
    for k in kw:
        idx = text.find(k)
        if idx >= 0:
            start = max(0, idx - 30)
            end = min(len(text), idx + len(k) + 60)
            hits.append(text[start:end].strip())
    return hits[:10]
