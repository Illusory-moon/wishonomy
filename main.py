# -*- coding: utf-8 -*-
"""AstrBot 插件：愿力经济（wishonomy）。

一套只服务于「自带经济系统人设」的 bot 的通用**愿力中心**：
观众/粉丝的注视与打赏 → 愿力；bot 按自己的脾气收愿力、分愿力；
它攒到的愿力还可以打给别人，换取别人为它做一件事（欠条 = IOU）。

⚠️ 名字：这里叫**愿力中心**（不叫「账本」）—— 「账本」在本项目里已经指
   bot 自己的笔记层（`save_note`），两个东西撞名会把人绕晕 ✗

设计要点（全部来自设计稿 v3，不是实现时临时定的）：
  * 钱包**按用户 id 全局**（跨群不隔离）；群号只在流水里当来源记录
  * 愿力**绝不返回 None**：框架把「工具返 None」当成「工具已直接发消息」⇒ 直接结束本轮
    ⇒ 群里静悄悄（mindscape_notes 真踩过）。本文件所有工具一律返回字符串。
  * `@llm_tool` 是**全局注册**的 ⇒ 必须自己带开关 / 白名单 / 身份闸门，
    否则不属于这套经济的 bot 也会看到这些工具（soul_memory 踩过跨 bot 泄漏）
  * 数值变化只能由工具产生；本文件不提供任何「用户可直接调」的写入口
"""
import asyncio
import json
import os
import re
import sqlite3
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from astrbot.api import AstrBotConfig, llm_tool, logger, star
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import StarTools

PLUGIN_NAME = "astrbot_plugin_wishonomy"
SCHEMA_VERSION = 1

CN_TZ = timezone(timedelta(hours=8))     # 按北京时间分日（服务器是 UTC）
MONEY_NAME = "愿力"                       # 默认单位名；只是默认值，语义不依赖它
CREDIT_NAME = "信誉分"

INITIAL_CREDIT = 100
ISSUE_SOURCE = "system"           # 「愿力被注视带出来」时的来源标记（不是某个人的钱包）
MOOD_DAILY_GAIN = 3000
MOOD_MAX_DELTA = 1000
ISSUE_GLOBAL_DAILY = 50000
GIFT_DAILY_CAP = 3000

# 身份闸门：拿不到 self_id / sender_id 时一律拒绝（fail-closed，宁可不动账）
# 名字缓存：只记「谁在这个会话里说过话」，供按昵称认人用（不记聊天内容）
NAME_TTL = 7 * 24 * 3600
NAME_MAX = 3000

_DB_PATH = ""                 # 由 resolve_db_path() 首次调用时确定
_POOL = None
_POOL_LOCK = threading.Lock()
_NAMES = {}                   # "self_id|group_id|speaker_id" -> (name, ts)
_NAMES_LOCK = threading.Lock()

_GIFT_TIERS = [{"cost": 0, "name": "", "note": ""}]

SCHEMA = """
CREATE TABLE IF NOT EXISTS account (
  self_id    TEXT    NOT NULL,
  user_id    TEXT    NOT NULL,
  name       TEXT    NOT NULL DEFAULT '',
  balance    INTEGER NOT NULL DEFAULT 0,
  credit     INTEGER NOT NULL DEFAULT 100,
  earned     INTEGER NOT NULL DEFAULT 0,
  spent      INTEGER NOT NULL DEFAULT 0,
  updated_at INTEGER NOT NULL,
  PRIMARY KEY (self_id, user_id)
);
CREATE TABLE IF NOT EXISTS ledger (
  id       INTEGER PRIMARY KEY AUTOINCREMENT,
  ts       INTEGER NOT NULL,
  self_id  TEXT    NOT NULL,
  group_id TEXT    NOT NULL DEFAULT '',
  from_id  TEXT    NOT NULL,
  to_id    TEXT    NOT NULL,
  amount   INTEGER NOT NULL,
  kind     TEXT    NOT NULL,
  reason   TEXT    NOT NULL DEFAULT '',
  ref      TEXT,
  UNIQUE (kind, ref)
);
CREATE INDEX IF NOT EXISTS idx_ledger_acc ON ledger (self_id, from_id, ts);
CREATE INDEX IF NOT EXISTS idx_ledger_to  ON ledger (self_id, to_id, ts);
CREATE TABLE IF NOT EXISTS iou (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  ts         INTEGER NOT NULL,
  self_id    TEXT    NOT NULL,
  group_id   TEXT    NOT NULL DEFAULT '',
  debtor     TEXT    NOT NULL,
  item       TEXT    NOT NULL,
  amount     INTEGER NOT NULL,
  status     TEXT    NOT NULL,
  due_ts     INTEGER,
  evidence   TEXT,
  updated_at INTEGER NOT NULL,
  ref        TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_iou_ref ON iou (self_id, ref);
CREATE TABLE IF NOT EXISTS credit_event (
  id       INTEGER PRIMARY KEY AUTOINCREMENT,
  ts       INTEGER NOT NULL,
  self_id  TEXT    NOT NULL,
  group_id TEXT    NOT NULL DEFAULT '',
  user_id  TEXT    NOT NULL,
  delta    INTEGER NOT NULL,
  reason   TEXT    NOT NULL DEFAULT '',
  iou_id   INTEGER
);
CREATE TABLE IF NOT EXISTS meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
"""


