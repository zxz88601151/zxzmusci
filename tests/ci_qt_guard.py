#!/usr/bin/env python3
"""CI 守卫：Qt 用例不得全部被跳过（D3 假绿拦截）。

背景：conftest 的 `qt_app` fixture 在 Qt 平台初始化失败时**降级为 skip**
（这是必要的——否则 headless 环境下会变成收集期硬错误）。但降级带来一个
副作用：整步 pytest 会以"0 通过、N skip"结束，pytest 退出码仍可能是 0，
GitHub 只看到 SUCCESS，于是**一条 Qt 断言都没跑却被当成绿的**。

本脚本读 pytest 的 `--junitxml` 产物（官方稳定契约，不解析任何文本输出），
做三件事：
  1. tests <= 0            → 收集期即失败（平台初始化崩溃 / INTERNALERROR）
  2. passed <= 0           → 全 skip 或全 fail（假绿）
  3. 其余                   → 通过

用法：
    pytest -m qt --junitxml=qt_guard.xml -v --tb=short
    python tests/ci_qt_guard.py qt_guard.xml [pytest_returncode]

退出码：0 = 通过；1 = 假绿/失败。
"""

from __future__ import annotations

import sys
import xml.etree.ElementTree as ET


def summarize(xml_path: str) -> tuple[int, int, int, int]:
    """返回 (tests, passed, failed, skipped)。"""
    root = ET.parse(xml_path).getroot()
    suites = [root] if root.tag == "testsuite" else root.findall("testsuite")
    tests = failed = skipped = 0
    for s in suites:
        tests += int(s.get("tests", 0))
        failed += int(s.get("failures", 0)) + int(s.get("errors", 0))
        skipped += int(s.get("skipped", 0))
    passed = tests - failed - skipped
    return tests, passed, failed, skipped


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("::error::用法: python tests/ci_qt_guard.py <junitxml> [pytest_rc]")
        return 1

    xml_path = argv[1]
    rc = argv[2] if len(argv) > 2 else "?"

    try:
        tests, passed, failed, skipped = summarize(xml_path)
    except (OSError, ET.ParseError) as exc:
        print(f"::error::Qt 守卫：无法解析 junitxml（{exc}）—— 收集期可能已崩溃")
        return 1

    print(
        f"Qt 守卫：tests={tests} passed={passed} failed={failed} "
        f"skipped={skipped} pytest_rc={rc}"
    )

    if tests <= 0:
        print("::error::Qt 用例数=0 —— 收集期即失败（平台初始化崩溃？）")
        return 1
    if failed > 0:
        print(f"::error::Qt 用例有 {failed} 条失败（tests={tests}）")
        return 1
    if passed <= 0:
        print(
            f"::error::Qt 用例 0 passed（failed={failed} skipped={skipped} / tests={tests}）"
            " —— QT_QPA_PLATFORM 或 Qt 系统库未生效，本步是假绿"
        )
        return 1

    print(f"GUARD PASS: Qt 用例确实执行并通过了 {passed} 条")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
