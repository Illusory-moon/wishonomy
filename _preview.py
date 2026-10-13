# -*- coding: utf-8 -*-
"""面板预览：把自检那套种子数据渲染成三张可离线打开的页面（临时用）。

跑法：
    D:\\smm-tempsparxie\\.venv\\Scripts\\python.exe D:\\sparxie\\wishonomy\\_preview.py

产物在 `%TEMP%\\wishonomy-panel-preview\\`（含一张演示库，看完整目录删掉即可）。
只读 `panel.py` / `panelcheck.py` 的公开口径，**不碰仓库里任何文件** ✓
"""
import importlib.util
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
OUT = os.path.join(tempfile.gettempdir(), "wishonomy-panel-preview")

spec = importlib.util.spec_from_file_location(
    "wishonomy_panelcheck", os.path.join(HERE, "panelcheck.py"))
pc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pc)
env = pc.Env(OUT)
env.seed()
data = env.panel.snapshot(env.db, self_id=env.sc.BOT)

blank_bot = {"self_id": "2713", "day": "2026-10-13",
             "today": {"issued": 0, "gifted": 0, "granted": 0, "refunded": 0,
                       "wallets": 0, "total": 0},
             "today_detail": {"open_ious": 0, "iou_amount": 0, "credit_up": 0,
                              "credit_down": 0, "top": []},
             "wallets": [], "ious": [], "brief": []}
pages = {
    "1-有数据.html": env.panel.render(data),
    "2-读不到账本.html": env.panel.render(
        {"available": False, "path": os.path.join(OUT, "missing.db"),
         "reason": "数据库还不存在", "now": data["now"]}),
    "3-空账本.html": env.panel.render(
        {"available": True, "path": "empty.db", "now": data["now"], "bots": [blank_bot]}),
}
for name, text in pages.items():
    with open(os.path.join(OUT, name), "w", encoding="utf-8") as handle:
        handle.write(text)
    print("  %-18s %d 字节" % (name, len(text)))
print("预览目录（整目录可删）：%s" % OUT)
