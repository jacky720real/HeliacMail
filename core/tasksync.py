# -*- coding: utf-8 -*-
"""
待办与"真正的任务软件"双向同步。当前实现 **Vikunja**（开源自托管）：
- 本地待办 → 自动在 Vikunja 项目里创建任务（紧急度映射为优先级）；
- 本地勾选完成 → 远端标记 done；
- 远端勾选完成 → 本地自动标记完成；
- 手机/网页手动新增的开放任务 → 自动同步回本地仪表板。

本模块调用长期保持兼容的 /api/v1 命名空间（Bearer tk_ 令牌；创建 token
见 core/vikunja_setup.py——v2.6 用 GET /api/v2/routes + POST /api/v2/tokens）。
更多后端（Todoist / Microsoft To Do）规划中：它们需要额外 OAuth 授权流程，
接入时将复用本模块的"本地↔远端"同步框架。
"""
import uuid
from datetime import datetime
from typing import Dict, List, Optional, Tuple

import requests

from core.config import expand_env_text
from core import todos as todos_mod


class VikunjaError(Exception):
    pass


class Vikunja:
    def __init__(self, url: str, token: str, timeout: int = 20):
        self.base = expand_env_text(str(url or "")).strip().rstrip("/") + "/api/v1"
        self.token = expand_env_text(str(token or "")).strip()
        self.timeout = timeout
        if not self.base.startswith("http"):
            raise VikunjaError("Vikunja URL 必须以 http(s):// 开头")

    def _headers(self) -> dict:
        h = {"Accept": "application/json", "Content-Type": "application/json"}
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        return h

    def _req(self, method: str, path: str, payload=None):
        url = f"{self.base}{path}"
        try:
            resp = requests.request(method, url, headers=self._headers(),
                                    json=payload, timeout=self.timeout)
        except requests.RequestException as e:
            raise VikunjaError(f"请求失败（请检查网络/地址）: {e}") from e
        if resp.status_code in (401, 403):
            raise VikunjaError(f"认证失败（HTTP {resp.status_code}）——请检查 API Token 是否正确")
        if resp.status_code >= 400:
            raise VikunjaError(f"Vikunja HTTP {resp.status_code}: {resp.text[:200]}")
        if resp.status_code == 204 or not resp.content:
            return None
        try:
            return resp.json()
        except Exception:
            return None

    @staticmethod
    def _unwrap_list(data) -> List[dict]:
        if isinstance(data, dict):      # v2 分页信封兼容
            data = data.get("items") or []
        return [x for x in (data or []) if isinstance(x, dict)]

    # ---------- 项目 ----------

    def projects(self) -> List[dict]:
        return self._unwrap_list(self._req("GET", "/projects?page=1&per_page=100"))

    def ensure_project(self, title: str, project_id=None) -> dict:
        """按 project_id 直接取；否则按标题查找；找不到则创建。"""
        if project_id:
            p = self._req("GET", f"/projects/{project_id}")
            if isinstance(p, dict) and p.get("id"):
                return p
        want = (title or "").strip()
        for p in self.projects():
            if project_id and str(p.get("id")) == str(project_id):
                return p
            if not project_id and (p.get("title") or "").strip() == want:
                return p
        created = self._req("PUT", "/projects", {"title": want or "邮箱待办"})
        if not isinstance(created, dict) or not created.get("id"):
            raise VikunjaError("自动创建项目失败（可能 Token 无创建权限）")
        return created

    def project_tasks(self, project_id) -> List[dict]:
        return self._unwrap_list(self._req("GET", f"/projects/{project_id}/tasks?page=1&per_page=100"))

    # ---------- 任务 ----------

    def create_task(self, project_id, title: str, description: str = "",
                    priority: int = 0) -> dict:
        payload = {"title": title, "description": description}
        if priority:
            payload["priority"] = int(priority)
        created = self._req("PUT", f"/projects/{project_id}/tasks", payload)
        if not isinstance(created, dict) or not created.get("id"):
            raise VikunjaError("创建任务失败（响应异常）")
        return created

    def set_done(self, task_id, done: bool) -> None:
        self._req("POST", f"/tasks/{task_id}", {"done": bool(done)})

    def delete_task(self, task_id) -> None:
        self._req("DELETE", f"/tasks/{task_id}")


# ---------- 配置辅助 ----------

def _scfg(cfg: dict) -> dict:
    return (cfg.get("sync") or {}).get("vikunja") or {}


def enabled(cfg: dict) -> bool:
    """是否启用了某个真实任务后端。"""
    return bool(cfg.get("sync") and cfg["sync"].get("enabled")
                and cfg["sync"].get("backend") == "vikunja"
                and _scfg(cfg).get("url", "").strip()
                and _scfg(cfg).get("token", "").strip())


def _priority_for(urgency: str, scfg: dict) -> int:
    if urgency == "高":
        return int(scfg.get("priority_high") or 5)
    if urgency == "低":
        return int(scfg.get("priority_low") or 1)
    return int(scfg.get("priority_med") or 3)


def _urgency_from_priority(p) -> str:
    try:
        p = int(p or 0)
    except (TypeError, ValueError):
        return "中"
    if p >= 4:
        return "高"
    if p <= 1:
        return "低"
    return "中"


def _description_for(t: dict) -> str:
    parts = []
    if t.get("category"):
        parts.append(f"分类: {t['category']}")
    if t.get("source_email"):
        parts.append(f"来源邮箱: {t['source_email']}")
    if t.get("source_subject"):
        parts.append(f"邮件主题: {t['source_subject']}")
    if t.get("ai"):
        parts.append("AI 提取")
    return " | ".join(parts)[:500]


