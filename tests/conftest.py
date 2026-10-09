"""pytest 公共夹具：miniaudio 替身 + Qt offscreen + 音频辅助。

所有验证都跑在"无真声卡、无显示器"的环境里：
- Qt 强制 offscreen 平台插件（不需要显示器即可构造 QApplication）
- miniaudio 由可配置的假后端替代（见 `_audio_stub.py`）

用法：
    python tests/run_pytest.py            # 一键跑全部
    python tests/run_pytest.py -k f7      # 只跑 F7 相关
"""

from __future__ import annotations

import array
import math
import os
import sys

# Qt 无头运行：必须在导入 PySide6 之前设置
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.join(_HERE, "..", "src")
# 隔离依赖目录（PySide6 / pytest 等）优先注入：本机 PYTHONPATH 会被外层环境剥离
for _d in (os.environ.get("PYSIDE6_DIR"), _HERE, _SRC):
    if _d and os.path.isdir(_d) and _d not in sys.path:
        sys.path.insert(0, _d)

import pytest  # noqa: E402

from _audio_stub import (  # noqa: E402
    BACKEND,
    CLOSES,
    EVENTS,
    FakeDevice,
    configure,
    install,
    reset_backend,
)

__all__ = ["configure", "install", "reset_backend"]


@pytest.fixture(autouse=True)
def _reset_backend_each_test():
    """每个用例前把共享替身后端恢复默认。

    【为什么必须】所有套件共用同一个 miniaudio 替身（sys.modules 只认首次 import），
    于是 BACKEND 也共享——某个套件把 duration/chunks 改了，后面的用例就会串味。
    """
    reset_backend()
    yield


@pytest.fixture
def miniaudio_stub():
    """可配置的 miniaudio 替身后端（原生 pytest 用例用）。

    需要不同后端行为的旧套件自带替身（每个套件要的振幅/错误模式/事件日志不同）。
    """
    install()
    reset_backend()
    yield BACKEND


@pytest.fixture
def tmp_audio(tmp_path):
    """生成临时音频文件路径（内容为空，解码由替身负责）。"""

    def _mk(name: str = "a.mp3") -> str:
        p = tmp_path / name
        p.write_bytes(b"")
        return str(p)

    return _mk


@pytest.fixture(scope="session")
def qt_app():
    """会话级 QApplication（offscreen）。没装 PySide6 时跳过。"""
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    return app


# ---------- 供原生 pytest 用例复用的纯辅助 ----------
def sine(frames: int, freq: float = 440.0, amp: int = 20000, rate: int = 44100):
    out = array.array("h")
    for i in range(frames):
        v = int(amp * math.sin(2 * math.pi * freq * i / rate))
        out.append(v)
        out.append(v)
    return out


def const(frames: int, val: int = 10000):
    return array.array("h", [val, val] * frames)


def rms(chunk) -> float:
    if len(chunk) == 0:
        return 0.0
    return math.sqrt(sum(s * s for s in chunk) / len(chunk))


def settle(engine, limit: int = 900) -> None:
    """把淡出推到底并让 poll() 收尾（模拟设备持续拉流 + UI 定时 poll）。

    【F7 语义变更】stop()/pause() 非阻塞后，"停止完成"发生在淡出走完 + poll() 之后。
    """
    for _ in range(limit):
        gen = engine._stream
        if gen is not None:
            try:
                next(gen)
            except StopIteration:
                pass
        engine.poll()
        if not engine.fade_pending and engine._pending_finalize is None:
            return


def is_exhausted(gen) -> bool:
    try:
        next(gen)
    except StopIteration:
        return True
    except Exception:  # noqa: BLE001
        return False
    return False
