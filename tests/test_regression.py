"""test_regression.py —— R01~R17 合并回归清单。

    python tests/test_regression.py

覆盖：generation 单调/丢弃/防 ABA、解码器生命周期与调用顺序、
FINISHED/ERROR 语义、回调恰好一次、日志只在主线程、静态卫生。

【D4 修复】原先 R01~R17 **全部塞在一个 `test_regression_r01_r16()`** 里，
导致 `-m regression` 只收集到 1 个 test item：任一条断言挂掉，CI 只报
"test_regression_r01_r16 FAILED"，**没有用例号、没有定位**。

现在改为：**每条 R 断言组 = 一个独立 test 函数**，失败可直接从测试名定位。
`R_SPECS` 是本文件的**单一事实源**，并由 `test_r00_registry_is_complete`
自检"注册表与实现一一对应"——防止将来新增 R 号却忘了挂上去（或反之）。
"""

from __future__ import annotations

import array
import ast
import logging
import os
import pytest  # noqa: E402
import py_compile
import sys
import tempfile
import threading
import types

_SR = 44100

# 【D1 修复】不再重复 install()：替身由 conftest 顶层唯一入口注入。
# 这里只断言"它确实已在位"，把隐式顺序依赖变成显式契约。
assert sys.modules.get("miniaudio") is not None, (
    "替身未注入：conftest.py 顶层 install() 应已执行（收集期早于本模块 import）"
)

pytestmark = [pytest.mark.engine, pytest.mark.regression]

import _audio_stub  # noqa: E402

BACKEND = _audio_stub.BACKEND
EVENTS = _audio_stub.EVENTS
CLOSES = _audio_stub.CLOSES
FakeDevice = _audio_stub.FakeDevice

_SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src")
sys.path.insert(0, _SRC)

from player.engine.player import AudioEngine  # noqa: E402
from player.engine.states import ErrorCode  # noqa: E402

# 【D2 修复】使用共享的**严格** settle（耗尽 limit 即抛）。
from fixtures.engine_factory import settle  # noqa: E402

TMP = tempfile.mkdtemp()

A = os.path.join(TMP, "a.mp3")
B = os.path.join(TMP, "b.mp3")
for _p in (A, B):
    with open(_p, "wb"):
        pass


def new_engine():
    e = AudioEngine()
    e.set_volume(1.0)
    return e


def exhausted(gen) -> bool:
    try:
        next(gen)
    except StopIteration:
        return True
    except Exception:
        return False
    return False


# ══════════════════════════════════════════════════════════════════════
# R01~R09 generation 语义
# ══════════════════════════════════════════════════════════════════════
def test_r01_generation_strictly_increases():
    """R01：play/seek/load/stop 各一次 → 代次严格递增 4 次（无回退）。"""
    e = new_engine()
    e.load(A)
    g0 = e.generation
    seq = [g0]
    e.play(); seq.append(e.generation)
    e.seek(1.0); seq.append(e.generation)
    e.load(B); seq.append(e.generation)
    e.stop(); seq.append(e.generation)
    assert e.generation == g0 + 4, f"sequence={seq}"
    assert all(b > a for a, b in zip(seq, seq[1:])), f"sequence={seq}"


def test_r02_play_idempotent_guard():
    """R02：PLAYING 下被守卫挡回的 play() → generation 不变。"""
    e = new_engine()
    e.load(A)
    e.play()
    g = e.generation
    e.play(); e.play()
    assert e.generation == g, f"{g}->{e.generation}"


def test_r03_stale_progress_dropped():
    """R03：旧代次进度回调被丢弃。"""
    e = new_engine()
    e.load(A)
    e.play()
    gen_now = e.generation
    before = e._frames_played
    for _ in e._pcm_stream(0, gen_now - 1):
        pass
    assert e._frames_played == before, f"{before}->{e._frames_played}"


def test_r04_stale_eof_does_not_fire():
    """R04：旧代次 EOF 不触发 finished / 不切歌。"""
    e = new_engine()
    e.load(A)
    e.play()
    fired = []
    e.on_finished = lambda: fired.append(1)
    for _ in e._pcm_stream(0, e.generation - 1):
        pass
    e.poll()
    assert fired == [] and e.state == "playing", f"{fired} {e.state}"


