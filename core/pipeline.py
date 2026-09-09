# -*- coding: utf-8 -*-
"""
处理编排（借鉴 mailpilot 的单轮扫描 + 水位线思想）：
每账户一轮：
  1. 取候选 UID（首轮=存量未读；之后=水位线之后的增量），跳过已推送的；
  2. 逐封：取信 → 关键词待办（始终保留） → AI 结构化分析（可选） →
     合并待办 → 记最近简报 → ntfy 推送 → 推进水位线；
  3. 首轮未读清空后建立“基线”，以后只做增量。
"""
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading
import time
from datetime import date, datetime, timedelta
from typing import Callable, Dict, List, Optional

from core import state as state_mod
from core import ai as ai_mod
from core import extract as extract_mod
from core import notifier as notify_mod
from core import recent as recent_mod
from core import tasksync as tasksync_mod
from core import todos as todos_mod
from core.config import expand_env_text, normalize_accounts
from core.imap_client import IMAPError, Mailbox


def _noop(*_a, **_k):
    pass


def _lookback_days(cfg: dict) -> int:
    """读取 monitor.lookback_days：新账户首轮只扫最近 N 天的未读；0/缺省=不限。"""
    try:
        n = int((cfg.get("monitor") or {}).get("lookback_days") or 0)
    except (TypeError, ValueError):
        return 0
    return max(n, 0)


def _mail_date_text(raw) -> str:
    """把邮件 Date 头解析为本地时间文本(YYYY-MM-DDTHH:MM:SS)；失败返回空串。"""
    try:
        from email.utils import parsedate_to_datetime
        dt = parsedate_to_datetime(str(raw or "").strip())
        if dt is None:
            return ""
        return dt.astimezone().replace(tzinfo=None).isoformat(timespec="seconds")
    except Exception:
        return ""


# ---------- 单账户一轮 ----------

