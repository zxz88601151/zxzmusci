"""test_volume_fade.py —— 感知音量曲线 + 淡入淡出 + 平滑过渡 的验收测试。

独立可运行（不依赖 pytest / 真声卡 / miniaudio）：

    python tests/test_volume_fade.py

做法：注入替身 miniaudio 后端，合成正弦/常量 PCM，逐段计算 RMS 与帧间增益差。
覆盖：volume_curve 不变式、FadeEnvelope 行为、smooth×fade 包络、路径定义、回归断言。
"""

from __future__ import annotations

import array
import logging
import math
import os
import sys
import tempfile
import time
import threading
import types

# ─────────── 替身 miniaudio 后端 ───────────
_SR = 44100
import _audio_stub  # noqa: E402

# 共享替身后端：sys.modules["miniaudio"] 只在**首次 import 引擎**时生效，
# 因此所有套件必须用同一个替身（差异只体现在 configure() 的参数上），
# 否则后加载的套件会拿到先加载套件的后端。
_audio_stub.install()
BACKEND = _audio_stub.BACKEND
EVENTS = _audio_stub.EVENTS
CLOSES = _audio_stub.CLOSES
FakeDevice = _audio_stub.FakeDevice

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from player.engine.fade import _FadeEnvelope  # noqa: E402
from player.engine.player import (  # noqa: E402
    FADE_IN_MS,
    FADE_OUT_MS,
    VOLUME_SMOOTH_MS,
    AudioEngine,
)
from player.engine.states import ErrorCode  # noqa: E402
from player.engine.volume_curve import (  # noqa: E402
    db_to_linear,
    gain_to_db,
    linear_to_db_gain,
    slider_db,
)
from player.settings import Settings  # noqa: E402
from player.engine.player import SEEK_FADE_SKIP_MS  # noqa: E402
from player.engine.volume_curve import MIN_DB  # noqa: E402
from player.settings import FADE_MS_MAX, FADE_MS_MIN  # noqa: E402
import glob  # noqa: E402
import json  # noqa: E402
import re  # noqa: E402

# ─────────── 迷你断言框架 ───────────
_FAILS: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(("PASS " if cond else "FAIL ") + name + (("  " + extra) if extra else ""))
    if not cond:
        _FAILS.append(name)


def close(a: float, b: float, tol: float) -> bool:
    return abs(a - b) <= tol


def sine(frames: int, freq: float = 440.0, amp: int = 20000) -> array.array:
    out = array.array("h")
    for i in range(frames):
        v = int(amp * math.sin(2 * math.pi * freq * i / _SR))
        out.append(v)
        out.append(v)
    return out


def const(frames: int, val: int = 10000) -> array.array:
    return array.array("h", [val, val] * frames)


def rms(chunk: array.array) -> float:
    if len(chunk) == 0:
        return 0.0
    return math.sqrt(sum(s * s for s in chunk) / len(chunk))


def _is_exhausted(gen) -> bool:
    """生成器被 close() 或自然结束后：next() 应抛 StopIteration。"""
    try:
        next(gen)
    except StopIteration:
        return True
    except Exception:
        return False
    return False


def steady_engine(volume: float) -> AudioEngine:
    """构造一个增益已进入稳态（平滑到位、淡入完成）的引擎，用于测曲线本身。"""
    e = AudioEngine()
    e.set_volume(volume)
    with e._lock:
        e._smooth_gain = e._target_gain
        e._fade = _FadeEnvelope(1.0, 1.0, _SR, start_gain=1.0)  # 1ms → 立即到位
    return e


TMP = tempfile.mkdtemp()


def settle(e, limit=900):
    """把淡出推到底并让 poll() 收尾（模拟设备持续拉流 + UI 定时 poll）。

    【F7 语义变更】stop()/pause() 改为非阻塞后，"停止完成"不再发生在调用返回时，
    而是发生在淡出走完 + poll() 收尾之后。断言因此需要先 settle 再检查终态。
    """
    for _ in range(limit):
        gen = e._stream
        if gen is not None:
            try:
                next(gen)
            except StopIteration:
                pass
        e.poll()
        if not e.fade_pending and e._pending_finalize is None:
            return

A = os.path.join(TMP, "a.mp3")
B = os.path.join(TMP, "b.mp3")
for _p in (A, B):
    with open(_p, "wb"):
        pass


