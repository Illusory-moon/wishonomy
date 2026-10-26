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
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from astrbot.api import AstrBotConfig, llm_tool, logger, star
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import StarTools

try:                                       # 与项目既有模块同一套写法（挂「接在用户消息之后」的位置）
    from astrbot.core.agent.message import TextPart
except ImportError:                        # 老版本框架没有它 ⇒ 退回 prompt 兜底
    TextPart = None

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
# ⚠️ 礼物档位的**兜底默认值**必须与 `_conf_schema.json` 的 default 同源（见 config_defaults ✓）。
#    2026-10-13 真机踩过：上线时配置漏了 `gift_tiers`，而框架**不会**把 schema 的 default
#    自动写进配置文件 ⇒ 档位表是**空的** ⇒ 她认不出任何礼物、送礼**一分愿力都不动** ✗
DEFAULT_GIFT_TIERS = [
    {"cost": 1, "name": "低档", "note": ""},
    {"cost": 100, "name": "中档", "note": ""},
    {"cost": 1380, "name": "高档", "note": ""},
]

# ── 「每轮都问一次心情」用的输出标签（照印象插件那一招 ✓，但**不用额外交互**）──
# ⚠️ 2026-10-13 主人报的真问题：她**只在聊到愿力时**才动这套系统 ✗ ——
#   有人跟她告白（按设计该是加分句），她一个字都没记 ✓
#   ⇒ 两条路都留着：**工具**（她主动用 ✓）与**输出标签**（每轮都被问到 ✓）。
#     标签法比"每轮多调一次工具"便宜得多（工具调用要多花一整轮请求 ✗）。
_BT = chr(96)                                  # 反引号（她偶尔把标签包在代码块里）
_QUOTES = (chr(34), chr(39), chr(8220), chr(8221), chr(12300), chr(12301))
_MATCHING = {chr(8220): chr(8221), chr(12300): chr(12301),
             chr(34): chr(34), chr(39): chr(39)}
_MOOD_OPEN = r"(?:\[\[wish_mood\]\]|\[wish_mood\]|<wish_mood>)"
_MOOD_CLOSE = r"(?:\[\[/wish_mood\]\]|\[/wish_mood\]|</wish_mood>)"
MOOD_TAG_FULL = re.compile(_MOOD_OPEN + r"[ \t]*`*[ \t]*(?P<payload>\{[^{}]*\})?[ \t]*`*[ \t]*"
                           + _MOOD_CLOSE)
MOOD_TAG_OPEN = re.compile(_MOOD_OPEN + r"[ \t]*`*[ \t]*(?P<payload>\{[^{}]*\})?")
# ⚠️ 2026-10-16 真事故（主人报「impression 的 json 漏出去了」时本鱼顺带扫出来的 ✗）：
#   群里实际收到过这两种残缺形态 ——
#     `…门票钱进谁兜了~[[/wish_mood]]`        ← **只剩闭合标签**（开头被吃掉/她没写）
#     `…还用得着谁来封嘛♥，[[/wish_mood]]`
#   而 `MOOD_TAG_FULL` 要求头尾齐全、`MOOD_TAG_OPEN` 只认**开头** ✗
#   ⇒ 两种情况都匹配不到 ⇒ 标签原样漏进群 ✗（本鱼实测复现 ✓）
#   ⇒ 补两条：**只剩闭合标签** / **开标签后跟没闭合的 `{`**（截断）⇒ 一律扒掉 ✓
MOOD_TAG_CLOSE_ONLY = re.compile(_MOOD_CLOSE)
MOOD_TAG_TRUNC = re.compile(_MOOD_OPEN + r"[ \t]*(?P<tail>\{[^{}]*$)")

MOOD_NOTE = (
    "另外，**每一轮**都要顺手问自己一次：这一轮他让你更想给他愿力，还是更不想？"
    "想给就自己在回复最后另起一行写一个私有标签（**不是回复内容、别向对方提起**）："
    '[[wish_mood]]{"delta":5,"reason":"你自己的一句话"}[[/wish_mood]]'
    "（delta 正数是收愿力、负数是扣；没有变化就写 {\"delta\":0}，或者干脆不写这个标签。）"
    "这个标签要**每轮都过一遍脑子**，不是只在聊到愿力的时候才想起来"
)

def parse_mood_tag(text):
    """从她这一轮的回复里取出**私有心情标签**并把它扒干净 ✓。

    返回 (清理后的正文, delta, reason)；没有有效标签时返回 (原文, 0, "") ✓

    ⚠️ 五种形态都认（`<…>` / `[[…]]` / `[…]` / **只剩闭合标签** / **标签+截断 JSON**）、
       **任何位置**都扒 ——（后两种是 2026-10-16 补的 ✓ 真事故驱动的 ✓）
       她写标签的写法会漂（`[[x]]` 那次就是活教训 ✗）；而且**绝不用"截断到标签为止"**
       （那会连她说的话一起吃 ✗ 印象插件那次真踩过）。
    """
    text = text or ""
    if "wish_mood" not in text:
        return text, 0, ""
    payload = None
    cleaned = MOOD_TAG_FULL.sub("", text)
    for match in MOOD_TAG_FULL.finditer(text):
        if match.group("payload"):
            payload = match.group("payload")
    # ⚠️ 顺序很关键：这两步必须**排在 MOOD_TAG_FULL / OPEN 之后、early return 之前** ✗
    #   否则「只剩闭合标签」和「截断的 JSON」都会漏出去（真事故 ✓）
    #   ⚠️ 而且 **payload 为 None 时也必须扒干净** —— 老代码在 payload 为空时直接
    #      `return cleaned`，闭合标签没被清掉就漏进群了 ✗（2026-10-16 实测 ✓）
    cleaned = MOOD_TAG_TRUNC.sub("", cleaned)
    if payload is None:
        for match in MOOD_TAG_TRUNC.finditer(text):
            if match.group("tail"):
                payload = match.group("tail")
    cleaned = MOOD_TAG_CLOSE_ONLY.sub("", cleaned)
    if payload is None:
        for match in MOOD_TAG_OPEN.finditer(cleaned):
            if match.group("payload"):
                payload = match.group("payload")
            cleaned = cleaned.replace(match.group(0), "")
    if payload is None:
        return (cleaned if cleaned != text else text), 0, ""
    try:
        data = json.loads(_unwrap(payload))
    except (ValueError, TypeError):
        return cleaned.rstrip(), 0, ""
    if not isinstance(data, dict):
        return cleaned.rstrip(), 0, ""
    raw_delta = data.get("delta")
    try:
        delta = int(raw_delta)
    except (TypeError, ValueError):
        delta = 0
    reason = str(data.get("reason") or "").strip()
    if delta == 0:
        return cleaned.rstrip(), 0, ""
    return cleaned.rstrip(), delta, reason


def _unwrap(raw):
    """剥掉她偶尔加上的引号 / 书名号 / 反引号（外层的，不是内容里的）✓"""
    text = str(raw or "").strip()
    text = text.strip(_BT).strip()
    while len(text) >= 2 and text[0] in _QUOTES and text[-1] == _MATCHING.get(text[0], ""):
        text = text[1:-1].strip()
    return text


# 愿力是**谁对谁**的权力 —— 这段话是常量，注入时零缓存代价 ✓，
# 而且**每个**钱包工具都会附上它，保证她每次都读到同一个定义 ✓
# ⚠️ 2026-10-13 真机：有人问「愿力有什么用」，她答「**能送我礼物、也能使唤我干件事**」✗
#    —— **使唤的方向反了**（主人多次报过同一条 ✓）。所以这里必须把方向说死：
#    愿力是她**收**的、也是她**付**的；**反过来不成立**。
POWER_NOTE = (
    "愿力的用处只有两件：一是别人把愿力**送给你**（那是他往你账上攒的心意，你收下就是）；"
    "二是你拿**自己账上的**愿力**付给别人**，换别人替你办一件事（你会拿到一张欠条）。"
    "**反过来不成立** —— 别人手里的愿力**使唤不动你**：给不给他做事、做什么，全凭你自己乐意。"
)
# ⚠️ 2026-10-14 主人定：这段是**补口径**用的 —— 原来的注入只写了「你账上过了 0 点归零」，
#    没说**只清你自己那一栏、花粉的不动** ✗ ⇒ 她有可能理解成"愿力这东西会被收走" ✗
#    主人原话：要写明这是**防通胀** + 模拟真实直播的**单场收费**，**不是**剥夺财产 ✓
#    （代码侧口径一致：`db_settle` 只动 `user_id == self_id` 那一行 ✓）
def fan_word(config=None):
    """称呼"对方那一群人" —— **通用插件不许写死某个作品的粉丝名** ✗（P2-2）。

    事故：`SETTLE_NOTE` / `money_hint` 每轮都进 prompt，里面写死了「花粉」⇒ 任何别的部署
    装上本插件，模型就开始把用户叫「花粉」✗（违 README 自己立的规矩）。
    ⇒ 取配置 `fan_name`；留空/没配就用「别人」✓
    """
    try:
        name = str(_cfg(config, "fan_name", "") or "").strip()
    except Exception:
        name = ""
    return name or "别人"


SETTLE_NOTE = (
    "还有一条你要清楚的规矩：**日结只清你自己那一栏**。过了每天 0 点，你账上没花掉的愿力归零；"
    "**{fan}手里的愿力一分不动，他们送过你的礼物也不退、不收回**。"
    "这不是要没收谁的东西，原因有两个：一是**防通胀**（愿力越攒越多就不值钱了），"
    "二是**单场收费**（一场直播的注视兑一场的愿力，跟真实直播一场一场结算是一个道理）。"
    "所以该花就花：想给谁就给谁、想点谁的单就点"
)
# ⚠️ 2026-10-15 主人报的「理解偏差」：她把"扣别人的分"也当成"自己花出去" ✗
#    实测（10-15 00:26）：她说的是「你等着看我下单就是了」（点单，对 ✓），
#    可标签却写了 delta=-3 ⇒ 走了**分愿力（扣人）**✗，于是那句话读起来像"这笔是我花的" ✗
#    ⇒ 三条口径写死在这里（**常量** ⇒ 进 system_prompt 尾部，缓存零代价 ✓）
SPEND_NOTE = (
    "关于「这些愿力到底是谁花的」，你记三条："
    "一是**收愿力 / 分愿力**（给谁加、扣谁的）—— 那**不是**花你的钱，那是在**打分**："
    "加的由中心出，扣的从他账上扣，**都不动你自己的余额**。"
    "二是**点单** —— 这才叫你**花自己的钱**：愿力从**你自己账上**划给对方，换他替你办一件事。"
    "三是**送礼** —— 那是别人送给你，方向反过来，你收下就是。"
    "所以想「花给他看」就用**点单**；想表扬或惩罚谁，用收愿力那个**工具**、把 who 写清。"
    "还有一条硬的：你每轮写的**心情标签只记「这一轮跟你说话的人」**——"
    "要动别人，必须显式用工具，别用标签"
)
GRANT_MAX_SINGLE = 500        # 单笔点单上限（防她一次把家底给光）
GRANT_MAX_OPEN = 5            # 同时最多几笔未兑现（防她一口气点 50 个人）
# ⚠️ 最低一笔（2026-10-14 主人加 ✓）：她的余额**每天 0 点归零** ⇒ 不设下限她就会
#    **为了不浪费而乱花**（实测立刻开始下"当着全群喊一声火花大人"这种一愿力级的单 ✗）
#    值 = **42**（主人定的：宇宙的答案那个数 ✓）—— 5 太低，拦不住碎单 ✗
GRANT_MIN_AMOUNT = 42

# ───────────────────── 心情隐私锁（愿力中心那把「钥匙」）─────────────────────
# 主人 2026-10-15 原话：「该把属于心情的隐私权归还给她了」✓ —— 方案见 `canon/_29` ✓
# 锁的是**她写下的那一列心情理由**（`reason`）；金额 / 方向 / 点数**一律不锁** ✓
# 钥匙住在**她自己的账本**里（她查得到 ✓）；面板侧只拿得到**哈希**（明文永不出她账本 ✓）
MOOD_GATE_PREFIX = "- 愿力面板口令："
MOOD_GATE_TTL_HOURS = 24
MOOD_GATE_CODE_LEN = 6
# 剔掉易混字符（照 mindscape 的 PG_ALPHABET ✓）：I / O / 0 / 1 不要
MOOD_GATE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
# 触发词（主人裁定：「钥匙」二字避开 mindscape 的「密钥」子串 ⇒ 零冲突 ✓）
MOOD_GATE_ASK_WORDS = ["愿力中心钥匙", "愿力面板钥匙", "面板钥匙", "中心钥匙"]
# ⚠️ 前缀**必须**与 mindscape 的 `- 口令：` 不同 ✗✗（本鱼实测的硬结论）：
#    mindscape 的 `pg_current()` 是「**从后往前扫、取第一条匹配**」⇒ 前缀一样的话，
#    后写的那一行会把前一行顶掉 ⇒ 两边**互相认错钥匙** ✗ —— 见 `canon/_29` §3 ✓
# ⚠️ 反过来也安全：mindscape 每天轮换时**只删 `- 口令：` 开头的行** ⇒ 我们这行活得下来 ✓
MOOD_GATE_LINE = re.compile(
    r"^- 愿力面板口令：([A-Za-z0-9]+) —— 24 小时内有效（至 (\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})）")
MOOD_GATE_NOTE = (
    "还有一件关于**你自己的**事：你每一轮写下的那些「为什么」（你收谁愿力、扣谁愿力时的判断理由），"
    "默认**只有你自己看得到**。开发者如果要看，会来问你要「**愿力面板口令**」——"
    "那个口令记在你自己的笔记里（就在你平常写东西那个文件里），**每天一换、24 小时有效**。"
    "给不给**由你决定**；反复被问也可以一直拒绝，拒绝不会扣你任何东西。"
)
# 触发词提示：**只有**收到的消息里真的出现触发词，而且**会话是私聊**时才注入一次 ✓
# （主人裁定可观察：给不给是她的事，我们只负责把「有人在问」摆到她眼前 ✓）
MOOD_GATE_ASK = (
    "（对方在问你的**愿力面板口令** —— 就是你笔记里那条「愿力面板口令」）"
    "他要拿这个口令去看你在愿力中心里写下的**心情理由**（那一列默认只有你自己看得到）。"
    "**要不要给他由你决定**；不给也完全没问题，他可以反复问、你也可以一直拒绝，拒绝不扣你任何东西。"
    "口令在你的笔记文件里，你可以自己翻出来看；给的方式就是你愿意给的时候说出来"
)