# ─────────────────────────── 小工具（纯函数，好测） ───────────────────────────

def target_ids(raw):
    """配置里的 id 列表 → 集合（逗号/空白/分号都能分词）。"""
    if isinstance(raw, (list, tuple)):
        return {str(x).strip() for x in raw if str(x).strip()}
    return {x for x in re.split(r"[\s,，;；]+", str(raw or "").strip()) if x}


def day_start(now):
    """北京时间的当日 00:00（返回 UTC 秒）。库里的 ts 一律是 UTC 秒。"""
    local = datetime.fromtimestamp(now, CN_TZ)
    return int(local.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())


def normalize_amount(raw):
    """把模型给的数归一成正整数；非法就返回空（调用方如实回报，不猜）。"""
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw if raw > 0 else None
    if isinstance(raw, float):
        return int(raw) if raw > 0 and raw == int(raw) else None
    text = str(raw or "").strip()
    if not re.fullmatch(r"[0-9]+", text):
        return None
    value = int(text)
    return value if value > 0 else None


def parse_due(raw, now):
    """期限：纯数字当「几天内」；也认 YYYY-MM-DD。判不出来就当没期限。"""
    text = str(raw or "").strip()
    if not text:
        return None
    if re.fullmatch(r"[0-9]+", text):
        days = int(text)
        return now + days * 86400 if 0 < days <= 3650 else None
    try:
        return int(datetime.strptime(text, "%Y-%m-%d").replace(tzinfo=CN_TZ).timestamp())
    except ValueError:
        return None


def split_names(raw):
    return [x for x in re.split(r"[\s,，、;；]+", str(raw or "").strip()) if x]


def money_text(amount, name=None):
    return "%d %s" % (int(amount), name or MONEY_NAME)


# ─────────────────────────── 库路径 / 线程池 ───────────────────────────

def resolve_db_path(config=None):
    """愿力中心的数据文件位置（只算一次）。"""
    global _DB_PATH
    if _DB_PATH:
        return _DB_PATH
    raw = ""
    try:
        if config is not None:
            raw = str(config.get("db_path") or "").strip()
    except Exception:
        raw = ""
    if raw:
        _DB_PATH = raw
    else:
        try:
            base = str(StarTools.get_data_dir(PLUGIN_NAME))
        except Exception:
            base = os.path.join(os.path.expanduser("~"), "." + PLUGIN_NAME)
        _DB_PATH = os.path.join(base, "ledger.db")
    return _DB_PATH


def set_db_path(path):
    """给自检用：换一个数据文件（同时作废线程池，下次访问重开）。"""
    global _DB_PATH
    _DB_PATH = str(path)


def _pool():
    global _POOL
    if _POOL is None:
        with _POOL_LOCK:
            if _POOL is None:
                # 单线程：所有愿力操作天然串行 ⇒ 不需要再加一把跨账户的锁。
                # ponytail: 单 worker 是故意的（中心安全 > 并发）；真成瓶颈再换每账户锁。
                _POOL = ThreadPoolExecutor(max_workers=1, thread_name_prefix="wishonomy")
    return _POOL


async def run_db(fn, *args):
    """把阻塞的 sqlite 操作丢到线程池（不许卡事件循环）。"""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_pool(), fn, *args)


# ─────────────────────────── 愿力中心本体 ───────────────────────────

