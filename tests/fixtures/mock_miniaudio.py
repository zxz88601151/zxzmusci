"""miniaudio 替身模块 —— 模拟音频设备与解码器行为。

⚠️【与提示词原稿的差异 · 此处需要重构 X】
原稿的替身 API（`Decoder.open/read/close`、`AudioDevice.start()/stop()`）
是按"想象中的引擎接口"设计的，与本项目引擎**实际调用的 miniaudio 接口不一致**：

    引擎实际调用                             原稿替身提供的
    ─────────────────────────────────────   ─────────────────────
    miniaudio.SampleFormat.SIGNED16          （无）
    miniaudio.PlaybackDevice(output_format=, （无，且 AudioDevice 不接受参数）
        nchannels=, sample_rate=)
    device.start(generator) / stop() / close()  start()/stop() 无参、无 generator
    miniaudio.stream_file(path, **kw) -> 生成器 （无）
    miniaudio.get_file_info(path).duration      Decoder.get_info() 返回 dict

替身必须匹配**被测代码实际调用的接口**，否则测的是替身而不是代码。
因此本模块按引擎的真实调用面实现，同时保留原稿的**故障注入**设计（fault injection）。

用法：
    from fixtures.mock_miniaudio import install, configure, reset, EVENTS, CLOSES
    install()                      # 必须在 import 引擎之前调用
    configure(duration=5.0, mode="error")
"""

from __future__ import annotations

import array
import sys
import types

# ---------- 故障类型 ----------
class DeviceError(Exception):
    """设备层故障（打不开/启动失败）。"""


class DecodeError(Exception):
    """解码故障（格式不支持/数据损坏）。"""


class IOError_(Exception):
    """读盘故障。"""


# 原稿把 IOError 也列为故障类型；这里保留名字但避免遮蔽内建 IOError
IOError = IOError_  # noqa: A001

FAULTS = {"DECODE_ERROR", "IO_ERROR", "DEVICE_ERROR"}

DEFAULTS = {
    "duration": 5.0,          # get_file_info().duration
    "chunks": 50,             # stream_file 产出多少段
    "frames_per_chunk": 2000,  # 每段帧数
    "amplitude": 10000,       # PCM 幅值（16bit）
    "mode": "ok",             # "ok" | "error"（error = 中途抛 DecodeError）
    "start_fault": None,      # None | "DEVICE_ERROR"
}

BACKEND: dict = dict(DEFAULTS)
EVENTS: list[str] = []        # ["device.stop", "raw.close", ...] —— 验证 R10 关闭顺序
CLOSES: list[str] = []        # raw.close 次数

_FAKE = types.ModuleType("miniaudio")


class _SampleFormat:
    SIGNED16 = "s16"
    SIGNED32 = "s32"
    FLOAT32 = "f32"


class FakeDevice:
    """PlaybackDevice 替身：记录 start/stop，stop 时可快照 engine.state。

    刻意**不**在 stop() 里关闭生成器——关闭由引擎的 `_close_decoder()` 负责，
    这样 R10「先 device.stop() 再关解码器」的顺序才可被观测。
    """

    def __init__(self, **kw):
        self.kw = kw
        self.gen = None
        self.started = 0
        self.stopped = 0
        self.state_at_stop: list[str] = []
        self.engine = None       # 由测试注入，用于快照 state

    def start(self, gen):
        if BACKEND.get("start_fault") == "DEVICE_ERROR":
            raise DeviceError("Simulated device start failure")
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


class _FileInfo:
    def __init__(self, duration):
        self.duration = duration
        self.sample_rate = 44100
        self.nchannels = 2


def _stream_file(path, **kw):
    """stream_file 替身：产出 16bit 立体声 PCM，支持中途故障注入。"""
    try:
        if BACKEND["mode"] == "error":
            amp = BACKEND["amplitude"]
            yield array.array("h", [amp, amp])
            raise DecodeError("boom: 文件损坏")
        n = BACKEND["frames_per_chunk"]
        amp = BACKEND["amplitude"]
        for _ in range(BACKEND["chunks"]):
            yield array.array("h", [amp, amp] * n)
    finally:
        CLOSES.append("raw.close")
        EVENTS.append("raw.close")


_FAKE.SampleFormat = _SampleFormat
_FAKE.PlaybackDevice = FakeDevice
_FAKE.get_file_info = lambda p: _FileInfo(BACKEND["duration"])
_FAKE.stream_file = _stream_file


# ---------- 控制面 ----------
def configure(**kw) -> None:
    BACKEND.update(kw)


def reset() -> None:
    BACKEND.clear()
    BACKEND.update(DEFAULTS)
    EVENTS.clear()
    CLOSES.clear()


def install() -> None:
    """把替身装进 sys.modules —— **必须在 import 引擎之前**调用。

    ⚠️【翻车点】`sys.modules` 只在模块**首次 import** 时生效。引擎一旦 import 完成，
    再替换 `sys.modules["miniaudio"]` 不会改变引擎里已绑定的引用。
    因此本函数在 `conftest.py` 的**模块顶层**（而非 fixture 里）被调用：
    pytest 保证 conftest 先于测试模块加载，而测试模块是在**收集期** import 引擎的，
    函数作用域/session 作用域的 fixture 都太晚。
    """
    sys.modules["miniaudio"] = _FAKE


def set_fault(name: str) -> None:
    """故障注入：DECODE_ERROR / IO_ERROR / DEVICE_ERROR。"""
    if name == "DEVICE_ERROR":
        BACKEND["start_fault"] = "DEVICE_ERROR"
    else:
        BACKEND["mode"] = "error"


def clear_fault() -> None:
    BACKEND["start_fault"] = None
    BACKEND["mode"] = "ok"
