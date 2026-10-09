# 破坏性审计报告 —— 测试基建（第 4 轮）

> 审计人：资深测试架构师 / 代码审计专家
> 审计时点：M/F 段替身自检通过之后（替身可信窗口）
> 方法：**只做代码走查 + 假设验证**（16 组探针 A~V，实跑，不写产品代码）
> 基线：`24 passed, 2 skipped in 2.12s`（py312-embed / 本机无 PySide6）
> 修复后：**`64 passed, 2 skipped`**（详见文末「修复状态」）

---

## 修复状态（第 18 轮落地）

| 编号 | 严重度 | 修复方式 | 自检断言 | 反证结果 |
|---|---|---|---|---|
| D2 | P1 | `engine_factory.settle()` 耗尽即抛 `AssertionError`（带 state/fade_pending 诊断） | `test_settle_contract.py`（10 项）+ `test_f2b` | ✅ 改回静默 → 6 项变红 |
| D3 | P1 | `QApplication` 移出模块顶层（惰性 + skip 降级）；CI 补 Qt 系统库 | `test_ui_error.py` 顶层无 QApplication；收集期不再报错 | ✅ 收集期仅 skip，0 error |
| D4 | P1 | R01~R17 参数化为 **21 个可定位用例** | `test_r00_registry_matches_implementation` | ✅ 断言数与实现比对 |
| D7 | P2 | 删除 `measure_burst_ms()` 占位符（含 `__all__` 导出） | — | ✅ 已无引用点 |
| D5 | P2 | `FakeDevice.close()` 记 EVENTS 并计 `closed` | `test_m9` / `test_m9b` | ✅ 改回 `pass` → 2 项变红 |
| D6 | P2 | 新增 4 类故障注入：`STOP/CLOSE/READ/INFO_ERROR` | `test_m10`~`test_m13` | ✅ 抹掉注入 → 4 项变红 |
| D6-R | — | **设计取舍（非缺陷）**：见下方专条 | `test_m10/m10b/m11` 锁"可降级"契约 | ✅ 断言 error_code is None |
| D1 | P2 | `install()` 收口为 **conftest 顶层唯一入口**（非 pytest_configure，见下） | `test_f6d_install_has_single_entry_point` | ✅ 扫描全仓调用点 |
| G1 | GAP | **已量化未缓解**：`_ramp_out_chunk` 末帧台阶量化 | `test_f7_interrupt.py` G1-a / G1-b（7 项） | ✅ 变异 ramp → 2 项变红 |

### D6-R —— device.stop()/close() 异常不入 ERROR（设计取舍，非缺陷）

原审计设想断言为「`STOP_ERROR`/`CLOSE_ERROR` → `state=ERROR` 且不冒泡」。**实测该设想与实现相反**：

- `player.py` 五处（`227-231` `_stop_now`、`246-251` `_finalize_fade_out`、
   `306-310` poll 清理、`360-371` shutdown、`368` close）一律 `except Exception: pass`；
- 异常**确实不冒泡**（这点与设想一致），但随后照常收敛到 `STOPPED`/`PAUSED`，
  **不置 `State.ERROR`、不记 `ErrorCode`**。

即引擎把"设备关闭失败"定义为**可降级**（用户仍可继续操作，不被弹错）。
`test_m10/m10b/m11` 已按**实际契约**锁定（不冒泡 + 收敛到终态 + `error_code is None` + 故障确实触发过）；
`test_m12`（READ→ERROR/FILE_CORRUPT，含 poll 上报）与 `test_m13`（INFO→load 抛 `AudioError`）
则覆盖"真错误必须上报"的另一侧。

**若未来要改为"设备异常上报 ERROR"**：需重构 `player.py` —— 新增 `_on_device_fault()` 显式入口，
经 `poll()` 事件出口上报（保持"音频线程不碰 Qt"与"状态变更显式化"两条既有约束）。
在未重构前，**按可降级契约锁定是正确做法**，不得写成状态=ERROR（那会是假绿）。

### G1 —— cancel 瞬间旧流末帧增益台阶（已量化未缓解）

**缺口**：F7-5 只验证"新流从接管点起跳"，从没验证**被丢弃的旧流**在 cancel 那一刻如何收尾。
旧流走 `_ramp_out_chunk`（当前有效增益 → 0，一整段 chunk 内线性降），若存在跳变即为爆音源。

