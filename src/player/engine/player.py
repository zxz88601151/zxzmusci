"""Phase 0 音频引擎：miniaudio 解码 + 播放。

设计要点（v1.0 架构的子集）：
- 所有文件统一重采样到 44.1kHz / 立体声 / 16bit，喂同一个 PlaybackDevice。
- 软件音量：在 PCM 回调层做乘法（也是将来 EQ/频谱的挂载点）。
- seek：重建 stream 并指定 seek_frame（miniaudio 原生支持）。
- ffmpeg 管道格式（m4a/alac/ape/wma/mp4）在 Phase 1 接入，当前遇到会报人话错误。

【P0 修复】
- P0-1：`state` 纯只读；新增 FINISHED 状态替代 `_eof`；音频线程显式转移 + 主线程 poll() 消费。
- P0-2：`play()` 守卫改用状态枚举，播完 / 出错后可重播。
- P0-3：`_pcm_stream` 捕获解码异常并转入 ERROR，异常不穿出音频线程。

【P1-7 修复 · 解码器生命周期】
- 引擎持有 `_stream`；stop/pause/seek/load/错误清理/shutdown 六条路径统一 `_close_decoder()`；
  关闭顺序固定「先 device.stop() → 再 _close_decoder()」，杜绝孤儿流。

【P1-2 / P1-3 修复 · generation 代次号】
- `_generation` 单调递增（Python int，无溢出），**永不清零**（避免 ABA）。
- 真正生效的 play / seek / load / stop 各递增 1 次；被守卫挡回的 play() 不递增。
- `_pcm_stream` 在创建时快照自己的代次号；进度累加、EOF、错误三类事件
  **全部在锁内做「代次校验 + 应用」的原子操作**，代次不匹配一律静默丢弃（不抛异常）。
- 因此：旧代次的进度/EOF/错误既不污染新流的位置与状态，也不会触发 finished / 切歌。
"""

from __future__ import annotations

import array
import logging
import os
import threading
from collections.abc import Iterator
from typing import Callable

import miniaudio

from player.engine.states import ErrorCode, State

logger = logging.getLogger(__name__)

# miniaudio 原生解码的格式；其余走 ffmpeg 管道（Phase 1）
NATIVE_EXTS = {".mp3", ".wav", ".flac", ".ogg", ".opus"}
PIPE_EXTS = {".m4a", ".alac", ".ape", ".wma", ".mp4"}

_SAMPLE_RATE = 44100
_NCHANNELS = 2
_PCM_MIN = -32768
_PCM_MAX = 32767


class AudioError(Exception):
    """给用户看的播放错误（UI 直接弹这句话）。"""

    # 【P0-3】携带错误码，便于 UI 分档提示与埋点。
    def __init__(self, message: str, code: ErrorCode = ErrorCode.UNKNOWN) -> None:
        super().__init__(message)
        self.code = code


def _fail(code: ErrorCode, message: str) -> AudioError:
    """【P1-1】统一构造并记录 AudioError：日志里始终带 ErrorCode。"""
    logger.warning("audio error code=%s detail=%s", code.value, message)
    return AudioError(message, code)


