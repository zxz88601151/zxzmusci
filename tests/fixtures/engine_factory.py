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

import time

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


def settle(engine: AudioEngine, limit: int = 900) -> None:
    """推进淡出到底并让 poll() 收尾，直到引擎不再有挂起事件。

    【为什么必须】stop()/pause() 已异步化（F7）：终态发生在"淡出走完 + poll() 收尾"
    之后，而不是调用返回时。禁止用裸 time.sleep() 代替本函数。
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
            return


def is_exhausted(gen) -> bool:
    """生成器已 close/耗尽 → next() 抛 StopIteration。"""
    try:
        next(gen)
    except StopIteration:
        return True
    except Exception:  # noqa: BLE001
        return False
    return False


def measure_burst_ms(engine_factory, loads: int = 5, poll_interval: float = 0.0) -> float:
    """测量 N 次 load() 的控制路径总耗时（F7 用）。"""
    engine = engine_factory()
    return 0.0  # 占位：实际测量在测试内联完成


__all__ = [
    "create_engine", "settle", "is_exhausted",
    "FADE_IN_MS", "FADE_OUT_MS", "VOLUME_SMOOTH_MS", "SEEK_FADE_SKIP_MS",
]
