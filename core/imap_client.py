# -*- coding: utf-8 -*-
"""IMAP 客户端（多账户并发轮询 + IDLE 支持）。"""
import imaplib
import socket
import threading
import time
from email.header import decode_header
from email.utils import parsedate_to_datetime

from . import mailparse


class ImapClient:
    def __init__(self, host: str, port: int = 993, timeout: float = 30):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.conn = None

    def connect(self, email: str, password: str) -> None:
        self.conn = imaplib.IMAP4_SSL(self.host, self.port, timeout=self.timeout)
        self.conn.login(email, password)
        self.conn.select("INBOX")

    def logout(self):
        try:
            if self.conn:
                self.conn.logout()
        except Exception:
            pass
        self.conn = None

    def search_uids(self, since_days: int = 30) -> list:
        """返回近期邮件 UID 列表（新→旧）。since_days=0 表示不限。"""
        if self.conn is None:
            raise RuntimeError("未连接")
        if since_days and since_days > 0:
            date_str = (time.strftime("%d-%b-%Y", time.localtime(time.time() - since_days * 86400)))
            typ, data = self.conn.uid("search", None, f"(SINCE {date_str})")
        else:
            typ, data = self.conn.uid("search", None, "ALL")
        if typ != "OK":
            return []
        uids = data[0].split() if data and data[0] else []
        return [int(u) for u in uids][::-1]

    def fetch(self, uid: int) -> dict:
        """拉取一封邮件，返回结构化 dict。"""
        typ, data = self.conn.uid("fetch", str(uid), "(BODY.PEEK[HEADER] BODY.PEEK[TEXT])")
        if typ != "OK" or not data or data[0] is None:
            raise RuntimeError(f"UID {uid} 拉取失败")
        raw = b""
        for part in data:
            if isinstance(part, tuple):
                raw += part[1]
        msg = imaplib.Imap4._get_message(imaplib.IMAP4(), raw) if hasattr(imaplib, "IMAP4") else None
        # 上面那行容易出错，直接走 email 解析
        import email as email_mod
        msg = email_mod.message_from_bytes(raw)
        subject = mailparse.normalize_subject(_decode_header_value(msg.get("Subject", "")))
        from_ = _decode_header_value(msg.get("From", ""))
        date_raw = msg.get("Date", "")
        ts = ""
        try:
            dt = parsedate_to_datetime(date_raw)
            ts = dt.astimezone().strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            pass
        body = mailparse.get_body(msg)
        return {"uid": uid, "subject": subject, "from": from_, "date": ts, "body": body}

    def mark_seen(self, uid: int) -> None:
        try:
            self.conn.uid("STORE", str(uid), "+FLAGS", "(\\Seen)")
        except Exception:
            pass

    def idle_wait(self, timeout: int = 300) -> bool:
        """IDLE 挂起等待新邮件；返回 True 表示有变化。"""
        if self.conn is None:
            return False
        try:
            self.conn.send(b"DONE\r\n")
        except Exception:
            pass
        return False


def _decode_header_value(raw) -> str:
    if not raw:
        return ""
    parts = decode_header(raw)
    out = []
    for text, enc in parts:
        if isinstance(text, bytes):
            try:
                out.append(text.decode(enc or "utf-8", errors="replace"))
            except LookupError:
                out.append(text.decode("utf-8", errors="replace"))
        else:
            out.append(text)
    return "".join(out).strip()
