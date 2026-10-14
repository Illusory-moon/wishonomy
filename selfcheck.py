# -*- coding: utf-8 -*-
"""愿力经济（wishonomy）· 自检 —— P1。

口径（照项目习惯）：
  * **失败就 exit 1**（真 `if` 分支，绝不"只打印不拦"）
  * 没有断言框架、没有 fixture：一个函数一项，返回 True/False
  * 拿**真库真事务**跑（临时目录），不是打桩模拟

跑法（本机）：
    D:\\smm-tempsparxie\\.venv\\Scripts\\python.exe D:\\sparxie\\wishonomy\\selfcheck.py
"""
import asyncio
import importlib.util
import json
import os
import sqlite3
import sys
import tempfile
import time
import types
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.join(HERE, "main.py")

BOT = "10001"          # 启用的 bot
OTHER_BOT = "10002"    # 没启用的 bot
ALICE = "20002"
BOB = "20003"
GROUP = "30001"
GROUP2 = "30002"


# ─────────────────────── 把 astrbot 顶层依赖打桩，再加载真模块 ───────────────────────

def load_plugin_module(tmpdir):
    api = types.ModuleType("astrbot.api")
    event_api = types.ModuleType("astrbot.api.event")
    star_api = types.ModuleType("astrbot.api.star")
    root = types.ModuleType("astrbot")

    class Star:
        def __init__(self, context=None):
            self.context = context

    class _Log:
        def info(self, *a, **k):
            pass

        def warning(self, *a, **k):
            pass

        def error(self, *a, **k):
            pass

    def llm_tool(name=None, **kwargs):
        def deco(fn):
            return fn
        return deco

    def hook(*a, **k):
        return lambda fn: fn

    api.AstrBotConfig = dict
    api.logger = _Log()
    api.star = star_api
    api.llm_tool = llm_tool
    star_api.Star = Star
    star_api.Context = object
    star_api.StarTools = types.SimpleNamespace(
        get_data_dir=lambda name: os.path.join(tmpdir, name))
    event_api.AstrMessageEvent = object
    event_api.filter = types.SimpleNamespace(
        on_llm_request=hook, on_llm_response=hook, on_agent_done=hook)

    core = types.ModuleType("astrbot.core")
    agent = types.ModuleType("astrbot.core.agent")
    agent_message = types.ModuleType("astrbot.core.agent.message")

    class TextPart:
        """框架的「接在用户消息之后」那个位置的文本片段（注入用）。"""

        def __init__(self, text):
            self.text = text

        def mark_as_temp(self):
            return self

    agent_message.TextPart = TextPart

    for name, mod in {
        "astrbot": root, "astrbot.api": api, "astrbot.api.event": event_api,
        "astrbot.api.star": star_api, "astrbot.core": core,
        "astrbot.core.agent": agent, "astrbot.core.agent.message": agent_message,
    }.items():
        sys.modules[name] = mod

    spec = importlib.util.spec_from_file_location("wishonomy_main", PLUGIN)
    module = importlib.util.module_from_spec(spec)
    sys.modules["wishonomy_main"] = module
    spec.loader.exec_module(module)
    return module


class Ev:
    """最小的平台事件替身（只实现插件真的用到的那几个方法）。"""

    def __init__(self, self_id=BOT, sender=ALICE, group=GROUP, name="", cron=False):
        self._self = self_id
        self._sender = sender
        self._group = group
        self._name = name
        self._extras = {"cron_job": True} if cron else {}

    def get_self_id(self):
        return self._self

    def get_sender_id(self):
        return self._sender

    def get_group_id(self):
        return self._group

    def get_sender_name(self):
        return self._name

    def get_extra(self, key=None, default=None):
        if key is None:
            return self._extras
        return self._extras.get(key, default)


CONFIG = {}

TIERS = [{"cost": 1, "name": "甲档", "note": ""},
         {"cost": 100, "name": "乙档", "note": ""},
         {"cost": 1380, "name": "丙档", "note": ""}]


def make_plugin(mod):
    CONFIG.clear()
    CONFIG.update({
        "enabled": True,
        "enabled_self_ids": [BOT],
        "money_name": "愿力",
        "inject_money": True,
        "attention_gain": True,
        "mood_max_delta": 1000,
        "mood_daily_gain": 3000,
        "issue_global_daily": 50000,
        "gift_daily_cap": 3000,
        "gift_tiers": TIERS,
    })
    plugin = mod.Main(context=None, config=CONFIG)
    return plugin


def call(tool, plugin, event, **kwargs):
    """按框架的方式调工具：插件实例与事件都当**位置参数**进 *args，参数走 kwargs。"""
    return asyncio.run(tool(plugin, event, **kwargs))


def call_raw(tool, plugin, event, *args):
    return asyncio.run(tool(plugin, event, *args))


def raw_mood(mod, user_id, delta, ref, now, cap=3000, group=GROUP, name=""):
    conn = mod._connect()
    try:
        return mod.db_mood(conn, BOT, group, user_id, delta, "自检", ref, now, cap,
                           name=name)
    finally:
        conn.close()


def raw_gift(mod, giver, cost, ref, now, cap=3000, name="甲档"):
    conn = mod._connect()
    try:
        return mod.db_gift(conn, BOT, GROUP, giver, cost, name, "", ref, now, cap)
    finally:
        conn.close()


def try_parse(mod, src, fn_name, args):
    """从源码里抠出一个纯函数来验（比另写一份模拟可信 ✓）。"""
    import ast
    tree = ast.parse(src)
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == fn_name:
            mod_ast = ast.Module(body=[node], type_ignores=[])
            namespace = {}
            exec(compile(mod_ast, "<extract>", "exec"), namespace)   # noqa: S102
            return namespace[fn_name](*args)
    raise AssertionError("源码里找不到 %s" % fn_name)


