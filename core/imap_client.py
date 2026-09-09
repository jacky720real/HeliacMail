# -*- coding: utf-8 -*-
"""
IMAP 客户端：只读连接、UID 增量扫描、取信、可选标记已读。
改进自原 imaplib 轮询：不再依赖 UNSEEN + 标已读 做去重，
改用 UID 水位线（借鉴 mailpilot）。
"""
import imaplib
import select
import time
from typing import Dict, List, Optional

from core.mailparse import parse_rfc822

CONNECT_TIMEOUT = 30


class IMAPError(Exception):
    pass


class Mailbox:
    def __init__(self, acct: dict, readonly: bool = True):
        self.acct = acct
        self.readonly = readonly
        self.conn: Optional[imaplib.IMAP4_SSL] = None
        self.uidvalidity: int = 0

    def connect(self) -> None:
        email = self.acct["email"]
        host = self.acct.get("imap_host") or "imap.gmail.com"
        port = int(self.acct.get("imap_port") or 993)
        pwd = self.acct.get("password") or ""
        try:
            conn = imaplib.IMAP4_SSL(host, port, timeout=CONNECT_TIMEOUT)
        except Exception as e:
            raise IMAPError(f"IMAP 连接失败 {host}:{port}: {e}") from e
        try:
            conn.login(email, pwd)
        except Exception as e:
            try:
                conn.shutdown()
            except Exception:
                pass
            raise IMAPError(f"登录失败（请确认使用授权码/应用专用密码）: {e}") from e
        try:
            typ, data = conn.select("INBOX", readonly=self.readonly)
            if typ != "OK":
                raise IMAPError(f"选择收件箱失败: {data}")
        except Exception as e:
            try:
                conn.shutdown()
            except Exception:
                pass
            raise IMAPError(f"选择收件箱失败: {e}") from e
        self.conn = conn
        try:
            r = conn.response("UIDVALIDITY")
            if r and r[0] == "OK" and r[1]:
                self.uidvalidity = int(r[1][0])
        except Exception:
            self.uidvalidity = 0

    def close(self) -> None:
        if self.conn is not None:
            try:
                self.conn.logout()
            except Exception:
                pass
            try:
                self.conn.shutdown()
            except Exception:
                pass
            self.conn = None

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *exc):
        self.close()

    # ---------- 底层 ----------

    def _uid_search(self, criteria: str) -> List[int]:
        try:
            typ, data = self.conn.uid("search", None, criteria)
        except Exception as e:
            raise IMAPError(f"UID 检索失败: {e}") from e
        if typ != "OK":
            return []
        nums: List[int] = []
        for chunk in data:
            if isinstance(chunk, bytes):
                nums.extend(int(x) for x in chunk.split() if x.isdigit())
        return sorted(set(nums))

    def search_unseen_after(self, last_uid: int) -> List[int]:
        """首轮基线：返回 UID > last_uid 的未读邮件（升序）。"""
        return [u for u in self._uid_search("UNSEEN") if u > last_uid]

    def search_unseen_since(self, last_uid: int, since_date) -> List[int]:
        """首轮基线（限时窗口）：只返回 UID > last_uid、且在 since_date 当天
        及之后收到（服务器内部日期）的未读邮件（升序）。
        用于“只看最近 N 天”，避免把多年老邮件全部翻出来。
        """
        dstr = since_date.strftime("%d-%b-%Y")
        return [u for u in self._uid_search(f"UNSEEN SINCE {dstr}") if u > last_uid]

    def max_uid(self) -> int:
        """当前邮箱最大 UID（无邮件时返回 0）。用于把基线水位线对齐到“现在”，
        从而跳过历史邮件。"""
        nums = self._uid_search("1:*")
        return nums[-1] if nums else 0

    def search_after(self, last_uid: int) -> List[int]:
        """增量：返回 UID 在 (last_uid, +∞) 的全部邮件（升序）。"""
        return self._uid_search(f"(UID {last_uid + 1}:*)")

    # ---------- 取信 ----------

    def fetch(self, uid: int, want_images: bool = True) -> dict:
        try:
            # BODY.PEEK[]：不触发 \\Seen，始终保持只读
            typ, data = self.conn.uid("fetch", str(uid), "(BODY.PEEK[])")
        except Exception as e:
            raise IMAPError(f"取信 uid={uid} 失败: {e}") from e
        if typ != "OK" or not data:
            raise IMAPError(f"取信 uid={uid} 无数据")
        raw = None
        for chunk in data:
            if isinstance(chunk, tuple) and chunk[1]:
                raw = chunk[1]
                break
        if raw is None:
            raise IMAPError(f"取信 uid={uid} 解析不到正文")
        m = parse_rfc822(raw, want_images=want_images)
        m["uid"] = uid
        return m

    def mark_seen(self, uid: int) -> None:
        if self.conn is None or self.readonly:
            return
        try:
            self.conn.uid("store", str(uid), "+FLAGS", r"(\Seen)")
        except Exception:
            pass

    # ---------- IMAP IDLE 实时 ----------

    def supports_idle(self) -> bool:
        """服务器 CAPABILITY 是否含 IDLE。"""
        try:
            typ, caps = self.conn.capability()
            blob = b" ".join(caps).upper() if caps else b""
            return b"IDLE" in blob
        except Exception:
            return False

    def _idle_done(self) -> None:
        try:
            self.conn.send(b"DONE\r\n")
            self.conn.readline()  # OK IDLE terminated
        except Exception:
            pass

    def idle_once(self, timeout: float) -> bool:
        """
        进入一次 IDLE：等待新邮件事件（返回 True）或超时（返回 False）。
        超时也照常结束 IDLE（用于周期性兜底扫描）。
        """
        conn = self.conn
        if conn is None:
            raise IMAPError("连接已关闭")
        try:
            conn.send(b"IDLE\r\n")
            line = conn.readline()
            if not line or b"+ idling" not in line.upper():
                raise IMAPError("服务器不支持/拒绝了 IDLE")
        except IMAPError:
            raise
        except Exception as e:
            raise IMAPError(f"进入 IDLE 失败: {e}") from e
        try:
            deadline = time.monotonic() + float(timeout)
            while time.monotonic() < deadline:
                remain = deadline - time.monotonic()
                r, _, _ = select.select([conn.sock], [], [], max(0.1, min(1.0, remain)))
                if r:
                    try:
                        resp = conn.readline()
                    except Exception:
                        raise IMAPError("IDLE 连接中断")
                    if not resp:
                        raise IMAPError("IDLE 连接被服务器关闭")
                    up = resp.upper()
                    if b"EXISTS" in up or b"RECENT" in up:
                        return True
            return False
        finally:
            self._idle_done()

    def idle_loop(self, timeout: float, on_update) -> None:
        """
        循环 IDLE：新邮件秒级触发 on_update()；超时也触发一次（兜底补漏）。
        连接异常向上抛出，由调用方重连。借鉴 mailpilot 的 IdleLoop。
        """
        while True:
            try:
                self.idle_once(timeout)
            except IMAPError:
                raise
            except Exception as e:
                raise IMAPError(f"IDLE 异常: {e}") from e
            on_update()