def process_account(acct: dict, cfg: dict,
                    state_path: str, todos_path: str, recent_path: str,
                    log: Callable = None) -> Dict:
    log = log or _noop
    email = acct.get("email", "")
    mark_seen = bool(acct.get("mark_seen"))
    max_run = int((cfg.get("monitor") or {}).get("max_per_run") or 20)
    ai_cfg = cfg.get("ai") or {}
    ai_enabled = bool(ai_cfg.get("enabled")) and bool(ai_cfg.get("providers"))
    # 仅当有启用视觉的 provider 时才在解析阶段保留邮件图片（省内存/CPU）
    vision_on = bool(ai_enabled and any((p or {}).get("vision")
                                        for p in ai_cfg.get("providers") or []))
    stats = {"email": email, "ok": True, "processed": 0, "added": 0,
             "ai_ok": False, "pushed": 0, "skipped_cat": 0, "error": ""}

    acct_eff = dict(acct)
    acct_eff["password"] = expand_env_text(str(acct.get("password") or ""))

    first_batch = False  # 循环外默认值：IMAP 连接阶段异常时避免 UnboundLocalError
    try:
        with Mailbox(acct_eff, readonly=not mark_seen) as box:
            uidv = box.uidvalidity
            stored_uidv, last_uid = state_mod.get_watermark(state_path, email)
            baseline = state_mod.get_baseline(state_path, email)
            first_batch = not baseline  # 首轮存量（尚未建立基线）只静默消化，不逐封推送

            # uidvalidity 变化 = 邮箱被重建，重置后重新走首轮
            if stored_uidv and stored_uidv != uidv:
                state_mod.reset_account(state_path, email)
                baseline, last_uid, stored_uidv = False, 0, 0
                log(f"[{email}] UIDVALIDITY 变化，水位线已重置")

            candidates: List[int] = []
            if not baseline:
                # 首轮：只扫最近 lookback_days 天内的未读（可到设置里调整），
                # 避免把几个月/几年前的老邮件全翻出来生成待办和推送。
                lookback = _lookback_days(cfg)
                if lookback > 0:
                    since = date.today() - timedelta(days=lookback)
                    candidates = [u for u in box.search_unseen_since(last_uid, since)
                                  if not state_mod.was_pushed(state_path, email, u)]
                else:
                    candidates = [u for u in box.search_unseen_after(last_uid)
                                  if not state_mod.was_pushed(state_path, email, u)]
                if not candidates:
                    # 关键修复：基线水位线直接对齐邮箱当前最大 UID。
                    # 原逻辑在“没有未读”时把 last_uid=0 当成基线，之后增量扫描
                    # 会从 UID 0 开始把整箱历史邮件当作“新邮件”全部读一遍。
                    base_uid = max(int(box.max_uid()), int(last_uid))
                    state_mod.set_baseline_done(state_path, email, uidv, base_uid)
                    log(f"[{email}] 存量未读(时间窗内)已处理完，建立基线 last_uid={base_uid}")
            else:
                candidates = [u for u in box.search_after(last_uid)
                              if not state_mod.was_pushed(state_path, email, u)]

            if not candidates:
                return stats

            if len(candidates) > max_run:
                log(f"[{email}] 本轮待处理 {len(candidates)} 封，先处理最旧 {max_run} 封")
            skip_fetch = set()

            for uid in candidates[:max_run]:
                if uid in skip_fetch:
                    continue
                log(f"[{email}] 处理 uid={uid}")
                try:
                    mail = box.fetch(uid, want_images=vision_on)
                except IMAPError as e:
                    log(f"[{email}] ✗ uid={uid} 取信失败，跳过: {e}")
                    skip_fetch.add(uid)
                    continue

                subject = mail.get("subject") or "(无主题)"
                body = mail.get("body") or ""
                kw_todos = extract_mod.extract_todos(
                    subject, body, (cfg.get("todo") or {}).get("keywords") or [])

                analysis = None
                mode = "keyword"
                if ai_enabled:
                    analysis, err = ai_mod.analyze_mail(mail, ai_cfg, log=log)
                    if analysis is not None:
                        mode = "ai"
                        stats["ai_ok"] = True
                    else:
                        log(f"[{email}] ✗ AI 分析失败，回退关键词模式: {err[:160]}")

                # 待办来源（去重后入库）：
                # - AI 成功：只采用 AI 判定的“未来仍需本人处理”的 action_items；
                #   AI 判断邮件纯属提醒/通知/营销时会给空数组 → 不生成待办，仅手机提醒。
                # - AI 未启用或失败：退回到关键词提取兜底。
                seen_texts = set()
                todo_sources = []
                if analysis is not None:
                    src = analysis.action_items
                    ai_flag = True
                else:
                    src = kw_todos
                    ai_flag = False
                for it in src:
                    if it and it not in seen_texts:
                        seen_texts.add(it)
                        todo_sources.append((it, ai_flag))
                for text, is_ai in todo_sources:
                    if todos_mod.add(todos_path, text, email, subject,
                                     category=analysis.category if analysis else "",
                                     urgency=analysis.urgency if analysis else "",
                                     ai=is_ai):
                        stats["added"] += 1

                # 最近简报
                if analysis is not None:
                    summary = (analysis.summary or kw_todos[0]) if (analysis.summary or kw_todos) else "（无摘要）"
                    recent_mod.add(recent_path, email, subject, analysis.category,
                                   analysis.urgency, summary, analysis.key_points, mode,
                                   url=analysis.action_url, message_id=mail.get("message_id", ""),
                                   uid=uid, mail_time=_mail_date_text(mail.get("date")))
                else:
                    recent_mod.add(recent_path, email, subject, "", "",
                                   kw_todos[0] if kw_todos else "（关键词模式：本邮件含待办关键词）",
                                   kw_todos[:3], mode, url=(mail.get("links") or [""])[0],
                                   message_id=mail.get("message_id", ""), uid=uid,
                                   mail_time=_mail_date_text(mail.get("date")))

                # ntfy 推送：命中“跳过分类”不推；其余按紧急度定优先级
                push_needed = notify_mod.topic_configured(cfg)
                if first_batch and notify_mod.first_batch_quiet(cfg):
                    push_needed = False  # 首轮存量邮件：静默入库/简报，不逐封推送手机
                if analysis is not None and notify_mod.is_skipped_category(analysis.category, cfg):
                    stats["skipped_cat"] += 1
                    push_needed = False
                if push_needed:
                    try:
                        # 真实业务推送：邮件摘要正文统一 300 字符上限（notifier 默认截断），
                        # 请求超时 30 秒、网络异常已内部转为友好提示。
                        ok = notify_mod.publish(cfg, _mail_title(mail, analysis),
                                                _mail_body(mail, analysis, kw_todos, mode),
                                                priority=notify_mod.priority_of(analysis, cfg)
                                                if analysis else 3,
                                                click=analysis.action_url if analysis else "")
                        if ok:
                            stats["pushed"] += 1
                    except Exception as e:
                        log(f"[{email}] ✗ uid={uid} 推送失败（下轮重试，不丢信）: {e}")
                        break  # 不推进水位线 → 下轮重新处理并重推
                elif not push_needed and mode == "ai":
                    pass  # 未配置主题：静默，待办仍入库

                state_mod.mark_processed(state_path, email, uidv, uid)
                if mark_seen:
                    box.mark_seen(uid)
                stats["processed"] += 1
                log(f"[{email}] ✓ uid={uid} 处理完成（{mode}模式）")
    except IMAPError as e:
        stats["ok"] = False
        stats["error"] = str(e)
        log(f"[{email}] ✗ {e}")
    except Exception as e:
        stats["ok"] = False
        stats["error"] = str(e)
        log(f"[{email}] ✗ 未预期错误: {e}")

    # 未接任务软件时：新增了待办 → 把整份清单同步到手机（已接 Vikunja 则由 cycle 统一同步+提示）
    try:
        quiet_first = first_batch and notify_mod.first_batch_quiet(cfg)
        if stats["added"] and notify_mod.topic_configured(cfg) and not quiet_first \
                and not tasksync_mod.enabled(cfg):
            sync_todos_to_phone(cfg, todos_path, log=log)
    except Exception as e:
        log(f"[{email}] ✗ 待办同步推送失败: {e}")
    return stats


