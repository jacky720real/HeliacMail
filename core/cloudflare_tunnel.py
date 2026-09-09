# -*- coding: utf-8 -*-
"""
Cloudflare Tunnel（TryCloudflare quick tunnel）—— 在交互层「一键安装并部署」：
  1) 没装时自动下载官方 cloudflared-windows-amd64.exe（GitHub Release，约 60MB）；
  2) 后台运行：cloudflared tunnel --no-autoupdate --url http://127.0.0.1:3456
  3) 从日志解析免费域名 https://xxxx.trycloudflare.com 写入缓存，
     并把它作为 Vikunja 的 publicurl（登录回跳、手机 App 填的地址）。

说明/限制：
- 免费 quick tunnel 的域名在 cloudflared 每次重启后会变（每次启动都会自动重新应用）；
  长期固定地址请用「命名隧道 + 自有域名」（需要你在 Cloudflare 后台配置，本工具不代管）。
- 相比路由器端口映射/cpolar/ngrok：出站连到 Cloudflare 边缘、不暴露入站端口，
  传输全程 TLS，且不要求手机开 VPN——这是它相对 Tailscale 的主要体验优势。
"""
import os
import re
import subprocess
import time
from typing import Optional, Tuple

import requests

DIR_NAME = "cloudflared_local"
# GitHub release 的 “latest/download” 会 302 到具体版本
CF_DOWNLOAD_URL = ("https://github.com/cloudflare/cloudflared/releases/"
                   "latest/download/cloudflared-windows-amd64.exe")
_URL_RE = re.compile(
    r"https://[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.trycloudflare\.com", re.I)

LAST = {"ts": 0, "ok": False, "msg": ""}  # 最近一次启动结果，供网页轮询


def _dir(base_dir: str) -> str:
    return os.path.join(base_dir, DIR_NAME)


def _bin(base_dir: str) -> str:
    return os.path.join(_dir(base_dir), "cloudflared.exe")


def _pid_path(base_dir: str) -> str:
    return os.path.join(_dir(base_dir), "cloudflared.pid")


def _out_path(base_dir: str) -> str:
    return os.path.join(_dir(base_dir), "cloudflared.out")


def _url_path(base_dir: str) -> str:
    return os.path.join(_dir(base_dir), "public_url.txt")


def is_installed(base_dir: str) -> bool:
    return os.path.isfile(_bin(base_dir))


def read_tail(base_dir: str, n: int = 25) -> str:
    try:
        with open(_out_path(base_dir), "r", encoding="utf-8",
                  errors="replace") as f:
            lines = f.readlines()
        return "".join(lines[-n:])
    except Exception:
        return ""


def _read_pid(base_dir: str) -> Optional[int]:
    try:
        with open(_pid_path(base_dir), "r") as f:
            return int(f.read().strip())
    except Exception:
        return None


def is_running(base_dir: str) -> bool:
    pid = _read_pid(base_dir)
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def current_url(base_dir: str) -> str:
    try:
        with open(_url_path(base_dir), "r", encoding="utf-8") as f:
            return f.read().strip()
    except Exception:
        return ""


def install(base_dir: str, log) -> bool:
    """下载 cloudflared.exe（幂等）。"""
    if is_installed(base_dir):
        return True
    root = _dir(base_dir)
    os.makedirs(root, exist_ok=True)
    dest = _bin(base_dir)
    log("[Cloudflare] 下载 cloudflared.exe（约 60MB，来自 GitHub Release）…")
    try:
        r = requests.get(CF_DOWNLOAD_URL, stream=True, timeout=60,
                         allow_redirects=True)
        r.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in r.iter_content(1 << 18):
                if chunk:
                    f.write(chunk)
    except Exception:
        try:
            os.remove(dest)
        except Exception:
            pass
        return False
    if not is_installed(base_dir) or os.path.getsize(dest) < 5_000_000:
        try:
            os.remove(dest)
        except Exception:
            pass
        return False
    # 完整性校验说明：cloudflared 官方 GitHub Release 未提供稳定可用的哈希端点，
    # 这里保留文件大小下限校验（≥5MB）并记录来源 URL 与字节数，该风险已接受。
    log("[Cloudflare] 下载完成：来源 %s，字节数 %s（无官方哈希校验，风险已接受）"
        % (CF_DOWNLOAD_URL, os.path.getsize(dest)))
    log("[Cloudflare] 已安装：%s" % dest)
    return True


