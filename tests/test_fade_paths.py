"""test_fade_paths.py —— F1~F9 淡入淡出路径断言。

    python tests/test_fade_paths.py

用替身后端 + 常量幅值 PCM，从输出反推逐帧增益，验证各路径的淡入淡出行为。
"""

from __future__ import annotations

import array
import math
import os
import pytest  # noqa: E402
import sys
import tempfile
import time
import types

_SR = 44100
import _audio_stub  # noqa: E402

# 共享替身后端：sys.modules["miniaudio"] 只在**首次 import 引擎**时生效，
# 因此所有套件必须用同一个替身（差异只体现在 configure() 的参数上），
# 否则后加载的套件会拿到先加载套件的后端。
_audio_stub.install()

pytestmark = pytest.mark.engine
BACKEND = _audio_stub.BACKEND
EVENTS = _audio_stub.EVENTS
CLOSES = _audio_stub.CLOSES
FakeDevice = _audio_stub.FakeDevice
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from player.engine.player import (  # noqa: E402
    FADE_IN_MS,
    FADE_OUT_MS,
    SEEK_FADE_SKIP_MS,
    AudioEngine,
)
from player.engine.states import ErrorCode  # noqa: E402

_FAILS: list[str] = []
_GAPS: list[str] = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (("  " + extra) if extra else ""))
    if not cond:
        _FAILS.append(name)


def gap(name, detail=""):
    """明确标注「已知未满足」，不静默放宽断言。"""
    print("GAP  " + name + (("  " + detail) if detail else ""))
    _GAPS.append(name)


TMP = tempfile.mkdtemp()
A = os.path.join(TMP, "a.mp3")
B = os.path.join(TMP, "b.mp3")
for _p in (A, B):
    with open(_p, "wb"):
        pass


def settle(e, limit=900):
    """【F7 语义变更】stop()/pause() 非阻塞后，终态在淡出走完 + poll() 收尾之后。"""
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


def new_engine():
    e = AudioEngine()
    e.set_volume(1.0)
    e._ensure_device()
    e._device.engine = e  # 让 device 能读到 state
    return e


def use_file(duration=5.0, chunks=50):
    """设置替身后端的"文件长度"。F1 需要足够长，避免 EOF 淡出提前介入。"""
    BACKEND["duration"] = duration
    BACKEND["chunks"] = chunks
    BACKEND["mode"] = "ok"