def _mail_title(mail: dict, analysis) -> str:
    cat = analysis.category if analysis is not None else "其他"
    subj = mail.get("subject") or "(无主题)"
    return f"{cat} · {subj}"[:120]


def _mail_body(mail: dict, analysis, kw_todos: List[str], mode: str) -> str:
    lines = []
    from_t = mail.get("from_text") or mail.get("from_addr") or ""
    if from_t:
        lines.append(f"发件人：{from_t}")
    if analysis is not None:
        if analysis.summary:
            lines.append(f"摘要：{analysis.summary}")
        for p in analysis.key_points[:5]:
            lines.append(f"- {p}")
        if analysis.verification_code:
            lines.append(f"验证码：{analysis.verification_code}")
        if analysis.suggested_action:
            lines.append(f"建议：{analysis.suggested_action}")
    else:
        lines.append("（AI 未启用或不可用，以下为按关键词提取的内容）")
        for t in kw_todos[:5]:
            lines.append(f"- {t}")
    links = mail.get("links") or []
    if links:
        first = next((x for x in links if "unsubscribe" not in x.lower()), links[0])
        lines.append(f"链接：{first}")
    return "\n".join(lines)


def sync_todos_to_phone(cfg: dict, todos_path: str, log: Callable = None) -> bool:
    """把当前待办清单推送到手机。"""
    log = log or _noop
    if not notify_mod.topic_configured(cfg):
        return False
    todos = todos_mod.load(todos_path)
    title, body = notify_mod.build_todo_snapshot(todos)
    try:
        notify_mod.publish(cfg, title, body, tags=["clipboard"], priority=3,
                           max_len=notify_mod.MESSAGE_FULL_LIMIT)
        log("[推送] 待办清单已同步到手机")
        return True
    except Exception as e:
        log(f"[推送] 待办同步失败: {e}")
        return False


# ---------- 监控调度（多账户并发轮询） ----------

# 单轮最多同时处理的账户数：控制 IMAP 连接 + AI 请求的峰值占用。
MAX_SCAN_PARALLEL = 4