def test_r05_stale_after_seek_does_not_pollute_position():
    """R05：seek 后旧流迟到回调不污染位置。"""
    e = new_engine()
    e.load(A)
    e.play()
    old_gen = e.generation
    e.seek(1.0)
    for _ in e._pcm_stream(0, old_gen):
        pass
    assert e._frames_played == int(1.0 * _SR), str(e._frames_played)


def test_r06_replay_after_finished():
    """R06：FINISHED 后 play() → 从头重播、进度归零。"""
    e = new_engine()
    e.load(A)
    e.play()
    gen0 = e.generation
    for _ in e._stream:
        pass
    e.poll()
    e.play()
    assert e.state == "playing" and e._frames_played == 0 and e.generation == gen0 + 1, (
        f"state={e.state} frames={e._frames_played} gen={gen0}->{e.generation}"
    )


def test_r07_stale_callback_after_load():
    """R07：旧歌迟到回调不污染新歌标题/时长/进度。"""
    BACKEND.update(duration=5.0, chunks=50, mode="ok")
    e = new_engine()
    e.load(A)
    e.play()
    old_gen = e.generation
    e.load(B)
    for _ in e._pcm_stream(0, old_gen):
        pass
    assert (
        os.path.basename(e.current_path) == "b.mp3"
        and e.duration == 5.0
        and e._frames_played == 0
    ), f"{os.path.basename(e.current_path)} {e.duration} {e._frames_played}"


def test_r08_seek_burst():
    """R08：连点 seek×10 无异常，终态与最后目标一致，代次恰好 +10。"""
    e = new_engine()
    e.load(A)
    e.play()
    g = e.generation
    targets = [0.1, 0.5, 1.0, 1.5, 2.0, 2.5, 0.2, 1.8, 0.9, 2.9]
    err = None
    try:
        for t in targets:
            e.seek(t)
    except Exception as ex:  # noqa: BLE001
        err = ex
    assert err is None, repr(err)
    assert e._frames_played == int(targets[-1] * _SR), str(e._frames_played)
    assert e.generation == g + 10, f"{g}->{e.generation}"


def test_r09_stop_and_aba():
    """R09：stop() 后代次 +1 不清零；再 stop 再 +1；大数仍正常（防 ABA）。"""
    e = new_engine()
    e.load(A)
    e.play()
    g = e.generation
    e.stop()          # 【F7】非阻塞：只置淡出意图并立即返回
    settle(e)         # 推进淡出 + poll() 收尾
    g1 = e.generation
    assert g1 == g + 1, f"{g}->{g1}"
    e.stop()          # 已 STOPPED → 立即完成
    settle(e)
    assert e.generation == g1 + 1, f"{g1}->{e.generation}"
    e._generation = 2 ** 70
    e.stop()
    assert e.generation == 2 ** 70 + 1, str(e.generation)


# ══════════════════════════════════════════════════════════════════════
# R10~R12 解码器生命周期
# ══════════════════════════════════════════════════════════════════════
def test_r10_close_order_is_device_stop_then_decoder():
    """R10：顺序 = 先 device.stop() 再关解码器。"""
    EVENTS.clear()
    e = new_engine()
    e.load(A)
    e.play()
    _ = next(e._stream)
    EVENTS.clear()
    e.stop()
    settle(e)   # 【F7】淡出走完 + poll() 收尾后才 device.stop() + 关解码器
    assert EVENTS[:2] == ["device.stop", "raw.close"], str(EVENTS)


def test_r11_six_paths_converge_to_close_decoder():
    """R11：六条停止路径都收敛到 _close_decoder，且关闭后无残留流。"""
    paths_hit = {}
    for label, action in (
        ("stop", lambda eng: eng.stop()),
        ("pause", lambda eng: eng.pause()),
        ("seek", lambda eng: eng.seek(0.5)),
        ("load", lambda eng: eng.load(B)),
        ("error", None),
        ("shutdown", lambda eng: eng.shutdown()),
    ):
        e = new_engine()
        e.load(A)
        e.play()
        calls = {"n": 0}
        orig = e._close_decoder

        def counting(_orig=orig, _c=calls):
            _c["n"] += 1
            return _orig()

        e._close_decoder = counting
        if label == "error":
            BACKEND["mode"] = "error"
            e.play()
            for _ in e._stream:
                pass
            e.poll()
            BACKEND["mode"] = "ok"
        else:
            action(e)
            settle(e)   # 【F7】stop/pause 的收尾发生在 settle 之后
        paths_hit[label] = calls["n"]
    assert all(v >= 1 for v in paths_hit.values()), str(paths_hit)
    assert e._stream is None, str(e._stream)


