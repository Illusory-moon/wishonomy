# -*- coding: utf-8 -*-
"""容器内自检：**她笔记里那一行 ↔ 键文件里的哈希** 对得上吗（`canon/_29` 的跨进程契约 ✓）。

用途：部署 / 轮换之后跑一次，确认"面板拿到的哈希"就是"她笔记里那把口令"的哈希 ✓。
明文不入日志 ✓（只打印长度与两个时刻）。
"""
import datetime as dt
import importlib.util
import json
import os
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
HERE = os.path.dirname(os.path.abspath(__file__))
NOTES = os.environ.get("WISH_NOTES", "/opt/astrbot/data/sparxie_notes.md")
sys.path.insert(0, HERE)

spec = importlib.util.spec_from_file_location("rt_check", os.path.join(HERE, "rotate_gate.py"))
rt = importlib.util.module_from_spec(spec)
sys.modules["rt_check"] = rt
spec.loader.exec_module(rt)

conf = rt.read_config()
mod = rt.load_plugin(conf)
merged = mod.merge_config(conf)
with open(NOTES, encoding="utf-8") as handle:
    notes = handle.read()
code, expiry = mod.gate_read(notes)
if not code:
    print("✗ 她笔记里没有**有效**的口令行（过期了？）")
    sys.exit(1)
keys_path = mod.gate_keys_path(merged)
with open(keys_path, encoding="utf-8") as handle:
    keys = json.load(handle)
entry = keys.get("2713420580") or {}
rotated = (expiry - dt.timedelta(hours=mod.MOOD_GATE_TTL_HOURS)).strftime("%Y-%m-%d %H:%M:%S")
expiry_text = expiry.strftime("%Y-%m-%d %H:%M:%S")
print("口令长度 = %d（不打印值 ✓）" % len(code))
print("笔记那行：到期 %s ⇒ 反推轮换时刻 %s" % (expiry_text, rotated))
print("键文件  ：rotated %s" % entry.get("rotated"))
ok_rot = rotated == entry.get("rotated")
ok_hash = mod.gate_hash(code, expiry_text, rotated) == entry.get("hash")
print("口径一致 = %s ｜ 哈希一致 = %s" % (ok_rot, ok_hash))
sys.exit(0 if (ok_rot and ok_hash) else 1)
