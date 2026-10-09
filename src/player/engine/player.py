"""Phase 0 音频引擎：miniaudio 解码 + 播放。

════════════════════════════════════════════════════════════════════════
增益链路（唯一落点：`_apply_gain()` 里的 `round(sample * gain)`）
════════════════════════════════════════════════════════════════════════

    volume(0~1)
      │  volume_curve.linear_to_db_gain()        ← 等响/dB 映射（A 节）
      ▼
    _target_gain  ──[指数平滑 VOLUME_SMOOTH_MS]──►  _smooth_gain   （C 节，去爆音）
      │
      │                                            _fade (淡入淡出包络)  （B 节）
      ▼                                                  │
    final_gain = _smooth_gain × fade_gain  ◄──────────────┘
      │
      ▼
    PCM 每帧乘数 → 输出

`play/seek/load/stop` **只写目标值**，一律不直接乘样本（强制约束 1）。
真正逐帧相乘只发生在 `_pcm_stream` 内部、代次校验通过之后、`yield` 之前。

【历史修复】
- P0-1/2/3：state 纯只读 + FINISHED/ERROR 状态 + 解码异常安全。
- P1-7：`_close_decoder()` 收口六条停止路径，杜绝孤儿流。
- P1-2/P1-3：generation 代次号；进度/EOF/错误在锁内「校验+应用」，不匹配静默丢弃。
- P1-4/P1-5（本轮重构）：音量改 dB 曲线；增益 = 平滑 × 淡入淡出；见 `volume_curve.py` / `fade.py`。
"""

from __future__ import annotations

import array
import logging
import math
import os
import threading
import time
from collections.abc import Iterator
from typing import Callable

import miniaudio

from player.engine.fade import _FadeEnvelope
from player.engine.states import ErrorCode, State
from player.engine.volume_curve import linear_to_db_gain

logger = logging.getLogger(__name__)

# miniaudio 原生解码的格式；其余走 ffmpeg 管道（Phase 1）
# 用 frozenset：模块级只留不可变常量，杜绝可变全局状态（R16）
NATIVE_EXTS = frozenset({".mp3", ".wav", ".flac", ".ogg", ".opus"})
PIPE_EXTS = frozenset({".m4a", ".alac", ".ape", ".wma", ".mp4"})

_SAMPLE_RATE = 44100
_NCHANNELS = 2
_PCM_MIN = -32768
_PCM_MAX = 32767

# ---- 可配置常量（禁止魔法数字散落）----
FADE_IN_MS = 200.0            # 淡入时长
FADE_OUT_MS = 300.0           # 淡出时长
VOLUME_SMOOTH_MS = 80.0       # 音量平滑时间常数（50~100ms）
SEEK_FADE_SKIP_MS = 100.0     # seek 跳跃 < 此值视为"短跳"：跳过 ramp-out（内容几乎连续）
_FADE_MARGIN = 0.005          # 等待淡出走完的额外余量（秒）
_DEFAULT_VOLUME = 0.8


def _clamp_pcm(v: float) -> int:
    if v < _PCM_MIN:
        return _PCM_MIN
    if v > _PCM_MAX:
        return _PCM_MAX
    return int(v)


class AudioError(Exception):
    """给用户看的播放错误（UI 直接弹这句话）。"""

    def __init__(self, message: str, code: ErrorCode = ErrorCode.UNKNOWN) -> None:
        super().__init__(message)
        self.code = code


def _fail(code: ErrorCode, message: str) -> AudioError:
    """统一构造并记录 AudioError：日志里始终带 ErrorCode。"""
    logger.warning("audio error code=%s detail=%s", code.value, message)
    return AudioError(message, code)


