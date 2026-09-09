# -*- coding: utf-8 -*-
"""运行状态（原子化落盘）。"""
import json
import os
import tempfile
import time


def _default_state() -> dict:
    return {
        "version": 2,
        "uid_watermarks": {},   # email -> {uid: ts} 最近处理过的 UID
        "last_cycle": "",
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }


def load(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return _default_state()
        st = _default_state()
        st.update({k: v for k, v in data.items() if k in st})
        return st
    except Exception:
        return _default_state()


def save(state: dict, path: str) -> None:
    """原子写入：先写临时文件再替换。"""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix="state_", suffix=".tmp", dir=os.path.dirname(path) or ".")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def watermark(state: dict, email: str, uid: int) -> None:
    wm = state.setdefault("uid_watermarks", {})
    bucket = wm.setdefault(email, {})
    bucket[str(uid)] = int(time.time())
    # 每账户最多保留 4000 条水位，避免无限膨胀
    if len(bucket) > 4000:
        for k in sorted(bucket, key=lambda x: bucket[x])[: len(bucket) - 4000]:
            bucket.pop(k, None)


def seen(state: dict, email: str, uid: int) -> bool:
    return str(uid) in (state.get("uid_watermarks") or {}).get(email, {})
