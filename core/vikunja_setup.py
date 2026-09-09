#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本地 Vikunja 一键部署 / 状态 / 停止 / 外部访问地址（public URL）管理。

部署产物目录：{BASE_DIR}/vikunja_local
- vikunja.exe：官方 Windows 单文件二进制
- vikunja.db：SQLite 数据
- config.yml：写入的配置

用途：邮箱待办可同步进本地 Vikunja 项目管理（网页 /sync 设置启用）。
"""
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import zipfile
from urllib.request import urlopen

import yaml

# 本地部署产物目录名（相对 BASE_DIR）
LOCAL_DIR_NAME = "vikunja_local"
VIKUNJA_PORT = 3456
DEFAULT_DOWNLOAD = (
    "https://dl.vikunja.io/vikunja/{version}/vikunja_{version}_windows_amd64.zip"
)
SUPPORTED_VERSIONS = ["0.24.6", "0.24.5", "0.24.4"]

# 供网页轮询的最近一次部署结果
LAST_DEPLOY = {"ts": 0, "ok": False, "msg": ""}


def local_dir(base_dir: str) -> str:
    return os.path.join(base_dir, LOCAL_DIR_NAME)


def _log(log, msg):
    if log:
        try:
            log(msg)
        except Exception:
            pass


def _read_cfg(config_file: str) -> dict:
    try:
        with open(config_file, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except Exception:
        return {}


def _write_cfg(config_file: str, cfg: dict) -> bool:
    try:
        with open(config_file, "w", encoding="utf-8") as f:
            yaml.safe_dump(cfg, f, allow_unicode=True, sort_keys=False)
        return True
    except Exception:
        return False


def _exe_name(base_dir: str) -> str:
    return os.path.join(local_dir(base_dir), "vikunja.exe")


def _running(base_dir: str) -> bool:
    """检测本地 vikunja.exe 是否在运行。"""
    exe = _exe_name(base_dir)
    if not os.path.exists(exe):
        return False
    try:
        import psutil
        for p in psutil.process_iter(["name", "exe"]):
            try:
                if p.info.get("name") == "vikunja.exe":
                    return True
            except Exception:
                continue
    except Exception:
        pass
    return False


def _health_ok(base_dir: str) -> bool:
    """HTTP 探测本地 Vikunja 是否就绪。"""
    try:
        import requests
        r = requests.get(f"http://127.0.0.1:{VIKUNJA_PORT}/api/v1/info", timeout=3)
        return r.status_code == 200
    except Exception:
        return False


def _pid_of(base_dir: str):
    try:
        import psutil
        exe = _exe_name(base_dir)
        for p in psutil.process_iter(["pid", "name", "exe"]):
            try:
                if p.info.get("name") == "vikunja.exe" or \
                   (p.info.get("exe") and os.path.normcase(p.info["exe"]) == os.path.normcase(exe)):
                    return p.info["pid"]
            except Exception:
                continue
    except Exception:
        pass
    return None


def status(base_dir: str) -> dict:
    """网页 /api/vikunja/status。"""
    d = local_dir(base_dir)
    exe = _exe_name(base_dir)
    return {
        "installed": os.path.exists(exe),
        "running": _running(base_dir),
        "healthy": _health_ok(base_dir),
        "port": VIKUNJA_PORT,
        "pid": _pid_of(base_dir),
        "dir": d if os.path.isdir(d) else "",
        "last_deploy": dict(LAST_DEPLOY),
        "configured": bool(_configured_url(base_dir)),
    }


def _configured_url(base_dir: str) -> str:
    """从本地部署 config.yml 读取 public URL（若配置过）。"""
    p = os.path.join(local_dir(base_dir), "config.yml")
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        svc = data.get("service", {})
        return (svc.get("publicurl") or "").strip()
    except Exception:
        return ""


def effective_public_url(base_dir: str) -> str:
    """取外部访问地址：config.yml 的 publicurl 优先，否则本机地址。"""
    u = _configured_url(base_dir)
    if u:
        return u
    return f"http://127.0.0.1:{VIKUNJA_PORT}"


def stop(base_dir: str) -> str:
    """停止本地 vikunja 进程（仅限本工具拉起的）。"""
    pid = _pid_of(base_dir)
    if not pid:
        return "Vikunja 未在运行"
    try:
        subprocess.run(["taskkill", "/PID", str(pid), "/F"],
                       capture_output=True, timeout=15)
        return f"已停止 Vikunja（PID {pid}）"
    except Exception as e:
        return f"停止失败: {e}"


def stop_port_process(pid: int) -> tuple:
    """结束占用 3456 端口的外部进程（如旧目录残留的 vikunja.exe）。"""
    if pid <= 0:
        return False, "缺少 PID"
    try:
        subprocess.run(["taskkill", "/PID", str(pid), "/F"],
                       capture_output=True, timeout=15)
        return True, f"已结束进程 {pid}"
    except Exception as e:
        return False, f"结束失败: {e}"


def stop_if_owned(base_dir: str) -> None:
    """程序退出前，停止由本工具拉起的 vikunja.exe（避免孤儿进程占端口）。"""
    try:
        pid = _pid_of(base_dir)
        if pid:
            subprocess.run(["taskkill", "/PID", str(pid), "/F"],
                           capture_output=True, timeout=10)
    except Exception:
        pass


def _download(url: str, dest: str, log) -> bool:
    """下载并解压 vikunja zip（Windows amd64 单文件）。"""
    _log(log, f"下载 {url}")
    try:
        with urlopen(url, timeout=120) as r, open(dest, "wb") as f:
            shutil.copyfileobj(r, f)
        return True
    except Exception as e:
        _log(log, f"下载失败: {e}")
        return False


def _ensure_downloaded(base_dir: str, log) -> tuple:
    """确保 vikunja.exe 存在；不存在则下载。返回 (ok, msg)。"""
    exe = _exe_name(base_dir)
    if os.path.exists(exe):
        return True, "已存在"
    d = local_dir(base_dir)
    os.makedirs(d, exist_ok=True)
    for ver in SUPPORTED_VERSIONS:
        url = DEFAULT_DOWNLOAD.format(version=ver)
        zip_path = os.path.join(d, f"vikunja_{ver}.zip")
        if not _download(url, zip_path, log):
            continue
        try:
            with zipfile.ZipFile(zip_path) as z:
                names = z.namelist()
                exe_in = next((n for n in names if n.lower().endswith("vikunja.exe")), None)
                if not exe_in:
                    _log(log, f"压缩包内未找到 vikunja.exe（{ver}）")
                    continue
                with z.open(exe_in) as src, open(exe, "wb") as dst:
                    shutil.copyfileobj(src, dst)
            os.remove(zip_path)
            return True, f"已下载 v{ver}"
        except Exception as e:
            _log(log, f"解压失败（{ver}）: {e}")
            continue
    return False, "所有候选版本下载均失败"


def _write_config(base_dir: str) -> bool:
    """写本地 config.yml（SQLite + 服务端口 + 开放注册）。"""
    d = local_dir(base_dir)
    os.makedirs(d, exist_ok=True)
    cfg = {
        "service": {
            "publicurl": f"http://127.0.0.1:{VIKUNJA_PORT}",
            "timezone": "Asia/Shanghai",
        },
        "database": {"type": "sqlite", "path": os.path.join(d, "vikunja.db")},
        "server": {"host": "127.0.0.1", "port": VIKUNJA_PORT},
    }
    try:
        with open(os.path.join(d, "config.yml"), "w", encoding="utf-8") as f:
            yaml.safe_dump(cfg, f, allow_unicode=True, sort_keys=False)
        return True
    except Exception:
        return False


def deploy(base_dir: str, config_file: str, log=None) -> tuple:
    """一键部署：下载 → 写配置 → 启动 → 写回 sync 配置。返回 (ok, msg)。"""
    _log(log, "开始部署本地 Vikunja")
    if _health_ok(base_dir):
        msg = "Vikunja 已在运行且健康，无需重新部署"
        _log(log, msg)
        return True, msg
    ok, msg = _ensure_downloaded(base_dir, log)
    if not ok:
        return False, msg
    if not _write_config(base_dir):
        return False, "写入 config.yml 失败"
    if not _running(base_dir):
        exe = _exe_name(base_dir)
        try:
            subprocess.Popen([exe], cwd=local_dir(base_dir),
                             creationflags=subprocess.CREATE_NO_WINDOW)
            _log(log, f"已启动 {exe}")
        except Exception as e:
            return False, f"启动失败: {e}"
    # 等待就绪
    for _ in range(30):
        time.sleep(1)
        if _health_ok(base_dir):
            break
    if not _health_ok(base_dir):
        return False, "Vikunja 启动后未就绪（请查看 vikunja_local 目录日志）"
    # 写回全局配置 sync.vikunja
    cfg = _read_cfg(config_file)
    vk = (cfg.setdefault("sync", {}).setdefault("vikunja", {}))
    vk["url"] = f"http://127.0.0.1:{VIKUNJA_PORT}"
    vk.setdefault("username", "demo")
    vk.setdefault("password", "demo")
    if not _write_cfg(config_file, cfg):
        return False, "Vikunja 已启动，但写回配置失败"
    _log(log, "Vikunja 部署完成，sync 配置已写回")
    return True, "Vikunja 已就绪（本地部署完成，可到 设置→同步 里测试连接）"


def set_public_url(base_dir: str, url: str, log=None) -> tuple:
    """应用外部访问地址（写入 config.yml 的 service.publicurl）。"""
    url = (url or "").strip().rstrip("/")
    if not url.startswith("http://") and not url.startswith("https://"):
        return False, "地址需以 http:// 或 https:// 开头"
    p = os.path.join(local_dir(base_dir), "config.yml")
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except Exception:
        data = {}
    data.setdefault("service", {})["publicurl"] = url
    try:
        with open(p, "w", encoding="utf-8") as f:
            yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False)
    except Exception as e:
        return False, f"写入 config.yml 失败: {e}"
    _log(log, f"已设置外部访问地址: {url}")
    return True, f"已应用外部访问地址：{url}（重启 Vikunja 后生效）"


def auto_start_local(cfg: dict, base_dir: str, log=None) -> tuple:
    """程序启动时：若已装过本地 Vikunja 且配置指向本机，自动拉起。"""
    vk = (cfg.get("sync") or {}).get("vikunja") or {}
    url = (vk.get("url") or "").strip()
    if not url or url != f"http://127.0.0.1:{VIKUNJA_PORT}":
        return False, "未配置指向本机的 Vikunja"
    exe = _exe_name(base_dir)
    if not os.path.exists(exe):
        return False, "本地未安装 Vikunja（可到网页一键部署）"
    if _health_ok(base_dir):
        return True, "Vikunja 已在运行"
    if _running(base_dir):
        _log(log, "Vikunja 进程在但未就绪，等待…")
        for _ in range(15):
            time.sleep(1)
            if _health_ok(base_dir):
                return True, "Vikunja 已就绪"
        return False, "Vikunja 进程存在但未就绪"
    try:
        subprocess.Popen([exe], cwd=local_dir(base_dir),
                         creationflags=subprocess.CREATE_NO_WINDOW)
    except Exception as e:
        return False, f"自动启动失败: {e}"
    for _ in range(15):
        time.sleep(1)
        if _health_ok(base_dir):
            return True, "Vikunja 已自动启动"
    return False, "Vikunja 启动超时"


# （旧 status 重复定义已移除；生效的是上方带 configured 字段的 status）


# （旧版“注册 API”自动建号流程已废弃，实际使用上面基于 CLI 的 deploy 实现）