class Monitor:
    """后台轮询调度器：多账户并发、配置热加载、支持手动触发立即检查。"""

    def __init__(self, cfg_path: str, state_path: str, todos_path: str,
                 recent_path: str, log: Callable = None):
        self.cfg_path = cfg_path
        self.state_path = state_path
        self.todos_path = todos_path
        self.recent_path = recent_path
        self.log = log or _noop
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.results: Dict[str, Dict] = {}
        self.last_cycle = ""
        self.running = False
        self.checking = False
        self._lock = threading.Lock()
        self._idle_threads: Dict[str, threading.Thread] = {}
        self._scan_locks: Dict[str, threading.Lock] = {}
        self._scan_slots = threading.BoundedSemaphore(MAX_SCAN_PARALLEL)  # 并发扫描上限，降低峰值占用
        self._sync_lock = threading.Lock()
        self._last_sync = 0.0
        self._last_notice = 0.0
        self._manual = False

    # ---------- 状态 ----------

    def status(self) -> Dict:
        with self._lock:
            return {
                "running": self.running,
                "checking": self.checking,
                "last_cycle": self.last_cycle,
                "accounts": dict(self.results),
            }

    def _update_result(self, key: str, val: Dict):
        with self._lock:
            self.results[key] = val

    # ---------- 一轮检查 ----------

    def check_once(self) -> None:
        from core.config import load_config
        try:
            cfg = load_config(self.cfg_path)
            accounts = normalize_accounts(cfg)
        except Exception as e:
            self.log(f"[监控] 配置读取失败: {e}")
            return
        if not accounts:
            with self._lock:
                self.checking = False
                self.last_cycle = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            return
        with self._lock:
            self.checking = True
        try:
            self.log(f"[监控] 开始一轮检查（{len(accounts)} 个账户）")
            workers = min(len(accounts), MAX_SCAN_PARALLEL)
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futs = {pool.submit(self._run_guarded, a.get("email", ""), a, cfg): a
                        for a in accounts}
                for fut in as_completed(futs):
                    acc = futs[fut]
                    try:
                        stats = fut.result()
                    except Exception as e:
                        stats = {"email": acc.get("email", ""), "ok": False,
                                 "processed": 0, "added": 0, "ai_ok": False,
                                 "pushed": 0, "skipped_cat": 0, "error": str(e)}
                        self._update_result(acc.get("email", ""), stats)
                    else:
                        if stats is not None:
                            self._update_result(acc.get("email", ""), stats)
            self._run_sync_if_enabled(cfg)
            self.log("[监控] 一轮检查完成")
        finally:
            with self._lock:
                self.checking = False
                self.last_cycle = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # ---------- 生命周期 ----------

    def _cfg_safe(self) -> Dict:
        from core.config import load_config
        try:
            return load_config(self.cfg_path)
        except Exception:
            return {}

    def _mon(self, cfg=None) -> dict:
        cfg = self._cfg_safe() if cfg is None else cfg
        return cfg.get("monitor") or {}

    def _use_idle(self, cfg=None) -> bool:
        return bool(self._mon(cfg).get("use_idle", True))

    def _account_present(self, email: str) -> bool:
        try:
            return any(a.get("email", "") == email
                       for a in normalize_accounts(self._cfg_safe()))
        except Exception:
            return False

    def _sync_workers(self, cfg: Dict) -> None:
        """按配置启停每个账户的 IDLE 常驻线程。"""
        wanted = {a["email"]: a for a in normalize_accounts(cfg) if a.get("enabled", True)}
        for email, acct in wanted.items():
            th = self._idle_threads.get(email)
            if th is None or not th.is_alive():
                t = threading.Thread(target=self._idle_worker, args=(acct,),
                                     daemon=True, name=f"Idle-{email}")
                self._idle_threads[email] = t
                t.start()
        for email in [e for e in self._idle_threads if e not in wanted]:
            self._idle_threads.pop(email, None)  # 旧线程检测到账户消失后自行退出

    def _run_guarded(self, email: str, acct: Dict, cfg: Dict):
        """执行一次单账户处理：同账户互斥锁 + 全局并发上限。

        返回处理统计 dict；若该账户此刻正有另一路在跑（IDLE 触发 / 轮询 / 手动
        立即检查撞车）则返回 None，避免同一批邮件被并发读两遍、重复生成待办。
        """
        if not email or acct is None:
            return None
        if not self._scan_slots.acquire(blocking=False):
            return None
        try:
            lock = self._scan_locks.setdefault(email, threading.Lock())
            if not lock.acquire(blocking=False):
                return None
            try:
                return process_account(acct, cfg, self.state_path,
                                       self.todos_path, self.recent_path, self.log)
            finally:
                lock.release()
        finally:
            self._scan_slots.release()

    def _scan_account(self, email: str) -> None:
        """对单个账户扫描一次（同账户不并发；全局限流，避免高占用）。"""
        try:
            cfg = self._cfg_safe()
            acct = next((a for a in normalize_accounts(cfg)
                         if a.get("email") == email), None)
            if acct is None:
                return
            stats = self._run_guarded(email, acct, cfg)
            if stats is None:
                return
            with self._lock:
                self.last_cycle = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            self._run_sync_if_enabled(cfg)
        except Exception as e:
            self.log(f"[{email}] 扫描异常: {e}")

    def _idle_worker(self, acct: Dict) -> None:
        email = acct.get("email", "")
        self.log(f"[IDLE] {email} 常驻监听已启动")
        first = True
        while self.running and self._account_present(email) and self._use_idle():
            if first:
                first = False
                self._scan_account(email)  # 启动兜底：先补一次遗漏
            cfg = self._cfg_safe()
            cur = next((a for a in normalize_accounts(cfg) if a.get("email") == email), None)
            if cur is None:
                break
            acct_eff = dict(cur)
            acct_eff["password"] = expand_env_text(str(cur.get("password") or ""))
            try:
                with Mailbox(acct_eff, readonly=True) as watcher:
                    if not watcher.supports_idle():
                        raise IMAPError("服务器不支持 IDLE")
                    self.log(f"[IDLE] {email} 已进入 IDLE（秒级实时）")
                    timeout = int(self._mon(cfg).get("idle_timeout") or 300)
                    watcher.idle_loop(timeout, lambda: self._scan_account(email))
            except IMAPError as e:
                msg = str(e)
                self.log(f"[IDLE] {email} ✗ {msg}")
                if "不支持" in msg or "拒绝" in msg:
                    self.log(f"[IDLE] {email} 该邮箱不支持 IDLE → 自动改用轮询")
                    self._poll_worker(acct_eff)
                    return
                time.sleep(3)
            except Exception as e:
                self.log(f"[IDLE] {email} 异常: {e}，3s 后重连")
                time.sleep(3)
        self.log(f"[IDLE] {email} 已退出")

    def _poll_worker(self, acct: Dict) -> None:
        """某邮箱不支持 IDLE 时的轮询兜底（独立于全局检查）。"""
        email = acct.get("email", "")
        self.log(f"[轮询] {email} 采用轮询模式")
        while self.running and self._account_present(email) and self._use_idle():
            self._scan_account(email)
            interval = int(self._mon().get("poll_interval") or 30)
            self._wake.wait(timeout=max(interval, 10))
            self._wake.clear()
        self.log(f"[轮询] {email} 已退出")

    def _run_sync_if_enabled(self, cfg: Dict) -> None:
        """真实任务后端启用时：双向同步 + 新待办 ntfy 提醒（≥15s 去抖，避免每封都调远端）。"""
        now = time.monotonic()
        if now - self._last_sync < 15 or not self._sync_lock.acquire(blocking=False):
            return
        try:
            if tasksync_mod.enabled(cfg):
                counts = tasksync_mod.sync_todos(cfg, self.todos_path, log=self.log)
                changed = counts["ok"] and (
                    counts["created"] or counts["adopted"] or counts["done_local"])
                if changed and notify_mod.topic_configured(cfg):
                    # 冷却：60 秒内不重复发同类通知，避免“点一下测试→循环推送”
                    now = time.monotonic()
                    if now - self._last_notice >= 60:
                        notify_mod.push_new_todo_notice(
                            cfg, created=counts["created"], adopted=counts["adopted"],
                            done_local=counts["done_local"],
                            project=counts.get("project", ""))
                        self._last_notice = now
        except Exception as e:
            self.log(f"[同步] ✗ 周期同步失败: {e}")
        finally:
            self._last_sync = time.monotonic()
            self._sync_lock.release()

    def _loop(self) -> None:
        while not self._stop.is_set():
            cfg = self._cfg_safe()
            if self._use_idle(cfg):
                self._sync_workers(cfg)
                if self._manual:
                    self._manual = False
                    self.check_once()  # 手动“立即检查”
                self._wake.wait(timeout=15)
                self._wake.clear()
            else:
                self._sync_workers({})
                self.check_once()
                interval = int(self._mon(cfg).get("poll_interval") or 30)
                self._wake.wait(timeout=max(interval, 10))
                self._wake.clear()

    def trigger(self) -> None:
        """配置保存/手动按钮后调用，立即触发一次检查。"""
        self._manual = True
        self._wake.set()

    def start(self) -> None:
        if self.running:
            return
        self.running = True
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="MailMonitor")
        self._thread.start()
        self.log("[监控] 监控线程已启动")

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        self.running = False


