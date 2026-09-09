# -*- coding: utf-8 -*-
"""
Vikunja 部署公共工具函数。

- wait_for_port：等待 TCP 端口就绪，并检测子进程存活；
- kill_stale_vikunja_processes：只清理「占用指定端口」的残留 vikunja-*.exe 进程，
  避免误杀其它目录/用途的实例。
"""
import socket
import time
import subprocess
import psutil
from typing import Optional

def wait_for_port(
    host: str,
    port: int,
    process: Optional[subprocess.Popen] = None,
    timeout_total: int = 30,
    check_interval: float = 0.5,
) -> bool:
    """
    等待指定TCP端口可连接，同时检测子进程是否存活。
    
    :param host: 目标主机，通常为 127.0.0.1
    :param port: 目标端口，如 3456
    :param process: 已启动的 Vikunja 子进程对象，用于存活检测
    :param timeout_total: 最大等待秒数，默认30秒
    :param check_interval: 每次探测间隔秒数
    :return: 端口连通返回 True，否则抛出异常
    :raises TimeoutError: 端口在超时时间内未就绪
    :raises RuntimeError: 子进程在等待期间意外退出
    """
    start_time = time.time()
    
    while time.time() - start_time < timeout_total:
        if process is not None and process.poll() is not None:
            exit_code = process.returncode
            raise RuntimeError(
                f"Vikunja 进程意外退出，退出码 {exit_code}。详见 logs/vikunja.log"
            )
        
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(1)
            result = sock.connect_ex((host, port))
            sock.close()
            if result == 0:
                return True
        except Exception:
            pass
        
        time.sleep(check_interval)
    
    raise TimeoutError(
        f"等待 {host}:{port} 端口就绪超时（{timeout_total}秒），Vikunja HTTP 服务未监听该端口。"
    )


def _pids_listening_on(port: int) -> set:
    """返回正监听指定 TCP 端口的进程 PID 集合。"""
    pids = set()
    try:
        for conn in psutil.net_connections(kind="inet"):
            try:
                if (conn.status == psutil.CONN_LISTEN and conn.laddr
                        and conn.laddr.port == port and conn.pid):
                    pids.add(conn.pid)
            except Exception:
                continue
    except Exception:
        pass
    return pids


def kill_stale_vikunja_processes(port: int = 3456):
    """
    只清理「占用指定端口」的残留 vikunja-*.exe 进程（如上次崩溃遗留的孤儿进程），
    避免误杀用户其它目录/其它端口上正常使用的 Vikunja 实例。
    """
    target_pids = _pids_listening_on(port)
    for proc in psutil.process_iter(["pid", "name"]):
        try:
            proc_name = (proc.info["name"] or "").lower()
            if (proc_name.startswith("vikunja-") and proc_name.endswith(".exe")
                    and proc.info["pid"] in target_pids):
                proc.kill()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