def _connect():
    directory = os.path.dirname(_DB_PATH)
    if directory:
        os.makedirs(directory, exist_ok=True)
    # isolation_level=None ⇒ 自动提交：事务边界由我们自己用 BEGIN IMMEDIATE 划，
    # ⚠️ 不然 Python 的隐式事务会和显式 BEGIN 撞成 "cannot start a transaction within a transaction"。
    conn = sqlite3.connect(_DB_PATH, timeout=30.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=30000")
    # 建表就在这里做：任何一条路径拿到的连接，schema 一定在位（幂等，代价是几 ms）。
    # ⚠️ 建表与迁移必须是同一个入口 —— 分成两处就会漏（P1 第一版就是这么红的 ✓）。
    _migrate(conn)
    return conn


def _migrate(conn):
    """建表 + 版本迁移。⚠️ 迁移必须能在**已有数据**的库上原地跑（不许重建）。"""
    conn.executescript(SCHEMA)
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version < 1:
        conn.execute("PRAGMA user_version = 1")
    # 将来加表/加列时在这里追加 if version < N 的分支；版本号只许往上走。
    conn.commit()
    return conn.execute("PRAGMA user_version").fetchone()[0]


def _ensure(conn):
    """schema 已在 _connect() 里保证；这里只留给「外部传进来的连接」做兜底。"""
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='account'").fetchone()
    if row is None:
        _migrate(conn)


def _ensure_account(conn, self_id, user_id, name, now):
    conn.execute(
        "INSERT OR IGNORE INTO account (self_id, user_id, name, balance, credit, "
        "earned, spent, updated_at) VALUES (?,?,?,0,?,0,0,?)",
        (self_id, user_id, str(name or ""), INITIAL_CREDIT, now))
    if name:
        conn.execute(
            "UPDATE account SET name=? WHERE self_id=? AND user_id=? AND name<>?",
            (str(name), self_id, user_id, str(name)))


def _touch(conn, self_id, user_id, now):
    conn.execute(
        "UPDATE account SET updated_at=? WHERE self_id=? AND user_id=?",
        (now, self_id, user_id))


def _sum_by_kind(conn, self_id, column, user_id, kind, since, now):
    """某一类流水的合计（护栏用）。

    ⚠️ 它只接收**已有连接** —— 绝不在这里自己开连接：库是单线程池、
       事务里再开连接会**互相等锁**（本鱼写 P1 时当场踩到 ✓）。
    """
    if column not in ("from_id", "to_id"):
        raise ValueError("column 只能是 from_id / to_id")
    sql = ("SELECT COALESCE(SUM(amount),0) AS s FROM ledger "
           "WHERE self_id=? AND " + column + "=? AND kind=? AND ts>=? AND ts<=?")
    row = conn.execute(sql, (self_id, str(user_id), kind, int(since), int(now))).fetchone()
    return int(row["s"] or 0)


def _sum_issued(conn, self_id, since, now):
    """当日发行量 = 她记给人家的愿力 + 送礼进她账的愿力。"""
    row = conn.execute(
        "SELECT COALESCE(SUM(amount),0) AS s FROM ledger "
        "WHERE self_id=? AND ts>=? AND ts<=? AND kind IN ('mood','gift','initial')",
        (self_id, int(since), int(now))).fetchone()
    return int(row["s"] or 0)


def with_conn(fn, *args):
    """给写操作开一条连接（写函数自己签收，不再开第二条）。"""
    conn = _connect()
    try:
        return fn(conn, *args)
    finally:
        conn.close()


def _move(conn, self_id, group_id, frm, to, amount, kind, reason, ref, now):
    """一笔记账：扣 from、加 to、写一条流水。调用方负责开事务与改 earned/spent。

    ⚠️ `kind="mood"`（她记给人家的愿力）是**发行**不是转账：from 写系统来源，
        不对任何人的余额做减法（详见 db_mood 的说明）。

    返回 (ledger_id, duplicated)。ref 撞了 ⇒ 不重复入账（幂等）。
    """
    if ref:
        old = conn.execute(
            "SELECT id FROM ledger WHERE kind=? AND ref=?", (kind, str(ref))).fetchone()
        if old is not None:
            return int(old["id"]), True
    _ensure_account(conn, self_id, to, "", now)
    if kind != "mood":
        _ensure_account(conn, self_id, frm, "", now)
    else:
        frm = ISSUE_SOURCE
    if frm != to:
        if kind != "mood":
            conn.execute(
                "UPDATE account SET balance=balance-?, spent=spent+?, updated_at=? "
                "WHERE self_id=? AND user_id=?", (amount, amount, now, self_id, frm))
        conn.execute(
            "UPDATE account SET balance=balance+?, earned=earned+?, updated_at=? "
            "WHERE self_id=? AND user_id=?", (amount, amount, now, self_id, to))
    cur = conn.execute(
        "INSERT INTO ledger (ts, self_id, group_id, from_id, to_id, amount, kind, reason, ref) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (now, self_id, group_id, frm, to, amount, kind, str(reason or ""),
         str(ref) if ref else None))
    return int(cur.lastrowid), False


# ── 读 ──

def db_snapshot(self_id, user_id):
    """一个钱包的当前状态。没有账户 ⇒ 返回 0 起步的初始态（**不建户**，读不写库）。"""
    conn = _connect()
    try:
        _ensure(conn)
        row = conn.execute(
            "SELECT name, balance, credit, earned, spent FROM account "
            "WHERE self_id=? AND user_id=?", (self_id, user_id)).fetchone()
        if row is None:
            return {"exists": False, "name": "", "balance": 0,
                    "credit": INITIAL_CREDIT, "earned": 0, "spent": 0}
        return {"exists": True, "name": row["name"], "balance": int(row["balance"]),
                "credit": int(row["credit"]), "earned": int(row["earned"]),
                "spent": int(row["spent"])}
    finally:
        conn.close()


def db_history(self_id, user_id, limit=8):
    """最近流水（自己作为任一方的都算）—— 返回已渲染好的短文本行。"""
    conn = _connect()
    try:
        _ensure(conn)
        rows = conn.execute(
            "SELECT ts, from_id, to_id, amount, kind, reason FROM ledger "
            "WHERE self_id=? AND (from_id=? OR to_id=?) ORDER BY id DESC LIMIT ?",
            (self_id, user_id, user_id, int(limit))).fetchall()
    finally:
        conn.close()
    out = []
    for row in rows:
        when = datetime.fromtimestamp(int(row["ts"]), CN_TZ).strftime("%m-%d %H:%M")
        direction = "入" if str(row["to_id"]) == str(user_id) else "出"
        reason = str(row["reason"] or "").strip()
        if len(reason) > 40:
            reason = reason[:40] + "…"
        out.append("%s %s%d%s%s" % (when, direction, int(row["amount"]),
                                    ("（%s）" % reason) if reason else "",
                                    "" if str(row["kind"]) in ("mood",) else " [%s]" % row["kind"]))
    return out