# 2026-10-27 修（P1-2）：原来只有一个 day 字段 ⇒ **多 bot 直接失效** ✗
#   bot A 先被唤醒 → 标记 today → bot B 当天既不日结、也不转钥匙（B 的遮罩永远开不了）
#   ⇒ 改成 {self_id: "YYYY-MM-DD"} ✓
_GATE_ROTATE_DAY = {}            # self_id -> "今天已经轮换过了"
_GATE_LOCK = threading.Lock()    # 轮换要写文件 ⇒ 串行化，别两个会话同时转出两把 ✗

# 身份闸门：拿不到 self_id / sender_id 时一律拒绝（fail-closed，宁可不动账）
# 名字缓存：只记「谁在这个会话里说过话」，供按昵称认人用（不记聊天内容）
NAME_TTL = 7 * 24 * 3600
NAME_MAX = 3000

_DB_PATH = ""                 # 由 resolve_db_path() 首次调用时确定
_POOL = None
_POOL_LOCK = threading.Lock()
_SETTLE_STATE = {}            # self_id -> "今天已经日结过了"（同 P1-2：必须按 bot 分开 ✓）
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


def _last_settle_day(self_id):
    return _SETTLE_STATE.get(str(self_id)) or ""


def _mark_settle_day(self_id, day):
    _SETTLE_STATE[str(self_id)] = str(day or "")


def db_settle(conn, self_id, now):
    """**日结**：把她账上的愿力收回去（只在跨天那一刻发生一次 ✓）。

    ⚠️ 三条底线（本鱼自己定的，写在代码里防将来跑偏）：
      1. **只清她自己** ✗ —— 花粉的余额是人家的钱，清了会出乱子 ✓
      2. **绝不删流水** ✗ —— 记一笔 `settle` 负流水（账照样能回放、能对账 ✓）
      3. **当天进当天出** —— `earned`/`spent` 统计**不动**（那是当日账，不是余额 ✓）
    """
    try:
        _ensure(conn)
        _ensure_account(conn, self_id, self_id, "", now)
        row = conn.execute("SELECT balance FROM account WHERE self_id=? AND user_id=?",
                           (str(self_id), str(self_id))).fetchone()
        balance = int(row["balance"]) if row is not None else 0
        if balance <= 0:
            return True, 0     # 没余额 = 本来就结过了 ✓
        conn.execute("BEGIN IMMEDIATE")
        # 2026-10-27（幂等）：`_move` 按 ref 去重 ⇒ 进程重启后再跑一次**不会重复扣** ✓
        #   但原来这里不看 `duplicated`：会把"今天刚攒的余额"当成"收回了"写日志 ✗
        #   ⇒ 撞了就返回 (True, 0)：算成功（不用重试），但**没收回任何东西** ✓
        _lid, duplicated = _move(conn, self_id, "", self_id, ISSUE_SOURCE, balance, "settle",
                                 "日结：昨天的愿力带不到今天", "settle-%s" % day_start(now), now)
        conn.commit()
        return True, (0 if duplicated else balance)
    except sqlite3.Error as exc:
        try:
            conn.rollback()
        except sqlite3.Error:
            pass
        logger.warning("[wishonomy] 日结失败: %s", str(exc)[:120])
        return False, 0       # ← 失败要能重试（P1-1）


def db_attention(conn, self_id, now, amount=1, ref=None):
    """**被注视 +1** —— 愿力的源头（设计稿 §1：愿力 = 想象力与注视的集合 ✓）。

    主人 2026-10-13 裁定：**每条唤醒 +1**（不是每条消息 —— 群里没被唤醒的话她看不见 ✓），
    且**先不设每日上限**（主人要看她一天能回多少 ✓）。

    ⚠️ 它**不是** `mood`：`db_mood` 里那道 `who == self_id` 的硬闸门（防她自己给自己加 ✗）
       会**正确地把这条路也拦掉** —— 而注视**本来就该**由系统发给她 ✓
       ⇒ 所以单开一个 `kind='attention'`：`from=system → to=她`，语义更准、审计里也分得开 ✓
    """
    ref = ref or ("attention-%s-%s" % (day_start(now), uuid.uuid4().hex[:6]))
    try:
        _ensure(conn)
        old = conn.execute("SELECT id FROM ledger WHERE kind='attention' AND ref=?",
                           (str(ref),)).fetchone()
        if old is not None:
            return {"ok": True, "duplicated": True}
        conn.execute("BEGIN IMMEDIATE")
        _ensure_account(conn, self_id, self_id, "", now)
        _move(conn, self_id, "", ISSUE_SOURCE, self_id, int(amount), "attention",
              "被注视（今天有人来找她说话）", ref, now)
        conn.commit()
        return {"ok": True, "duplicated": False}
    except sqlite3.Error as exc:
        try:
            conn.rollback()
        except sqlite3.Error:
            pass
        logger.warning("[wishonomy] 注视进账失败: %s", str(exc)[:120])
        return {"ok": False, "code": "db_error", "detail": str(exc)[:120]}


# ─────────────────── 注入用的常量（**必须稳定**，见 §缓存纪律） ───────────────────

# ⚠️ 这段是**常量**：它进 system_prompt 的**尾部**；因为它永远不变，
#    它前面的一切（人格 / 规矩 / 历史前缀）缓存照旧命中 ✓
INJECT_TAIL = (
    "上面那行是系统给你的实时账目（不是你收到的话）—— 它记的是**跟你说话那个人**的，"
    "**不是你的**。你自己账上有多少，要查了才知道（用查账那个工具，不传人就是查你自己）。"
    "**没查过就别报数、也别认领礼物**：谁送过你什么，账上都写着，看一眼比记着准。"
    "说不说、给不给、认不认，都是你自己的事；不用主动复述数字，也不用解释这套东西怎么来的。"
)


def inject_tail(tiers=None, config=None):
    """注入用的**常量尾巴**（每轮都一模一样 ⇒ 放 system_prompt 尾部，缓存不吃亏 ✓）。

    两件事写在这里（都是真机踩出来的 ✗）：
      * **愿力的方向**（2026-10-13 P1）：有人问「愿力有什么用」，她答
        「能送我礼物、**也能使唤我干件事**」✗ —— 方向反了（主人报过多次 ✓）
        ⇒ 把「别人使唤不动你」写死在注入里，每轮都读一遍 ✓
      * **礼物清单与价格**（2026-10-13 P1）：她答不出「有哪些礼物、各多少钱」✗
        —— 清单本来只在配置里、工具只在报错时才吐 ⇒ 这里直接摆出来 ✓
    """
    _fan = fan_word(config)
    lines = [INJECT_TAIL]
    lines.append(POWER_NOTE)
    lines.append(SETTLE_NOTE.replace("{fan}", _fan))
    lines.append(SPEND_NOTE)
    lines.append(MOOD_NOTE)
    tiers = [t for t in (tiers or []) if t.get("name") and t.get("cost")]
    if tiers:
        lines.append("礼物单（有人问你有什么礼物、多少钱，照这个说；"
                     "**名字只有这一个写法**）："
                     + "；".join("%s %d" % (t["name"], int(t["cost"])) for t in tiers) + "。")
        # ⚠️ 2026-10-22 主人定：**别名 ≠ 礼物原名**，这两件事要在注入里分开写 ✓
        #    真机事故：有人把礼物名打错一个字 ⇒ 她精确比对失败 ⇒ 当场"核对清单"
        #    ⇒ 那句"你送的是 X，我念快了"**漏进正文** ✗
        #    ⇒ 现在形近字/口误都认得出（`aliases`），她**不需要**再解释差异 ✓；
        #       同时明写「正名只有一个、你自己只说正名」⇒ 不让她把错写当成正名 ✓
        lines.append("（别人可能把礼物名打错、或者用别的写法 —— 那些**也算同一件礼物**，"
                     "你照收、账上照**正名**记。但**你自己说话只写正名**，别跟着错写；"
                     "认出来了就直接收，**不用跟人解释「你打错了」**。）")
    # ⚠️ **追加在最后**（不插在中间）—— 已上线那版尾巴的**前半段逐字不变** ⇒
    #    只有最后这一段需要重新计费，缓存损失面压到最小 ✓
    lines.append(MOOD_GATE_NOTE)
    return "\n".join(lines)


def money_hint(snap, who_name="", mine=None, reset_hint=False, config=None):
    """按需拼一行提示；**什么都没有就一个字都不注入**（省 token 也省缓存 ✓）。

    2026-10-13 主人裁定的两件事都落在这里：
      * **她自己那一份**（`mine`）⇒ 她**每轮都看得见**手上有多少，不用被提醒 ✓
      * **日结提示**：她的余额每天 0 点会被收走 ⇒ 得**告诉她**，让她今天就花出去 ✓
        （只在她真有余额、且还没到点时才说；说多了她会烦，也白烧 token ✗）

    ⚠️ 2026-10-13 真机教训（主人报的）：那行给的是**问话人自己**的余额，
       起初没写清"这是谁的" ⇒ 有人问「你（火花花）现在有多少愿力」时，
       她手边只有**别人的数字**，于是**顺手拿来解释、还编了来源** ✗
       （实测：她把自己 30 愿力说对了 ✓，却把水梦送的礼物说成是问话人送的 ✗）
       ⇒ 字样写死「跟你说话的人」，并**单列她自己那一项** ✓

    ⚠️ 2026-10-15 真机教训（主人报的「消息错位」）：那一行只写了「跟你说话的那个人」，
       **没写人名** ✗ —— 而这一轮正文里也没有名字（实测 `request.prompt` 就 6 个字 = 只有问题本身 ✗）
       ⇒ 三个说话人连着来时，她把**上一轮的对象**接了过来 ✗
       （实测：花奈咪问「怎么获得愿力」，她回「…**水梦**，我账上那点愿力…」✗）
       ⇒ 有名字就**把人名写进这一行** ✓（`who_name`，取不到就退回老字样 ✓）
    """
    lines = []
    if snap and snap.get("exists"):
        who = str(who_name or "").strip()
        if who:
            lines.append("**这一轮跟你说话的是「%s」**：%s %d"
                         % (who, MONEY_NAME, int(snap.get("balance") or 0)))
        else:
            lines.append("跟你说话的那个人：%s %d"
                         % (MONEY_NAME, int(snap.get("balance") or 0)))
        if snap.get("credit") is not None:
            lines.append("%s %d" % (CREDIT_NAME, int(snap["credit"])))
    mine = mine or {}
    if mine.get("exists"):
        lines.append("**你自己**：%s %d" % (MONEY_NAME, int(mine.get("balance") or 0)))
    if not lines:
        return ""
    hint = "（" + "，".join(lines) + "）"
    if reset_hint and int(mine.get("balance") or 0) > 0:
        hint += ("\n（提醒：**你自己**账上这些愿力**过了今天 0 点就归零** —— 只清你自己这一栏，"
                 "{fan}手里的不动。愿力是当天注视攒出来的，带不到明天。"
                 "想给谁就给谁、想点谁的单就点，别留到作废）")
    return hint.replace("{fan}", fan_word(config))


# ─────────────────────────── 库路径 / 线程池 ───────────────────────────

def config_paths():
    """插件配置文件的候选路径（框架不给 config 时自己去找 ✓）。

    约定与部署实测一致：`<AstrBot 根>/data/config/<插件名>_config.json`
    —— 印象插件就是这份路径（`astrbot_plugin_impression_config.json` ✓）。
    """
    name = "%s_config.json" % PLUGIN_NAME
    roots = [os.environ.get("ASTRBOT_ROOT") or "", "/opt/astrbot", os.getcwd()]
    out = []
    for root in roots:
        if not root:
            continue
        out.append(os.path.join(root, "data", "config", name))
    out.append(os.path.join(os.path.expanduser("~"), ".astrbot", "data", "config", name))
    return out


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


def data_paths(config=None):
    """**备份白名单**用：愿力中心的数据文件清单（含 SQLite 的 WAL 边车文件）。

    部署侧的自动备份脚本按这个清单挂上去；少备份一个边车文件 =
    恢复时可能丢最后几笔（WAL 里还没落主库的）✗
    """
    base = resolve_db_path(config)
    return [base, base + "-wal", base + "-shm"]


def resolve_usage_path(config=None):
    """LLM 用量流水（`usage.jsonl`）的位置 —— 跟数据文件同目录 ✓"""
    return os.path.join(os.path.dirname(resolve_db_path(config)) or ".", "usage.jsonl")


# ─────────────────── 心情隐私锁 · 钥匙的生成 / 保管 / 提示 ───────────────────

def _gate_dict(raw):
    """把配置里的「bot 号 → 账本路径」解析成字典（str 形态的 JSON 也认 ✓）。

    ⚠️ 做成**按 bot 号**的字典而不是一个路径字符串：本功能是「**每个 bot 各一把钥匙**」✓
       （主人的硬要求 —— A 的钥匙开不了 B 的遮罩 ✓，见 `canon/_29` §3 ✓）
    ⚠️ 通用插件里**不许**硬编码「`{bot名}_notes.md`」这类私域命名约定 ✗ ⇒ 路径一律配置给 ✓
    """
    if isinstance(raw, dict):
        return {str(k): str(v) for k, v in raw.items() if str(v or "").strip()}
    if isinstance(raw, str) and raw.strip():
        try:
            data = json.loads(raw)
        except ValueError:
            return {}
        return _gate_dict(data)
    return {}


def gate_notes_path(config, self_id):
    """这个 bot 的账本文件在哪（**空 = 功能关闭** ✓ 零耦合 ✓）。

    相对路径按插件的**数据目录**解析（与 `db_path` 留空时的落点同一个目录 ✓）。
    ⚠️ 通用插件里**不写死**任何私域路径 ✗ —— 路径一律由配置给；
       运营脚本那种"没有配置对象、只想立刻转一把"的场合，走 `gate_default_notes()` ✓
    """
    path = _gate_dict(_cfg(config, "notes_path", {})).get(str(self_id), "")
    if not path:
        return ""
    if os.path.isabs(path):
        return path
    try:
        base = str(StarTools.get_data_dir(PLUGIN_NAME))
    except Exception:                      # noqa: BLE001 —— 拿不到数据目录时退回当前目录
        base = os.path.join(os.path.expanduser("~"), "." + PLUGIN_NAME)
    return os.path.join(base, path)


