"""test_ui_error.py —— UI 层验收（P1-1 错误统一 / V6 dB 读数 / C3 音量恢复）。

需要 PySide6。若未安装则**跳过**（打印 SKIP 并以 0 退出），不影响其余测试。

    python tests/test_ui_error.py
    PYSIDE6_DIR=<PySide6 安装目录> python tests/test_ui_error.py

无头运行（CI/本机无显示器）：自动设置 QT_QPA_PLATFORM=offscreen。
"""

from __future__ import annotations

import array
import logging
import os
import sys
import tempfile
import types

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.join(_HERE, "..", "src")

# 允许通过环境变量指定 PySide6 安装目录（例如 pip --target 的场景）
_extra = os.environ.get("PYSIDE6_DIR")
if _extra:
    sys.path.insert(0, _extra)

import pytest

try:
    import PySide6  # noqa: F401
except ImportError:  # pragma: no cover
    pytest.skip("未安装 PySide6（设置 PYSIDE6_DIR 或 pip install PySide6-Essentials）",
                allow_module_level=True)

# 替身 miniaudio（引擎模块导入需要）
import _audio_stub  # noqa: E402

# 共享替身后端：sys.modules["miniaudio"] 只在**首次 import 引擎**时生效，
# 因此所有套件必须用同一个替身（差异只体现在 configure() 的参数上），
# 否则后加载的套件会拿到先加载套件的后端。
_audio_stub.install()

pytestmark = pytest.mark.qt
BACKEND = _audio_stub.BACKEND
EVENTS = _audio_stub.EVENTS
CLOSES = _audio_stub.CLOSES
FakeDevice = _audio_stub.FakeDevice
sys.path.insert(0, _SRC)

from PySide6.QtWidgets import QApplication  # noqa: E402

app = QApplication([])
from player.engine.player import AudioError  # noqa: E402
from player.engine.states import ErrorCode  # noqa: E402
from player.ui import main_window as mw  # noqa: E402

_FAILS: list[str] = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (("  " + extra) if extra else ""))
    if not cond:
        _FAILS.append(name)


_recs: list[str] = []


class _H(logging.Handler):
    def emit(self, r):
        _recs.append(r.getMessage())


_lg = logging.getLogger("player")
_lg.setLevel(logging.DEBUG)
_lg.addHandler(_H())

# 先隔离 Settings 到临时目录，避免读写用户真实配置
import player.settings as _st0  # noqa: E402
import PySide6  # noqa: F401
import pytest as _pt
import player.settings as _st

_SP0 = os.path.join(tempfile.mkdtemp(), "settings.json")
mw.Settings = lambda: _st0.Settings(_SP0)

w = mw.MainWindow()
_calls = []
mw.MainWindow._show_error_dialog = lambda self, report: (_calls.append(report), False)[1]

_tmp = tempfile.mkdtemp()
_mp3 = os.path.join(_tmp, "a.mp3")
_m4a = os.path.join(_tmp, "a.m4a")
for _p in (_mp3, _m4a):
    with open(_p, "wb"):
        pass

_sel = {"path": ""}
mw.QFileDialog.getOpenFileName = staticmethod(lambda *a, **k: (_sel["path"], ""))


def case(name, code, trigger):
    _calls.clear()
    _recs.clear()
    trigger()
    one = len(_calls) == 1
    clean = one and "Traceback" not in _calls[0].text and "Error" not in _calls[0].text
    logged = any(f"code={code.value}" in m for m in _recs)
    check(f"[{name}] UI 只弹一次", one, f"弹窗={len(_calls)}")
    check(f"[{name}] 文案不露堆栈", clean, _calls[0].text if _calls else "无")
    check(f"[{name}] 日志含 code={code.value}", logged, str(_recs[-1:]))


def test_ui_error():
    print("── 错误统一（P1-1）──")
    _sel["path"] = os.path.join(_tmp, "missing.mp3")
    case("文件不存在", ErrorCode.IO_ERROR, w._on_open)
    _sel["path"] = _m4a
    case("格式不支持", ErrorCode.FORMAT_NOT_SUPPORTED, w._on_open)

    w.engine.load(_mp3)
    w.engine._ensure_device = lambda: (_ for _ in ()).throw(AudioError("busy", ErrorCode.DEVICE_BUSY))
    case("设备被占", ErrorCode.DEVICE_BUSY, w._on_play_pause)
    case("IO 中断", ErrorCode.IO_ERROR, lambda: w._on_engine_error(ErrorCode.IO_ERROR, "medium removed"))
    case("未知错误", ErrorCode.UNKNOWN, lambda: w._on_engine_error(ErrorCode.UNKNOWN, "boom"))

    _calls.clear()
    w._error_dialog_open = True
    w._on_error(ErrorCode.UNKNOWN, "dup")
    w._error_dialog_open = False
    check("弹窗打开期间不重复弹", len(_calls) == 0, f"弹窗={len(_calls)}")

    _retried = []
    mw.MainWindow._show_error_dialog = lambda self, report: (_calls.append(report), report.retryable)[1]
    w.engine.play = lambda: _retried.append(1)
    _calls.clear()
    w._on_error(ErrorCode.DEVICE_BUSY, "busy")
    check("可重试类触发重试", len(_retried) == 1, str(_retried))
    _retried.clear()
    w._on_error(ErrorCode.FILE_CORRUPT, "bad")
    check("不可恢复类不触发重试", len(_retried) == 0, str(_retried))

    print("── V6 音量 dB 读数 ──")
    w._on_volume_changed(0)
    check("V6 滑块 0 → -60.0 dB", w.vol_db.text().startswith("-60.0"), w.vol_db.text())
    w._on_volume_changed(50)
    check("V6 滑块 50 → -30.0 dB", w.vol_db.text().startswith("-30.0"), w.vol_db.text())
    w._on_volume_changed(100)
    check("V6 滑块 100 → 0.0 dB", w.vol_db.text().startswith("0.0"), w.vol_db.text())

    print("── C3 音量持久化 ──")

    _sp = os.path.join(tempfile.mkdtemp(), "settings.json")
    _sd = _st.Settings(_sp)
    _sd.volume = 0.37
    _sd.save()
    mw.Settings = lambda: _st.Settings(_sp)
    _w3 = mw.MainWindow()
    check("C3 启动恢复滑块到 37", _w3.vol.value() == 37, str(_w3.vol.value()))
    check("C3 恢复后 dB 读数一致(-37.8dB)", _w3.vol_db.text().startswith("-37.8"), _w3.vol_db.text())
    _w3.vol.setValue(62)
    _w3._persist_volume()
    check("C3 拖动后落盘 0.62", abs(_st.Settings(_sp).volume - 0.62) < 1e-9,
          str(_st.Settings(_sp).volume))

    # 汇总：任一断言失败则整个用例失败（失败清单会完整列出）
    assert not _FAILS, f"{len(_FAILS)} 项断言失败：" + "; ".join(_FAILS)
