"""test_f7_interrupt.py —— F7 中断接管 + B1~B5 边界路径。

    python tests/test_f7_interrupt.py

断言不写死 300/200 等魔法数，一律读常量/构造参数（约束 4）。
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
    VOLUME_SMOOTH_MS,
    AudioEngine,
)
from player.engine.states import ErrorCode  # noqa: E402

_FAILS: list[str] = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (("  " + extra) if extra else ""))
    if not cond:
        _FAILS.append(name)


TMP = tempfile.mkdtemp()
A = os.path.join(TMP, "a.mp3")
B = os.path.join(TMP, "b.mp3")
for _p in (A, B):
    with open(_p, "wb"):
        pass


def new_engine(**kw):
    e = AudioEngine(**kw)
    e.set_volume(1.0)
    return e


def _exh(gen) -> bool:
    """生成器已 close/耗尽 → next() 抛 StopIteration。"""
    try:
        next(gen)
    except StopIteration:
        return True
    except Exception:
        return False
    return False


def pump(e, chunks=1):
    """推进当前流 chunks 段，返回每帧增益（输入恒为 10000）。"""
    gains = []
    for _ in range(chunks):
        gen = e._stream
        if gen is None:
            break
        try:
            out = next(gen)
        except StopIteration:
            break
        gains.extend(max(0.0, out[i * 2] / 10000.0) for i in range(min(2048, len(out) // 2)))
    return gains


def settle(e, limit=600):
    """把淡出推到底并让 poll() 收尾（模拟真实设备持续拉流 + UI 定时 poll）。"""
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
    raise AssertionError("settle 未收敛")


def test_f7_interrupt():
    # ═══════════ F7-1 / F7-2 延迟与复杂度 ═══════════
    print("── F7-1 / F7-2 ──")


    def burst(n):
        e = new_engine()
        e.load(A)
        e.play()
        t0 = time.perf_counter()
        for i in range(n):
            e.load(B if i % 2 else A)
            e.play()
        return (time.perf_counter() - t0) * 1000, e


    el5, e5 = burst(5)
    check("F7-1 5 连点切歌 < 500ms", el5 < 500.0, f"{el5:.2f}ms（原 1528ms）")

    xs, ys = list(range(1, 11)), []
    for n in xs:
        el, _ = burst(n)
        ys.append(el)
    # 线性拟合斜率：应接近 0（O(1)）
    n_ = len(xs)
    mx, my = sum(xs) / n_, sum(ys) / n_
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sum((x - mx) ** 2 for x in xs)
    check("F7-2 耗时随点击次数 O(1)（斜率≈0）", abs(slope) < 1.0,
          f"slope={slope:.4f} ms/次；y={[round(v,2) for v in ys]}")

    # ═══════════ F7-3 控制路径非阻塞 ═══════════
    print("── F7-3 ──")
    e = new_engine()
    e.load(A)
    e.play()
    t0 = time.perf_counter()
    e.stop()
    t_stop = (time.perf_counter() - t0) * 1000
    check("F7-3 stop() < 1ms", t_stop < 1.0, f"{t_stop:.3f}ms")

    e = new_engine()
    e.load(A)
    e.play()
    t0 = time.perf_counter()
    e.pause()
    t_pause = (time.perf_counter() - t0) * 1000
    check("F7-3 pause() < 1ms", t_pause < 1.0, f"{t_pause:.3f}ms")

    e = new_engine()
    e.load(A)
    e.play()
    t0 = time.perf_counter()
    e.load(B)
    t_load = (time.perf_counter() - t0) * 1000
    check("F7-3 load() < 1ms", t_load < 1.0, f"{t_load:.3f}ms")

    _src_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src",
                             "player", "engine", "player.py")
    _raw = open(_src_path, encoding="utf-8").read()
    # 剥离注释后再扫，避免注释里出现这些词造成误判
    _code = "\n".join(line.split("#")[0] for line in _raw.splitlines())
    check("F7-3 控制路径无 sleep/join/while 等待",
          "time.sleep" not in _code and ".join(" not in _code and "while " not in _code,
          "源码（去注释）扫描")

    # ═══════════ F7-4 cancel 后 step 返回 0 ═══════════
    print("── F7-4 ──")
    e = new_engine()
    e.load(A)
    e.play()
    pump(e, 1)
    env = e._fade
    env.cancel()
    check("F7-4 cancel 后 is_cancelled", env.is_cancelled)
    check("F7-4 cancel 后 step() 返回 0", env.step(512) == 0.0, str(env.step(512)))
    check("F7-4 cancel 后 block() 全 0", env.block(512) == (0.0, 0.0, 0), str(env.block(512)))
    check("F7-4 cancel 后 current_gain 为 0", env.current_gain == 0.0, str(env.current_gain))

    # ═══════════ F7-5 接管起点 = 旧 envelope.current_gain，曲线连续 ═══════════
    print("── F7-5 ──")
    e = new_engine()
    e.load(A)
    e.play()
    pump(e, 2)                      # 淡入进行到一半
    old_eff = e._smooth_gain * e._fade.current_gain
    e.pause()                       # 进入淡出
    pump(e, 1)                      # 淡出走一点
    mid = e._fade.current_gain
    e.load(B)                       # 中断接管
    handoff = e._handoff_gain
    check("F7-5 接管增益 = 中断时的 envelope 有效增益", abs(handoff - mid) < 1e-9,
          f"handoff={handoff:.6f} mid={mid:.6f}")
    e.play()
    g0 = pump(e, 1)
    check("F7-5 新流首帧增益 ≈ 接管点（无跳变）", abs(g0[0] - handoff) < 0.02,
          f"first={g0[0]:.6f} handoff={handoff:.6f}")
    check("F7-5 增益曲线连续（首帧 > 0，非从静音起跳）", g0[0] > 0.0, f"{g0[0]:.6f}")

    # ═══════════ F7-6 / F7-7 / F7-8 / F7-9 5 连点终态 ═══════════
    print("── F7-6 ~ F7-9 ──")
    CLOSES.clear()
    e = new_engine()
    e.load(A)
    e.play()
    g0 = e.generation
    old_gen = e.generation
    old_stream = e._stream
    for i in range(5):
        e.load(B if i % 2 else A)
        e.play()
    check("F7-6 终态播放最后一首歌", os.path.basename(e.current_path) == "a.mp3",
          os.path.basename(e.current_path))
    check("F7-6 duration 与新歌一致", e.duration == BACKEND["duration"], str(e.duration))
    check("F7-7 generation 严格递增（≥5 次）", e.generation >= g0 + 5, f"{g0}->{e.generation}")
    check("F7-7 无异常、状态正常", e.state == "playing", e.state)

    stale = e._pcm_stream(0, old_gen)
    before = e._frames_played
    for _ in stale:
        pass
    check("F7-8 旧流迟到回调被代次校验丢弃", e._frames_played == before,
          f"{before}->{e._frames_played}")
    check("F7-9 中断后旧生成器 next() 抛 StopIteration", _exh(old_stream))

    n_close = len(CLOSES)
    e2 = new_engine()
    e2.load(A)
    e2.play()
    CLOSES.clear()
    e2.stop()
    settle(e2)
    check("F7-9 淡出中断后旧解码器恰好关闭一次", len(CLOSES) == 1, str(CLOSES))
    check("F7-9 收尾后 _stream 已清空（无孤儿）", e2._stream is None)

    # ═══════════ B1~B5 边界路径 ═══════════
    print("── B1~B5 ──")
    # B1 淡出中途 seek
    e = new_engine()
    e.load(A)
    e.play()
    pump(e, 2)
    e.pause()
    pump(e, 1)
    e.seek(2.0)                     # 长跳 ≥ 阈值
    check("B1 淡出中途 seek → 淡出被取消", not e.fade_pending)
    check("B1 位置=seek 目标（未推进前）", e._frames_played == int(2.0 * _SR), str(e._frames_played))
    check("B1 seek 后新流从接管点淡入（首帧>0）", pump(e, 1)[0] > 0.0)
    e = new_engine()
    e.load(A)
    e.play()
    e.pause()
    pump(e, 1)
    e.seek(0.001)                   # 短跳 < 阈值
    check("B1 短跳（< SEEK_FADE_SKIP_MS）跳过淡出，首帧≈0", pump(e, 1)[0] < 0.2,
          f"阈值={SEEK_FADE_SKIP_MS}ms")

    # B2 淡出中途 error
    BACKEND["mode"] = "error"
    e = new_engine()
    e.load(A)
    e.play()
    e.pause()
    t0 = time.perf_counter()
    for _ in e._stream:
        pass
    e.poll()
    t_err = (time.perf_counter() - t0) * 1000
    check("B2 淡出中途 error → 立即 ERROR，不等待淡出", e.state == "error" and t_err < FADE_OUT_MS,
          f"{e.state} {t_err:.1f}ms")
    check("B2 error_code 正确", e.error_code == ErrorCode.FILE_CORRUPT, str(e.error_code))
    BACKEND["mode"] = "ok"

    # B3 淡出中途 shutdown
    e = new_engine()
    e.load(A)
    e.play()
    e.pause()
    err = None
    try:
        e.shutdown()
    except Exception as ex:  # noqa: BLE001
        err = ex
    check("B3 淡出中途 shutdown 跳过一切直关，无异常", err is None and e.state == "stopped",
          f"{err} {e.state}")

    # B4 淡出完成瞬间 state 才转
    e = new_engine()
    e.load(A)
    e.play()
    e.stop()
    check("B4 淡出期间 state 仍为 PLAYING", e.state == "playing", e.state)
    check("B4 淡出期间 fade_pending=True", e.fade_pending)
    settle(e)
    check("B4 淡出完成后才转 STOPPED", e.state == "stopped", e.state)

    # B5 淡出期间 mute/unmute 不打断淡出曲线
    e = new_engine()
    e.load(A)
    e.play()
    pump(e, 2)
    e.pause()
    g_before = e._fade.current_gain
    e.set_muted(True)
    g_muted = e._fade.current_gain
    e.set_muted(False)
    g_after = e._fade.current_gain
    check("B5 mute/unmute 不打断淡出（envelope 目标仍为 0）",
          e._fade.current_target == 0.0, str(e._fade.current_target))
    check("B5 mute 不重置淡出进度（单调下降）", g_muted <= g_before + 1e-9 and g_after <= g_muted + 1e-9,
          f"{g_before:.4f}->{g_muted:.4f}->{g_after:.4f}")

    # 汇总：任一断言失败则整个用例失败（失败清单会完整列出）
    assert not _FAILS, f"{len(_FAILS)} 项断言失败：" + "; ".join(_FAILS)