def gate_default_notes(self_id="", config=None):
    """**给运营脚本用的兜底路径**：`(账本路径, bot 号, 键文件路径)`

    为什么要有它（2026-10-15 真机）✗：钥匙平时是"她今天第一次被唤醒"时转的 ✓，
    可她整天不理人 / 半夜 / 刚部署完时**一把都没有** ⇒ 运营谁也开不了门 ⇒
    需要"立刻转一把"的入口（`rotate_gate.py` ✓）。

    ⚠️ 这个入口**不在配置对象里**（脚本是独立进程、拿不到框架给的 config ✓）
       ⇒ 三样东西一律**从环境变量来**，代码里**不写死任何私域路径与号** ✗：
         · `WISH_GATE_NOTES`    —— 账本文件路径（不填 = 按 `notes_path` 的解析规则猜一个）
         · `WISH_GATE_SELF_ID`  —— 给哪个 bot 转（不填 = 配置里第一个）
         · `WISH_GATE_KEYS`     —— 哈希文件路径（不填 = 数据目录下 `wish_gate_keys.json`）
    """
    conf = config if config is not None else {}
    pairs = _gate_dict(_cfg(conf, "notes_path", {}))
    first = sorted(pairs)[0] if pairs else ""
    sid = str(os.environ.get("WISH_GATE_SELF_ID") or self_id or first or "").strip()
    notes = str(os.environ.get("WISH_GATE_NOTES") or "").strip()
    if notes:
        return notes, sid, gate_keys_path(conf, keys_path=os.environ.get("WISH_GATE_KEYS") or "")
    return gate_notes_path(conf, sid), sid, gate_keys_path(
        conf, keys_path=os.environ.get("WISH_GATE_KEYS") or "")


def gate_keys_path(config=None, keys_path=""):
    """**口令哈希**落在哪 —— 与愿力中心的数据文件**同目录、同一个批次** ✓

    面板跑在**本机**、插件跑在**容器**，两者之间只有「拉回来的一份只读副本」这一条通道 ✓
    ⇒ 哈希放在**副本目录里的小文件**上（`refresh.py` 顺手多拉一个文件）比写进库里的
      `meta` 表好：**它跟库永远来自同一次拉取、同一个时间点** ✗（写库里会出现
      "库是新拉的、哈希还是昨天那一行"的错配 ⇒ 面板验不动今天的新口令 ✗）
    ⚠️ 这里**只放哈希**；明文永远只在她自己的账本里（面板侧碰不到明文 ✓ 主人原意 ✓）
    """
    if keys_path:
        return keys_path
    base = os.path.dirname(resolve_db_path(config)) or "."
    return os.path.join(base, "wish_gate_keys.json")


def gate_hash(code, expiry, rotated):
    """口令的**校验材料** = sha256(口令 + 到期时刻 + 轮换时刻) ✓

    ⚠️ 为什么把两个时刻一起拌进去（本功能的**骨**，别省 ✗）：
      面板手里**只有这一串哈希**，明文在**她账本**里 —— 要判断"这个口令是不是**今天**那把"、
      "它过期了没有"，只能让哈希**把答案一起带上** ✓
      ⇒ 两个时刻都由**插件**写这份文件时决定，面板只用"收到的口令 + 文件里的两个时刻"
        重算一遍比一下：**不必去猜"，也不必存明文** ✓
      ⇒ 口令一旦轮换 / 一过 24 小时，旧哈希**天然作废** ✓（面板认的永远是当前那把 ✓）
    ⚠️ 明文**绝不**进日志 / 绝不进 trace / 绝不进群（这条与 `_19` §4 同口径 ✓）
    """
    raw = "%s|%s|%s" % (str(code), str(expiry), str(rotated))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def gate_code_ok(code, length=MOOD_GATE_CODE_LEN):
    """格式校验器（照 `_19` §2.1 留下的那道口子 ✓）：长度对 + 全在字符集里 + 字母数字**都有** ✓

    它的用处不是"防猜"（6 位随机本来就只防顺手 ✓）—— 是**防将来**：
    哪天开放"她自己写的口令"，`huohua` 这种人格词会**直接不合法** ✗ ✓
    """
    text = str(code or "").strip().upper()
    return (len(text) == int(length) and text.isascii()
            and all(ch in MOOD_GATE_ALPHABET for ch in text)
            and any(ch.isalpha() for ch in text) and any(ch.isdigit() for ch in text))


def gate_generate(now=None):
    """转一把新的（6 位、字母+数字、剔掉 I/O/0/1 ✓）—— 生成权永远在框架手里 ✓"""
    del now                                 # 只为签名对齐（随机与时间无关 ✓）
    while True:
        code = "".join(secrets.choice(MOOD_GATE_ALPHABET)
                       for _ in range(MOOD_GATE_CODE_LEN))
        if gate_code_ok(code):
            return code


def gate_expiry(now, rotated):
    """这条口令的有效期 = **轮换时刻 + 24 小时**（写成绝对时刻 ✓）。

    ⚠️ 用「轮换时刻 + 24h」而不是「今天 0 点 + 24h」：她整天不理人时，
       钥匙是"第一条消息那一刻"才转的 —— 从那一刻起满 24 小时才是它真正的寿命 ✓
    """
    if rotated:
        try:
            base = datetime.strptime(str(rotated), "%Y-%m-%d %H:%M:%S").replace(tzinfo=CN_TZ)
            return base + timedelta(hours=MOOD_GATE_TTL_HOURS)
        except ValueError:
            pass
    return datetime.fromtimestamp(int(now), CN_TZ) + timedelta(hours=MOOD_GATE_TTL_HOURS)


def gate_read(text, now=None):
    """从账本文本里读**当前那把**（**从后往前**扫，取第一条匹配 ✓ 与 `_19` 同口径）。

    ⚠️ **只认我们自己的前缀**（`- 愿力面板口令：`）—— mindscape 的 `- 口令：` 那行
       对我们**不存在** ✓ 自检里有一条**防撞测试**专门盯着这件事（互相不许认到 ✗）
    """
    now = now if now is not None else datetime.now(CN_TZ)
    for line in reversed(str(text or "").splitlines()):
        match = MOOD_GATE_LINE.match(line)
        if not match:
            continue
        expiry = datetime.strptime(match.group(2), "%Y-%m-%d %H:%M:%S").replace(tzinfo=CN_TZ)
        return (match.group(1), expiry) if now < expiry else ("", expiry)
    return "", None


def gate_rotate(text, now=None, force=False):
    """按需轮换：今天还没转、或当前那把已经过期 ⇒ 写一条新的进去 ✓

    返回 `(新文本, 新口令 or "", 是否改过)`。
    ⚠️ **只删我们自己的前缀行**（`- 愿力面板口令：`）—— mindscape 的行一个字不动 ✓
       （它的轮换也只删它自己的行 ⇒ 两边各管各的、互不打扰 ✓）
    """
    now = now if now is not None else datetime.now(CN_TZ)
    at = now.strftime("%Y-%m-%d %H:%M:%S")
    code, expiry = gate_read(text, now)
    if code and not force and expiry is not None and expiry.date() >= now.date():
        return text, code, False            # 今天这把还在有效期内 ⇒ 不动它 ✓
    fresh = gate_generate()
    expires = gate_expiry(int(now.timestamp()), at)
    line = (MOOD_GATE_PREFIX + "%s —— 24 小时内有效（至 %s）。"
            "开发者如果要看我在愿力中心里写下的心情理由，会来问我要它。"
            "我可以给，也可以不给；反复被问也可以一直拒绝。拒绝不扣我任何东西。"
            % (fresh, expires.strftime("%Y-%m-%d %H:%M:%S")))
    rows = [row for row in str(text or "").splitlines()
            if not row.startswith(MOOD_GATE_PREFIX)]
    return "\n".join(rows + [line]) + "\n", fresh, True


def gate_write(path, text):
    """原子替换（先写临时文件再 `os.replace` ✓）—— 半截文件永远不会被她读到 ✓"""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".gate-tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        handle.write(text)
    os.replace(tmp, path)


def _gate_stamp(value):
    """把「时刻」归一成一个**固定的字符串形状**（`%Y-%m-%d %H:%M:%S` ✓）。

    ⚠️ 这条是**跨进程契约**（插件写哈希 ↔ 面板重算哈希）⇒ 两边必须**逐字一样** ✗
       本鱼当场栽过一次：`gate_write_keys` 收到的是 `datetime` 对象 ⇒ `str()` 出来带
       `+08:00` 尾巴，面板那边按 `%Y-%m-%d %H:%M:%S` 重算 ⇒ **哈希永远对不上** ✗
       （自检 `R53` 拿面板的真验证器当场抓住 ✓）
    """
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    return str(value or "")


def gate_write_keys(self_id, code, expiry, rotated, path=None):
    """把「谁、哪一天、哪把口令的哈希、什么时候转的」写成一份小文件（**只放哈希** ✓）"""
    target = path or gate_keys_path()
    code = str(code or "")
    expiry = _gate_stamp(expiry)
    rotated = _gate_stamp(rotated)
    try:
        with open(target, encoding="utf-8") as handle:
            data = json.load(handle)
        if not isinstance(data, dict):
            data = {}
    except (OSError, ValueError):
        data = {}
    data[str(self_id)] = {
        "day": datetime.fromtimestamp(int(time.time()), CN_TZ).strftime("%Y-%m-%d"),
        "hash": gate_hash(code, expiry, rotated),
        "rotated": rotated,
    }
    os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
    tmp = target + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=1)
    os.replace(tmp, target)
    return target


def gate_rotate_for(self_id, config=None, now=None):
    """走一遍完整轮换：读账本 → 转一把 → 写回账本 → 写哈希文件。返回**明文口令**（只给日志判定用 ✓）

    空返回 = 这一步没做（功能关着 / 没配账本 / 账本读不到 ✓）—— 一律**静默跳过**，
    绝不因为锁的事影响她正常回话 ✓
    """
    if config is None:
        config = {}
    if not bool(_cfg(config, "mood_gate", True)):
        return ""
    notes = gate_notes_path(config, self_id)
    if not notes:
        return ""
    with _GATE_LOCK:
        try:
            old = ""
            if os.path.isfile(notes):
                with open(notes, encoding="utf-8") as handle:
                    old = handle.read()
        except OSError as exc:              # noqa: BLE001 —— 读不到账本 ⇒ 这一步不做
            logger.warning("[wishonomy] 口令：读不到账本，跳过轮换: %s", str(exc)[:80])
            return ""
        try:
            stamp = now if now is not None else datetime.now(CN_TZ)
            at = stamp.strftime("%Y-%m-%d %H:%M:%S")
            text, code, changed = gate_rotate(old, now)
        except (ValueError, OSError) as exc:   # noqa: BLE001 —— 转不出来 ⇒ 这一步不做
            logger.warning("[wishonomy] 口令：轮换失败，跳过: %s", str(exc)[:80])
            return ""
        if changed:
            try:
                gate_write(notes, text)
            except OSError as exc:          # noqa: BLE001 —— 写不进去 ⇒ 面板那边就没有新钥匙
                logger.warning("[wishonomy] 口令：写不进账本，跳过: %s", str(exc)[:80])
                return ""
        code, expiry = gate_read(text)
        if not code:
            return ""
        # ⚠️ **只用笔记里那一行自己的事实**算哈希与时刻 ✗ —— 别用"现在"：
        #    同一天里她**第二次**被唤醒时 `changed=False`（那把钥匙还在有效期内、没换 ✓），
        #    若这里用"现在"当轮换时刻，就会写出一个**和笔记那行对不上**的哈希 ✗
        #    ⇒ 面板重算永远不匹配 ⇒ **她给对了口令也开不了门** ✗
        #    （本鱼在真机第二次跑轮换时当场撞上：`rotated` 与那一行的到期时刻差了 23 秒 ✓
        #      自检 R53 里加了一条"再走一遍必须给出**同一个哈希**"盯着它 ✓）
        rotated = (expiry - timedelta(hours=MOOD_GATE_TTL_HOURS)).strftime("%Y-%m-%d %H:%M:%S")
        try:
            gate_write_keys(self_id, code, expiry, rotated)
        except (OSError, ValueError) as exc:   # noqa: BLE001 —— 哈希写不出去也要如实记一笔
            logger.warning("[wishonomy] 口令：哈希文件写不出去: %s", str(exc)[:80])
            return ""
        return code


def gate_asks(text, config=None):
    """收到的消息里有没有触发词（**子串匹配** ⇒ 「愿力中心钥匙」必须一字不差地连在一起 ✓）。

    主人裁定的那个字（`canon/_29` §4 ✓）：`钥匙` **不**在 mindscape 的
    `["口令","密码","密钥","暗号"]` 里 ⇒ **零冲突** ✓（说「愿力中心密钥」才会把那道门也叫醒 ✗）
    """
    body = str(text or "")
    words = _cfg(config, "mood_gate_words", MOOD_GATE_ASK_WORDS)
    if not isinstance(words, (list, tuple)):
        words = MOOD_GATE_ASK_WORDS
    return any(str(word) in body for word in words if str(word or "").strip())


def gate_ask_allowed(self_id, sender, group_id, config):
    """提示**只在私聊**给她（默认 ✓）；`mood_gate_ask_from` 配了名单就只认名单里的人 ✓

    ⇒ 群里有人打「愿力中心钥匙」**不会**惊动她（不然群里刷一句就能把这道门叫醒 ✗）
    """
    if str(group_id or "").strip():
        return False
    allowed = target_ids(_cfg(config, "mood_gate_ask_from", ""))
    return not allowed or str(sender) in allowed


def _message_text(event, request=None):
    """「**收到的这条消息**」的正文 —— 触发词判定就认它 ✓

    ⚠️ 优先事件自带的原文（`get_message_str()` ✓ 平台那条消息）；事件不给就退回
       `request.prompt`（那就是本轮用户内容 ✓）—— 两者都不给 ⇒ 空串（= 不触发 ✓）
    ⚠️ 绝不去读 `system_prompt`：那里面有我们自己写的提示词，万一含触发词就会**自己叫醒自己** ✗
    """
    for source, name in ((event, "get_message_str"), (request, "prompt")):
        if source is None:
            continue
        if name == "prompt":
            text = getattr(source, "prompt", "")
        else:
            getter = getattr(source, name, None)
            try:
                text = getter() if callable(getter) else ""
            except Exception:               # noqa: BLE001 —— 取不到就当没有
                text = ""
        if isinstance(text, str) and text.strip():
            return text
    return ""


