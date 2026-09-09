# -*- coding: utf-8 -*-
"""
去重状态：每个邮箱账户一条 UID 水位线 + 已推送 UID 列表（借鉴 mailpilot 水位线思想）。

- UID 通常单调递增；记录 last_uid，只处理 > last_uid 的邮件。
- uidvalidity 变化说明邮箱被重建，自动重置水位线。
- pushed 记录已成功推送的 UID，避免“处理中途崩溃导致整封重推”的重复推送。
- 全部原子落盘到 state.json。
"""
from datetime import datetime
from typing import List, Optional, Tuple

from core.storage import read_json, update_json

DEFAULT_STATE_PATH = "state.json"
MAX_PUSHED = 500


def _acct(data: dict, email: str) -> dict:
    data.setdefault("accounts", {})
    a = data["accounts"].setdefault(email, {})
    a.setdefault("uidvalidity", 0)
    a.setdefault("last_uid", 0)
    a.setdefault("baseline", False)  # False=首轮：只处理存量未读；True=增量水位线
    a.setdefault("pushed", [])
    return a


def get_watermark(path: str, email: str) -> Tuple[int, int]:
    # 只读：不要 update_json（它会在每次“读”时把整份文件重写一遍）
    data = read_json(path)
    a = _acct(data, email)
    return a["uidvalidity"], a["last_uid"]


def set_watermark(path: str, email: str, uidvalidity: int, last_uid: int) -> None:
    def _fn(d):
        a = _acct(d, email)
        a["uidvalidity"] = int(uidvalidity)
        a["last_uid"] = int(last_uid)
        d["updated_at"] = datetime.now().isoformat(timespec="seconds")
        return d
    update_json(path, _fn)


def mark_processed(path: str, email: str, uidvalidity: int, uid: int) -> None:
    """处理成功后一次落盘：推进水位线 + 记入已推送表（避免两次写盘）。"""
    def _fn(d):
        a = _acct(d, email)
        a["uidvalidity"] = int(uidvalidity)
        a["last_uid"] = int(uid)
        lst = [int(u) for u in a["pushed"]]
        if int(uid) not in lst:
            lst.append(int(uid))
            a["pushed"] = lst[-MAX_PUSHED:]
        d["updated_at"] = datetime.now().isoformat(timespec="seconds")
        return d
    update_json(path, _fn)


def reset_account(path: str, email: str) -> None:
    def _fn(d):
        d.setdefault("accounts", {})
        d["accounts"][email] = {"uidvalidity": 0, "last_uid": 0, "baseline": False, "pushed": []}
        return d
    update_json(path, _fn)


def get_baseline(path: str, email: str) -> bool:
    data = read_json(path)
    return bool(_acct(data, email).get("baseline", False))


def set_baseline_done(path: str, email: str, uidvalidity: int, last_uid: int) -> None:
    """首轮基线完成：跳过存量历史，水位线设到当前最大 UID。"""
    def _fn(d):
        a = _acct(d, email)
        a["uidvalidity"] = int(uidvalidity)
        a["last_uid"] = int(last_uid)
        a["baseline"] = True
        d["updated_at"] = datetime.now().isoformat(timespec="seconds")
        return d
    update_json(path, _fn)


def was_pushed(path: str, email: str, uid: int) -> bool:
    data = read_json(path)
    return int(uid) in set(int(u) for u in _acct(data, email)["pushed"])


def mark_pushed(path: str, email: str, uid: int) -> None:
    def _fn(d):
        a = _acct(d, email)
        lst = [int(u) for u in a["pushed"]]
        if int(uid) not in lst:
            lst.append(int(uid))
            a["pushed"] = lst[-MAX_PUSHED:]
        return d
    update_json(path, _fn)
