"""引擎 fixture 工厂 + settle 辅助。

⚠️【与提示词原稿的差异 · 此处需要重构 X】
原稿的三处 API 与本项目实际不符，已按真实代码修正：

    原稿                                          实际
    ──────────────────────────────────────────    ──────────────────────────────
    from src.player.engine.player import          from player.engine.player import
        PlayerEngine                                  AudioEngine
        （src 不是包，无 src/__init__.py；            （src 需在 sys.path 上，
         类名也不是 PlayerEngine）                     由 conftest 注入）
    from src.player.engine.constants import       player.engine.player 模块内定义
        FADE_IN_MS, FADE_OUT_MS                       （不存在 constants.py）
    PlayerEngine(volume=0.8)                      AudioEngine.__init__ **不接受**
                                                      volume，须另调 set_volume()

另外原稿的 `async def settle(...)` 是**不可用**的：本项目引擎是同步的，
没有 event loop，没人 await 它；而且 async 函数体里用 time.sleep() 自相矛盾。
故此处实现为**同步** settle。
"""

from __future__ import annotations

from player.engine.player import (
    FADE_IN_MS,
    FADE_OUT_MS,
    SEEK_FADE_SKIP_MS,
    VOLUME_SMOOTH_MS,
    AudioEngine,
)


def create_engine(fade_in_ms: float = FADE_IN_MS,
                  fade_out_ms: float = FADE_OUT_MS,
                  volume: float = 0.8) -> AudioEngine:
    """创建配置好的引擎实例（替身需已由 conftest 注入）。"""
    engine = AudioEngine(fade_in_ms=fade_in_ms, fade_out_ms=fade_out_ms)
    engine.set_volume(volume)   # 原稿把它当构造参数，实际是 setter
    return engine


def settle(engine: AudioEngine, limit: int = 900) -> bool:
    """推进淡出到底并让 poll() 收尾，直到引擎不再有挂起事件。

    【为什么必须】stop()/pause() 已异步化（F7）：终态发生在"淡出走完 + poll() 收尾"
    之后，而不是调用返回时。禁止用裸 time.sleep() 代替本函数。

    【D2 修复 · 结构性假绿堵口】原实现在耗尽 `limit` 后**静默返回** ——
    调用方无法区分"已收敛"与"超限放弃"，于是"审的是半途状态"却显示为通过。
    现在改为：耗尽即抛 `AssertionError`，并把当时的 state / fade_pending /
    _pending_finalize 一并带出，让失败可定位。

    返回 True 仅表示"本轮确实观察到了收敛"；调用方**不应**依赖返回值做判断
    （未收敛会抛异常）。保留返回值只是为了兼容 `assert settle(...)` 写法。
    """
    for _ in range(limit):
        gen = engine._stream
        if gen is not None:
            try:
                next(gen)
            except StopIteration:
                pass
        engine.poll()
        if not engine.fade_pending and engine._pending_finalize is None:
            return True
    # 耗尽 limit 仍未收敛 —— 这是**真问题**，不是 flaky：要么被测代码没收尾，
    # 要么本用例给的状态机前提不成立。必须显式暴露，绝不静默放过。
    raise AssertionError(
        f"settle() 未在 {limit} 步内收敛（疑似假绿窗口）："
        f"state={engine.state!r} fade_pending={engine.fade_pending} "
        f"_pending_finalize={engine._pending_finalize!r} "
        f"_stream={'None' if engine._stream is None else 'alive'}"
    )


def is_exhausted(gen) -> bool:
    """生成器已 close/耗尽 → next() 抛 StopIteration。"""
    try:
        next(gen)
    except StopIteration:
        return True
    except Exception:  # noqa: BLE001
        return False
    return False


__all__ = [
    "create_engine", "settle", "is_exhausted",
    "FADE_IN_MS", "FADE_OUT_MS", "VOLUME_SMOOTH_MS", "SEEK_FADE_SKIP_MS",
]
