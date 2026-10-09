# 音乐播放器（musicplayer）

完全离线、无登录的桌面音乐播放器。播放器本体全部从零自写；
解码只用基础设施库（miniaudio / ffmpeg / mutagen / numpy），不参考任何现有播放器项目。

定案报告：`~/workspace/your_files/音乐播放器定案报告-V1.0.md`

## 技术栈

- UI：Python + PySide6
- 播放解码：python-miniaudio（MP3/WAV/FLAC/OGG/Opus）
- 万能管道：ffmpeg 二进制（M4A/ALAC/APE/WMA/MP4 解码；MP4→MP3 提取；MP3 压缩）
- 标签：mutagen；曲库：sqlite3；数值：numpy；打包：PyInstaller

## 运行（开发）

```bash
pip install -r requirements.txt
python -m player.app          # 从 src/ 目录运行，或把 src 加入 PYTHONPATH
```

Windows 上：`set PYTHONPATH=src && python -m player.app`

## 当前进度

- [x] Phase 0：工程骨架 + AudioEngine + 最小主窗口
- [ ] M1 出声验证（需 Windows 真机）
- [ ] Phase 1：核心播放（队列/gapless/循环随机/快捷键/淡入淡出）
- [ ] Phase 2：曲库（扫描流水线/搜索/浏览视图/播放列表）
- [ ] Phase 3：工具（提取/压缩/批量）
- [ ] Phase 4：体验（壁纸/频谱/EQ/歌词/托盘/断点续播）
- [ ] Phase 5：PyInstaller 打包发布