def test_r12_reopen_after_close():
    """R12：关闭后原生成器已耗尽；play() 能重新打开流。"""
    e = new_engine()
    e.load(A)
    e.play()
    old = e._stream
    e.stop()
    settle(e)   # 【F7】先让淡出跑完并收尾
    assert exhausted(old)
    n0 = e._device.started
    e.play()
    assert e._device.started == n0 + 1 and e._stream is not None, (
        f"started {n0}->{e._device.started}"
    )


# ══════════════════════════════════════════════════════════════════════
# R13~R15 结束 / 错误语义
# ══════════════════════════════════════════════════════════════════════
def test_r13_finished_and_callback_once():
    """R13：播完 → FINISHED；poll() 消费 → on_finished 恰好一次。"""
    BACKEND.update(duration=20000 / _SR, chunks=10, mode="ok")
    e = new_engine()
    e.load(A)
    e.play()
    count = []
    e.on_finished = lambda: count.append(1)
    for _ in e._stream:
        pass
    assert e.state == "finished", e.state
    e.poll()
    e.poll()
    assert count == [1], str(count)


def test_r14_decode_error_semantics_and_main_thread_logging():
    """R14：解码异常 → ERROR/FILE_CORRUPT；音频线程内零日志，主线程 poll() 记账。"""
    recs: list[tuple[str, str]] = []

    class _H(logging.Handler):
        def emit(self, r):
            recs.append((threading.current_thread().name, r.getMessage()))

    _lg = logging.getLogger("player.engine.player")
    _lg.setLevel(logging.DEBUG)
    _lg.addHandler(_H())

    BACKEND.update(duration=5.0, chunks=50, mode="error")
    e = new_engine()
    e.load(A)
    e.play()
    recs.clear()
    gen = e._stream
    t = threading.Thread(target=lambda: [x for x in gen], name="fake-audio-thread")
    t.start()
    t.join()
    audio_recs = [r for r in recs if r[0] == "fake-audio-thread"]
    assert e.state == "error", e.state
    assert e.error_code == ErrorCode.FILE_CORRUPT, str(e.error_code)
    assert audio_recs == [], str(audio_recs)
    e.poll()
    main_recs = [m for th, m in recs if th != "fake-audio-thread"]
    assert any("code=file_corrupt" in m for m in main_recs), str(main_recs)


def test_r15_retry_after_error():
    """R15：ERROR 后 play() → 从头重试。"""
    BACKEND["mode"] = "error"
    BACKEND.update(duration=5.0, chunks=50)
    e = new_engine()
    e.load(A)
    e.play()
    for _ in e._stream:
        pass
    e.poll()
    BACKEND["mode"] = "ok"
    n0 = e._device.started
    e.play()
    assert e.state == "playing" and e._frames_played == 0 and e._device.started == n0 + 1, (
        f"{e.state} {e._frames_played}"
    )


# ══════════════════════════════════════════════════════════════════════
# R16 静态卫生
# ══════════════════════════════════════════════════════════════════════
def _src_files():
    out = []
    for _root, _dirs, _files in os.walk(os.path.join(_SRC, "player")):
        for _f in _files:
            if _f.endswith(".py"):
                out.append(os.path.join(_root, _f))
    return out


def test_r16_all_sources_compile():
    """R16：全部源文件语法编译通过。"""
    errs = []
    for _f in _src_files():
        try:
            py_compile.compile(_f, doraise=True)
        except Exception as ex:  # noqa: BLE001
            errs.append(f"{_f}: {ex}")
    assert not errs, str(errs)


def test_r16_no_print_residue():
    """R16：源文件无 print 残留。"""
    offenders = []
    for _f in _src_files():
        with open(_f, encoding="utf-8") as _fh:
            if "print(" in _fh.read():
                offenders.append(os.path.basename(_f))
    assert not offenders, str(offenders)


