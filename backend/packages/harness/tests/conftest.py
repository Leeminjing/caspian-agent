"""测试级全局隔离：把 CASPIAN_HOME 指向临时目录，避免 home 化的默认落盘污染真实 ~/.caspian。

设置时机：pytest 加载本 conftest 时即执行（早于任何测试模块 import），因此
caspian/runtime/home.py 与 path_utils（模块加载期解析 CASPIAN_HOME）都能拿到隔离值。
"""

import atexit
import os
import shutil
import tempfile

_cas_home = tempfile.mkdtemp(prefix="cas_test_home_")
os.environ["CASPIAN_HOME"] = _cas_home
atexit.register(shutil.rmtree, _cas_home, ignore_errors=True)
