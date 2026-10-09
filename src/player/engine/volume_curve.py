"""感知音量曲线（纯函数，零依赖，可单测）。

链路：滑块 volume ∈ [0,1] → dB → 线性增益 gain ∈ [0,1]

等响近似：
    gain = 10 ** ((v - 1.0) * MIN_DB / 20)
    - v = 1.0 → 0 dB    → gain = 1.0        （满音量）
    - v = 0.5 → -MIN_DB/2 dB                （MIN_DB=60 时 ≈ 0.0316，听感约"一半"）
    - v = 0.0 → 直接返回 0.0（真静音，不是 -MIN_DB 的残余增益）

【规格冲突已确认】原文同时写了「v=0 → gain≈0.0001（-80dB）」与
「gain = 10**((v-1)*60/20)（-60dB~0dB）」。二者不可同时成立：
- 若 v=0 走公式（-60dB → 0.001），则「set_volume(0.0) → 输出 RMS < 1e-4」这条不变式
  在常见输入（正弦 RMS≈0.7）下会得到 ≈7e-4，**必然失败**。
- 故此处 v<=0 直接返回 0.0（真静音），既满足 RMS 不变式，也不影响 v=0.5≈0.0316。
"""

from __future__ import annotations

# 可配置：动态范围下界（dB）。60 对应 v=0.5 → 10**(-1.5) ≈ 0.0316。
MIN_DB = 60.0


def _clamp01(v: float) -> float:
    """非法输入一律夹到 [0,1] 边界，不抛异常（UI 抖动不应炸引擎）。"""
    try:
        x = float(v)
    except (TypeError, ValueError):
        return 0.0
    if x != x:  # NaN
        return 0.0
    if x < 0.0:
        return 0.0
    if x > 1.0:
        return 1.0
    return x


def linear_to_db_gain(v: float) -> float:
    """滑块值 → 线性增益（等响近似）。v<=0 返回 0.0（真静音）。"""
    v = _clamp01(v)
    if v <= 0.0:
        return 0.0
    return 10.0 ** ((v - 1.0) * MIN_DB / 20.0)


def db_to_linear(db: float) -> float:
    """dB → 线性增益。0dB→1.0；<= -MIN_DB → 0.0；非法输入夹边界。"""
    try:
        x = float(db)
    except (TypeError, ValueError):
        return 0.0
    if x != x:  # NaN
        return 0.0
    if x >= 0.0:
        return 1.0
    if x <= -MIN_DB:
        return 0.0
    return 10.0 ** (x / 20.0)


def gain_to_db(gain: float) -> float:
    """线性增益 → dB（供 UI 显示）。gain<=0 → 返回 -MIN_DB（下界）。"""
    try:
        g = float(gain)
    except (TypeError, ValueError):
        return -MIN_DB
    if g != g or g <= 0.0:  # NaN / 静音
        return -MIN_DB
    import math

    return max(-MIN_DB, 20.0 * math.log10(g))


def slider_db(v: float) -> float:
    """滑块值 → 显示用 dB（V6）。

    - v = 0.0 → -MIN_DB（默认 -60.0）
    - v = 1.0 → 0.0
    - 在 [0,1] 上严格单调递增
    """
    return gain_to_db(linear_to_db_gain(v))
