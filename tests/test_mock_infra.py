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


# ═══════════════ M9~M14【D5/D6 修复】替身异常面与 close 可观测 ═══════════════
# 第 17 轮审计结论：
#   D5 —— FakeDevice.close() 是空实现且不进 EVENTS → "shutdown 是否真关了设备"不可观测
#   D6 —— 替身零异常面 → 引擎 6 处 `except Exception: pass` 与 `except → _fail(...)`
#         分支**永远执行不到**（覆盖率虚高，是假绿高发区）
# 以下各条把"异常面确实触达引擎分支"变成可执行的断言，而不是注释。
#
# ──【D6-R · 设计取舍，非缺陷】device.stop()/close() 抛错时引擎**不转 ERROR** ──
# 原审计设想："STOP_ERROR/CLOSE_ERROR → state=ERROR 且不冒泡"。
# 实测（本文件 M10/M10b/M11 即为证据）引擎的**真实契约**是"尽力而为收尾"：
#   * `player.py:246-251`（_finalize_fade_out）、`227-231`（_stop_now）、
#     `306-310`（poll 的 finished/errored 清理）、`360-371`（shutdown）
#     四处对 device.stop()/close() 一律 `except Exception: pass`；
#   * 异常**不冒泡**（对），但随后照常把 state 收敛到 STOPPED / PAUSED，
#     **不会**置 State.ERROR、**不会**记 ErrorCode；
#   * 即设备关闭失败被定义为"可降级"——用户还能继续操作，不会被弹错。
# 故本文件按**实际契约**锁定（不冒泡 + 仍收敛到终态 + 异常确实触发过），
# 而不是按设想断言 ERROR。若未来希望"设备异常上报 ERROR"，需重构：
# 新增 `_on_device_fault()` 显式入口，经 `poll()` 事件出口上报
# （保持"音频线程不碰 Qt / 状态变更显式化"两条既有约束）。
# 依项目约定：无法在不改架构下满足的不变式 → 标注"此处需要重构 X"，禁止悄悄放宽断言。

def test_m9_device_close_is_observable(tmp_path):
    """M9【D5】：shutdown() 必须真的调用 device.close()，且事件可观测。

    修复前 FakeDevice.close() 是 `pass`，EVENTS 里只有 device.stop/raw.close，
    于是"shutdown 是否释放了设备句柄"根本无法断言。
    """
    from fixtures.engine_factory import create_engine
    from fixtures.mock_miniaudio import EVENTS

    p = tmp_path / "a.mp3"
    p.write_bytes(b"")
    engine = create_engine()
    engine.load(str(p))
    engine.play()

    EVENTS.clear()
    engine.shutdown()

    assert "device.close" in EVENTS, f"device.close 未被记录：EVENTS={EVENTS}"
    # 顺序契约：stop 先于 close
    assert EVENTS.index("device.stop") < EVENTS.index("device.close"), str(EVENTS)


def test_m9b_device_close_count_is_exactly_once(tmp_path):
    """M9b【D5】：`engine._device` 的 closed 计数证明 close 恰好执行一次。"""
    from fixtures.engine_factory import create_engine

    p = tmp_path / "a.mp3"
    p.write_bytes(b"")
    engine = create_engine()
    engine.load(str(p))
    engine.play()
    dev = engine._device
    assert dev is not None

    engine.shutdown()
    assert dev.closed == 1, f"close 调用次数={dev.closed}（期望 1）"

    # shutdown 幂等：第二次不应再 close（device 已置 None）
    engine.shutdown()
    assert dev.closed == 1, f"幂等 shutdown 后又 close 了：{dev.closed}"


