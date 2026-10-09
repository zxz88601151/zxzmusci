"""Phase 0 最小主窗口：打开文件、播/停、进度条、音量。

【P1-1 修复 · UI 异常统一】
- 所有槽函数经 `@guard_audio` 包裹：引擎异常一律汇入 `_on_error(code, msg)`，
  不再有散落的 try/except + 各自弹窗。
- 运行期错误（解码 / IO）由 `engine.poll()` → `on_error` 回调进入同一入口。
- `_on_error` 是 UI 侧**唯一**错误出口，负责三件事：
  1. 按 ErrorCode 映射人话文案（**不露堆栈**）；
  2. 写错误码日志（`code=...`）；
  3. 区分「可重试」与「不可恢复」两类，可重试时提供「重试」按钮。
- 用 `_error_dialog_open` 保证同一次失败**只弹一次**。
"""

from __future__ import annotations

import functools
import logging
import os

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from player.engine.player import AudioEngine, AudioError
from player.engine.states import ErrorCode
from player.engine.volume_curve import slider_db
from player.settings import Settings
from player.ui.error_text import describe

logger = logging.getLogger(__name__)


def _fmt(sec: float) -> str:
    sec = max(0, int(sec))
    return f"{sec // 60}:{sec % 60:02d}"


def guard_audio(slot):
    """【P1-1】统一包裹槽函数：任何引擎异常都汇入 self._on_error，绝不穿出 Qt 槽。"""

    @functools.wraps(slot)
    def wrapper(self, *args, **kwargs):
        try:
            return slot(self, *args, **kwargs)
        except AudioError as exc:
            self._on_error(exc.code, str(exc))
        except Exception as exc:  # noqa: BLE001 - 兜底
            self._on_error(ErrorCode.UNKNOWN, str(exc))

    return wrapper


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("音乐播放器")
        self.resize(520, 220)

        self.engine = AudioEngine()
        # 【C3】音量记忆：持久化的是 linear 0~1 滑块值，恢复后经同一条 dB 曲线 ⇒ 听感一致
        self.settings = Settings()
        self._seeking = False  # 用户正在拖进度条时，不让 timer 回写
        self._error_dialog_open = False  # 【P1-1】防止同一失败重复弹窗
        self._ui_generation = -1  # 【P1-2】UI 当前展示的流代次

        # 【P0-1/P0-3】订阅引擎事件：由 _refresh -> engine.poll() 在主线程触发。
        self.engine.on_finished = self._on_engine_finished
        self.engine.on_error = self._on_engine_error

        # --- 控件 ---
        self.track_label = QLabel("未加载音频 — 点「打开」选一首歌")
        self.track_label.setWordWrap(True)

        self.seek = QSlider(Qt.Horizontal)
        self.seek.setRange(0, 1000)
        self.seek.sliderPressed.connect(lambda: setattr(self, "_seeking", True))
        self.seek.sliderReleased.connect(self._on_seek_released)

        self.time_label = QLabel("0:00 / 0:00")

        self.btn_open = QPushButton("打开")
        self.btn_play = QPushButton("播放")
        self.btn_stop = QPushButton("停止")
        self.btn_open.clicked.connect(self._on_open)
        self.btn_play.clicked.connect(self._on_play_pause)
        self.btn_stop.clicked.connect(self._on_stop)

        self.vol = QSlider(Qt.Horizontal)
        self.vol.setRange(0, 100)
        self.vol.setValue(int(round(self.settings.volume * 100)))  # 【C3】恢复上次音量
        self.vol.setMaximumWidth(120)
        self.vol.valueChanged.connect(self._on_volume_changed)
        self.vol.sliderReleased.connect(self._persist_volume)

        # 【V6】音量 dB 读数：0.0 → -60.0 dB，1.0 → 0.0 dB，单调递增
        self.vol_db = QLabel()
        self.vol_db.setMinimumWidth(64)
        self._on_volume_changed(self.vol.value())

        # --- 布局 ---
        top = QHBoxLayout()
        top.addWidget(self.btn_open)
        top.addWidget(self.btn_play)
        top.addWidget(self.btn_stop)
        top.addStretch(1)
        top.addWidget(QLabel("音量"))
        top.addWidget(self.vol)
        top.addWidget(self.vol_db)

        seek_row = QHBoxLayout()
        seek_row.addWidget(self.seek)
        seek_row.addWidget(self.time_label)

        root = QVBoxLayout()
        root.addWidget(self.track_label)
        root.addLayout(seek_row)
        root.addLayout(top)

        central = QWidget()
        central.setLayout(root)
        self.setCentralWidget(central)

        # --- 位置刷新 ---
        self.timer = QTimer(self)
        self.timer.setInterval(250)
        self.timer.timeout.connect(self._refresh)
        self.timer.start()

    # --- 事件（统一经 guard_audio 包裹）---
    @guard_audio
    def _on_open(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "打开音频",
            "",
            "音频 (*.mp3 *.wav *.flac *.ogg *.opus *.m4a *.ape *.wma *.mp4);;所有文件 (*)",
        )
        if not path:
            return
        self.engine.load(path)  # 失败 → guard_audio → _on_error
        self.track_label.setText(os.path.basename(path))
        self.engine.play()
        self._sync_play_button()

    @guard_audio
    def _on_play_pause(self) -> None:
        self.engine.toggle()
        self._sync_play_button()

    @guard_audio
    def _on_stop(self) -> None:
        self.engine.stop()
        self._sync_play_button()

    @guard_audio
    def _on_volume_changed(self, value: int) -> None:
        """【V6】滑块 → 引擎音量 + dB 读数（同一套 dB 曲线，保证显示与听感一致）。"""
        self.engine.set_volume(value / 100.0)
        self.vol_db.setText(f"{slider_db(value / 100.0):.1f} dB")

    def _persist_volume(self) -> None:
        """【C3】落盘 linear 0~1 音量（拖动结束时写一次，避免每 tick 都落盘）。"""
        self.settings.volume = self.vol.value() / 100.0
        self.settings.save()

    @guard_audio
    def _on_seek_released(self) -> None:
        self._seeking = False
        dur = self.engine.duration
        if dur > 0:
            self.engine.seek(self.seek.value() / 1000.0 * dur)

    # --- 引擎事件（主线程，由 poll() 触发）---
    def _on_engine_finished(self) -> None:
        # 【P0-1】自然播完。Phase 1 将在此自动切下一首；P0 只同步 UI。
        self._sync_play_button()

    def _on_engine_error(self, code, message) -> None:
        # 【P1-1】运行期错误与 load 错误进入同一入口。
        self._on_error(code, message)

    # --- 唯一错误出口 ---
    def _on_error(self, code: ErrorCode | None, message: str = "") -> None:
        """【P1-1】UI 侧唯一错误入口：映射文案 + 错误码日志 + 可重试分流。"""
        code = code or ErrorCode.UNKNOWN
        # 日志始终带 ErrorCode（原始 message 只进日志，不进 UI 文案）
        logger.error("UI error code=%s detail=%s", code.value, message)
        self._sync_play_button()

        if self._error_dialog_open:  # 同一次失败只弹一次
            return

        report = describe(code)
        self._error_dialog_open = True
        try:
            retry = self._show_error_dialog(report)
        finally:
            self._error_dialog_open = False

        if retry:
            self._retry_play()

    def _show_error_dialog(self, report) -> bool:
        """弹出统一错误对话框；返回用户是否选择「重试」。"""
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Warning)
        box.setWindowTitle("播放出错")
        box.setText(report.text)  # 固定文案，不含堆栈
        if report.retryable:
            box.setInformativeText("可稍后重试。")
            box.setStandardButtons(QMessageBox.Retry | QMessageBox.Cancel)
            box.setButtonText(QMessageBox.Retry, "重试")
            box.setButtonText(QMessageBox.Cancel, "取消")
            return box.exec() == QMessageBox.Retry
        box.setInformativeText("该文件无法播放。")
        box.setStandardButtons(QMessageBox.Ok)
        box.exec()
        return False

    def _retry_play(self) -> None:
        """用户点「重试」：重新尝试播放当前曲目（同样经统一错误入口）。"""
        try:
            self.engine.play()
        except AudioError as exc:
            self._on_error(exc.code, str(exc))
        except Exception as exc:  # noqa: BLE001
            self._on_error(ErrorCode.UNKNOWN, str(exc))

    # --- 刷新 ---
    def _refresh(self) -> None:
        # 【P0-3/P1-7】先消费音频线程的一次性事件（错误/结束），再刷新 UI。
        self.engine.poll()
        # 【P1-2】一次性取一致快照，杜绝"旧歌的 position 配新歌的 duration / 标题"。
        generation, pos, dur, _state = self.engine.snapshot()
        if generation != self._ui_generation:
            self._ui_generation = generation
            self._refresh_track_label()
        if dur > 0 and not self._seeking:
            self.seek.setValue(int(pos / dur * 1000))
        self.time_label.setText(f"{_fmt(pos)} / {_fmt(dur)}")
        # 【P0-1】state 已是纯只读，直接同步按钮即可（含 finished / error）。
        self._sync_play_button()

    def _refresh_track_label(self) -> None:
        # 【P1-2】标题只从"当前代次对应的曲目"派生，迟到回调无法污染。
        path = self.engine.current_path
        self.track_label.setText(
            os.path.basename(path) if path else "未加载音频 — 点「打开」选一首歌"
        )

    def _sync_play_button(self) -> None:
        text = "暂停" if self.engine.state == "playing" else "播放"
        if self.btn_play.text() != text:  # 避免每 250ms 无谓重绘
            self.btn_play.setText(text)

    def closeEvent(self, event) -> None:  # noqa: N802
        self._persist_volume()  # 【C3】退出前再存一次，保证不丢
        self.engine.shutdown()
        super().closeEvent(event)