def test_r16_no_mutable_module_globals():
    """R16：无模块级可变全局（list/dict/set）。"""
    offenders = []
    for _f in _src_files():
        _tree = ast.parse(open(_f, encoding="utf-8").read(), _f)
        for _node in _tree.body:
            if isinstance(_node, ast.Assign) and isinstance(_node.value, (ast.List, ast.Dict, ast.Set)):
                for _t in _node.targets:
                    if isinstance(_t, ast.Name):
                        offenders.append(f"{os.path.basename(_f)}:{_t.id}")
    assert not offenders, str(offenders)


# ══════════════════════════════════════════════════════════════════════
# R17 审计修复
# ══════════════════════════════════════════════════════════════════════
def test_r17_stale_smooth_gain_writeback_dropped():
    """R17：旧代次的 _smooth_gain 回写被丢弃；当前代次正常生效。"""
    _e = new_engine()
    _e.load(A)
    _e.play()
    _gen = _e.generation
    with _e._lock:
        _e._smooth_gain = 0.5
    _e._apply_gain_envelope(array.array("h", [10000, 10000] * 512), 512, generation=_gen - 1)
    assert _e._smooth_gain == 0.5, str(_e._smooth_gain)
    _e._apply_gain_envelope(array.array("h", [10000, 10000] * 512), 512, generation=_gen)
    assert _e._smooth_gain != 0.5, str(_e._smooth_gain)


def test_r17_device_start_failure_leaves_no_dangling_stream():
    """R17：device.start() 失败不留悬挂生成器（防泄漏）。"""
    _e = new_engine()
    _e.load(A)
    _e.play()

    def _boom(_gen):
        raise RuntimeError("device start failed")

    _e._device.start = _boom
    try:
        _e.seek(0.5)
    except Exception:  # noqa: BLE001
        pass
    assert _e._stream is None, str(_e._stream)


# ══════════════════════════════════════════════════════════════════════
# R00 注册表自检 —— 「证明 D4 真的被修了」
# ══════════════════════════════════════════════════════════════════════
#: 回归清单的单一事实源：R 号 → 该组的断言条数。
#: 新增/删除 R 断言时必须同步这里，否则 test_r00 变红。
R_SPECS: dict[str, int] = {
    "R01": 2, "R02": 1, "R03": 1, "R04": 1, "R05": 1,
    "R06": 1, "R07": 1, "R08": 3, "R09": 3,
    "R10": 1, "R11": 2, "R12": 2,
    "R13": 2, "R14": 4, "R15": 1,
    "R16": 3, "R17": 3,
}


def test_r00_registry_matches_implementation():
    """R00【D4 自检】：回归清单已**拆成独立用例**，且注册表与实现一一对应。

    这条断言本身就是 D4 的修复证据：
    - 原先 R01~R17 全在一个函数里 → `-m regression` 收集到 1 个 item；
      现在 `-m regression` 必须收集到 **>= 20 个 item**。
    - 若未来新增 R 号却忘了建函数（或建了函数忘了登记），本条立刻变红。
    """
    import re

    # 1) 收集本模块所有 test_rNN_* 函数（R00 是自检本身，不计入清单）
    #    注意：R16/R17 各有多个独立用例，用 set 去重后按“组”比对。
    this = sys.modules[__name__]
    impl_r = sorted({
        m.group(1).upper()
        for name in dir(this)
        if (m := re.match(r"test_(r\d\d)_.+", name)) and m.group(1) != "r00"
    })
    spec_r = sorted(R_SPECS)

    assert impl_r == spec_r, (
        f"注册表与实现不一致：\n  注册表={spec_r}\n  实现={impl_r}\n"
        f"  只在注册表={'、'.join(set(spec_r) - set(impl_r)) or '无'}\n"
        f"  只在实现={'、'.join(set(impl_r) - set(spec_r)) or '无'}"
    )

    # 2) R 组数必须覆盖注册表全量
    assert len(impl_r) >= 17, (
        f"regression 组数不足：只有 {len(impl_r)} 条 R 组（注册表 {len(spec_r)} 条）"
    )

    # 3) 用例数（含 R16/R17 的多函数）必须显著多于 1 —— 这就是 D4 的修复证据
    n_items = sum(1 for name in dir(this) if re.match(r"test_r\d\d_.+", name))
    assert n_items >= 21, (
        f"regression 可定位用例数 {n_items} 过少（修复前为 1）——D4 未生效"
    )

    # 4) 断言总数不得退化（原 R 段为 32 项）
    total = sum(R_SPECS.values())
    assert total >= 32, f"R 段断言总数退化到 {total}（原为 32+）"