class AudioEngine:
    def __init__(self) -> None:
        self._device: miniaudio.PlaybackDevice | None = None
        self._stream: Iterator[array.array] | None = None  # 【P1-7】当前 PCM 生成器
        self._path: str | None = None
        self._duration = 0.0
        self._frames_played = 0  # 已播出的 PCM 帧数（含 seek 起点）
        self._volume = 0.8
        self._state = State.STOPPED  # 【P0-1】状态枚举
        # 【P1-2/P1-3】代次号：每次"真正生效"的流生命周期操作 +1，永不清零
        self._generation = 0
        self._error_code: ErrorCode | None = None
        self._error_msg: str | None = None
        # 【P0-1/P0-3】音频线程置位、主线程 poll() 消费的一次性事件标志
        self._pending_finished = False
        self._pending_error = False
        self._lock = threading.Lock()
        # 主线程回调（由 poll() 触发），UI 可挂载
        self.on_finished: Callable[[], None] | None = None
        self.on_error: Callable[[ErrorCode | None, str | None], None] | None = None

    # ---------- 公开 API ----------
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
            raise _fail(
                ErrorCode.FORMAT_NOT_SUPPORTED,
                f"不支持的格式：{ext or '（无扩展名）'}",
            )
        try:
            info = miniaudio.get_file_info(path)
        except Exception as exc:  # noqa: BLE001
            raise _fail(ErrorCode.FILE_CORRUPT, f"解码器打不开这个文件：{exc}") from exc
        # 【P1-2】stop() 内部会 +1 代次（旧歌回调立即失效）并关闭旧解码器。
        self.stop()
        with self._lock:
            self._path = path
            self._duration = float(info.duration or 0.0)
            self._frames_played = 0
            self._error_code = None
            self._error_msg = None
        logger.info("loaded %s (%.1fs) gen=%d", path, self._duration, self.generation)

    def play(self) -> None:
        # 【P0-2】守卫：仅 PLAYING 时幂等返回（【P1-2】此时不递增代次）。
        if not self._path:
            return
        self._ensure_device()  # 可能抛 AudioError，由 UI 负责提示
        with self._lock:
            if self._state is State.PLAYING:
                return  # 幂等：不 bump generation
            if self._state is State.FINISHED:
                start_frame = 0  # 【P1-2】重播从 0 开始
            elif self._state is State.ERROR:
                start_frame = 0
                self._error_code = None
                self._error_msg = None
            else:
                start_frame = self._frames_played
            self._generation += 1  # 真正起播才递增
            self._frames_played = start_frame
            self._pending_finished = False
            self._pending_error = False
            # 先置位再 start：避免极短文件在 start 返回前就触发 _on_finished 被覆盖
            self._state = State.PLAYING
        self._start_stream(start_frame)

    def pause(self) -> None:
        with self._lock:
            if self._state is not State.PLAYING or self._device is None:
                return
            self._state = State.PAUSED
        # 【P1-7】先停设备（等音频线程退出），再关生成器，杜绝孤儿流
        self._device.stop()
        self._close_decoder()

    def toggle(self) -> None:
        # 【P0-1】走纯只读的 state 属性。
        if self.state == State.PLAYING.value:
            self.pause()
        else:
            self.play()

    def stop(self) -> None:
        with self._lock:
            # 【P1-2/P1-3】先递增代次：任何在途/迟到的旧回调立即失效。
            # 永不清零，避免 ABA（新流拿到与旧流相同的代次号）。
            self._generation += 1
        if self._device is not None:
            try:
                self._device.stop()
            except Exception:  # noqa: BLE001 - 关闭时不纠结
                pass
        self._close_decoder()  # 【P1-7】
        with self._lock:
            self._state = State.STOPPED
            self._frames_played = 0
            self._pending_finished = False
            self._pending_error = False

    def seek(self, seconds: float) -> None:
        """只在 playing/paused/finished 有效；stopped 下 seek 等价于下次从该处播。"""
        if not self._path or self._duration <= 0:
            return
        seconds = max(0.0, min(seconds, self._duration))
        frame = int(seconds * _SAMPLE_RATE)
        with self._lock:
            was_playing = self._state is State.PLAYING
            if self._state is State.FINISHED:
                self._state = State.PAUSED
            # 【P1-3】先 bump：从这一刻起到新流首帧之间，旧代次的进度回调全部被丢弃。
            self._generation += 1
            self._frames_played = frame
            self._pending_finished = False
        if was_playing and self._device is not None:
            self._device.stop()
            self._close_decoder()
            self._start_stream(frame)

    def poll(self) -> None:
        """【P0-1/P0-3/P1-7】主线程调用：消费音频线程的一次性事件并触发回调。

        该方法是 UI 与音频线程之间唯一的事件出口。除资源清理（停设备 + 关生成器）外，
        **不改变播放状态**——状态由音频线程的显式转移决定。
        """
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
        if finished and self.on_finished is not None:
            self.on_finished()
        if errored and self.on_error is not None:
            self.on_error(code, msg)

    def set_volume(self, v: float) -> None:
        self._volume = max(0.0, min(1.0, v))

    # ---------- 只读查询 ----------
    @property
    def volume(self) -> float:
        return self._volume

    @property
    def position(self) -> float:
        with self._lock:
            frames = self._frames_played
        return frames / _SAMPLE_RATE

    @property
    def duration(self) -> float:
        return self._duration

    @property
    def state(self) -> str:
        # 【P0-1】纯只读：不再消费 _eof、不再改写 _state / _frames_played。
        with self._lock:
            return self._state.value

    @property
    def generation(self) -> int:
        # 【P1-2】只读代次号，供 UI 判断"当前进度属于哪一版流"。
        with self._lock:
            return self._generation

    @property
    def error_code(self) -> ErrorCode | None:
        return self._error_code

    @property
    def current_path(self) -> str | None:
        return self._path

    def snapshot(self) -> tuple[int, float, float, str]:
        """【P1-2】一次性取 (generation, position, duration, state) 的一致快照。

        UI 用它在同一把锁下读到自洽的一组值，避免"旧歌的 position 配新歌的 duration"。
        """
        with self._lock:
            return (
                self._generation,
                self._frames_played / _SAMPLE_RATE,
                self._duration,
                self._state.value,
            )

    def shutdown(self) -> None:
        self.stop()  # 【P1-7】内含关闭生成器
        if self._device is not None:
            try:
                self._device.close()
            except Exception:  # noqa: BLE001
                pass
            self._device = None

    # ---------- 内部 ----------
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

    def _start_stream(self, seek_frame: int) -> None:
        """【P1-7/P1-2】创建新 PCM 生成器并交给设备；调用前旧流必须已关闭。

        生成器创建时快照当前代次号，之后所有回调都以该代次号校验。
        """
        self._close_decoder()
        assert self._device is not None
        with self._lock:
            generation = self._generation
        stream = self._pcm_stream(seek_frame, generation)
        self._stream = stream
        self._device.start(stream)

    def _close_decoder(self) -> None:
        """【P1-7】关闭当前 PCM 生成器（其 finally 会释放底层解码器）。

        stop / pause / seek / load / 错误清理 / shutdown 六条路径全部汇聚到这里。
        调用前提：设备已 stop（音频线程已退出），否则生成器可能正在执行。
        """
        gen = self._stream
        self._stream = None
        if gen is not None:
            try:
                gen.close()
            except Exception as exc:  # noqa: BLE001 - 含 ValueError(generator already executing)
                logger.debug("decoder close skipped: %s", exc)

    def _on_finished(self, generation: int) -> None:
        """【P0-1/P1-2】音频线程在生成器正常耗尽时调用。

        代次不匹配 → 静默丢弃（旧流的 EOF 不得触发 finished / 切歌）。
        """
        with self._lock:
            if generation != self._generation:
                return  # 旧代次，丢弃
            if self._state is not State.PLAYING:
                return
            self._state = State.FINISHED
            self._pending_finished = True

    def _on_error(self, generation: int, code: ErrorCode, message: str) -> None:
        """【P0-3/P1-2】音频线程在解码异常时调用；代次不匹配同样丢弃。"""
        with self._lock:
            if generation != self._generation:
                return  # 旧代次，丢弃
            if self._state is not State.PLAYING:
                return
            self._state = State.ERROR
            self._error_code = code
            self._error_msg = message
            self._pending_error = True

    def _pcm_stream(self, seek_frame: int, generation: int):
        """包一层：计数已播帧、软件音量、显式结束/错误事件。device 在独立线程里拉它。

        【P1-2/P1-3】`generation` 是本流创建时的代次号快照；所有对引擎状态的写入
        都必须先校验代次是否仍然有效，否则一律丢弃。
        """
        raw = None
        try:
            raw = miniaudio.stream_file(
                self._path,
                output_format=miniaudio.SampleFormat.SIGNED16,
                nchannels=_NCHANNELS,
                sample_rate=_SAMPLE_RATE,
                seek_frame=seek_frame,
            )
            for chunk in raw:  # chunk: array('h')
                frames = len(chunk) // _NCHANNELS
                with self._lock:
                    # 「快照校验 + 应用」在同一把锁内原子完成：
                    # 校验与写入之间不可能被其它线程插入代次变更。
                    if generation != self._generation:
                        return  # 代次已变（seek/load/stop/play）→ 本流立即终止，不再产出
                    self._frames_played += frames
                vol = self._volume
                if vol < 1.0:
                    chunk = array.array(
                        "h",
                        (max(_PCM_MIN, min(_PCM_MAX, int(s * vol))) for s in chunk),
                    )
                yield chunk
        except Exception as exc:  # noqa: BLE001
            # 【P0-3】只捕获 Exception（不捕获 GeneratorExit），异常绝不穿出到音频线程。
            logger.error("decode error: %s", exc)
            self._on_error(generation, ErrorCode.FILE_CORRUPT, str(exc))
        else:
            # 循环正常结束 = 自然播完（被 close() 中断时走 GeneratorExit，不会到这里；
            # 代次失效时走上面的 return，也不会到这里）。
            self._on_finished(generation)
        finally:
            # 【P1-7】无论正常结束、异常还是被 close()，都释放底层解码器。
            if raw is not None:
                try:
                    raw.close()
                except Exception:  # noqa: BLE001
                    pass