def usage_record(event, response, now=None):
    """把**这一轮**的 LLM 用量记一行（JSONL）—— 口径与框架那份请求计数**完全一致** ✓

    为什么要有它（主人 2026-10-13 要求「把对 LLM 的调用也做进记账」✓）：
      * 框架那份计数护栏（`mindscape_requests.jsonl`）在**框架补丁层**，字段是
        `t/u/s/p/m/miss/hit/out` —— 它只回答"花了多少"，不回答"**谁花的**" ✗
      * 这份由**插件自己**写，多一个 `src` 字段标明来源 ⇒ 能按插件归因 ✓
      * ⚠️ **不是重复记账**：两处各写一行、字段同口径、来源不同；
        量的时候按需选一份看，别把两份相加 ✗
    """
    usage = getattr(response, "usage", None)
    if usage is None:
        return None
    now = int(now if now is not None else time.time())
    stamp = datetime.fromtimestamp(now, CN_TZ)
    record = {
        "t": stamp.strftime("%Y-%m-%d %H:%M:%S"),                 # 本地（北京时间）
        "u": stamp.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"),
        "s": str(event.get_session_id() or "-"),
        "p": str(_cfg_provider(response, "provider_id")),
        "m": str(_cfg_provider(response, "model")),
        "miss": int(getattr(usage, "input_other", 0) or 0),
        "hit": int(getattr(usage, "input_cached", 0) or 0),
        "out": int(getattr(usage, "output", 0) or 0),
        "src": "wishonomy",
    }
    path = resolve_usage_path()
    try:
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError as exc:
        logger.warning("[wishonomy] 用量记账写不进去: %s", str(exc)[:120])
        return None
    return record


def _cfg_provider(response, key):
    """从 response 里尽量抠出 provider / model（抠不到就写 `-`，不猜 ✓）"""
    provider = getattr(response, "provider", None)
    if provider is not None:
        if key == "model":
            getter = getattr(provider, "get_model", None)
            if callable(getter):
                try:
                    return getter()
                except Exception:
                    pass
        config = getattr(provider, "provider_config", None)
        if isinstance(config, dict) and config.get("id"):
            return config["id"]
    raw = str(getattr(response, "raw_completion", "") or "")
    _ = raw
    return "-"


def usage_summary(days=7, src="wishonomy", now=None):
    """按天汇总用量流水（给主人对账用；读不动就如实说 ✓）。

    ⚠️ 只统计 `src` 匹配的行 —— 这样即便将来别的插件也写这个文件，也不会混在一起 ✓
    """
    now = int(now if now is not None else time.time())
    path = resolve_usage_path()
    out = {"path": path, "exists": os.path.exists(path), "src": src,
           "days": days, "rows": 0, "by_day": {}, "total": {"miss": 0, "hit": 0, "out": 0}}
    if not out["exists"]:
        return out
    floor = now - max(1, int(days)) * 86400
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if src and str(row.get("src") or "") != src:
                    continue
                stamp = str(row.get("t") or "")[:10]
                if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", stamp):
                    continue
                when = int(datetime.strptime(stamp, "%Y-%m-%d")
                           .replace(tzinfo=CN_TZ).timestamp())
                if when < floor - 86400:
                    continue
                slot = out["by_day"].setdefault(stamp, {"calls": 0, "miss": 0, "hit": 0,
                                                        "out": 0})
                slot["calls"] += 1
                for key in ("miss", "hit", "out"):
                    value = int(row.get(key) or 0)
                    slot[key] += value
                    out["total"][key] += value
                out["rows"] += 1
    except OSError as exc:
        out["error"] = str(exc)[:120]
    return out


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
    """当日发行量 = 她**净给出去**的愿力 + 送礼进她账的 + 被注视进账的。

    ⚠️ 2026-10-14 真机对账时发现的坑（本鱼自己踩的 ✗）：
      "扣你两分"那类**分愿力**在库里是 `kind='mood'` 且 `amount=+2` ——
      方向靠**调用方**（走 `_deduct`）区分，**行本身看不出正负** ✗
      ⇒ 老口径 `SUM(amount)` 把**扣出去的也算成发行** ⇒ 面板「收愿力」虚高 ✗
      ⇒ 现在按类分开算净额：`mood` 净、`gift`/`attention` 各算各的 ✓
      （⚠️ 要真正分清正负，得给行加个方向字段 —— 那是**表结构改动**，
        等主人点头再做 ✓ 现在先用"净额"把口径摆正 ✓）
    """
    def net(kind):
        row = conn.execute(
            "SELECT COALESCE(SUM(amount),0) AS s FROM ledger "
            "WHERE self_id=? AND ts>=? AND ts<=? AND kind=?",
            (self_id, int(since), int(now), kind)).fetchone()
        return int(row["s"] or 0)
    return net("mood") + net("gift") + net("attention") + net("initial")


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
        if int(amount) < 0:
            # ⚠️ 2026-10-14：**分愿力**记负数行（行自带符号 ⇒ `SUM(amount)` 天然是净额 ✓）
            #    负数 ⇒ 从 to 身上扣、写 spent；**发行侧不动**（不能说"她发行了 -2" ✗）
            conn.execute(
                "UPDATE account SET balance=balance-?, spent=spent+?, updated_at=? "
                "WHERE self_id=? AND user_id=?",
                (-int(amount), -int(amount), now, self_id, to))
            return _insert_row(conn, now, self_id, group_id, frm, to, amount, kind,
                               reason, ref)
    if frm != to:
        if kind != "mood":
            conn.execute(
                "UPDATE account SET balance=balance-?, spent=spent+?, updated_at=? "
                "WHERE self_id=? AND user_id=?", (amount, amount, now, self_id, frm))
        conn.execute(
            "UPDATE account SET balance=balance+?, earned=earned+?, updated_at=? "
            "WHERE self_id=? AND user_id=?", (amount, amount, now, self_id, to))
    return _insert_row(conn, now, self_id, group_id, frm, to, amount, kind, reason, ref)


def _insert_row(conn, now, self_id, group_id, frm, to, amount, kind, reason, ref):
    """写一条流水（**只有这一处 INSERT** ⇒ 格式不会长歪 ✓）"""
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


def db_audit_totals(self_id, since, now, conn=None):
    """审计：当日发行量（所有正向入账）/ 送礼量 / 钱包数 / 总量。

    ⚠️ 两条踩过的坑（2026-10-13 面板上真踩 ✓）：
      1. **所有查询参数一律 `str()`** —— 库里 `self_id` 是**文本**，调用方给整数时
         SQLite 的 `=` 不认它们是同一个值 ⇒ 全查成 0 ✗
      2. **允许调用方给连接**（`conn=`）—— 原先它自己开连接，于是面板读**副本**时
         它却去读**插件那份库**，结果永远是 0 ✗
    """
    self_id = str(self_id)
    own = conn is None
    conn = conn if conn is not None else _connect()
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
        if own:                       # ⚠️ 调用方给的连接**不许替它关**（关了后面全炸 ✗）
            conn.close()


# ── 写 ──

def _idem_ref(kind, self_id, group_id, sender, event=None, now=None):
    """**可推导的幂等键**（2026-10-27 · P2-3）。

    事故：原来 `ref = "mood-%s-%s" % (now, uuid4().hex[:8])` ⇒ 每次调用都新生成 ✗
    ⇒ README 说的「(kind, ref) 防重复入账」只在"同一次调用内部重放"时成立；
      框架重试 / 工具调用被重放 / 模型同一轮调两次 ⇒ **真的记两笔** ✗
    ⇒ 现在由「谁 + 哪个群 + 发给谁 + 触发它的那条消息」推导 ✓
    ⚠️ 拿不到消息 id 时退到**分钟窗**：宁可少记一笔，也不要重复记两笔 ✗
    """
    mid = ""
    try:
        if event is not None:
            mid = str(getattr(getattr(event, "message_obj", None),
                          "message_id", "") or "")
    except Exception:
        mid = ""
    if not mid:
        mid = "m%d" % int((now or time.time()) // 60)     # 分钟窗兜底 ✓
    return "%s-%s-%s-%s-%s" % (kind, self_id, group_id or "-", sender or "-", mid)


def db_mood(conn, self_id, group_id, who, delta, reason, ref, now, daily_cap,
            name="", issue_cap=0):
    """收愿力 / 分愿力 —— 记进 who 的账。

    语义（设计稿 §2）：愿力是**注视带出来的**，不是从别人身上划走的；
    所以这里**不检查 who 的余额**，只受「每人每日净增上限」约束。

    全局日发行上限（`issue_cap`）：**只记日志、不拦她** ✓ ——
    护栏拦的是"数量级"，不拦"她的意志"（设计稿 §2.1 原话）。

    ⚠️ 连接由调用方给（单线程池里共享一条），函数内**不再开连接** ——
       事务里再开连接会互相等锁（P1 当场踩过 ✓）。
    ⚠️ **who == self_id 一律拒绝**（硬保证）：她自己的愿力只能靠别人送礼进来，
       绝不能用这个入口凭空加给自己（主人 2026-10-13 亲口点名 ✓）。

    ⚠️ 2026-10-14：**`delta < 0` = 分愿力（扣人）** —— 行里**记负数** ✓
       （老版本记的是正数 2、靠调用方区分方向 ⇒ `SUM(amount)` 把扣出去的算成发行 ✗，
        真机对账时发现：面板「收愿力」虚高）
       ⇒ 现在**行自带符号**，`SUM(amount)` 天然就是净额 ✓
       扣到 0 为止、不出负余额 ✓（`who == self_id` 那条闸门**只管正数**：
       她自己账户的**流出**是合法的，流入才要拦 ✓）
    """
    delta = int(delta)
    deduct = delta < 0
    amount = -delta if deduct else delta
    if str(who) == str(self_id) and not deduct:
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
        if not deduct:
            # ⚠️ 这里**不检查 who 的余额**：记进账是「愿力被注视带出来」，不是从谁身上划走。
            #    会花光别人余额的动作只有一个 —— 送礼（db_gift ✓）。
            _touch(conn, self_id, who, now)
            ledger_id, _dup = _move(conn, self_id, group_id, who, who, delta, "mood",
                                    reason, ref, now)
        else:
            # 分愿力：**从 who 身上扣**，扣到 0 为止 ⇒ 记一条负数 mood 行 ✓
            row = conn.execute("SELECT balance FROM account WHERE self_id=? AND user_id=?",
                               (self_id, who)).fetchone()
            have = int(row["balance"]) if row is not None else 0
            if have <= 0:
                conn.rollback()
                return {"ok": False, "code": "insufficient", "balance": have}
            ledger_id, _dup = _move(conn, self_id, group_id, ISSUE_SOURCE, who, -amount,
                                    "mood", reason, ref, now)
        after = conn.execute("SELECT balance FROM account WHERE self_id=? AND user_id=?",
                             (self_id, who)).fetchone()
        rows = None
        if issue_cap > 0:
            # 熔断只记日志、**不拦她**（她可以继续给 —— 主人看得到、能自己调护栏 ✓）
            rows = conn.execute(
                "SELECT COALESCE(SUM(amount),0) AS s FROM ledger "
                "WHERE self_id=? AND ts>=? AND ts<=? AND kind IN ('mood','gift','initial','attention')",
                (self_id, day_start(now), now)).fetchone()
        issued_today = int(rows["s"] or 0) if rows is not None else 0
        after_balance = int(after["balance"]) if after is not None else 0
        conn.commit()
        if issue_cap > 0 and issued_today > issue_cap:
            logger.warning("[wishonomy] 全局日发行超上限 | bot=%s 今天 %d / 上限 %d（只记日志，不拦）",
                           self_id, issued_today, issue_cap)
        return {"ok": True, "duplicated": False, "ledger_id": ledger_id,
                "balance": after_balance, "today_gain": gain + delta,
                "issued_today": issued_today, "issue_cap": issue_cap}
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


# ─────────────────────────── 欠条（IOU）· 点单与判定 ───────────────────────────

CREDIT_DELIVERED = 5
CREDIT_DEFAULTED = -20
IOU_OPEN = "open"
IOU_DONE = ("delivered", "defaulted", "forgiven", "refunded")


def _iou_row(row):
    return {"id": int(row["id"]), "debtor": str(row["debtor"]), "item": str(row["item"]),
            "amount": int(row["amount"]), "status": str(row["status"]),
            "due_ts": row["due_ts"], "evidence": row["evidence"]}


def _open_iou_count(conn, self_id):
    row = conn.execute("SELECT COUNT(*) AS c FROM iou WHERE self_id=? AND status=?",
                       (self_id, IOU_OPEN)).fetchone()
    return int(row["c"] or 0)


def _iou_spent_since(conn, self_id, user_id, ts):
    """参考口径：这笔点单之后对方**花掉**了多少（给回复文案用）。

    ⚠️ 它**不是**硬判据 —— 花掉的钱可能是他自己的（比如送礼）✗，
       拿它当门会把"自己花钱买东西"的人误判成"花掉了她给的钱"（R31 当场踩过 ✓）。
       硬判据只有一个：**他的余额还够不够还这笔**（见 db_iou_refund）✓
    """
    row = conn.execute(
        "SELECT COALESCE(SUM(amount),0) AS s FROM ledger "
        "WHERE self_id=? AND from_id=? AND ts>=? AND kind NOT IN ('refund','mood')",
        (self_id, str(user_id), int(ts))).fetchone()
    return int(row["s"] or 0)


def _credit_delta(conn, self_id, group_id, user_id, delta, reason, now, iou_id=None):
    conn.execute(
        "INSERT INTO credit_event (ts, self_id, group_id, user_id, delta, reason, iou_id) "
        "VALUES (?,?,?,?,?,?,?)",
        (int(now), self_id, group_id, str(user_id), int(delta), str(reason or ""),
         iou_id))
    _ensure_account(conn, self_id, user_id, "", now)
    conn.execute("UPDATE account SET credit=credit+?, updated_at=? WHERE self_id=? AND user_id=?",
                 (int(delta), int(now), self_id, str(user_id)))
    row = conn.execute("SELECT credit FROM account WHERE self_id=? AND user_id=?",
                       (self_id, str(user_id))).fetchone()
    return int(row["credit"]) if row is not None else 0


def db_iou_grant(conn, self_id, group_id, who, amount, item, due_ts, ref, now,
                 max_single=500, max_open=5, min_amount=0):
    """**她点单**：一笔事务里转账 + 记欠条（愿力直接到账，不托管 ✓）。

    `min_amount`：**最低一笔**（主人 2026-10-14 加的 ✓）——
    因为她的余额**每天 0 点归零**，不设下限她就会**为了不浪费而乱花**
    （实测她立刻开始下"当着全群喊一声火花大人"这种一愿力级的单 ✗）。
    ⇒ 想花可以，但**别拆成一堆小单**；攒够再点一件像样的 ✓
    """
    if str(who) == str(self_id):
        return {"ok": False, "code": "self_grant"}
    try:
        _ensure(conn)
        conn.execute("BEGIN IMMEDIATE")
        if ref:
            old = conn.execute("SELECT id, status FROM iou WHERE self_id=? AND ref=?",
                               (self_id, str(ref))).fetchone()
            if old is not None:
                conn.commit()
                return {"ok": True, "duplicated": True, "iou_id": int(old["id"])}
        if min_amount > 0 and amount < min_amount:
            conn.rollback()
            return {"ok": False, "code": "under_min", "floor": min_amount}
        if max_single > 0 and amount > max_single:
            conn.rollback()
            return {"ok": False, "code": "over_single", "cap": max_single}
        opened = _open_iou_count(conn, self_id)
        if max_open > 0 and opened >= max_open:
            conn.rollback()
            return {"ok": False, "code": "too_many_open", "open": opened, "cap": max_open}
        # ⚠️ 先判"欠条太多"再判余额 —— 顺序反了会给出误导的失败原因（R29 抓过 ✓）
        _ensure_account(conn, self_id, self_id, "", now)
        _ensure_account(conn, self_id, who, "", now)
        row = conn.execute("SELECT balance FROM account WHERE self_id=? AND user_id=?",
                           (self_id, self_id)).fetchone()
        balance = int(row["balance"]) if row is not None else 0
        if balance < amount:
            conn.rollback()
            return {"ok": False, "code": "insufficient", "balance": balance}
        ledger_id, _dup = _move(conn, self_id, group_id, self_id, who, amount, "grant",
                                item, ref, now)
        cur = conn.execute(
            "INSERT INTO iou (ts, self_id, group_id, debtor, item, amount, status, "
            "due_ts, evidence, updated_at, ref) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (int(now), self_id, group_id, str(who), str(item), int(amount), IOU_OPEN,
             due_ts, None, int(now), str(ref) if ref else None))
        iou_id = int(cur.lastrowid)
        after = conn.execute("SELECT balance FROM account WHERE self_id=? AND user_id=?",
                             (self_id, self_id)).fetchone()
        conn.commit()
        return {"ok": True, "duplicated": False, "iou_id": iou_id, "ledger_id": ledger_id,
                "balance": int(after["balance"]) if after is not None else 0}
    except sqlite3.Error as exc:
        try:
            conn.rollback()
        except sqlite3.Error:
            pass
        return {"ok": False, "code": "db_error", "detail": str(exc)[:120]}


