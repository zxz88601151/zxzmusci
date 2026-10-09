"""纯函数音频合成器 —— 零依赖，不 import miniaudio / Qt。

提供两套接口：
- 原稿接口：bytes（16bit mono），配合 wave 模块写 WAV 文件
- 本项目接口：array('h')（16bit stereo 交织），配合引擎的 PCM 层断言
"""

from __future__ import annotations

import array
import math
import random
import struct


# ---------- 原稿接口：bytes / mono ----------
def generate_sine(freq: float = 440.0, duration_ms: int = 100,
                  sample_rate: int = 44100, amplitude: float = 0.8) -> bytes:
    """生成正弦波 PCM（16bit mono）。"""
    n = int(sample_rate * duration_ms / 1000)
    samples = [int(amplitude * math.sin(2 * math.pi * freq * i / sample_rate) * 32767)
               for i in range(n)]
    return struct.pack(f"<{n}h", *samples)


def generate_white_noise(duration_ms: int = 100, sample_rate: int = 44100,
                         amplitude: float = 0.8) -> bytes:
    """生成白噪声 PCM（16bit mono）。"""
    n = int(sample_rate * duration_ms / 1000)
    samples = [int(amplitude * (2 * random.random() - 1) * 32767) for _ in range(n)]
    return struct.pack(f"<{n}h", *samples)


def generate_silence(duration_ms: int = 100, sample_rate: int = 44100) -> bytes:
    """生成静音 PCM（16bit mono）。"""
    return b"\x00\x00" * int(sample_rate * duration_ms / 1000)


def compute_rms(samples: bytes) -> float:
    """PCM bytes → 归一化 RMS（0~1）。"""
    if len(samples) < 2:
        return 0.0
    n = len(samples) // 2
    values = struct.unpack(f"<{n}h", samples)
    return math.sqrt(sum(v * v for v in values) / n) / 32767.0


def compute_clipping(samples: bytes) -> bool:
    """PCM bytes 是否削波（超出 [-1.0, 1.0]）。"""
    if len(samples) < 2:
        return False
    n = len(samples) // 2
    return any(abs(v / 32767.0) > 1.0 for v in struct.unpack(f"<{n}h", samples))


# ---------- 本项目接口：array('h') / stereo 交织 ----------
def sine_frames(frames: int, freq: float = 440.0, amp: int = 20000,
                rate: int = 44100) -> array.array:
    """生成 stereo 交织正弦 PCM（左右同相）。"""
    out = array.array("h")
    for i in range(frames):
        v = int(amp * math.sin(2 * math.pi * freq * i / rate))
        out.append(v)
        out.append(v)
    return out


def const_frames(frames: int, val: int = 10000) -> array.array:
    """生成 stereo 交织常量 PCM（便于从输出反推逐帧增益）。"""
    return array.array("h", [val, val] * frames)


def rms_array(chunk: array.array) -> float:
    """array('h') → 归一化 RMS。"""
    if len(chunk) == 0:
        return 0.0
    return math.sqrt(sum(s * s for s in chunk) / len(chunk)) / 32767.0


def peak_array(chunk: array.array) -> int:
    return max((abs(s) for s in chunk), default=0)
