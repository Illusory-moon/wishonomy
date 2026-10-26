# -*- coding: utf-8 -*-
"""容器内自检：**她笔记里那一行 ↔ 键文件里的哈希** 对得上吗（`docs/canon-29.md` 的跨进程契约 ✓）。

用途：部署 / 轮换之后跑一次，确认"面板拿到的哈希"就是"她笔记里那把口令"的哈希 ✓。
明文不入日志 ✓（只打印长度与两个时刻）。

⚠️ **不写死任何路径与 bot 号** ✗：走 `rotate_gate.py` 那套（配置 + `WISH_GATE_*` 环境变量 ✓）。
"""
import datetime as dt
import importlib.util
import json
import os
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

spec = importlib.util.spec_from_file_location("rt_check", os.path.join(HERE, "rotate_gate.py"))
rt = importlib.util.module_from_spec(spec)
sys.modules["rt_check"] = rt
spec.loader.exec_module(rt)

conf = rt.read_config()
mod = rt.load_plugin(conf)
merged = mod.merge_config(conf)
notes_path, sid, keys_path = mod.gate_default_notes("", merged)
if not notes_path:
    print("✗ 不知道该读哪个笔记（配 `notes_path` 或给 `WISH_GATE_NOTES`）")
    sys.exit(1)
if not sid:
    print("✗ 不知道该核哪个 bot（配 `notes_path` 或给 `WISH_GATE_SELF_ID`）")
    sys.exit(1)
with open(notes_path, encoding="utf-8") as handle:
    notes = handle.read()
code, expiry = mod.gate_read(notes)
if not code:
    print("✗ 那个笔记里没有**有效**的口令行（过期了？）")
    sys.exit(1)
with open(keys_path, encoding="utf-8") as handle:
    keys = json.load(handle)
entry = keys.get(str(sid)) or {}
rotated = (expiry - dt.timedelta(hours=mod.MOOD_GATE_TTL_HOURS)).strftime("%Y-%m-%d %H:%M:%S")
expiry_text = expiry.strftime("%Y-%m-%d %H:%M:%S")
print("bot = %s ｜ 笔记 = %s" % (sid, notes_path))
print("口令长度 = %d（不打印值 ✓）" % len(code))
print("笔记那行：到期 %s ⇒ 反推轮换时刻 %s" % (expiry_text, rotated))
print("键文件  ：rotated %s" % entry.get("rotated"))
ok_rot = rotated == entry.get("rotated")
ok_hash = mod.gate_hash(code, expiry_text, rotated) == entry.get("hash")
print("口径一致 = %s ｜ 哈希一致 = %s" % (ok_rot, ok_hash))
sys.exit(0 if (ok_rot and ok_hash) else 1)