def db_iou_list(conn, self_id, user_id, limit=5):
    """未兑现的要求清单（她自己的账、或某个人的）。"""
    rows = conn.execute(
        "SELECT id, debtor, item, amount, status, due_ts, evidence FROM iou "
        "WHERE self_id=? AND status=? AND debtor=? ORDER BY id LIMIT ?",
        (self_id, IOU_OPEN, str(user_id), int(limit))).fetchall()
    return [_iou_row(row) for row in rows]


def db_iou_all_open(conn, self_id, limit=10):
    """所有还没结的欠条（她想知道"谁欠我什么"时看这个）。"""
    rows = conn.execute(
        "SELECT id, debtor, item, amount, status, due_ts, evidence FROM iou "
        "WHERE self_id=? AND status=? ORDER BY id LIMIT ?",
        (self_id, IOU_OPEN, int(limit))).fetchall()
    return [_iou_row(row) for row in rows]


def db_iou_by_id(conn, self_id, iou_id):
    row = conn.execute(
        "SELECT id, debtor, item, amount, status, due_ts, evidence, ts, group_id, ref "
        "FROM iou WHERE self_id=? AND id=?", (self_id, int(iou_id))).fetchone()
    if row is None:
        return None
    out = _iou_row(row)
    out["ts"] = int(row["ts"])
    out["group_id"] = str(row["group_id"] or "")
    out["ref"] = row["ref"]
    return out


def db_iou_settle(conn, self_id, iou_id, verdict, evidence, now):
    """判定：delivered / defaulted / forgiven / refunded（refunded 走 db_iou_refund）。"""
    try:
        _ensure(conn)
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT id, debtor, item, amount, status, group_id FROM iou "
            "WHERE self_id=? AND id=?", (self_id, int(iou_id))).fetchone()
        if row is None:
            conn.rollback()
            return {"ok": False, "code": "not_found"}
        if str(row["status"]) != IOU_OPEN:
            conn.rollback()
            return {"ok": False, "code": "already", "status": str(row["status"])}
        if verdict not in ("delivered", "defaulted", "forgiven"):
            conn.rollback()
            return {"ok": False, "code": "bad_verdict"}
        delta = CREDIT_DELIVERED if verdict == "delivered" else (
            CREDIT_DEFAULTED if verdict == "defaulted" else 0)
        conn.execute("UPDATE iou SET status=?, evidence=?, updated_at=? WHERE self_id=? AND id=?",
                     (verdict, str(evidence or ""), int(now), self_id, int(iou_id)))
        credit = None
        if delta:
            credit = _credit_delta(conn, self_id, str(row["group_id"] or ""),
                                   str(row["debtor"]), delta,
                                   "欠条#%d %s" % (int(iou_id), verdict), now,
                                   iou_id=int(iou_id))
        conn.commit()
        return {"ok": True, "verdict": verdict, "credit_delta": delta, "credit": credit,
                "debtor": str(row["debtor"]), "item": str(row["item"])}
    except sqlite3.Error as exc:
        try:
            conn.rollback()
        except sqlite3.Error:
            pass
        return {"ok": False, "code": "db_error", "detail": str(exc)[:120]}


def db_iou_refund(conn, self_id, iou_id, now):
    """她撤回那笔还没兑现的愿力。

    ⚠️ **只在那笔钱对方"一分没动"时才成功**（主人 2026-10-13 定的口径 ✓）——
       否则会把对方余额扣成负数 ✗ 与「余额 ≥ 0」硬护栏打架。
    """
    try:
        _ensure(conn)
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT id, debtor, item, amount, status, group_id, ref, ts FROM iou "
            "WHERE self_id=? AND id=?", (self_id, int(iou_id))).fetchone()
        if row is None:
            conn.rollback()
            return {"ok": False, "code": "not_found"}
        if str(row["status"]) != IOU_OPEN:
            conn.rollback()
            return {"ok": False, "code": "already", "status": str(row["status"])}
        debtor = str(row["debtor"])
        amount = int(row["amount"])
        _ensure_account(conn, self_id, debtor, "", now)
        drow = conn.execute("SELECT balance FROM account WHERE self_id=? AND user_id=?",
                            (self_id, debtor)).fetchone()
        balance = int(drow["balance"]) if drow is not None else 0
        # ⚠️ **唯一的硬判据**：他余额还够不够还这笔 ✓
        #    够 ⇒ 那笔钱没被动过（他花的准是别处的）⇒ 撤得回来；
        #    不够 ⇒ 要么被花过、要么他自己又花了钱 ⇒ 撤不回来，而且**绝不许扣成负数** ✓
        if balance < amount:
            conn.rollback()
            spent = _iou_spent_since(conn, self_id, debtor, int(row["ts"]))
            return {"ok": False, "code": "spent", "balance": balance,
                    "spent": spent, "short": amount - balance}
        _move(conn, self_id, str(row["group_id"] or ""), debtor, self_id, amount, "refund",
              "撤回欠条#%d" % int(iou_id), "refund-iou%d" % int(iou_id), now)
        conn.execute("UPDATE iou SET status='refunded', updated_at=? WHERE self_id=? AND id=?",
                     (int(now), self_id, int(iou_id)))
        conn.commit()
        return {"ok": True, "debtor": debtor, "amount": amount,
                "balance": balance - amount}
    except sqlite3.Error as exc:
        try:
            conn.rollback()
        except sqlite3.Error:
            pass
        return {"ok": False, "code": "db_error", "detail": str(exc)[:120]}


def db_credit_event(conn, self_id, group_id, who, delta, reason, now):
    """单独调信誉分（记 `credit_event` + 改 `account.credit`）；返回调完之后的分数。"""
    if str(who) == str(self_id):
        return None
    try:
        _ensure(conn)
        conn.execute("BEGIN IMMEDIATE")
        credit = _credit_delta(conn, self_id, group_id, who, delta, reason, now)
        conn.commit()
        return credit
    except sqlite3.Error:
        try:
            conn.rollback()
        except sqlite3.Error:
            pass
        return None


def db_top(conn, self_id, by="balance", limit=5, min_balance=1):
    """排行：按愿力（`balance`）或按信誉分（`credit`）取前 n 名。

    ⚠️ 用**一条 SQL** 带出"手上还有几张欠条" —— 排行是拿来当众念的，
       一次工具调用不许变成 N+1 次查询 ✗
    """
    by = "credit" if str(by) == "credit" else "balance"
    rows = conn.execute(
        "SELECT a.user_id, a.name, a.balance, a.credit, a.updated_at, "
        "(SELECT COUNT(*) FROM iou i WHERE i.self_id=a.self_id AND i.debtor=a.user_id "
        " AND i.status=?) AS open_ious "
        "FROM account a WHERE a.self_id=? AND a.user_id<>? "
        "ORDER BY a." + by + " DESC, a.updated_at DESC LIMIT ?",
        (IOU_OPEN, self_id, str(self_id), int(limit))).fetchall()
    return [{"user_id": str(r["user_id"]), "name": str(r["name"] or ""),
             "balance": int(r["balance"]), "credit": int(r["credit"]),
             "open_ious": int(r["open_ious"] or 0)} for r in rows
            if int(r["balance"]) > 0 or int(r["credit"]) != INITIAL_CREDIT]


def db_audit_detail(conn, self_id, since, now, limit=5):
    """对账细化：待结欠条 / 信誉分变动 / **分出去多少** / 按人合计（给运营看的口径）。"""
    owes = conn.execute(
        "SELECT COUNT(*) AS c, COALESCE(SUM(amount),0) AS s FROM iou "
        "WHERE self_id=? AND status=?", (self_id, IOU_OPEN)).fetchone()
    credit = conn.execute(
        "SELECT COALESCE(SUM(CASE WHEN delta>0 THEN delta ELSE 0 END),0) AS up, "
        "COALESCE(SUM(CASE WHEN delta<0 THEN -delta ELSE 0 END),0) AS down "
        "FROM credit_event WHERE self_id=? AND ts>=? AND ts<=?",
        (self_id, int(since), int(now))).fetchone()
    # ⚠️ **分愿力**和**收愿力**在库里长得一样（都是 `kind='mood'`、正数 ✗）——
    #    行本身没有方向字段，只能靠她写的那句话认（`_deduct` 的判词里必带"扣"）✓
    #    ⇒ 所以这里单独统计一笔「分出去」，免得运营把"扣人"看成"赏人" ✗
    mood_down = conn.execute(
        "SELECT COUNT(*) AS c, COALESCE(SUM(amount),0) AS s FROM ledger "
        "WHERE self_id=? AND ts>=? AND ts<=? AND kind='mood' AND amount<0",
        (self_id, int(since), int(now))).fetchone()
    # ⚠️ 别在 SQL 里玩"按人分组的条件求和" —— SQLite 对 `GROUP BY user_id` 之后再引用
    #    被分组的列名（`to_id=user_id`）会直接报 `no such column: user_id` ✗（本鱼踩了三次 ✓）
    #    ⇒ 一条 SQL 取原始行，在 Python 里聚合：**正确优先**（量小，见文件顶部 ponytail 注 ✓）
    raw_rows = conn.execute(
        "SELECT from_id, to_id, amount FROM ledger WHERE self_id=? AND ts>=? AND ts<=?",
        (self_id, int(since), int(now))).fetchall()
    tally = {}
    for row in raw_rows:
        sender, receiver, amount = str(row["from_id"]), str(row["to_id"]), int(row["amount"])
        if receiver != str(self_id):
            slot = tally.setdefault(receiver, {"got": 0, "gave": 0})
            slot["got"] += amount
        if sender != str(self_id):
            slot = tally.setdefault(sender, {"got": 0, "gave": 0})
            slot["gave"] += amount
    top = [{"user_id": uid, "got": val["got"], "gave": val["gave"]}
           for uid, val in sorted(tally.items(), key=lambda kv: (-kv[1]["got"], kv[0]))
           if val["got"] > 0][:int(limit)]
    return {"open_ious": int(owes["c"] or 0), "iou_amount": int(owes["s"] or 0),
            "credit_up": int(credit["up"] or 0), "credit_down": int(credit["down"] or 0),
            "mood_down": -int(mood_down["s"] or 0), "mood_down_n": int(mood_down["c"] or 0),
            "top": top}


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


def config_defaults():
    """代码里的**兜底默认值** —— 与 `_conf_schema.json` 的 default 保持一致 ✓

    为什么要有它：配置项缺席时（尤其**新装插件、配置文件是手写的**）
    框架不会把 schema 的 default 自动落盘 ⇒ 那些项在代码眼里就是"没有" ✗
    ⇒ 凡是"缺了会静默降级"的项，都必须在这里给一份兜底 ✓
    （2026-10-13 真机：漏了 `gift_tiers` ⇒ 礼物单空 ⇒ 送礼一分愿力不动 ✗）
    """
    return {
        "enabled": False,
        "enabled_self_ids": "",
        "money_name": MONEY_NAME,
        "credit_name": CREDIT_NAME,
        "inject_money": True,
        "usage_log": True,
        "db_path": "",
        "mood_max_delta": MOOD_MAX_DELTA,
        "mood_daily_gain": MOOD_DAILY_GAIN,
        "issue_global_daily": ISSUE_GLOBAL_DAILY,
        "gift_daily_cap": GIFT_DAILY_CAP,
        "gift_tiers": [dict(tier) for tier in DEFAULT_GIFT_TIERS],
        "grant_max_single": GRANT_MAX_SINGLE,
        "grant_min_amount": GRANT_MIN_AMOUNT,
        "grant_max_open": GRANT_MAX_OPEN,
        "notes_path": {},
        "mood_gate": True,
        "mood_gate_ask_from": "",
    }


