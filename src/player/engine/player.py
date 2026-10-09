"""Phase 0 音频引擎：miniaudio 解码 + 播放。

设计要点（v1.0 架构的子集）：
- 所有文件统一重采样到 44.1kHz / 立体声 / 16bit，喂同一个 PlaybackDevice。
- 软件音量：在 PCM 回调层做乘法（也是将来 EQ/频谱的挂载点）。
- seek：重建 stream 并指定 seek_frame（miniaudio 原生支持）。
- ffmpeg 管道格式（m4a/alac/ape/wma/mp4）在 Phase 1 接入，当前遇到会报人话错误。

【P0 修复】
- P0-1：`state` 改为纯只读属性（删除副作用）；新增 FINISHED 状态替代 `_eof` 幽灵标志；
        生成器正常耗尽时由音频线程调用 `_on_finished()` 显式转移，主线程 `poll()` 消费并回调。
- P0-2：`play()` 守卫改用状态枚举，播完 / 出错后再次 `play()` 能正确重播，不再静默 return。
- P0-3：`_pcm_stream` 捕获解码异常并转入 ERROR 状态，异常不再穿出到音频线程。

【P1-7 修复 · 解码器生命周期】
- 引擎持有 `_stream`（当前 PCM 生成器）引用；所有停止路径（stop / pause / seek / load /
  错误清理 / shutdown）都汇聚到同一个 `_close_decoder()`。
- 关闭顺序统一为「先 device.stop()（等音频线程退出）→ 再 _close_decoder()」，
  从根上杜绝"状态变了但生成器还在跑"的孤儿流。
- 关闭后再次 `play()` 会重新创建生成器（`_start_stream` 先关旧再开新），可正常重放。

【P1-1 修复 · 错误码日志】
- 所有 `AudioError` 抛出前统一经 `_fail()` 记录 `code=...`，便于日志检索。
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
        self._state = State.STOPPED  # 【P0-1】改用状态枚举
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
        self.stop()  # 【P1-7】stop() 内部会关闭旧解码器
        with self._lock:
            self._path = path
            self._duration = float(info.duration or 0.0)
            self._frames_played = 0
            self._error_code = None
            self._error_msg = None
        logger.info("loaded %s (%.1fs)", path, self._duration)

    def play(self) -> None:
        # 【P0-2】守卫改用状态枚举：仅 PLAYING 时幂等返回；
        # FINISHED → 从头重播；ERROR → 从头重试；其余 → 从当前位置续播。
        if not self._path:
            return
        self._ensure_device()  # 可能抛 AudioError，由 UI 负责提示
        with self._lock:
            if self._state is State.PLAYING:
                return
            if self._state is State.FINISHED:
                start_frame = 0
            elif self._state is State.ERROR:
                start_frame = 0
                self._error_code = None
                self._error_msg = None
            else:
                start_frame = self._frames_played
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
        # 【P0-1】走纯只读的 state 属性，不再依赖原始 _state 字段。
        if self.state == State.PLAYING.value:
            self.pause()
        else:
            self.play()

    def stop(self) -> None:
        if self._device is not None:
            try:
                self._device.stop()
            except Exception:  # noqa: BLE001 - 关闭时不纠结
                pass
        self._close_decoder()  # 【P1-7】与 pause 汇聚到同一处关闭
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
                self._state = State.PAUSED  # 播完后拖动进度条 → 回到"已装载未播放"
            self._frames_played = frame
            self._pending_finished = False
        if was_playing and self._device is not None:
            # 【P1-7】先停 → 关旧生成器 → 再开新流
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
            # 【P1-7】流已自然结束或出错：停设备并回收生成器引用（不改 _state）
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
    def error_code(self) -> ErrorCode | None:
        # 【P0-3】最近一次错误码（无错误时为 None）。
        return self._error_code

    @property
    def current_path(self) -> str | None:
        return self._path

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
        """【P1-7】创建新 PCM 生成器并交给设备；调用前旧流必须已关闭。"""
        self._close_decoder()  # 兜底：确保没有旧生成器残留
        assert self._device is not None
        gen = self._pcm_stream(seek_frame)
        self._stream = gen
        self._device.start(gen)

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

    def _on_finished(self) -> None:
        """【P0-1】音频线程在生成器正常耗尽时调用：显式转移到 FINISHED。

        只做线程安全的字段写入，绝不触碰 Qt；主线程通过 poll() 得到回调。
        """
        with self._lock:
            if self._state is not State.PLAYING:
                return  # 已被 stop/pause/seek 取代，丢弃本次事件
            self._state = State.FINISHED
            self._pending_finished = True

    def _on_error(self, code: ErrorCode, message: str) -> None:
        """【P0-3】音频线程在解码异常时调用：显式转移到 ERROR。"""
        with self._lock:
            if self._state is not State.PLAYING:
                return
            self._state = State.ERROR
            self._error_code = code
            self._error_msg = message
            self._pending_error = True

    def _pcm_stream(self, seek_frame: int):
        """包一层：计数已播帧、软件音量、显式结束/错误事件。device 在独立线程里拉它。"""
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
                vol = self._volume
                if vol < 1.0:
                    chunk = array.array(
                        "h",
                        (max(_PCM_MIN, min(_PCM_MAX, int(s * vol))) for s in chunk),
                    )
                with self._lock:
                    self._frames_played += frames
                yield chunk
        except Exception as exc:  # noqa: BLE001
            # 【P0-3】只捕获 Exception（不捕获 GeneratorExit），异常绝不穿出到音频线程。
            logger.error("decode error: %s", exc)
            self._on_error(ErrorCode.FILE_CORRUPT, str(exc))
        else:
            # 循环正常结束 = 自然播完（被 stop/pause/seek 中断时走 GeneratorExit，不会到这里）
            self._on_finished()
        finally:
            # 【P1-7】无论正常结束、异常还是被 close()，都释放底层解码器。
            if raw is not None:
                try:
                    raw.close()
                except Exception:  # noqa: BLE001
                    pass