**已量化**（`test_f7_interrupt.py`，7 项断言）：
- G1-a 集成路径：代次失效后旧流仍产出 ramp-out 段，末帧增益 ≤ 2 LSB（精确静音）；
- G1-b 单元路径：给非零起始增益（实测 0.0145），验证长度不丢帧、首帧 ≈ 起始增益、
  末帧 ≈ 0、单调不增、逐帧台阶 ≤ 线性步长上界（实测 max_step 0.0001 ≤ ub 0.000228）。

**未缓解**：本项只**量化**了 ramp 形状，不改变 300ms 淡出与 P1-2「seek 后立弃旧代次」的固有取舍
（见 `player.py:269-271` 的「此处需要重构 X」）；真实爆音仍只能在真机 M1 听感验证。
标注为 **GAP（已量化未缓解）**，供 M1 阶段复测。

### D2 修复后变红的项

无。全量 64 passed —— 说明既有断言在严格语义下**本来就都成立**，
原先的隐患是"若某处 limit 不足则静默通过"，而非"已有用例在裸奔"。
代价是明确了收敛阈值（实测 limit ∈ (5, 10]），由 `test_t_d2_4` 钉为回归基线。

### 关于 D1 的修正说明

审计原建议"收口到 `pytest_configure`"**在技术上不可行**。
`pytest_configure` 晚于 conftest 模块顶层执行，而 `install()` 注入
`sys.modules["miniaudio"]` 必须早于任何测试模块被 import（收集期）。
把它搬进 `pytest_configure` 是把正确改成错误。实际执行的是消除双入口：
6 个业务套件里的重复 `install()` 删除，改为**断言替身已在位**（隐式顺序依赖 → 显式契约）。

### CI 守卫（第 6 步）

`.github/workflows/ci.yml` 新增守卫 step：**Qt 用例"收集到但全 skipped"= 假绿，直接 fail**。
本机四象限验证：无 PySide6 → FAIL；收集到但全 skip → FAIL；有通过项 → PASS；全收集失败 → FAIL。
（原稿守卫有 2 处真 bug：`grep -cE '^tests[/\\].*::test_'` 在 headless 下恒为 0 使守卫**永不触发**；
`grep -c` 非零退出泄漏换行致 `[: integer expected`。已修为 `grep -cE '^tests'` + 显式 `TOTAL<=0` 判空。）

---

## 审计范围

| 类别 | 文件 |
|---|---|
| 基建 | `tests/conftest.py`、`pytest.ini`、`tests/run_pytest.py`、`tests/_audio_stub.py` |
| 替身 | `tests/fixtures/mock_miniaudio.py`、`audio_synthesizer.py`、`engine_factory.py`、`mock_qt.py` |
| 自检套件 | `test_mock_infra.py`（M1~M8）、`test_fixture_health.py`（F1~F6） |
| 业务套件 | `test_volume_curve.py`、`test_fade_envelope.py`、`test_fade_paths.py`、`test_f7_interrupt.py`、`test_regression.py`、`test_ui_error.py` |
| 被审计引擎 | `src/player/engine/player.py`（仅作为"测试能否观测到它"的参照） |
| CI | `.github/workflows/ci.yml` |

**探针清单（A~V，全部实跑）**：单文件独立运行、模块身份同一性、settle 步数分布、settle limit 阈值扫描、reset 完整性、Windows 句柄、marker 收集数、3.10 语法静态扫描、EVENTS/CLOSES 顺序、settle 空转、CI 路径解析模拟、CI 计时。

---

## 发现的问题