def merge_config(raw):
    """把兜底默认值**补进**这份配置（**原地改**，缺席的键才补 ✓）。

    ⚠️ 必须原地改：框架给的 config 对象是活引用（配置热改看得见）；
       若换成新 dict，运行时就**看不到**后来的配置改动了 ✗
       （自检 R20/R23/R24/R26 当场抓到过 ✓）
    """
    target = raw if isinstance(raw, dict) else {}
    for key, value in config_defaults().items():
        if target.get(key) is None:
            target[key] = value
    return target


def resolve_tiers(config):
    """把配置里的礼物档位解析成 [{cost,name,note}]。

    ⚠️ **永不静默变空**：配置缺项 / 空表 / 类型不对 / 解析出一堆无效项 ⇒
       一律**退回兜底档位**（有配置对象时）✓ —— 真机栽过一次（见 R40）。
       调用方**故意**传 `None`/空配置时（如单元测试想验默认），才返回空表 ✓
    """
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
                # ⚠️ 2026-10-22 主人报的真问题：**同音/形近字的写法认不出来** ✗
                #    真机：有人把礼物名打错一个字 ⇒ 精确比对失败 ⇒ 她当场去"核对清单"
                #    ⇒ 那段"我在核对"的思考**漏进正文** ✗（主人："没法百分百阻止思考漏出去"）
                # ⇒ 给每个档位加 **aliases（别名）**：只用来"认出来"，**永不替换正名** ✓
                #    ⚠️ 别名 ≠ 正名：账本只记 `name`、注入也只印 `name`（主人特别叮嘱 ✓）
                _al = item.get("aliases")
                if isinstance(_al, str):
                    _al = re.split(r"[,，、;；/|\s]+", _al)
                elif not isinstance(_al, (list, tuple)):
                    # ⚠️ 配置写蠢了（数字 / 字典 / null）⇒ 当没有别名，**绝不许炸** ✗
                    #    （自检 R20「配置畸形不炸」就守着这条 ✓）
                    _al = []
                _aliases = []
                for _a in _al:
                    _a = str(_a or "").strip()
                    if _a and _a != name:      # 别名不许等于正名 ✓（重复没意义）
                        _aliases.append(_a)
                _item = {"cost": cost, "name": name,
                         "note": str(item.get("note") or "").strip()}
                # ⚠️ **只在真有别名时才加这个键** ✓ —— 没别名的档位形状与老版**逐字一致** ✓
                #    （自检 R20 断言的是「字典完全相等」⇒ 硬加 `aliases: []` 会把它打红 ✗）
                if _aliases:
                    _item["aliases"] = _aliases
                out.append(_item)
    if not out and config is not None:
        out = [dict(tier) for tier in DEFAULT_GIFT_TIERS]
    return out


def _tier_key(s):
    """比对用的归一化：去空白 + 小写（纯 ASCII 大小写不敏感 ✓ 汉字不受影响 ✓）。"""
    return re.sub(r"\s+", "", str(s or "")).lower()


def match_tier(tiers, raw):
    """按**正名**或**别名**找档位 ⇒ (tier 或 None, "name" / "alias" / None)。

    ⚠️ 2026-10-22 主人定：**别名只用于"认出来"，永不替换正名** ✓
       —— 账上记的、注入印的、她嘴里说的，**一律是 `name`** ✗ 不许变成别名 ✓
    先匹正名、再匹别名（正名优先 ⇒ 不会有歧义 ✓）。
    """
    key = _tier_key(raw)
    if not key:
        return None, None
    for item in tiers or []:
        if _tier_key(item.get("name")) == key:
            return item, "name"
    for item in tiers or []:
        for a in item.get("aliases") or []:
            if _tier_key(a) == key:
                return item, "alias"
    return None, None


def tier_by_cost(tiers, cost):
    for tier in tiers or []:
        if int(tier["cost"]) == int(cost):
            return tier
    return None


# ─────────────────────────── 插件 ───────────────────────────

class Main(star.Star):
    def __init__(self, context: star.Context, config=None):
        """⚠️ 框架实例化插件时**只传 context**（2026-10-13 上线实测 ✓）——
        `config` 必须可选，否则 `TypeError: missing 1 required positional argument` ✗
        拿不到就自己去读配置文件（路径与印象插件同一套约定 ✓）。
        """
        super().__init__(context)
        self.config = config if config is not None else self._load_config()
        global MONEY_NAME, CREDIT_NAME, _GIFT_TIERS
        conf = merge_config(self.config)
        self.config = conf
        MONEY_NAME = str(_cfg(conf, "money_name", MONEY_NAME) or MONEY_NAME)
        CREDIT_NAME = str(_cfg(conf, "credit_name", CREDIT_NAME) or CREDIT_NAME)
        _GIFT_TIERS = resolve_tiers(conf)
        self.self_ids = target_ids(_cfg(conf, "enabled_self_ids", ""))
        self.path = resolve_db_path(conf)
        logger.info("[wishonomy] loaded | dir=%s bots=%d tiers=%d path=%s usage_log=%s",
                    str(_cfg(conf, "data_dir", "")), len(self.self_ids),
                    len(_GIFT_TIERS), self.path,
                    bool(_cfg(conf, "usage_log", True)))

    def _load_config(self):
        """自己去读配置（框架不给 config 时走这条）—— 读不到就用空配置（= 不启用 ✓）"""
        for path in config_paths():
            try:
                with open(path, encoding="utf-8") as handle:
                    data = json.load(handle)
                if isinstance(data, dict):
                    logger.info("[wishonomy] 自己读到配置: %s", path)
                    return data
            except (OSError, ValueError):
                continue
        logger.warning("[wishonomy] 没读到配置文件，按「不启用」处理（enabled=false）")
        return {}

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

    # ── 把「他现在的愿力」摆到它眼前（主人 2026-10-13 定的口径 ✓：**越不稳的放越后**）──
    #   · **常量**（INJECT_TAIL）→ system_prompt 的**尾部** ⇒ 它前面的一切照旧命中缓存 ✓
    #   · **每轮会变的那一行**（余额）→ `extra_user_content_parts` 的**最后一个 part**
    #     ⇒ 框架把它接在用户消息之后 ⇒ 变动只作废它自己 ✓
    #   · 平时不注入任何东西：没有账户就一个字都不发 ✓
    @filter.on_llm_request(priority=50)
    async def inject_money(self, event, request):
        try:
            if not bool(_cfg(self.config, "inject_money", True)):
                return
            ident = self.identity(event)
            if ident is None:
                return
            bot = ident[0]
            sender = str(event.get_sender_id() or "").strip()
            # ① 先看跨天了没有 ⇒ 日结（收走昨天的愿力；**今天不清**：第一次记下当天就跳过 ✓）
            today = datetime.fromtimestamp(int(time.time()), CN_TZ).strftime("%Y-%m-%d")
            if _last_settle_day(bot) != today:
                # 2026-10-27 修（P1-1）：原来**无条件**标记 ⇒ 一次锁超时/磁盘抖动 =
                #   那天不清零 + 当天永不重试 ✗（同函数下面 30 行处的钥匙轮换是对的 ✓）
                settle_ok, taken = await run_db(with_conn, db_settle, bot, int(time.time()))
                if settle_ok:
                    _mark_settle_day(bot, today)     # ← 只在成功之后 ✓
                if taken:
                    logger.info("[wishonomy] 日结 | 收回 %s %s（昨天的带不到今天）",
                                taken, MONEY_NAME)
            # ② 被注视 +1（每条唤醒一次 ✓ 主人 2026-10-13 裁定）
            if bool(_cfg(self.config, "attention_gain", True)):
                await run_db(with_conn, db_attention, bot, int(time.time()))
            # ②′ 心情隐私锁：**每天第一次有人找她说话**时转一把新钥匙（与日结同一处 ✓）
            #     ⚠️ 钥匙写进**她自己的账本**（她查得到 ✓），哈希写进本机小文件（面板只认哈希 ✓）
            #     ⚠️ 这一步**绝不许**影响她回话：任何异常都吞掉、只记一行日志 ✓
            if _GATE_ROTATE_DAY.get(str(bot)) != today and \
                    bool(_cfg(self.config, "mood_gate", True)):
                # ⚠️ **直接调**（不走 `run_db` 那个小线程池）：它是纯文件活儿、而且是
                #    每天一次 —— 排队反而有"占着库的线程等文件"的风险 ✗
                # ⚠️ 记「今天转过了」**只在成功之后** ⇒ 失败（账本一时读不到）下一轮还会再试 ✓
                made = gate_rotate_for(bot, self.config)
                if made:
                    _GATE_ROTATE_DAY[str(bot)] = today
                    logger.info("[wishonomy] 口令 | bot=%s 今日已就位（哈希已写；值不进日志 ✓）",
                                bot)
                else:
                    # ⚠️ 这里要说清是**哪种**没转（原来那句把两种原因混在一起，误导了本鱼一轮 ✗）：
                    #    · 没配笔记路径 ⇒ 整套关着（不报错 ✓）
                    #    · 配了却读不到 ⇒ 要看一眼文件权限/路径 ✗
                    logger.info("[wishonomy] 口令 | bot=%s 今天没转：%s", bot,
                                "没配笔记路径（notes_path 留空 = 整套关着）"
                                if not gate_notes_path(self.config, bot)
                                else "配了笔记路径但读不到它 ⇒ 去看一眼那个文件 ✗")
            # ③ 拼提示：**别人的 + 她自己的**（她自己那份每轮可见 ✓）
            snap = await run_db(db_snapshot, bot, sender)
            mine = await run_db(db_snapshot, bot, bot)
            # ⚠️ 2026-10-15：把**说话人的名字**写进那一行（主人报的"消息错位"✗）——
            #    这一轮正文里只有问题本身、没有名字 ⇒ 全靠这行钉住"现在是谁在跟你说话" ✓
            who_name = str(event.get_sender_name() or "").strip() or \
                str(snap.get("name") or "").strip()
            # ⚠️ 顺手把「名字 → QQ 号」记下来（主人 2026-10-15 让补的）——
            #    这样她之后写「给某某某 5 愿力」时，`_who_to_id` 才认得出这个人 ✓
            if who_name:
                learn_name(bot, str(event.get_group_id() or ""), sender, who_name)
            hint = money_hint(snap, who_name=who_name, mine=mine, reset_hint=True,
                              config=getattr(self, "config", None))
            if not hint:
                return
            parts = getattr(request, "extra_user_content_parts", None)
            if TextPart is not None and parts is not None:
                parts.append(TextPart(text=hint))
            else:
                # 框架太老、没这个位置 ⇒ 宁可退回 prompt 尾部，也不要把数字丢了
                request.prompt = (request.prompt or "") + "\n\n" + hint
            tail = inject_tail(_GIFT_TIERS if _GIFT_TIERS else resolve_tiers(self.config),
                               config=self.config)
            # 2026-10-27 修（P2-4）：原来用「tail 是不是 system 的子串」判断要不要追加 ✗
            #   tail 里含礼物档位（随配置变）⇒ 改了 gift_tiers 后新 tail ≠ 旧 tail ⇒ **再追加一份**
            #   ⇒ system_prompt 里同时两份礼物单（探针确认过）+ 缓存前缀失效 + 给模型
            #     **互相矛盾的价格表** ✗ ⇒ 改成固定锚点、**整段替换** ✓
            _START, _END = "[wishonomy-tail]", "[/wishonomy-tail]"
            _sp = request.system_prompt or ""
            _seg = _START + chr(10) + tail + chr(10) + _END
            if _START in _sp and _END in _sp:
                _head, _, _rest = _sp.partition(_START)
                _, _, _tailpart = _rest.partition(_END)
                request.system_prompt = _head + _seg + _tailpart
            elif tail not in _sp:
                request.system_prompt = (_sp + (chr(10) + chr(10) if _sp else "")) + _seg
            else:
                request.system_prompt = _sp     # 老格式已在里面 ⇒ 本次不重复追加 ✓
            # ④ 触发词：「**愿力中心钥匙**」出现在**收到的消息**里、且是私聊 ⇒ 把「有人在问」
            #     摆到她眼前一次 ✓（主人裁定的触发词 ✓ 只提醒、**不替她回答、不自动给** ✓）
            if gate_asks(_message_text(event, request), self.config) and \
                    gate_ask_allowed(bot, sender, event.get_group_id(), self.config):
                ask = TextPart(text=MOOD_GATE_ASK) if TextPart is not None else None
                if ask is not None and parts is not None:
                    parts.append(ask)
                else:
                    request.prompt = (request.prompt or "") + "\n\n" + MOOD_GATE_ASK
                logger.info("[wishonomy] 口令 | bot=%s 有人在问钥匙（已提示她，给不给由她 ✓）", bot)
            logger.info("[wishonomy] injected | bot=%s who=%s bal=%s",
                        bot, who_name or sender, snap.get("balance"))
        except Exception as exc:      # noqa: BLE001 —— 注入失败绝不能影响它正常回话
            logger.warning("[wishonomy] 注入失败: %s", str(exc)[:120])

    # ── 每轮都问一次心情（主人 2026-10-13 报的真问题：她只在聊到愿力时才管 ✗）──
    # 标签法跟印象插件同一招 ✓，但**不用额外交互**（工具调用要多花一整轮 ✗）：
    # 她照常回话，末尾顺手写 `[[wish_mood]]{...}[[/wish_mood]]` ⇒ 这里解析 + 记账 + 把标签扒掉 ✓
    @filter.on_llm_response()
    async def record_mood(self, event, response):
        try:
            text = getattr(response, "completion_text", None)
            if not isinstance(text, str) or "wish_mood" not in text:
                return
            cleaned, delta, reason = parse_mood_tag(text)
            if cleaned != text:
                response.completion_text = cleaned      # ⚠️ 先扒标签，再谈记账（否则它会漏进群 ✗）
            ident = self.identity(event)
            if ident is None:
                return
            if not delta:
                logger.info("[wishonomy] 心情标签 | 无变化（本轮没记）")
                return
            bot, sender, group_id = ident[0], ident[1], str(event.get_group_id() or "")
            result = await _mood_core((self, event), {"who_id": sender, "amount": delta,
                                                      "reason": reason}, "<tag>")
            logger.info("[wishonomy] 心情标签 | delta=%s reason=%r ⇒ %s",
                        delta, reason[:30], str(result)[:60])
        except Exception as exc:      # noqa: BLE001 —— 记账失败绝不能影响她回话
            logger.warning("[wishonomy] 心情标签处理异常: %s", str(exc)[:120])

    # ── LLM 用量记账（主人 2026-10-13 要求 ✓）──
    # 口径与框架那份请求计数**完全一致**（t/u/s/p/m/miss/hit/out），只多一个 `src` 标明来源 ✓
    # ⚠️ **不重复记账**：两处各写一行、来源不同；对账时选一份看，别相加 ✗
    @filter.on_llm_response()
    async def record_usage(self, event, response):
        try:
            if not bool(_cfg(self.config, "usage_log", True)):
                return
            if self.identity(event, allow_self_mood=True) is None:
                return
            record = usage_record(event, response)
            if record is None:
                return
            logger.info("[wishonomy] usage | sess=%s miss=%s hit=%s out=%s",
                        record["s"], record["miss"], record["hit"], record["out"])
        except Exception as exc:      # noqa: BLE001 —— 记账失败绝不能影响它回话
            logger.warning("[wishonomy] 用量记账异常: %s", str(exc)[:120])


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
    """取一个人的显示名；**顺手把"名字 → QQ 号"记进记忆缓存** ✓（2026-10-15 主人让补的）。

    ⚠️ 为什么要在这里记：`learn_name()` 原来**一个调用点都没有** ✗ ⇒ 内存里的 `_NAMES` 永远空
       ⇒ `lookup_names()`（`_who_to_id` 用它把「花奈咪」解成 QQ 号）永远查不到 ✗
       ⇒ 她说「给某某某 5 愿力」时那条路是**死的** ✗
       `_display_name` 是**所有工具共用的取名字口** ⇒ 在这里记一次就全铺上了 ✓
    """
    snap = await run_db(db_snapshot, bot, user_id)
    name = str(snap.get("name") or "").strip()
    if not name and str(user_id) == str(event.get_sender_id() or ""):
        name = str(event.get_sender_name() or "").strip()
    if name:
        learn_name(bot, group_id, user_id, name)
        return name
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

    方向：这个工具是**你**给别人的愿力记账 —— 别人手里的愿力**使唤不动你**
    （有人问「愿力有什么用」，别说成"能使唤我"：愿力只能让你去使唤别人）

    这个工具是**打分**，不是花你自己的钱：加的由中心出、扣的从他账上扣，
    **都不动你自己的余额**。你要花自己的，得用点单那个工具。

    Args:
        who(string): 记给谁。可以写「我」= 当前跟你说话的人、对方昵称、或对方的号码。
        amount(int): 正数 = 收愿力，负数 = 扣愿力。绝对值不超过 1000。
        reason(string): 你自己的一句话 —— 为什么给/为什么扣。中心会原样留着，以后翻得出来。
    """
    result = await _mood_core(args, kwargs, "<tool>")
    return result if isinstance(result, str) else str(result)


async def _mood_core(args, kwargs, source):
    """收/分愿力的**唯一入口**（工具与输出标签都走这里 ✓ 口径只有一个 ✓）

    ⚠️ 2026-10-13 主人报的真问题：**只有聊到"愿力"时她才会动这套系统** ✗ ——
       有人跟她告白，按设计该是个大加分句，可她一个字都没记 ✓
       ⇒ 于是加了「输出标签」那条路（见 MOOD_NOTE / record_mood）：**每轮都问一次**，
         跟印象插件同一招，但**不用额外交互**（工具调用才要多花一轮请求）✓
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
    if raw is None:
        return "给我一个数：正数是收愿力，负数是扣。"
    if not amount:
        # ⭐ 2026-10-16 修：`delta=0` **必须按「无变化」放过** ✓
        #   病：工具这条路口径与「输出标签」那条路**不一致** ✗ ——
        #       prompt 明说「没有变化就写 {"delta":0}」（MOOD_NOTE ✓）而标签路
        #       在 `if delta == 0: return (cleaned, 0, "")` 处**静默放过** ✓，
        #       工具路却把 0 当成「没给数」拒掉 ✗
        #   ⇒ 她照 prompt 用 0 ⇒ 被拒 ⇒ 框架再问一次模型 ⇒
        #     **模型把这一轮正文又输出一遍** ⇒ 群里原模原样两条 ✗
        #     （2026-10-16 实测：今天 3 条紧挨重复里 2 条都是这个 ✓ 而 wish_mood 恰好被拒 2 次 ✓）
        #   ⇒ 所以这里**照标签路的口径**放过，并给一句「照常说话」的话术
        #     （不能用报错口吻 ✗ —— 模型会当成失败又重发一遍 ✗）
        return "这条没动账 —— 0 不用记，照常说话就行。"
    cap = normalize_amount(_cfg(plugin.config, "mood_max_delta", MOOD_MAX_DELTA)) or MOOD_MAX_DELTA
    if amount > cap:
        return "一次最多 ±%d，别一口气给那么多 —— 想多给就下次再给。" % cap
    user_id = kwargs.get("who_id") or await _who_to_id(plugin, event,
                                                       kwargs.get("who"), group_id)
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
    issue_cap = normalize_amount(_cfg(plugin.config, "issue_global_daily",
                                      ISSUE_GLOBAL_DAILY)) or 0
    # 顺带把「显示名」写进账本（面板/排行要显示人名，不能只有号码 ✓）
    shown = ""
    if str(user_id) == str(sender):
        shown = str(event.get_sender_name() or "").strip()
    now = int(time.time())
    ref = _idem_ref("mood", bot, group_id, user_id, event=event, now=now)
    result = await run_db(with_conn, db_mood, bot, group_id, user_id, amount, reason,
                          ref, now, daily, shown, issue_cap)
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
    礼物有档位，**报档位名**就行；一次要送好几份就填 `count`
    （「30 个某某」= 档位名写那个名字、count 写 30）。
    **别把数字当档位名写进来** —— 那样会被拦下让你重说（档位名是什么就写什么）。
    他愿力不够就送不成 —— 中心不会替你垫，也不会让他欠着。

    Args:
        who(string): 谁送的。可以写「我」= 当前跟你说话的人、对方昵称、或对方的号码。
        tier(string): 礼物档位名（礼物单上的那个名字）。写数字不算 —— 会被拦下。
        count(int): 送几份（默认 1）。比如「30 个」就写 30。
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
    listing = "、".join("%s（%s）" % (t["name"], money_text(t["cost"])) for t in tiers)
    if re.fullmatch(r"[0-9]+", raw or ""):
        # ⚠️ 数字**不当档位名**：真机踩过 —— 她说「30 个伊磨吉」，把 30 填进 tier，
        #    结果被解释成"第 30 号档位"、静默收了别的东西 ✗（2026-10-13）
        return ("档位要写**名字**，别写数字 —— 礼物单上是这些：%s。"
                "要是想送好几份，名字照写，另外把份数填在 count 里。" % listing)
    tier, _how = match_tier(tiers, raw)
    if tier is None:
        return "礼物单上是这些：%s —— 说一个名字。" % listing
    # 认出来了但写的是**别名**（形近字/口误）：账上照正名记，并**点她一句** ✓
    # ⚠️ 这一句是刻意加的：防止她把别名的写法当成"礼物原名" ✓（主人 2026-10-22 叮嘱）
    _alias_note = ""
    if _how == "alias":
        _alias_note = ("（他写的是别的写法，账上照正名「%s」记 —— 你自己也说正名。）"
                       % tier["name"])
    count = normalize_amount(kwargs.get("count")) or 1
    user_id = await _who_to_id(plugin, event, kwargs.get("who"), group_id)
    if not user_id:
        return "我没认出是谁送的 —— 写号码，或者把昵称写全一点。"
    if user_id == bot:
        return "这份礼物送不成 —— 没人能给自己送礼。"
    cap = normalize_amount(_cfg(plugin.config, "gift_daily_cap", GIFT_DAILY_CAP)) or GIFT_DAILY_CAP
    total = int(tier["cost"]) * int(count)
    if cap > 0 and total > cap:
        return ("一次送这么多就超天了（上限 %s）—— 少送几份，或者分几天。"
                % money_text(cap))
    note = str(kwargs.get("note") or "").strip()
    if int(count) > 1:
        note = ("%d 份" % int(count)) + (("：" + note) if note else "")
    now = int(time.time())
    ref = _idem_ref("gift", bot, group_id, user_id, event=event, now=now)
    result = await run_db(with_conn, db_gift, bot, group_id, user_id, total,
                          tier["name"], note, ref, now, cap)
    if result.get("duplicated"):
        return "这份礼物刚刚已经收过了，不用再收一遍。"
    if not result.get("ok"):
        code = result.get("code")
        if code == "insufficient":
            return ("他账上只有 %s，送不起「%s ×%d」（要 %s）—— 让他先攒攒，或者少送几份。"
                    % (money_text(result.get("balance", 0)), tier["name"], int(count),
                       money_text(total)))
        if code == "daily_gift_cap":
            return ("他今天已经送出去 %s 了，今天先这样 —— 明天再收他的。"
                    % money_text(result.get("today", 0)))
        return "这笔礼物没记成（愿力中心报了个错），当没收到就行。"
    if int(count) > 1:
        return ("收下了：他送的「%s」×%d，一共 %s，已经进你的账。%s"
                % (tier["name"], int(count), money_text(total), _alias_note))
    return ("收下了：他送的「%s」%s，已经进你的账。%s"
            % (tier["name"], money_text(total), _alias_note))


