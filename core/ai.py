# -*- coding: utf-8 -*-
"""AI 邮件分析（OpenAI 兼容接口，多 provider 自动降级）。"""
import json
import time
import requests

CATEGORIES = ["urgent", "important", "normal", "news", "social", "promo", "finance", "other"]
CATEGORY_LABELS = {
    "urgent": "紧急",
    "important": "重要",
    "normal": "普通",
    "news": "资讯",
    "social": "社交",
    "promo": "促销",
    "finance": "财务",
    "other": "其他",
}

DEFAULT_WEIGHTS = {
    "urgent": 5,
    "important": 4,
    "normal": 3,
    "news": 2,
    "social": 2,
    "promo": 1,
    "finance": 4,
    "other": 2,
}

_SYSTEM_PROMPT = """你是邮件助手。分析邮件，只输出 JSON（不要 markdown）：
{"category": "urgent|important|normal|news|social|promo|finance|other",
 "summary": "一句话中文摘要（40字内）",
 "todo": "如需行动，给出简短待办（一句话）；否则为空字符串",
 "deadline": "如有截止时间（YYYY-MM-DD 或原文）；否则为空"}
邮件语言可能是中文或英文，摘要一律用中文。"""


def _call_provider(provider: dict, messages: list, timeout: int) -> dict:
    """调用单个 OpenAI 兼容 provider，返回解析后的 JSON。"""
    url = (provider.get("base_url") or "").rstrip("/")
    if not url:
        raise ValueError("base_url 为空")
    api_url = url + "/chat/completions"
    headers = {"Authorization": f"Bearer {provider.get('api_key') or ''}", "Content-Type": "application/json"}
    payload = {
        "model": provider.get("model") or "",
        "messages": messages,
        "temperature": 0.2,
        "max_tokens": 500,
        "response_format": {"type": "json_object"},
    }
    r = requests.post(api_url, headers=headers, json=payload, timeout=timeout)
    r.raise_for_status()
    content = r.json()["choices"][0]["message"]["content"]
    return _parse_json(content)


def _parse_json(content: str) -> dict:
    content = (content or "").strip()
    # 去掉可能的 ```json ... ``` 包裹
    if content.startswith("```"):
        content = content.strip("`")
        if content.lower().startswith("json"):
            content = content[4:]
        content = content.strip()
    try:
        return json.loads(content)
    except Exception:
        # 尝试截取第一个 { 到最后一个 }
        s, e = content.find("{"), content.rfind("}")
        if s >= 0 and e > s:
            try:
                return json.loads(content[s : e + 1])
            except Exception:
                pass
        return {}


def _fallback_result() -> dict:
    return {"category": "other", "summary": "", "todo": "", "deadline": ""}


def analyze(ai_cfg: dict, subject: str, body_text: str, log=None) -> dict:
    """对一封邮件做 AI 分析；全部 provider 失败时返回空结果。"""
    if not ai_cfg.get("enabled"):
        return _fallback_result()
    providers = ai_cfg.get("providers") or []
    if not providers:
        return _fallback_result()
    timeout = int(ai_cfg.get("timeout") or 120)
    language = (ai_cfg.get("language") or "中文").strip() or "中文"
    preferences = (ai_cfg.get("preferences") or "").strip()
    body = (body_text or "")[:3000]
    user_msg = f"语言: {language}\n"
    if preferences:
        user_msg += f"用户偏好: {preferences}\n"
    user_msg += f"主题: {subject}\n正文:\n{body}"
    messages = [{"role": "system", "content": _SYSTEM_PROMPT}, {"role": "user", "content": user_msg}]
    errs = []
    for p in providers:
        try:
            res = _call_provider(p, messages, timeout)
            if res:
                res.setdefault("category", "other")
                if res["category"] not in CATEGORIES:
                    res["category"] = "other"
                res.setdefault("summary", "")
                res.setdefault("todo", "")
                res.setdefault("deadline", "")
                return res
        except Exception as e:
            errs.append(f"{p.get('name') or '?'}: {e}")
            if log:
                log(f"AI provider 失败（{p.get('name')}）: {e}")
    if log:
        log(f"所有 AI provider 均失败: {'; '.join(errs)[:300]}")
    return _fallback_result()


def quick_test(ai_cfg: dict, log=None) -> tuple:
    """测试 AI 配置：返回 (ok, result_or_error)。"""
    try:
        res = analyze(ai_cfg, "测试：明天下午三点开周会", "请确认参会并准备上周数据。", log=log)
        if not res.get("category") and not res.get("summary"):
            return False, "AI 未返回有效结果，请检查 Base URL / Model / API Key"
        return True, res
    except Exception as e:
        return False, str(e)