| 编号 | 严重度 | 文件 | 描述 | 修复建议 |
|---|---|---|---|---|
| **D1** | **P1** | `conftest.py:44-45` + 6 个业务套件模块顶层 | **`install()` 与实际被调用的 install 存在"双入口"**：conftest 顶层装一次，6 个业务套件（volume_curve / fade_envelope / fade_paths / f7_interrupt / regression / ui_error）各自在模块顶层又 `_audio_stub.install()` 一次。`install()` 只是 `sys.modules["miniaudio"] = _FAKE`，两者指向同一 `_FAKE`（探针 B 已验证：`_audio_stub.BACKEND is mock_miniaudio.BACKEND → True`），所以当前**不出错**。但这意味着"替身必须先于引擎 import"这条性命攸关的顺序约束，被**分散在 7 个文件里各自维护**；任何新增套件漏写就静默依赖 import 顺序，且没人知道到底哪次 install 是"生效的那次"。这是**认知债**，不是当前故障。 | 删掉 6 个业务套件里重复的 `_audio_stub.install()`，只保留 conftest 顶层一处；若担心 `import _audio_stub` 语义，改成断言 `assert sys.modules.get("miniaudio") is not None`。**单一入口**才是可维护的。 |
| **D2** | **P1** | `fixtures/engine_factory.py:44-59` | **`settle()` 静默耗尽，调用方无法区分"收敛"与"超限"**。实跑（探针 J）：`settle(limit=2)` 时引擎停在 `state=playing, fade_pending=True`，**函数不抛异常、不返回任何标志**，安静返回。调用方若写 `settle(e); assert e.state=="stopped"` 尚能兜住；但大量既有断言是 `settle(e); check(state) 收集进 _FAILS`，一旦 limit 耗尽，被审计的其实是"没走完的半途状态"，极易被后续断言误判为通过。 | `settle()` 应在耗尽时 `raise AssertionError(f"settle 未收敛：state={engine.state} fade_pending=...")`，或返回 `bool` 并由调用方断言。**这是本轮唯一能直接制造"假绿"的结构性缺陷**。 |
| **D3** | **P1** | `.github/workflows/ci.yml:33-36` + `tests/test_ui_error.py:53-55` | **CI 的 Qt 步骤在 Ubuntu headless 上有明确翻车风险，且没有任何 xvfb 兜底**。`test_ui_error.py` 在**模块顶层**（import 期）就 `from PySide6.QtWidgets import QApplication; app = QApplication([])`。`QApplication` 构造需要 QtGui 平台插件；`QT_QPA_PLATFORM=offscreen` 只免 X server，**不免 Linux 下加载 QtGui 的动态库依赖**（libEGL / libGL / libxkbcommon 等）。GitHub `ubuntu-latest` 上这些库**不保证齐备**，一旦缺失，`QApplication([])` 在**收集期**就抛 `ImportError/OSError`，`pytest -m qt` 整步失败（不是 skip）。**本地无法直验**（本机无 PySide6），属"必须在 CI 上实测确认"的项。 | 二选一：① `- name: Qt 测试` 前加 `sudo apt-get install -y libegl1 libgl1 libxkbcommon-x11-0 libdbus-1-3`；② 或加 xvfb 包装 `xvfb-run -a pytest -m qt`；③ 最稳：把 `QApplication([])` 从模块顶层**移进 `qt_app` fixture**，让平台初始化失败能被 `pytest.importorskip` 风格优雅降级为 skip 而非硬失败。 |
| **D4** | **P1** | `.github/workflows/ci.yml:38-39` + `test_regression.py:31` | **"回归套件（每轮必须绿）"这一步实际只跑 1 个测试函数**。实跑（探针 F）：`-m regression` = `1/25 collected (24 deselected)`。`R01~R16` 共 16 组回归断言**全部塞在 `test_regression.py` 的单个 `def test_...()` 里**（文件 375 行仅 1 个函数）。这意味着：任一断言挂掉 → 整步红，**无法从测试名定位是哪条 R0x**；且该步的"绿"只代表这 1 个函数绿。CI 上这三步（engine 23 + qt 1 + regression 1）合计仍覆盖 25 个，但**步骤划分的语义是误导性的**——看 CI 面板会以为 regression 是独立一份保障。 | regression 拆成 16 个独立 `def test_r01_...` / `test_r16_...`，或至少 `@pytest.mark.parametrize`。这本来就是 M/F 段已经示范过的正确做法（F 段拆得就很干净）。 |
| **D5** | **P2** | `conftest.py:134-143` `engine` fixture | **`engine` fixture 在 teardown 只 `shutdown()`，不清理该 fixture 创建的 `miniaudio` 设备引用是否真被 `close`**。实跑（探针 N2）：仅走 `shutdown()` 路径时 `EVENTS=['device.stop']`、`CLOSES=[]`——即 **`FakeDevice.close()` 是空实现（`mock_miniaudio.py:103-104`），既不进 EVENTS 也不进 CLOSES**。于是"shutdown 是否真的关闭了设备"**在替身里不可观测**。真实 miniaudio 的设备句柄泄漏（声卡被占）是 Windows 上最经典的"跑完测试后放不出声"故障，而我们的替身恰好对这一路径**做了哑替身**。 | 给 `FakeDevice.close()` 补 `EVENTS.append("device.close")`；并加一条断言"shutdown 后 device.close 恰好被调用 1 次"。当前 `EVENTS` 注释写着"验证 R10 关闭顺序"，但 device.close 根本没记录 → 该注释名不副实。 |
| **D6** | **P2** | `fixtures/mock_miniaudio.py:96-101` `FakeDevice.stop()` | **`FakeDevice` 是"全乐观替身"，零异常面**：`start`/`stop`/`close` 除 `start_fault` 外永不抛。真实 `PlaybackDevice.stop()` 在设备已失效时会抛；`close()` 在重复 close 时会抛。因此 `player.py` 里那 6 处 `try: self._device.stop() except Exception: pass`（第 229/248/289/308/362/368 行）**在测试里永远是"没触发 except"的分支**——这些防御代码的**覆盖率是虚的**（假绿高发区）。 | 增加 `BACKEND["stop_fault"]` / `["close_fault"]` 注入点，至少各写 1 条"stop 抛异常时引擎仍能走到 STOPPED"的断言。否则 6 处 except 是死代码。 |
| **D7** | **P2** | `fixtures/engine_factory.py:73-76` | **`measure_burst_ms()` 是纯占位符**：函数体 `return 0.0`，形参 `loads`/`poll_interval` 完全没用上，注释自认"占位：实际测量在测试内联完成"。它被 `__all__` 导出，任何新套件若误用它，会拿到恒 0 的"耗时"，从而**在 F7 这类性能断言上制造绝对假绿**。 | 要么删掉（连同 `__all__` 里的导出），要么如实实现。**留着比删掉危险**——它看起来像一个可用的测量工具。 |
| **D8** | **P2** | `tests/*.py` 各巨型 `test_xxx()` | **断言总数是"单测函数里手写 check()"的 218 项，而 pytest 只看到 25 个 test item**。`check()` 框架（`_FAILS` + 末尾 `assert not _FAILS`）本身**是正确的**（已验证失败会传播，不会吞异常）。但后果是：**任何一条断言失败，pytest 只报"`test_volume_curve` 失败"，不给行号定位**（除非看 stdout 里的 `FAIL xxx` 行）。CI 的 `--tb=short` 在这种结构下帮助有限。这是"以断言数量冒充测试数量"的经典反模式。 | 短期可接受（有 `FAIL name` 打印）；中期应把 218 项按语义切成 ~40 个 `def test_`，让失败可直接定位。至少给 `check()` 补 `raise` 模式开关，CI 上开严格模式。 |
| **D9** | **P2** | `.github/workflows/ci.yml:42-43` | **覆盖率步骤只在 3.11 跑，且 `--cov=src/player` 在 `-m engine` 下统计的是"被 engine 标记测试触及的代码"**。由于 D1（qt 测试被排除在外）与 D6（except 分支测不到），覆盖率数字会**系统性虚高**，且没有任何门控（`pytest.ini` 明确写了"不设 --cov-fail-under"）。作为"参考数字"可接受，但**不要用它做质量判断**。 | 保留现状可以，但建议把该步骤改为 `-m "engine or qt"` 且去掉 `--cov-fail-under` 注释里"初期"这种会过期的措辞。 |
| **D10** | **Info** | `fixtures/mock_miniaudio.py:66` | `_FAKE = types.ModuleType("miniaudio")` 是裸模块，**缺 `__spec__`/`__file__`/`__path__`/`__loader__`**（探针 L 实测：`__spec__=None`、`__file__` MISSING）。当前引擎只用 `import miniaudio` + 属性访问，**无影响**。但若未来引入任何走 `importlib.util.find_spec` 或 `module.__file__` 的第三方库/工具（如 pytest-cov 的源码定位、`pkgutil` 遍历），会在替身上翻车。 | 低成本加固：`_FAKE.__spec__ = importlib.machinery.ModuleSpec("miniaudio", None)`；或直接写一个真 `.py` 文件（如 `tests/fixtures/_miniaudio_real_shim.py`）再 `importlib` 载入。当前不紧急。 |
| **D11** | **Info** | `conftest.py:23` | `os.environ.setdefault("QT_QPA_PLATFORM","offscreen")` 用 **`setdefault`**：若 CI 环境已有该变量（例如被设为 `xcb`），offscreen 不生效，Qt 测试会去找 X server 而失败。语义上"我兜底，但不覆盖用户显式设置"是对的，**但 CI 上没有"用户显式设置"，这个分支只会在意外注入时咬人**。 | 在 CI 的 qt 步骤里**显式** `env: QT_QPA_PLATFORM: offscreen`（现在只有 qt 步骤有此 env，是对的）——保持；再在 conftest 里加一句 CI 场景的 logger.debug 输出实际取值，便于排查。 |
| **D12** | **Info** | `conftest.py:161-167` `qt_app` fixture | `qt_app` 是 **session scope**，但 `test_ui_error.py` **根本没用它**——该文件自己在模块顶层建了 `QApplication([])`。于是 conftest 提供的 `qt_app` 是**死 fixture**（25 个 test item 中 0 个消费它），而真正生效的 QApplication 在别处。下次有人改 `qt_app` 会以为改了 Qt 测试行为，**实际毫无影响**。 | 让 `test_ui_error.py` 改用 `qt_app` fixture（配合 D3 把平台初始化移进 fixture），删掉模块顶层的 `app = QApplication([])`。一处定义，一处生效。 |

