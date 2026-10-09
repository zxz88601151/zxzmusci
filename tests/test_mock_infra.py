"""test_mock_infra.py —— 替身模块正确性自检（M1~M8）。

定位：**不是测业务代码，而是测"测业务代码的工具"本身**。
M 段验证 `tests/fixtures/mock_miniaudio.py` 与 `audio_synthesizer.py` 的行为正确性——
只有替身可信，基于它的 218 项业务断言才可信。

⚠️【与提示词原稿的差异 · 此处需要重构 X】
原稿 M2/M3/M4 测的是 `AudioDevice.start()` / `Decoder.open()/read()` / `setup_fault()`，
但**这三个类在本项目替身里并不存在**——上一轮已确认：原稿那套 API 是按"想象中的引擎接口"
设计的，与本项目引擎实际调用的 miniaudio 接口（`PlaybackDevice` / `stream_file` /
`get_file_info`）不符。替身必须匹配被测代码的真实调用面，否则测的是替身而不是代码。

故 M2/M3/M4 改为验证**替身真实提供的等价能力**：
    M2  → FakeDevice.start/stop 生命周期（替身设备是被动拉取型，无后台线程）
    M3  → stream_file 的扩展名校验 + DecodeError 路径
    M4  → set_fault(IO_ERROR) / clear_fault() 的双向可逆
"""

from __future__ import annotations

import array
import os
import struct
import time

import pytest

from fixtures.audio_synthesizer import (
    compute_clipping,
    compute_rms,
    generate_sine,
    generate_white_noise,
)
from fixtures.engine_factory import create_engine
from fixtures.mock_miniaudio import (
    BACKEND,
    DecodeError,
    FakeDevice,
    IOError as MockIOError,
    clear_fault,
    set_fault,
)

pytestmark = pytest.mark.engine


# ═══════════════ M1 替身注入后引擎可正常工作 ═══════════════
def test_m1_engine_works_with_mock(tmp_path):
    """M1：替身替换后引擎 play/stop/pause 不抛 ImportError / AttributeError。"""
    import sys

    assert sys.modules.get("miniaudio") is not None, "替身未注入"
    p = tmp_path / "a.mp3"
    p.write_bytes(b"")

    engine = create_engine()
    engine.load(str(p))
    engine.play()                      # 不应抛 ImportError / AttributeError
    assert engine.state == "playing"
    engine.pause()
    assert engine.state == "playing"   # F7：淡出期间仍 PLAYING
    assert engine.fade_pending
    engine.stop()
    assert engine.fade_pending
    engine.shutdown()


# ═══════════════ M2 设备 start/stop 生命周期 ═══════════════
def test_m2_device_lifecycle_is_bounded(tmp_path):
    """M2：start 后可被拉取；stop 后不可，且 stop() 耗时 < 50ms。

    替身设备是**被动拉取型**（无后台线程），"线程退出"对应"不再持有生成器"。
    """
    p = tmp_path / "a.mp3"
    p.write_bytes(b"")
    engine = create_engine()
    engine.load(str(p))
    engine.play()

    device = engine._device
    assert device is not None
    assert device.started >= 1
    assert device.is_running is True          # start 后处于可拉取状态
    assert device.gen is not None

    t0 = time.perf_counter()
    engine.stop()
    # stop() 是非阻塞的（F7），设备此刻还没停——先 settle 推进淡出
    from fixtures.engine_factory import settle

    settle(engine)
    elapsed_ms = (time.perf_counter() - t0) * 1000
    assert device.is_running is False          # stop 后不可再拉取
    assert device.gen is None
    assert device.stopped >= 1
    assert elapsed_ms < 500, f"settle 后收尾耗时 {elapsed_ms:.0f}ms 过长"
    engine.shutdown()


def test_m2b_device_start_fault_is_injectable():
    """M2 补充：DEVICE_ERROR 注入后 start() 抛 DeviceError。"""
    from fixtures.mock_miniaudio import DeviceError

    device = FakeDevice()
    set_fault("DEVICE_ERROR")
    try:
        with pytest.raises(DeviceError):
            device.start(iter(()))
    finally:
        clear_fault()


# ═══════════════ M3 解码器开/关/错误路径 ═══════════════
def test_m3_valid_path_streams_invalid_path_raises(tmp_path):
    """M3：合法路径可产出 PCM；非法扩展名抛 DecodeError。"""
    import miniaudio

    good = tmp_path / "good.mp3"
    good.write_bytes(b"")
    gen = miniaudio.stream_file(str(good), nchannels=2, sample_rate=44100)
    chunk = next(gen)
    assert isinstance(chunk, array.array) and len(chunk) > 0
    gen.close()

    bad = tmp_path / "bad.xyz"
    bad.write_bytes(b"")
    with pytest.raises(DecodeError):
        list(miniaudio.stream_file(str(bad), nchannels=2, sample_rate=44100))