def start(base_dir: str, log, target: str = "http://127.0.0.1:3456") -> Tuple[bool, str]:
    """安装(如需)→启动 quick tunnel→解析 trycloudflare 地址→应用到 Vikunja publicurl。"""
    ok = install(base_dir, log)
    if not ok:
        return False, "cloudflared 下载失败，请检查网络后重试"
    if is_running(base_dir):
        url = current_url(base_dir)
        return True, (url or "cloudflared 已在运行，正在等待分配地址…")

    root = _dir(base_dir)
    os.makedirs(root, exist_ok=True)
    kwargs = {}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    with open(_out_path(base_dir), "ab") as out:
        try:
            proc = subprocess.Popen(
                [_bin(base_dir), "tunnel", "--no-autoupdate", "--url", target],
                cwd=root, stdout=out, stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL, **kwargs)
        except Exception as e:
            return False, f"启动 cloudflared 失败：{e}"
    with open(_pid_path(base_dir), "w") as f:
        f.write(str(proc.pid))

    url, deadline = "", time.monotonic() + 75
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            break
        m = _URL_RE.search(read_tail(base_dir, 60))
        if m:
            url = m.group(0)
            break
        time.sleep(1)
    if not url:
        try:
            proc.terminate()
        except Exception:
            pass
        return False, "未能在 75 秒内取得 trycloudflare 地址（网络/临时限额？），日志尾部：\n" \
                      + read_tail(base_dir, 8)[-1200:]

    with open(_url_path(base_dir), "w", encoding="utf-8") as f:
        f.write(url)
    LAST.update({"ts": int(time.time()), "ok": True, "msg": url})
    log(f"[Cloudflare] 隧道地址：{url}（正在应用到 Vikunja publicurl…）")
    try:
        from core import vikunja_setup as vk
        vk.set_public_url(base_dir, url, log)
    except Exception as e:
        log(f"[Cloudflare] 应用 publicurl 提示（可稍后手动补一次）：{e}")
    return True, url


def stop(base_dir: str, log=None) -> str:
    """停止隧道；若 Vikunja publicurl 正好指向本隧道地址则回退到 Tailscale/127，避免死链。"""
    if log is None:
        log = lambda *_: None
    old_url = current_url(base_dir)
    pid = _read_pid(base_dir)
    msg = "Cloudflare 隧道未在运行"
    if pid:
        try:
            import signal
            os.kill(pid, signal.SIGTERM)
            time.sleep(1.2)
            try:
                os.kill(pid, 0)
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
            msg = "已停止 Cloudflare 隧道"
        except Exception as e:
            msg = f"停止失败：{e}"
        try:
            os.remove(_pid_path(base_dir))
        except Exception:
            pass
    LAST.update({"ts": int(time.time()), "ok": False, "msg": msg})
    try:
        from core import vikunja_setup as vk
        root = vk.local_dir(base_dir)
        if old_url and vk.effective_public_url(root).lower() == old_url.lower():
            fallback = ""
            try:
                ts = vk.tailscale_ip()
                if ts:
                    fallback = f"http://{ts}:{vk.PORT}"
            except Exception:
                pass
            vk.set_public_url(base_dir, fallback or vk.base_url(), log)
    except Exception:
        pass
    return msg


def status(base_dir: str) -> dict:
    return {
        "running": is_running(base_dir),
        "installed": is_installed(base_dir),
        "url": current_url(base_dir),
        "pid": _read_pid(base_dir),
        "log_tail": read_tail(base_dir, 8),
        "deploy_ts": LAST["ts"],
        "deploy_ok": LAST["ok"],
        "deploy_error": LAST["msg"],
    }
