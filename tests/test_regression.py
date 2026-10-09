"""test_regression.py —— R01~R16 合并回归清单。

    python tests/test_regression.py

覆盖：generation 单调/丢弃/防 ABA、解码器生命周期与调用顺序、
FINISHED/ERROR 语义、回调恰好一次、日志只在主线程、静态卫生。
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
import time
import types

_SR = 44100
import _audio_stub  # noqa: E402

# 共享替身后端：sys.modules["miniaudio"] 只在**首次 import 引擎**时生效，
# 因此所有套件必须用同一个替身（差异只体现在 configure() 的参数上），
# 否则后加载的套件会拿到先加载套件的后端。
_audio_stub.install()

pytestmark = [pytest.mark.engine, pytest.mark.regression]
BACKEND = _audio_stub.BACKEND
EVENTS = _audio_stub.EVENTS
CLOSES = _audio_stub.CLOSES
FakeDevice = _audio_stub.FakeDevice

_SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src")
sys.path.insert(0, _SRC)

from player.engine.player import AudioEngine  # noqa: E402
from player.engine.states import ErrorCode  # noqa: E402

_FAILS: list[str] = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (("  " + extra) if extra else ""))
    if not cond:
        _FAILS.append(name)


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


def test_regression_r01_r16():
    # ══════════ R01~R09 generation 语义 ══════════
    print("── R01~R09 generation ──")
    e = new_engine()
    e.load(A)
    g0 = e.generation
    seq = [g0]
    e.play(); seq.append(e.generation)
    e.seek(1.0); seq.append(e.generation)
    e.load(B); seq.append(e.generation)
    e.stop(); seq.append(e.generation)
    check("R01 play/seek/load/stop 各一次 → 严格递增 4 次", e.generation == g0 + 4, str(seq))
    check("R01 严格递增（无回退）", all(b > a for a, b in zip(seq, seq[1:])), str(seq))

    e = new_engine()
    e.load(A)
    e.play()
    g = e.generation
    e.play(); e.play()
    check("R02 PLAYING 下被守卫挡回的 play() → generation 不变", e.generation == g, f"{g}->{e.generation}")

    e = new_engine()
    e.load(A)
    e.play()
    gen_now = e.generation
    before = e._frames_played
    stale = e._pcm_stream(0, gen_now - 1)
    for _ in stale:
        pass
    check("R03 旧代次进度回调被丢弃", e._frames_played == before, f"{before}->{e._frames_played}")

    e = new_engine()
    e.load(A)
    e.play()
    fired = []
    e.on_finished = lambda: fired.append(1)
    stale = e._pcm_stream(0, e.generation - 1)
    for _ in stale:
        pass
    e.poll()
    check("R04 旧代次 EOF 不触发 finished / 不切歌", fired == [] and e.state == "playing",
          f"{fired} {e.state}")

    e = new_engine()
    e.load(A)
    e.play()
    old_gen = e.generation
    e.seek(1.0)
    target = int(1.0 * _SR)
    stale = e._pcm_stream(0, old_gen)
    for _ in stale:
        pass
    check("R05 seek 后旧流迟到回调不污染位置", e._frames_played == target, str(e._frames_played))

    e = new_engine()
    e.load(A)
    e.play()
    gen0 = e.generation
    for _ in e._stream:
        pass
    e.poll()
    e.play()
    check("R06 FINISHED 后 play() → 从头重播、进度归零",
          e.state == "playing" and e._frames_played == 0 and e.generation == gen0 + 1,
          f"state={e.state} frames={e._frames_played} gen={gen0}->{e.generation}")

    BACKEND.update(duration=5.0, chunks=50, mode="ok")
    e = new_engine()
    e.load(A)
    e.play()
    old_gen = e.generation
    e.load(B)
    stale = e._pcm_stream(0, old_gen)
    for _ in stale:
        pass
    check("R07 旧歌迟到回调不污染新歌标题/时长/进度",
          os.path.basename(e.current_path) == "b.mp3" and e.duration == 5.0 and e._frames_played == 0,
          f"{os.path.basename(e.current_path)} {e.duration} {e._frames_played}")

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
    check("R08 连点 seek×10 无异常", err is None, repr(err))
    check("R08 终态与最后一次目标一致", e._frames_played == int(targets[-1] * _SR), str(e._frames_played))
    check("R08 代次恰好 +10", e.generation == g + 10, f"{g}->{e.generation}")

    e = new_engine()
    e.load(A)
    e.play()
    g = e.generation
    e.stop()          # 【F7】非阻塞：只置淡出意图并立即返回
    settle(e)         # 推进淡出 + poll() 收尾
    g1 = e.generation
    check("R09 stop()（settle 后）→ 代次 +1、不清零", g1 == g + 1, f"{g}->{g1}")
    e.stop()          # 已 STOPPED → 立即完成
    settle(e)
    check("R09 再次 stop → 代次再 +1", e.generation == g1 + 1, f"{g1}->{e.generation}")
    e._generation = 2 ** 70
    e.stop()
    check("R09 大数仍正常 +1（非定长，防 ABA）", e.generation == 2 ** 70 + 1, str(e.generation))

    # ══════════ R10~R12 解码器生命周期 ══════════
    print("── R10~R12 生命周期 ──")
    EVENTS.clear()
    e = new_engine()
    e.load(A)
    e.play()
    _ = next(e._stream)
    EVENTS.clear()
    e.stop()
    settle(e)   # 【F7】淡出走完 + poll() 收尾后才 device.stop() + 关解码器
    check("R10 顺序：先 device.stop() 再关解码器", EVENTS[:2] == ["device.stop", "raw.close"], str(EVENTS))

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
    check("R11 六条路径都收敛到 _close_decoder", all(v >= 1 for v in paths_hit.values()), str(paths_hit))
    check("R11 关闭后无残留流（各路径 _stream 为 None）", e._stream is None, str(e._stream))

    e = new_engine()
    e.load(A)
    e.play()
    old = e._stream
    e.stop()
    settle(e)   # 【F7】先让淡出跑完并收尾
    check("R12 关闭后原生成器 next() 抛 StopIteration", exhausted(old))
    n0 = e._device.started
    e.play()
    check("R12 关闭后 play() 能重新打开流", e._device.started == n0 + 1 and e._stream is not None,
          f"started {n0}->{e._device.started}")

    # ══════════ R13~R15 结束/错误语义 ══════════
    print("── R13~R15 结束/错误 ──")
    BACKEND.update(duration=20000 / _SR, chunks=10, mode="ok")
    e = new_engine()
    e.load(A)
    e.play()
    count = []
    e.on_finished = lambda: count.append(1)
    for _ in e._stream:
        pass
    check("R13 播完 → FINISHED", e.state == "finished", e.state)
    e.poll()
    e.poll()
    check("R13 poll() 消费 → on_finished 恰好一次", count == [1], str(count))

    recs: list[str] = []


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
    check("R14 解码异常 → state=ERROR", e.state == "error", e.state)
    check("R14 ErrorCode=FILE_CORRUPT", e.error_code == ErrorCode.FILE_CORRUPT, str(e.error_code))
    check("R14 音频线程内零日志（约束2）", audio_recs == [], str(audio_recs))
    e.poll()
    main_recs = [m for th, m in recs if th != "fake-audio-thread"]
    check("R14 主线程 poll() 记录 logging.error 且带 code=",
          any("code=file_corrupt" in m for m in main_recs), str(main_recs))

    BACKEND["mode"] = "ok"
    n0 = e._device.started
    e.play()
    check("R15 ERROR 后 play() → 从头重试", e.state == "playing" and e._frames_played == 0
          and e._device.started == n0 + 1, f"{e.state} {e._frames_played}")

    # ══════════ R16 静态卫生 ══════════
    print("── R16 静态卫生 ──")
    _src_files = []
    for _root, _dirs, _files in os.walk(os.path.join(_SRC, "player")):
        for _f in _files:
            if _f.endswith(".py"):
                _src_files.append(os.path.join(_root, _f))
    compile_err = []
    for _f in _src_files:
        try:
            py_compile.compile(_f, doraise=True)
        except Exception as ex:  # noqa: BLE001
            compile_err.append(f"{_f}: {ex}")
    check("R16 全部源文件语法编译通过", not compile_err, str(compile_err))

    print_offenders = []
    for _f in _src_files:
        with open(_f, encoding="utf-8") as _fh:
            if "print(" in _fh.read():
                print_offenders.append(os.path.basename(_f))
    check("R16 无 print 残留", not print_offenders, str(print_offenders))

    _mutable_globals = []
    for _f in _src_files:
        _tree = ast.parse(open(_f, encoding="utf-8").read(), _f)
        for _node in _tree.body:
            if isinstance(_node, ast.Assign) and isinstance(_node.value, (ast.List, ast.Dict, ast.Set)):
                for _t in _node.targets:
                    if isinstance(_t, ast.Name):
                        _mutable_globals.append(f"{os.path.basename(_f)}:{_t.id}")
    check("R16 无模块级可变全局（list/dict/set）", not _mutable_globals, str(_mutable_globals))

    print("── R17 审计修复（本轮新增）──")
    _e = new_engine()
    _e.load(A)
    _e.play()
    _gen = _e.generation
    with _e._lock:
        _e._smooth_gain = 0.5
    _e._apply_gain_envelope(array.array("h", [10000, 10000] * 512), 512, generation=_gen - 1)
    check("R17 旧代次的 _smooth_gain 回写被丢弃（不覆盖新流初值）", _e._smooth_gain == 0.5,
          str(_e._smooth_gain))
    _e._apply_gain_envelope(array.array("h", [10000, 10000] * 512), 512, generation=_gen)
    check("R17 当前代次的回写正常生效", _e._smooth_gain != 0.5, str(_e._smooth_gain))

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
    check("R17 device.start() 失败不留悬挂生成器（防泄漏）", _e._stream is None, str(_e._stream))

    # 汇总：任一断言失败则整个用例失败（失败清单会完整列出）
    assert not _FAILS, f"{len(_FAILS)} 项断言失败：" + "; ".join(_FAILS)