def test_m10_stop_fault_reaches_engine_guard(tmp_path):
    """M10【D6】：device.stop() 抛异常时，引擎那几处 `except: pass` 真的执行到，
    且**异常不穿出**控制路径（引擎必须仍然走到 STOPPED）。

    【契约见文件头 D6-R】本用例断言的是**实际契约**：不冒泡 + 收敛到 STOPPED，
    而**不是** state=ERROR（引擎有意把设备关闭失败定义为可降级）。

    【关键】必须证明"故障真的被触发了"，否则本用例在**没注入异常**时也会绿
    （引擎本来就能正常 stop）→ 那是假绿。故显式记录 stop 抛出的次数。
    """
    from fixtures.engine_factory import create_engine, settle
    from fixtures.mock_miniaudio import set_fault, clear_fault

    p = tmp_path / "a.mp3"
    p.write_bytes(b"")
    engine = create_engine()
    engine.load(str(p))
    engine.play()

    dev = engine._device
    raised = {"n": 0}
    orig_stop = dev.stop

    def counting_stop():
        try:
            return orig_stop()
        except Exception:
            raised["n"] += 1
            raise

    dev.stop = counting_stop

    set_fault("STOP_ERROR")
    try:
        engine.stop()          # 控制路径：只置淡出意图，立即返回，不应抛
        settle(engine)         # 内部会调 device.stop() → 抛 → 被引擎 except 吃掉
        assert raised["n"] >= 1, (
            "device.stop() 根本没抛 —— 故障未注入，本用例测的是空气（假绿）"
        )
        assert engine.state == "stopped", (
            f"device.stop() 抛异常后引擎未收敛到 STOPPED：{engine.state}"
        )
        # 设备关闭失败**不得**污染错误码（真实契约：可降级）
        assert engine.error_code is None, (
            f"device.stop() 异常被误记成错误码：{engine.error_code}（与 D6-R 契约不符）"
        )
    finally:
        clear_fault()
    engine.shutdown()


def test_m10b_stop_fault_on_pause_path_is_swallowed(tmp_path):
    """M10b【D6】：pause 路径（_finalize_fade_out intent="pause"）下
    device.stop() 抛错，引擎同样吞掉异常并收敛到 PAUSED，不冒泡、不转 ERROR。

    覆盖 M10 未覆盖的第二条 player.py 吞异常点（`_finalize_fade_out` 的 pause 分支）
    与第三个终局状态 PAUSED。
    """
    from fixtures.engine_factory import create_engine, settle
    from fixtures.mock_miniaudio import set_fault, clear_fault

    p = tmp_path / "a.mp3"
    p.write_bytes(b"")
    engine = create_engine()
    engine.load(str(p))
    engine.play()

    dev = engine._device
    raised = {"n": 0}
    orig_stop = dev.stop

    def counting_stop():
        try:
            return orig_stop()
        except Exception:
            raised["n"] += 1
            raise

    dev.stop = counting_stop

    set_fault("STOP_ERROR")
    try:
        engine.pause()                 # 控制路径：只置意图，立即返回
        assert engine.state == "playing", "pause() 不应在淡出完成前改状态（B4）"
        settle(engine)                 # 淡出走完 → _finalize_fade_out → device.stop() 抛
        assert raised["n"] >= 1, (
            "pause 路径上 device.stop() 根本没抛 —— 故障未注入（假绿）"
        )
        assert engine.state == "paused", (
            f"pause 路径吞异常后未收敛到 PAUSED：{engine.state}"
        )
        assert engine.error_code is None, (
            f"pause 路径异常被误记成错误码：{engine.error_code}（与 D6-R 契约不符）"
        )
    finally:
        clear_fault()
    engine.shutdown()


def test_m11_close_fault_reaches_engine_guard(tmp_path):
    """M11【D6】：device.close() 抛异常时，shutdown() 的 except 真的执行到，
    且异常不穿出（shutdown 必须仍然完成收尾）。

    【契约见文件头 D6-R】断言实际契约：不冒泡 + `_device` 置 None，非 state=ERROR。

    【关键】同 M10：断言"close 确实抛了"，否则无故障时也会绿。
    """
    from fixtures.engine_factory import create_engine
    from fixtures.mock_miniaudio import set_fault, clear_fault

    p = tmp_path / "a.mp3"
    p.write_bytes(b"")
    engine = create_engine()
    engine.load(str(p))
    engine.play()
    dev = engine._device

    raised = {"n": 0}
    orig_close = dev.close

    def counting_close():
        try:
            return orig_close()
        except Exception:
            raised["n"] += 1
            raise

    dev.close = counting_close

    set_fault("CLOSE_ERROR")
    try:
        engine.shutdown()      # 不应抛
        assert raised["n"] == 1, (
            f"device.close() 抛出次数={raised['n']}（期望 1）—— 故障未生效则为假绿"
        )
        assert engine._device is None, "shutdown 未完成收尾（_device 未置 None）"
        assert dev.closed == 1, "device.close() 未被调用到"
        assert engine.state == "stopped", f"shutdown 后状态异常：{engine.state}"
        assert engine.error_code is None, (
            f"device.close() 异常被误记成错误码：{engine.error_code}（与 D6-R 契约不符）"
        )
    finally:
        clear_fault()