class AudioEngine:
    def __init__(
        self,
        *,
        fade_in_ms: float = FADE_IN_MS,
        fade_out_ms: float = FADE_OUT_MS,
        smooth_ms: float = VOLUME_SMOOTH_MS,
        seek_fade_skip_ms: float = SEEK_FADE_SKIP_MS,
    ) -> None:
        # 【C2】三个时长抽成构造参数：改任一个都不影响另外两个。
        self._fade_in_ms = float(fade_in_ms)
        self._fade_out_ms = float(fade_out_ms)
        self._smooth_ms = float(smooth_ms)
        self._seek_fade_skip_ms = float(seek_fade_skip_ms)
        self._device: miniaudio.PlaybackDevice | None = None
        self._stream: Iterator[array.array] | None = None
        self._path: str | None = None
        self._duration = 0.0
        self._frames_played = 0
        self._state = State.STOPPED

        # ---- 增益链路 ----
        self._volume = _DEFAULT_VOLUME                    # 原始滑块值 [0,1]
        self._target_gain = linear_to_db_gain(_DEFAULT_VOLUME)
        self._smooth_gain = self._target_gain             # 实际平滑增益
        self._smooth_frames = max(1.0, _SAMPLE_RATE * self._smooth_ms / 1000.0)
        self._smooth_alpha = 1.0 - math.exp(-1.0 / self._smooth_frames)
        self._fade = _FadeEnvelope(1.0, self._fade_in_ms, _SAMPLE_RATE, start_gain=0.0)
        self._resume_gain = 0.0
        self._muted = False
        self._gain_changed_at = 0.0                       # 目标变更时间戳
        self._fading_out = False                          # 淡出期间抑制 finished

        # ---- 代次号 ----
        self._generation = 0
        self._error_code: ErrorCode | None = None
        self._error_msg: str | None = None
        self._pending_finished = False
        self._pending_error = False
        self._lock = threading.Lock()

        self.on_finished: Callable[[], None] | None = None
        self.on_error: Callable[[ErrorCode | None, str | None], None] | None = None

    # ══════════ 公开 API ══════════
    def load(self, path: str) -> None:
        if not os.path.isfile(path):
            raise _fail(ErrorCode.IO_ERROR, f"文件不存在：{path}")
        ext = os.path.splitext(path)[1].lower()
        if ext in PIPE_EXTS:
            raise _fail(
                ErrorCode.FORMAT_NOT_SUPPORTED,
                "该格式走 ffmpeg 管道，Phase 1 接入，Phase 0 暂不支持",
            )
        if ext not in NATIVE_EXTS:
            raise _fail(ErrorCode.FORMAT_NOT_SUPPORTED, f"不支持的格式：{ext or '（无扩展名）'}")
        try:
            info = miniaudio.get_file_info(path)
        except Exception as exc:  # noqa: BLE001
            raise _fail(ErrorCode.FILE_CORRUPT, f"解码器打不开这个文件：{exc}") from exc
        # 切歌路径：旧流淡出（stop 内部完成）→ 关旧解码器 → 新流随后淡入
        self.stop()
        with self._lock:
            self._path = path
            self._duration = float(info.duration or 0.0)
            self._frames_played = 0
            self._error_code = None
            self._error_msg = None
        logger.info("loaded %s (%.1fs) gen=%d", path, self._duration, self.generation)

    def play(self) -> None:
        if not self._path:
            return
        self._ensure_device()
        with self._lock:
            if self._state is State.PLAYING:
                return  # 幂等：不 bump generation
            if self._state is State.FINISHED:
                start_frame, start_gain = 0, 0.0      # 重播：从头 + 从 0 淡入
            elif self._state is State.ERROR:
                start_frame, start_gain = 0, 0.0
                self._error_code = None
                self._error_msg = None
            elif self._state is State.PAUSED:
                start_frame, start_gain = self._frames_played, self._resume_gain
            else:
                start_frame, start_gain = self._frames_played, 0.0
            self._generation += 1
            self._frames_played = start_frame
            self._pending_finished = False
            self._pending_error = False
            self._state = State.PLAYING
        self._start_stream(start_frame, start_gain)

    def pause(self) -> None:
        """淡出（期间 state 仍 PLAYING）→ 淡出走完 → device.stop() + 关解码器 → PAUSED。

        【F4】若暂停发生在淡入**进行中**，冻结当时的增益作为恢复起点
        （淡入已完成时恢复点仍为 0，保证正常的淡入听感）。
        """
        with self._lock:
            if self._state is not State.PLAYING or self._device is None:
                return
            self._resume_gain = 0.0 if self._fade.is_done else self._fade.current_gain
            self._fading_out = True
            self._fade.reset(0.0, self._fade_out_ms)
        self._wait_fade_out()
        self._device.stop()
        self._close_decoder()
        with self._lock:
            self._state = State.PAUSED
            self._fading_out = False

    def toggle(self) -> None:
        if self.state == State.PLAYING.value:
            self.pause()
        else:
            self.play()

    def stop(self) -> None:
        """旧流淡出 → 淡出走完 → device.stop() + _close_decoder() → STOPPED。"""
        with self._lock:
            was_active = self._state is State.PLAYING and self._stream is not None
            if was_active:
                self._fading_out = True
                self._fade.reset(0.0, self._fade_out_ms)
        if was_active:
            self._wait_fade_out()
        with self._lock:
            self._generation += 1     # 淡出之后才失效代次（淡出本身仍需产出音频）
            self._fading_out = False
        if self._device is not None:
            try:
                self._device.stop()
            except Exception:  # noqa: BLE001
                pass
        self._close_decoder()
        with self._lock:
            self._state = State.STOPPED
            self._frames_played = 0
            self._pending_finished = False
            self._pending_error = False

    def seek(self, seconds: float) -> None:
        """取消淡出，在新位置启动淡入。

        【SEEK_FADE_SKIP_MS】跳跃距离 < 阈值视为"短跳"：音频内容几乎连续，
        连 ramp-out 尾巴都省掉（0 等待）；≥ 阈值则做一段快速 ramp-out 消爆音。

        【此处需要重构 X】长跳转的**完整 300ms 淡出**与 P1-2「seek 后旧代次回调立即丢弃」
        互斥（要淡出就得让旧流继续产出，要立即丢弃就得立刻失效代次）。当前选择保住 P1-2，
        只用一段 ~20ms 的 ramp-out 尾巴，不做完整淡出。
        """
        if not self._path or self._duration <= 0:
            return
        seconds = max(0.0, min(seconds, self._duration))
        frame = int(seconds * _SAMPLE_RATE)
        with self._lock:
            was_playing = self._state is State.PLAYING
            jump_ms = abs(seconds - self._frames_played / _SAMPLE_RATE) * 1000.0
            need_ramp = jump_ms >= self._seek_fade_skip_ms
            if self._state is State.FINISHED:
                self._state = State.PAUSED
            self._generation += 1          # 立即失效：旧代次进度/EOF/错误全部丢弃
            self._frames_played = frame
            self._pending_finished = False
            self._fade.reset(1.0, self._fade_in_ms)  # 取消任何进行中的淡出，改为新位置淡入
        if was_playing and self._device is not None:
            if need_ramp:
                time.sleep(_FADE_MARGIN + 0.015)  # 等一个 chunk 的 ramp-out 尾巴
            self._device.stop()
            self._close_decoder()
            self._start_stream(frame, 0.0)

    def poll(self) -> None:
        """主线程调用：消费音频线程的一次性事件并触发回调（唯一事件出口）。"""
        with self._lock:
            finished = self._pending_finished
            self._pending_finished = False
            errored = self._pending_error
            self._pending_error = False
            code, msg = self._error_code, self._error_msg
        if finished or errored:
            if self._device is not None:
                try:
                    self._device.stop()
                except Exception:  # noqa: BLE001
                    pass
            self._close_decoder()
        if errored:
            # 【约束2】日志只在主线程写：音频回调里不做任何可能阻塞的 IO。
            logger.error(
                "decode error code=%s detail=%s",
                code.value if code is not None else "unknown",
                msg,
            )
        if finished and self.on_finished is not None:
            self.on_finished()
        if errored and self.on_error is not None:
            self.on_error(code, msg)

    # ---- 音量 / 静音 ----
    def set_volume(self, v: float) -> None:
        """只写目标增益（锁内）+ 记录变更时间戳；实际增益由 _pcm_stream 逐帧插值。"""
        from player.engine.volume_curve import clamp01

        v = clamp01(v)
        with self._lock:
            self._volume = v
            if not self._muted:
                self._target_gain = linear_to_db_gain(v)
                self._gain_changed_at = time.monotonic()

    def set_muted(self, flag: bool) -> None:
        """mute 复用同一套平滑逻辑：目标置 0；unmute 恢复音量。

        注：unmute 用**当前** `_volume`（滑块值）而非"mute 前的快照"——若用户在静音
        期间拖动过滑块，UI 与引擎才不会各说各话；未拖动时二者等价（V7 往返仍成立）。
        """
        with self._lock:
            if flag and not self._muted:
                self._muted = True
                self._target_gain = 0.0
                self._gain_changed_at = time.monotonic()
            elif not flag and self._muted:
                self._muted = False
                self._target_gain = linear_to_db_gain(self._volume)
                self._gain_changed_at = time.monotonic()

    def shutdown(self) -> None:
        """跳过淡出，直接关（退出路径不拖时间）。"""
        with self._lock:
            self._generation += 1
            self._fading_out = False
        if self._device is not None:
            try:
                self._device.stop()
            except Exception:  # noqa: BLE001
                pass
        self._close_decoder()
        if self._device is not None:
            try:
                self._device.close()
            except Exception:  # noqa: BLE001
                pass
            self._device = None
        with self._lock:
            self._state = State.STOPPED

    # ══════════ 只读查询 ══════════
    @property
    def volume(self) -> float:
        return self._volume

    @property
    def muted(self) -> bool:
        return self._muted

    # 【C2】三个时长只读回显（便于测试与 UI 展示）
    @property
    def fade_in_ms(self) -> float:
        return self._fade_in_ms

    @property
    def fade_out_ms(self) -> float:
        return self._fade_out_ms

    @property
    def smooth_ms(self) -> float:
        return self._smooth_ms

    @property
    def seek_fade_skip_ms(self) -> float:
        return self._seek_fade_skip_ms

    @property
    def target_gain(self) -> float:
        with self._lock:
            return self._target_gain

    @property
    def smooth_gain(self) -> float:
        with self._lock:
            return self._smooth_gain

    @property
    def fade_gain(self) -> float:
        with self._lock:
            return self._fade.current_gain

    @property
    def position(self) -> float:
        with self._lock:
            return self._frames_played / _SAMPLE_RATE

    @property
    def duration(self) -> float:
        return self._duration

    @property
    def state(self) -> str:
        with self._lock:
            return self._state.value

    @property
    def generation(self) -> int:
        with self._lock:
            return self._generation

    @property
    def error_code(self) -> ErrorCode | None:
        return self._error_code

    @property
    def current_path(self) -> str | None:
        return self._path

    def snapshot(self) -> tuple[int, float, float, str]:
        with self._lock:
            return (
                self._generation,
                self._frames_played / _SAMPLE_RATE,
                self._duration,
                self._state.value,
            )

    # ══════════ 内部 ══════════
    def _wait_fade_out(self) -> None:
        time.sleep(self._fade_out_ms / 1000.0 + _FADE_MARGIN)

    def _ensure_device(self) -> None:
        if self._device is None:
            try:
                self._device = miniaudio.PlaybackDevice(
                    output_format=miniaudio.SampleFormat.SIGNED16,
                    nchannels=_NCHANNELS,
                    sample_rate=_SAMPLE_RATE,
                )
            except Exception as exc:  # noqa: BLE001
                raise _fail(ErrorCode.DEVICE_NOT_FOUND, f"打不开声卡：{exc}") from exc

    def _start_stream(self, seek_frame: int, start_gain: float = 0.0) -> None:
        """新流启动：平滑增益对齐目标，淡入包络从 start_gain 走到 1.0。"""
        self._close_decoder()
        assert self._device is not None
        with self._lock:
            generation = self._generation
            self._smooth_gain = self._target_gain
            self._fade = _FadeEnvelope(1.0, self._fade_in_ms, _SAMPLE_RATE, start_gain=start_gain)
        stream = self._pcm_stream(seek_frame, generation)
        self._stream = stream
        try:
            self._device.start(stream)
        except Exception:
            # 【防泄漏】启动失败时不留悬挂生成器（否则解码器句柄要等 GC 才回收）
            self._close_decoder()
            raise

    def _close_decoder(self) -> None:
        """六条停止路径（stop/pause/seek/load/错误清理/shutdown）唯一收口。"""
        gen = self._stream
        self._stream = None
        if gen is not None:
            try:
                gen.close()
            except Exception as exc:  # noqa: BLE001
                logger.debug("decoder close skipped: %s", exc)

    def _on_finished(self, generation: int) -> None:
        with self._lock:
            if generation != self._generation or self._fading_out:
                return  # 旧代次 / 正在淡出 → 丢弃
            if self._state is not State.PLAYING:
                return
            self._state = State.FINISHED
            self._pending_finished = True

    def _on_error(self, generation: int, code: ErrorCode, message: str) -> None:
        """error 路径**不淡出**：立即转 ERROR，不拖长用户等待。"""
        with self._lock:
            if generation != self._generation:
                return
            if self._state is not State.PLAYING:
                return
            self._state = State.ERROR
            self._error_code = code
            self._error_msg = message
            self._pending_error = True

    # ══════════ PCM 增益 / 包络 ══════════
    @staticmethod
    def _apply_gain(sample: int, gain: float) -> int:
        """唯一乘法落点：四舍五入 + 钳位。"""
        return _clamp_pcm(round(sample * gain))

    @staticmethod
    def _ramp_table(count: int, start: float, end: float) -> list[float]:
        """预计算 count 个线性 ramp 系数（含两端）。"""
        if count <= 0:
            return []
        if count == 1:
            return [end]
        step = (end - start) / (count - 1)
        return [start + step * i for i in range(count)]

    def _apply_gain_envelope(
        self, chunk: array.array, frames: int, generation: int | None = None
    ) -> array.array:
        """逐帧 final_gain = smooth_gain × fade_gain，再乘到 PCM 上。

        - smooth：指数平滑逼近 `_target_gain`（时间常数 smooth_ms）
        - fade  ：本段内按真实轨迹插值（block 返回段首/段尾/有效帧数）

        【竞态防护】`_smooth_gain` 的"读—改—写"横跨两次加锁，本身不是原子操作。
        因此回写时必须校验代次：若本流已被 seek/load/stop 取代（代次已变），
        就**丢弃**这次回写，避免旧流 chunk 收尾覆盖新流刚设好的平滑初值。
        `generation=None` 表示直接调用（测试/工具场景），按当前代次处理。
        """
        n = _NCHANNELS
        with self._lock:
            if generation is None:
                generation = self._generation
            smooth = self._smooth_gain
            target = self._target_gain
            # block() 告知"本段内真正处于过渡的帧数 move"：
            # 超出 move 的帧保持段尾增益，避免 chunk 粒度把包络拉长（端点仍精确命中）。
            fade0, fade1, move = self._fade.block(frames)
        alpha = self._smooth_alpha
        inv = 1.0 / (move - 1) if move > 1 else 0.0
        apply = self._apply_gain
        out = array.array("h")
        append = out.append
        s = smooth
        for i in range(frames):
            s += (target - s) * alpha
            if move <= 0:
                f = fade1
            elif i >= move:
                f = fade1
            else:
                f = fade0 + (fade1 - fade0) * (i * inv)
            g = s * f
            base = i * n
            for c in range(n):
                append(apply(chunk[base + c], g))
        with self._lock:
            if generation == self._generation:  # 【竞态防护】代次已变则丢弃回写
                self._smooth_gain = s
        return out

    def _ramp_out_chunk(self, chunk: array.array) -> array.array:
        """代次失效时对最后一段做快速 ramp-out（从当前有效增益降到 0）。"""
        n = _NCHANNELS
        frames = len(chunk) // n
        with self._lock:
            start = self._smooth_gain * self._fade.current_gain
        table = self._ramp_table(frames, start, 0.0)
        apply = self._apply_gain
        out = array.array("h")
        append = out.append
        for i in range(frames):
            g = table[i]
            base = i * n
            for c in range(n):
                append(apply(chunk[base + c], g))
        return out

    def _pcm_stream(self, seek_frame: int, generation: int):
        """音频线程回调：代次校验 → 增益包络 → yield。"""
        raw = None
        try:
            raw = miniaudio.stream_file(
                self._path,
                output_format=miniaudio.SampleFormat.SIGNED16,
                nchannels=_NCHANNELS,
                sample_rate=_SAMPLE_RATE,
                seek_frame=seek_frame,
            )
            total_frames = int(self._duration * _SAMPLE_RATE) if self._duration > 0 else 0
            fade_out_frames = int(_SAMPLE_RATE * self._fade_out_ms / 1000.0)
            eof_fade_at = total_frames - fade_out_frames
            eof_fade_started = False
            abs_pos = seek_frame
            for chunk in raw:  # chunk: array('h')
                frames = len(chunk) // _NCHANNELS
                with self._lock:
                    stale = generation != self._generation
                    if not stale:
                        self._frames_played += frames
                if stale:
                    yield self._ramp_out_chunk(chunk)  # 不计数、不触发事件
                    return
                # 自然播完前启动淡出：淡出时长按"剩余帧数"动态设定，
                # 保证包络在文件末尾**精确走到 0**（不受 chunk 粒度影响）。
                if (
                    total_frames > fade_out_frames
                    and not eof_fade_started
                    and (abs_pos + frames) > eof_fade_at
                ):
                    remaining_ms = max(1.0, (total_frames - abs_pos) * 1000.0 / _SAMPLE_RATE)
                    with self._lock:
                        self._fade.reset(0.0, remaining_ms)
                    eof_fade_started = True
                out = self._apply_gain_envelope(chunk, frames, generation)
                abs_pos += frames
                yield out
        except Exception as exc:  # noqa: BLE001
            # 只捕获 Exception（不捕获 GeneratorExit）；音频线程内不打日志（约束2）
            self._on_error(generation, ErrorCode.FILE_CORRUPT, str(exc))
        else:
            self._on_finished(generation)
        finally:
            if raw is not None:
                try:
                    raw.close()
                except Exception:  # noqa: BLE001
                    pass
