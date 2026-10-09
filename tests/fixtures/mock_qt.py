"""Qt 最小化替身 —— **默认不使用**。

⚠️【与提示词原稿的差异 · 此处需要重构 X】
原稿要求"把内联 Qt 替身换成 mock_qt fixture"。本项目**已经用真实 PySide6 +
QT_QPA_PLATFORM=offscreen 跑通 24 项 UI 断言**（见 test_ui_error.py）。
换成最小 Qt 替身意味着"测替身而不是测被测代码"，是**倒退**：

- 真实 Qt 能暴露信号槽时序、控件状态、布局等真实问题；
- 最小替身只会验证"我的替身被我调用了"，属假绿风险（原稿第五节自己列了"假绿"）。

因此本模块**保留但默认不启用**，仅在两种场景下使用：
1. 目标环境装不了 PySide6，但想跑通用例骨架（降级）；
2. 需要精确控制 Qt 回调时序（真实 Qt 难以稳定复现）。

默认路径：conftest 的 `qt_app` fixture 使用真实 PySide6 + offscreen。
"""

from __future__ import annotations

from typing import Optional

ENABLED = False   # 默认不启用；如需降级，显式置 True


class _SkipMixin:
    allow_skip: bool = True


class QApplication(_SkipMixin):
    _instance: Optional["QApplication"] = None

    def __init__(self, *args, **kwargs):
        QApplication._instance = self

    @staticmethod
    def instance():
        return QApplication._instance

    def exec(self):
        return 0


class QTimer(_SkipMixin):
    def __init__(self, *args, **kwargs):
        self._interval = 0
        self._callback = None

    def setInterval(self, ms: int):
        self._interval = ms

    def timeout(self):
        return self

    def connect(self, callback):
        self._callback = callback

    def start(self):
        pass

    def stop(self):
        pass