def test_m12_read_error_reaches_pcm_stream_except(tmp_path):
    """M12【D6】：流中途抛异常 → `_pcm_stream` 的 `except Exception` 分支执行到，
    转成 ERROR/FILE_CORRUPT，且**异常不穿出音频回调**（约束2）。

    【D6 语义断言①】READ_ERROR → state=error 且 error_code=FILE_CORRUPT。
    与 M10/M11 的"可降级"契约相对——解码错误是**真错误**，必须上报。
    """
    from fixtures.engine_factory import create_engine
    from fixtures.mock_miniaudio import set_fault, clear_fault

    p = tmp_path / "a.mp3"
    p.write_bytes(b"")
    engine = create_engine()
    engine.load(str(p))
    engine.play()

    set_fault("READ_ERROR")
    try:
        # 直接遍历流：不得抛异常（引擎在 _pcm_stream 内已捕获）
        for _ in engine._stream:      # 若异常穿出，本行会直接 raise → 用例红
            pass
        assert engine.state == "error", f"未转 ERROR：{engine.state}"
        from player.engine.states import ErrorCode

        assert engine.error_code == ErrorCode.FILE_CORRUPT, str(engine.error_code)
    finally:
        clear_fault()
    # 【不冒泡】poll() 是唯一事件出口：错误经 pending 标志走出，不在音频线程抛
    errors: list = []
    engine.on_error = lambda code, msg: errors.append((code, msg))
    engine.poll()
    from player.engine.states import ErrorCode

    assert errors and errors[0][0] == ErrorCode.FILE_CORRUPT, (
        f"poll() 未上报错误事件：{errors}"
    )
    engine.shutdown()


def test_m13_info_error_reaches_load_except(tmp_path):
    """M13【D6】：get_file_info() 抛异常 → `load()` 的 `except Exception → _fail(...)`
    分支执行到，且映射为 FILE_CORRUPT。

    【D6 语义断言②】INFO_ERROR → load() 抛 AudioError(code=FILE_CORRUPT)。
    load() 是**同步控制路径**（主线程），与 READ_ERROR 不同：它**允许**抛——
    因为此时还没有活跃流，抛错是最直接的契约（UI 直接弹）。
    """
    from fixtures.engine_factory import create_engine
    from fixtures.mock_miniaudio import set_fault, clear_fault
    from player.engine.player import AudioError
    from player.engine.states import ErrorCode

    p = tmp_path / "a.mp3"
    p.write_bytes(b"")
    engine = create_engine()

    set_fault("INFO_ERROR")
    try:
        with pytest.raises(AudioError) as ei:
            engine.load(str(p))
        assert ei.value.code == ErrorCode.FILE_CORRUPT, str(ei.value.code)
    finally:
        clear_fault()
    # 错误后引擎仍可用于下一首（load 失败不应留下脏状态）
    b = tmp_path / "b.mp3"
    b.write_bytes(b"")
    engine.load(str(b))
    assert engine.state == "stopped", f"load 失败后状态被污染：{engine.state}"
    engine.shutdown()


def test_m14_new_faults_are_reversible():
    """M14【D6】：新增四类故障必须双向可逆（与 M4 同等要求）。

    否则一个用例注入了故障、下一个用例就莫名奇妙地中招（跨用例串味）。
    """
    from fixtures.mock_miniaudio import DEFAULTS, set_fault, clear_fault

    for name in ("STOP_ERROR", "CLOSE_ERROR", "READ_ERROR", "INFO_ERROR"):
        set_fault(name)
        # 注入后至少有一个字段偏离默认
        drifted = any(BACKEND[k] != DEFAULTS[k] for k in DEFAULTS)
        assert drifted, f"set_fault({name}) 未改变任何 BACKEND 字段"
        clear_fault()
        assert BACKEND == DEFAULTS, f"clear_fault() 后仍有残留：{BACKEND}"

    assert set(BACKEND) == set(DEFAULTS), "BACKEND 出现 DEFAULTS 之外的键"
