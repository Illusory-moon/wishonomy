# -*- coding: utf-8 -*-
"""愿力中心 · 只读面板（P6）。

给**运营**看的一页：发行量 / 送礼 / 支出 / 欠条 / 信誉分 / 钱包排行 / 最近流水。

三条纪律（代码保证 ✓）：
  1. **只读**：连接一律用 SQLite 的 `mode=ro` URI 打开 —— 面板**写不了**库 ✗
     （连 WAL 都不去碰：只读连接不会建 `-wal`/`-shm` ✓）
  2. **不占群里任何一句话**：它是本地页面，不是 bot 的功能 ✓
  3. **转义**：流水里的 `reason` 是用户/她写的原话 ⇒ 一律 `html.escape`（防 XSS ✓）

跑法：
    python panel.py                      # 起在 127.0.0.1:8765
    python panel.py --port 9000          # 换端口
    python panel.py --db <ledger.db>     # 指定数据库
    python panel.py --json               # 不开服务，直接吐 JSON（给脚本/部署后核对用）
"""
import argparse
import html
import json
import os
import sqlite3
import sys
import time
import types
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def _ensure_astrbot_stub():
    """让面板能**独立跑**：本机没有 AstrBot 时，给 `import main` 铺一层最小桩。

    （线上/容器里当然有真的 AstrBot，这个函数会立刻返回 ✓）
    """
    if "astrbot" in sys.modules or importlib_has_astrbot():
        return
    api = types.ModuleType("astrbot.api")
    event_api = types.ModuleType("astrbot.api.event")
    star_api = types.ModuleType("astrbot.api.star")
    root = types.ModuleType("astrbot")
    core = types.ModuleType("astrbot.core")
    agent = types.ModuleType("astrbot.core.agent")
    agent_message = types.ModuleType("astrbot.core.agent.message")

    class TextPart:                     # 插件真的会用它，得是真类 ✓
        def __init__(self, text):
            self.text = text

        def mark_as_temp(self):
            return self

    api.AstrBotConfig = dict
    api.llm_tool = lambda *a, **k: (lambda fn: fn)
    api.logger = types.SimpleNamespace(info=lambda *a, **k: None,
                                       warning=lambda *a, **k: None,
                                       error=lambda *a, **k: None)
    api.star = star_api
    star_api.Star = type("Star", (), {"__init__": lambda self, context=None: None})
    star_api.Context = object
    star_api.StarTools = types.SimpleNamespace(
        get_data_dir=lambda name: os.path.join(os.path.expanduser("~"), ".astrbot", name))
    event_api.AstrMessageEvent = object
    event_api.filter = types.SimpleNamespace(
        on_llm_request=lambda *a, **k: (lambda fn: fn),
        on_llm_response=lambda *a, **k: (lambda fn: fn),
        on_agent_done=lambda *a, **k: (lambda fn: fn))
    agent_message.TextPart = TextPart
    for name, module in {
        "astrbot": root, "astrbot.api": api, "astrbot.api.event": event_api,
        "astrbot.api.star": star_api, "astrbot.core": core,
        "astrbot.core.agent": agent, "astrbot.core.agent.message": agent_message,
    }.items():
        sys.modules[name] = module


def importlib_has_astrbot():
    import importlib.util as _iu
    try:
        return _iu.find_spec("astrbot") is not None
    except (ImportError, ValueError):
        return False


_ensure_astrbot_stub()

import main as plugin                                  # noqa: E402 —— 复用同一套口径

CN_TZ = timezone(timedelta(hours=8))
BRIEF_LIMIT = 12
TOP_LIMIT = 12
WALLET_LIMIT = 30


def connect(path):
    """**只读**连接（`mode=ro`）—— 面板要写库就直接失败，这是设计 ✓"""
    uri = "file:%s?mode=ro" % str(path).replace("?", "%3f").replace("#", "%23")
    conn = sqlite3.connect(uri, uri=True, timeout=5.0)
    conn.row_factory = sqlite3.Row
    return conn


