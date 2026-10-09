"""test_fixture_health.py —— Fixture 可靠性自检（F1~F6）。

定位：验证 `conftest.py` 提供的 fixture 生命周期、隔离性与默认值是否正确。
F 段若不可信，业务测试的"绿"就没有意义。

⚠️【与提示词原稿的差异 · 此处需要重构 X】
原稿 F4 要求"用 `Decoder` 正常打开 sine_file"。本项目替身**没有 Decoder 类**
（解码通过 `miniaudio.stream_file`，与引擎真实调用面一致）。
故 F4 改为验证**等价能力**：WAV 文件本身合法（可被 `wave` 打开、参数正确），
且可被替身的 `stream_file` 正常产出 PCM。
"""

from __future__ import annotations

import gc
import os
import time
import wave

import pytest

from fixtures.engine_factory import create_engine, settle
from fixtures.mock_miniaudio import BACKEND, DEFAULTS

pytestmark = pytest.mark.engine


# ═══════════════ F1 引擎初始态 ═══════════════
def test_f1_create_engine_initial_state():
    """F1：create_engine() 返回的引擎初始 state=STOPPED、generation=0。"""
    engine = create_engine()
    try:
        assert engine.state == "stopped", engine.state
        assert engine.generation == 0, engine.generation
        assert engine.position == 0.0
        assert engine.current_path is None
    finally:
        engine.shutdown()


# ═══════════════ F2 settle() 语义与耗时 ═══════════════
def test_f2_settle_after_stop_reaches_stopped(tmp_path):
    """F2：play → stop → settle() → state=STOPPED，且 settle 耗时 ≤500ms。"""
    p = tmp_path / "a.mp3"
    p.write_bytes(b"")
    engine = create_engine()
    try:
        engine.load(str(p))
        engine.play()
        assert engine.state == "playing"

        t0 = time.perf_counter()
        engine.stop()
        settle(engine)
        elapsed_ms = (time.perf_counter() - t0) * 1000

        assert engine.state == "stopped", engine.state
        assert elapsed_ms <= 500.0, f"settle 耗时 {elapsed_ms:.0f}ms > 500ms"
        assert not engine.fade_pending
    finally:
        engine.shutdown()


# ═══════════════ F3 零淡出边界不阻塞 ═══════════════
def test_f3_settle_returns_immediately_when_fade_out_zero(tmp_path):
    """F3：fade_out_ms=0 时 settle() 立即返回（不 sleep）。"""
    p = tmp_path / "a.mp3"
    p.write_bytes(b"")
    engine = create_engine(fade_out_ms=0.0)
    try:
        engine.load(str(p))
        engine.play()

        t0 = time.perf_counter()
        engine.stop()
        settle(engine)
        elapsed_ms = (time.perf_counter() - t0) * 1000

        assert engine.state == "stopped", engine.state
        assert elapsed_ms < 100.0, f"零淡出下 settle 耗时 {elapsed_ms:.1f}ms 过长"
    finally:
        engine.shutdown()


# ═══════════════ F4 sine_file 生成正确 ═══════════════
def test_f4_sine_file_is_valid_wav(sine_file):
    """F4：sine_file fixture 生成的是合法 WAV，且可被替身正常解码。"""
    assert os.path.isfile(sine_file)
    with wave.open(sine_file, "rb") as wf:
        assert wf.getnchannels() == 1
        assert wf.getsampwidth() == 2
        assert wf.getframerate() == 44100
        assert wf.getnframes() > 0

    import miniaudio

    gen = miniaudio.stream_file(sine_file, nchannels=2, sample_rate=44100)
    assert len(next(gen)) > 0
    gen.close()


def test_f4b_noise_file_is_valid_wav(noise_file):
    """F4 补充：noise_file fixture 同样合法。"""
    assert os.path.isfile(noise_file)
    with wave.open(noise_file, "rb") as wf:
        assert wf.getframerate() == 44100 and wf.getnframes() > 0


# ═══════════════ F5 teardown 释放资源 ═══════════════
def test_f5_shutdown_releases_resources(tmp_path):
    """F5：shutdown() 后设备/生成器引用清空，文件无残留句柄（可删除）。"""
    p = tmp_path / "a.mp3"
    p.write_bytes(b"")
    engine = create_engine()
    engine.load(str(p))
    engine.play()
    assert engine._stream is not None
    assert engine._device is not None

    engine.shutdown()          # 等价于 conftest 里 engine fixture 的 teardown

    assert engine._device is None, "设备引用未释放"
    assert engine._stream is None, "生成器引用未释放"
    assert not engine.fade_pending

    gc.collect()
    os.remove(str(p))          # 无残留句柄 → 可删除（Windows 上会直接报错）


def test_f5b_engine_fixture_teardown_is_wired(engine, tmp_path):
    """F5 补充：conftest 的 engine fixture 可用，且 shutdown 是幂等的。"""
    p = tmp_path / "b.mp3"
    p.write_bytes(b"")
    engine.load(str(p))
    engine.play()
    engine.shutdown()
    engine.shutdown()          # 幂等，不应抛异常
    assert engine.state == "stopped"


# ═══════════════ F6 替身状态跨用例隔离 ═══════════════
def test_f6a_backend_mutation_is_local():
    """F6（A）：本用例修改 BACKEND，不应影响下一个用例。"""
    BACKEND["duration"] = 999.0
    BACKEND["chunks"] = 1
    assert BACKEND["duration"] == 999.0


def test_f6b_backend_reset_between_tests():
    """F6（B）：上一个用例的修改已被 conftest 的 autouse 夹具重置。

    这就是「session scope 的替身注入 + 逐例状态重置」的隔离保证：
    替身模块本身是 session 级共享（sys.modules 只认首次 import），
    但**可变的 BACKEND 配置**必须逐例重置，否则套件间会串味。
    """
    assert BACKEND["duration"] == DEFAULTS["duration"], BACKEND["duration"]
    assert BACKEND["chunks"] == DEFAULTS["chunks"], BACKEND["chunks"]


def test_f6c_engine_instances_do_not_share_state(tmp_path):
    """F6（C）：两个引擎实例互不影响（无隐藏的模块级共享状态）。"""
    a, b = tmp_path / "a.mp3", tmp_path / "b.mp3"
    a.write_bytes(b"")
    b.write_bytes(b"")
    e1, e2 = create_engine(), create_engine()
    try:
        e1.load(str(a))
        e1.play()
        assert e2.state == "stopped"
        assert e2.generation == 0, "e2 被 e1 的操作影响了"
        # e1: load() +1（内部 _stop_now）+ play() +1 = 2；关键是 e2 保持 0
        assert e1.generation == 2, e1.generation
    finally:
        e1.shutdown()
        e2.shutdown()