---

## 未发现问题的维度（已实跑验证，结论可信）

### 维度一：Fixture 泄漏 —— **基本干净，1 处认知债（D1）**

- ✅ `engine` / `mock_device` / `audio_dir` / `sine_file` 等**全部是 function scope**（无 session 级可变状态泄漏）。
- ✅ `_reset_backend_each_test` 是 `autouse=True` 且 **function scope**，每例前 `reset()`。
- ✅ `reset()` **完整清除所有模块级可变状态**（探针 D）：`BACKEND` 复位到 `DEFAULTS`（含人工注入的 `custom_leak` 键也被清掉）、`EVENTS.clear()`、`CLOSES.clear()`。**无遗漏字段**。
- ✅ **替身模块里不存在模块级 `FakeDevice` 实例、也不存在模块级 generator**（探针 D 静态扫描 `vars(m)`）——即"没有跨用例残留的线程/设备"。
- ✅ **替身内部零线程**（探针：`grep threading` 在 `fixtures/` 只命中注释）→ 不存在"漏掉的 `threading.Thread` 实例"。
- ✅ `autouse` reset **跨文件**同样生效（探针 K 反证：文件 A 污染 `BACKEND['duration']=12345` → 文件 B 读到 `5.0`）。
- ✅ `temp_settings` 类 fixture **本项目不存在**（settings 持久化测试走 `tmp_path`）；`test_f5` 用 `os.remove` 后紧跟 `gc.collect()`，且探针 E 实测 **shutdown 后无需 gc 即可删除文件**（Windows 句柄已释放）→ 用户提的 `pathlib.unlink(missing_ok=True)` 建议在此**非必要**。
- ⚠️ D1：`install()` 双入口（7 处）。

