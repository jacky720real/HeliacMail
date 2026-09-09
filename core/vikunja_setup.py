# -*- coding: utf-8 -*-
"""
Vikunja 免 Docker —— Windows 原生一键部署（官方 exe 包）：
  1) 若本机 127.0.0.1:3456 已有可用 Vikunja HTTP 服务（预设账号可登录）→ 直接 API 接入复用；
  2) 否则从官方 dl.vikunja.io / GitHub Release 下载 Windows zip 并解压到 vikunja_local/
  3) 生成最小 config.yml（SQLite 本地库 + 端口 3456），以 --config 显式启动 vikunja.exe
     （无黑框，stdout/stderr 落 logs/vikunja.log）
  4) 登录管理账号，用官方 v2 API 创建「长效 API Token（tk_ 开头）」自动写入本工具配置
  5) 之后监控轮次直接把待办同步进去，手机 Vikunja App 连 http://电脑IP:3456

API 约定：
- 登录：POST /api/v1/login  →  JWT（短期，仅用于换取下面的长效 token）
- 建长效 Token（v2.6 起）：GET /api/v2/routes 取权限清单 →
    POST /api/v2/tokens {"title","expires_at","permissions":{分组:[权限...]}}
    （老版本 v0.23/0.24 才用 PUT /api/v1/tokens，只带 title+expires_at）
- 日常同步走 /api/v1/*（项目/任务 CRUD），Authorization: Bearer tk_xxx
"""
import os
import secrets
import subprocess
import time
import zipfile
from typing import Dict, Optional, Tuple

import requests

from core import config as config_mod
from core.vikunja_manager import kill_stale_vikunja_processes, wait_for_port

PORT = 3456
# 更名 HeliacMail 前的旧版本曾以旧项目名作为 Vikunja 默认登录账号（已弃用）。
# 现统一使用下面的 PRESET_USER：旧实例迁移兼容沿用 PRESET_PWD；
# 全新部署会随机生成密码并持久化到 config.yaml（见 _resolve_creds）。
PRESET_USER = "vikunja_admin"
PRESET_PWD = "vikunja_local_2024"
FALLBACK_TAG = "v2.6.0"
FALLBACK_NAME = "vikunja-v2.6.0-windows-4.0-386.exe-full.zip"
FALLBACK_URL = f"https://dl.vikunja.io/vikunja/{FALLBACK_TAG}/{FALLBACK_NAME}"
PREFERENCE = ["windows-4.0-amd64", "windows-amd64", "windows-4.0-386", "windows-386",
              "windows-4.0-arm64", "windows-arm64"]

# 进程内记录最近一次自动部署结果（供网页轮询判断成功/失败）
LAST_DEPLOY = {"ts": 0, "ok": False, "msg": ""}


def local_dir(base_dir: str) -> str:
    return os.path.join(base_dir, "vikunja_local")


def exe_path(base_dir: str) -> Optional[str]:
    root = local_dir(base_dir)
    if not os.path.isdir(root):
        return None
    best = None
    for dp, _, files in os.walk(root):
        for f in files:
            if f.lower().endswith(".exe"):
                if "vikunja" in f.lower():
                    return os.path.join(dp, f)
                if best is None:
                    best = os.path.join(dp, f)
    return best


def _log_path(base_dir: str) -> str:
    """Vikunja 子进程 stdout/stderr 统一落盘：项目本地 logs/vikunja.log（不再写 vikunja_local/vikunja.out）。"""
    return os.path.join(base_dir, "logs", "vikunja.log")


def _pid_path(base_dir: str) -> str:
    return os.path.join(local_dir(base_dir), "vikunja.pid")


def _owned_path(base_dir: str) -> str:
    return os.path.join(local_dir(base_dir), "owned.txt")


def base_url() -> str:
    return f"http://127.0.0.1:{PORT}"


def is_port_open(host: str = "127.0.0.1", port: int = PORT) -> bool:
    import socket
    try:
        with socket.create_connection((host, port), timeout=2):
            return True
    except OSError:
        return False


def _pid_on_port(port: int = PORT):
    """Windows：找出正监听指定端口（LISTENING）的进程 PID；其它平台/查不到返回 None。"""
    if os.name != "nt":
        return None
    try:
        out = subprocess.run(["netstat", "-ano", "-p", "tcp"],
                             capture_output=True, text=True, encoding="gbk",
                             errors="replace", timeout=15).stdout or ""
    except Exception:
        return None
    for line in out.splitlines():
        if "LISTENING" not in line or f":{port}" not in line:
            continue
        parts = line.split()
        if parts and parts[-1].isdigit():
            return int(parts[-1])
    return None


def _process_name(pid: int) -> str:
    """Windows：按 PID 查进程名（如 vikunja.exe）。"""
    if os.name != "nt" or not pid:
        return ""
    try:
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                             capture_output=True, text=True, encoding="gbk",
                             errors="replace", timeout=15).stdout or ""
    except Exception:
        return ""
    for ln in out.splitlines():
        ln = ln.strip()
        if ln:
            return ln.split('","')[0].lstrip('"').strip()
    return ""


def stop_port_process(pid: int):
    """结束正占用 3456 的进程（重新核对 PID 后才动手，防止误杀）。"""
    if not pid:
        return False, "缺少进程 PID"
    if os.name != "nt":
        return False, "当前系统不支持自动结束进程，请手动处理"
    if _pid_on_port() != int(pid):
        return False, "该进程已不在监听 3456（可能已退出），请刷新状态"
    try:
        r = subprocess.run(["taskkill", "/PID", str(int(pid)), "/F"],
                           capture_output=True, text=True, encoding="gbk",
                           errors="replace", timeout=15)
        if r.returncode == 0:
            return True, f"已结束占用进程 PID {int(pid)}，可重新一键部署"
        msg = (r.stdout or r.stderr or "").strip()
        return False, f"结束进程失败（可能需管理员权限）：{msg or '未知错误'}"
    except Exception as e:
        return False, f"结束进程异常：{e}"


def read_tail(path: str, n: int = 30) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
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