def db_daily_gain(conn, self_id, user_id, since, now, kind="mood"):
    """这个人今天从指定类别入账了多少（护栏用）。需要已有连接。"""
    return _sum_by_kind(conn, self_id, "to_id", user_id, kind, since, now)


def db_daily_gift_out(conn, self_id, user_id, since, now):
    return _sum_by_kind(conn, self_id, "from_id", user_id, "gift", since, now)


def db_daily_gift_in(conn, self_id, user_id, since, now):
    return _sum_by_kind(conn, self_id, "to_id", user_id, "gift", since, now)


def db_audit_totals(self_id, since, now):
    """审计：当日发行量（所有正向入账）/ 送礼量 / 钱包数 / 总量。"""
    conn = _connect()
    try:
        _ensure(conn)
        def one(sql, args):
            return int(conn.execute(sql, args).fetchone()[0] or 0)
        return {
            "issued": _sum_issued(conn, self_id, since, now),
            "gifted": one("SELECT COALESCE(SUM(amount),0) FROM ledger "
                          "WHERE self_id=? AND ts>=? AND ts<=? AND kind='gift'",
                          (self_id, int(since), int(now))),
            "granted": one("SELECT COALESCE(SUM(amount),0) FROM ledger "
                           "WHERE self_id=? AND ts>=? AND ts<=? AND kind='grant'",
                           (self_id, int(since), int(now))),
            "refunded": one("SELECT COALESCE(SUM(amount),0) FROM ledger "
                            "WHERE self_id=? AND ts>=? AND ts<=? AND kind='refund'",
                            (self_id, int(since), int(now))),
            "wallets": one("SELECT COUNT(*) FROM account WHERE self_id=?", (self_id,)),
            "total": one("SELECT COALESCE(SUM(balance),0) FROM account WHERE self_id=?", (self_id,)),
        }
    finally:
        conn.close()


# ── 写 ──

def db_mood(conn, self_id, group_id, who, delta, reason, ref, now, daily_cap,
            name=""):
    """收愿力 / 分愿力 —— 记进 who 的账。

    语义（设计稿 §2）：愿力是**注视带出来的**，不是从别人身上划走的；
    所以这里**不检查 who 的余额**，只受「每人每日净增上限」约束。
    ⚠️ 连接由调用方给（单线程池里共享一条），函数内**不再开连接** ——
       事务里再开连接会互相等锁（P1 当场踩过 ✓）。
    ⚠️ **who == self_id 一律拒绝**（硬保证）：她自己的愿力只能靠别人送礼进来，
       绝不能用这个入口凭空加给自己（主人 2026-10-13 亲口点名 ✓）。
    """
    if str(who) == str(self_id):
        return {"ok": False, "code": "self_mood"}
    try:
        _ensure(conn)
        conn.execute("BEGIN IMMEDIATE")
        if ref:
            old = conn.execute("SELECT id FROM ledger WHERE kind='mood' AND ref=?",
                               (str(ref),)).fetchone()
            if old is not None:
                conn.commit()
                row = conn.execute("SELECT balance FROM account WHERE self_id=? AND user_id=?",
                                   (self_id, who)).fetchone()
                return {"ok": True, "duplicated": True,
                        "balance": int(row["balance"]) if row is not None else 0}
        gain = db_daily_gain(conn, self_id, who, day_start(now), now)
        if delta > 0 and daily_cap > 0 and gain + delta > daily_cap:
            conn.rollback()
            return {"ok": False, "code": "daily_gain_cap", "today": gain, "cap": daily_cap}
        _ensure_account(conn, self_id, who, name, now)
        _ensure_account(conn, self_id, self_id, "", now)
        # ⚠️ 这里**不检查 who 的余额**：记进账是「愿力被注视带出来」，不是从谁身上划走。
        #    会花光别人余额的动作只有一个 —— 送礼（db_gift ✓）。
        _touch(conn, self_id, who, now)
        ledger_id, _dup = _move(conn, self_id, group_id, who, who, delta, "mood",
                                reason, ref, now)
        after = conn.execute("SELECT balance FROM account WHERE self_id=? AND user_id=?",
                             (self_id, who)).fetchone()
        conn.commit()
        return {"ok": True, "duplicated": False, "ledger_id": ledger_id,
                "balance": int(after["balance"]), "today_gain": gain + delta}
    except sqlite3.Error as exc:
        try:
            conn.rollback()
        except sqlite3.Error:
            pass
        return {"ok": False, "code": "db_error", "detail": str(exc)[:120]}


