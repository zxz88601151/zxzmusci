"""播放器状态与错误码定义。

【P0 引入】
- State.FINISHED：替代原先的 `_eof` 幽灵标志，让"自然播完"成为显式状态。
- State.ERROR   ：让运行期解码/IO 失败有明确归属（P0-3）。

设计说明：
- State 继承 str，`AudioEngine.state` 仍返回字符串，保持与既有 UI 比较逻辑兼容。
- P1-6 将在此基础上扩展 IDLE / LOADING / READY 并引入 Event；此处先落地 P0 所需的最小集合。
"""

from __future__ import annotations

from enum import Enum


class State(str, Enum):
    """播放器状态。"""

    STOPPED = "stopped"    # 已装载或未装载，未播放（P1-6 将拆分为 IDLE / READY）
    PLAYING = "playing"
    PAUSED = "paused"
    FINISHED = "finished"  # 自然播完（替代 _eof 幽灵标志）
    ERROR = "error"        # 运行期解码 / IO 错误


class ErrorCode(str, Enum):
    """错误码，供 UI 分档提示与后续埋点使用。"""

    FORMAT_NOT_SUPPORTED = "format_not_supported"
    FILE_CORRUPT = "file_corrupt"
    DEVICE_BUSY = "device_busy"
    DEVICE_NOT_FOUND = "device_not_found"
    IO_ERROR = "io_error"
    UNKNOWN = "unknown"
