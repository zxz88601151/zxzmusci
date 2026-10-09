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

# 【D1 修复】替身由 conftest.py 顶层**唯一入口** install()。
# 原先此处也调一次 install()，形成"双入口"：靠 install() 幂等才侥幸正确。
# 现在改为**显式断言**替身已在位 —— 把隐式 import 顺序依赖变成显式契约。
assert sys.modules.get("miniaudio") is not None, (
    "替身未注入：conftest.py 顶层 install() 应已执行（收集期早于本模块 import）"
)
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


def test_fade_envelope():
    """淡入淡出包络 + 路径（原 test_volume_fade.py 拆分）"""
    # ═══════════════ D ═══════════════

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

    # ═══════════════ E ═══════════════

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

    # ═══════════════ F ═══════════════

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

    # ═══════════════ H ═══════════════

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


    assert not _FAILS, f"{len(_FAILS)} 项断言失败：" + "; ".join(_FAILS)