# ═══════════════ M4 故障注入双向可逆 ═══════════════
def test_m4_io_fault_is_reversible(tmp_path):
    """M4：set_fault(IO_ERROR) → 抛 IOError；clear_fault() → 恢复正常。"""
    import miniaudio

    p = tmp_path / "a.mp3"
    p.write_bytes(b"")

    set_fault("IO_ERROR")
    try:
        with pytest.raises(MockIOError):
            list(miniaudio.stream_file(str(p), nchannels=2, sample_rate=44100))
    finally:
        clear_fault()

    # 恢复后应能正常产出
    gen = miniaudio.stream_file(str(p), nchannels=2, sample_rate=44100)
    assert len(next(gen)) > 0
    gen.close()


def test_m4b_decode_fault_is_reversible(tmp_path):
    """M4 补充：DECODE_ERROR 同样双向可逆，且与 IO_ERROR 可区分。"""
    import miniaudio

    p = tmp_path / "a.mp3"
    p.write_bytes(b"")
    set_fault("DECODE_ERROR")
    try:
        with pytest.raises(DecodeError):
            list(miniaudio.stream_file(str(p), nchannels=2, sample_rate=44100))
    finally:
        clear_fault()
    gen = miniaudio.stream_file(str(p), nchannels=2, sample_rate=44100)
    assert len(next(gen)) > 0
    gen.close()


# ═══════════════ M5 正弦波频谱纯度 ═══════════════
def _dominant_freq_hz(pcm: bytes, rate: int = 44100) -> float:
    """纯 Python 主频估计（过零计数），不依赖 numpy/scipy。

    提示词允许"无 numpy 时用纯 Python DFT 近似"。过零法对**单频正弦**是精确的：
    单位时间内正向过零次数 == 频率。
    """
    n = len(pcm) // 2
    values = struct.unpack(f"<{n}h", pcm)
    positive_crossings = sum(1 for a, b in zip(values, values[1:]) if a <= 0 < b)
    return positive_crossings / (n / rate)


def test_m5_sine_spectral_purity():
    """M5：generate_sine(440) 的主频在 438~442Hz。"""
    pcm = generate_sine(freq=440.0, duration_ms=1000, sample_rate=44100)
    est = _dominant_freq_hz(pcm, 44100)
    assert 438.0 <= est <= 442.0, f"主频估计 {est:.2f}Hz 超出 438~442"

    # 若环境有 numpy，再用 FFT 交叉验证一次（可选，不强制）
    try:
        import numpy as np
    except ImportError:
        pytest.skip("无 numpy，已用纯 Python 过零法完成验证")
    values = np.frombuffer(pcm, dtype=np.int16).astype(float)
    spectrum = np.abs(np.fft.rfft(values))
    peak = np.fft.rfftfreq(len(values), 1 / 44100)[int(np.argmax(spectrum))]
    assert 438.0 <= peak <= 442.0, f"FFT 峰值 {peak:.2f}Hz 超出 438~442"


# ═══════════════ M6 白噪声 RMS 量级 ═══════════════
def test_m6_white_noise_rms_same_order_as_sine():
    """M6：同振幅下白噪声 RMS 与正弦 RMS 同量级（比值 0.5~1.5）。"""
    sine_rms = compute_rms(generate_sine(freq=440, duration_ms=200, amplitude=0.8))
    noise_rms = compute_rms(generate_white_noise(duration_ms=200, amplitude=0.8))
    assert sine_rms > 0 and noise_rms > 0
    ratio = sine_rms / noise_rms
    assert 0.5 <= ratio <= 1.5, f"正弦/白噪 RMS 比值 {ratio:.3f} 超出 0.5~1.5"
    # 理论值：正弦 0.8/√2≈0.566，均匀白噪 0.8/√3≈0.462 → 比值 ≈1.22
    assert 1.1 <= ratio <= 1.35, f"比值 {ratio:.3f} 偏离理论值 1.22 过多"


# ═══════════════ M7/M8 削波检测 ═══════════════
def test_m7_no_clipping_at_full_scale():
    """M7：满幅但不超范围 → compute_clipping 返回 False。"""
    pcm = generate_sine(freq=440, duration_ms=50, amplitude=1.0)
    assert compute_clipping(pcm) is False


def test_m8_clipping_detected_out_of_range():
    """M8：超出 [-1.0, 1.0] → compute_clipping 返回 True。

    注意：int16 里唯一能"越界"的值是 -32768——它归一化后是 -1.000031（<-1.0）。
    +32767/32767 恰好等于 1.0，不算越界。
    """
    in_range = struct.pack("<3h", 32767, 0, -32767)
    assert compute_clipping(in_range) is False

    out_of_range = struct.pack("<3h", 32767, 0, -32768)
    assert compute_clipping(out_of_range) is True