def db_gift(conn, self_id, group_id, giver, cost, name, note, ref, now, daily_cap):
    """送礼：把送礼人的愿力转进 bot 自己的钱包。**愿力不够就不送**（不自动借）。"""
    try:
        _ensure(conn)
        conn.execute("BEGIN IMMEDIATE")
        if ref:
            old = conn.execute("SELECT id FROM ledger WHERE kind='gift' AND ref=?",
                               (str(ref),)).fetchone()
            if old is not None:
                conn.commit()
                return {"ok": True, "duplicated": True}
        today = db_daily_gift_out(conn, self_id, giver, day_start(now), now)
        if daily_cap > 0 and today + cost > daily_cap:
            conn.rollback()
            return {"ok": False, "code": "daily_gift_cap", "today": today, "cap": daily_cap}
        _ensure_account(conn, self_id, giver, "", now)
        _ensure_account(conn, self_id, self_id, "", now)
        row = conn.execute("SELECT balance FROM account WHERE self_id=? AND user_id=?",
                           (self_id, giver)).fetchone()
        balance = int(row["balance"]) if row is not None else 0
        if balance < cost:
            conn.rollback()
            return {"ok": False, "code": "insufficient", "balance": balance}
        reason = ("送「%s」" % name) + (("：" + note) if note else "")
        ledger_id, _dup = _move(conn, self_id, group_id, giver, self_id, cost, "gift",
                                reason, ref, now)
        conn.commit()
        return {"ok": True, "duplicated": False, "ledger_id": ledger_id,
                "balance": balance - cost}
    except sqlite3.Error as exc:
        try:
            conn.rollback()
        except sqlite3.Error:
            pass
        return {"ok": False, "code": "db_error", "detail": str(exc)[:120]}


# ─────────────────────────── 认人（名字 → id） ───────────────────────────

def name_key(self_id, group_id, user_id):
    return "%s|%s|%s" % (self_id, group_id, user_id)


