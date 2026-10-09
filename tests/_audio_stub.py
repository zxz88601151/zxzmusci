"""可配置的 miniaudio 替身后端（无真声卡即可跑全部音频用例）。

设计：
- `BACKEND` 是一个全局配置字典（duration/chunks/frames_per_chunk/amplitude/mode）。
- `install()` 把替身模块塞进 `sys.modules["miniaudio"]`，**必须在 import 引擎之前**调用。
- `EVENTS` / `CLOSES` 记录 `device.stop` / `raw.close` 的调用，用于验证关闭顺序（R10）。

注意：`sys.modules` 只在模块**首次 import** 时生效——引擎一旦 import 完成，
再替换 `sys.modules["miniaudio"]` 不会改变引擎里已绑定的引用。
所以每个测试套件若需要不同的后端行为，应在 import 引擎前完成 `install()`。
"""

from __future__ import annotations

import array
import sys
import types

DEFAULTS = {
    "duration": 5.0,
    "chunks": 50,
    "frames_per_chunk": 2000,
    "amplitude": 10000,
    "mode": "ok",          # "ok" | "error"
}

BACKEND: dict = dict(DEFAULTS)
EVENTS: list[str] = []     # ["device.stop", "raw.close", ...] 顺序日志
CLOSES: list[str] = []     # raw.close 次数

_FAKE = types.ModuleType("miniaudio")


class _SampleFormat:
    SIGNED16 = "s16"


class FakeDevice:
    """记录 start/stop 次数；stop 时可快照 engine.state（验证 B4/F3 顺序）。"""

    def __init__(self, **kw):
        self.gen = None
        self.started = 0
        self.stopped = 0
        self.state_at_stop: list[str] = []
        self.engine = None

    def start(self, gen):
        self.started += 1
        self.gen = gen

    def stop(self):
        self.stopped += 1
        EVENTS.append("device.stop")
        self.gen = None
        if self.engine is not None:
            self.state_at_stop.append(self.engine.state)

    def close(self):
        pass


class _Info:
    def __init__(self, duration):
        self.duration = duration


def _stream_file(path, **kw):
    try:
        if BACKEND["mode"] == "error":
            yield array.array("h", [BACKEND["amplitude"]] * 2)
            raise RuntimeError("boom: 文件损坏")
        n = BACKEND["frames_per_chunk"]
        amp = BACKEND["amplitude"]
        for _ in range(BACKEND["chunks"]):
            yield array.array("h", [amp, amp] * n)
    finally:
        CLOSES.append("raw.close")
        EVENTS.append("raw.close")


_FAKE.SampleFormat = _SampleFormat
_FAKE.PlaybackDevice = FakeDevice
_FAKE.get_file_info = lambda p: _Info(BACKEND["duration"])
_FAKE.stream_file = _stream_file


def configure(**kw) -> None:
    BACKEND.update(kw)


def reset_backend() -> None:
    BACKEND.clear()
    BACKEND.update(DEFAULTS)
    EVENTS.clear()
    CLOSES.clear()


def install() -> None:
    """把替身装进 sys.modules（须在 import 引擎前调用）。"""
    sys.modules["miniaudio"] = _FAKE