# ---------- 下载 / 启动 / 停止 ----------

def _latest_asset() -> Tuple[str, str, str]:
    """从 GitHub Release 找最新 Windows 包，失败用硬编码兜底。"""
    try:
        r = requests.get(
            "https://api.github.com/repos/go-vikunja/vikunja/releases/latest",
            timeout=15)
        r.raise_for_status()
        tag = r.json().get("tag_name", "")
        candidates = []
        for a in r.json().get("assets", []):
            name = a.get("name") or ""
            low = name.lower()
            if "windows" in low and low.endswith(".zip") and "veans" not in low:
                candidates.append(name)
        if candidates:
            for want in PREFERENCE:
                hit = next((n for n in candidates if want in n.lower()), None)
                if hit:
                    return tag, hit, f"https://dl.vikunja.io/vikunja/{tag}/{hit}"
        return tag, FALLBACK_NAME, FALLBACK_URL
    except Exception:
        return FALLBACK_TAG, FALLBACK_NAME, FALLBACK_URL


def _download(url: str, dest_zip: str, log) -> None:
    log(f"下载 {url.split('/')[-1]} …（约 30~45MB，请耐心等待）")
    with requests.get(url, stream=True, timeout=300) as r:
        r.raise_for_status()
        total = int(r.headers.get("Content-Length") or 0)
        done = 0
        with open(dest_zip, "wb") as f:
            for chunk in r.iter_content(256 * 1024):
                f.write(chunk)
                done += len(chunk)
                if total:
                    log(f"下载中 {done * 100 // total}%")
    # 完整性校验说明：官方源（dl.vikunja.io / GitHub）未提供稳定可用的哈希/签名端点，
    # 这里只做「来源 URL + 字节数记录 + 大小下限校验」，该风险已接受（下载走 HTTPS 官方源）。
    # 如需更强保证，请自行到官方发布页核对校验值。
    size = os.path.getsize(dest_zip) if os.path.exists(dest_zip) else 0
    log(f"下载完成：来源 {url}，字节数 {size}")
    if size < 1_000_000:
        try:
            os.remove(dest_zip)
        except Exception:
            pass
        raise RuntimeError(f"下载文件过小（{size} 字节），疑似下载不完整，已删除")
    log("下载完成，解压中…")
    root = os.path.abspath(os.path.dirname(dest_zip))
    with zipfile.ZipFile(dest_zip) as z:
        # zip-slip 防护：拒绝把成员解压到解压目录之外
        for member in z.infolist():
            target = os.path.abspath(os.path.join(root, member.filename))
            if target != root and not target.startswith(root + os.sep):
                raise RuntimeError(f"压缩包包含非法路径：{member.filename}")
        z.extractall(root)
    os.remove(dest_zip)


def _public_url_file(root: str) -> str:
    return os.path.join(root, "public_url.txt")


def effective_public_url(root: str) -> str:
    """publicurl：优先读取用户填的“外部访问地址”，否则退回 127.0.0.1。"""
    try:
        with open(_public_url_file(root), "r", encoding="utf-8") as f:
            u = f.read().strip()
        if u:
            return u.rstrip("/")
    except Exception:
        pass
    return f"http://127.0.0.1:{PORT}"


def write_config(root: str) -> str:
    import yaml
    # Vikunja(Windows) 解析“相对”sqlite 路径时并不基于启动目录，而是落到
    # 默认的 %LOCALAPPDATA%\Vikunja\vikunja.db，导致库和账号放错地方。
    # 因此这里写“运行时按项目目录生成的绝对路径”。config.yml 由程序在运行时
    # 写入 vikunja_local/（已被 .gitignore 排除），不会进入源码仓库。
    db_path = os.path.join(root, "vikunja.db").replace("\\", "/")
    cfg = {
        "service": {"port": PORT, "publicurl": effective_public_url(root)},
        "database": {"type": "sqlite", "path": db_path},
    }
    path = os.path.join(root, "config.yml")
    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(cfg, f, allow_unicode=True, sort_keys=False)
    return path


def set_public_url(base_dir: str, url: str, log) -> Tuple[bool, str]:
    """设置手机要访问的“外部地址”，写入 publicurl 并重启实例使其生效。

    例：http://100.x.y.z:3456（Tailscale）、http://192.168.1.5:3456（同局域网）、
        http://你的域名:3456（云服务器/内网穿透/端口映射）。

    行为（Bug 修复）：
    - 先保存到本目录 public_url.txt 与 config.yml；
    - 若 3456 上正在运行的是“本目录托管实例”(owned) → 真正重启并等端口+HTTP 就绪，
      任何一步都有日志输出，绝不“点了没反应”；
    - 若运行中的实例非本目录托管（如直接复用了其它目录实例）→ 明确提示“无法自动重启”，
      不再谎报“已重启生效”；
    - 未运行 → 提示“已保存，下次部署/启动自动生效”。
    """
    url = (url or "").strip().rstrip("/")
    if not url.startswith(("http://", "https://")):
        log(f"[Vikunja] 外部访问地址格式错误：{url}")
        return False, "外部地址需以 http(s):// 开头"
    root = local_dir(base_dir)
    try:
        os.makedirs(root, exist_ok=True)
        with open(_public_url_file(root), "w", encoding="utf-8") as f:
            f.write(url)
    except Exception as e:
        log(f"[Vikunja] 写入 public_url.txt 失败：{e}")
        return False, f"写入失败：{e}"
    cfg_path = write_config(root)          # 同步更新本目录 config.yml 的 service.publicurl
    log(f"[Vikunja] 外部访问地址已写入 {cfg_path}：{url}")

    running = is_port_open()
    owned = os.path.exists(_owned_path(base_dir)) and os.path.exists(_pid_path(base_dir))
    if running and owned:
        log("[Vikunja] 检测到本目录托管实例在运行 → 重启以应用新的外部访问地址…")
        stop(base_dir)
        # 等端口真正释放，避免 start() 误判“已在运行”而不重启
        for _ in range(20):
            if not is_port_open():
                break
            time.sleep(0.3)
        ok, msg = start(base_dir, log)
        if not ok:
            return False, msg
        if not _http_ready(20):
            return False, ("服务已重启但 HTTP 未就绪，请查看日志尾部（logs/vikunja.log）：\n"
                           + read_tail(_log_path(base_dir), 40))
        log(f"[Vikunja] 实例已重启，外部访问地址生效：{url}")
        return True, f"外部访问地址已设为：{url}（已重启实例并生效）"
    if running:
        # 有服务在跑但不是本目录托管（复用/外部实例）
        log("[Vikunja] 正在运行的 Vikunja 非本目录托管，已保存地址但无法自动重启。"
            f"如需立即生效，请在该实例所在目录应用，或点「一键部署」由本目录接管（PID {_pid_on_port(PORT)}）。")
        return True, (f"已保存：{url}\n"
                      "注意：当前运行的 Vikunja 不是本目录托管的实例，无法自动重启生效。\n"
                      "请在运行它的那个目录里点「应用并重启生效」，或在当前目录点「一键部署」让它接管。")
    log(f"[Vikunja] 本地实例未运行，外部访问地址已保存，下次部署/启动自动生效：{url}")
    return True, f"已保存：{url}（本地实例当前未运行，下次部署/启动时自动生效）"


