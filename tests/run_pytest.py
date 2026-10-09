"""pytest 一键入口：注入隔离依赖目录后调用 pytest。

    python tests/run_pytest.py             # 全量
    python tests/run_pytest.py -k f7       # 只跑 F7 相关
    PYSIDE6_DIR=<dir> python tests/run_pytest.py

之所以需要入口脚本：本机外层环境会剥离 PYTHONPATH，
而 PySide6/pytest 装在隔离目录里，必须在进程内注入 sys.path。
"""

from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)

for _d in (os.environ.get("PYSIDE6_DIR"), _HERE):
    if _d and os.path.isdir(_d) and _d not in sys.path:
        sys.path.insert(0, _d)

try:
    import pytest
except ImportError:  # pragma: no cover
    raise SystemExit(
        "未找到 pytest。请先安装：\n"
        "  python -m pip install --target <dir> pytest\n"
        "  并设置 PYSIDE6_DIR=<dir>（或把 <dir> 加入 PYTHONPATH）"
    )

raise SystemExit(
    pytest.main(["-c", os.path.join(_ROOT, "pytest.ini"), _HERE, *sys.argv[1:]])
)