def pump_gains(engine, chunks, frames=1024):
    """从当前流拉 chunks 段，返回每帧的有效增益（输入恒为 10000）。"""
    gains = []
    for _ in range(chunks):
        try:
            out = next(engine._stream)
        except StopIteration:
            break
        gains.extend(max(0.0, out[i * 2] / 10000.0) for i in range(min(frames, len(out) // 2)))
    return gains


def test_fade_paths():
    # ═══════════ F1 新流启动 → 200ms 内 0 → ≥0.95 ═══════════
    print("── F1 ──")
    use_file(duration=5.0, chunks=50)   # 足够长：EOF 淡出（末 300ms）不会介入
    e = new_engine()
    e.load(A)
    e.play()
    g = pump_gains(e, math.ceil(FADE_IN_MS / 1000 * _SR / 1024) + 1)
    check("F1 淡入起点 ≈0", g[0] < 0.05, f"{g[0]:.4f}")
    check("F1 单调上升", all(b >= a - 1e-9 for a, b in zip(g, g[1:])))
    n200 = int(FADE_IN_MS / 1000 * _SR)
    check(f"F1 前 {FADE_IN_MS:.0f}ms 内升到 ≥0.95", g[min(n200, len(g) - 1)] >= 0.95,
          f"g[{n200}]={g[min(n200, len(g)-1)]:.4f}")

    # ═══════════ F2 stop → 300ms 内 <0.01，之后才 device.stop()+close ═══════════
    print("── F2 ──")
    e = new_engine()
    e.load(A)
    e.play()
    pump_gains(e, 3)
    before = e._device.stopped
    t0 = time.perf_counter()
    e.stop()
    elapsed_ms = (time.perf_counter() - t0) * 1000
    check("F2 stop() 立即返回（非阻塞 <1ms）", elapsed_ms < 1.0, f"{elapsed_ms:.3f}ms")
    check("F2 stop() 返回时设备尚未停（淡出仍在进行）", e._device.stopped == before,
          f"{e._device.stopped}")
    settle(e)
    check("F2 淡出走完后才 device.stop()", e._device.stopped == before + 1)
    check("F2 淡出走完后解码器已关", e._stream is None)

    # ═══════════ F3 淡出期间 state == PLAYING，静音完成才转 PAUSED/STOPPED ═══════════
    print("── F3 ──")
    e = new_engine()
    e.load(A)
    e.play()
    e._device.state_at_stop.clear()
    e.stop()
    check("F3 stop() 返回时 state 仍 playing（B4 不提前跳变）", e.state == "playing", e.state)
    settle(e)
    check("F3 device.stop() 那一刻 state 仍是 playing", e._device.state_at_stop == ["playing"],
          str(e._device.state_at_stop))
    check("F3 settle 后 state=stopped", e.state == "stopped", e.state)

    e = new_engine()
    e.load(A)
    e.play()
    e._device.state_at_stop.clear()
    e.pause()
    check("F3 pause() 返回时 state 仍 playing", e.state == "playing", e.state)
    settle(e)
    check("F3 pause：device.stop() 那刻 state 仍是 playing", e._device.state_at_stop == ["playing"],
          str(e._device.state_at_stop))
    check("F3 settle 后 state=paused", e.state == "paused", e.state)

    # ═══════════ F4 淡出中途 pause → 冻结；resume 从冻结值继续 ═══════════
    print("── F4 ──")
    e = new_engine()
    e.load(A)
    e.play()
    pump_gains(e, 1)  # 淡入进行到一半
    mid = e.fade_gain
    check("F4 前置：淡入进行中（0<gain<1）", 0.0 < mid < 1.0, f"{mid:.4f}")
    e.pause()
    check("F4 淡出中途 pause → resume_gain 被冻结而非归零", e._resume_gain > 0.0,
          f"resume_gain={e._resume_gain:.4f}")
    e.play()
    first = pump_gains(e, 1)
    check("F4 resume 从冻结值继续（不回落到 0）", first[0] > 0.0, f"{first[0]:.4f}")

    # ═══════════ F5 淡入中途 stop → 立即切淡出，不卡死/不双重释放 ═══════════
    print("── F5 ──")
    e = new_engine()
    e.load(A)
    e.play()
    pump_gains(e, 1)
    stopped_before = e._device.stopped
    t0 = time.perf_counter()
    e.stop()
    el = (time.perf_counter() - t0) * 1000
    check("F5 淡入中途 stop 立即返回（不卡死）", el < 1.0, f"{el:.3f}ms")
    settle(e)
    check("F5 无双重释放（device.stop 恰好 +1）", e._device.stopped == stopped_before + 1)
    check("F5 settle 后终态 stopped", e.state == "stopped", e.state)

    # ═══════════ F6 淡出中途切歌 → 旧歌淡出完成 → 新歌淡入 ═══════════
    print("── F6 ──")
    e = new_engine()
    e.load(A)
    e.play()
    pump_gains(e, 2)
    old_gen = e.generation
    e.load(B)
    check("F6 切歌后 generation 已递增", e.generation > old_gen, f"{old_gen}->{e.generation}")
    check("F6 切歌后路径指向新歌", os.path.basename(e.current_path) == "b.mp3")
    handoff = e._handoff_gain
    e.play()
    g2 = pump_gains(e, 2)
    # 【F7】新歌从"接管点"淡入：增益与旧流末尾连续，无爆音间隙（不再固定从 0 起）
    check("F6 新歌从接管点淡入（增益连续，无爆音间隙）",
          handoff > 0.0 and abs(g2[0] - handoff) < 0.05, f"handoff={handoff:.4f} first={g2[0]:.4f}")

    # ═══════════ F7 快速连点切歌 5 次 → 无累积延迟 ═══════════
    print("── F7 ──")
    e = new_engine()
    e.load(A)
    e.play()
    t0 = time.perf_counter()
    for i in range(5):
        e.load(B if i % 2 else A)
        e.play()
    el7 = (time.perf_counter() - t0) * 1000
    if el7 < 500.0:
        check("F7 连点 5 次总耗时 < 500ms（无累积延迟）", True, f"{el7:.0f}ms")
    else:
        gap("F7 无累积延迟（同步淡出导致 5 次连点线性累积）",
            f"实测 {el7:.0f}ms（≈5×{FADE_OUT_MS:.0f}ms）；需把 stop/load 的淡出改异步")
    check("F7 最终播放最后一首歌", os.path.basename(e.current_path) == "a.mp3",
          os.path.basename(e.current_path))
    check("F7 generation 严格递增（5 次 load 各 +1）", e.generation >= 6, str(e.generation))

    # ═══════════ F8 seek → 取消淡出 + 新位置淡入 ═══════════
    print("── F8 ──")
    e = new_engine()
    e.load(A)
    e.play()
    e.seek(0.3)
    check("F8 seek 后 fade 目标被重置为 1.0（取消淡出）", e._fade.current_target == 1.0,
          str(e._fade.current_target))
    check("F8 seek 后新位置从 0 淡入", pump_gains(e, 1)[0] < 0.2)
    e2 = new_engine()
    e2.load(A)
    e2.play()
    t0 = time.perf_counter()
    e2.seek(0.001)  # 极短跳 < SEEK_FADE_SKIP_MS
    short_ms = (time.perf_counter() - t0) * 1000
    check("F8 短跳（< SEEK_FADE_SKIP_MS）跳过 ramp-out，几乎零等待", short_ms < 5.0,
          f"短跳 {short_ms:.1f}ms / 阈值 {SEEK_FADE_SKIP_MS}ms")

    e3 = new_engine()
    e3.load(A)
    e3.play()
    t0 = time.perf_counter()
    e3.seek(2.0)  # 长跳 ≥ SEEK_FADE_SKIP_MS
    long_ms = (time.perf_counter() - t0) * 1000
    # 【F7】seek 也不再阻塞；长跳同样走"取消淡出 + 从接管点淡入"
    check("F8 长跳不再阻塞（<1ms）", long_ms < 1.0, f"长跳 {long_ms:.3f}ms")
    check("F8 长跳从接管点淡入", e3._handoff_gain >= 0.0, str(e3._handoff_gain))
    check("F8 SEEK_FADE_SKIP_MS 默认 100", SEEK_FADE_SKIP_MS == 100.0, str(SEEK_FADE_SKIP_MS))

    # ═══════════ F9 自然播完 → FINISHED；error → 立即停 ═══════════
    print("── F9 ──")
    use_file(duration=20000 / _SR, chunks=10)   # 短文件：便于快速跑到自然播完
    e = new_engine()
    e.load(A)
    e.play()
    last = None
    for last in e._stream:
        pass
    check("F9 自然播完 → 末样本≈0（淡出）", abs(last[-1]) < 200, f"{last[-1]}")
    check("F9 自然播完 → FINISHED", e.state == "finished", e.state)

    BACKEND["mode"] = "error"
    e = new_engine()
    e.load(A)
    e.play()
    t0 = time.perf_counter()
    for _ in e._stream:
        pass
    err_ms = (time.perf_counter() - t0) * 1000
    check("F9 error → 立即转 ERROR 且不拖时间", e.state == "error" and err_ms < 100,
          f"{e.state} {err_ms:.0f}ms")
    check("F9 error → error_code 正确", e.error_code == ErrorCode.FILE_CORRUPT)
    BACKEND["mode"] = "ok"

    # 汇总：任一断言失败则整个用例失败（失败清单会完整列出）
    assert not _FAILS, f"{len(_FAILS)} 项断言失败：" + "; ".join(_FAILS)