def test_volume_fade():
    # ═══════════════ A. 感知音量曲线 ═══════════════
    print("── A. volume_curve ──")
    check("A1 v=1.0 → 0dB → gain=1.0", linear_to_db_gain(1.0) == 1.0)
    check("A2 v=0.0 → 真静音 0.0", linear_to_db_gain(0.0) == 0.0)
    check(
        "A3 v=0.5 → ≈10**(-30/20)=0.0316（±10%）",
        close(linear_to_db_gain(0.5), 10 ** (-30 / 20), 10 ** (-30 / 20) * 0.10),
        f"{linear_to_db_gain(0.5):.5f}",
    )
    check("A4 单调递增", all(linear_to_db_gain(i / 100) < linear_to_db_gain((i + 1) / 100) for i in range(100)))
    check("A5 clamp 越界不抛异常", linear_to_db_gain(-5) == 0.0 and linear_to_db_gain(9) == 1.0)
    check("A6 非法输入返回边界而非抛异常",
          linear_to_db_gain("x") == 0.0 and linear_to_db_gain(None) == 0.0 and linear_to_db_gain(float("nan")) == 0.0)
    check("A7 db_to_linear 反向", db_to_linear(0.0) == 1.0 and db_to_linear(-60.0) == 0.0
          and close(db_to_linear(-30.0), 0.0316, 0.001))
    check("A8 gain_to_db 往返一致", close(gain_to_db(linear_to_db_gain(0.7)), -18.0, 0.01),
          f"{gain_to_db(linear_to_db_gain(0.7)):.3f}dB")

    # ═══════════════ B. 音量不变式（RMS） ═══════════════
    print("── B. 音量 RMS 不变式 ──")
    sig = sine(4096)
    in_rms = rms(sig)
    e = steady_engine(1.0)
    out = e._apply_gain_envelope(sig, 4096)
    ratio = rms(out) / in_rms
    check("B1 v=1.0 → 输出/输入 RMS ∈ [0.95,1.05]", 0.95 <= ratio <= 1.05, f"{ratio:.4f}")

    e = steady_engine(0.0)
    out = e._apply_gain_envelope(sig, 4096)
    check("B2 v=0.0 → 输出 RMS < 1e-4", rms(out) < 1e-4, f"{rms(out):.2e}")

    e = steady_engine(0.5)
    out = e._apply_gain_envelope(sig, 4096)
    r = rms(out) / in_rms
    check("B3 v=0.5 → 增益 ≈0.0316（±10%）", close(r, 0.0316, 0.0032), f"{r:.5f}")

    # ═══════════════ C. 平滑过渡（帧间增益比 ∈ [0.5,2.0]） ═══════════════
    print("── C. 平滑过渡 ──")
    e = AudioEngine()
    with e._lock:
        e._smooth_gain = 1.0
        e._target_gain = 0.0
        e._fade = _FadeEnvelope(1.0, 1.0, _SR, start_gain=1.0)
    out = e._apply_gain_envelope(const(4096), 4096)
    gains = [max(1e-9, out[i * 2] / 10000.0) for i in range(4096)]
    ratios = [b / a for a, b in zip(gains, gains[1:])]
    check("C1 帧间增益比 ∈ [0.5, 2.0]（≤±6dB）", all(0.5 <= r <= 2.0 for r in ratios),
          f"min={min(ratios):.6f} max={max(ratios):.6f}")
    check("C2 平滑确实在收敛（末值 < 首值）", gains[-1] < gains[0], f"{gains[0]:.5f}→{gains[-1]:.5f}")

    e2 = AudioEngine()
    with e2._lock:
        e2._smooth_gain = 0.0
        e2._target_gain = 1.0
        e2._fade = _FadeEnvelope(1.0, 1.0, _SR, start_gain=1.0)
    out = e2._apply_gain_envelope(const(4096), 4096)
    g2 = [max(1e-9, out[i * 2] / 10000.0) for i in range(4096)]
    r2 = [b / a for a, b in zip(g2, g2[1:])]
    check("C3 上升方向同样平滑", all(0.5 <= r <= 2.0 for r in r2), f"max={max(r2):.6f}")
    check("C4 平滑时间常数符合常量（80ms≈3528帧）", abs(VOLUME_SMOOTH_MS - 80.0) < 1e-9)

    # ═══════════════ D. FadeEnvelope 行为 ═══════════════
    print("── D. _FadeEnvelope ──")
    f = _FadeEnvelope(target=1.0, duration_ms=100.0, sample_rate=1000, start_gain=0.0)
    check("D1 初始未完成", not f.is_done and f.current_gain == 0.0)
    start = f.step(50)
    check("D2 step 返回段首增益", start == 0.0)
    check("D3 推进后未完成", not f.is_done and close(f.current_gain, 0.5, 1e-9), f"{f.current_gain}")
    f.step(50)
    check("D4 端点精确命中 1.0", f.is_done and f.current_gain == 1.0, f"{f.current_gain}")
    check("D5 current_target 正确", f.current_target == 1.0)
    check("D6 完成后 step 不再变化", f.step(10) == 1.0)

    f2 = _FadeEnvelope(target=1.0, duration_ms=100.0, sample_rate=1000, start_gain=0.0)
    f2.step(50)                     # 到 0.5
    f2.reset(0.0, 100.0)            # 中途改向：以当前增益为起点，重新计时 100 帧走到 0
    check("D7 中途改向以当前值为起点", close(f2.current_gain, 0.5, 1e-9), f"{f2.current_gain}")
    f2.step(50)
    check("D8 改向后半程到 0.25", close(f2.current_gain, 0.25, 1e-9), f"{f2.current_gain}")
    f2.step(50)
    check("D8b 改向后端点精确命中 0.0", f2.current_gain == 0.0, f"{f2.current_gain}")

    f3 = _FadeEnvelope(target=1.0, duration_ms=100.0, sample_rate=1000, start_gain=0.0)
    f3.step(50)
    f3.freeze()
    before = f3.current_gain
    f3.step(50)
    check("D9 freeze 冻结当前增益", f3.current_gain == before and f3.frozen, f"{f3.current_gain}")
    f3.unfreeze()
    f3.step(50)
    check("D10 unfreeze 后继续到目标", f3.current_gain == 1.0, f"{f3.current_gain}")

    # ═══════════════ E. 路径定义 ═══════════════
    print("── E. 路径定义 ──")
    BACKEND.update(duration=20000 / _SR, chunks=10, frames_per_chunk=2000, mode="ok")

    # E1 play → 从 0 淡入
    e = AudioEngine()
    e.set_volume(1.0)
    e.load(A)
    e.play()
    first = next(e._stream)
    check("E1 play → 新流从 0 淡入（首样本远小于满幅）", abs(first[0]) < 8000, f"{first[0]}")

    # E2 自然播完 → 淡出后 FINISHED
    e = AudioEngine()
    e.set_volume(1.0)
    e.load(A)
    e.play()
    last_chunk = None
    for last_chunk in e._stream:
        pass
    check("E2 自然播完 → 淡出（末样本≈0）", abs(last_chunk[-1]) < 200, f"{last_chunk[-1]}")
    check("E3 自然播完 → FINISHED", e.state == "finished", e.state)

    # E4 stop → 淡出完成才 device.stop() + 关解码器
    e = AudioEngine()
    e.set_volume(1.0)
    e.load(A)
    e.play()
    gen = e._stream
    before_stop = e._device.stopped
    e.stop()
    check("E4 淡出期间 state 仍 PLAYING（B4）", e.state == "playing" and e.fade_pending, e.state)
    settle(e)
    check("E4 淡出完成后才转 STOPPED", e.state == "stopped", e.state)
    check("E5 stop 后才 device.stop()", e._device.stopped == before_stop + 1, str(e._device.stopped))
    check("E6 stop 后解码器已关（旧生成器耗尽）", e._stream is None and _is_exhausted(gen))

    # E7 pause → 终态 PAUSED
    e = AudioEngine()
    e.set_volume(1.0)
    e.load(A)
    e.play()
    e.pause()
    check("E7 淡出期间 state 仍 PLAYING", e.state == "playing" and e.fade_pending, e.state)
    settle(e)
    check("E7 淡出完成后 → PAUSED", e.state == "paused", e.state)

    # E8 error → 立即停止，不淡出
    BACKEND["mode"] = "error"
    e = AudioEngine()
    e.set_volume(1.0)
    e.load(A)
    e.play()
    gen = e._stream
    for _ in gen:
        pass
    check("E8 error → 立即转 ERROR（不等淡出）", e.state == "error", e.state)
    check("E9 error → error_code 正确", e.error_code == ErrorCode.FILE_CORRUPT, str(e.error_code))
    BACKEND["mode"] = "ok"

    # E10 shutdown → 跳过淡出
    e = AudioEngine()
    e.set_volume(1.0)
    e.load(A)
    e.play()
    e.shutdown()
    check("E10 shutdown 跳过淡出直接 STOPPED", e.state == "stopped", e.state)

    # E11 seek → 新位置淡入
    e = AudioEngine()
    e.set_volume(1.0)
    e.load(A)
    e.play()
    e.seek(0.2)
    check("E11 seek 后位置=目标", e._frames_played == int(0.2 * _SR), str(e._frames_played))
    check("E12 seek 后新流从 0 淡入", next(e._stream)[0] < 8000)

    # E13 mute / unmute 走同一套平滑
    e = AudioEngine()
    e.set_volume(0.7)
    g_before = e.target_gain
    e.set_muted(True)
    check("E13 mute → 目标增益 0", e.target_gain == 0.0 and e.muted)
    e.set_muted(False)
    check("E14 unmute → 恢复 mute 前增益", close(e.target_gain, g_before, 1e-12) and not e.muted)

    # ═══════════════ F. 回归断言（本次改动可能破坏的既有能力） ═══════════════
    print("── F. 回归 ──")
    # F1 generation 丢弃逻辑
    e = AudioEngine()
    e.set_volume(1.0)
    e.load(A)
    e.play()
    gen_now = e.generation
    before = e._frames_played
    stale = e._pcm_stream(0, gen_now - 1)
    for _ in stale:
        pass
    check("F1 旧代次进度仍被丢弃", e._frames_played == before, f"{before}->{e._frames_played}")

    # F2 _close_decoder 顺序
    e = AudioEngine()
    e.set_volume(1.0)
    e.load(A)
    e.play()
    g = e._stream
    e.stop()
    settle(e)
    check("F2 stop（settle 后）_stream 已清空", e._stream is None)
    check("F3 stop 后旧生成器已耗尽", _is_exhausted(g))

    # F4 FINISHED 重播
    e = AudioEngine()
    e.set_volume(1.0)
    e.load(A)
    e.play()
    gen0 = e.generation
    for _ in e._stream:
        pass
    e.poll()
    check("F4 播完 → FINISHED", e.state == "finished", e.state)
    e.play()
    check("F5 FINISHED 后 play → generation +1", e.generation == gen0 + 1, f"{gen0}->{e.generation}")
    check("F6 FINISHED 后 play → 进度归零", e._frames_played == 0, str(e._frames_played))

    # F7 快速连点切歌 5 次
    e = AudioEngine()
    e.set_volume(1.0)
    e.load(A)
    e.play()
    for i in range(5):
        e.load(B if i % 2 else A)
        e.play()
    check("F7 连点切歌 5 次无异常且最终路径正确", os.path.basename(e.current_path) == "a.mp3", str(e.current_path))
    check("F8 连点后仍处于 PLAYING", e.state == "playing", e.state)

    # ═══════════════ G. V1~V7 断言清单 ═══════════════
    print("── G. V1~V7 ──")
    _v_sig = sine(4096)
    _v_in = rms(_v_sig)

    # V1
    _e = steady_engine(1.0)
    _v_r = rms(_e._apply_gain_envelope(_v_sig, 4096)) / _v_in
    check("V1 set_volume(1.0) → 输出/输入 RMS ∈ [0.95,1.05]", 0.95 <= _v_r <= 1.05, f"{_v_r:.4f}")

    # V2
    _e = steady_engine(0.0)
    _v_o = rms(_e._apply_gain_envelope(_v_sig, 4096))
    check("V2 set_volume(0.0) → 输出 RMS < 1e-4", _v_o < 1e-4, f"{_v_o:.2e}")

    # V3
    _e = steady_engine(0.5)
    _v_r = rms(_e._apply_gain_envelope(_v_sig, 4096)) / _v_in
    check("V3 set_volume(0.5) → 增益 ≈0.0316（±10%）", close(_v_r, 0.0316, 0.00316), f"{_v_r:.5f}")

    # V4 连续变速 0.0→1.0→0.3→1.0，逐帧增益比
    _e = AudioEngine()
    with _e._lock:
        _e._smooth_gain = _e._target_gain
        _e._fade = _FadeEnvelope(1.0, 1.0, _SR, start_gain=1.0)
    _gains: list[float] = []
    for _v in (0.0, 1.0, 0.3, 1.0):
        _e.set_volume(_v)
        for _ in range(6):
            _out = _e._apply_gain_envelope(const(1024), 1024)
            _gains.extend(max(0.0, _out[i * 2] / 10000.0) for i in range(1024))
    _viol = [(a, b) for a, b in zip(_gains, _gains[1:]) if a > 1e-6 and not (0.5 <= b / a <= 2.0)]
    _max_delta = max(abs(b - a) for a, b in zip(_gains, _gains[1:]))
    check("V4 连续变速 → 每帧增益比 ∈ [0.5,2.0]", not _viol, f"违规={len(_viol)}")
    check("V4 无爆音尖刺（单帧增益变化 ≤0.01）", _max_delta <= 0.01, f"max_delta={_max_delta:.6f}")

    # V5 越界 / 非法输入
    _e = AudioEngine()
    _v5_ok = True
    for _bad in (-0.1, 1.5, float("nan"), None, "x", float("inf")):
        try:
            _e.set_volume(_bad)
            if not (0.0 <= _e.volume <= 1.0):
                _v5_ok = False
        except Exception:
            _v5_ok = False
    check("V5 越界/非法输入 clamp 到 [0,1] 且不抛异常", _v5_ok, f"volume={_e.volume}")

    # V6 UI dB 显示
    check("V6 slider_db(0.0) = -60.0 dB", close(slider_db(0.0), -60.0, 1e-9), f"{slider_db(0.0)}")
    check("V6 slider_db(1.0) = 0.0 dB", close(slider_db(1.0), 0.0, 1e-9), f"{slider_db(1.0)}")
    _dbs = [slider_db(i / 100) for i in range(101)]
    check("V6 dB 读数单调递增", all(b >= a for a, b in zip(_dbs, _dbs[1:])), f"{_dbs[0]:.1f} → {_dbs[-1]:.1f}")

    # V7 mute/unmute 往返 + 曲线连续
    _e = AudioEngine()
    _e.set_volume(0.6)
    _g0 = _e.target_gain
    _e.set_muted(True)
    _mute_ok = _e.target_gain == 0.0
    _e.set_muted(False)
    check("V7 mute/unmute 往返恢复原音量",
          _mute_ok and close(_e.target_gain, _g0, 1e-12) and close(_e.volume, 0.6, 1e-12),
          f"{_e.target_gain:.6f}")

    _e2 = AudioEngine()
    with _e2._lock:
        _e2._smooth_gain = _e2._target_gain
        _e2._fade = _FadeEnvelope(1.0, 1.0, _SR, start_gain=1.0)
    _g7: list[float] = []
    for _act in (True, False, True, False):
        _e2.set_muted(_act)
        for _ in range(5):
            _out = _e2._apply_gain_envelope(const(1024), 1024)
            _g7.extend(max(0.0, _out[i * 2] / 10000.0) for i in range(1024))
    _viol7 = [(a, b) for a, b in zip(_g7, _g7[1:]) if a > 1e-6 and not (0.5 <= b / a <= 2.0)]
    check("V7 mute/unmute 往返曲线连续无跳变", not _viol7, f"违规={len(_viol7)}")

    # ═══════════════ H. C1~C4 断言清单 ═══════════════
    print("── H. C1~C4 ──")


    # C1 播放中拖动音量滑块 → 过渡平滑、RMS 曲线无台阶跳变
    _e = AudioEngine()
    with _e._lock:
        _e._smooth_gain = _e._target_gain
        _e._fade = _FadeEnvelope(1.0, 1.0, _SR, start_gain=1.0)
    _c1_gains: list[float] = []
    for _v in [0.05 + 0.95 * _i / 19 for _i in range(20)]:   # 模拟人手拖动：20 个小步
        _e.set_volume(_v)                     # 模拟拖动过程中的连续 set_volume
        for _ in range(3):
            _out = _e._apply_gain_envelope(const(512), 512)
            _c1_gains.extend(max(0.0, _out[i * 2] / 10000.0) for i in range(512))
    _c1_d = [abs(b - a) for a, b in zip(_c1_gains, _c1_gains[1:])]
    check("C1 拖动音量：逐帧无台阶跳变", max(_c1_d) < 0.002, f"max={max(_c1_d):.6f}")
    _w = 441
    _c1_win = [
        sum(_c1_gains[i : i + _w]) / _w
        for i in range(0, len(_c1_gains) - _w, _w)
    ]
    _c1_db = [20 * math.log10(max(v, 1e-4)) for v in _c1_win]
    _c1_ddb = [abs(b - a) for a, b in zip(_c1_db, _c1_db[1:])]
    check("C1 窗口化曲线无台阶（相邻 10ms 增益变化 < 2dB）", max(_c1_ddb) < 2.0,
          f"max={max(_c1_ddb):.3f}dB")

    # C2 FADE_IN_MS / FADE_OUT_MS / SMOOTH_MS 改为 50/600/200 后生效且互不干扰
    _e = AudioEngine(fade_in_ms=50, fade_out_ms=600, smooth_ms=200)
    check("C2 fade_in_ms=50 生效", _e.fade_in_ms == 50.0, str(_e.fade_in_ms))
    check("C2 fade_out_ms=600 生效", _e.fade_out_ms == 600.0, str(_e.fade_out_ms))
    check("C2 smooth_ms=200 生效", _e.smooth_ms == 200.0, str(_e.smooth_ms))

    BACKEND.update(duration=5.0, chunks=50, frames_per_chunk=2000, mode="ok")
    _e.set_volume(1.0)
    _e.load(A)
    _e.play()
    _c2 = []
    for _ in range(3):
        _out = next(_e._stream)
        _c2.extend(max(0.0, _out[i * 2] / 10000.0) for i in range(2000))
    check("C2 淡入在 50ms 内到位（不受 smooth=200 干扰）",
          _c2[min(int(50 / 1000 * _SR), len(_c2) - 1)] >= 0.95,
          f"g[2205]={_c2[min(2205, len(_c2) - 1)]:.4f}")

    _e2 = AudioEngine(smooth_ms=200)
    with _e2._lock:
        _e2._smooth_gain = 1.0
        _e2._target_gain = 0.0
        _e2._fade = _FadeEnvelope(1.0, 1.0, _SR, start_gain=1.0)
    _out = _e2._apply_gain_envelope(const(8820), 8820)
    _g_at_tau = _out[8818 * 2] / 10000.0
    check("C2 平滑时间常数 200ms 生效（τ 处 ≈0.368）", close(_g_at_tau, math.exp(-1), 0.02),
          f"{_g_at_tau:.4f}")

    _e3 = AudioEngine(fade_out_ms=600)
    _e3.set_volume(1.0)
    _e3.load(A)
    _e3.play()
    # 【F7 语义变更】stop() 不再阻塞；改为验证"淡出实际消耗的帧数 ≈ fade_out_ms"
    _frames = 0
    _e3.stop()
    while _e3.fade_pending and _frames < 10 * _SR:
        try:
            _frames += len(next(_e3._stream)) // 2
        except StopIteration:
            break
    _e3.poll()
    _expect = int(600 / 1000 * _SR)
    check("C2 淡出 600ms 生效（实际推进帧数 ≈ 600ms）",
          abs(_frames - _expect) <= _expect * 0.25, f"{_frames} 帧 vs 期望 {_expect} 帧")

    # C3 音量记忆：存 linear 0~1，恢复后听感一致
    _p = os.path.join(tempfile.mkdtemp(), "settings.json")
    _s = Settings(_p)
    _s.volume = 0.37
    _s.save()
    _s2 = Settings(_p)
    check("C3 恢复音量（linear 0~1）", close(_s2.volume, 0.37, 1e-9), f"{_s2.volume}")
    with open(_p, encoding="utf-8") as _f:
        _raw = json.load(_f)
    check("C3 落盘的是 linear 值而非 dB/gain", close(_raw["volume"], 0.37, 1e-9), str(_raw))
    check("C3 恢复后听感一致（同一条 dB 曲线）",
          linear_to_db_gain(_s2.volume) == linear_to_db_gain(0.37))
    _s.volume = 5.0
    check("C3 越界值被 clamp 到 1.0", _s.volume == 1.0, str(_s.volume))
    with open(_p, "w", encoding="utf-8") as _f:
        _f.write("{ 这不是合法 JSON")
    check("C3 设置文件损坏不炸启动（回退默认 0.8）", close(Settings(_p).volume, 0.8, 1e-9),
          str(Settings(_p).volume))

    # C4 静音状态下切歌 → 新歌仍静音
    _e = AudioEngine()
    _e.set_volume(1.0)
    _e.set_muted(True)
    _e.load(A)
    _e.play()
    _out = next(_e._stream)
    check("C4 静音下切歌 → 新歌仍静音（全 0）", all(s == 0 for s in _out),
          f"max|s|={max(abs(s) for s in _out)}")
    check("C4 静音期间目标增益仍为 0", _e.target_gain == 0.0, str(_e.target_gain))
    _e.set_volume(0.9)  # 静音期间调音量
    _out2 = next(_e._stream)
    check("C4 静音期间调音量仍不出声", all(s == 0 for s in _out2),
          f"max|s|={max(abs(s) for s in _out2)}")
    _e.set_muted(False)
    check("C4 解除静音后恢复音量", close(_e.target_gain, linear_to_db_gain(0.9), 1e-12),
          f"{_e.target_gain:.5f}")

    # ═══════════════ I. 参数表核对 ═══════════════
    print("── I. 参数表 ──")

    check("参数 MIN_DB == -60（dB 下限，负号）", MIN_DB == -60.0, str(MIN_DB))
    check("参数 FADE_IN_MS == 200", FADE_IN_MS == 200.0, str(FADE_IN_MS))
    check("参数 FADE_OUT_MS == 300", FADE_OUT_MS == 300.0, str(FADE_OUT_MS))
    check("参数 VOLUME_SMOOTH_MS == 80", VOLUME_SMOOTH_MS == 80.0, str(VOLUME_SMOOTH_MS))
    check("参数 SEEK_FADE_SKIP_MS == 100", SEEK_FADE_SKIP_MS == 100.0, str(SEEK_FADE_SKIP_MS))
    _e = AudioEngine(seek_fade_skip_ms=250)
    check("参数 seek_fade_skip_ms 可构造覆盖", _e.seek_fade_skip_ms == 250.0, str(_e.seek_fade_skip_ms))

    # ═══════════════ J. C1~C6 断言清单 ═══════════════
    print("── J. C1~C6 ──")


    # C1 settings 写入 fade_in=600 / fade_out=1500 → 重启后引擎实际使用
    _pj = os.path.join(tempfile.mkdtemp(), "settings.json")
    _sj = Settings(_pj)
    _sj.fade_in_ms = 600.0
    _sj.fade_out_ms = 1500.0
    _sj.save()
    _sj2 = Settings(_pj)
    check("C1 重启后读回 fade_in=600 / fade_out=1500",
          _sj2.fade_in_ms == 600.0 and _sj2.fade_out_ms == 1500.0,
          f"{_sj2.fade_in_ms}/{_sj2.fade_out_ms}")
    _ej = AudioEngine(fade_in_ms=_sj2.fade_in_ms, fade_out_ms=_sj2.fade_out_ms)
    check("C1 引擎实际使用 600/1500", _ej.fade_in_ms == 600.0 and _ej.fade_out_ms == 1500.0,
          f"{_ej.fade_in_ms}/{_ej.fade_out_ms}")

    # C2 越界/非法 → clamp 到 [0,2000] + warning，不崩溃
    _warns: list[str] = []


    class _WH(logging.Handler):
        def emit(self, r):
            _warns.append(r.getMessage())


    _slg = logging.getLogger("player.settings")
    _slg.setLevel(logging.DEBUG)
    _slg.addHandler(_WH())
    _c2_ok = True
    for _bad, _exp in ((-100, FADE_MS_MIN), (9999, FADE_MS_MAX), ("abc", 200.0), (None, 200.0)):
        try:
            _sj.fade_in_ms = _bad
            _got = _sj.fade_in_ms
            if abs(_got - _exp) > 1e-9:
                _c2_ok = False
        except Exception:
            _c2_ok = False
    check("C2 越界/非法值 clamp 到 [0,2000] 且不崩溃", _c2_ok, f"fade_in={_sj.fade_in_ms}")
    check("C2 越界与非法输入均记录 warning", len(_warns) >= 4, f"{len(_warns)} 条 warning")

    # C3 滑块 dB 单调、0→-60dB、1→0dB
    _dbs = [slider_db(i / 100) for i in range(101)]
    check("C3 dB 显示 0→-60dB、1→0dB、单调递增",
          close(_dbs[0], -60.0, 1e-9) and close(_dbs[-1], 0.0, 1e-9)
          and all(b >= a for a, b in zip(_dbs, _dbs[1:])),
          f"{_dbs[0]:.1f} → {_dbs[-1]:.1f}")

    # C4 音量持久化为 linear，恢复后 RMS 比值误差 <5%
    _sv = Settings(os.path.join(tempfile.mkdtemp(), "s.json"))
    _sv.volume = 0.42
    _sv.save()
    _restored = Settings(_sv.path).volume
    _sig = sine(4096)
    _e4 = steady_engine(0.42)
    _r_a = rms(_e4._apply_gain_envelope(_sig, 4096)) / rms(_sig)
    _e5 = steady_engine(_restored)
    _r_b = rms(_e5._apply_gain_envelope(_sig, 4096)) / rms(_sig)
    check("C4 恢复后听感一致（RMS 比值误差 <5%）",
          abs(_r_a - _r_b) / max(1e-9, _r_a) < 0.05, f"{_r_a:.5f} vs {_r_b:.5f}")

    # C5 静音下切歌仍静音
    BACKEND.update(duration=5.0, chunks=50, frames_per_chunk=2000, mode="ok")
    _e6 = AudioEngine()
    _e6.set_volume(1.0)
    _e6.set_muted(True)
    _e6.load(A)
    _e6.play()
    _out6 = next(_e6._stream)
    check("C5 静音下切歌 → 新歌仍静音（全 0）", all(v == 0 for v in _out6),
          f"max|s|={max(abs(v) for v in _out6)}")

    # C6 调参后断言自动适配：①行为随参数缩放 ②断言不写死 200/300
    def _fade_frames(ms):
        _e = AudioEngine(fade_out_ms=ms)
        _e.set_volume(1.0)
        _e.load(A)
        _e.play()
        _n = 0
        _e.stop()
        while _e.fade_pending and _n < 20 * _SR:
            try:
                _n += len(next(_e._stream)) // 2
            except StopIteration:
                break
        _e.poll()
        return _n


    _f300, _f600 = _fade_frames(300.0), _fade_frames(600.0)
    check("C6 淡出行为随参数缩放（300→600 约翻倍）", 1.6 < _f600 / max(1, _f300) < 2.4,
          f"{_f300} 帧 vs {_f600} 帧")
    # 变异测试：把 fade_out_ms 改成 0/80/300/1500，控制路径延迟与终态都必须一致。
    # 若任何断言写死了 300/200，调参后必然在这里暴露。
    def _burst_ms(_ms):
        _e = AudioEngine(fade_out_ms=_ms)
        _e.set_volume(1.0)
        _e.load(A)
        _e.play()
        _t0 = time.perf_counter()
        for _i in range(5):
            _e.load(B if _i % 2 else A)
            _e.play()
        _dt = (time.perf_counter() - _t0) * 1000
        _ok = os.path.basename(_e.current_path) == "a.mp3" and _e.state == "playing"
        return _dt, _ok


    _lat = {}
    for _ms in (0.0, 80.0, 300.0, 1500.0):
        _lat[_ms] = _burst_ms(_ms)
    check("C6 调参（0/80/300/1500ms）后控制路径延迟不变且均 <500ms",
          all(_d < 500.0 and _ok for _d, _ok in _lat.values()),
          str({k: round(v[0], 2) for k, v in _lat.items()}))

    # 静态检查：测试文件不得重新定义常量（否则"改常量"不会生效）
    _redef: list[str] = []
    for _tf in glob.glob(os.path.join(os.path.dirname(os.path.abspath(__file__)), "test_*.py")):
        for _ln, _line in enumerate(open(_tf, encoding="utf-8"), 1):
            if re.match(r"\s*(FADE_IN_MS|FADE_OUT_MS|VOLUME_SMOOTH_MS|SEEK_FADE_SKIP_MS)\s*=", _line):
                _redef.append(f"{os.path.basename(_tf)}:{_ln}")
    check("C6 测试未重新定义时长常量（改常量即可全局生效）", not _redef, str(_redef))

    # 汇总：任一断言失败则整个用例失败（失败清单会完整列出）
    assert not _FAILS, f"{len(_FAILS)} 项断言失败：" + "; ".join(_FAILS)
