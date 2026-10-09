# 测试基建（专项 B）

**一键跑**：

```bash
python tests/run_pytest.py                 # 全量（218 项断言，约 2.2s）
python tests/run_pytest.py -m engine       # 纯引擎测试（无需 Qt / 显示器）
python tests/run_pytest.py -m regression   # 回归套件（每轮必须绿）
python tests/run_pytest.py -m qt           # UI 测试（真实 Qt offscreen）
SKIP_SLOW=1 python tests/run_pytest.py     # 跳过 slow 标记
python tests/run_pytest.py -k f7           # 只跑 F7 相关
```

> 为什么需要 `run_pytest.py` 而不是直接 `pytest`：本机外层环境会剥离 `PYTHONPATH`，
> 而 PySide6 / pytest 装在隔离目录里，必须在**进程内**注入 `sys.path`。
> CI 上依赖装进 site-packages，直接 `pytest` 即可。

## 目录结构

```
tests/
├── __init__.py                 # （无，tests 不是包——保持 pytest 默认 rootdir 插入语义）
├── conftest.py                 # 替身注入(模块顶层!) + markers + 自动跳过 + 共享 fixture
├── fixtures/
│   ├── __init__.py
│   ├── mock_miniaudio.py       # miniaudio 替身（设备/解码/故障注入）
│   ├── mock_qt.py              # Qt 最小替身（默认不启用，见文件内说明）
│   ├── audio_synthesizer.py    # 纯函数音频合成器（零依赖）
│   └── engine_factory.py       # 引擎工厂 + settle 辅助
├── _audio_stub.py              # 兼容垫片 → fixtures.mock_miniaudio
├── run_pytest.py               # 一键入口
├── test_volume_curve.py        # 音量曲线 / RMS / 平滑 / V1-V7 / 参数表 / C1-C6
├── test_fade_envelope.py       # 包络 / 路径 / 回归 / C1-C4
├── test_fade_paths.py          # F1-F9 路径
├── test_f7_interrupt.py        # F7-1..F7-9 + B1-B5
├── test_regression.py          # R01-R16 + R17
└── test_ui_error.py            # P1-1 错误统一 / V6 dB / C3 音量恢复（需 PySide6）
```

## 迁移状态表

| 原文件 | 目标 | 断言数 | 状态 |
|---|---|---|---|
| `test_volume_fade.py`（94） | `test_volume_curve.py` + `test_fade_envelope.py` | 94 | ✅ 拆分完成 |
| `test_fade_paths.py`（35） | `test_fade_paths.py` | 35 | ✅ 迁移完成 |
| `test_f7_interrupt.py`（33） | `test_f7_interrupt.py` | 33 | ✅ 迁移完成 |
| `test_regression.py`（32） | `test_regression.py` | 32 | ✅ 迁移完成 |
| `test_ui_error.py`（24） | `test_ui_error.py` | 24 | ✅ 迁移完成 |
| **合计** | | **218** | **全绿** |

## 三个必须知道的坑

1. **替身必须在 conftest 的模块顶层 install()**，不能放 fixture 里（哪怕 session 作用域）。
   测试模块在**收集期**就 import 引擎，fixture 在**运行期**才跑，那时引擎已绑定完毕。
2. **所有套件共用同一个替身**（`sys.modules` 只认首次 import），因此 `BACKEND` 是全局共享的，
   靠 conftest 的 autouse 夹具逐例 `reset()`。新增套件务必注意别改坏别人的 `duration/chunks`。
3. **`settle()` 不能省**。`stop()`/`pause()` 已异步化（F7），终态发生在
   "淡出走完 + `poll()` 收尾"之后。涉及状态变化的断言必须先 `settle()`。