### 维度二：竞态假绿 —— **有 1 处结构性假绿（D2），其余健康**

- ✅ **`settle()` 用轮询 `state`/`fade_pending`，不用 `time.sleep`**（探针：`grep time.sleep` 在 fixtures 只命中注释）→ 用户担心的"sleep 不够长导致 flaky"**不成立**；反过来是"轮询上限"问题，见 D2。
- ✅ 超时保护**存在**：`limit=900` 硬上限，不会死循环。
- ✅ **替身内部无线程**（`FakeDevice` 是"被动拉取型"，`is_running` 靠 `gen is not None` 判）→ 用户提的"替身内部线程与主线程竞态恰好不触发"**不成立**，这是替身的正确设计选择。
- ✅ **`_generation` 每次 `create_engine()` 真正从 0 开始**（F1 断言 + 探针全绿）；模块级无共享设备 → 两引擎互不影响（F6c 已验证）。
- ⚠️ **实测 settle 真实步数**（探针 C）：`fade_out=300ms / chunk=45.35ms` → 真实需 ~7 步收敛；`limit=900` **余量 128×**，因此缩到 50 也是绿的，**缩超时不会暴露竞态**。真正的假绿窗口在 **D2**：`limit=1..5` 时 settle 静默耗尽、引擎停在 `playing`，而**函数自己不报错**。
- ✅ 我按你的方法把 limit 从 900 降到 1/2/3/5/10 实跑（探针 C2）：`1~5` 确实红、`10` 绿 —— 证明**有窗口，但没有被 settle 自身捕获**，只能靠调用方后续 `assert state` 兜。若某条断言恰恰不断言 state，就是静默假绿。

