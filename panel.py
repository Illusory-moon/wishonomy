# -*- coding: utf-8 -*-
"""愿力中心 · 只读面板（P6）。

给**运营**看的页面：发行量 / 送礼 / 支出 / 欠条 / 信誉分 / 钱包排行 / 最近流水。

两个页面（2026-10-14 起 ✓）：
  * `/`          —— 主页面：上面那些都在这一页；**最近流水不含被注视**（被注视一天两三百笔，
                    混进来会把表占满 ✗）· **默认只看本日**，可**按日 / 按类别**检索 ✓
  * `/attention` —— **被注视次数**：总计 · 按天 · 今天按小时（它单独一页 ✓）

三条纪律（代码保证 ✓）：
  1. **只读**：连接一律用 SQLite 的 `mode=ro` URI 打开 —— 面板**写不了**库 ✗
     （连 WAL 都不去碰：只读连接不会建 `-wal`/`-shm` ✓）
  2. **不占群里任何一句话**：它是本地页面，不是 bot 的功能 ✓
  3. **转义**：流水里的 `reason` 是用户/她写的原话 ⇒ 一律 `html.escape`（防 XSS ✓）
  4. **JS 只有一段**（2026-10-14 主人批的：他要"换个日期/看点更早的**别把页面弹回顶部**"✓）：
     内联、无外链、无 `onclick=`、无 CDN/字体 ⇒ 仍然**单文件、可离线打开** ✓
     **关掉 JS 也活**（工具条是原生 `<form method="get">`、"更早的"是真链接 ✓）

跑法：
    python panel.py                      # 起在 127.0.0.1:8765
    python panel.py --port 9000          # 换端口
    python panel.py --db <ledger.db>     # 指定数据库
    python panel.py --json               # 不开服务，直接吐 JSON（给脚本/部署后核对用）

⚠️ **按需取，不是"一天全读"**（主人给的理由：**开源插件不能拿我们自己的用量当设计基准** ✗）：
   服务端只读"所选那一天的 N 行"（`?day=&kind=&n=`），大用量实例一天几万笔也扛得住 ✓
"""
import argparse
import html
import json
import os
import sqlite3
import sys
import threading
import time
import types
import urllib.parse
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
BRIEF_LIMIT = 400       # 一次从库里读多少行（**本地只读库、读 400 行很便宜** ✓）
BRIEF_STAGE = 60        # 页面上**每次「再看更早的」放开多少行**（纯 CSS 展开，不刷新 ✓）
BRIEF_MAX = 5000        # 上限（再多就该用 --json / 直接查库了 ✓）
TOP_LIMIT = 12
WALLET_LIMIT = 30
ATTENTION_KIND = "attention"   # 「被注视」的流水类别 —— **不进最近流水**，单独一页 ✓
ATTENTION_DAYS = 60            # 「被注视次数」那页按天最多列多少天
CN_HOURS = int(CN_TZ.utcoffset(None).total_seconds() // 3600)   # SQLite 分天用（与 CN_TZ 同源 ✓）


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


def rows_of(conn, self_id, rows):
    """把 ledger 的行整成面板要的形状（`brief` 与 `more` 共用 ✓）。"""
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


def _date_text(ts):
    """时间戳 ⇒ 北京时间的 `YYYY-MM-DD` ✓"""
    return datetime.fromtimestamp(int(ts), CN_TZ).strftime("%Y-%m-%d")


def _day_bounds(day, now):
    """把 `day` 参数解释成 `[start, end)`（**北京时间** ✓）。

    * **空 ⇒ 今天**（主人 2026-10-14 定：流水**默认只看本日** ✓）
    * `YYYY-MM-DD` ⇒ 那一天
    * 解析不出来 ⇒ 退回今天（**不报错、也不吐 500** ✓）
    """
    text = str(day or "").strip()
    if text:
        try:
            stamp = datetime.strptime(text, "%Y-%m-%d")
            start = int(stamp.replace(tzinfo=CN_TZ).timestamp())
            return start, start + 86400, _date_text(start)
        except ValueError:
            pass
    start = int(plugin.day_start(now))
    return start, start + 86400, _date_text(start)


def _flow_where(bot, day_start, day_end, kind):
    """流水的 WHERE 片段 + 参数（**只读** ✓）。

    ⚠️ 两条口径写死在这里：
      * **被注视永远排除**（`kind<>'attention'`）—— 它一天两三百笔，会把窗口占满 ✗
      * **按日**（`ts>=? AND ts<?`）—— 主人 2026-10-14 定的默认行为 ✓
        按需取的好处：**别人那边一天几万笔也不会被我们的小账本"惯坏"** ✓
    """
    conds = ["self_id=?", "kind<>?"]
    args = [str(bot), ATTENTION_KIND]
    if day_end is not None:
        conds.append("ts>=?")
        conds.append("ts<?")
        args += [int(day_start), int(day_end)]
    if kind:
        conds.append("kind=?")
        args.append(str(kind))
    return " AND ".join(conds), args


def snapshot(path, now=None, self_id=None, limit=BRIEF_LIMIT, more=False, day="", kind=""):
    """面板要的全部数据（纯读 ✓）。数据库不存在/没建表 ⇒ 返回 available=False。

    ⚠️ 2026-10-14：这里曾有**两份定义**（先定义的那份被后定义的遮住 ✗ ⇒ 改哪一份都不生效）
       —— 已清掉，现在**只有这一份** ✓
    ⚠️ **被注视（`kind='attention'`）刻意不在这份数据里**：它一天两三百笔，会把 400 行的
       窗口占满（主人原话「大多都是被注视」✗）⇒ 它单独一页（见 `attention_snapshot()`）✓
    ⚠️ **流水默认只看本日**（`day` 空 = 今天 ✓）；`kind` 是类别筛选（空 = 全部）✓
    """
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
        start, end, day_label = _day_bounds(day, now)
        bots = []
        for bot in ids:
            where, args = _flow_where(bot, start, end, kind)
            rows = conn.execute(
                "SELECT ts, group_id, from_id, to_id, amount, kind, reason FROM ledger "
                "WHERE %s ORDER BY id DESC LIMIT ?" % where,
                tuple(args + [int(limit)])).fetchall()
            entry_list = rows_of(conn, bot, rows)
            total = int(conn.execute(
                "SELECT COUNT(*) c FROM ledger WHERE %s" % where, tuple(args)).fetchone()["c"])
            kinds = [str(r["kind"]) for r in conn.execute(
                "SELECT DISTINCT kind FROM ledger WHERE self_id=? AND kind<>? ORDER BY kind",
                (bot, ATTENTION_KIND)).fetchall()]
            today_start = int(plugin.day_start(now))
            bots.append({
                "self_id": bot,
                "day": datetime.fromtimestamp(today_start, CN_TZ).strftime("%Y-%m-%d"),
                "today": plugin.db_audit_totals(bot, today_start, now, conn=conn),
                "today_detail": plugin.db_audit_detail(conn, bot, today_start, now, TOP_LIMIT),
                "wallets": wallets_of(conn, bot),
                "ious": ious_of(conn, bot),
                "brief": entry_list,
                "brief_count": len(entry_list),
                "ledger_total": total,
                "limit": int(limit),
                "more": bool(more),
                "kinds": kinds,
                "flow_day": day_label,
                "kind": str(kind or ""),
                "prev_day": _date_text(start - 86400),
                "next_day": ("" if start >= today_start else _date_text(start + 86400)),
                "is_today": start >= today_start,
            })
        return {"available": True, "path": str(path), "now": now, "bots": bots}
    finally:
        conn.close()


def _stamps(row):
    """把一条 `COUNT/SUM/MIN/MAX` 的结果整成「次数 / 折合愿力 / 首末时间」（纯读 ✓）"""
    def at(value):
        return (datetime.fromtimestamp(int(value), CN_TZ).strftime("%m-%d %H:%M")
                if value else "")

    return {"count": int(row["c"] or 0), "amount": int(row["s"] or 0),
            "first": at(row["a"]), "last": at(row["b"])}


def attention_snapshot(path, now=None, self_id=None, days=ATTENTION_DAYS):
    """「被注视次数」那一页要的数据（**纯读** ✓ 主人 2026-10-14 裁定的独立界面）。

    数字口径（准则②：**每个数字都要说清"这是谁的"** ✓）：
      · 被注视 = `system → 她自己`（每条**唤醒** +1）⇒ 这些次数进的是**她自己**那一栏的愿力，
        **不是别人的**（页面上每一处都写明 ✓）
      · 分天 / 分小时一律按**北京时间**（`CN_HOURS` 与 `plugin.day_start()` 同源 ✓）
    """
    now = int(now if now is not None else time.time())
    if not os.path.exists(path):
        return {"available": False, "path": str(path), "now": now,
                "reason": "数据库还不存在"}
    conn = connect(path)
    try:
        if not _has_schema(conn):
            return {"available": False, "path": str(path), "now": now,
                    "reason": "数据库里还没有账本表"}
        shift = "%+d hours" % CN_HOURS
        ids = ([str(self_id)] if self_id else
               [str(r["self_id"]) for r in conn.execute(
                   "SELECT DISTINCT self_id FROM account ORDER BY self_id").fetchall()])
        bots = []
        for bot in ids:
            start = plugin.day_start(now)
            total = conn.execute(
                "SELECT COUNT(*) c, SUM(amount) s, MIN(ts) a, MAX(ts) b FROM ledger "
                "WHERE self_id=? AND kind=?", (bot, ATTENTION_KIND)).fetchone()
            today = conn.execute(
                "SELECT COUNT(*) c, SUM(amount) s, MIN(ts) a, MAX(ts) b FROM ledger "
                "WHERE self_id=? AND kind=? AND ts>=? AND ts<=?",
                (bot, ATTENTION_KIND, start, now)).fetchone()
            by_day = conn.execute(
                "SELECT date(ts,'unixepoch',?) d, COUNT(*) c, SUM(amount) s, "
                "MIN(ts) a, MAX(ts) b FROM ledger WHERE self_id=? AND kind=? "
                "GROUP BY d ORDER BY d DESC LIMIT ?",
                (shift, bot, ATTENTION_KIND, int(days))).fetchall()
            by_hour = conn.execute(
                "SELECT CAST(strftime('%H', ts, 'unixepoch', ?) AS INTEGER) h, COUNT(*) c "
                "FROM ledger WHERE self_id=? AND kind=? AND ts>=? AND ts<=? "
                "GROUP BY h ORDER BY h", (shift, bot, ATTENTION_KIND, start, now)).fetchall()
            bots.append({
                "self_id": bot,
                "day": datetime.fromtimestamp(start, CN_TZ).strftime("%Y-%m-%d"),
                "total": _stamps(total),
                "today": _stamps(today),
                "days": [dict(_stamps(r), day=str(r["d"])) for r in by_day],
                "hours": [{"hour": int(r["h"] or 0), "count": int(r["c"])}
                          for r in by_hour],
            })
        return {"available": True, "path": str(path), "now": now, "bots": bots}
    finally:
        conn.close()


# ─────────────────────────────── 页面 ───────────────────────────────

PAGE = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="light dark">
<title>{title}</title>
<style>
:root{{
 --bg:#eef1f6; --card:#fff; --ink:#151b2b; --ink2:#4a5568; --mut:#7b8698;
 --line:#e3e7ef; --line2:#eef1f6; --head:#0e1524; --head2:#1d2942;
 --good:#118a55; --good-bg:#e6f6ee; --warn:#a35a06; --warn-bg:#fdf1de;
 --bad:#c33a3a; --bad-bg:#fdecec; --info:#3159c9; --info-bg:#e8edfd;
 --violet:#6c46c6; --violet-bg:#f0ebfd; --zebra:#f8fafc; --soft-bg:#f4f6fa;
 --radius:12px; --pad:16px;
}}
@media (prefers-color-scheme:dark){{
 :root{{
  --bg:#0b0f18; --card:#141a26; --ink:#e8ecf4; --ink2:#b3bccd; --mut:#8a94a8;
  --line:#232c3d; --line2:#1c2434; --head:#080c13; --head2:#141d2f;
  --good:#4ecb8d; --good-bg:#12301f; --warn:#e0a353; --warn-bg:#33260f;
  --bad:#f08585; --bad-bg:#3a1b1b; --info:#8aa8f5; --info-bg:#182342;
  --violet:#b79bf0; --violet-bg:#251c40; --zebra:#171e2b; --soft-bg:#1a2231;
 }}
}}
*{{box-sizing:border-box}}
body{{margin:0;background:var(--bg);color:var(--ink);
 font:14px/1.55 -apple-system,"Segoe UI",Roboto,"Helvetica Neue",
 "PingFang SC","Microsoft YaHei",system-ui,sans-serif;
 -webkit-font-smoothing:antialiased}}
a{{color:var(--info)}}

/* ── 深色标题区 ── */
.top{{background:linear-gradient(135deg,var(--head),var(--head2));
 color:#f2f5fb;padding:22px 20px 18px}}
.top .wrap{{max-width:1240px;margin:0 auto}}
.brand{{display:flex;align-items:center;gap:10px;flex-wrap:wrap}}
.brand h1{{font-size:19px;margin:0;letter-spacing:.01em;font-weight:650}}
.dot{{width:8px;height:8px;border-radius:50%;background:#54d19a;
 box-shadow:0 0 0 3px rgba(84,209,154,.18)}}
.chips{{display:flex;flex-wrap:wrap;gap:8px;margin-top:12px}}
.chip{{background:rgba(255,255,255,.08);border:1px solid rgba(255,255,255,.14);
 border-radius:999px;padding:4px 11px;font-size:12px;color:#cfd7e6;
 max-width:100%;overflow-wrap:anywhere}}
.chip b{{color:#fff;font-weight:600}}
.chip.ok{{background:rgba(84,209,154,.16);border-color:rgba(84,209,154,.4);color:#c8f2dc}}

/* ── 浅色内容区 ── */
main{{max-width:1240px;margin:0 auto;padding:20px var(--pad) 48px}}
.card{{background:var(--card);border:1px solid var(--line);border-radius:var(--radius);
 margin-top:18px;overflow:hidden;box-shadow:0 1px 2px rgba(16,24,40,.04)}}
.card>h2{{font-size:13px;margin:0;padding:11px 16px;color:var(--ink2);font-weight:650;
 letter-spacing:.06em;border-bottom:1px solid var(--line);background:var(--soft-bg);
 display:flex;gap:8px;align-items:baseline;flex-wrap:wrap}}
.card>h2 .tag{{font-size:11px;font-weight:500;color:var(--mut);letter-spacing:0}}
.card .pad{{padding:var(--pad)}}

.bot{{display:flex;gap:10px;align-items:center;flex-wrap:wrap;
 margin:26px 0 4px;font-size:13px;color:var(--ink2)}}
.bot .who{{background:var(--card);border:1px solid var(--line);border-radius:999px;
 padding:3px 11px;font-weight:600;color:var(--ink);font-variant-numeric:tabular-nums}}
.bot .day{{color:var(--mut)}}

/* ── 指标卡 ── */
.metrics{{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));
 gap:10px;padding:var(--pad)}}
.metric{{background:var(--soft-bg);border:1px solid var(--line2);border-radius:10px;
 padding:11px 13px;min-width:0}}
.metric .lab{{font-size:11.5px;color:var(--mut);letter-spacing:.03em;
 display:flex;align-items:center;gap:5px}}
.metric .val{{font-size:21px;font-weight:650;margin-top:5px;line-height:1.2;
 font-variant-numeric:tabular-nums;overflow-wrap:anywhere}}
.metric .sub{{font-size:11.5px;color:var(--mut);margin-top:3px;overflow-wrap:anywhere}}
.metric.good .val{{color:var(--good)}} .metric.warn .val{{color:var(--warn)}}
.metric.bad .val{{color:var(--bad)}}

/* ── 表格 ── */
.tw{{overflow-x:auto}}
table{{width:100%;border-collapse:separate;border-spacing:0;font-size:13px}}
thead th{{position:sticky;top:0;z-index:1;background:var(--soft-bg);color:var(--ink2);
 text-align:left;font-weight:600;font-size:11.5px;letter-spacing:.05em;
 padding:8px 12px;border-bottom:1px solid var(--line);white-space:nowrap}}
tbody td{{padding:7px 12px;border-bottom:1px solid var(--line2);vertical-align:top;
 overflow-wrap:anywhere;min-width:0}}
tbody tr:nth-child(even){{background:var(--zebra)}}
tbody tr:last-child td{{border-bottom:0}}
tbody tr:hover{{background:var(--soft-bg)}}
td.num,th.num{{text-align:right;white-space:nowrap;
 font-variant-numeric:tabular-nums;
 font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:12.5px}}
td.num{{font-size:13px}}
tr.muted td{{color:var(--mut)}}
.pos{{color:var(--good)}} .neg{{color:var(--warn)}}
.zero{{color:var(--mut)}}
.mut{{color:var(--mut)}}
.flowbar{{display:flex;gap:8px;align-items:center;flex-wrap:wrap}}
.flowbar form{{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin:0}}
.flowbar input,.flowbar select{{font:inherit;padding:6px 10px;border-radius:8px;
 border:1px solid var(--line);background:var(--card);color:var(--ink)}}
.flowbar button{{font:inherit;cursor:pointer}}
.bar{{display:inline-block;height:10px;min-width:3px;border-radius:3px;
 background:var(--info);vertical-align:middle}}
.barwrap{{display:flex;align-items:center;gap:8px;min-width:120px}}
.barwrap .n{{color:var(--mut);font-variant-numeric:tabular-nums;font-size:12px}}
.ids{{font-size:11.5px;color:var(--mut);font-variant-numeric:tabular-nums}}

/* ── 徽章 ── */
.pill{{display:inline-block;padding:2px 9px;border-radius:999px;font-size:11.5px;
 font-weight:600;line-height:1.6;white-space:nowrap;
 background:var(--soft-bg);color:var(--ink2);border:1px solid var(--line)}}
.p-mood{{background:var(--good-bg);color:var(--good)}}
.p-gift{{background:var(--info-bg);color:var(--info)}}
.p-grant{{background:var(--warn-bg);color:var(--warn)}}
.p-refund{{background:var(--bad-bg);color:var(--bad)}}
.p-credit{{background:var(--violet-bg);color:var(--violet)}}

/* ── 空状态 ── */
.empty{{padding:22px 16px;text-align:center;color:var(--mut);font-size:13px}}
.empty .big{{font-size:22px;display:block;margin-bottom:6px;opacity:.5}}

/* ── 提示条 ── */
.note{{border-left:3px solid var(--warn);background:var(--warn-bg);color:var(--ink);
 border-radius:8px;padding:12px 14px;font-size:13.5px;overflow-wrap:anywhere}}
.note .t{{font-weight:650;margin-bottom:3px}}
.note .r{{color:var(--ink2)}}
.note .p{{margin-top:8px;font-size:12px;color:var(--mut);font-variant-numeric:tabular-nums}}

footer{{max-width:1240px;margin:0 auto;padding:0 var(--pad) 40px;
 color:var(--mut);font-size:11.5px;line-height:1.7;overflow-wrap:anywhere}}

@media (max-width:720px){{
 :root{{--pad:12px}}
 .top{{padding:18px 14px 15px}}
 .brand h1{{font-size:17px}}
 .metrics{{grid-template-columns:repeat(auto-fit,minmax(128px,1fr));gap:8px}}
 .metric .val{{font-size:18px}}
 .card{{border-radius:10px}}
 tbody td,thead th{{padding:6px 10px}}
 table{{font-size:12.5px}}
}}
</style></head><body>
<div class="top"><div class="wrap">
 <div class="brand"><span class="dot"></span><h1>{title}</h1></div>
 <div class="chips">
  <span class="chip">数据文件：<b>{path}</b></span>
  <span class="chip">看的时间：<b>{shown}</b></span>
  <span class="chip ok">连接：只读（面板写不了库 ✓）</span>
  {refresh_chip}
  {nav}
 </div>
</div></div>
<main>
{body}
</main>
{script}
<footer>本页是<b>只读</b>视图：数据来自账本的只读快照，页面上不会写入任何东西。<br>
 页面零外部依赖（没有 CDN / 外链字体 / 外链脚本）；JS 只有本页内联的那一小段，
 只为了"换个日期/看点更早的别把页面弹回顶部" —— 关掉 JS 也一样能用 ✓</footer>
</body></html>
"""

KIND_LABEL = {"mood": "收愿力", "gift": "送礼", "grant": "点单", "refund": "撤回",
              "credit": "信誉"}
# 页面上**显示**的名字（主人 2026-10-14：徽章原来直接吐英文 kind，跟上面的指标卡不一致 ✗）
# ⚠️ 原始 kind 仍然留在 `title=` 里（悬停可见）⇒ 对着库查账时不会被这层显示名挡住 ✓
KIND_TEXT = {"mood": "收愿力", "gift": "送礼", "grant": "点单", "refund": "撤回",
             "credit": "信誉", "settle": "日结", "attention": "被注视", "initial": "初始"}


def _badge(kind):
    """流水类别小徽章（颜色按类别分 · **文字用中文名**，原始 kind 放 `title` ✓）。"""
    text = str(kind)
    cls = text if text in KIND_LABEL else "other"
    return ('<span class="pill p-%s" title="%s">%s</span>'
            % (cls, html.escape(text), html.escape(KIND_TEXT.get(text, text))))


def _metric(label, value, sub="", tone=""):
    """指标卡：大数字 + 小标签（`tone` = good / warn / bad ✓）。"""
    tail = '<div class="sub">%s</div>' % html.escape(str(sub)) if sub else ""
    return ('<div class="metric %s"><div class="lab">%s</div>'
            '<div class="val">%s</div>%s</div>'
            % (tone, html.escape(str(label)), html.escape(str(value)), tail))


def _table(headers, rows, align=(), rowcls=None):
    """表格：斑马纹 / 紧凑行高 / 表头吸顶 / 数字右对齐（等宽数字）。

    单元格可以是 `str`（原样转义后当文本）或
    `{"v": 取值, "cls": "pos|neg|zero|muted|ids", "raw": True}`（`raw` = 已经是 HTML ✓）；
    `align` 里点名过的列一律右对齐 + 等宽数字；
    `rowcls` 是与 `rows` 等长的行级 class 列表（余额 <= 0 的行整行弱化 ✓）
    """
    heads = []
    for index, head in enumerate(headers):
        cls = ' class="num"' if index in align else ""
        heads.append("<th%s>%s</th>" % (cls, html.escape(str(head))))
    body = []
    for position, row in enumerate(rows):
        cells = []
        for index, cell in enumerate(row):
            spec = {"v": cell} if isinstance(cell, (str, int, float)) else dict(cell)
            tokens = str(spec.get("cls", "")).split()
            if index in align:
                tokens = (["num"] + [tok for tok in tokens if tok != "num"])
            inner = str(spec.get("v", ""))
            if not spec.get("raw"):
                inner = html.escape(inner)                   # 默认一律转义 ✓
            cls = ' class="%s"' % " ".join(tokens) if tokens else ""
            cells.append("<td%s>%s</td>" % (cls, inner))
        cls = rowcls[position] if rowcls else ""
        attr = ' class="%s"' % cls if cls else ""
        body.append("<tr%s>%s</tr>" % (attr, "".join(cells)))
    return ('<div class="tw"><table><thead><tr>%s</tr></thead><tbody>%s</tbody></table></div>'
            % ("".join(heads), "".join(body)))


def _empty(text, mark="—"):
    return '<div class="empty"><span class="big">%s</span>%s</div>' % (
        html.escape(mark), html.escape(text))


def _card(title, inner, tag=""):
    tail = '<span class="tag">%s</span>' % html.escape(str(tag)) if tag else ""
    return '<section class="card"><h2>%s%s</h2>%s</section>' % (
        html.escape(str(title)), tail, inner)


def _refresh_chip(on):
    """紫色那颗「一键拉取」按钮（**纯链接 + GET** ⇒ 不需要任何 JS ✓）"""
    if not on:
        return '<span class="chip">一键拉取：关（启动时加 --refresh 打开）</span>'
    return ('<a class="chip btn" href="?refresh=1" '
            'title="去服务器把账本的只读副本拉一份新的过来（不会改动远端任何东西）">'
            '⟳ 一键拉取新副本</a>')


def brief_row(item):
    """一行流水的单元格（分组内每一条都用它 ✓）。"""
    amount = int(item["amount"])
    return [{"v": item["time"], "cls": "num"}, {"v": _badge(item["kind"]), "raw": True},
            item["from"], item["to"],
            {"v": "+%d" % amount if amount > 0 else "%+d" % amount,
             "cls": "num pos" if amount > 0 else "num neg"},
            item["reason"] or "—"]


def brief_table(bot, prefix="b"):
    """最近流水：**被注视不在这张表里**（它单独一页 ✓ 主人 2026-10-14 要的）。

    展开 = **label + checkbox**（纯 CSS、零 JS、零请求 ✓）：
      · 点的是 `<label>`，**不是 `#` 锚点** ⇒ 浏览器不做文档跳转 ⇒ **页面零位移** ✓
        （旧版是 `<a href="#more-N">` + `:target`，两个真病：① 点一下浏览器把这一行滚到视口
         顶部 = 主人报的"比较大的位移" ✗ ② `:target` 同一时刻只认一个元素 ⇒ 点第二颗时
         第一批整批缩回去 ✗ = 主人报的"第一个被折叠掉"）
      · **同一时刻只看得见一颗按钮**（主人本轮的裁定 ✓）：它永远在最前沿 —— 夹在"已展开的
        那批"末尾；点过之后它自己只留一条窄信息（按钮 `visibility:hidden`，**行高不塌** ⇒
        上方一个像素都不动 ✓），下一颗按钮跟着内容**自然往下顺延** ✓
      · 展开过的批次**只增不减**（勾上就一直勾着）✓
      · 表底那颗「库里还有更早的」要重新查库 ⇒ **会刷新一次**（全页只有这一条会跳 ✓）
    """
    entries = bot["brief"]
    day = str(bot.get("flow_day") or "")
    kind = str(bot.get("kind") or "")
    if not entries:
        if kind:
            return _empty("这一天没有「%s」这一类（被注视不在这张表里）"
                          % KIND_TEXT.get(kind, kind), "·")
        return _empty("这一天没有有内容的流水（被注视不在这张表里）· 上面换个日期看看", "·")
    head = ["时间", "类别", "从", "到", "数额", "她/他写的原话"]
    total = bot.get("ledger_total") or 0
    shown = bot.get("brief_count", 0)
    stages = max(1, (len(entries) + BRIEF_STAGE - 1) // BRIEF_STAGE)
    hidden_after = shown < total          # 这一天还有更早的 ⇒ 表底给"多读"的入口 ✓

    body = []
    for index, item in enumerate(entries):
        if index and index % BRIEF_STAGE == 0:
            # 第 stage 批**之前**放这一颗按钮：点了才放开第 stage+1 批 ✓
            body.append(_anchor_row(prefix, index // BRIEF_STAGE, index, total))
        classes = []
        if index >= BRIEF_STAGE:
            classes.append("stage")
            classes.append("s%d" % (index // BRIEF_STAGE + 1))
        attr = ' class="%s"' % " ".join(classes) if classes else ""
        body.append("<tr%s>%s</tr>" % (attr, "".join(_cell(cell) for cell in brief_row(item))))

    scope = "%s%s" % (html.escape(day), " · 只看「%s」" % html.escape(KIND_TEXT.get(kind, kind))
                      if kind else "")
    foot = ['<span class="mut">%s · <b>不含被注视</b>（它单独一页）· 这一类共 <b>%d</b> 笔，'
            '这里显示最新的 <b>%d</b> 笔</span>' % (scope, total, shown)]
    if hidden_after:
        nxt = min(int(bot.get("limit") or BRIEF_LIMIT) * 3, BRIEF_MAX)
        foot.append('<a class="btn ghost" href="?%s" data-more="%d">这一天还有更早的 '
                    '%d 笔，就地多读</a>'
                    % (_flow_query(day, kind, nxt), nxt, max(0, total - shown)))
    return ('<div class="tw"><table><thead><tr>%s</tr></thead><tbody>%s</tbody></table></div>'
            '<div class="foot">%s</div>'
            % ("".join("<th%s>%s</th>" % (' class="num"' if i in (0, 4) else "", h)
                       for i, h in enumerate(head)), "".join(body), "".join(foot)))


def _flow_query(day, kind, n=None):
    """流水链接 / 片段请求**共用的查询串**（口径只有一个 ⇒ 两边不会跑偏 ✓）"""
    parts = ["day=%s" % urllib.parse.quote(str(day or ""))]
    if kind:
        parts.append("kind=%s" % urllib.parse.quote(str(kind)))
    if n:
        parts.append("n=%d" % int(n))
    return "&".join(parts)


def flow_toolbar(data):
    """「看哪一天的流水」工具条（全页**一个**，管所有 bot ✓）。

    ⚠️ 设计要点（主人 2026-10-14 追加的需求 + 他给的理由）：
      · **默认只看本日**、要能**按日检索**（"以后流水多了至少要能按日查"✓）
      · 主人原话：**"开源插件不能拿我们自己的用量当设计基准"** ⇒ 服务端**按需取**
        （只读这一天的 N 行），不是"一天全读" ✗ ⇒ 别人一天几万笔也扛得住 ✓
      · 控件全部用**原生**的（`<input type="date">` / `<select>` / `<form method=get>`）
        ⇒ **没 JS 也能用**（提交即整页刷新 ✓）；有 JS 时被接管成就地换 ✓
    """
    bots = data.get("bots") or []
    if not bots:
        return ""
    first = bots[0]
    day = str(first.get("flow_day") or "")
    kind = str(first.get("kind") or "")
    kinds = sorted({k for b in bots for k in (b.get("kinds") or [])})
    options = ['<option value="">全部类别</option>']
    for one in kinds:
        options.append('<option value="%s"%s>%s</option>'
                       % (html.escape(one), " selected" if one == kind else "",
                          html.escape(KIND_TEXT.get(one, one))))
    prev_link = ('<a class="btn ghost" href="?%s" title="看前一天">‹ 前一天</a>'
                 % _flow_query(first["prev_day"], kind))
    next_link = ('<a class="btn ghost" href="?%s" title="看后一天">后一天 ›</a>'
                 % _flow_query(first["next_day"], kind)
                 if first.get("next_day") else '<span class="mut">后一天 ›</span>')
    today_link = ('<a class="btn ghost" href="?%s">今天</a>'
                  % _flow_query(first.get("day") or "", kind))
    return ('<div class="pad flowbar"><form method="get" id="flowform">'
            '<input type="date" name="day" value="%s" aria-label="看哪一天的流水">'
            '<select name="kind" aria-label="看哪一类流水">%s</select>'
            '<button class="btn" type="submit">看</button></form>%s %s %s'
            '<span class="mut">默认只看<b>本日</b>（北京时间）· 「被注视」在另一页</span>'
            '</div>' % (html.escape(day), "".join(options), prev_link, next_link, today_link))


def _anchor_row(prefix, stage, seen, total):
    """一颗「再看更早的」按钮 = **label + 藏起来的 checkbox**（点了不跳页 ✓）。

    ⚠️ 两个踩过的坑都写进规则里：
      · 第一颗（`a1`）**永远可见** —— 上一版把所有按钮都藏了 ⇒ 页面上 0 颗按钮 ✗
      · `for` 指的是**这一批自己的**开关；点过之后 CSS 只把按钮 `visibility:hidden`
        （**不能 `display:none`** —— 那会塌掉一行，上方内容就会向上跳 ✗）
    """
    box = "%s-tg-%d" % (prefix, stage)
    return ('<tr class="anchor a%d"><td colspan="6">'
            '<input type="checkbox" class="tg" id="%s">'
            '<label class="btn" for="%s" title="就地展开，页面不会跳 ✓">'
            '⬇ 再看更早的 %d 行</label>'
            '<span class="mut">已看到第 %d 行 · 这一类共 %d 笔</span></td></tr>'
            % (stage, box, box, BRIEF_STAGE, seen, total))


def stage_css(prefix="b", stages=6):
    """展开用的**纯 CSS 规则**（零 JS ⇒ 零请求、零跳顶部 ✓）。

    机制（主人本轮裁定「同一时刻只看得见一颗按钮」✓）：
      · `input.tg` 是真开关：视觉上藏起来，但**还能 Tab 到** ⇒ 键盘也开得动 ✓
      · 第 k+1 批的行 + 第 k+1 颗按钮，要 `#…-tg-k` 勾上才出现 ⇒ 只有点过前一颗才冒出来 ✓
      · 点过的那颗：**只把按钮 `visibility:hidden`**（留行高 ⇒ 上方零位移 ✓），
        那一行从此变成一条窄信息（"已看到第 N 行"）
      · 老浏览器没有 `:has()` ⇒ `@supports not …` 里**一次全展开**（不会"点了没反应" ✓）
    """
    rules = [".stage{display:none}", "tr.anchor{display:none}",
             # ⚠️ 第一颗按钮必须永远可见 —— 上一版把它也藏了 ⇒ 页面上 0 颗按钮 ✗
             "tr.a1{display:table-row}",
             "input.tg{position:absolute;width:1px;height:1px;margin:0;border:0;"
             "padding:0;opacity:0;pointer-events:none}",
             "tr.anchor:has(input.tg:checked) label.btn{visibility:hidden}",
             "tr.anchor:has(input.tg:focus-visible) label.btn{outline:2px solid var(--info);"
             "outline-offset:2px}"]
    for stage in range(1, stages + 1):
        rules.append("table:has(#%s-tg-%d:checked) tr.s%d{display:table-row}"
                     % (prefix, stage, stage + 1))
        rules.append("table:has(#%s-tg-%d:checked) tr.a%d{display:table-row}"
                     % (prefix, stage, stage + 1))
    rules.append("@supports not selector(:has(*)){.stage{display:table-row}"
                 "tr.anchor{display:none}}")
    rules.append('.anchor td,.foot{text-align:center;padding:10px 8px;background:var(--soft-bg);'
                 'border-top:1px solid var(--line)}')
    rules.append('.btn{display:inline-block;padding:7px 14px;margin:0 6px;border-radius:999px;'
                 'background:var(--info);color:#fff;font-weight:600;text-decoration:none;'
                 'border:1px solid transparent;cursor:pointer}')
    rules.append('.btn:hover{filter:brightness(1.08)}')
    rules.append('.btn.ghost{background:transparent;color:var(--info);border-color:var(--info)}')
    return "<style>" + "".join(rules) + "</style>"


def _cell(cell):
    """`_table` 那份单元格逻辑（原样复用，别写两套 ✓）"""
    value = cell["v"] if isinstance(cell, dict) else cell
    cls = cell.get("cls", "") if isinstance(cell, dict) else ""
    raw = cell.get("raw") if isinstance(cell, dict) else False
    text = value if raw else html.escape(str(value))
    return '<td%s>%s</td>' % (' class="%s"' % cls if cls else "", text)


MAIN_TITLE = "愿力中心 · 只读面板"
ATTENTION_TITLE = "愿力中心 · 被注视次数"
NAV_ATTENTION = ('<a class="chip btn" href="/attention" '
                 'title="她每被唤醒一次 +1；只列次数，不混进流水">👁 查看被注视次数</a>')
NAV_HOME = '<a class="chip btn" href="/" title="回主页面（流水 / 钱包 / 欠条那一页）">← 回主页面</a>'

# 面板里**唯一**的一段 JS（主人 2026-10-14 批的：他要"点了就地换、不回顶"✓）
# ⚠️ 纪律变了但**没收松**：从"零 <script>"改成"**只许这一段内联 JS**" ——
#    没有外链、没有 `onclick=` 内联属性、没有 CDN/字体/外链资源 ⇒ 仍然**单文件、可离线打开** ✓
# ⚠️ 没 JS 也活：工具条是原生 `<form method="get">`（提交即整页刷新 ✓）、
#    「还有更早的」是**真链接**（JS 只是把它拦下来改成就地取 ✓）
FLOW_JS = """<script>
(function () {
  var form = document.getElementById('flowform');
  if (!form) { return; }
  function boxes() { return document.querySelectorAll('div.flows'); }
  function opened() {
    var ids = [];
    boxes().forEach(function (box) {
      box.querySelectorAll('input.tg:checked').forEach(function (el) { ids.push(el.id); });
    });
    return ids;
  }
  function query(n) {
    var q = new URLSearchParams();
    if (form.day.value) { q.set('day', form.day.value); }
    if (form.kind.value) { q.set('kind', form.kind.value); }
    if (n) { q.set('n', n); }
    return q;
  }
  function load(n, keep) {
    var q = query(n);
    var keepIds = keep ? opened() : [];
    fetch('/?' + q.toString(), {headers: {'X-Panel-Partial': '1'}})
      .then(function (resp) { return resp.ok ? resp.text() : ''; })
      .then(function (html) {
        if (!html) { return; }
        var fresh = new DOMParser().parseFromString(html, 'text/html');
        fresh.querySelectorAll('div.flows').forEach(function (one) {
          var old = document.getElementById(one.id);
          if (old) { old.innerHTML = one.innerHTML; }
        });
        keepIds.forEach(function (id) {
          var box = document.getElementById(id);
          if (box) { box.checked = true; }
        });
        history.replaceState(null, '', location.pathname + '?' + q.toString());
      });
  }
  form.addEventListener('submit', function (ev) { ev.preventDefault(); load('', false); });
  form.addEventListener('change', function () { load('', false); });
  document.addEventListener('click', function (ev) {
    var link = ev.target && ev.target.closest ? ev.target.closest('a[data-more]') : null;
    if (!link) { return; }
    ev.preventDefault();
    load(link.getAttribute('data-more'), true);
  });
})();
</script>"""


def render(data, refresh_on=False, flash=""):
    """把 `snapshot()` 的 dict 拼成一整页 HTML（**纯呈现**：一个数据查询都不做 ✓）。"""
    chip = _refresh_chip(refresh_on)
    if not data.get("available"):
        note = ('<div class="note"><div class="t">还读不到账本</div>'
                '<div class="r">%s</div><div class="p">%s</div></div>'
                % (html.escape(str(data.get("reason") or "")),
                   html.escape(str(data.get("path") or ""))))
        return PAGE.format(title=MAIN_TITLE, nav=NAV_ATTENTION, script=FLOW_JS, shown="-",
                           path=html.escape(str(data.get("path") or "")),
                           refresh_chip=chip,
                           body=flash + _card("读不到账本", '<div class="pad">%s</div>' % note))

    blocks = []
    toolbar = flow_toolbar(data)
    if toolbar:
        blocks.append(_card("看哪一天的流水", toolbar, tag="默认只看本日 · 按日检索"))
    for bot in data.get("bots") or []:
        today = bot["today"]
        detail = bot["today_detail"]
        blocks.append('<div class="bot"><span class="who">bot %s</span>'
                      '<span class="day">%s 这一天的账</span></div>'
                      % (html.escape(str(bot["self_id"])), html.escape(str(bot["day"]))))

        # ① 当日概览 —— 指标卡
        metrics = "".join([
            _metric("收愿力（净）", plugin.money_text(today["issued"]), tone="good"),
            _metric("分愿力（扣人）", plugin.money_text(detail.get("mood_down", 0)),
                    sub="%d 笔" % detail.get("mood_down_n", 0),
                    tone="warn" if detail.get("mood_down") else ""),
            _metric("送出（送礼）", plugin.money_text(today["gifted"])),
            _metric("支出（点单）", plugin.money_text(today["granted"]), tone="warn"),
            _metric("撤回", plugin.money_text(today["refunded"])),
            _metric("账上共", plugin.money_text(today["total"]),
                    sub="%d 个钱包" % today["wallets"]),
            _metric("未结欠条", "%d 张" % detail["open_ious"],
                    sub="挂着 %s" % plugin.money_text(detail["iou_amount"]),
                    tone="warn" if detail["open_ious"] else ""),
            _metric("今日信誉分",
                    "+%d / −%d" % (detail["credit_up"], detail["credit_down"]),
                    sub="涨 / 跌"),
            _metric("今日活跃", "%d 人" % len(detail["top"]), sub="有过往来的人"),
        ])
        blocks.append(_card("当日概览", '<div class="metrics">%s</div>' % metrics,
                            tag=str(bot["day"])))

        # ② 钱包一览
        wallets = bot["wallets"]
        if wallets:
            rows, weak_rows = [], []
            for wal in wallets:
                toned = "pos" if wal["credit"] > 0 else ("neg" if wal["credit"] < 0 else "zero")
                rows.append([wal["name"],
                             {"v": wal["balance"]},
                             {"v": "%+d" % wal["credit"], "cls": toned},
                             {"v": wal["open_ious"]},
                             {"v": wal["updated_at"]},
                             {"v": wal["user_id"], "cls": "ids"}])
                weak_rows.append("muted" if int(wal["balance"]) <= 0 else "")
            table = _table(["谁", "愿力", "信誉分", "未结欠条", "最近动过", "账号"], rows,
                           align=(1, 2, 3, 4, 5), rowcls=weak_rows)   # 余额 <= 0 ⇒ 整行弱化 ✓
        else:
            table = _empty("还没有人动过账", "·")
        blocks.append(_card("钱包一览", table, tag="按愿力多少排"))

        # ③ 未结欠条
        ious = bot["ious"]
        if ious:
            rows = [[{"v": i["id"], "cls": "num"}, i["debtor"], i["item"],
                     {"v": i["amount"], "cls": "num"},
                     {"v": i["time"], "cls": "num"},
                     {"v": i["due"] or "—", "cls": "num"}] for i in ious]
            table = _table(["#", "谁欠的", "要他做什么", "愿力", "什么时候点的", "期限"],
                           rows, align=(0, 3, 4, 5))
        else:
            table = _empty("没有未结的欠条", "·")
        blocks.append(_card("未结欠条", table, tag="还没兑现的要求"))

        # ④ 今日活跃
        top = detail["top"]
        if top:
            rows = [[t["user_id"],
                     {"v": "+%d" % t["got"], "cls": "num pos"} if t["got"] else
                     {"v": "0", "cls": "num zero"},
                     {"v": "−%d" % t["gave"], "cls": "num neg"} if t["gave"] else
                     {"v": "0", "cls": "num zero"}] for t in top]
            table = _table(["账号", "收（进）", "出"], rows, align=(1, 2))
        else:
            table = _empty("今天还没人动过", "·")
        blocks.append(_card("今日活跃", table, tag="进 / 出合计"))

        # ⑤ 最近流水（**被注视不在这张表里** —— 它单独一页；展开是 label+checkbox，点了不跳页 ✓）
        #    `div.flows` 是**唯一会被那段内联 JS 换掉**的块（别的卡片一个字节不动 ⇒ 零重排 ✓）
        prefix = "b%s" % bot["self_id"]
        stages = max(1, (len(bot["brief"]) + BRIEF_STAGE - 1) // BRIEF_STAGE)
        link = ('<div class="pad"><a class="btn" href="/attention">👁 查看被注视次数</a>'
                '<span class="mut">被注视（每被唤醒一次 +1）不在下面这张表里 —— 它单独一页 ✓</span>'
                '</div>')
        body = stage_css(prefix, min(stages, 12)) + brief_table(bot, prefix)
        blocks.append(_card("最近流水",
                            link + '<div class="flows" id="flows-%s">%s</div>'
                            % (html.escape(str(bot["self_id"])), body),
                            tag="%s · 最新在前 · 不含被注视（另有一页）"
                                % str(bot.get("flow_day") or "")))

    if not blocks:
        blocks.append(_card("账本", '<div class="pad">%s</div>'
                            % _empty("账本里还没有任何 bot 的账", "·")))
    return PAGE.format(title=MAIN_TITLE, nav=NAV_ATTENTION, script=FLOW_JS,
                       path=html.escape(str(data.get("path") or "")),
                       shown=datetime.fromtimestamp(int(data["now"]), CN_TZ)
                       .strftime("%Y-%m-%d %H:%M:%S"),
                       refresh_chip=chip,
                       body=flash + "".join(blocks))


def render_flow_fragment(data):
    """**片段模式**：只吐流水那几块（`div.flows`）—— 给面板自己那段内联 JS 用 ✓。

    ⚠️ 为什么要有它（主人 2026-10-14 定的方向）：
       他要的是"点了**就地换**、不回顶"✗。整页重载会（a）把全部卡片重建一遍
       （b）把视口弹回顶部 ✗ ⇒ **只换流水那几块** ⇒ 页面别处一个字节不动 ⇒ 零重排 ✓
    ⚠️ 每块都带上自己那份 `stage_css`：换了日期之后批次数量会变 ✗
       （不带上它，新出现的批次就点不开）✓
    """
    if not data.get("available"):
        return ""
    blocks = []
    for bot in data.get("bots") or []:
        prefix = "b%s" % bot["self_id"]
        stages = max(1, (len(bot["brief"]) + BRIEF_STAGE - 1) // BRIEF_STAGE)
        blocks.append('<div class="flows" id="flows-%s">%s%s</div>'
                      % (html.escape(str(bot["self_id"])),
                         stage_css(prefix, min(stages, 12)), brief_table(bot, prefix)))
    return "".join(blocks)


def attention_page(data, refresh_on=False, flash=""):
    """「被注视次数」那一页（**纯呈现** ✓ 主人 2026-10-14 要的独立界面）。

    看三样：**总计** · **按天** · **今天按小时**。每一处都写明「这是**她自己**那一栏的」✓
    """
    chip = _refresh_chip(refresh_on)
    if not data.get("available"):
        note = ('<div class="note"><div class="t">还读不到账本</div>'
                '<div class="r">%s</div><div class="p">%s</div></div>'
                % (html.escape(str(data.get("reason") or "")),
                   html.escape(str(data.get("path") or ""))))
        return PAGE.format(title=ATTENTION_TITLE, nav=NAV_HOME, script="", shown="-",
                           path=html.escape(str(data.get("path") or "")),
                           refresh_chip=chip,
                           body=flash + _card("读不到账本", '<div class="pad">%s</div>' % note))

    blocks = []
    for bot in data.get("bots") or []:
        total, today = bot["total"], bot["today"]
        blocks.append('<div class="bot"><span class="who">bot %s</span>'
                      '<span class="day">被注视 = 她每被<b>唤醒</b>一次 +1 —— '
                      '这些愿力进的是<b>她自己</b>那一栏，不是别人的 ✓</span></div>'
                      % html.escape(str(bot["self_id"])))
        blocks.append(_card("总计", '<div class="metrics">%s</div>' % "".join([
            _metric("被注视（总次数）", "%d 次" % total["count"], tone="good",
                    sub="最早 %s" % (total["first"] or "—")),
            _metric("折合愿力（她自己的）", plugin.money_text(total["amount"]),
                    sub="system → 她自己"),
            _metric("今天", "%d 次" % today["count"], sub=str(bot["day"])),
            _metric("今天折合（她自己的）", plugin.money_text(today["amount"]),
                    sub="0 点日结会把它清掉 ✓"),
            _metric("有记录的天数", "%d 天" % len(bot["days"]),
                    sub="最多列 %d 天" % ATTENTION_DAYS),
            _metric("最近一次", today["last"] or total["last"] or "—",
                    sub="最近一次被唤醒"),
        ]), tag="全部时间"))

        if bot["days"]:
            rows = [[d["day"], {"v": "%d 次" % d["count"], "cls": "num"},
                     {"v": "+%d" % d["amount"], "cls": "num pos"},
                     {"v": d["first"], "cls": "num"}, {"v": d["last"], "cls": "num"}]
                    for d in bot["days"]]
            table = _table(["日期", "次数", "折合愿力（她的）", "那天第一次", "那天最后一次"],
                           rows, align=(1, 2, 3, 4))
        else:
            table = _empty("还没有被注视过", "·")
        blocks.append(_card("按天", table, tag="最新在前 · 北京时间"))

        if bot["hours"]:
            peak = max([h["count"] for h in bot["hours"]] + [1])
            rows = [[{"v": "%02d:00" % h["hour"], "cls": "num"},
                     {"v": "%d 次" % h["count"], "cls": "num"},
                     {"v": '<span class="bar" style="width:%dpx"></span>'
                           % max(4, int(240 * h["count"] / peak)), "raw": True}]
                    for h in bot["hours"]]
            table = _table(["时", "次数", "多少"], rows, align=(0, 1))
        else:
            table = _empty("今天还没被注视过", "·")
        blocks.append(_card("今天按小时", table, tag=str(bot["day"])))

    blocks.append(_card("这一页是什么", '<div class="pad"><div class="note">'
                       '<div class="t">为什么单独一页</div>'
                       '<div class="r">被注视一天两三百笔，混在「最近流水」里会把整张表占满 '
                       '（所以流水那张表**整类不含**它）。这里只看**次数**。</div>'
                       '<div class="p">口径：每条唤醒 +1（`system → 她自己`）· '
                       '负数/日结不算注视 · 全部按北京时间 · 本页只读 ✓</div>'
                       '</div></div>'))
    if not blocks:
        blocks.append(_card("被注视", '<div class="pad">%s</div>'
                            % _empty("账本里还没有任何 bot 的账", "·")))
    return PAGE.format(title=ATTENTION_TITLE, nav=NAV_HOME, script="",
                       path=html.escape(str(data.get("path") or "")),
                       shown=datetime.fromtimestamp(int(data["now"]), CN_TZ)
                       .strftime("%Y-%m-%d %H:%M:%S"),
                       refresh_chip=chip,
                       body=flash + "".join(blocks))


class Handler(BaseHTTPRequestHandler):
    db_path = ""
    quiet = True
    refresh = False          # 是否允许「一键拉取」（由服务启动参数决定 ✓）
    refresh_fn = None        # 可注入的拉取函数（自检用它，不真连服务器）

    def _path_text(self):
        """`self.path` 在标准库里可能是 `pathlib.Path` —— 先转成字符串再解析 ✓"""
        return str(self.path or "/")

    def _same_origin(self):
        """只认本机来源：Host 必须是回环；带了 Origin/Referer 也必须是回环 ✓（防 CSRF）"""
        host = (self.headers.get("Host") or "").split(":")[0].strip().lower()
        if host not in ("127.0.0.1", "localhost", "::1", "[::1]"):
            return False
        for name in ("Origin", "Referer"):
            value = self.headers.get(name)
            if not value:
                continue
            parsed = urllib.parse.urlparse(value)
            if (parsed.hostname or "").lower() not in ("127.0.0.1", "localhost", "::1"):
                return False
        return True

    def _json(self, obj, code=200):
        payload = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def _refresh_now(self):
        if self.refresh_fn is not None:
            info = self.refresh_fn()
        else:
            import refresh as _refresh       # 懒导入：不开刷新时连它都不碰 ✓
            info = _refresh.pull()
        if isinstance(info, dict) and info.get("db"):
            type(self).db_path = info["db"]
        return info if isinstance(info, dict) else {"ok": False, "error": str(info)}

    def do_GET(self):                                   # noqa: N802 —— http.server 的约定
        target = self._path_text()
        path, _sep, query = target.partition("?")
        # 两个页面：`/` = 流水那一页 · `/attention` = 被注视次数那一页（主人 2026-10-14 要的 ✓）
        if path not in ("/", "/index.html", "/attention", "/attention.html"):
            self.send_error(404, "only / and /attention are served")
            return
        wanted_attention = path.startswith("/attention")
        # **片段模式**：面板自己那段内联 JS 来取"只换流水那几块"的 HTML ✓
        # （认这个头就只吐 `div.flows`；不认别的 ⇒ 仍然只是个只读 GET ✓）
        wanted_partial = (self.headers.get("X-Panel-Partial") or "").strip() == "1"
        flash = ""
        wants = urllib.parse.parse_qs(query or "")
        # 「再看更多」：`?n=36` 之类 ⇒ 这次多读一点（有上限、且必须是真的整数 ✓）
        try:
            limit = int((wants.get("n") or [BRIEF_LIMIT])[0])
        except (TypeError, ValueError):
            limit = BRIEF_LIMIT
        limit = max(BRIEF_LIMIT, min(limit, BRIEF_MAX))
        # 「看哪一天 / 哪一类」：`day=2026-10-14`（**空 = 今天** ✓）· `kind=gift`（空 = 全部）
        day = str((wants.get("day") or [""])[0])[:10]
        kind = str((wants.get("kind") or [""])[0])[:32]
        # 「一键拉取」：同一台机器上的页面点那个按钮 ⇒ 走这里（GET 只是为了让浏览器停在页面上 ✓）
        if "refresh" in wants and self.refresh and self._same_origin():
            try:
                info = self._refresh_now()
                if info.get("ok"):
                    flash = ('<div class="note ok"><div class="t">已从服务器拉取新副本</div>'
                             '<div class="r">%s</div></div>'
                             % html.escape("、".join(info.get("files") or [])))
                else:
                    flash = ('<div class="note"><div class="t">拉取失败</div><div class="r">%s</div></div>'
                             % html.escape(str(info.get("error") or "未知原因")))
            except Exception as exc:            # noqa: BLE001 —— 拉取失败要把话说清楚
                flash = ('<div class="note"><div class="t">拉取失败</div><div class="r">%s</div></div>'
                         % html.escape(str(exc)[:200]))
        try:
            if wanted_attention:
                payload = attention_page(attention_snapshot(self.db_path),
                                         refresh_on=self.refresh,
                                         flash=flash).encode("utf-8")
            else:
                data = snapshot(self.db_path, limit=limit, day=day, kind=kind)
                payload = (render_flow_fragment(data) if wanted_partial else
                           render(data, refresh_on=self.refresh, flash=flash)).encode("utf-8")
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

    def do_POST(self):                                  # noqa: N802 —— http.server 的约定
        """唯一的一个写动作：**去服务器拉一份新的只读副本**（不碰远端任何文件 ✓）"""
        path = self._path_text().split("?")[0]
        if path != "/api/refresh":
            self._json({"ok": False, "error": "only /api/refresh"}, 404)
            return
        if not self.refresh:
            self._json({"ok": False, "error": "这次启动没开一键拉取（用 --refresh 打开）"}, 403)
            return
        if not self._same_origin():
            self._json({"ok": False, "error": "只接受本机页面发来的请求"}, 403)
            return
        try:
            if self.refresh_fn is not None:
                info = self.refresh_fn()
            else:
                import refresh as _refresh       # 懒导入：不开刷新时连它都不碰 ✓
                info = _refresh.pull()
            if isinstance(info, dict) and info.get("db"):
                type(self).db_path = info["db"]
            self._json({"ok": bool(isinstance(info, dict) and info.get("ok")),
                        "info": info if isinstance(info, dict) else str(info)})
        except Exception as exc:                 # noqa: BLE001 —— 拉取失败要如实回报，不许静默
            self._json({"ok": False, "error": str(exc)[:200]}, 500)

    def log_message(self, *args):
        if not self.quiet:
            BaseHTTPRequestHandler.log_message(self, *args)


def serve(db_path, host="127.0.0.1", port=8765, quiet=True, refresh=False,
          refresh_fn=None):
    Handler.db_path = db_path
    Handler.quiet = quiet
    Handler.refresh = bool(refresh)
    Handler.refresh_fn = refresh_fn
    httpd = ThreadingHTTPServer((host, port), Handler)
    return httpd


def open_browser_once(url):
    """开**一次**浏览器（就一次，绝不重试 ✓）。

    ⚠️ 2026-10-13 真事故：`open_panel.bat` 里原本用 `start "" "http://…"` 让 cmd 开浏览器，
       结果**一直弹新窗口**（cmd 解析 URL 的行为随引号/环境影响）✗
       ⇒ 从此**不由 cmd 开**：交给 Python 的 `webbrowser`，并且**只调一次**、
        放进守护线程、失败就算了（不会重试、不会连锁）✓
    """
    def _once():
        try:
            import webbrowser
            webbrowser.open(url, new=2)
        except Exception:            # noqa: BLE001 —— 开不了浏览器不算错，地址已经打出来了
            pass
    thread = threading.Timer(0.8, _once)
    thread.daemon = True
    thread.start()


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="愿力中心 · 只读面板")
    parser.add_argument("--db", default="", help="数据文件（默认取插件的数据目录）")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--json", action="store_true", help="不开服务，直接吐 JSON")
    parser.add_argument("--self-id", default="", help="只看某个 bot（默认全部）")
    parser.add_argument("--refresh", action="store_true",
                        help="打开「一键拉取」（POST /api/refresh 会去服务器取新副本）")
    parser.add_argument("--no-refresh", action="store_true", help="明确关掉一键拉取")
    parser.add_argument("--open-browser", action="store_true",
                        help="起来之后自己开一次浏览器（就一次 ✓）")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    db_path = args.db or plugin.resolve_db_path()
    if args.json:
        print(json.dumps(snapshot(db_path, self_id=args.self_id or None),
                         ensure_ascii=False, indent=2))
        return 0
    allow_refresh = bool(args.refresh) and not bool(args.no_refresh)
    if not os.path.exists(db_path):
        print("⚠️ 数据文件还不存在：%s" % db_path)
        print("  （面板照常起得来，页面上会写「还读不到账本」✓）")
    httpd = serve(db_path, args.host, args.port, quiet=not args.verbose,
                  refresh=allow_refresh)
    url = "http://%s:%d/" % (args.host, args.port)
    print("愿力中心 · 只读面板：%s  （Ctrl+C 停）" % url)
    print("数据文件：%s" % db_path)
    print("一键拉取：%s" % ("开 ✓（页面上那个按钮）" if allow_refresh else "关（加 --refresh 打开）"))
    if args.open_browser:
        open_browser_once(url)
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