def _has_schema(conn):
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='ledger'").fetchone()
    return row is not None


def _name_of(conn, self_id, user_id):
    row = conn.execute("SELECT name FROM account WHERE self_id=? AND user_id=?",
                       (self_id, str(user_id))).fetchone()
    name = str(row["name"] or "") if row is not None else ""
    return name or str(user_id)


def brief_of(conn, self_id, limit=BRIEF_LIMIT):
    """最近流水（最新在前）。"""
    rows = conn.execute(
        "SELECT ts, group_id, from_id, to_id, amount, kind, reason FROM ledger "
        "WHERE self_id=? ORDER BY id DESC LIMIT ?", (self_id, int(limit))).fetchall()
    out = []
    for row in rows:
        out.append({
            "time": datetime.fromtimestamp(int(row["ts"]), CN_TZ).strftime("%m-%d %H:%M"),
            "kind": str(row["kind"]),
            "from": _name_of(conn, self_id, row["from_id"]),
            "to": _name_of(conn, self_id, row["to_id"]),
            "amount": int(row["amount"]),
            "reason": str(row["reason"] or ""),
            "group_id": str(row["group_id"] or ""),
        })
    return out


def wallets_of(conn, self_id, limit=WALLET_LIMIT):
    """钱包一览（愿力多的在前；含未结欠条数）。"""
    rows = conn.execute(
        "SELECT a.user_id, a.name, a.balance, a.credit, a.updated_at, "
        "(SELECT COUNT(*) FROM iou i WHERE i.self_id=a.self_id AND i.debtor=a.user_id "
        " AND i.status='open') AS open_ious "
        "FROM account a WHERE a.self_id=? ORDER BY a.balance DESC, a.updated_at DESC "
        "LIMIT ?", (self_id, int(limit))).fetchall()
    return [{"user_id": str(r["user_id"]), "name": str(r["name"] or "") or str(r["user_id"]),
             "balance": int(r["balance"]), "credit": int(r["credit"]),
             "open_ious": int(r["open_ious"] or 0),
             "updated_at": datetime.fromtimestamp(int(r["updated_at"]), CN_TZ)
             .strftime("%m-%d %H:%M")} for r in rows]


def ious_of(conn, self_id, limit=TOP_LIMIT):
    """还没兑现的要求（欠条）。"""
    rows = conn.execute(
        "SELECT id, ts, debtor, item, amount, due_ts FROM iou "
        "WHERE self_id=? AND status='open' ORDER BY id DESC LIMIT ?",
        (self_id, int(limit))).fetchall()
    return [{"id": int(r["id"]), "debtor": _name_of(conn, self_id, r["debtor"]),
             "item": str(r["item"]), "amount": int(r["amount"]),
             "time": datetime.fromtimestamp(int(r["ts"]), CN_TZ).strftime("%m-%d %H:%M"),
             "due": (datetime.fromtimestamp(int(r["due_ts"]), CN_TZ).strftime("%m-%d")
                     if r["due_ts"] else "")} for r in rows]


def snapshot(path, now=None, self_id=None):
    """面板要的全部数据（纯读 ✓）。数据库不存在/没建表 ⇒ 返回 available=False。"""
    now = int(now if now is not None else time.time())
    if not os.path.exists(path):
        return {"available": False, "path": str(path), "reason": "数据库还不存在",
                "now": now}
    conn = connect(path)
    try:
        if not _has_schema(conn):
            return {"available": False, "path": str(path),
                    "reason": "数据库里还没有账本表", "now": now}
        ids = ([str(self_id)] if self_id else
               [str(r["self_id"]) for r in conn.execute(
                   "SELECT DISTINCT self_id FROM account ORDER BY self_id").fetchall()])
        bots = []
        for bot in ids:
            start = plugin.day_start(now)
            bots.append({
                "self_id": bot,
                "day": datetime.fromtimestamp(start, CN_TZ).strftime("%Y-%m-%d"),
                "today": plugin.db_audit_totals(bot, start, now, conn=conn),
                "today_detail": plugin.db_audit_detail(conn, bot, start, now, TOP_LIMIT),
                "wallets": wallets_of(conn, bot),
                "ious": ious_of(conn, bot),
                "brief": brief_of(conn, bot),
            })
        return {"available": True, "path": str(path), "now": now, "bots": bots}
    finally:
        conn.close()