### 维度三：导入顺序陷阱 —— **当前安全，但对"顺序"的依赖过度分散**

- ✅ `install()` 在 **conftest 模块顶层**（第 45 行），**不在** `pytest_configure`。这在本项目**是正确选择**：pytest 保证 conftest 先于测试模块加载，而测试模块在**收集期**就 import 引擎——放 `pytest_configure` 其实也可行（该钩子同样早于收集），但放顶层更直接、更少意外。
- ✅ **7 个文件各自单独跑全部通过**（探针 A3）：`test_volume_curve` / `test_fade_envelope` / `test_f7_interrupt` / `test_regression` / `test_fade_paths` / `test_mock_infra` / `test_fixture_health` 单文件独立运行**均绿**。你提的 `pytest teststest_xxx.py`（路径粘连）在**当前目录结构下不构成问题**。
- ✅ `cd tests && pytest test_volume_curve.py`（探针 A2，模拟"不经过 tests/ 目录"）**同样通过**——因为 conftest 的 `sys.path` 注入是基于 `__file__` 绝对路径，不依赖 CWD。
- ✅ 三个含 `install()` 的文件**任意顺序**跑都绿（探针 A4）。
- ✅ **无 3.10 不兼容语法**（探针 H 静态扫描：`match`/`except*`/`type X =` 均无；`from typing import` 只用 `Optional`/`Callable`，3.10 全支持）。
- ⚠️ D1（双入口）、D12（`qt_app` 是死 fixture）。
- ℹ️ `install()` 是**幂等**的（`sys.modules["miniaudio"] = _FAKE` 重复执行只是重赋同一个对象，探针 B 验证 `m1._FAKE is sys.modules['miniaudio'] → True`），所以双入口当前无害。

### 维度四：CI 环境差异 —— **有 2 处需在真实 CI 上确认（D3/D4）**

- ✅ **Python 版本矩阵 [3.10, 3.11, 3.12] 覆盖了本机版本**——本机隔离环境实为 **3.12.10**（`py312-embed`），在矩阵内。✅ 测试代码**无任何 3.11+ 独有 API**（无 `types.NoneType`、无 `StrEnum`、无 `tomllib`、无 `ExceptionGroup`）。
- ✅ **`wave` 是纯标准库**，任何 Python 安装都有 → 你担心的"CI 有没有 wave"**不成立**。
- ✅ **WAV 写出/读入路径一致**：`_write_wav` 用 `wave` 写 mono/16bit/44100，替身 `stream_file` 只按扩展名识别（不真读文件内容），`get_file_info` 返回 `BACKEND["duration"]`——两条路径**不通过文件内容耦合**，无"路径不一致"问题。
- ✅ **CI 的 `pytest -m engine` 在无 PySide6 环境实测 22 passed / 2 skipped**（探针 U：numpy 与 PySide6 缺失各自 skip 1 项），**用时 1.94s**，`timeout-minutes: 10` 余量充足。
- ✅ **CI 不装 numpy 是安全的**：M5 的 FFT 交叉验证在无 numpy 时**优雅 skip**（`test_mock_infra.py:192`），纯 Python 过零法已独立完成验证。
- ⚠️ D3：**`QT_QPA_PLATFORM=offscreen` 在 Ubuntu headless 上是否足够，必须在真实 CI 上实测**。PySide6-Essentials 含 QtWidgets，但 Linux 下加载 QtGui 需系统库（libEGL/libxkbcommon 等），GitHub runner 不保证齐备；且 `test_ui_error.py` 在**收集期**就建 QApplication，一旦失败是**硬错误**不是 skip。**这是本轮唯一"本地无法验证、必须 CI 实测"的项。**
- ⚠️ D4：CI 的 `regression` 步骤实际只跑 1 个函数。

