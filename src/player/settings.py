"""极简用户设置持久化（JSON）。目前只存音量。

【C3】只存**线性 0~1 的滑块值**（`volume`），**不存 dB、不存 gain**。
恢复时经同一条 dB 曲线 `linear_to_db_gain()` 换算 ⇒ 听感与上次完全一致。
这样即使将来调整曲线参数（MIN_DB），旧设置也仍然是"用户当初拖到的那个位置"，
语义稳定，不会因为曲线变了而突然变响/变轻。

零 Qt 依赖，可单测；UI 层只负责把值喂进来 / 取出去。
"""

from __future__ import annotations

import json
import logging
import os

from player.engine.volume_curve import clamp01

logger = logging.getLogger(__name__)

DEFAULT_VOLUME = 0.8
_KEY_VOLUME = "volume"


def default_path() -> str:
    """默认落盘位置：用户主目录下的 .musicplayer/settings.json。"""
    return os.path.join(os.path.expanduser("~"), ".musicplayer", "settings.json")


class Settings:
    def __init__(self, path: str | None = None) -> None:
        self._path = path or default_path()
        self._data: dict = {}
        self.load()

    # ---------- 读写 ----------
    def load(self) -> None:
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                data = json.load(f)
            self._data = data if isinstance(data, dict) else {}
        except FileNotFoundError:
            self._data = {}
        except Exception as exc:  # noqa: BLE001 - 设置损坏不应炸启动
            logger.warning("settings load failed (%s): %s", self._path, exc)
            self._data = {}

    def save(self) -> None:
        try:
            parent = os.path.dirname(self._path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            tmp = self._path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self._path)  # 原子替换，避免半截文件
        except Exception as exc:  # noqa: BLE001
            logger.warning("settings save failed (%s): %s", self._path, exc)

    # ---------- 音量（linear 0~1）----------
    @property
    def volume(self) -> float:
        return clamp01(self._data.get(_KEY_VOLUME, DEFAULT_VOLUME))

    @volume.setter
    def volume(self, v: float) -> None:
        self._data[_KEY_VOLUME] = clamp01(v)

    @property
    def path(self) -> str:
        return self._path