# ─────────────────────────────── 页面 ───────────────────────────────

PAGE = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>愿力中心 · 只读</title>
<style>
 body{{font:14px/1.6 -apple-system,"Segoe UI",system-ui,sans-serif;margin:24px;color:#222}}
 h1{{font-size:20px;margin:0 0 4px}} h2{{font-size:15px;margin:22px 0 6px;color:#444}}
 .meta{{color:#777;font-size:12px;margin-bottom:14px}}
 table{{border-collapse:collapse;width:100%;max-width:1100px}}
 th,td{{border-bottom:1px solid #eee;padding:5px 8px;text-align:left;vertical-align:top}}
 th{{background:#fafafa;font-weight:600;color:#555}}
 td.num{{text-align:right;font-variant-numeric:tabular-nums}}
 .k{{display:inline-block;margin:0 14px 6px 0}} .k b{{font-size:16px}}
 .kind{{color:#888;font-size:12px}} .warn{{color:#b26a00}} .ro{{color:#0a7;font-weight:600}}
</style></head><body>
<h1>愿力中心 · 只读面板</h1>
<div class="meta">数据文件：{path} ｜ 看的时间：{shown} ｜ 连接：<span class="ro">只读</span>（面板写不了库 ✓）</div>
{body}
</body></html>
"""


def _kv(label, value, warn=False):
    return '<span class="k">%s <b class="%s">%s</b></span>' % (
        html.escape(label), "warn" if warn else "", html.escape(str(value)))


def _table(headers, rows):
    out = ["<table><tr>" + "".join("<th>%s</th>" % html.escape(h) for h in headers) + "</tr>"]
    for row in rows:
        cells = []
        for index, cell in enumerate(row):
            cls = ' class="num"' if index and isinstance(cell, int) else ""
            cells.append("<td%s>%s</td>" % (cls, html.escape(str(cell))))
        out.append("<tr>" + "".join(cells) + "</tr>")
    out.append("</table>")
    return "".join(out)


def render(data):
    if not data.get("available"):
        body = ('<p class="warn">还读不到账本：%s</p><p class="meta">%s</p>'
                % (html.escape(str(data.get("reason"))), html.escape(str(data.get("path")))))
        return PAGE.format(path=html.escape(str(data.get("path"))),
                           shown="-", body=body)
    blocks = []
    for bot in data["bots"]:
        today = bot["today"]
        detail = bot["today_detail"]
        head = "<h2>bot %s ｜ %s 的账</h2>" % (html.escape(bot["self_id"]),
                                              html.escape(bot["day"]))
        stats = "".join([
            _kv("收愿力", plugin.money_text(today["issued"])),
            _kv("送礼", plugin.money_text(today["gifted"])),
            _kv("支出（点单）", plugin.money_text(today["granted"])),
            _kv("撤回", plugin.money_text(today["refunded"])),
            _kv("账上共", plugin.money_text(today["total"])),
            _kv("钱包", "%d 个" % today["wallets"]),
            _kv("未结欠条", "%d 张（挂着 %s）" % (detail["open_ious"],
                                              plugin.money_text(detail["iou_amount"])),
                warn=detail["open_ious"] > 0),
            _kv("今日信誉分", "+%d / -%d" % (detail["credit_up"], detail["credit_down"])),
        ])
        blocks.append(head + "<div>" + stats + "</div>")

        blocks.append("<h2>钱包一览</h2>" + _table(
            ["谁", "愿力", "信誉分", "未结欠条", "最近动过"],
            [[w["name"], w["balance"], w["credit"], w["open_ious"], w["updated_at"]]
             for w in bot["wallets"]] or [["（还没有人动过账）", "", "", "", ""]]))
        blocks.append("<h2>还没兑现的要求</h2>" + _table(
            ["#", "谁欠的", "要他做什么", "付了多少", "什么时候点的", "期限"],
            [[i["id"], i["debtor"], i["item"], i["amount"], i["time"], i["due"] or "—"]
             for i in bot["ious"]] or [["—", "（没有未结的欠条）", "", "", "", ""]]))
        blocks.append("<h2>今天动得最多的人</h2>" + _table(
            ["账号", "收", "出"],
            [[t["user_id"], t["got"], t["gave"]] for t in detail["top"]]
            or [["—", "（今天还没人动过）", ""]]))
        blocks.append("<h2>最近流水</h2>" + _table(
            ["时间", "类别", "从", "到", "数额", "她/他写的原话"],
            [[b["time"], b["kind"], b["from"], b["to"], b["amount"], b["reason"]]
             for b in bot["brief"]] or [["—", "", "", "", "", "（还没有流水）"]]))
    return PAGE.format(path=html.escape(str(data["path"])),
                       shown=datetime.fromtimestamp(int(data["now"]), CN_TZ)
                       .strftime("%Y-%m-%d %H:%M:%S"),
                       body="".join(blocks))


class Handler(BaseHTTPRequestHandler):
    db_path = ""
    quiet = True

    def do_GET(self):                                   # noqa: N802 —— http.server 的约定
        if self.path.split("?")[0] not in ("/", "/index.html"):
            self.send_error(404, "only / is served")
            return
        try:
            payload = render(snapshot(self.db_path)).encode("utf-8")
            status = 200
        except Exception as exc:                        # noqa: BLE001 —— 面板出错也别把栈甩给浏览器
            payload = ("读账本失败：%s" % html.escape(str(exc)[:200])).encode("utf-8")
            status = 500
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        if not self.quiet:
            BaseHTTPRequestHandler.log_message(self, *args)


def serve(db_path, host="127.0.0.1", port=8765, quiet=True):
    Handler.db_path = db_path
    Handler.quiet = quiet
    httpd = ThreadingHTTPServer((host, port), Handler)
    return httpd


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="愿力中心 · 只读面板")
    parser.add_argument("--db", default="", help="数据文件（默认取插件的数据目录）")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--json", action="store_true", help="不开服务，直接吐 JSON")
    parser.add_argument("--self-id", default="", help="只看某个 bot（默认全部）")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    db_path = args.db or plugin.resolve_db_path()
    if args.json:
        print(json.dumps(snapshot(db_path, self_id=args.self_id or None),
                         ensure_ascii=False, indent=2))
        return 0
    if not os.path.exists(db_path):
        print("⚠️ 数据文件还不存在：%s" % db_path)
        print("  （面板照常起得来，页面上会写「还读不到账本」✓）")
    httpd = serve(db_path, args.host, args.port, quiet=not args.verbose)
    print("愿力中心 · 只读面板：http://%s:%d/  （Ctrl+C 停）" % (args.host, args.port))
    print("数据文件：%s" % db_path)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n停了 ✓")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    if __package__ in (None, ""):
        # 直接 `python panel.py` 跑：模块名是 `__main__`、不在包里 ⇒ 上面的
        # `import main as plugin` 会变成**再导入一次**，于是有两份 main ✓
        # 让两边共用同一份：把 `__main__` 指成 `main` 模块，再走函数里的 `import main` ✓
        sys.modules["main"] = sys.modules["__main__"]
    sys.exit(main())
