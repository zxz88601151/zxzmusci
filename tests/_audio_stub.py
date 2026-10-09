"""兼容垫片：真实实现已迁移到 `tests/fixtures/mock_miniaudio.py`。

保留本模块是为了让既有套件的 `import _audio_stub` 继续可用；
新代码请直接从 `fixtures.mock_miniaudio` 导入。
"""

from __future__ import annotations

from fixtures.mock_miniaudio import (  # noqa: F401
    BACKEND,
    CLOSES,
    DEFAULTS,
    EVENTS,
    DecodeError,
    DeviceError,
    FakeDevice,
    clear_fault,
    configure,
    install,
    reset,
    reset as reset_backend,
    set_fault,
)

__all__ = [
    "BACKEND", "CLOSES", "DEFAULTS", "EVENTS", "FakeDevice",
    "DecodeError", "DeviceError",
    "install", "configure", "reset", "reset_backend", "set_fault", "clear_fault",
]