def ledger_of(mod, user_id, kind=None):
    conn = mod._connect()
    try:
        if kind:
            rows = conn.execute("SELECT * FROM ledger WHERE self_id=? AND from_id=? AND kind=?",
                                (BOT, user_id, kind)).fetchall()
        else:
            rows = conn.execute("SELECT * FROM ledger WHERE self_id=? AND (from_id=? OR to_id=?)",
                                (BOT, user_id, user_id)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def balance_of(mod, user_id):
    return mod.db_snapshot(BOT, user_id)["balance"]


# ─────────────────────────────── 各项自检 ───────────────────────────────

def t01_schema(mod, _plugin):
    """库结构：四张表 + user_version = 1 + WAL。"""
    conn = mod._connect()
    try:
        names = {r["name"] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        journal = conn.execute("PRAGMA journal_mode").fetchone()[0]
    finally:
        conn.close()
    need = {"account", "ledger", "iou", "credit_event", "meta"}
    if not need.issubset(names):
        print("      缺表:", need - names)
        return False
    if version != 1 or str(journal).lower() != "wal":
        print("      user_version=%s journal=%s" % (version, journal))
        return False
    return True


def t02_migration_idempotent(mod, _plugin):
    """迁移能在**已有数据**的库上反复跑，且不动数据。"""
    now = int(time.time())
    raw_mood(mod, ALICE, 7, "mig-1", now)
    before = balance_of(mod, ALICE)
    conn = mod._connect()
    try:
        mod._migrate(conn)          # 再跑一次
        version = conn.execute("PRAGMA user_version").fetchone()[0]
    finally:
        conn.close()
    return before == 7 and version == 1


def t03_mood_duplicate_ref(mod, _plugin):
    """同一笔（同 ref）不给第二次入账。"""
    now = int(time.time())
    n0 = len(ledger_of(mod, BOB))
    raw_mood(mod, BOB, 5, "dup-1", now)
    again = raw_mood(mod, BOB, 5, "dup-1", now)
    return (again.get("duplicated") is True
            and len(ledger_of(mod, BOB)) == n0 + 1
            and balance_of(mod, BOB) == 5)


def t04_daily_gain_cap(mod, _plugin):
    """每人每日净增上限（按人算，不按群）。"""
    now = int(time.time())
    first = raw_mood(mod, ALICE, 900, "cap-a", now, cap=1000)
    blocked = raw_mood(mod, ALICE, 200, "cap-b", now, cap=1000, group=GROUP2)
    ok_small = raw_mood(mod, ALICE, 100, "cap-c", now, cap=1000, group=GROUP2)
    over = raw_mood(mod, ALICE, 1, "cap-d", now, cap=1000)
    return (first.get("ok") is True
            and blocked.get("ok") is False and blocked.get("code") == "daily_gain_cap"
            and ok_small.get("ok") is True
            and over.get("ok") is False and balance_of(mod, ALICE) == 1000)


def t05_negative_balance_impossible(mod, _plugin):
    """能花光余额的动作只有一个（送礼）：花不起就失败，余额永远不为负。"""
    now = int(time.time())
    raw_mood(mod, BOB, 3, "neg-1", now)
    result = raw_gift(mod, BOB, 500, "neg-2", now)
    snap = mod.db_snapshot(BOT, BOB)
    return (result.get("ok") is False and result.get("code") == "insufficient"
            and snap["balance"] == 3 and snap["spent"] == 0)


def t06_ref_collision_across_kind(mod, _plugin):
    """幂等键按 (kind, ref) —— 不同类别的同名字符串互不影响。"""
    now = int(time.time())
    raw_mood(mod, ALICE, 1, "same-ref-seed", now)      # 先让她账上有 1，才送得起
    gift = raw_gift(mod, ALICE, 1, "same-ref", now)
    mood = raw_mood(mod, ALICE, 2, "same-ref", now)
    return gift.get("ok") is True and mood.get("ok") is True


def t07_tools_never_return_none_or_nonstr(mod, plugin):
    """所有工具在「畸形输入 + 正常输入」下都返回非空字符串。"""
    ev = Ev()
    cases = [
        (mod.wish_mood, {"who": "我", "amount": 5, "reason": "自检"}),
        (mod.wish_mood, {"who": "我", "amount": -5, "reason": "自检"}),
        (mod.wish_mood, {"who": "我", "amount": -5}),                 # 缺理由
        (mod.wish_mood, {"who": "不存在的人", "amount": 5, "reason": "x"}),
        (mod.wish_mood, {"who": "我", "amount": 99999, "reason": "x"}),  # 超上限
        (mod.wish_mood, {"who": "我", "amount": "abc", "reason": "x"}),  # 非数字
        (mod.wish_mood, {}),                                          # 空
        (mod.wish_check, {}),
        (mod.wish_check, {"who": "我"}),
        (mod.wish_check, {"who": "查无此人"}),
        (mod.wish_gift, {"who": "我", "tier": "甲档"}),
        (mod.wish_gift, {"who": "我", "tier": "999"}),
        (mod.wish_gift, {"who": "我"}),
        (mod.wish_audit, {}),
        (mod.wish_audit, {"day": "昨天"}),
    ]
    bad = []
    for tool, kwargs in cases:
        out = call(tool, plugin, ev, **kwargs)
        if not isinstance(out, str) or not out.strip():
            bad.append((tool.__name__, kwargs, repr(out)[:60]))
    if bad:
        for item in bad:
            print("      返回不是非空字符串:", item)
        return False
    return True


def t08_tool_guard_swallows_exceptions(mod, _plugin):
    """兜底：抛异常 / 返回 None 都不会漏出去（框架对 None 有特殊语义）。"""
    @mod.tool_guard
    async def boom(*a, **k):
        raise RuntimeError("炸了")

    @mod.tool_guard
    async def none_tool(*a, **k):
        return None

    out1 = asyncio.run(boom())
    out2 = asyncio.run(none_tool())
    return (isinstance(out1, str) and out1.strip()
            and isinstance(out2, str) and out2.strip())


def t09_identity_gate(mod, plugin):
    """身份闸门：四条，任何一条不满足都当作「不是我管的账」。"""
    cases = [
        (Ev(self_id=OTHER_BOT), "别的 bot"),
        (Ev(sender=BOT), "发送者就是 bot 自己"),
        (Ev(sender=""), "拿不到发送者"),
        (Ev(cron=True), "定时任务轮"),
    ]
    for ev, why in cases:
        if plugin.identity(ev) is not None:
            print("      闸门漏了:", why)
            return False
    return plugin.identity(Ev()) == (BOT, ALICE)


def t10_self_mood_rejected(mod, plugin):
    """她自己给自己记愿力 —— 工具与身份两道都要挡住。"""
    ev = Ev()
    before = balance_of(mod, BOT)
    out = call(mod.wish_mood, plugin, ev, who="你", amount=100, reason="自检")
    after = balance_of(mod, BOT)
    if after != before:
        print("      工具没拦住自记：%s → %s" % (before, after))
        return False
    if "不" not in out:
        print("      工具没说清为什么不行:", out)
        return False
    # 连底层也拦：把 who 写成 bot 自己，愿力中心**一分不许动**（自家钱包既不增也不减）
    now = int(time.time())
    res = raw_mood(mod, BOT, 50, "self-1", now)
    conn = mod._connect()
    try:
        bot_balance = conn.execute(
            "SELECT COALESCE(SUM(balance),0) AS s FROM account WHERE self_id=? AND user_id=?",
            (BOT, BOT)).fetchone()["s"]
        transfers = conn.execute(
            "SELECT COUNT(*) AS c FROM ledger WHERE self_id=? AND kind='mood'",
            (BOT,)).fetchone()["c"]
    finally:
        conn.close()
    if int(bot_balance) != 0:
        print("      自家钱包被写进了:", bot_balance)
        return False
    if balance_of(mod, BOT) != before:
        print("      自家余额变了:", before, "→", balance_of(mod, BOT))
        return False
    if res.get("code") != "self_mood" or res.get("ok") is not False:
        print("      底层没硬拦自记:", res)
        return False
    return True


def t11_gift_flow(mod, plugin):
    """送礼：愿力从送礼人进 bot 的账；不够就送不成；档位名要能认。"""
    ev = Ev(sender=ALICE, name="小甲")
    mod.learn_name(BOT, GROUP, ALICE, "小甲")
    now = int(time.time())
    raw_mood(mod, ALICE, 200, "gift-seed", now)
    before_bot = balance_of(mod, BOT)
    out_ok = call(mod.wish_gift, plugin, ev, who="小甲", tier="乙档")
    out_poor = call(mod.wish_gift, plugin, ev, who="小甲", tier="丙档")
    after_bot = balance_of(mod, BOT)
    return (after_bot == before_bot + 100
            and balance_of(mod, ALICE) == 100
            and "收下" in out_ok
            and "送不起" in out_poor)


def t12_name_resolution(mod, plugin):
    """认人：拼音名/号码都能找到；同名多人就不猜。"""
    ev = Ev(sender=BOB, name="小乙")
    mod.learn_name(BOT, GROUP, BOB, "小乙")
    mod.learn_name(BOT, GROUP, ALICE, "小乙")     # 故意重名
    ambiguous = asyncio.run(mod._who_to_id(plugin, ev, "小乙", GROUP))
    exact = asyncio.run(mod._who_to_id(plugin, ev, str(BOB), GROUP))
    me = asyncio.run(mod._who_to_id(plugin, ev, "我", GROUP))
    you = asyncio.run(mod._who_to_id(plugin, ev, "你", GROUP))
    mod.forget_names()
    mod.learn_name(BOT, GROUP, BOB, "小乙")
    single = asyncio.run(mod._who_to_id(plugin, ev, "小乙", GROUP))
    return (ambiguous is None and exact == BOB and me == BOB and you == BOT
            and single == BOB)


def t13_audit_numbers(mod, plugin):
    """审计口径：发行 / 送礼 / 钱包数 / 总量对得上。"""
    out = call(mod.wish_audit, plugin, Ev())
    conn = mod._connect()
    try:
        wallets = conn.execute("SELECT COUNT(*) AS c FROM account WHERE self_id=?",
                               (BOT,)).fetchone()["c"]
        total = conn.execute("SELECT COALESCE(SUM(balance),0) AS s FROM account WHERE self_id=?",
                             (BOT,)).fetchone()["s"]
        gifts = conn.execute("SELECT COALESCE(SUM(amount),0) AS s FROM ledger "
                             "WHERE self_id=? AND kind='gift'", (BOT,)).fetchone()["s"]
    finally:
        conn.close()
    return (str(wallets) in out and str(total) in out and str(gifts) in out
            and "钱包" in out and "送礼" in out)


def t14_db_history_readonly(mod, _plugin):
    """流水查询：能读出条目；查一个没记录的人不建户（读不写库）。"""
    now = int(time.time())
    raw_mood(mod, ALICE, 3, "hist-1", now)
    ghost = "29999"
    lines = mod.db_history(BOT, ALICE, 3)
    snap = mod.db_snapshot(BOT, ghost)
    conn = mod._connect()
    try:
        exists = conn.execute("SELECT COUNT(*) AS c FROM account WHERE self_id=? AND user_id=?",
                              (BOT, ghost)).fetchone()["c"]
    finally:
        conn.close()
    return len(lines) >= 1 and snap["exists"] is False and exists == 0


def t15_day_boundary_is_beijing(mod, _plugin):
    """分日按北京时间（服务器是 UTC）—— 差 8 小时不能算错。"""
    import datetime as dt
    utc_midnight = dt.datetime(2026, 10, 13, 16, 30, tzinfo=dt.timezone.utc).timestamp()
    start = mod.day_start(int(utc_midnight))
    got = dt.datetime.fromtimestamp(start, dt.timezone(dt.timedelta(hours=8)))
    return (got.year, got.month, got.day, got.hour, got.minute) == (2026, 10, 14, 0, 0)


def t16_amount_and_due_parsing(mod, _plugin):
    """数字归一与期限解析：畸形输入一律 None（不猜）。"""
    good = (mod.normalize_amount(5) == 5 and mod.normalize_amount("12") == 12
            and mod.normalize_amount(3.0) == 3)
    bad = all(mod.normalize_amount(x) is None
              for x in (0, -3, 2.5, "abc", "", None, True, "1e3"))
    now = 1_700_000_000
    due_days = mod.parse_due("3", now) == now + 3 * 86400
    due_blank = mod.parse_due("", now) is None
    due_junk = mod.parse_due("下周", now) is None
    due_date = isinstance(mod.parse_due("2026-12-31", now), int)
    return good and bad and due_days and due_blank and due_junk and due_date


def t17_transaction_rollback(mod, _plugin):
    """失败的分支不许留下半笔账（rollback 真的生效）。"""
    now = int(time.time())
    before = len(ledger_of(mod, BOB))
    raw_mood(mod, BOB, 999999, "rollback-1", now)         # 余额不足
    raw_mood(mod, BOB, 5000, "rollback-2", now, cap=100)  # 超日上限
    return len(ledger_of(mod, BOB)) == before


def t18_gift_daily_cap(mod, _plugin):
    """送礼也有单人单日上限（防互相刷礼）。"""
    now = int(time.time())
    raw_mood(mod, BOB, 500, "giftcap-seed", now)
    first = raw_gift(mod, BOB, 300, "giftcap-1", now, cap=300)
    second = raw_gift(mod, BOB, 1, "giftcap-2", now, cap=300)
    return (first.get("ok") is True and second.get("ok") is False
            and second.get("code") == "daily_gift_cap")


def t19_external_write_path_missing(mod, _plugin):
    """写入口只走工具：模块不暴露任何「用户可直接调」的写函数名。"""
    banned = [n for n in dir(mod) if n.startswith(("set_balance", "add_balance",
                                                   "credit_user", "grant_"))
              and callable(getattr(mod, n))]
    return not banned


def t20_json_safe_defaults(mod, _plugin):
    """配置畸形不炸：JSON 字符串能解析；坏值/缺项一律走兜底（**绝不静默变空** ✗）。

    ⚠️ 2026-10-13 起口径变了：坏掉的 `gift_tiers` 不再返回空表，而是**退回兜底档位** ✓
       （真机栽过：配置漏了这一项 ⇒ 礼物单空 ⇒ 送礼一分愿力不动 ✗ 见 R40）
    """
    ok_json = mod.resolve_tiers({"gift_tiers": "[{\"cost\": 7, \"name\": \"丁档\"}]"})
    if ok_json != [{"cost": 7, "name": "丁档", "note": ""}]:
        print("      JSON 字符串没解析对:", ok_json)
        return False
    fallback = len(mod.DEFAULT_GIFT_TIERS)
    for bad in ("不是 JSON", "", None, [], "{}", 5):
        got = mod.resolve_tiers({"gift_tiers": bad})
        if len(got) != fallback:
            print("      坏值 %r 没走兜底:" % (bad,), got)
            return False
    if mod.tier_by_cost(ok_json, 7)["name"] != "丁档":
        return False
    # 整份配置都是坏值时，实例化与工具也不许炸 ✓
    CONFIG.clear()
    CONFIG.update({"enabled": True, "enabled_self_ids": [BOT], "gift_tiers": "坏",
                   "mood_max_delta": "abc", "gift_daily_cap": -5, "grant_max_open": 0})
    try:
        plugin = mod.Main(context=None, config=CONFIG)
        out = call(mod.wish_mood, plugin, Ev(), who="我", amount=5, reason="畸形配置自检")
        return isinstance(out, str) and out.strip() != ""
    finally:
        make_plugin(mod)


def t21_source_hygiene(mod, _plugin):
    """源码卫生：没有打印调试残留、没有硬编码的 bot 号/群号、工具不 return None。"""
    with open(PLUGIN, encoding="utf-8") as handle:
        src = handle.read()
    if any(line.strip().startswith("print(") for line in src.splitlines()):
        print("      源码里有 print(")
        return False
    for token in ("2713420580", "1609770237", "1076751753", "870863632"):
        if token in src:
            print("      源码里出现私域号码:", token)
            return False
    # 只认「写在工具函数体里的 return None」（框架会把 None 当成「工具已直接发消息」⇒ 结束本轮）。
    # ⚠️ 别的函数返回 None 是正常的（解析不出来就该返回空）—— 不许一刀切地禁掉 ✓
    body = src.split("@llm_tool", 1)[-1]
    for chunk in body.split("@llm_tool")[1:]:
        head, _sep, _tail = chunk.partition("\n\n\n")
        if "return None" in head:
            print("      工具函数体里有 return None")
            return False
    for name in ("wish_check", "wish_mood", "wish_gift", "wish_audit"):
        if "async def %s(" % name not in src:
            print("      工具不见了:", name)
            return False
    return True


def t22_sum_by_kind_windows(mod, _plugin):
    """从源码里抠出护栏求和函数来验：类别 / 方向 / 时间窗都要分得清。"""
    with open(PLUGIN, encoding="utf-8") as handle:
        src = handle.read()
    now = int(time.time())
    raw_mood(mod, ALICE, 50, "sum-1", now)
    conn = mod._connect()
    try:
        before = try_parse(mod, src, "_sum_by_kind",
                           (conn, BOT, "to_id", ALICE, "mood", now - 86400, now))
        after = try_parse(mod, src, "_sum_by_kind",
                          (conn, BOT, "to_id", ALICE, "mood", now + 60, now + 120))
        gift_side = try_parse(mod, src, "_sum_by_kind",
                              (conn, BOT, "to_id", ALICE, "gift", now - 86400, now))
        try:
            try_parse(mod, src, "_sum_by_kind",
                      (conn, BOT, "balance; DROP TABLE ledger", ALICE, "mood", 0, now))
            print("      护栏求和没挡住拼接参数")
            return False
        except ValueError:
            pass
        return before == 50 and after == 0 and gift_side == 0
    finally:
        conn.close()


def t23_tool_docs_are_const(mod, _plugin):
    """工具说明必须是**常量**（docstring 里不许有随轮次变化的格式化占位）。"""
    import ast
    with open(PLUGIN, encoding="utf-8") as handle:
        src = handle.read()
    tree = ast.parse(src)
    found = 0
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("wish_"):
            doc = ast.get_docstring(node) or ""
            if not doc.strip():
                print("      工具没有说明（模型会看不懂）:", node.name)
                return False
            if "%s" in doc or "{}" in doc or "{0}" in doc:
                print("      工具说明里有格式化占位:", node.name)
                return False
            found += 1
    return found >= 4


def t23_cache_placement(mod, plugin):
    """缓存纪律：**越不稳的放越后**（常量进 system_prompt 尾部、每轮变的挂用户消息之后）。

    这是主人 2026-10-13 定的口径 —— 不是"不许注入"，而是"注入的位置要按稳定性排" ✓
    """
    class Req:
        def __init__(self):
            self.system_prompt = "人格与规矩"          # 稳定前缀
            self.prompt = "本轮用户消息"
            self.extra_user_content_parts = []

    now = int(time.time())
    raw_mood(mod, ALICE, 12, "inj-seed", now)
    raw_mood(mod, BOB, 3, "inj-seed-b", now)

    # ① 她（bot）自己没有账户 ⇒ 一个字都不注入（省 token 也省缓存）
    req_none = Req()
    asyncio.run(plugin.inject_money(Ev(sender=BOT), req_none))
    if req_none.extra_user_content_parts or req_none.system_prompt != "人格与规矩":
        print("      没账户也注入了:", req_none.system_prompt, req_none.extra_user_content_parts)
        return False

    # ② 有人说话 ⇒ 变动的那一行**只在 parts 里**，system_prompt 只多出常量段
    req_a = Req()
    asyncio.run(plugin.inject_money(Ev(sender=ALICE), req_a))
    if len(req_a.extra_user_content_parts) != 1:
        print("      变动内容没挂对位置:", req_a.extra_user_content_parts)
        return False
    hint = req_a.extra_user_content_parts[0].text
    if "12" not in hint:
        print("      提示里没有数字:", hint)
        return False
    # ⚠️ 2026-10-13 真机教训：这行必须**写清是谁的**，否则她会拿别人的数字解释自己 ✗
    if "跟你说话的那个人" not in hint:
        print("      提示没写清「这是谁的余额」:", hint)
        return False
    if "跟你说话的那个人" not in mod.INJECT_TAIL and "不是你的" not in mod.INJECT_TAIL:
        print("      常量段没说清「这不是你的账」:", mod.INJECT_TAIL[:60])
        return False
    # ⚠️ 2026-10-13 P1 真机：她答「愿力能送我礼物、也能使唤我干件事」✗ 方向反了
    #    ⇒ 注入尾巴必须写死方向 + **把礼物清单摆出来**（她答不出「有哪些礼物、多少钱」✗）
    tail = req_a.system_prompt[len("人格与规矩"):]
    if "使唤不动你" not in tail:
        print("      注入尾巴没说清「别人使唤不动你」✗")
        return False
    for tier in mod.resolve_tiers(CONFIG):
        if tier["name"] not in tail:
            print("      注入尾巴没带礼物清单，缺:", tier["name"])
            return False
    if "不是你的" not in tail:
        print("      注入尾巴没说「那行不是你自己的账」✗")
        return False
    if "12" in req_a.system_prompt:
        print("      变动数字跑进 system_prompt 了（会废掉后面的缓存）:", req_a.system_prompt)
        return False
    if mod.INJECT_TAIL not in req_a.system_prompt:
        print("      常量段没进 system_prompt:", req_a.system_prompt)
        return False
    if not req_a.system_prompt.startswith("人格与规矩"):
        print("      常量段没挂在尾部:", req_a.system_prompt)
        return False

    # ③ 两个都有账户的人 ⇒ **系统提示一模一样**（常量），只有那一个 part 变（变动）
    req_b = Req()
    asyncio.run(plugin.inject_money(Ev(sender=BOB), req_b))
    if req_b.system_prompt != req_a.system_prompt:
        print("      换个人就让系统提示变了（缓存会废）")
        return False
    hint_b = req_b.extra_user_content_parts[0].text if req_b.extra_user_content_parts else ""
    if hint_b == hint or "3" not in hint_b:
        print("      换个人提示却没变对:", hint_b)
        return False

    # ④ 注入过程**绝不写库**（只是读一眼余额）
    before = balance_of(mod, ALICE)
    req_c = Req()
    asyncio.run(plugin.inject_money(Ev(sender=ALICE), req_c))
    if balance_of(mod, ALICE) != before:
        print("      注入动了余额:", before, "→", balance_of(mod, ALICE))
        return False

    # ⑤ 开关关掉 ⇒ 一点都不注入
    CONFIG["inject_money"] = False
    try:
        req_off = Req()
        asyncio.run(plugin.inject_money(Ev(sender=ALICE), req_off))
        if req_off.extra_user_content_parts or req_off.system_prompt != "人格与规矩":
            print("      关了还在注入")
            return False
    finally:
        CONFIG["inject_money"] = True

    # ⑥ 身份闸门照旧管着注入（别的 bot / 定时轮 / 自己给自己 ⇒ 不注入）
    for ev, why in ((Ev(self_id=OTHER_BOT), "别的 bot"),
                    (Ev(cron=True), "定时轮"),
                    (Ev(sender=BOT), "发送者是自己")):
        req_g = Req()
        asyncio.run(plugin.inject_money(ev, req_g))
        if req_g.extra_user_content_parts or req_g.system_prompt != "人格与规矩":
            print("      闸门没管住注入:", why)
            return False
    return True


def t24_issue_global_fuse(mod, plugin):
    """全局日发行上限：**只记日志、不拦她**（设计稿 §2.1 的原话）。"""
    seen = []
    old_warning = mod.logger.warning
    mod.logger.warning = lambda *a, **k: seen.append(" ".join(str(x) for x in a))
    try:
        CONFIG["issue_global_daily"] = 5
        out = call(mod.wish_mood, plugin, Ev(), who="我", amount=4, reason="熔断自检")
        if "记下了" not in out:
            print("      第一笔记愿力就失败了:", out)
            return False
        out2 = call(mod.wish_mood, plugin, Ev(), who="我", amount=4, reason="熔断自检2")
        if "记下了" not in out2:
            print("      超上限**不该**拦她，但被拦了:", out2)
            return False
    finally:
        mod.logger.warning = old_warning
        CONFIG["issue_global_daily"] = 50000
    if not any("发行超上限" in line for line in seen):
        print("      超上限没记日志:", seen[:2])
        return False
    if balance_of(mod, ALICE) != 8:
        print("      余额不对（说明真被拦了）:", balance_of(mod, ALICE))
        return False
    return True


def t25_backup_whitelist(mod, _plugin):
    """备份清单：主库 + WAL 边车都要在（少一个 = 恢复时可能丢最后几笔）。"""
    paths = mod.data_paths()
    names = [os.path.basename(p) for p in paths]
    now = int(time.time())
    raw_mood(mod, ALICE, 3, "bk-1", now)
    return (len(paths) == 3 and names[0].endswith(".db")
            and names[1].endswith("-wal") and names[2].endswith("-shm")
            and os.path.exists(paths[0]))


def t26_config_hot_reload(mod, plugin):
    """配置改动**当轮就生效**（工具每次调用都读配置，不缓存）。"""
    try:
        CONFIG["mood_max_delta"] = 5
        too_big = call(mod.wish_mood, plugin, Ev(), who="我", amount=50, reason="热载自检")
        ok_small = call(mod.wish_mood, plugin, Ev(), who="我", amount=5, reason="热载自检")
    finally:
        CONFIG["mood_max_delta"] = 1000
    again = call(mod.wish_mood, plugin, Ev(), who="我", amount=50, reason="热载自检")
    if "最多" not in too_big:
        print("      改小上限没生效:", too_big)
        return False
    if "记下了" not in ok_small:
        print("      上限内的那笔该成功:", ok_small)
        return False
    if "记下了" not in again:
        print("      改回上限后该重新放行:", again)
        return False
    return True


def t27_audit_reports_issued(mod, plugin):
    """审计口径里能看到当日发行量（主人对账要看的那一行）。"""
    now = int(time.time())
    raw_mood(mod, ALICE, 42, "audit-1", now)
    out = call(mod.wish_audit, plugin, Ev())
    return "收愿力 42" in out and "送礼" in out and "钱包" in out


def raw_grant(mod, who, amount, item="念一句台词", ref=None, now=None, due=None,
              max_single=500, max_open=5, min_amount=None):
    now = int(now if now is not None else time.time())
    ref = ref or ("grant-self-%s" % uuid.uuid4().hex[:8])
    if min_amount is None:
        min_amount = getattr(mod, "GRANT_MIN_AMOUNT", 0)
    conn = mod._connect()
    try:
        return mod.db_iou_grant(conn, BOT, GROUP, who, amount, item, due, ref, now,
                                max_single, max_open, min_amount)
    finally:
        conn.close()


def raw_settle(mod, iou_id, verdict, evidence=""):
    conn = mod._connect()
    try:
        return mod.db_iou_settle(conn, BOT, iou_id, verdict, evidence, int(time.time()))
    finally:
        conn.close()


def raw_refund(mod, iou_id):
    conn = mod._connect()
    try:
        return mod.db_iou_refund(conn, BOT, iou_id, int(time.time()))
    finally:
        conn.close()


def open_ious(mod):
    conn = mod._connect()
    try:
        return mod.db_iou_all_open(conn, BOT, 20)
    finally:
        conn.close()


def credit_of(mod, user_id):
    return mod.db_snapshot(BOT, user_id)["credit"]


def seed_bot(mod, amount, tag="seedbot"):
    """给 **bot 自己**攒愿力 —— 只能走「别人送礼」这条路 ✓

    ⚠️ 不能用 raw_mood(BOT, …)：那条路被硬拦着（她不能给自己加愿力 ✓ 设计要的）。
    所以：先让送礼人有愿力，再让他送给她 —— 这正是她真实的进项 ✓
    """
    now = int(time.time())
    raw_mood(mod, BOB, amount, tag + "-bob", now)
    return raw_gift(mod, BOB, amount, tag + "-gift", now, cap=0)


def t28_grant_atomic(mod, plugin):
    """点单闭环：愿力**立刻到账** + 欠条入库；工具返回里带着欠条号 ✓"""
    now = int(time.time())
    seed_bot(mod, 400, "grant-seed")                    # 她的钱只能靠别人送礼进来 ✓
    before = balance_of(mod, BOT)
    out = call(mod.wish_grant, plugin, Ev(), who="我", amount=120, item="在群里念一句台词")
    if "欠条 #" not in out or "已经打给" not in out:
        print("      工具回话不对:", out)
        return False
    return (balance_of(mod, BOT) == before - 120
            and balance_of(mod, ALICE) == 120
            and len(open_ious(mod)) == 1)


def t29_grant_limits(mod, plugin):
    """点单四条护栏：单笔上限 / 未兑现上限 / 不能点自己 / 愿力不够。"""
    now = int(time.time())
    seed_bot(mod, 3000, "grant-seed2")
    single = call(mod.wish_grant, plugin, Ev(), who="我", amount=900, item="太大了")
    if "单笔最多" not in single:
        print("      单笔上限没拦住:", single)
        return False
    for i in range(5):
        raw_grant(mod, ALICE, mod.GRANT_MIN_AMOUNT, item="第%d件" % i)
    full = call(mod.wish_grant, plugin, Ev(), who="我", amount=mod.GRANT_MIN_AMOUNT, item="第六件")
    if "没结的欠条" not in full:
        print("      未兑现上限没拦住:", full)
        return False
    self_grant = raw_grant(mod, BOT, 10)
    if self_grant.get("code") != "self_grant":
        print("      没拦住点自己:", self_grant)
        return False
    # 愿力不够：先把手上那 5 笔清掉（否则会先撞"欠条太多"），再点一大笔
    for row in open_ious(mod):
        raw_settle(mod, row["id"], "forgiven")
    conn = mod._connect()
    try:
        conn.execute("UPDATE account SET balance=1 WHERE self_id=? AND user_id=?", (BOT, BOT))
    finally:
        conn.close()
    poor = raw_grant(mod, ALICE, 400)
    return poor.get("code") == "insufficient"


def t30_settle_verdicts(mod, plugin):
    """判定：做了 ⇒ 信誉分涨；没做 ⇒ 信誉分掉且**愿力不退**；判过的不许再判。"""
    now = int(time.time())
    seed_bot(mod, 500, "settle-seed")
    g1 = raw_grant(mod, ALICE, 50, item="做件事")
    g2 = raw_grant(mod, BOB, 50, item="另一件事")
    c0 = credit_of(mod, ALICE)
    done = raw_settle(mod, g1["iou_id"], "delivered", "他做了")
    c1 = credit_of(mod, ALICE)
    miss = raw_settle(mod, g2["iou_id"], "defaulted", "他没做")
    c2 = credit_of(mod, BOB)
    again = raw_settle(mod, g1["iou_id"], "delivered")
    if not (done.get("ok") and c1 == c0 + 5):
        print("      做了没涨信誉分:", done, c0, c1)
        return False
    if not (miss.get("ok") and c2 == 100 - 20):
        print("      没做没掉信誉分:", miss, c2)
        return False
    if again.get("code") != "already":
        print("      判过还能再判:", again)
        return False
    return balance_of(mod, ALICE) == 50 and balance_of(mod, BOB) == 50


def t31_refund_rules(mod, _plugin):
    """撤回：只在**对方一分没动**时成功；动过了就撤回失败、余额不动 ✓"""
    now = int(time.time())
    seed_bot(mod, 500, "refund-seed")
    # ① 没动过 ⇒ 撤回成功，愿力回到她账上，欠条变 refunded
    g1 = raw_grant(mod, ALICE, 60, item="还没做的事")
    r1 = raw_refund(mod, g1["iou_id"])
    if not r1.get("ok") or balance_of(mod, ALICE) != 0:
        print("      该撤的没撤成:", r1, balance_of(mod, ALICE))
        return False
    # ② 他把这笔花掉了一部分 ⇒ 余额不够还 ⇒ 撤回失败，余额不变（**绝不许扣成负数**）
    g2 = raw_grant(mod, BOB, 80, item="他已经在花这笔了")
    raw_gift(mod, BOB, 40, "refund-spend-1", now)     # 花掉 40 ⇒ 他只剩 40
    before_bob = balance_of(mod, BOB)
    r2 = raw_refund(mod, g2["iou_id"])
    if r2.get("code") != "spent" or balance_of(mod, BOB) != before_bob or before_bob < 0:
        print("      花过的还能撤:", r2, before_bob, balance_of(mod, BOB))
        return False
    # ③ 花的是**自己的**钱（送礼），她给的那笔一分没动 ⇒ **撤得回来** ✓
    g3 = raw_grant(mod, ALICE, 45, item="她给的那笔还没动")
    raw_mood(mod, ALICE, 25, "refund-own-money", now)   # 他自己另外赚了 25
    raw_gift(mod, ALICE, 25, "refund-own-spend", now)   # 又把自己那 25 送出去
    r3 = raw_refund(mod, g3["iou_id"])
    if not r3.get("ok") or balance_of(mod, ALICE) != 0:
        print("      自己花钱不该影响撤回:", r3, balance_of(mod, ALICE))
        return False
    return all(row["status"] == "open" for row in open_ious(mod) if row["id"] == g2["iou_id"])


def t32_ious_tool(mod, plugin):
    """看欠条：列出未结的；全结掉之后如实说「谁也不欠你什么」✓"""
    now = int(time.time())
    seed_bot(mod, 300, "ious-seed")
    g = raw_grant(mod, ALICE, 45, item="念台词", due=now + 3 * 86400)
    listing = call(mod.wish_ious, plugin, Ev())
    if "念台词" not in listing or "#%d" % g["iou_id"] not in listing:
        print("      欠条清单不对:", listing)
        return False
    raw_settle(mod, g["iou_id"], "forgiven")
    empty = call(mod.wish_ious, plugin, Ev())
    return "谁也不欠你什么" in empty


def t33_credit_tool(mod, plugin):
    """信誉分工具：正负都能记、超限拦住、理由必填、不许给自己写 ✓"""
    now = int(time.time())
    raw_mood(mod, ALICE, 5, "credit-seed", now)
    up = call(mod.wish_credit, plugin, Ev(), who="我", delta=7, reason="他说话算数")
    if "信誉分" not in up or credit_of(mod, ALICE) != 107:
        print("      加分没生效:", up, credit_of(mod, ALICE))
        return False
    down = call(mod.wish_credit, plugin, Ev(), who="我", delta=-10, reason="满嘴跑火车")
    if credit_of(mod, ALICE) != 97:
        print("      扣分没生效:", down, credit_of(mod, ALICE))
        return False
    too_big = call(mod.wish_credit, plugin, Ev(), who="我", delta=999, reason="太多")
    if "最多" not in too_big:
        print("      上限没拦住:", too_big)
        return False
    no_reason = call(mod.wish_credit, plugin, Ev(), who="我", delta=5)
    if "为什么" not in no_reason:
        print("      缺理由没拦住:", no_reason)
        return False
    me = call(mod.wish_credit, plugin, Ev(), who="你", delta=5, reason="自吹")
    if "不用自己写" not in me:
        print("      给自己写没拦住:", me)
        return False
    return credit_of(mod, ALICE) == 97


def t34_leaderboard(mod, plugin):
    """排行：按愿力 / 按信誉分两种口径；空账本要如实说 ✓"""
    now = int(time.time())
    empty = call(mod.wish_leaderboard, plugin, Ev())
    if "还没有别人" not in empty:
        print("      空账本没说清:", empty)
        return False
    raw_mood(mod, ALICE, 300, "lb-a", now)
    raw_mood(mod, BOB, 10, "lb-b", now)
    call(mod.wish_credit, plugin, Ev(), who="20003", delta=30, reason="很守约")
    by_money = call(mod.wish_leaderboard, plugin, Ev())
    by_credit = call(mod.wish_leaderboard, plugin, Ev(), by="信誉", n=2)
    if by_money.index(ALICE) > by_money.index(BOB):
        print("      按愿力排的顺序不对:", by_money)
        return False
    if by_credit.index(BOB) > by_credit.index(ALICE):
        print("      按信誉分排的顺序不对:", by_credit)
        return False
    if by_credit.count("\n") > 2:
        print("      n 没生效:", by_credit)
        return False
    return "信誉分" in by_credit


def t35_audit_detail(mod, plugin):
    """对账细化：待结欠条 / 信誉分增减 / 按人合计都要有 ✓"""
    now = int(time.time())
    seed_bot(mod, 400, "audit-detail")
    raw_mood(mod, ALICE, 60, "audit-detail-a", now)
    g = raw_grant(mod, ALICE, 45, item="留一张欠条")
    raw_settle(mod, g["iou_id"], "defaulted", "没做")
    out = call(mod.wish_audit, plugin, Ev())
    ok = ("还没兑现的欠条" in out and "收愿力" in out and "送礼" in out
          and "钱包" in out and "动得最多的人" in out)
    if not ok:
        print("      对账缺字段:", out)
        return False
    return ("欠条 1 张" in out or "欠条 0 张" in out)


class _Usage:
    def __init__(self, other, cached, out):
        self.input_other = other
        self.input_cached = cached
        self.output = out
        self.input = other + cached
        self.total = other + cached + out


class _Resp:
    def __init__(self):
        self.usage = _Usage(100, 200, 30)
        self.completion_text = "好"


class _EvWithSession(Ev):
    def get_session_id(self):
        return "test:GroupMessage:1"


def t36_usage_record(mod, plugin):
    """用量记账：口径与框架那份一致 + 多一个 src；不写库 ✓"""
    path = mod.resolve_usage_path()
    if os.path.exists(path):
        os.remove(path)
    ev = _EvWithSession(sender=ALICE)
    asyncio.run(plugin.record_usage(ev, _Resp()))
    if not os.path.exists(path):
        print("      没写出用量流水:", path)
        return False
    with open(path, encoding="utf-8") as handle:
        row = json.loads(handle.readline())
    want = {"t", "u", "s", "p", "m", "miss", "hit", "out", "src"}
    if set(row) != want:
        print("      字段与框架口径不一致:", sorted(row))
        return False
    if (row["miss"], row["hit"], row["out"], row["src"]) != (100, 200, 30, "wishonomy"):
        print("      数值/来源不对:", row)
        return False
    # 身份闸门照旧管着记账
    for bad, why in ((Ev(self_id=OTHER_BOT), "别的 bot"), (Ev(cron=True), "定时轮"),
                     (Ev(sender=BOT), "自己给自己")):
        asyncio.run(plugin.record_usage(bad, _Resp()))
    with open(path, encoding="utf-8") as handle:
        lines = [l for l in handle if l.strip()]
    if len(lines) != 1:
        print("      闸门没管住记账，行数 =", len(lines))
        return False
    conn = mod._connect()
    try:
        before = {t: conn.execute("SELECT COUNT(*) AS c FROM %s" % t).fetchone()["c"]
                  for t in ("account", "ledger", "iou", "credit_event")}
    finally:
        conn.close()
    asyncio.run(plugin.record_usage(ev, _Resp()))
    conn = mod._connect()
    try:
        after = {t: conn.execute("SELECT COUNT(*) AS c FROM %s" % t).fetchone()["c"]
                 for t in ("account", "ledger", "iou", "credit_event")}
    finally:
        conn.close()
    return before == after


def t37_usage_summary(mod, plugin):
    """用量汇总：按天聚合、按 src 过滤（别人的行不能混进来）✓"""
    path = mod.resolve_usage_path()
    if os.path.exists(path):
        os.remove(path)
    ev = _EvWithSession(sender=ALICE)
    asyncio.run(plugin.record_usage(ev, _Resp()))
    with open(path, encoding="utf-8") as handle:
        row = json.loads(handle.readline())
    other = json.loads(json.dumps(row))
    other["src"] = "别的插件"
    other["out"] = 9999
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(other, ensure_ascii=False) + "\n")
    data = mod.usage_summary(days=7)
    empty = mod.usage_summary(days=7, src="根本不存在的来源")
    if not data["exists"] or data["rows"] != 1 or data["total"]["out"] != 30:
        print("      汇总不对:", data)
        return False
    if empty["rows"] != 0:
        print("      src 过滤没生效:", empty["rows"])
        return False
    out = call(mod.wish_usage, plugin, Ev(), days=7)
    return isinstance(out, str) and out.strip() != ""


def t38_panel_not_shipped(mod, _plugin):
    """面板不进容器：`panel.py` 里那句 `import main` 在线上会导到框架的 main ✗ ✓"""
    here = os.path.dirname(PLUGIN)
    panel = os.path.join(here, "panel.py")
    if not os.path.exists(panel):
        return True
    with open(panel, encoding="utf-8") as handle:
        src = handle.read()
    if "import main as plugin" not in src:
        return True
    deploy_dir = os.path.join(here, "dist")
    if not os.path.isdir(deploy_dir):
        return True                                  # 还没有部署产物目录 ⇒ 无需断言
    shipped = [fn for fn in os.listdir(deploy_dir) if fn == "panel.py"]
    if shipped:
        print("      面板被放进部署产物了 ✗:", shipped)
        return False
    return True


def t39_framework_instantiation(mod, _plugin):
    """**框架只传 context** 时也要能实例化（2026-10-13 上线当场栽在这儿 ✗）

    框架那句是 `metadata.star_cls_type(context=self.context)` ——
    少传 config 就 `TypeError: missing 1 required positional argument` ✗
    ⇒ 自检里永远要按**框架的调用方式**实例化一次 ✓
    """
    CONFIG.clear()                                  # 故意不给配置 ⇒ 走"自己去读文件"那条路
    try:
        plugin = mod.Main(context=None)             # 只传 context（框架就是这么调的）
    except TypeError as exc:
        print("      框架式实例化失败了:", exc)
        return False
    finally:
        make_plugin(mod)                            # 把配置补回来给后面的项用
    # 没有配置 ⇒ 一切按"不启用"，但**不许崩** ✓
    if plugin.identity(Ev()) is not None:
        print("      没配置却启用了身份闸门 ✗")
        return False
    return isinstance(getattr(plugin, "config", None), dict)


def t40_tiers_default_when_missing(mod, _plugin):
    """配置**漏了 `gift_tiers`** 时必须有兜底档位，不能是空表（2026-10-13 真机栽过 ✗）

    真机现场：配置是手写的、没写 `gift_tiers`，而框架**不会**把 schema 的 default
    自动落盘 ⇒ `resolve_tiers` 返回 `[]` ⇒ 她认不出任何礼物、**送礼一分愿力不动** ✗
    """
    tiers = mod.resolve_tiers({})
    merged = mod.merge_config({"enabled": True})
    default_keys = mod.config_defaults()
    if len(tiers) != len(mod.DEFAULT_GIFT_TIERS):
        print("      缺配置时档位表是空的 ✗:", tiers)
        return False
    if not all(t["cost"] and t["name"] for t in tiers):
        print("      兜底档位不完整:", tiers)
        return False
    if "gift_tiers" not in merged or len(merged["gift_tiers"]) != len(tiers):
        print("      merge_config 没补上 gift_tiers:", merged.get("gift_tiers"))
        return False
    if set(merged) != set(default_keys):
        print("      merge_config 的键与 defaults 不一致:",
              sorted(set(merged) ^ set(default_keys)))
        return False
    # 已给的项不许被默认值覆盖 ✓
    kept = mod.merge_config({"money_name": "自定名", "gift_tiers": [{"cost": 7, "name": "甲"}]})
    return kept["money_name"] == "自定名" and len(kept["gift_tiers"]) == 1


def t41_gift_count(mod, plugin):
    """送礼的「份数」：数字**不当档位名**、count 才是个数（2026-10-13 真机踩过 ✗）

    真机现场：主人说「送你 30 个伊磨吉」，她填 `tier='30'` ⇒ 被解释成"第 30 号档位" ✗
     ⇒ 只收了 1 个伊磨吉（1 号档），主人的 30 变成 29 ⇒ 双方都懵 ✓
    """
    now = int(time.time())
    raw_mood(mod, ALICE, 500, "gift-count-seed", now)
    ev = Ev(sender=ALICE, name="小甲")
    mod.learn_name(BOT, GROUP, ALICE, "小甲")
    tiers = mod.resolve_tiers(CONFIG)
    cheap = min(tiers, key=lambda t: t["cost"])
    # ① 数字当档位名 ⇒ 必须**拦下来**（不能静默收别的东西）
    bad = call(mod.wish_gift, plugin, ev, who="小甲", tier="30")
    if "别写数字" not in bad:
        print("      数字当档位名没被拦住:", bad)
        return False
    # ② 报名字 + count ⇒ 按份数收
    before = balance_of(mod, ALICE)
    many = call(mod.wish_gift, plugin, ev, who="小甲", tier=cheap["name"], count=30)
    spent = before - balance_of(mod, ALICE)
    if spent != cheap["cost"] * 30:
        print("      count 没生效：扣了 %s（应为 %s）" % (spent, cheap["cost"] * 30))
        return False
    if "×30" not in many:
        print("      回话没说清份数:", many)
        return False
    # ③ 单份照旧
    single = call(mod.wish_gift, plugin, ev, who="小甲", tier=cheap["name"])
    if "收下了" not in single or "×" in single:
        print("      单份回话不对:", single)
        return False
    # ④ 份数把日上限撞了 ⇒ 直接拦下，不落账
    cap_now = balance_of(mod, ALICE)
    over = call(mod.wish_gift, plugin, ev, who="小甲", tier=cheap["name"], count=99999)
    if "超天" not in over or balance_of(mod, ALICE) != cap_now:
        print("      超上限没拦住:", over, balance_of(mod, ALICE))
        return False
    return True


class _RespMood:
    def __init__(self, text):
        self.completion_text = text
        self.usage = None


def t42_mood_tag_parse(mod, _plugin):
    """私有心情标签：三种写法都认、任何位置都扒、**正文一个字不删** ✓"""
    cases = [
        ("双中括号", '你这话我可记住了~[[wish_mood]]{"delta":5,"reason":"他今天嘴甜"}[[/wish_mood]]',
         5, "你这话我可记住了~"),
        ("无变化", '嗯，知道了~ [[wish_mood]]{"delta":0}[[/wish_mood]]', 0, "嗯，知道了~"),
        ("在中间", '前半句[[wish_mood]]{"delta":-3,"reason":"拆台"}[[/wish_mood]]后半句',
         -3, "前半句后半句"),
        ("尖括号", '收到<wish_mood>{"delta":2,"reason":"接梗"}</wish_mood>', 2, "收到"),
        ("没有标签（不许误伤）", '今天就聊到这儿吧~', 0, "今天就聊到这儿吧~"),
    ]
    for name, text, want_delta, want_text in cases:
        cleaned, delta, _reason = mod.parse_mood_tag(text)
        if "wish_mood" in cleaned:
            print("      %s：标签没扒干净 ✗ %r" % (name, cleaned[-40:]))
            return False
        if delta != want_delta:
            print("      %s：delta 不对（%s ≠ %s）" % (name, delta, want_delta))
            return False
        if want_text not in cleaned:
            print("      %s：正文被删了（少了 %r）" % (name, want_text))
            return False
    return True


def t43_mood_tag_records(mod, plugin):
    """标签那条路**真的记账**：回复里的标签被扒掉 + 愿力真的涨 ✓

    ⚠️ 这是主人 2026-10-13 报的真问题（她只在聊到愿力时才记账 ✗）的回归 ✓
    """
    before = balance_of(mod, ALICE)
    resp = _RespMood('你这话我可记住了~[[wish_mood]]{"delta":7,"reason":"今天嘴甜"}[[/wish_mood]]')
    asyncio.run(plugin.record_mood(Ev(sender=ALICE), resp))
    if "wish_mood" in (resp.completion_text or ""):
        print("      标签漏在回复里:", resp.completion_text)
        return False
    if balance_of(mod, ALICE) != before + 7:
        print("      没记账：%s → %s" % (before, balance_of(mod, ALICE)))
        return False
    # 无变化 ⇒ 一分不动
    resp0 = _RespMood('随便说一句~[[wish_mood]]{"delta":0}[[/wish_mood]]')
    asyncio.run(plugin.record_mood(Ev(sender=ALICE), resp0))
    if balance_of(mod, ALICE) != before + 7 or "wish_mood" in resp0.completion_text:
        print("      delta=0 时动了账或漏了标签:", balance_of(mod, ALICE))
        return False
    # 她给自己 ⇒ 不许记账（身份闸门 + who==bot 双拦 ✓）
    resp_self = _RespMood('给自己记一笔[[wish_mood]]{"delta":50,"reason":"自夸"}[[/wish_mood]]')
    asyncio.run(plugin.record_mood(Ev(sender=BOT), resp_self))
    return balance_of(mod, BOT) == 0


def t44_settle_only_her(mod, _plugin):
    """日结：**只清她自己**、记一笔负流水（不删账）、花粉的钱一分不动 ✓"""
    now = int(time.time())
    raw_mood(mod, ALICE, 40, "settle-a", now)
    raw_mood(mod, BOT, 0, "settle-b", now)      # 这条会被拒（不许自记）⇒ 所以走送礼
    raw_gift(mod, ALICE, 10, "settle-gift", now)   # 她收到 10
    her_before = balance_of(mod, BOT)
    if her_before <= 0:
        print("      前置没造好（她账上应有 >0）:", her_before)
        return False
    conn = mod._connect()
    try:
        taken = mod.db_settle(conn, BOT, now)
        rows = conn.execute("SELECT kind, from_id, to_id, amount FROM ledger "
                            "WHERE self_id=? AND kind='settle'", (BOT,)).fetchall()
    finally:
        conn.close()
    if taken != her_before or balance_of(mod, BOT) != 0:
        print("      日结没生效：taken=%s 余额=%s" % (taken, balance_of(mod, BOT)))
        return False
    if len(rows) != 1 or int(rows[0]["amount"]) != her_before:
        print("      日结没记成负流水:", [dict(r) for r in rows])
        return False
    if str(rows[0]["from_id"]) != str(BOT):
        print("      日结流水的方向不对（应当 from=她自己）:", dict(rows[0]))
        return False
    if balance_of(mod, ALICE) != 30:
        print("      花粉的钱被清了 ✗:", balance_of(mod, ALICE))
        return False
    # 再结一次 ⇒ 不该重复扣（余额已经是 0 ✓）
    conn = mod._connect()
    try:
        again = mod.db_settle(conn, BOT, now)
    finally:
        conn.close()
    return again == 0 and balance_of(mod, BOT) == 0


def t45_attention_gain(mod, plugin):
    """被注视 +1：每条唤醒一次、走 mood 那条记账、护栏照旧管着 ✓"""
    before = balance_of(mod, BOT)
    asyncio.run(plugin.inject_money(Ev(sender=ALICE), _Req()))       # 一条唤醒
    one = balance_of(mod, BOT)
    if one != before + 1:
        print("      被注视没 +1：%s → %s" % (before, one))
        return False
    asyncio.run(plugin.inject_money(Ev(sender=BOB), _Req()))         # 再来一条
    if balance_of(mod, BOT) != one + 1:
        print("      第二次唤醒没 +1:", balance_of(mod, BOT))
        return False
    # 她自己在算的那一轮（sender == bot）不算"被注视" ✓
    asyncio.run(plugin.inject_money(Ev(sender=BOT), _Req()))
    if balance_of(mod, BOT) != one + 1:
        print("      自己那一轮也被算了 ✗:", balance_of(mod, BOT))
        return False
    # 关掉开关 ⇒ 不再涨
    CONFIG["attention_gain"] = False
    try:
        asyncio.run(plugin.inject_money(Ev(sender=ALICE), _Req()))
    finally:
        CONFIG["attention_gain"] = True
    return balance_of(mod, BOT) == one + 1


class _Req:
    def __init__(self):
        self.system_prompt = ""
        self.prompt = ""
        self.extra_user_content_parts = []


def t46_grant_floor(mod, plugin):
    """点单**最低一笔**：为了不浪费而拆小单，会被拦下来 ✓

    主人 2026-10-14 报的真现象：她的余额每天 0 点归零 ⇒ 一有愿力就急着花，
    立刻下了「当着全群喊一声火花大人」这种一愿力级的单 ✗
    """
    seed_bot(mod, 500, "floor-seed")
    floor = mod.GRANT_MIN_AMOUNT
    low = raw_grant(mod, ALICE, max(1, floor - 1), item="一件特别小的事")
    if low.get("code") != "under_min":
        print("      低于下限没被拦:", low)
        return False
    if balance_of(mod, ALICE) != 0:
        print("      被拦的那笔居然动账了:", balance_of(mod, ALICE))
        return False
    ok = raw_grant(mod, ALICE, floor, item="刚好到线的一件事")
    if not ok.get("ok") or balance_of(mod, ALICE) != floor:
        print("      到线的那笔该成功:", ok, balance_of(mod, ALICE))
        return False
    out = call(mod.wish_grant, plugin, Ev(), who="我", amount="1", item="再小一点")
    return "不值得你开口" in out


def t47_issued_net_of_deduct(mod, _plugin):
    """「收愿力」必须是**净额**，而且扣款行**自带负号** ✓

    ⚠️ 2026-10-14 真机对账发现的账目 bug（本鱼自己踩的 ✗）：
      「扣你两分」在库里原来是 `kind='mood'` 且 `amount=+2`（方向只在调用方 ✗）
      ⇒ `SUM(amount)` 把**扣出去的也算成发行** ⇒ 面板「收愿力」虚高 ✗
      ⇒ 现在 `db_mood(delta<0)` 直接记**负数行** ⇒ 汇总天然是净额 ✓
    """
    now = int(time.time())
    raw_mood(mod, ALICE, 30, "net-issued-a", now)          # 赏 ALICE 30
    raw_mood(mod, BOB, 20, "net-issued-seed", now)         # 先给 BOB 20，好让他有得扣 ✓
    conn = mod._connect()
    try:
        out = mod.db_mood(conn, BOT, GROUP, BOB, -12, "扣你两分意思一下", "net-issued-b",
                          now, 0)
        rows = conn.execute("SELECT amount FROM ledger WHERE kind='mood' AND amount<0"
                            ).fetchall()
        detail = mod.db_audit_detail(conn, BOT, mod.day_start(now), now)
        totals = mod.db_audit_totals(BOT, mod.day_start(now), now, conn=conn)
    finally:
        conn.close()                     # ⚠️ 不关的话 Windows 上临时目录都删不掉 ✗
    if not out.get("ok"):
        print("      扣愿力没成功:", out)
        return False
    if len(rows) != 1 or int(rows[0]["amount"]) != -12:
        print("      扣款行不是负数:", [dict(r) for r in rows])
        return False
    if detail.get("mood_down") != 12:
        print("      没单独统计「分愿力」:", detail.get("mood_down"))
        return False
    if totals["issued"] != 38:            # ALICE+30、BOB+20、BOB−12 ⇒ 净 38（不是 62 ✗）
        print("      收愿力不是净额：%s（应为 18）" % totals["issued"])
        return False
    return True


TESTS = [
    ("R01 库结构 / user_version / WAL", t01_schema),
    ("R02 迁移可重入（带数据）", t02_migration_idempotent),
    ("R03 幂等：同 ref 不重复入账", t03_mood_duplicate_ref),
    ("R04 每人每日净增上限（跨群同一个人）", t04_daily_gain_cap),
    ("R05 余额永不为负", t05_negative_balance_impossible),
    ("R06 幂等键按 (kind, ref)", t06_ref_collision_across_kind),
    ("R07 工具一律返回非空字符串", t07_tools_never_return_none_or_nonstr),
    ("R08 工具兜底吞异常 / 不吃 None", t08_tool_guard_swallows_exceptions),
    ("R09 身份闸门四条", t09_identity_gate),
    ("R10 不许给自己记愿力", t10_self_mood_rejected),
    ("R11 送礼闭环 + 愿力不足", t11_gift_flow),
    ("R12 认人：昵称 / 号码 / 重名", t12_name_resolution),
    ("R13 审计口径对得上", t13_audit_numbers),
    ("R14 查流水不建户（读不写库）", t14_db_history_readonly),
    ("R15 分日按北京时间", t15_day_boundary_is_beijing),
    ("R16 数字 / 期限解析不猜", t16_amount_and_due_parsing),
    ("R17 失败分支真回滚", t17_transaction_rollback),
    ("R18 送礼单人单日上限", t18_gift_daily_cap),
    ("R19 没有对外写入口", t19_external_write_path_missing),
    ("R20 配置畸形不炸", t20_json_safe_defaults),
    ("R21 源码卫生（无 print / 无私域号 / 无 return None）", t21_source_hygiene),
    ("R22 护栏求和：类别 / 方向 / 时间窗", t22_sum_by_kind_windows),
    ("R23 缓存纪律：常量进尾部 / 变动挂用户消息之后", t23_cache_placement),
    ("R24 全局日发行上限（只记日志、不拦她）", t24_issue_global_fuse),
    ("R25 备份清单含 WAL 边车", t25_backup_whitelist),
    ("R26 配置改动当轮生效", t26_config_hot_reload),
    ("R27 审计能看到当日发行量", t27_audit_reports_issued),
    ("R28 点单闭环：愿力立刻到账 + 欠条入库", t28_grant_atomic),
    ("R29 点单四条护栏（单笔/未兑现/自己/余额）", t29_grant_limits),
    ("R30 判定：守约涨分 / 违约掉分 / 不许重复判", t30_settle_verdicts),
    ("R31 撤回：只在那笔没被动过时成功", t31_refund_rules),
    ("R32 欠条清单（列未结 / 全清后如实说）", t32_ious_tool),
    ("R33 信誉分：加/减/上限/理由/不许自写", t33_credit_tool),
    ("R34 排行：两种口径 + n 限制", t34_leaderboard),
    ("R35 对账细化：欠条 / 信誉分 / 按人合计", t35_audit_detail),
    ("R36 用量记账：同口径 + src + 不写库", t36_usage_record),
    ("R37 用量汇总：按天 + 按来源过滤", t37_usage_summary),
    ("R38 面板不进部署产物", t38_panel_not_shipped),
    ("R39 框架式实例化（只传 context）", t39_framework_instantiation),
    ("R40 配置缺 gift_tiers 时有兜底档位", t40_tiers_default_when_missing),
    ("R41 送礼份数：数字不当档位名 / count 生效", t41_gift_count),
    ("R42 心情标签：三写法/任何位置/不删正文", t42_mood_tag_parse),
    ("R43 心情标签真记账 + 标签不漏进回复", t43_mood_tag_records),
    ("R44 日结：只清她自己 + 负流水不删账", t44_settle_only_her),
    ("R45 被注视 +1（每条唤醒一次）", t45_attention_gain),
    ("R46 点单最低一笔（拆小单会被拦）", t46_grant_floor),
    ("R47 收愿力是净额（扣人要减出去）", t47_issued_net_of_deduct),
]


def reset_state(mod):
    """每项自检开始前清空愿力中心与名字缓存 —— 各项必须**互不依赖**（顺序换了也要全绿 ✓）。"""
    conn = mod._connect()
    try:
        for table in ("ledger", "account", "iou", "credit_event"):
            conn.execute("DELETE FROM %s" % table)
    finally:
        conn.close()
    mod.forget_names()


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    passed, failed = [], []
    with tempfile.TemporaryDirectory(prefix="wishonomy-selfcheck-") as tmpdir:
        mod = load_plugin_module(tmpdir)
        mod.set_db_path(os.path.join(tmpdir, "ledger.db"))
        plugin = make_plugin(mod)
        print("=" * 66)
        print("愿力经济（wishonomy）· P1 自检")
        print("愿力中心:", mod.resolve_db_path())
        print("=" * 66)
        for name, fn in TESTS:
            reset_state(mod)
            try:
                ok = bool(fn(mod, plugin))
            except Exception as exc:            # noqa: BLE001 —— 自检要报出崩在哪一项
                ok = False
                print("      异常: %r" % (exc,))
            (passed if ok else failed).append(name)
            print("  [%s] %s" % ("OK  " if ok else "FAIL", name))
        print("-" * 66)
        print("通过 %d 项 / 失败 %d 项" % (len(passed), len(failed)))
        if failed:
            print("失败项:", "; ".join(failed))
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