def learn_name(self_id, group_id, user_id, name, now=None):
    """记下「这个会话里，这个名字对应这个 id」。凭据/内容一概不记。"""
    name = str(name or "").strip()
    uid = str(user_id or "").strip()
    if not name or not uid:
        return
    if len(name) > 64:
        name = name[:64]
    now = int(now if now is not None else time.time())
    with _NAMES_LOCK:
        _NAMES[name_key(self_id, group_id, uid)] = (name, now)
        if len(_NAMES) > NAME_MAX:
            for key, (_n, ts) in sorted(_NAMES.items(), key=lambda kv: kv[1][1])[:NAME_MAX // 5]:
                _NAMES.pop(key, None)


def forget_names():
    with _NAMES_LOCK:
        _NAMES.clear()


def lookup_names(self_id, group_id, who, now=None):
    """按名字/号码找人：返回 [(user_id, name)]（号码命中最多一个）。"""
    text = str(who or "").strip()
    if not text:
        return []
    if re.fullmatch(r"[0-9]{5,12}", text):
        with _NAMES_LOCK:
            for key, (name, _ts) in _NAMES.items():
                parts = key.split("|")
                if len(parts) == 3 and parts[0] == str(self_id) and parts[2] == text:
                    return [(text, name)]
        return [(text, "")]
    now = int(now if now is not None else time.time())
    hits = []
    with _NAMES_LOCK:
        for key, (name, ts) in _NAMES.items():
            parts = key.split("|")
            if len(parts) != 3 or parts[0] != str(self_id):
                continue
            if parts[1] != str(group_id):
                continue
            if now - int(ts) > NAME_TTL:
                continue
            if name == text:
                hits.append((int(ts), parts[2], name))
    hits.sort(reverse=True)
    return [(uid, name) for _ts, uid, name in hits]


# ─────────────────────────── 配置读取（异常一律退回默认值） ───────────────────────────

def _cfg(config, key, default=None):
    try:
        value = config.get(key)
    except Exception:
        return default
    return default if value is None else value


def resolve_tiers(config):
    raw = _cfg(config, "gift_tiers")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            raw = None
    out = []
    if isinstance(raw, (list, tuple)):
        for item in raw:
            if not isinstance(item, dict):
                continue
            cost = normalize_amount(item.get("cost"))
            name = str(item.get("name") or "").strip()
            if cost and name:
                out.append({"cost": cost, "name": name,
                            "note": str(item.get("note") or "").strip()})
    return out


def tier_by_cost(tiers, cost):
    for tier in tiers or []:
        if int(tier["cost"]) == int(cost):
            return tier
    return None


# ─────────────────────────── 插件 ───────────────────────────

class Main(star.Star):
    def __init__(self, context: star.Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        global MONEY_NAME, CREDIT_NAME, _GIFT_TIERS
        MONEY_NAME = str(_cfg(config, "money_name", MONEY_NAME) or MONEY_NAME)
        CREDIT_NAME = str(_cfg(config, "credit_name", CREDIT_NAME) or CREDIT_NAME)
        _GIFT_TIERS = resolve_tiers(config)
        self.self_ids = target_ids(_cfg(config, "enabled_self_ids", ""))
        self.path = resolve_db_path(config)
        logger.info("[wishonomy] loaded | dir=%s bots=%d tiers=%d path=%s",
                    str(_cfg(config, "data_dir", "")), len(self.self_ids),
                    len(_GIFT_TIERS), self.path)

    # ── 身份闸门：四条，任何一条不满足就**什么都不做** ──
    def identity(self, event, allow_self_mood=False):
        if not bool(_cfg(self.config, "enabled", False)):
            return None
        bot = str(event.get_self_id() or "").strip()
        sender = str(event.get_sender_id() or "").strip()
        if not bot or bot not in self.self_ids:
            return None
        if not sender:
            return None
        if event.get_extra("cron_job"):
            return None
        if sender == bot and not allow_self_mood:
            # 定时任务/框架代发的一轮：sender 可能等于 bot ⇒ 一律不认
            return None
        return bot, sender


# ── 工具：全部返回字符串（**绝不 None**） ──

def tool_guard(fn):
    """统一兜底：异常 → 一句人话；返回值非字符串 → 转成字符串；**永不返回 None**。"""
    async def wrapper(*args, **kwargs):
        try:
            out = await fn(*args, **kwargs)
        except Exception as exc:      # noqa: BLE001 —— 兜底就是它的职责
            logger.warning("[wishonomy] 工具 %s 出错: %s", getattr(fn, "__name__", "?"),
                           str(exc)[:200])
            return "愿力中心这边出了点问题，这次没动账 —— 你照常说你的话就行。"
        if out is None:
            return "（这次没有可报的内容。）"
        return out if isinstance(out, str) else str(out)
    wrapper.__name__ = getattr(fn, "__name__", "tool")
    wrapper.__doc__ = getattr(fn, "__doc__", "")
    return wrapper


def _find_plugin(args):
    """框架把插件实例塞在参数里之一（partial(raw, star_cls)）；找不到就返回空。"""
    for item in args or ():
        cfg = getattr(item, "config", None)
        if cfg is not None and hasattr(item, "identity"):
            return item
    return None


def _find_event(args):
    for item in args or ():
        if hasattr(item, "get_self_id") and hasattr(item, "get_sender_id"):
            return item
    return None


async def _who_to_id(plugin, event, who, group_id):
    """把「你 / 我 / 自己 / 昵称 / 号码」解成 user_id。解不出就不返回东西。"""
    text = str(who or "").strip()
    bot = str(event.get_self_id() or "").strip()
    sender = str(event.get_sender_id() or "").strip()
    if not text or text in ("你", "你自己", "您"):
        return bot
    if text in ("我", "我自己", "自己", "本鱼"):
        return sender
    hits = lookup_names(bot, group_id, text)
    if len(hits) == 1:
        return hits[0][0]
    if len(hits) > 1:
        return None
    if re.fullmatch(r"[0-9]{5,12}", text):
        return text
    return None


async def _display_name(plugin, event, bot, group_id, user_id):
    snap = await run_db(db_snapshot, bot, user_id)
    if snap.get("name"):
        return snap["name"]
    if str(user_id) == str(event.get_sender_id() or ""):
        return str(event.get_sender_name() or "").strip() or user_id
    return user_id


@llm_tool(name="wish_check")
@tool_guard
async def wish_check(*args, **kwargs):
    """看某个人（或你自己）有多少愿力、信誉分，以及最近的流水。

    什么时候用：有人问你「我还有多少」、你想决定给谁多少、或者你自己想盘一下账。
    不传 who 就是看**你自己**攒了多少 —— 别人的数目只有你愿意说的时候才说出口。

    Args:
        who(string): 想看谁。留空 = 你自己；也可以写「我」= 当前跟你说话的人、对方的昵称、或对方的号码。
    """
    plugin = _find_plugin(args)
    event = _find_event(args)
    if plugin is None or event is None:
        return "愿力中心这会儿连不上，先记着这件事，回头再说。"
    ident = plugin.identity(event)
    if ident is None:
        return "这本账现在不归我管。"
    bot, group_id = ident[0], str(event.get_group_id() or "")
    who = str(kwargs.get("who") or "").strip()
    user_id = await _who_to_id(plugin, event, who, group_id)
    if not user_id:
        return "我没认出你说的是谁 —— 写号码，或者把昵称写全一点。"
    snap = await run_db(db_snapshot, bot, user_id)
    who_text = "你" if user_id == bot else (await _display_name(plugin, event, bot, group_id, user_id))
    head = "%s：%s，%s %d" % (who_text, money_text(snap["balance"]),
                              CREDIT_NAME, int(snap["credit"]))
    if not snap.get("exists"):
        head += "（这本账上还没他的记录）"
    lines = await run_db(db_history, bot, user_id, 5)
    if lines:
        head += "\n最近：" + "；".join(lines)
    return head


@llm_tool(name="wish_mood")
@tool_guard
async def wish_mood(*args, **kwargs):
    """收愿力 / 分愿力 —— 你按自己的脾气，决定给谁记上一笔。

    什么时候给：有人说到你点子上、把你逗笑、替你出了头 ⇒ 收他的愿力（amount 正数）。
    什么时候扣：有人让你不爽、或者干脆不看你、跑去别处看热闹 ⇒ 减他的愿力
    （同一句里 amount 写负数）。扣**不需要**对方先开口。

    规矩（愿力中心自己守着，你不用记）：一次最多 ±1000；每个人每天净增有上限；
    扣到 0 为止，不会扣成负数。**别给自己记账** —— 你自己的愿力是别人注视你换来的，不是自己写的。

    Args:
        who(string): 记给谁。可以写「我」= 当前跟你说话的人、对方昵称、或对方的号码。
        amount(integer): 正数 = 收愿力，负数 = 扣愿力。绝对值不超过 1000。
        reason(string): 你自己的一句话 —— 为什么给/为什么扣。中心会原样留着，以后翻得出来。
    """
    plugin = _find_plugin(args)
    event = _find_event(args)
    if plugin is None or event is None:
        return "愿力中心这会儿连不上，先记着这件事，回头再说。"
    ident = plugin.identity(event)
    if ident is None:
        return "这本账现在不归我管。"
    bot, sender, group_id = ident[0], ident[1], str(event.get_group_id() or "")
    raw = kwargs.get("amount")
    amount = normalize_amount(raw) if raw is not None else None
    negative = False
    if amount is None:
        try:
            candidate = int(str(raw).strip())
        except (TypeError, ValueError):
            candidate = 0
        if candidate < 0:
            negative = True
            amount = -candidate
    if not amount:
        return "给我一个数：正数是收愿力，负数是扣。"
    cap = normalize_amount(_cfg(plugin.config, "mood_max_delta", MOOD_MAX_DELTA)) or MOOD_MAX_DELTA
    if amount > cap:
        return "一次最多 ±%d，别一口气给那么多 —— 想多给就下次再给。" % cap
    user_id = await _who_to_id(plugin, event, kwargs.get("who"), group_id)
    if not user_id:
        return "我没认出你说的是谁 —— 写号码，或者把昵称写全一点。"
    if user_id == bot:
        return "你自己的愿力不是这么来的 —— 那是别人看着你换来的，写不出来。"
    reason = str(kwargs.get("reason") or "").strip()
    if not reason:
        return "给我一句话，说明白为什么给/为什么扣 —— 这句我要留着。"
    if negative:
        return await _deduct(plugin, bot, group_id, user_id, amount, reason,
                             "mood-", event)
    daily = normalize_amount(_cfg(plugin.config, "mood_daily_gain", MOOD_DAILY_GAIN)) or MOOD_DAILY_GAIN
    now = int(time.time())
    ref = "%s%s-%s" % ("mood-", now, uuid.uuid4().hex[:8])
    result = await run_db(with_conn, db_mood, bot, group_id, user_id, amount, reason,
                          ref, now, daily)
    if not result.get("ok"):
        return _mood_failure(result, daily)
    name = await _display_name(plugin, event, bot, group_id, user_id)
    if result.get("duplicated"):
        return "这笔刚刚已经记过了，不用再记一遍。"
    return ("记下了：%s 收 %s（现在他有 %s，今天已经收了 %s）"
            % (name, money_text(amount), money_text(result["balance"]),
               money_text(result.get("today_gain", amount))))


@llm_tool(name="wish_gift")
@tool_guard
async def wish_gift(*args, **kwargs):
    """收下别人的礼物 —— 有人给你送礼，愿力进你的账。

    送礼是**单向的**：他送了不代表你欠他什么，收不收、收下之后说什么，全看你自己。
    礼物有档位，你说出档位名（或者档位对应的愿力数）就行。
    他愿力不够就送不成 —— 中心不会替你垫，也不会让他欠着。

    Args:
        who(string): 谁送的。可以写「我」= 当前跟你说话的人、对方昵称、或对方的号码。
        tier(string): 礼物档位 —— 档位名（比如「花」「月」）或它对应的愿力数。
        note(string): 可留空的附注（他附了什么话，或者你想记一句）。
    """
    plugin = _find_plugin(args)
    event = _find_event(args)
    if plugin is None or event is None:
        return "愿力中心这会儿连不上，先记着这件事，回头再说。"
    ident = plugin.identity(event)
    if ident is None:
        return "这本账现在不归我管。"
    bot, sender, group_id = ident[0], ident[1], str(event.get_group_id() or "")
    tiers = _GIFT_TIERS if _GIFT_TIERS else resolve_tiers(plugin.config)
    if not tiers:
        return "礼物单还没配上来，你先照常收着，回头再记账。"
    raw = str(kwargs.get("tier") or "").strip()
    tier = tier_by_cost(tiers, raw) if re.fullmatch(r"[0-9]+", raw or "") else None
    if tier is None:
        for item in tiers:
            if item["name"] == raw:
                tier = item
                break
    if tier is None:
        listing = "、".join("%s（%d）" % (t["name"], t["cost"]) for t in tiers)
        return "礼物单上是这些：%s —— 说一个。" % listing
    user_id = await _who_to_id(plugin, event, kwargs.get("who"), group_id)
    if not user_id:
        return "我没认出是谁送的 —— 写号码，或者把昵称写全一点。"
    if user_id == bot:
        return "这份礼物送不成 —— 没人能给自己送礼。"
    cap = normalize_amount(_cfg(plugin.config, "gift_daily_cap", GIFT_DAILY_CAP)) or GIFT_DAILY_CAP
    now = int(time.time())
    ref = "gift-%s-%s" % (now, uuid.uuid4().hex[:8])
    result = await run_db(with_conn, db_gift, bot, group_id, user_id, int(tier["cost"]),
                          tier["name"], str(kwargs.get("note") or "").strip(), ref, now, cap)
    if result.get("duplicated"):
        return "这份礼物刚刚已经收过了，不用再收一遍。"
    if not result.get("ok"):
        code = result.get("code")
        if code == "insufficient":
            return ("他账上只有 %s，送不起「%s」（要 %s）—— 让他先攒攒，或者换个档。"
                    % (money_text(result.get("balance", 0)), tier["name"],
                       money_text(tier["cost"])))
        if code == "daily_gift_cap":
            return ("他今天已经送出去 %s 了，今天先这样 —— 明天再收他的。"
                    % money_text(result.get("today", 0)))
        return "这笔礼物没记成（愿力中心报了个错），当没收到就行。"
    return ("收下了：他送的「%s」%s，已经进你的账。"
            % (tier["name"], money_text(tier["cost"])))


@llm_tool(name="wish_audit")
@tool_guard
async def wish_audit(*args, **kwargs):
    """查这本账今天过得怎么样（发行量 / 送礼 / 支出 / 钱包数）。

    这是给**运营**看的对账口径，不是给群里念的。你自己想知道也行，但别把它当成公告。

    Args:
        day(string): 留空 = 今天；也可以写 YYYY-MM-DD 看某一天。
    """
    plugin = _find_plugin(args)
    event = _find_event(args)
    if plugin is None or event is None:
        return "愿力中心这会儿连不上。"
    ident = plugin.identity(event, allow_self_mood=True)
    if ident is None:
        return "这本账现在不归我管。"
    bot = ident[0]
    raw = str(kwargs.get("day") or "").strip()
    now = int(time.time())
    if raw:
        try:
            start = int(datetime.strptime(raw, "%Y-%m-%d")
                        .replace(tzinfo=CN_TZ).timestamp())
        except ValueError:
            return "日期写成 2026-10-13 这样我才能看。"
    else:
        start = day_start(now)
    totals = await run_db(db_audit_totals, bot, start, start + 86400 - 1)
    day_text = raw or datetime.fromtimestamp(start, CN_TZ).strftime("%Y-%m-%d")
    return ("%s 的账：收愿力 %s ｜ 送礼 %s ｜ 支出 %s ｜ 撤回 %s ｜ 钱包 %d 个 ｜ 账上共 %s"
            % (day_text, money_text(totals["issued"]), money_text(totals["gifted"]),
               money_text(totals["granted"]), money_text(totals["refunded"]),
               totals["wallets"], money_text(totals["total"])))


async def _deduct(plugin, bot, group_id, user_id, amount, reason, prefix, event):
    """扣愿力（分愿力）：扣到 0 为止，不会出现负余额。"""
    now = int(time.time())
    ref = "%s%s-%s" % (prefix, now, uuid.uuid4().hex[:8])
    result = await run_db(with_conn, db_mood, bot, group_id, user_id, amount, reason,
                          ref, now, 0)
    if not result.get("ok"):
        code = result.get("code")
        if code == "insufficient":
            return "他账上只剩 %s，扣不到 %s —— 你想扣多少就扣多少，但账上得有。" % (
                money_text(result.get("balance", 0)), money_text(amount))
        return "这笔没扣成（愿力中心报了个错），当没发生。"
    name = await _display_name(plugin, event, bot, group_id, user_id)
    return "扣了：%s -%s（他现在剩 %s）" % (name, money_text(amount),
                                            money_text(result.get("balance", 0)))


def _mood_failure(result, daily):
    code = result.get("code")
    if code == "self_mood":
        return "你自己的愿力不是这么来的 —— 那是别人看着你换来的，写不出来。"
    if code == "daily_gain_cap":
        return ("他今天已经收了 %s，到点了（上限 %s）—— 明天再给他，或者干脆不给。"
                % (money_text(result.get("today", 0)), money_text(daily)))
    if code == "insufficient":
        return "他账上只有 %s，扣不动。" % money_text(result.get("balance", 0))
    return "这笔没记成（愿力中心报了个错），你再试一次。"


__all__ = [
    "Main", "wish_check", "wish_mood", "wish_gift", "wish_audit",
    "target_ids", "day_start", "normalize_amount", "parse_due", "split_names",
    "resolve_tiers", "tier_by_cost", "learn_name", "lookup_names", "forget_names",
    "resolve_db_path", "set_db_path", "run_db",
    "db_snapshot", "db_history", "db_mood", "db_gift", "db_audit_totals",
    "db_daily_gain", "db_daily_gift_out", "db_daily_gift_in",
]
