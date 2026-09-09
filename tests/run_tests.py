# -*- coding: utf-8 -*-
"""测试驱动器：结果写入 _test_result.log（UTF-8），供脚本环境可靠读取。"""
import io
import os
import sys
import traceback
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
log_path = os.path.join(ROOT, "_test_result.log")
buf = io.StringIO()
try:
    suite = unittest.defaultTestLoader.discover(os.path.join(ROOT, "tests"))
    runner = unittest.TextTestRunner(stream=buf, verbosity=2)
    result = runner.run(suite)
    summary = "RUN_OK failures=%d errors=%d" % (len(result.failures), len(result.errors))
except Exception:
    summary = "RUN_EXCEPTION\n" + traceback.format_exc()
buf.write("\n" + summary)
with open(log_path, "w", encoding="utf-8") as f:
    f.write(buf.getvalue())
