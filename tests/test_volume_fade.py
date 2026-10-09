"""test_volume_fade.py —— 感知音量曲线 + 淡入淡出 + 平滑过渡 的验收测试。

独立可运行（不依赖 pytest / 真声卡 / miniaudio）：

    python tests/test_volume_fade.py

做法：注入替身 miniaudio 后端，合成正弦/常量 PCM，逐段计算 RMS 与帧间增益差。
覆盖：volume_curve 不变式、FadeEnvelope 行为、smooth×fade 包络、路径定义、回归断言。
"""

from __future__ import annotations

import array
import math
import os
import sys
import tempfile
import time
import threading
import types

# ─────────── 替身 miniaudio 后端 ───────────
_SR = 44100
_FAKE = types.ModuleType("miniaudio")


class _SampleFormat:
    SIGNED16 = "s16"


class FakeDevice:
    def __init__(self, **kw):
        self.gen = None
        self.started = 0
        self.stopped = 0

    def start(self, gen):
        self.started += 1
        self.gen = gen

    def stop(self):
        self.stopped += 1
        self.gen = None

    def close(self):
        pass


class _Info:
    def __init__(self, duration):
        self.duration = duration


# 每个测试自行设置 DUR / CHUNKS / MODE
BACKEND = {"duration": 3.0, "chunks": 4, "frames_per_chunk": 256, "mode": "ok"}


def _stream_file(path, **kw):
    if BACKEND["mode"] == "error":
        yield array.array("h", [1000, 1000])
        raise RuntimeError("boom: 文件损坏")
    n = BACKEND["frames_per_chunk"]
    for _ in range(BACKEND["chunks"]):
        yield array.array("h", [10000, 10000] * n)


_FAKE.SampleFormat = _SampleFormat
_FAKE.PlaybackDevice = FakeDevice
_FAKE.get_file_info = lambda p: _Info(BACKEND["duration"])
_FAKE.stream_file = _stream_file
sys.modules["miniaudio"] = _FAKE

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
A = os.path.join(TMP, "a.mp3")
B = os.path.join(TMP, "b.mp3")
for _p in (A, B):
    with open(_p, "wb"):
        pass


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
check("E4 stop 期间 state 曾为 PLAYING→终态 STOPPED", e.state == "stopped", e.state)
check("E5 stop 后才 device.stop()", e._device.stopped == before_stop + 1, str(e._device.stopped))
check("E6 stop 后解码器已关（旧生成器耗尽）", e._stream is None and _is_exhausted(gen))

# E7 pause → 终态 PAUSED
e = AudioEngine()
e.set_volume(1.0)
e.load(A)
e.play()
e.pause()
check("E7 pause → 终态 PAUSED", e.state == "paused", e.state)

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
check("F2 stop 后 _stream 已清空", e._stream is None)
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
import json  # noqa: E402

from player.settings import Settings  # noqa: E402

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
_t0 = time.perf_counter()
_e3.stop()
_el = (time.perf_counter() - _t0) * 1000
check("C2 淡出 600ms 生效（stop 阻塞 ≥600ms）", _el >= 600 * 0.95, f"{_el:.0f}ms")

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

print()
print("RESULT:", "ALL PASS" if not _FAILS else f"{len(_FAILS)} FAILED -> {_FAILS}")
sys.exit(1 if _FAILS else 0)