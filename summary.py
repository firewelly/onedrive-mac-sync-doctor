#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""One-screen health summary for every local OneDrive account.

Verdict is based on *actionable* categories, not the raw count:
a handful of symlinks and the system temp record are expected and harmless,
because OneDrive will never sync those and they never resolve on their own.
What matters is ghosts (metadata pointing at deleted paths) and stubborn items
(files that exist but whose change never lands).
"""
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import od                # noqa: E402
import od_audit          # noqa: E402

ACTIONABLE = ("ghost", "stubborn")
LABELS = {
    "ghost": "幽灵记录（路径已不存在）",
    "stubborn": "顽固文件（需重建）",
    "pending": "在途上传（正常，等它传完）",
    "symlink": "符号链接 / ._ 文件（永不同步，可忽略）",
    "build-artifact": "构建产物（可忽略）",
    "system": "系统 temp 记录（正常）",
}


def state():
    out = subprocess.run([sys.executable, os.path.join(HERE, "od_state.py")],
                         capture_output=True, text=True)
    try:
        return json.loads(out.stdout)
    except Exception:                                          # noqa: BLE001
        return None


def main():
    s = state()
    if not s:
        print("无法读取状态，请直接运行: python3 tools/od_state.py")
        return 1

    print("OneDrive 版本 :", s.get("onedrive_version"))
    load = s.get("load") or ""
    if "load averages:" in load:
        print("系统负载     :", load.split("load averages:")[-1].strip())
    print()

    needs_attention = False
    for name, a in (s.get("accounts") or {}).items():
        if not a.get("db_exists"):
            print(f"[{name}] 未找到数据库")
            continue
        try:
            counts = od_audit.bucket_counts(name)
        except Exception as exc:                               # noqa: BLE001
            print(f"[{name}] 分类失败: {exc}")
            continue

        total = a.get("not_in_sync")
        bad = sum(counts.get(k, 0) for k in ACTIONABLE)
        if bad:
            needs_attention = True

        print(f"[{name}]  未同步总数 {total}   "
              f"完整性 {a.get('integrity')}   "
              f"队列 {a.get('change_records')}")
        if not counts:
            print("    （无卡住条目）")
        for kind in ("ghost", "stubborn", "pending", "symlink",
                     "build-artifact", "system"):
            n = counts.get(kind, 0)
            if n:
                mark = "← 需处理" if kind in ACTIONABLE else ""
                print(f"    {n:>5}  {LABELS.get(kind, kind)} {mark}")
        print()

    print("-" * 56)
    if needs_attention:
        print("结论: 有需要处理的条目。详见上面标记，处理方法见 START-HERE.md")
    else:
        print("结论: 健康。剩下的软链/系统记录不影响同步，不用管。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