# ---------- 同步主流程 ----------

def test_connection(cfg: dict) -> Tuple[bool, str]:
    """测试 Vikunja 连接与 Token 权限，返回 (ok, message)。"""
    scfg = _scfg(cfg)
    if not (cfg.get("sync") or {}).get("enabled"):
        return False, "请先在设置里启用【待办同步】"
    if not scfg.get("url") or not scfg.get("token"):
        return False, "请先填写 Vikunja 服务器 URL 与 API Token"
    try:
        v = Vikunja(scfg["url"], scfg["token"])
        projects = v.projects()
        title = (scfg.get("project_title") or "邮箱待办").strip()
        proj = None
        if scfg.get("project_id"):
            try:
                proj = v.ensure_project(title, scfg["project_id"])
            except Exception:
                proj = None
        if proj is None:
            proj = v.ensure_project(title, None)
        return True, f"连接成功 ✓ 项目「{proj.get('title') or title}」(id={proj.get('id')})，" \
                     f"共 {len(projects)} 个项目"
    except Exception as e:
        return False, str(e)


def sync_todos(cfg: dict, todos_path: str, log=None) -> Dict:
    """双向同步一轮。返回统计 dict。未启用/配置不全会安全跳过。

    本地表只在内存中改一次、最后统一原子落盘（此前每个新增/标记完成都整表
    写一次，任务多时会产生大量磁盘 I/O）。
    """
    if log is None:
        log = lambda *_: None
    counts = {"ok": False, "created": 0, "done_remote": 0, "done_local": 0,
              "adopted": 0, "error": ""}
    if not enabled(cfg):
        return counts
    scfg = _scfg(cfg)
    try:
        v = Vikunja(scfg["url"], scfg["token"])
        title = (scfg.get("project_title") or "邮箱待办").strip()
        proj = v.ensure_project(title, scfg.get("project_id") or None)
        pid = proj.get("id")
        remote_tasks = {str(t.get("id")): t for t in v.project_tasks(pid)}

        todos = todos_mod.load(todos_path)
        dirty = False

        # ① 本地 → 远端
        for t in todos:
            tid = t.get("id")
            rid = t.get("remote_id")
            if t.get("done"):
                if rid is not None:
                    rt = remote_tasks.get(str(rid))
                    if rt and not rt.get("done"):
                        v.set_done(rid, True)
                        counts["done_remote"] += 1
            elif rid is None:
                # 只推送"邮件生成"的开放待办（手机导入的本身就在远端，跳过）
                if t.get("source_email") and not str(t.get("source_email", "")).startswith("("):
                    priority = _priority_for(t.get("urgency", ""), scfg)
                    created = v.create_task(
                        pid, (t.get("text") or "")[:300],
                        _description_for(t), priority)
                    t["backend"] = "vikunja"
                    t["remote_id"] = created.get("id")
                    counts["created"] += 1
                    dirty = True
                    log(f"[同步] 已创建 Vikunja 任务: {(t.get('text') or '')[:40]}")

        # ② 远端 → 本地（勾选完成 / 手机手动新增）
        for rid, rt in remote_tasks.items():
            local = next((x for x in todos
                          if x.get("backend") == "vikunja"
                          and str(x.get("remote_id")) == str(rid)), None)
            if local:
                if rt.get("done") and not local.get("done"):
                    local["done"] = True
                    counts["done_local"] += 1
                    dirty = True
                    log(f"[同步] 远端已完成 → 本地标记: {(local.get('text') or '')[:40]}")
            elif scfg.get("adopt_remote") and not rt.get("done"):
                text = (rt.get("title") or "").strip()
                if not text:
                    continue
                text = text[:300]
                # 与 todos.add 的去重口径一致：同来源"(手机)"+同文本不重复收录
                dup = any((x.get("source_email") or "").lower() == "(手机)"
                          and x.get("text") == text for x in todos)
                if dup:
                    continue
                todos.insert(0, {
                    "id": uuid.uuid4().hex[:12],
                    "text": text,
                    "source_email": "(手机)",
                    "source_subject": (rt.get("description") or "")[:120],
                    "category": "",
                    "urgency": _urgency_from_priority(rt.get("priority")),
                    "ai": False,
                    "backend": "vikunja",
                    "remote_id": rid,
                    "created_at": datetime.now().isoformat(timespec="seconds"),
                    "done": False,
                })
                counts["adopted"] += 1
                dirty = True
                log(f"[同步] 已纳入手机新增任务: {text[:40]}")

        if dirty:
            todos_mod.save(todos_path, todos)

        counts["ok"] = True
        counts["project"] = (proj.get("title") or title)
    except Exception as e:
        counts["error"] = str(e)
        log(f"[同步] ✗ 失败: {e}")
    return counts


def delete_local(cfg: dict, todos_path: str, tid: str, log=None) -> str:
    """删除本地待办时同时删除远端任务。返回提示文本或空。"""
    if log is None:
        log = lambda *_: None
    t = next((x for x in todos_mod.load(todos_path) if x.get("id") == tid), None)
    if t is None:
        return "未找到该待办"
    if t.get("backend") == "vikunja" and t.get("remote_id") is not None and enabled(cfg):
        try:
            Vikunja(_scfg(cfg)["url"], _scfg(cfg)["token"]).delete_task(t["remote_id"])
        except Exception as e:
            log(f"[同步] 远端删除失败: {e}")
    todos_mod.remove(todos_path, tid)
    return ""

