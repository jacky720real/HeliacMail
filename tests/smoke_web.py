# -*- coding: utf-8 -*-
"""网络冒烟：启动临时 HTTP 服务并调用 3 个核心 API（不阻塞、自动退出）。"""
import os
import sys
import threading
import time
from http.server import ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import requests  # noqa: E402
import app  # noqa: E402  （导入 Handler/monitor，不执行 main）

PORT = 8097
httpd = ThreadingHTTPServer(("127.0.0.1", PORT), app.Handler)
threading.Thread(target=httpd.serve_forever, daemon=True).start()
ok = True
try:
    time.sleep(0.4)
    for path in ("/api/status", "/api/settings", "/api/todos", "/api/vikunja/status",
                 "/api/vikunja/public-url"):
        r = requests.get(f"http://127.0.0.1:{PORT}{path}", timeout=5)
        body = r.json()
        print(path, r.status_code, body.get("success"))
        ok = ok and r.status_code == 200 and body.get("success") is True
finally:
    httpd.shutdown()
with open(os.path.join(ROOT, "_smoke_web_ok.txt"), "w", encoding="utf-8") as f:
    f.write("WEB_OK" if ok else "WEB_FAIL")
print("WEB_OK" if ok else "WEB_FAIL")
