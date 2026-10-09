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
        yield array.array("h", [8000, -8000] * n)


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
from player.engine.volume_curve import db_to_linear, gain_to_db, linear_to_db_gain  # noqa: E402

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

print()
print("RESULT:", "ALL PASS" if not _FAILS else f"{len(_FAILS)} FAILED -> {_FAILS}")
sys.exit(1 if _FAILS else 0)