def is_running(base_dir: str) -> bool:
    """“本目录安装并管理”的本地 Vikunja 是否在运行。

    以 owned.txt/pid 文件为准判断归属，避免把其它目录/旧实例占用的 3456
    端口误认成自己的实例（纯净目录第一次打开应显示“未安装/可下载”）。
    """
    if not (os.path.exists(_owned_path(base_dir)) or os.path.exists(_pid_path(base_dir))):
        return False
    pid = _read_pid(base_dir)
    if pid:
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            pass
    return is_port_open()


def _http_ready(timeout: float) -> bool:
    """等待本地 Vikunja HTTP 真正可服务：收到任意 HTTP 应答即视为就绪。

    仅 TCP 端口开还不够（迁移/初始化可能仍在进行），此时登录会报连接被拒。
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            requests.get(base_url() + "/api/v1/login", timeout=2)
            return True
        except Exception:
            time.sleep(0.5)
    return False


def start(base_dir: str, log) -> Tuple[bool, str]:
    """确保本地 vikunja.exe 已下载并以「本地 config.yml」在后台运行。返回 (ok, msg)。

    关键点（Bug2 修复）：
    1) 启动前先 kill_stale_vikunja_processes() 查杀残留 vikunja-*.exe，避免端口/库冲突；
    2) 用 --config 显式指向项目本地 vikunja_local/config.yml，绝不读取系统用户目录配置；
    3) 子进程 stdout/stderr 重定向到 logs/vikunja.log；
    4) 用 wait_for_port 做 TCP 轮询（≤30s），其内部自带“进程崩溃”检测，
       不再用固定 sleep + 盲等端口，按“进程崩溃 / 端口就绪超时”分别返回提示。
    """
    # 部署/启动前清理本机残留的 vikunja-*.exe 进程（上次崩溃/被杀遗留的孤儿进程）
    log("启动前检查并清理残留 vikunja-*.exe 进程…")
    kill_stale_vikunja_processes()
    if is_port_open():
        if os.path.exists(_owned_path(base_dir)) or exe_path(base_dir) is not None:
            return True, f"Vikunja 已在运行：{base_url()}"
        return False, f"端口 {PORT} 已被其它程序占用，请更换端口或先停止占用程序"
    root = local_dir(base_dir)
    os.makedirs(root, exist_ok=True)
    exe = exe_path(base_dir)
    if exe is None:
        tag, name, url = _latest_asset()
        zip_path = os.path.join(root, name)
        try:
            _download(url, zip_path, log)
        except Exception as e:
            return False, f"下载失败：{e}"
        exe = exe_path(base_dir)
        if exe is None:
            return False, "解压后未找到 vikunja.exe"
    # 显式生成并使用本地绝对路径库的 config.yml（--config 跳过默认搜索路径）
    cfg_path = write_config(root)
    log(f"使用本地配置启动：{cfg_path}")
    log(f"启动 {os.path.basename(exe)} …")
    kwargs = {}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    # 子进程 stdout/stderr → 项目本地 logs/vikunja.log
    log_file = _log_path(base_dir)
    try:
        os.makedirs(os.path.dirname(log_file), exist_ok=True)
    except Exception:
        pass
    with open(log_file, "ab") as out:
        proc = subprocess.Popen([exe, "--config", cfg_path], cwd=root,
                                stdout=out, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, **kwargs)
    with open(_pid_path(base_dir), "w") as f:
        f.write(str(proc.pid))
    with open(_owned_path(base_dir), "w") as f:
        f.write("1")
    # TCP 端口轮询（wait_for_port 自带子进程存活检测），最多 30 秒
    try:
        wait_for_port("127.0.0.1", PORT, process=proc,
                      timeout_total=30, check_interval=0.5)
    except RuntimeError as e:
        # 进程在等待期间退出 = 程序崩溃
        log(f"[Vikunja] 进程崩溃：{e}")
        return False, (f"Vikunja 程序崩溃：{e}\n"
                       f"请查看日志尾部（logs/vikunja.log）：\n{read_tail(log_file, 40)}")
    except TimeoutError as e:
        # 进程仍在但端口迟迟不开 = 就绪超时
        log(f"[Vikunja] 端口就绪超时：{e}")
        return False, (f"Vikunja 端口就绪超时：{e}\n"
                       f"进程仍在但 HTTP 服务未监听 {PORT} 端口，"
                       f"请查看日志尾部（logs/vikunja.log）：\n{read_tail(log_file, 40)}")
    return True, f"Vikunja 启动成功（端口已就绪）：{base_url()}"


def stop(base_dir: str) -> str:
    """停止本工具管理的本地 Vikunja，并清理 pid/owned 标记文件。

    问题背景：Windows 上 os.kill 偶发失败/标记文件删除失败时，会残留
    vikunja.pid / owned.txt，导致 is_running() 与“CLI 建号后重启”判断
    错乱（表现为：CLI 已创建用户，但服务没起来，登录一直 10061 拒绝）。
    因此这里用 taskkill /T /F 双保险，且无论如何都删除标记文件。
    """
    pid = _read_pid(base_dir)
    errs = []
    if pid:
        try:
            import signal
            os.kill(pid, signal.SIGTERM)
            time.sleep(1.2)
        except Exception as e:
            errs.append(f"kill({pid}): {e}")
        if os.name == "nt":
            try:
                subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                               capture_output=True, text=True, encoding="gbk",
                               errors="replace", timeout=15)
            except Exception as e:
                errs.append(f"taskkill({pid}): {e}")
    # pid 文件缺失/读取失败但端口仍被占用时，再按端口兜底（仅本工具管理场景）
    if is_port_open():
        occ = _pid_on_port(PORT)
        if occ and pid is None:
            ok, m = stop_port_process(occ)
            if not ok:
                errs.append(m)
    # 无论进程是否已退出，都尽力删除标记文件，避免残留误导后续重启/状态判断
    for f in (_pid_path, _owned_path):
        try:
            if os.path.exists(f(base_dir)):
                os.remove(f(base_dir))
        except Exception as e:
            errs.append(f"删除标记文件失败: {e}")
    return ("已停止本地 Vikunja"
            + (f"（提示：{'; '.join(errs[:3])}）" if errs else ""))


def stop_if_owned(base_dir: str) -> None:
    if os.path.exists(_owned_path(base_dir)) and os.path.exists(_pid_path(base_dir)):
        try:
            stop(base_dir)
        except Exception:
            pass


def _restart_local_server(base_dir: str, log) -> Tuple[bool, str]:
    """CLI 改库后重启本目录管理的 HTTP 服务 —— 强制、不信任残留状态。

    修复要点（Bug2 复诊）：之前用 is_running() 判断“服务在跑就不用重启”，
    一旦 pid/owned 残留或 3456 被其它目录的实例占住，重启就会被跳过，
    于是出现“CLI 已创建用户、服务却没起来、登录 10061 拒绝”。

    这里无条件：
    1) 若 3456 被非本目录实例占用 → 先结束占用进程（本函数处于“部署本目录实例”语义）；
    2) 删除任何残留 pid/owned 标记；
    3) start()：下载(如缺)→按本地 config.yml 启动→wait_for_port 轮询端口(≤30s)；
    4) 再用 HTTP 探测确认真的能应答（403 也算应答：说明 HTTP 服务在工作）。
    """
    # 1) 端口被其它进程占用（含其它目录残留实例）→ 先接管端口
    if is_port_open():
        occ = _pid_on_port(PORT)
        if occ:
            own_pid = _read_pid(base_dir)
            if own_pid and occ == own_pid:
                log(f"[Vikunja] 停止本目录旧实例 PID {occ}…")
                stop(base_dir)
            else:
                log(f"[Vikunja] 端口 {PORT} 被非本目录实例 PID {occ} 占用，先结束占用进程…")
                ok, m = stop_port_process(occ)
                if not ok:
                    log(f"[Vikunja] 结束占用进程失败：{m}")
            time.sleep(1.0)
    # 2) 清除残留标记（可能指向已退出/已回收的 PID）
    for f in (_pid_path, _owned_path):
        try:
            if os.path.exists(f(base_dir)):
                os.remove(f(base_dir))
        except Exception:
            pass
    # 3) 启动并等待端口就绪（wait_for_port 内部带进程存活检测，≤30s）
    ok, msg = start(base_dir, log)
    if not ok:
        return False, msg
    # 4) 端口能连≠HTTP 就绪：再探测一次 HTTP 应答（403 也算 HTTP 在工作）
    if not _http_ready(20):
        return False, ("Vikunja 端口已监听但 HTTP 未应答（服务可能仍在初始化或已异常）。\n"
                       "请查看日志尾部（logs/vikunja.log）：\n"
                       + read_tail(_log_path(base_dir), 40))
    return True, msg


# ---------- API / 自动配置 ----------

def _api(method: str, path: str, payload=None, token: str = "") -> dict:
    url = base_url() + path
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    resp = requests.request(method, url, json=payload, headers=headers, timeout=15)
    if resp.status_code >= 400:
        raise RuntimeError(f"{method} {path} -> HTTP {resp.status_code}: {resp.text[:160]}")
    return resp.json() if resp.content else {}


# ---------- v2.6 兼容：用 CLI 建号/重置密码（注册 API 为 POST /register，405/409 场景较多） ----------

def _run_cli(base_dir: str, args, log) -> Tuple[int, str]:
    exe = exe_path(base_dir)
    if not exe:
        raise RuntimeError("未找到 vikunja.exe")
    # CLI 直接读写 sqlite，必须和 HTTP 服务指向同一份本地配置
    # （--config 显式指定 vikunja_local/config.yml，避免落到系统用户目录的默认库）
    cfg_path = write_config(local_dir(base_dir))
    kw = {"cwd": local_dir(base_dir), "capture_output": True,
          "timeout": 180}
    if os.name == "nt":
        kw["creationflags"] = subprocess.CREATE_NO_WINDOW
    try:
        r = subprocess.run([exe, "--config", cfg_path] + list(args), **kw)
    except Exception as e:
        raise RuntimeError(f"CLI 执行失败：{e}") from e
    # 中文 Windows 默认 GBK，Vikunja 输出为 UTF-8 → 一律按 UTF-8 容错解码
    out = ((r.stdout or b"") + (r.stderr or b"")).decode("utf-8", errors="replace").strip()
    if out:
        if len(out) > 1400:
            log("CLI: " + out[:600] + "\n……(中间省略)……\n" + out[-800:])
        else:
            log("CLI: " + out[:1400])
    return r.returncode, out


def _creds(base_dir: str, pref_user: str = "", pref_pwd: str = "") -> Tuple[str, str]:
    """决定写入/用于 Vikunja 的账号密码，并把最终账号写入 admin.txt 供查看。

    规则（Bug2 修复后的扩展）：
    - UI/config 填了自定义用户名 + ≥8 位密码 → 优先用用户自己的账号；
    - 只填了密码（用户名留空）→ 沿用默认用户名 PRESET_USER + 用户密码；
    - 都没填 → 使用固定默认预设 PRESET_USER / PRESET_PWD。
    全程不解析 CLI 输出/历史文件，密码由本函数直接作为参数交给 CLI 建号/重置。
    """
    user = (pref_user or "").strip() or PRESET_USER
    pwd = (pref_pwd or "").strip() or PRESET_PWD
    if (pref_user or "").strip() and len(pwd) < 8:
        raise RuntimeError("Vikunja 自定义密码至少 8 位，请重新填写")
    try:
        path = os.path.join(local_dir(base_dir), "admin.txt")
        with open(path, "w", encoding="utf-8") as f:
            f.write("Vikunja 本地账号（用于网页/手机登录）\n"
                    f"地址: {base_url()}\n用户名: {user}\n密码: {pwd}\n")
    except Exception:
        pass
    return user, pwd


def _api_login(user: str, pwd: str) -> str:
    data = _api("POST", "/api/v1/login", {"username": user, "password": pwd})
    token = data.get("token") or ""
    if not token:
        raise RuntimeError("登录成功但响应无 token")
    return token


def _to_long_token(token: str, log) -> str:
    """用登录 JWT 换取「长效 API Token」，失败依次降级，返回一个可用 Bearer 令牌。

    Vikunja v2.6 起 API Token 必须带非空 permissions（分组→权限名数组），
    过期时间 expires_at 也要给（传 RFC3339 字符串）。旧版 v1 才支持不带
    permissions 的 PUT /api/v1/tokens。全部失败时退回登录 JWT 并明确警告
    （JWT 会过期，本工具同步将间歇性 401，见 logs/app.log）。
    """
    import datetime
    far = (datetime.datetime.now(datetime.timezone.utc)
           + datetime.timedelta(days=365 * 20))          # 约 20 年
    exp = far.strftime("%Y-%m-%dT%H:%M:%SZ")
    headers = {"Accept": "application/json",
               "Content-Type": "application/json",
               "Authorization": f"Bearer {token}"}

    # 1) 先取「分组→权限」清单，构造全权限 permissions 映射
    perms: dict = {}
    try:
        r = requests.get(base_url() + "/api/v2/routes", headers=headers, timeout=15)
        r.raise_for_status()
        groups = r.json()
        if isinstance(groups, dict):
            for group, actions in groups.items():
                if isinstance(actions, dict):
                    keys = [k for k in actions if k]
                    if keys:
                        perms[group] = keys
            if not perms:
                log("GET /api/v2/routes 返回空权限清单，将尝试不带 permissions 建 token")
    except Exception as e:
        log(f"获取 /api/v2/routes 权限清单失败（{e}），将尝试旧版方式")

    # 2) v2 官方方式：POST /api/v2/tokens
    try:
        payload = {"title": "auto-sync-token", "expires_at": exp}
        if perms:
            payload["permissions"] = perms
        r = requests.post(base_url() + "/api/v2/tokens", headers=headers,
                          json=payload, timeout=15)
        if r.status_code in (200, 201):
            t = ((r.json() or {}).get("token") or "").strip()
            if t:
                log("已创建长效 API Token（v2 POST /api/v2/tokens，tk_ 开头，约 20 年）")
                return t
        log(f"POST /api/v2/tokens 不可用：HTTP {r.status_code} {r.text[:150]}")
    except Exception as e:
        log(f"POST /api/v2/tokens 请求失败：{e}")

    # 3) 旧版 v1（Vikunja v0.23/0.24 等）：PUT /api/v1/tokens
    candidates = [{"title": "auto-sync-token", "expires_at": exp, "permissions": perms},
                  {"title": "auto-sync-token", "expires_at": exp},
                  {"title": "auto-sync-token"}]
    for payload in candidates:
        try:
            r = requests.put(base_url() + "/api/v1/tokens", headers=headers,
                             json=payload, timeout=15)
            if r.status_code in (200, 201):
                data = r.json() or {}
                for k in ("token", "tokenstring"):
                    if data.get(k):
                        log("已创建长效 API Token（legacy PUT /api/v1/tokens）")
                        return str(data[k])
            log(f"PUT /api/v1/tokens {payload.get('expires_at') and '带expires_at' or '无expires_at'} 不可用："
                f"HTTP {r.status_code} {r.text[:150]}")
        except Exception as e:
            log(f"PUT /api/v1/tokens 请求失败：{e}")

    log("⚠ 未能创建长效 API Token，将使用登录 JWT（会过期，建议在 Vikunja 网页"
        "「设置 → API Tokens → 新建」手动创建 tk_ 令牌填入设置）")
    return token


def _provision_account(base_dir: str, log, user: Optional[str] = None,
                       pwd: Optional[str] = None) -> str:
    """确保本地库存在「固定预设账号」，并返回可用 Bearer 令牌。

    顺序（Bug2 修复，关键认知：CLI 直接读写 sqlite ≠ HTTP 服务已就绪）：
    ① 服务端口已由 start() 轮询就绪 → 先用预设账号直接 API 登录（成功即跳过 CLI）；
    ② 登录失败 → 停服（避免 SQLite 锁），用 CLI 操作同一份本地库：
       - 数据库没有预设用户 → user create 直接以参数指定账号密码创建；
       - 用户已存在 → 跳过创建，仅把密码重置为预设值；
    ③ 重启服务（内部再次 wait_for_port 轮询端口）→ 重新 API 登录。
    全程不解析 CLI 输出提取账号密码；错误按「进程崩溃 / 端口超时 / 账号密码」区分。
    """
    import re as _re
    user = user or PRESET_USER
    pwd = pwd or PRESET_PWD
    last_err: Optional[Exception] = None
    # ① 端口已就绪后 HTTP 可能还有瞬时波动，做少量短间隔登录尝试（非固定长睡判断就绪）
    for attempt in range(1, 4):
        try:
            token = _to_long_token(_api_login(user, pwd), log)
            log(f"API 登录成功（用户 {user}，无需 CLI 建号）")
            return token
        except Exception as e:
            last_err = e
            log(f"[Vikunja] 第 {attempt} 次 API 登录未通过：{str(e)[:140]}"
                "（将用 CLI 检查本地库预设用户）")
            time.sleep(0.8)

    # ② CLI 阶段：先停服再改库
    if is_running(base_dir):
        log("[Vikunja] 停止 HTTP 服务，由 CLI 直接操作本地 sqlite…")
        stop(base_dir)
        time.sleep(0.8)

    _rc, listing = _run_cli(base_dir, ["user", "list"], log)
    exists = (_rc == 0) and any(user.lower() in (ln or "").lower()
                                for ln in listing.splitlines())
    if not exists:
        log(f"[Vikunja] 本地库没有预设用户 {user} → CLI 创建（-u/-p 参数直接指定）")
        rc, out = _run_cli(base_dir, ["user", "create", "-u", user, "-p", pwd,
                                      "-e", f"{user}@example.com"], log)
        if rc != 0:
            raise RuntimeError(
                f"CLI 创建预设用户失败（exit={rc}）：{out[-400:]}\n"
                "请查看 logs/vikunja.log；如为重复创建报错，可删除 "
                "vikunja_local/vikunja.db 后重新一键部署。")
    else:
        log(f"[Vikunja] 预设用户 {user} 已存在 → 跳过创建，仅将密码重置为你设置的新密码")
        found_id = None
        for line in listing.splitlines():
            if user.lower() in (line or "").lower():
                nums = _re.findall(r"\b\d+\b", line)
                if nums:
                    found_id = nums[0]
                    break
        if found_id is None:
            raise RuntimeError(
                f"无法定位预设用户 {user} 的 ID。请手动到 http://127.0.0.1:3456 处理账号，"
                "或删除 vikunja_local/vikunja.db 后重新点一键部署。")
        _run_cli(base_dir, ["user", "reset-password", found_id,
                            "--direct", "-p", pwd], log)

    # ③ 重启本地 HTTP 服务（强制，不信任 is_running/pid 残留状态）
    ok, msg = _restart_local_server(base_dir, log)
    if not ok:
        # 已区分“进程崩溃 / 端口就绪超时 / HTTP 未应答”并引导查看 logs/vikunja.log
        raise RuntimeError(msg)

    # 重启后重新登录：端口轮询通过后仍留少量短间隔重试（非固定等待判就绪）
    last_err = None
    for _ in range(10):
        try:
            return _to_long_token(_api_login(user, pwd), log)
        except Exception as e:
            last_err = e
            time.sleep(0.8)
    # 到这里：服务在运行、端口通、用户存在/刚建好，却始终登录不了 → 账号/密码类错误
    raise RuntimeError(
        f"账号密码错误：Vikunja HTTP 服务已就绪，但使用固定预设账号登录失败"
        f"（用户 {user}，最后一次异常：{last_err}）。\n"
        "请核对 logs/vikunja.log；若 vikunja.db 是历史残留，可停止后删除 "
        "vikunja_local/vikunja.db 再重新点一键部署。")


def _preset_login_ok() -> bool:
    """本机 3456 是否已有「预设账号可登录」的旧实例（旧版本迁移兼容判断）。"""
    if not is_port_open():
        return False
    if not _http_ready(5):
        return False
    try:
        _api_login(PRESET_USER, PRESET_PWD)
        return True
    except Exception:
        return False


def _persist_creds(cfg_path: str, user: str, pwd: str, log) -> None:
    """把账号密码持久化写回 config.yaml 的 sync.vikunja.username/password。"""
    try:
        cfg = config_mod.load_config(cfg_path)
        sync_cfg = cfg.setdefault("sync", {})
        vk_cfg = sync_cfg.setdefault("vikunja", {})
        vk_cfg["username"] = user
        vk_cfg["password"] = pwd
        config_mod.save_config(cfg, cfg_path)
    except Exception as e:
        log(f"[Vikunja] 写入登录凭据到 config.yaml 失败：{e}")


def _resolve_creds(cfg_path: str, log) -> Tuple[str, str]:
    """决定本次部署生效的账号密码。

    规则：
    - config 里填了自定义用户名 + ≥8 位密码 → 用自定义账号；
    - 只填密码（用户名留空）→ 默认用户名 PRESET_USER + 自定义密码；
    - 都没填 → 若本机 3456 已有「预设账号可登录」的旧实例（旧版本迁移），沿用
      PRESET_PWD 并提示尽快改自定义密码；否则（全新部署）生成随机密码并持久化
      写回 config.yaml，保证重复部署密码一致。
    密码只来自这里或预设/随机值，绝不解析 CLI 输出/历史文件。
    """
    try:
        cfg = config_mod.load_config(cfg_path)
    except Exception:
        cfg = {}
    vk_cfg = ((cfg.get("sync") or {}).get("vikunja") or {})
    username = (vk_cfg.get("username") or "").strip()
    password = str(vk_cfg.get("password") or "").strip()
    if username:
        if len(password) < 8:
            log(f"[Vikunja] 检测到自定义账号「{username}」但密码不足 8 位或为空，"
                f"将改用默认预设账号（请至少填写 8 位密码）")
            return PRESET_USER, PRESET_PWD
        return username, password
    if len(password) >= 8:
        return PRESET_USER, password
    # 都没有：旧实例迁移兼容优先
    if _preset_login_ok():
        log("[Vikunja] 检测到本机 3456 已有用预设账号可登录的旧实例 → 沿用预设密码（迁移兼容）。"
            "请尽快在设置页填写自定义账号密码以替换预设密码。")
        return PRESET_USER, PRESET_PWD
    # 全新部署：生成随机密码并持久化，保证两次部署密码一致
    pwd = secrets.token_urlsafe(12)
    _persist_creds(cfg_path, PRESET_USER, pwd, log)
    log("[Vikunja] 未提供自定义账号密码 → 已生成随机登录密码并写入 config.yaml"
        "（见 vikunja_local/admin.txt）")
    return PRESET_USER, pwd


def _save_vikunja_config(cfg_path: str, token: str, base_dir: str, log,
                         user: str = PRESET_USER, pwd: str = PRESET_PWD) -> str:
    """把「已接入本机 Vikunja」状态写入 config.yaml / configured.ok，返回错误文本（空=成功）。"""
    try:
        cfg = config_mod.load_config(cfg_path)
        sync_cfg = cfg.setdefault("sync", {})
        sync_cfg["enabled"] = True
        sync_cfg["backend"] = "vikunja"
        vk_cfg = sync_cfg.setdefault("vikunja", {})
        vk_cfg["url"] = base_url()
        vk_cfg["token"] = token
        vk_cfg["username"] = user
        vk_cfg["password"] = pwd
        # 保留用户此前填写的项目/优先级等设置，不整段覆盖
        config_mod.save_config(cfg, cfg_path)
    except Exception as e:
        return f"写入配置失败：{e}"
    try:
        with open(os.path.join(local_dir(base_dir), "configured.ok"),
                  "w", encoding="utf-8") as f:
            f.write(str(int(time.time())))
    except Exception:
        pass
    _creds(base_dir, user, pwd)   # 写 admin.txt（实际生效的账号，供人工查看/登录）
    return ""


def _try_adopt_existing_service(base_dir: str, cfg_path: str, log,
                                user: str = PRESET_USER,
                                pwd: str = PRESET_PWD) -> Tuple[bool, str]:
    """若本机 127.0.0.1:3456 已有一个可用 Vikunja HTTP 服务且能用「期望的账号」
    （UI 填的自定义账号，或默认预设）登录 → 直接通过 API 接入复用。

    用户诉求：本地已有 Vikunja 服务时不必重新下载/重复部署，直接在交互层告知“已复用”，
    避免多目录重复下载造成文件堆积与端口/库互相打架。
    仅在「端口有 HTTP 应答 + 期望账号能登录」时才接入，否则返回 (False, "")，
    交给正常部署流程去按期望账号建号/重置。
    """
    if not is_port_open():
        return False, ""
    if not _http_ready(5):      # 端口通但 HTTP 不应答 → 不属于可复用的服务
        return False, ""
    try:
        token = _to_long_token(_api_login(user, pwd), log)
    except Exception:
        log(f"[Vikunja] 本机 3456 已有服务但账号 {user} 登录失败，无法直接复用，将本地部署接管")
        return False, ""
    err = _save_vikunja_config(cfg_path, token, base_dir, log, user, pwd)
    if err:
        return False, err
    log(f"[Vikunja] 检测到本机 3456 已有可用的 Vikunja 服务（账号 {user}）→ 直接接入复用，不再重复下载/部署")
    return True, ("✓ 检测到本机已有 Vikunja 服务在运行（http://127.0.0.1:3456），"
                  f"已用账号 {user} 直接接入复用，无需重复下载/部署。\n"
                  f"登录账号：{user}（凭据见 vikunja_local/admin.txt）\n"
                  "提示：若该服务来自其它目录的部署，请勿让两个目录同时托管它，"
                  "否则会互相清理端口/数据库。")


def deploy(base_dir: str, cfg_path: str, log) -> Tuple[bool, str]:
    """一键部署/接入本地 Vikunja。返回 (ok, 提示)。

    顺序：① 从 config.yaml 读取用户期望账号（自定义/默认预设）；
    ② 本机 3456 已有「该账号可登录」的服务 → 直接接入复用；
    ③ 否则清理残留/端口占用 → 下载(如缺)→启动→按期望账号登录/CLI 建号→写入配置。
    """
    # ① 确定本次要生效的账号：UI/config 自定义优先，否则默认预设
    try:
        user, pwd = _resolve_creds(cfg_path, log)
        log(f"[Vikunja] 本次部署使用的登录账号：{user}")
    except Exception as e:
        return False, f"账号配置失败：{e}"
    # ② 已有「期望账号可登录」的服务 → 直接接入（避免重复下载与多目录互相打架）
    adopted, ad_msg = _try_adopt_existing_service(base_dir, cfg_path, log, user, pwd)
    if adopted:
        return True, ad_msg
    # ③ 部署前查杀残留 vikunja-*.exe 进程，避免端口、数据库冲突
    log("[Vikunja] 部署前查杀残留 vikunja-*.exe 进程…")
    kill_stale_vikunja_processes()
    # ④ 端口若被旧实例/外部实例占用 → 接管端口（本目录旧实例用 stop；外部实例结束进程）
    if is_port_open():
        occ = _pid_on_port(PORT)
        if occ:
            own_pid = _read_pid(base_dir)
            if own_pid and occ == own_pid and os.path.exists(_owned_path(base_dir)):
                log("检测到本机实例使用旧配置，先停止并用最新配置重启…")
                stop(base_dir)
            else:
                log(f"[Vikunja] 端口 {PORT} 被外部实例(PID {occ})占用，先结束占用进程再接管…")
                ok, m = stop_port_process(occ)
                if not ok:
                    log(f"[Vikunja] 结束占用进程失败：{m}")
            time.sleep(1.2)
    # ⑤ 清理本目录可能残留的 pid/owned 标记（防止旧标记误判服务状态）
    for f in (_pid_path, _owned_path):
        try:
            if os.path.exists(f(base_dir)):
                os.remove(f(base_dir))
        except Exception:
            pass
    # ⑥ 下载(如缺)并启动本地实例（start 内部会 wait_for_port 轮询端口就绪）
    ok, msg = start(base_dir, log)
    if not ok:
        return False, msg
    # ⑦ 按期望账号登录/建号（自定义账号已存在则重置其密码，不存在则 CLI 直接创建）
    try:
        token = _provision_account(base_dir, log, user, pwd)
    except Exception as e:
        return False, msg + f"\n账号配置失败：{e}"
    # ⑧ 写入本工具配置（同步已启用 + 长效 token + 实际生效账号）
    err = _save_vikunja_config(cfg_path, token, base_dir, log, user, pwd)
    if err:
        return False, msg + f"\n{err}"
    admin = os.path.join(local_dir(base_dir), "admin.txt")
    return True, (msg + f"\n✓ 自动配置完成：账号 {user} 已创建/确认，永久 API Token（tk_ 开头）已写入，"
                  f"待办将同步到本机 Vikunja。\n登录账号：{user}（凭据见 {admin}）"
                  f"\n手机与电脑同网络：登录 http://局域网IP:{PORT}；"
                  f"不同网络：选上方「📶 手机怎么连」方案并设置外部访问地址。")


def lan_ips() -> list:
    """本机 IPv4 地址（含 Tailscale/ZeroTier 的 100.x / 10.x 虚拟网卡 IP），供手机连接参考。"""
    import socket
    cands = set()
    try:
        for res in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            cands.add(res[4][0])
    except Exception:
        pass
    try:
        for ip in socket.gethostbyname_ex(socket.gethostname())[2]:
            cands.add(ip)
    except Exception:
        pass
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            cands.add(s.getsockname()[0])
        finally:
            s.close()
    except Exception:
        pass
    return sorted(x for x in cands if not x.startswith("127."))


_TS_CACHE = {"ip": "", "at": 0.0}


def tailscale_ip() -> str:
    """读取本机 Tailscale 分配的 100.x 地址（60 秒缓存）。"""
    now = time.monotonic()
    if _TS_CACHE["ip"] and now - _TS_CACHE["at"] < 60:
        return _TS_CACHE["ip"]
    import re as _re
    import shutil
    found = ""
    # Tailscale Windows 安装后 CLI 不一定在 PATH → 显式探测常见安装路径
    candidates = []
    if shutil.which("tailscale"):
        candidates.append(shutil.which("tailscale"))
    for c in (r"C:\Program Files\Tailscale\tailscale.exe",
              r"C:\Program Files (x86)\Tailscale\tailscale.exe"):
        if os.path.isfile(c):
            candidates.append(c)
    for exe in candidates:
        try:
            p = subprocess.run([exe, "ip", "-4"], capture_output=True,
                               text=True, encoding="gbk", errors="replace",
                               timeout=10)
            if p.returncode == 0:
                for line in (p.stdout or "").splitlines():
                    ip = line.strip()
                    if ip.startswith("100."):
                        found = ip
                        break
        except Exception:
            continue
        if found:
            break
    if not found:
        try:
            p = subprocess.run(["ipconfig"], capture_output=True, text=True,
                               encoding="gbk", errors="replace", timeout=15)
            for line in (p.stdout or "").splitlines():
                m = _re.search(r"(\d{1,3}(?:\.\d{1,3}){3})", line)
                if m and m.group(1).startswith("100."):
                    found = m.group(1)
                    break
        except Exception:
            pass
    if found:
        _TS_CACHE.update({"ip": found, "at": time.monotonic()})
    return found


def status(base_dir: str) -> dict:
    """运行/安装状态（供网页展示）。"""
    cfg_ok = os.path.join(local_dir(base_dir), "configured.ok")
    configured = 0
    try:
        configured = int(os.path.getmtime(cfg_ok))
    except Exception:
        pass
    running = is_running(base_dir)
    installed = exe_path(base_dir) is not None
    port_open = is_port_open()
    port_busy = port_open and not running
    busy_pid, busy_name = None, ""
    if port_busy:
        busy_pid = _pid_on_port()
        busy_name = _process_name(busy_pid) if busy_pid else ""
    return {
        "running": running,                # 本目录实例在运行（owned/pid 归属）
        "installed": installed,            # 本目录已有 exe
        "port_open": port_open,
        "port_busy": port_busy,            # 别人占着 3456
        "busy_pid": busy_pid,              # 占用者 PID（用于提示/结束）
        "busy_name": busy_name,            # 占用者进程名，如 vikunja.exe
        "configured": configured,
        "url": base_url(),
        "pid": _read_pid(base_dir),
        "owned": os.path.exists(_owned_path(base_dir)),
        "dir": local_dir(base_dir),
        "public_url": effective_public_url(local_dir(base_dir)),
        "lan_ips": lan_ips(),
        "tailscale_ip": tailscale_ip(),
        "deploy_ts": LAST_DEPLOY["ts"],
        "deploy_ok": LAST_DEPLOY["ok"],
        "deploy_error": LAST_DEPLOY["msg"],
    }


def auto_start_local(cfg: dict, base_dir: str, log) -> bool:
    """
    已装过本地 Vikunja 时，程序启动自动拉起（免去每次手动点部署）。
    仅当 ①sync 启用 ②后端为 vikunja ③配置 URL 正是本机 127.0.0.1:3456 时生效，
    不会动你指向远程服务器的实例。
    """
    try:
        sy = cfg.get("sync") or {}
        vk = sy.get("vikunja") or {}
        if not sy.get("enabled") or sy.get("backend") != "vikunja":
            return False
        if (vk.get("url") or "").strip().rstrip("/") != base_url():
            return False
        if exe_path(base_dir) is None:
            return False
        if is_running(base_dir):
            if os.path.exists(_owned_path(base_dir)):
                log("[Vikunja] 重启本机实例以应用最新配置…")
                stop(base_dir)
                time.sleep(1.2)
            else:
                log("[Vikunja] 检测到外部实例运行中，跳过自动管理")
                return True
        ok, msg = start(base_dir, log)
        if ok:
            log(f"[Vikunja] 已自动启动本地实例：{msg}")
        else:
            log(f"[Vikunja] 自动启动失败：{msg}")
        return ok
    except Exception as e:
        log(f"[Vikunja] 自动启动异常：{e}")
        return False




# （旧 status 重复定义已移除；生效的是上方带 configured 字段的 status）


# （旧版“注册 API”自动建号流程已废弃，实际使用上面基于 CLI 的 deploy 实现）


