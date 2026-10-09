"""错误码 → 用户文案映射（纯函数，不依赖 Qt，便于单测）。

【P1-1】UI 的唯一错误入口使用本模块，把引擎的 ErrorCode 统一翻译为：
- `text`      ：给用户看的短文案，**绝不包含异常堆栈或技术细节**
- `retryable` ：是否属于"可重试"类（可重试 → 弹窗提供「重试」按钮）

分类原则：
- 不可恢复（retryable=False）：格式不支持 / 文件损坏 / 无可用设备 —— 重试也不会成功
- 可重试  （retryable=True ）：设备被占 / IO 瞬时失败 / 未知 —— 稍后重试可能成功
"""

from __future__ import annotations

from dataclasses import dataclass

from player.engine.states import ErrorCode


@dataclass(frozen=True)
class ErrorReport:
    text: str        # 用户文案（无堆栈）
    retryable: bool  # 是否可重试


_MAP: dict[ErrorCode, ErrorReport] = {
    ErrorCode.FORMAT_NOT_SUPPORTED: ErrorReport("这个音频格式暂不支持播放。", False),
    ErrorCode.FILE_CORRUPT: ErrorReport("文件已损坏，无法解码播放。", False),
    ErrorCode.DEVICE_NOT_FOUND: ErrorReport("没有找到可用的音频输出设备。", False),
    ErrorCode.DEVICE_BUSY: ErrorReport("音频设备被其他程序占用了。", True),
    ErrorCode.IO_ERROR: ErrorReport("读取文件失败，文件可能已被移动或删除。", True),
    ErrorCode.UNKNOWN: ErrorReport("播放时发生了未知错误。", True),
}

_FALLBACK = _MAP[ErrorCode.UNKNOWN]


def describe(code: ErrorCode | None) -> ErrorReport:
    """把错误码翻译为 ErrorReport；未知或 None 一律走兜底文案。"""
    if code is None:
        return _FALLBACK
    return _MAP.get(code, _FALLBACK)
