"""淡入淡出包络（纯数据 + update，零依赖，可单测）。

`_FadeEnvelope` 只负责一件事：**从当前增益出发，用 duration_ms 毫秒线性走到目标增益**。
它与音量平滑（`VOLUME_SMOOTH_MS`）相互独立，最终增益 = 平滑增益 × 淡入淡出增益。

特性：
- 单调推进，**端点精确命中** 0.0 / 1.0（走完 duration 后 current_gain == target）。
- `reset(target, duration_ms)` 支持**中途改向**（淡出到一半要淡入 → 以当前增益为新起点）。
- `freeze()` / `unfreeze()`：淡出中途暂停时冻结当前增益，恢复时从冻结值继续。
"""

from __future__ import annotations


class _FadeEnvelope:
    def __init__(
        self,
        target: float = 1.0,
        duration_ms: float = 200.0,
        sample_rate: int = 44100,
        start_gain: float = 0.0,
    ) -> None:
        self._sample_rate = int(sample_rate)
        self._current = float(start_gain)
        self._target = float(target)
        self._duration_ms = float(duration_ms)
        self._frozen = False
        self._recompute()

    # ---------- 内部 ----------
    def _recompute(self) -> None:
        frames = max(1, int(self._sample_rate * self._duration_ms / 1000.0))
        self._frames_total = frames
        self._frames_left = frames
        self._step = (self._target - self._current) / frames

    # ---------- 公开 API ----------
    def reset(self, target: float, duration_ms: float) -> None:
        """中途改向：以**当前增益**为起点，重新走向新目标。"""
        self._target = float(target)
        self._duration_ms = float(duration_ms)
        self._frozen = False
        self._recompute()

    def step(self, n_frames: int) -> float:
        """推进 n_frames，返回本段的**起始增益**。

        调用方可用 `step(n)` 的返回值与本段结束后的 `current_gain` 做段内插值，
        得到逐帧平滑的包络（避免每 chunk 一级台阶的 zipper noise）。
        """
        if self._frozen or n_frames <= 0 or self._frames_left <= 0:
            return self._current
        start = self._current
        move = min(n_frames, self._frames_left)
        self._current += self._step * move
        self._frames_left -= move
        if self._frames_left <= 0:
            self._current = self._target  # 端点精确命中
        return start

    def block(self, n_frames: int) -> tuple[float, float, int]:
        """推进 n_frames，返回 `(段首增益, 段尾增益, 段内有效帧数)`。

        与 `step()` 的区别：额外告知"本段中有多少帧处于过渡区间"。
        若本段长于剩余过渡帧数，超出部分应保持段尾增益不变——
        这样调用方按 `move` 帧做线性插值时，端点仍然精确命中，
        不会因为 chunk 粒度把包络"拉长"。
        """
        if self._frozen or n_frames <= 0 or self._frames_left <= 0:
            return (self._current, self._current, 0)
        start = self._current
        move = min(n_frames, self._frames_left)
        self._current += self._step * move
        self._frames_left -= move
        if self._frames_left <= 0:
            self._current = self._target
        return (start, self._current, move)

    def freeze(self) -> None:
        """冻结在当前增益（淡出中途 pause）。"""
        self._frozen = True

    def unfreeze(self) -> None:
        self._frozen = False

    # ---------- 只读属性 ----------
    @property
    def is_done(self) -> bool:
        return self._frames_left <= 0

    @property
    def current_gain(self) -> float:
        return self._current

    @property
    def current_target(self) -> float:
        return self._target

    @property
    def frozen(self) -> bool:
        return self._frozen

    @property
    def frames_left(self) -> int:
        return max(0, self._frames_left)