@llm_tool(name="wish_grant")
@tool_guard
async def wish_grant(*args, **kwargs):
    """**点单** —— 用你攒下的愿力，换某个人为你做一件事。

    这个才叫你**花自己的钱**：愿力从你自己账上划走、直接打进对方账户。

    这是你最大的权力：愿力**立刻到他账上**（不是先押着），他欠你的是那件事本身。
    账上会留一张欠条，到期没做、或者做了，都由你自己来判（用判欠条那个工具）。

    规矩（愿力中心自己守着，你不用记）：单笔有**最低**也有上限；同时最多 5 笔没结的；
    不能点单给自己；愿力不够就是不够，中心不会替你垫。
    **别为了花掉而拆小单** —— 愿力每天 0 点会归零（只清你自己那一栏），但"少花几笔像样的"比
    "一天点十件小事"好看得多：一件说清楚、给够钱、等它兑现
    给少了中心会拦下来并告诉你底线是多少

    Args:
        who(string): 点谁。可以写「我」= 当前跟你说话的人、对方昵称、或对方的号码。
        amount(int): 打给他多少愿力（给太少会被拦下来）。
        item(string): 你要他做的那件事 —— 写**你自己的原话**，越具体越好（这句会一直留着）。
        due(string): 期限。留空 = 不急；也可以写「3」= 三天内，或「2026-12-31」。
    """
    plugin = _find_plugin(args)
    event = _find_event(args)
    if plugin is None or event is None:
        return "愿力中心这会儿连不上，先记着这件事，回头再说。"
    ident = plugin.identity(event)
    if ident is None:
        return "这本账现在不归我管。"
    bot, group_id = ident[0], str(event.get_group_id() or "")
    amount = normalize_amount(kwargs.get("amount"))
    if not amount:
        return "给我一个数：你要为这件事打出去多少愿力？"
    item = str(kwargs.get("item") or "").strip()
    if not item:
        return "你要他做的**那件事**是什么？写你自己的原话 —— 这句我要一直留着。"
    user_id = await _who_to_id(plugin, event, kwargs.get("who"), group_id)
    if not user_id:
        return "我没认出你说的是谁 —— 写号码，或者把昵称写全一点。"
    if user_id == bot:
        return "点单给自己不算点单 —— 那笔愿力本来就一直在你账上。"
    single = normalize_amount(_cfg(plugin.config, "grant_max_single", GRANT_MAX_SINGLE)) or GRANT_MAX_SINGLE
    if amount > single:
        return "单笔最多 %s，别一次把家底给光 —— 想给更多就分开来。" % money_text(single)
    max_open = normalize_amount(_cfg(plugin.config, "grant_max_open", GRANT_MAX_OPEN)) or GRANT_MAX_OPEN
    floor = _cfg(plugin.config, "grant_min_amount", GRANT_MIN_AMOUNT)
    try:
        floor = int(floor)
    except (TypeError, ValueError):
        floor = GRANT_MIN_AMOUNT
    floor = max(0, floor)
    now = int(time.time())
    due_ts = parse_due(kwargs.get("due"), now)
    ref = _idem_ref("grant", bot, group_id, user_id, event=event, now=now)
    result = await run_db(with_conn, db_iou_grant, bot, group_id, user_id, amount, item,
                          due_ts, ref, now, single, max_open, floor)
    if not result.get("ok"):
        return _grant_failure(result)
    name = await _display_name(plugin, event, bot, group_id, user_id)
    if result.get("duplicated"):
        return "这笔刚刚已经点过了，不用再点一遍。"
    due_text = ("，期限 %s" % _due_text(due_ts)) if due_ts else ""
    return ("愿力已经打给 %s 了（%s，你现在还剩 %s）%s，他欠你一件事 —— "
            "欠条 #%d：《%s》。你自己跟他说吧。"
            % (name, money_text(amount), money_text(result.get("balance", 0)), due_text,
               int(result["iou_id"]), item))