---

## 需要重构的地方（按项目约定显式标注）

> 按项目约定："无法在不改架构下满足的不变式须写「此处需要重构 X」，不许悄悄放宽断言。"

1. **`settle()` 的失败语义缺失（D2）** —— 此处需要重构 `engine_factory.settle()`：把"静默耗尽"改为"耗尽即抛"。这是测试基建的**契约缺陷**，不重构则任何依赖 settle 的断言都可能假绿。
2. **`FakeDevice` 的异常面为零（D5/D6）** —— 此处需要重构替身：`close()` 需进 EVENTS；`stop()/close()` 需可注入异常。否则引擎里 6 处 `except Exception: pass` 与 `device.close()` 路径**永远测不到**，覆盖率虚高。
3. **`measure_burst_ms()` 是恒 0 占位（D7）** —— 此处需要重构或删除。保留一个返回假数据的"测量工具"比对 F7 性能断言的杀伤力大于它的便利。
4. **`install()` 双入口（D1）** —— 此处需要重构为单一入口。当前无害纯因 `install()` 幂等；这是"靠巧合正确"，不是"靠设计正确"。

---

## 审计局限

- **未在本机验证 PySide6 相关路径**：本机无 PySide6，`test_ui_error.py`（qt 标记）在本地始终 skip，**24 passed 里不含它**。CI 上 qt 步骤的成败（D3）**只能 CI 实测**。
- **未做真实多线程压力测试**：替身内部零线程，本审计无法覆盖"真实 miniaudio 音频线程"与主线程的竞态。**G1（尾音截断）已在本机以单元+集成两级量化**（见「修复状态 · G1」，7 项断言，变异证明有效）；**G2（音频回调内分配内存）依然只能在真机 M1 验证**。量化 ≠ 缓解：G1 的真实听感仍待 M1。
- **未覆盖真实设备路径**：`FakeDevice` 只模拟被动拉取，真实声卡打开/占用/热插拔/采样率协商失败等路径**完全未测**。
- **未做跨平台验证**：句柄泄漏探针（E）只在 Windows 结论为"OK"；Linux/macOS 的文件锁语义不同，未验证。
- **未审计 `src/player/ui/`**：本报告只审测试基建，UI 代码本身（`main_window.py` 等）不在范围。
- **`--trace-config` 未实跑**：用户建议的 `pytest -xvs --trace-config` 需 `pytest-traceconfig` 插件，当前环境未装；我改用**等价的实跑探针**（模块身份、reset 完整性、跨文件污染反证、逐文件独立跑）达成同一验证目标。

## 建议后续补充

1. **CI 上先绿一次 qt 步骤**（D3 + 守卫 step），这是唯一必须靠 CI 才能定的问题。
2. ~~**给 settle() 补失败语义**（D2）~~ —— ✅ 已完成（`test_settle_contract.py`）。
3. ~~**替身补异常注入 + device.close 事件**（D5/D6）~~ —— ✅ 已完成（`test_m9`~`test_m14`）。
4. ~~**regression 拆函数**（D4）~~ —— ✅ 已完成（21 个可定位用例）。
5. ~~**G1 尾音台阶量化**~~ —— ✅ 已完成（`test_f7_interrupt.py` G1-a/G1-b，7 项）。
6. **M1 真机出声验证**——测试基建再可信，也替代不了"真的响"。G1/G2 的听感结论待此项。

---

## 一句话结论

**测试基建没有发现会导致假绿的 P0 缺陷；替身可信、隔离干净、CI 路径解析正确。**
原存在 **1 处可制造假绿的结构性缺陷（settle 静默耗尽，D2）**、**1 处只能在 CI 实测的风险
（Qt offscreen 系统库，D3）**、**3 处"靠巧合正确/哑替身"的认知债（D1/D5/D6）**，
以及 **2 项 GAP（G1 尾音台阶已量化 / G2 回调内分配待真机）**。
D1~D7 已全部落地，G1 已量化，全量 **64 passed, 2 skipped**，`src/` 零改动。
