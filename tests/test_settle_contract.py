"""test_settle_contract.py —— D2 修复的契约自检。

【为什么单独一个文件】D2（settle 静默耗尽）是**第 17 轮审计里唯一能直接
制造假绿的结构性缺陷**。它的修复必须有一条**能证明它真的被修了**的断言，
而不是只有一行注释。这条断言就是 T-D2-1。

本文件与业务套件同构，但只测 settle 自己的契约。
"""

from __future__ import annotations

import os
import sys

import pytest

# 【D1】替身由 conftest 顶层唯一入口注入，这里只断言已在位。
assert sys.modules.get("miniaudio") is not None, "替身未注入"

pytestmark = pytest.mark.engine

from fixtures.engine_factory import create_engine, settle  # noqa: E402


# ══════════════════ T-D2-1：耗尽 limit 必须显式失败 ══════════════════
def test_t_d2_1_exhausted_limit_raises_not_silent(tmp_path):
    """T-D2-1【D2 修复证据】：settle(limit=1) 必须**抛 AssertionError**，
    而不是静默返回。

    这是 D2 的核心：修复前 `settle()` 耗尽 limit 后安静 return，
    调用方无法区分"已收敛"与"超限放弃"→ 断言退化为提示。

    实测参考值：fade_out=300ms / chunk=45.35ms → 真实需 ~7 步才收敛，
    故 limit=1 必定耗尽。若本条**没有**抛异常，说明 D2 修复失效。
    """
    p = tmp_path / "a.mp3"
    p.write_bytes(b"")
    engine = create_engine()
    try:
        engine.load(str(p))
        engine.play()
        engine.stop()          # 非阻塞：只置淡出意图
        with pytest.raises(AssertionError) as ei:
            settle(engine, limit=1)
        msg = str(ei.value)
        # 报错必须带诊断信息，否则"抛了但没法定位"等于没修
        assert "未在 1 步内收敛" in msg, msg
        assert "state=" in msg and "fade_pending=" in msg, msg
    finally:
        engine.shutdown()


def test_t_d2_2_limit_1_actually_leaves_unsettled_state(tmp_path):
    """T-D2-2：证明 limit=1 **确实不足以收敛**（否则 T-D2-1 是假阳性）。

    若引擎在 limit=1 时恰好收敛，那 T-D2-1 测的就不是"耗尽"路径，
    整个契约断言毫无意义。这条用"绕过 settle、手动走 1 步"来证明。
    """
    p = tmp_path / "a.mp3"
    p.write_bytes(b"")
    engine = create_engine()
    try:
        engine.load(str(p))
        engine.play()
        engine.stop()
        # 手动模拟"只推进 1 步"（与 settle 内部同构）
        gen = engine._stream
        if gen is not None:
            try:
                next(gen)
            except StopIteration:
                pass
        engine.poll()
        assert engine.fade_pending, (
            "1 步后 fade_pending 已为 False —— limit=1 足够收敛，"
            "则 T-D2-1 测的不是耗尽路径"
        )
        assert engine.state != "stopped", (
            f"1 步后已到终态 {engine.state} —— T-D2-1 前提不成立"
        )
    finally:
        engine.shutdown()


# ══════════════════ T-D2-3：正常 limit 必须能收敛 ══════════════════
def test_t_d2_3_normal_limit_converges_and_returns_true(tmp_path):
    """T-D2-3：limit 充足时必须收敛，且返回 True（回归保护）。

    防止"修复过度"——把 settle 改成永远抛异常会让所有业务用例变红。
    """
    p = tmp_path / "a.mp3"
    p.write_bytes(b"")
    engine = create_engine()
    try:
        engine.load(str(p))
        engine.play()
        engine.stop()
        assert settle(engine, limit=900) is True
        assert engine.state == "stopped", engine.state
        assert not engine.fade_pending
    finally:
        engine.shutdown()


@pytest.mark.parametrize("limit,should_converge", [(1, False), (2, False), (3, False),
                                                   (5, False), (10, True), (900, True)])
def test_t_d2_4_convergence_threshold_is_bracketed(tmp_path, limit, should_converge):
    """T-D2-4：收敛阈值被夹在 (5, 10] 区间内 —— 上界不被撑大、下界不被缩水。

    实测：真实需 ~7 步。这条把"settle 到底需要多少步"钉成回归基线，
    一旦引擎的 chunk 粒度或淡出时长变了，这里会红，提醒重新评估 limit。
    """
    p = tmp_path / "a.mp3"
    p.write_bytes(b"")
    engine = create_engine()
    try:
        engine.load(str(p))
        engine.play()
        engine.stop()
        if should_converge:
            settle(engine, limit=limit)
            assert engine.state == "stopped"
        else:
            with pytest.raises(AssertionError):
                settle(engine, limit=limit)
    finally:
        engine.shutdown()


def test_t_d2_5_stopped_engine_settles_immediately(tmp_path):
    """T-D2-5：从未播放的引擎上 settle 是 no-op，不应误抛（边界保护）。"""
    engine = create_engine()
    try:
        assert settle(engine, limit=1) is True
    finally:
        engine.shutdown()
