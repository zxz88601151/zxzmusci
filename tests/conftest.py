"""全局 conftest —— 替身注入、markers、自动跳过规则、共享 fixture。

════════════════════════════════════════════════════════════════════════
⚠️ 关键顺序：替身必须在**测试模块 import 引擎之前**装好
════════════════════════════════════════════════════════════════════════
`sys.modules["miniaudio"]` 只在模块**首次 import** 时生效。测试模块是在 pytest 的
**收集期** import 引擎的，而 fixture 在**运行期**才执行 —— 所以
「在 fixture 里 patch sys.modules」（哪怕 session 作用域）**一律太晚**，
引擎已经绑定到真 miniaudio（或前一个套件的替身）了。

因此：本文件在**模块顶层**调用 `install()`。pytest 保证 conftest 先于测试模块加载。
这正是原稿第五节「翻车点 1」想解决的问题，但原稿的 session fixture 方案并不成立。
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
_FIXTURES = os.path.join(_HERE, "fixtures")
# 隔离依赖目录（PySide6 / pytest）+ fixtures 包：本机外层环境会剥离 PYTHONPATH
for _d in (os.environ.get("PYSIDE6_DIR"), _HERE, _FIXTURES, _SRC):
    if _d and os.path.isdir(_d) and _d not in sys.path:
        sys.path.insert(0, _d)

import pytest  # noqa: E402

from fixtures.mock_miniaudio import (  # noqa: E402
    BACKEND,
    CLOSES,
    EVENTS,
    FakeDevice,
    install,
    reset,
)

# ★★★ 必须在任何测试模块 import 引擎之前执行 ★★★
install()


# ============ Markers ============
def pytest_configure(config):
    config.addinivalue_line("markers", "slow: 耗时>1s 的测试（SKIP_SLOW=1 时跳过）")
    config.addinivalue_line("markers", "qt: 需要真实 Qt（PySide6 offscreen）的测试")
    config.addinivalue_line("markers", "engine: 纯引擎逻辑测试（不依赖 Qt）")
    config.addinivalue_line("markers", "regression: 回归断言（每轮必须跑）")


# ============ 自动跳过规则 ============
def pytest_collection_modifyitems(config, items):
    skip_slow = os.environ.get("SKIP_SLOW") == "1"
    try:
        import PySide6  # noqa: F401

        has_qt = True
    except ImportError:
        has_qt = False

    for item in items:
        if skip_slow and "slow" in item.keywords:
            item.add_marker(pytest.mark.skip(reason="SKIP_SLOW=1"))
        if "qt" in item.keywords and not has_qt:
            item.add_marker(pytest.mark.skip(reason="未安装 PySide6，跳过 Qt 测试"))


# ============ 逐例重置共享替身 ============
@pytest.fixture(autouse=True)
def _reset_backend_each_test():
    """每个用例前恢复替身后端默认值。

    【为什么必须】所有套件共用同一个替身（sys.modules 只认首次 import），
    BACKEND 也随之共享 —— 某个套件改了 duration/chunks，后面的用例就会串味。
    """
    reset()
    yield


# ============ 共享 fixture ============
@pytest.fixture
def audio_dir(tmp_path):
    """测试用临时目录（pytest 自动清理，避免 CI 磁盘爆满）。"""
    return tmp_path


@pytest.fixture
def tmp_audio(tmp_path):
    """生成临时音频文件路径（内容为空，解码由替身负责）。"""

    def _mk(name: str = "a.mp3") -> str:
        p = tmp_path / name
        p.write_bytes(b"")
        return str(p)

    return _mk


def _write_wav(path, pcm_bytes: bytes) -> str:
    import wave

    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(44100)
        wf.writeframes(pcm_bytes)
    return str(path)


@pytest.fixture
def sine_file(audio_dir):
    """440Hz 正弦波 WAV 路径（原稿接口）。"""
    from fixtures.audio_synthesizer import generate_sine

    return _write_wav(audio_dir / "test_sine_440.wav",
                      generate_sine(freq=440, duration_ms=1000, sample_rate=44100))


@pytest.fixture
def noise_file(audio_dir):
    """白噪声 WAV 路径（原稿接口）。"""
    from fixtures.audio_synthesizer import generate_white_noise

    return _write_wav(audio_dir / "test_noise.wav",
                      generate_white_noise(duration_ms=1000, sample_rate=44100))


@pytest.fixture
def engine():
    """配置好的引擎实例；退出时自动 shutdown（防止句柄泄漏）。"""
    from fixtures.engine_factory import create_engine

    eng = create_engine()
    yield eng
    try:
        eng.shutdown()
    except Exception:  # noqa: BLE001
        pass


@pytest.fixture
def mock_device():
    """独立的替身设备实例。"""
    device = FakeDevice()
    yield device
    device.stop()


@pytest.fixture
def miniaudio_stub():
    """替身后端句柄（供原生 pytest 用例配置 BACKEND）。"""
    reset()
    return BACKEND


@pytest.fixture(scope="session")
def qt_app():
    """会话级 QApplication（真实 PySide6 + offscreen）。未装 PySide6 时跳过。

    【D3 修复】平台初始化失败（Linux headless 缺 libEGL/libxkbcommon 等）
    一律降级为 **skip**，绝不抛给收集期 —— 否则 `pytest -m qt` 整步会以
    "收集错误"失败，而不是优雅跳过。Qt 用例的降级路径必须比硬错误更常见。
    """
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication

    try:
        app = QApplication.instance() or QApplication([])
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"无法初始化 QApplication（Qt 平台插件不可用）：{exc}")
    return app


# ============ 纯辅助（供原生用例 import）============
def sine(frames: int, freq: float = 440.0, amp: int = 20000, rate: int = 44100):
    from fixtures.audio_synthesizer import sine_frames

    return sine_frames(frames, freq=freq, amp=amp, rate=rate)


def const(frames: int, val: int = 10000):
    from fixtures.audio_synthesizer import const_frames

    return const_frames(frames, val=val)


def rms(chunk) -> float:
    if len(chunk) == 0:
        return 0.0
    return math.sqrt(sum(s * s for s in chunk) / len(chunk))


def settle(engine, limit: int = 900) -> bool:
    """严格 settle：耗尽 limit 仍未收敛则抛 AssertionError（D2 修复）。"""
    from fixtures.engine_factory import settle as _s

    return _s(engine, limit=limit)


def is_exhausted(gen) -> bool:
    from fixtures.engine_factory import is_exhausted as _e

    return _e(gen)


__all__ = ["BACKEND", "EVENTS", "CLOSES", "configure", "install", "reset"]
