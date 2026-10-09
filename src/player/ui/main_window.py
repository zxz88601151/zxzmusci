"""Phase 0 最小主窗口：打开文件、播/停、进度条、音量。"""

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

from player.engine.player import AudioEngine


def _fmt(sec: float) -> str:
    sec = max(0, int(sec))
    return f"{sec // 60}:{sec % 60:02d}"


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("音乐播放器")
        self.resize(520, 220)

        self.engine = AudioEngine()
        self._seeking = False  # 用户正在拖进度条时，不让 timer 回写

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
        self.vol.setValue(80)
        self.vol.setMaximumWidth(120)
        self.vol.valueChanged.connect(lambda v: self.engine.set_volume(v / 100.0))
        self.engine.set_volume(0.8)

        # --- 布局 ---
        top = QHBoxLayout()
        top.addWidget(self.btn_open)
        top.addWidget(self.btn_play)
        top.addWidget(self.btn_stop)
        top.addStretch(1)
        top.addWidget(QLabel("音量"))
        top.addWidget(self.vol)

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

    # --- 事件 ---
    def _on_open(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "打开音频",
            "",
            "音频 (*.mp3 *.wav *.flac *.ogg *.opus *.m4a *.ape *.wma *.mp4);;所有文件 (*)",
        )
        if not path:
            return
        try:
            self.engine.load(path)
        except Exception as exc:  # noqa: BLE001 - 播放失败要弹人话
            QMessageBox.warning(self, "打不开", f"这个文件播不了，已跳过：\n{exc}")
            return
        self.track_label.setText(os.path.basename(path))
        self.engine.play()
        self._sync_play_button()

    def _on_play_pause(self) -> None:
        self.engine.toggle()
        self._sync_play_button()

    def _on_stop(self) -> None:
        self.engine.stop()
        self._sync_play_button()

    def _on_seek_released(self) -> None:
        self._seeking = False
        dur = self.engine.duration
        if dur > 0:
            self.engine.seek(self.seek.value() / 1000.0 * dur)

    def _refresh(self) -> None:
        # 【P0-3】先消费音频线程的一次性事件（错误/结束），再刷新 UI。
        self.engine.poll()
        dur = self.engine.duration
        pos = self.engine.position
        if dur > 0 and not self._seeking:
            self.seek.setValue(int(pos / dur * 1000))
        self.time_label.setText(f"{_fmt(pos)} / {_fmt(dur)}")
        # 【P0-1】state 已是纯只读，直接同步按钮即可（含 finished / error）。
        self._sync_play_button()

    def _sync_play_button(self) -> None:
        text = "暂停" if self.engine.state == "playing" else "播放"
        if self.btn_play.text() != text:  # 避免每 250ms 无谓重绘
            self.btn_play.setText(text)

    # --- 引擎事件（主线程，由 poll() 触发）---
    def _on_engine_finished(self) -> None:
        # 【P0-1】自然播完。Phase 1 将在此自动切下一首；P0 只同步 UI。
        self._sync_play_button()

    def _on_engine_error(self, code, message) -> None:
        # 【P0-3】运行期解码/IO 错误：恢复按钮状态并给出人话提示。
        self._sync_play_button()
        QMessageBox.warning(self, "播放出错", f"这个文件播不下去了：\n{message}")

    def closeEvent(self, event) -> None:  # noqa: N802
        self.engine.shutdown()
        super().closeEvent(event)
