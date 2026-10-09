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

# 【C1/C2】淡入淡出时长持久化（毫秒）。引擎只认毫秒数，不认"音乐/播客"等业务概念。
_KEY_FADE_IN = "fade_in_ms"
_KEY_FADE_OUT = "fade_out_ms"
DEFAULT_FADE_IN_MS = 200.0
DEFAULT_FADE_OUT_MS = 300.0
FADE_MS_MIN = 0.0
FADE_MS_MAX = 2000.0


def clamp_range(value, lo: float, hi: float, name: str, default: float) -> float:
    """越界/非法值夹回边界并记 warning；无法解析则回退 default（C2）。"""
    try:
        v = float(value)
    except (TypeError, ValueError):
        logger.warning("settings %s 非法(%r)，回退默认 %s", name, value, default)
        return default
    if v != v:  # NaN
        logger.warning("settings %s 为 NaN，回退默认 %s", name, default)
        return default
    if v < lo:
        logger.warning("settings %s=%s 越界，夹到下限 %s", name, v, lo)
        return lo
    if v > hi:
        logger.warning("settings %s=%s 越界，夹到上限 %s", name, v, hi)
        return hi
    return v


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
        raw = self._data.get(_KEY_VOLUME, DEFAULT_VOLUME)
        v = clamp_range(raw, 0.0, 1.0, "volume", DEFAULT_VOLUME)
        return clamp01(v)

    @volume.setter
    def volume(self, v: float) -> None:
        self._data[_KEY_VOLUME] = clamp_range(v, 0.0, 1.0, "volume", DEFAULT_VOLUME)

    @property
    def fade_in_ms(self) -> float:
        raw = self._data.get(_KEY_FADE_IN, DEFAULT_FADE_IN_MS)
        return clamp_range(raw, FADE_MS_MIN, FADE_MS_MAX, _KEY_FADE_IN, DEFAULT_FADE_IN_MS)

    @fade_in_ms.setter
    def fade_in_ms(self, v: float) -> None:
        self._data[_KEY_FADE_IN] = clamp_range(v, FADE_MS_MIN, FADE_MS_MAX, _KEY_FADE_IN,
                                               DEFAULT_FADE_IN_MS)

    @property
    def fade_out_ms(self) -> float:
        raw = self._data.get(_KEY_FADE_OUT, DEFAULT_FADE_OUT_MS)
        return clamp_range(raw, FADE_MS_MIN, FADE_MS_MAX, _KEY_FADE_OUT, DEFAULT_FADE_OUT_MS)

    @fade_out_ms.setter
    def fade_out_ms(self, v: float) -> None:
        self._data[_KEY_FADE_OUT] = clamp_range(v, FADE_MS_MIN, FADE_MS_MAX, _KEY_FADE_OUT,
                                                DEFAULT_FADE_OUT_MS)

    @property
    def path(self) -> str:
        return self._path