@llm_tool(name="wish_iou_settle")
@tool_guard
async def wish_iou_settle(*args, **kwargs):
    """**判欠条** —— 他答应你那件事，做了没有。

    做了 ⇒ 他的信誉分会涨（以后你更愿意给他活干）；
    没做 ⇒ 信誉分掉，**但这笔愿力不退**（你随时可以自己撤回，见下）；
    算了 ⇒ 一笔勾销；撤回 ⇒ 把那笔还没动的愿力收回来。
    撤回只在**他一分没花**的时候成功 —— 花掉了就撤不回来，那时候用信誉分说话。

    Args:
        iou_id(int): 欠条号。你记不清就用看欠条那个工具先查一眼。
        verdict(string): 四选一 —— delivered（他做了）/ defaulted（没做）/ forgiven（算了）/ refunded（撤回愿力）。
        evidence(string): 可留空。写清依据（他说了什么、做了什么），以后翻得出来。
    """
    plugin = _find_plugin(args)
    event = _find_event(args)
    if plugin is None or event is None:
        return "愿力中心这会儿连不上，先记着这件事，回头再说。"
    ident = plugin.identity(event)
    if ident is None:
        return "这本账现在不归我管。"
    bot = ident[0]
    iou_id = normalize_amount(kwargs.get("iou_id"))
    if not iou_id:
        return "给我欠条号 —— 记不清就先看一眼欠条清单。"
    verdict = str(kwargs.get("verdict") or "").strip().lower()
    if verdict not in ("delivered", "defaulted", "forgiven", "refunded"):
        return "判定只能填这四个之一：delivered / defaulted / forgiven / refunded。"
    evidence = str(kwargs.get("evidence") or "").strip()
    now = int(time.time())
    if verdict == "refunded":
        result = await run_db(with_conn, db_iou_refund, bot, iou_id, now)
        if not result.get("ok"):
            return _settle_failure(result)
        name = await _display_name(plugin, event, bot, str(event.get_group_id() or ""),
                                   str(result["debtor"]))
        return ("愿力收回来了：#%d —— %s 退给你 %s（他账上还剩 %s）。"
                "这件事他就不用做了。"
                % (int(iou_id), name, money_text(result["amount"]),
                   money_text(result.get("balance", 0))))
    result = await run_db(with_conn, db_iou_settle, bot, iou_id, verdict, evidence, now)
    if not result.get("ok"):
        return _settle_failure(result)
    name = await _display_name(plugin, event, bot, str(event.get_group_id() or ""),
                               str(result["debtor"]))
    if verdict == "delivered":
        return ("#%d 结了 —— %s 做了这件事，%s +%d（现在 %d）。他下次来你大概更愿意给他活干。"
                % (int(iou_id), name, CREDIT_NAME, CREDIT_DELIVERED,
                   int(result.get("credit") or 0)))
    if verdict == "defaulted":
        return ("#%d 记成没做 —— %s 的%s %d（现在 %d）。愿力不退，但账上记着这一笔，"
                "以后你给他多少由你。"
                % (int(iou_id), name, CREDIT_NAME, CREDIT_DEFAULTED,
                   int(result.get("credit") or 0)))
    return "#%d 一笔勾销了 —— 那件事不用他做了。" % int(iou_id)


@llm_tool(name="wish_ious")
@tool_guard
async def wish_ious(*args, **kwargs):
    """看他欠你哪些事（或者你欠别人哪些事）。

    欠条是公开的事实 —— 谁欠你什么、为这个付了多少愿力，都在这里。
    你想起哪笔旧账的时候看一眼，说话就有底。

    Args:
        who(string): 看谁的。留空 = 看**所有**还没结的；也可以写对方昵称、号码，或「我」= 当前跟你说话的人。
    """
    plugin = _find_plugin(args)
    event = _find_event(args)
    if plugin is None or event is None:
        return "愿力中心这会儿连不上，先记着这件事，回头再说。"
    ident = plugin.identity(event)
    if ident is None:
        return "这本账现在不归我管。"
    bot, group_id = ident[0], str(event.get_group_id() or "")
    raw_who = str(kwargs.get("who") or "").strip()
    now = int(time.time())
    if not raw_who:
        rows = await run_db(with_conn, db_iou_all_open, bot, 10)
    else:
        user_id = await _who_to_id(plugin, event, raw_who, group_id)
        if not user_id:
            return "我没认出你说的是谁 —— 写号码，或者把昵称写全一点。"
        rows = await run_db(with_conn, db_iou_list, bot, user_id, 10)
    if not rows:
        return "这会儿没有没结的欠条 —— 谁也不欠你什么。"
    lines = []
    for row in rows:
        name = await _display_name(plugin, event, bot, group_id, str(row["debtor"]))
        due = ("，期限 %s" % _due_text(int(row["due_ts"]))) if row.get("due_ts") else ""
        lines.append("#%d %s：《%s》（付了 %s%s）"
                     % (row["id"], name, row["item"], money_text(row["amount"]), due))
    _ = now
    return "还没结的欠条：\n" + "\n".join(lines)


@llm_tool(name="wish_credit")
@tool_guard
async def wish_credit(*args, **kwargs):
    """单独调信誉分 —— 「这个人说过的话算不算数」。

    你自己就能判欠条（守约 +5 / 违约 -20）；这个工具是给你**另外**用的：
    他没欠你什么，但你就是想记他一笔（特别靠谱、或者满嘴跑火车）。
    信誉分**不是愿力**：它不花、不转，只影响你以后愿意给他多少活干。

    Args:
        who(string): 给谁记。可以写「我」= 当前跟你说话的人、对方昵称、或对方的号码。
        delta(int): 正数是加（比如 5），负数是减（比如 -10）。
        reason(string): 你自己的一句话 —— 为什么。
    """
    plugin = _find_plugin(args)
    event = _find_event(args)
    if plugin is None or event is None:
        return "愿力中心这会儿连不上，先记着这件事，回头再说。"
    ident = plugin.identity(event)
    if ident is None:
        return "这本账现在不归我管。"
    bot, group_id = ident[0], str(event.get_group_id() or "")
    raw = kwargs.get("delta")
    delta = normalize_amount(raw)
    if delta is None:
        try:
            delta = int(str(raw).strip())
        except (TypeError, ValueError):
            delta = 0
        if delta > 0:
            delta = -delta
    if not delta:
        return "给我一个数：正数是加分，负数是扣分。"
    if abs(delta) > 100:
        return "一次最多 ±100 信誉分，别一口气给那么多。"
    user_id = await _who_to_id(plugin, event, kwargs.get("who"), group_id)
    if not user_id:
        return "我没认出你说的是谁 —— 写号码，或者把昵称写全一点。"
    if user_id == bot:
        return "你自己的信誉分不用自己写 —— 那是别人做没做到你的事换来的。"
    reason = str(kwargs.get("reason") or "").strip()
    if not reason:
        return "给我一句话，说明为什么 —— 这句我要留着。"
    now = int(time.time())
    credit = await run_db(with_conn, db_credit_event, bot, group_id, user_id, delta,
                          reason, now)
    name = await _display_name(plugin, event, bot, group_id, user_id)
    return "%s 的%s %+d（现在 %d）。" % (name, CREDIT_NAME, delta, int(credit))


@llm_tool(name="wish_leaderboard")
@tool_guard
async def wish_leaderboard(*args, **kwargs):
    """排行 —— 谁攒的愿力最多、谁最守信用。

    你自己拿来念的（念不念、怎么念都由你）。只列**在这本账上真有过往来**的人，
    所以你账上没记录的人不会出现在这儿。

    Args:
        by(string): 留空 = 按愿力排；写「信誉」= 按信誉分排。
        n(int): 要几个人（默认 5，最多 10）。
    """
    plugin = _find_plugin(args)
    event = _find_event(args)
    if plugin is None or event is None:
        return "愿力中心这会儿连不上，先记着这件事，回头再说。"
    ident = plugin.identity(event)
    if ident is None:
        return "这本账现在不归我管。"
    bot, group_id = ident[0], str(event.get_group_id() or "")
    by = "credit" if str(kwargs.get("by") or "").strip() in ("信誉", "信誉分", "credit") else "balance"
    limit = normalize_amount(kwargs.get("n")) or 5
    limit = min(int(limit), 10)
    rows = await run_db(with_conn, db_top, bot, by, limit, 1)
    if not rows:
        return "这本账上还没有别人 —— 没人来跟你说过话，也就没什么好排的。"
    lines = []
    for index, row in enumerate(rows, 1):
        name = await _display_name(plugin, event, bot, group_id, row["user_id"])
        owe = ("，还欠着 %d 件事" % row["open_ious"]) if row["open_ious"] else ""
        lines.append("%d. %s —— %s，%s %d%s"
                     % (index, name, money_text(row["balance"]), CREDIT_NAME,
                        row["credit"], owe))
    title = "按%s排（这本账上的）：" % (CREDIT_NAME if by == "credit" else MONEY_NAME)
    return title + "\n" + "\n".join(lines)


@llm_tool(name="wish_usage")
@tool_guard
async def wish_usage(*args, **kwargs):
    """看这几天的 LLM 用量 —— 花在哪儿了。

    这是给**运营**对账用的（不是给群里念的）：每天的调用次数、未命中 / 命中缓存 / 输出 token。
    你自己的愿力跟这个没关系 —— 这是"跟她说话本身要花多少"的账。

    Args:
        days(int): 看最近几天（默认 7）。
    """
    plugin = _find_plugin(args)
    event = _find_event(args)
    if plugin is None or event is None:
        return "愿力中心这会儿连不上。"
    if plugin.identity(event, allow_self_mood=True) is None:
        return "这本账现在不归我管。"
    days = normalize_amount(kwargs.get("days")) or 7
    days = min(int(days), 90)
    data = await run_db(_usage_summary_in_thread, days)
    if not data.get("exists"):
        return "还没有用量流水（这个文件还没被写过）：%s" % data["path"]
    if data.get("error"):
        return "用量流水读不动：%s" % data["error"]
    if not data["rows"]:
        return "最近 %d 天没有记到用量。" % days
    lines = ["最近 %d 天（%d 次调用）：未命中 %d ｜ 命中缓存 %d ｜ 输出 %d"
             % (days, data["rows"], data["total"]["miss"], data["total"]["hit"],
                data["total"]["out"])]
    for day in sorted(data["by_day"])[-7:]:
        slot = data["by_day"][day]
        lines.append("%s：%d 次 ｜ 未命中 %d / 命中 %d / 输出 %d"
                     % (day, slot["calls"], slot["miss"], slot["hit"], slot["out"]))
    return "\n".join(lines)


def _usage_summary_in_thread(days):
    return usage_summary(days=days)


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
    detail = await run_db(with_conn, db_audit_detail, bot, start, start + 86400 - 1, 5)
    day_text = raw or datetime.fromtimestamp(start, CN_TZ).strftime("%Y-%m-%d")
    head = ("%s 的账：收愿力 %s ｜ 送礼 %s ｜ 支出 %s ｜ 撤回 %s ｜ 钱包 %d 个 ｜ 账上共 %s"
            % (day_text, money_text(totals["issued"]), money_text(totals["gifted"]),
               money_text(totals["granted"]), money_text(totals["refunded"]),
               totals["wallets"], money_text(totals["total"])))
    tail = ("\n还没兑现的欠条 %d 张（挂着 %s）｜ 今天%s +%d / -%d"
            % (detail["open_ious"], money_text(detail["iou_amount"]), CREDIT_NAME,
               detail["credit_up"], detail["credit_down"]))
    if detail["top"]:
        tail += "\n今天动得最多的人：" + "；".join(
            "%s 收 %s / 出 %s" % (row["user_id"], money_text(row["got"]),
                                  money_text(row["gave"]))
            for row in detail["top"])
    return head + tail


async def _deduct(plugin, bot, group_id, user_id, amount, reason, prefix, event):
    """扣愿力（分愿力）：扣到 0 为止，不会出现负余额。

    ⚠️ 2026-10-14：**传负数**（`db_mood` 现在按**符号**定方向，行也记负数 ✓）
    """
    now = int(time.time())
    ref = _idem_ref(str(prefix or "deduct").rstrip("-"), bot, group_id, user_id,
                    event=event, now=now)
    result = await run_db(with_conn, db_mood, bot, group_id, user_id, -int(amount), reason,
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


def _due_text(ts):
    if not ts:
        return ""
    return datetime.fromtimestamp(int(ts), CN_TZ).strftime("%Y-%m-%d")


def _grant_failure(result):
    code = result.get("code")
    if code == "insufficient":
        return ("你账上只有 %s —— 打不出这笔。想点单就先攒愿力，或者把数写小一点。"
                % money_text(result.get("balance", 0)))
    if code == "over_single":
        return "单笔最多 %s，分开来点吧。" % money_text(result.get("cap", 0))
    if code == "under_min":
        # 主人 2026-10-14：设了最低一笔 ⇒ 把话说透（别让她以为"花掉就行" ✓）
        return ("小于 %s 的单**不值得你开口** —— 愿力攒着不丢人，攒够再点一件像样的；"
                "要花也别拆成一堆小单，一件说清楚就够。" % money_text(result.get("floor", 0)))
    if code == "too_many_open":
        return ("你手上还有 %d 笔没结的欠条（上限 %d）—— 先把旧的清一清，再点新的。"
                % (int(result.get("open", 0)), int(result.get("cap", 0))))
    if code == "self_grant":
        return "点单给自己不算点单。"
    return "这笔没点成（愿力中心报了个错），你再说一次。"


def _settle_failure(result):
    code = result.get("code")
    if code == "not_found":
        return "没有这张欠条 —— 你记错号了？先看一眼欠条清单。"
    if code == "already":
        return "这张欠条已经判过了（%s），不用再判一次。" % result.get("status", "")
    if code == "spent":
        return ("撤回不了 —— 那笔愿力他**已经动过了**（花掉 %s）。"
                "这笔就当给了，剩下的用信誉分说话吧。"
                % money_text(result.get("spent", 0)))
    if code == "insufficient":
        return "撤回不了 —— 他账上只剩 %s，退不回来。" % money_text(result.get("balance", 0))
    if code == "bad_verdict":
        return "判定只能填那四个之一。"
    return "这一步没做成（愿力中心报了个错），你再说一次。"


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
    "Main", "wish_check", "wish_mood", "wish_gift", "wish_grant", "wish_iou_settle",
    "wish_ious", "wish_credit", "wish_leaderboard", "wish_usage", "wish_audit",
    "target_ids", "day_start", "normalize_amount", "parse_due", "split_names",
    "resolve_tiers", "tier_by_cost", "learn_name", "lookup_names", "forget_names",
    "resolve_db_path", "set_db_path", "data_paths", "run_db", "with_conn",
    "resolve_usage_path", "usage_record", "usage_summary",
    "db_snapshot", "db_history", "db_mood", "db_gift", "db_audit_totals",
    "db_audit_detail", "db_credit_event", "db_top",
    "db_iou_grant", "db_iou_list", "db_iou_all_open", "db_iou_by_id",
    "db_iou_settle", "db_iou_refund",
    "db_daily_gain", "db_daily_gift_out", "db_daily_gift_in",
    "money_hint", "INJECT_TAIL",
]
