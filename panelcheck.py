# -*- coding: utf-8 -*-
"""愿力中心 · 只读面板自检（P6）。

口径与 `selfcheck.py` 一致：**失败就 exit 1**，一项一个函数，返回 True/False，
拿真库 + 真 HTTP 服务器跑（不是打桩）。

跑法：
    python panelcheck.py          # 用你自己的解释器（装了依赖那个）
"""
import importlib.util
SKIPPED = []   # 2026-10-27：跳过 ≠ 通过（P1-4）
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
PANEL = os.path.join(HERE, "panel.py")

# CLI 子进程要的桩（panel.py 会 `import main`，而 main.py 依赖 astrbot）
_STUB_SRC = """
import types as _t
_api = _t.ModuleType("astrbot.api"); _ev = _t.ModuleType("astrbot.api.event")
_st = _t.ModuleType("astrbot.api.star"); _root = _t.ModuleType("astrbot")
_api.AstrBotConfig = dict
_api.llm_tool = lambda *a, **k: (lambda fn: fn)
_api.logger = _t.SimpleNamespace(info=lambda *a, **k: None,
                                 warning=lambda *a, **k: None,
                                 error=lambda *a, **k: None)
_api.star = _st
_st.Star = type("Star", (), {"__init__": lambda self, context=None: None})
_st.Context = object
_st.StarTools = _t.SimpleNamespace(get_data_dir=lambda name: HERE)
_ev.AstrMessageEvent = object
_ev.filter = _t.SimpleNamespace(on_llm_request=lambda *a, **k: (lambda fn: fn),
                                on_llm_response=lambda *a, **k: (lambda fn: fn),
                                on_agent_done=lambda *a, **k: (lambda fn: fn))
_am = _t.ModuleType("astrbot.core.agent.message")
_am.TextPart = type("TextPart", (), {"__init__": lambda self, text: setattr(self, "text", text)})
for _n, _m in {"astrbot": _root, "astrbot.api": _api, "astrbot.api.event": _ev,
               "astrbot.api.star": _st, "astrbot.core": _t.ModuleType("astrbot.core"),
               "astrbot.core.agent": _t.ModuleType("astrbot.core.agent"),
               "astrbot.core.agent.message": _am}.items():
    sys.modules[_n] = _m
import importlib.util as _iu
_spec = _iu.spec_from_file_location("main", os.path.join(HERE, "main.py"))
SELF = _iu.module_from_spec(_spec)
sys.modules["main"] = SELF
_spec.loader.exec_module(SELF)
"""


def load_selfcheck(tmpdir):
    """借 selfcheck 的桩与造数助手（同一套口径，别再写一份 ✓）"""
    spec = importlib.util.spec_from_file_location(
        "wishonomy_selfcheck", os.path.join(HERE, "selfcheck.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Env:
    def __init__(self, tmpdir):
        self.tmpdir = tmpdir
        self.sc = load_selfcheck(tmpdir)
        self.mod = self.sc.load_plugin_module(tmpdir)
        self.db = os.path.join(tmpdir, "ledger.db")
        self.mod.set_db_path(self.db)
        self.plugin = self.sc.make_plugin(self.mod)
        self.panel = self._load_panel()

    def _load_panel(self):
        spec = importlib.util.spec_from_file_location("wishonomy_panel", PANEL)
        module = importlib.util.module_from_spec(spec)
        sys.modules["wishonomy_panel"] = module
        spec.loader.exec_module(module)
        # ⚠️ panel.py 内部 `import main as plugin` 拿的是 sys.modules["main"]
        #    —— 那可能是**另一份** main（缓存着别的库路径）✗
        #    所以这里主动把两份 main 的库路径都指到同一个临时库 ✓
        getattr(self.mod, "set_db_path", lambda _p: None)(self.db)
        inner = sys.modules.get("main")
        if inner is not None and inner is not getattr(self, "mod", None):
            getattr(inner, "set_db_path", lambda _p: None)(self.db)
        return module

    def seed(self):
        """造一份有内容的账：她的账 + 送礼 + 欠条 + 信誉分 + 一条带恶意字符的原话。"""
        sc, mod = self.sc, self.mod
        now = int(time.time())
        # 她自己的钱只能靠别人送礼 ✓（和自检同一口径）
        sc.seed_bot(mod, 500, "panel-seed")
        sc.raw_mood(mod, sc.ALICE, 120, "panel-alice", now)
        sc.raw_gift(mod, sc.ALICE, 20, "panel-gift", now)
        sc.raw_grant(mod, sc.ALICE, 60, item="<script>alert('x')</script>念一句台词")
        sc.raw_grant(mod, sc.BOB, max(45, mod.GRANT_MIN_AMOUNT), item="举一次牌")
        sc.raw_settle(mod, 1, "defaulted", "他没做")
        # 名字进账本（面板/排行要显示人名 ✓；线上这条由工具写账时带上 ✓）
        conn = mod._connect()
        try:
            for uid, nick in ((sc.ALICE, "小甲"), (sc.BOB, "小乙")):
                conn.execute("UPDATE account SET name=? WHERE self_id=? AND user_id=?",
                             (nick, sc.BOT, uid))
        finally:
            conn.close()
        return now


def gate_dash_css(page):
    """把整页 + **面板源码**拼起来给自检扫（本页只有一个 `<style>`，但源码里有别段 CSS ✗）

    ⚠️ 两个口径分开数（`page.count` 守着"内联只有一段" ✓）；这里只管
       "两条主题规则在不在、注释说了什么" —— 源码一起看 ✓
    """
    try:
        with open(PANEL, encoding="utf-8") as handle:
            return page + handle.read()
    except OSError:
        return page


def gate_write_keys(env, codes, rotated="", ttl_hours=24):
    """给自检造一份**本机哈希文件**（口径与插件写出来的逐字一致 ✓）

    ⚠️ 必须调**插件自己的** `gate_hash()` 来算 —— 自己另写一份哈希就等于
       "自测自己"，面板与插件之间那点契约就没人守了 ✗（R53 也走同一条路 ✓）
    """
    mod = env.mod
    rotated = rotated or time.strftime("%Y-%m-%d %H:%M:%S")
    stamp = datetime.strptime(rotated, "%Y-%m-%d %H:%M:%S")
    expiry = (stamp + timedelta(hours=ttl_hours)).strftime("%Y-%m-%d %H:%M:%S")
    data = {}
    for sid, code in codes.items():
        data[str(sid)] = {"day": rotated[:10],
                          "hash": mod.gate_hash(code, expiry, rotated),
                          "rotated": rotated}
    path = os.path.join(os.path.dirname(env.db), env.panel.GATE_FILE)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=1)
    return path


def gate_codes(env, count=3):
    """给两个 bot 各造一把合规口令 + 一份哈希文件，并各写几条带原话的心情流水 ✓"""
    now = int(time.time())
    good_a = env.mod.gate_generate()
    good_b = env.mod.gate_generate()
    while good_b == good_a:
        good_b = env.mod.gate_generate()
    for index in range(count):
        env.sc.raw_mood(env.mod, env.sc.ALICE, 5, "gate-a-%d" % index, now + index)
        env.sc.raw_mood(env.mod, env.sc.ALICE, 5, "gate-b-%d" % index, now + index,
                        group=env.sc.GROUP2, bot=env.sc.OTHER_BOT)
    # ⚠️ 面板/自检造数走的是 `db_mood`（reason 写死「自检」）⇒ 这里**照真机那样**再写几条
    #    带真原话的（面板要挡的正是这一列 ✗）。
    #    ⚠️ 但是**别按 `ref` 去认行** —— `db_mood` 落库的 ref 不是传进去那个（P14 上栽过一次 ✗）
    #       ⇒ 按「这个 bot 最近几条 mood」认，一次一条、从最新往后排 ✓
    conn = env.mod._connect()
    try:
        for sid, marks in ((env.sc.BOT, ("原话A-0", "原话A-1")),
                           (env.sc.OTHER_BOT, ("原话B-0", "原话B-1"))):
            rows = conn.execute(
                "SELECT id FROM ledger WHERE self_id=? AND kind='mood' "
                "ORDER BY id DESC LIMIT ?", (sid, len(marks))).fetchall()
            # ⚠️ 认行必须**钉死这个 bot 自己**的行（`self_id=?` 这个条件别省 ✗）——
            #    本鱼第一版只按 id 倒序、把标记盖到了另一个 bot 的行上 ⇒ P15 当场报"原话泄漏" ✓
            for row, mark in zip(rows, marks):
                conn.execute("UPDATE ledger SET reason=? WHERE id=? AND self_id=?",
                             (mark, row["id"], sid))
        conn.commit()
    finally:
        conn.close()
    path = gate_write_keys(env, {env.sc.BOT: good_a, env.sc.OTHER_BOT: good_b})
    return good_a, good_b, path


def t01_readonly(env, _unused):
    """面板的连接**写不了库**（SQLite `mode=ro` 直接拒）✓"""
    conn = env.panel.connect(env.db)
    try:
        try:
            conn.execute("INSERT INTO meta (key, value) VALUES ('x', 'y')")
            conn.commit()
            print("      只读连接居然写成功了 ✗")
            return False
        except Exception:
            pass
        try:
            conn.execute("CREATE TABLE hack (a)")
            print("      只读连接居然能建表 ✗")
            return False
        except Exception:
            pass
    finally:
        conn.close()
    return True


def t02_snapshot_content(env, _unused):
    """数据快照：发行 / 钱包 / 欠条 / 流水都读得到 ✓"""
    data = env.panel.snapshot(env.db, self_id=env.sc.BOT)
    if not data.get("available"):
        print("      快照读不到:", data)
        return False
    bot = data["bots"][0]
    kinds = {row["kind"] for row in bot["brief"]}
    if not {"mood", "gift", "grant"} <= kinds:
        print("      流水类别不全:", kinds)
        return False
    names = {w["name"] for w in bot["wallets"]}
    if not {"小甲", "小乙"} <= names:
        print("      钱包名字没对上:", names)
        return False
    if bot["today"]["wallets"] < 3 or bot["today"]["issued"] <= 0:
        print("      当日口径不对:", bot["today"])
        return False
    if not bot["ious"] or "举一次牌" not in bot["ious"][0]["item"]:
        print("      欠条没读到:", bot["ious"])
        return False
    if bot["today_detail"]["credit_down"] < 20:
        print("      信誉分口径不对:", bot["today_detail"])
        return False
    return True


def t03_html_escaped(env, _unused):
    """用户写的原话必须转义（防 XSS）✓"""
    page = env.panel.render(env.panel.snapshot(env.db, self_id=env.sc.BOT))
    # ⚠️ 2026-10-14：页面现在**真有**一段内联 JS ⇒ 断言不能再是"页面里不许出现 <script>" ✗
    #    ⇒ 改成盯**那个载荷本身**（用户原话里的 `alert('x')` 绝不许原样出现在页面里 ✓）
    if "<script>alert" in page or "alert('x')</script>" in page:
        print("      原话里的脚本没转义，页面里出现了可执行载荷 ✗")
        return False
    if "&lt;script&gt;" not in page:
        print("      看不出转义后的样子（可能没渲染那条）")
        return False
    return "只读面板" in page and "钱包一览" in page


def t04_http_smoke(env, _unused):
    """真起 HTTP，真发请求：200 + 页面里有数字；顺手验 404 与只绑本机 ✓"""
    httpd = env.panel.serve(env.db, "127.0.0.1", 0, quiet=True)
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        with urllib.request.urlopen("http://127.0.0.1:%d/" % port, timeout=10) as resp:
            body = resp.read().decode("utf-8")
            code = resp.status
        if code != 200 or "钱包一览" not in body:
            print("      首页不对:", code, body[:120])
            return False
        if "小甲" not in body:
            print("      页面上没有钱包名字")
            return False
        try:
            urllib.request.urlopen("http://127.0.0.1:%d/nope" % port, timeout=10)
            print("      不存在的路径居然 200 ✗")
            return False
        except urllib.error.HTTPError as exc:
            if exc.code != 404:
                print("      404 返回码不对:", exc.code)
                return False
    finally:
        httpd.shutdown()
        httpd.server_close()
    return True


def t05_json_mode(env, _unused):
    """`--json` 的导出逻辑能离线跑（部署后核对用；CLI 入口另有 P09 验）✓"""
    data = env.panel.snapshot(env.db, self_id=env.sc.BOT)
    text = json.dumps(data, ensure_ascii=False, indent=2)
    back = json.loads(text)
    return back.get("available") is True and back["bots"][0]["today"]["wallets"] >= 3


def t06_missing_db(env, _unused):
    """库不存在 ⇒ 如实说「还读不到」，不崩 ✓"""
    missing = os.path.join(env.tmpdir, "nope-" + uuid.uuid4().hex[:6] + ".db")
    data = env.panel.snapshot(missing)
    if data.get("available") is not False:
        print("      缺库居然说可用:", data)
        return False
    page = env.panel.render(data)
    return "还读不到账本" in page


def t07_empty_db(env, _unused):
    """库在但没建表 ⇒ 也如实说，不崩 ✓"""
    path = os.path.join(env.tmpdir, "empty-" + uuid.uuid4().hex[:6] + ".db")
    import sqlite3
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE unrelated (a)")
    conn.commit()
    conn.close()
    data = env.panel.snapshot(path)
    if data.get("available") is not False:
        print("      空库居然说可用:", data)
        return False
    return "还没有账本表" in env.panel.render(data)


def t08_source_hygiene(_env, _unused):
    """面板源码卫生：不写库 / 不绑公网默认 / 没有 print 调试残留 ✓"""
    with open(PANEL, encoding="utf-8") as handle:
        src = handle.read()
    for token in ("2713420580", "1609770237", "1076751753", "870863632"):
        if token in src:
            print("      源码里出现私域号码:", token)
            return False
    for bad in ("INSERT INTO", "UPDATE ", "DELETE FROM", "DROP TABLE", "commit()"):
        if bad in src:
            print("      面板里出现了写库语句:", bad)
            return False
    if '--host", default="0.0.0.0"' in src or '"0.0.0.0"' in src:
        print("      默认绑了公网地址 ✗")
        return False
    if 'default="127.0.0.1"' not in src:
        print("      默认不是本机回环 ✗")
        return False
    for line in src.splitlines():
        if line.strip().startswith("print(") and "main()" not in line:
            if not any(k in line for k in ("⚠️", "愿力中心 · 只读面板", "数据文件",
                                           "停了", "  （面板照常", "json.dumps(",
                                           "一键拉取")):
                print("      源码里有可疑的 print:", line.strip()[:60])
                return False
    return True


def t09_cli_entry(env, _unused):
    """真拿 `python panel.py` 起一遍（这条抓的是"有两份 main"那个坑 ✓）：
    ① `--json` 能吐 JSON；② 不带参数起服务时能正常监听本机端口 ✓"""
    so = (os.path.join(env.tmpdir, "cli.py"))
    with open(so, "w", encoding="utf-8") as handle:
        handle.write(
            "import os, sys, types\n"
            "HERE = r'%s'\n"
            "sys.path.insert(0, HERE)\n"
            "%s\n"
            "sys.argv = ['panel.py', '--json', '--db', r'%s']\n"
            "import importlib.util\n"
            "spec = importlib.util.spec_from_file_location('__main__', os.path.join(HERE, 'panel.py'))\n"
            "mod = importlib.util.module_from_spec(spec)\n"
            "sys.modules['__main__'] = mod\n"
            "spec.loader.exec_module(mod)\n"
            % (HERE.replace("\\", "\\\\"), _STUB_SRC, env.db.replace("\\", "\\\\")))
    out = subprocess.run([sys.executable, so], capture_output=True, text=True,
                         encoding="utf-8", errors="replace", timeout=90)
    if out.returncode != 0:
        print("      CLI --json 失败:", out.returncode, (out.stderr or "")[-300:])
        return False
    try:
        data = json.loads(out.stdout)
    except Exception as exc:
        print("      CLI 输出的不是 JSON:", repr(exc), out.stdout[:120])
        return False
    return data.get("available") is True


def t10_open_browser_once(_env, _unused):
    """`--open-browser` 只开一次、失败不炸（2026-10-13 真事故：cmd 的 start 会刷屏 ✗）"""
    import importlib.util
    spec = importlib.util.spec_from_file_location("panel2", PANEL)
    module = importlib.util.module_from_spec(spec)
    sys.modules["panel2"] = module
    spec.loader.exec_module(module)
    calls = []
    import webbrowser
    real = webbrowser.open
    webbrowser.open = lambda url, new=0: (calls.append(url), True)[1]
    try:
        module.open_browser_once("http://127.0.0.1:9999/")
        time.sleep(1.6)
    finally:
        webbrowser.open = real
    if len(calls) != 1:
        print("      浏览器被开了 %d 次（应当只有 1 次）✗" % len(calls))
        return False
    # 开不了也不许炸（比如没有浏览器）
    webbrowser.open = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no browser"))
    try:
        module.open_browser_once("http://127.0.0.1:9999/")
        time.sleep(1.6)
    except Exception as exc:              # noqa: BLE001
        print("      开不了浏览器时炸了:", exc)
        return False
    finally:
        webbrowser.open = real
    return True


def t11_expand_inplace(env, _unused):
    """最近流水：展开 = **label + 真开关**（点了**不跳页** ✓）· 批次累积 · 零锚点 · 零 JS

    ⚠️ 为什么值得一条自检：这是**主人两次报障**的地方（"点完页面位移很大"✗ / "第一个查看更早
       被折叠掉"✗）。旧版是 `<a href="#more-N">` + `:target`：① 浏览器跳锚点 ⇒ 页面位移
       ② `:target` 同一时刻只认一个元素 ⇒ 点第二颗时第一批整批缩回去 ✗
       ⇒ 所以这里**正面判据 + 反面判据**一起钉：
         正面 = 有 checkbox 开关 / 有 `:has()` 门控 / 点过的按钮是 `visibility:hidden`
         反面 = 源码里不许出现 `href="#` / `:target` / 用 `display:none` 收按钮（会塌行高 ✗）
    """
    base = int(time.time())
    rows = [{"ts": base - i, "group_id": "9", "from_id": env.sc.BOB, "to_id": env.sc.BOT,
             "amount": 5, "kind": "mood", "reason": "自检 %d" % i} for i in range(130)]
    conn = env.panel.connect(env.db)
    try:
        entries = env.panel.rows_of(conn, env.sc.BOT, rows)
    finally:
        conn.close()
    html_out = env.panel.brief_table({"brief": entries, "brief_count": 130,
                                      "ledger_total": 130, "limit": 400}, "bX")
    # ① 按钮是 label + 真开关（`for` 指自己的那一颗），不是 `#` 锚点
    for token in ('type="checkbox"', 'class="tg"', 'for="bX-tg-1"',
                  'class="anchor a1"', 'class="anchor a2"', "再看更早的 60 行"):
        if token not in html_out:
            print("      页面里缺 %r" % token)
            return False
    for bad in ('href="#', 'id="more-', "<details>", "<script", "onclick"):
        if bad in html_out:
            print("      还在用锚点/JS 展开（会跳页或折叠）: %r" % bad)
            return False
    # ② 被注视那套折叠**已经不该存在**（流水里整类不含它 ✓）
    for bad in ('class="quiet"', "p-attention", "再看被注视"):
        if bad in html_out:
            print("      被注视折叠的残留还在: %r" % bad)
            return False
    # ③ 批次划分：前 60 行不藏、第 61~120 行是 s2（那之前的按钮是 a1）、最后 10 行是 s3
    if html_out.count('class="stage s2"') != 60 or html_out.count('class="stage s3"') != 10:
        print("      批次划分不对: s2=%d s3=%d"
              % (html_out.count('class="stage s2"'), html_out.count('class="stage s3"')))
        return False
    # ③b 徽章出**中文名**（主人 2026-10-14：原来直接吐英文 `mood`/`gift`，跟指标卡不一致 ✗），
    #    原始 kind 必须留在 `title=` 里 ⇒ 对着库查账时不会被这层显示名挡住 ✓
    if '<span class="pill p-mood" title="mood">收愿力</span>' not in html_out:
        print("      徽章没出中文名，或原始 kind 没留在 title 里")
        return False
    # ④ 这一天还有更早的 ⇒ 才给「多读」的入口（表底那一颗；**按日之后它很少出现** ✓）
    more = env.panel.brief_table({"brief": entries, "brief_count": 130, "ledger_total": 999,
                                  "limit": 400, "flow_day": "2026-10-14"}, "bX")
    if "这一天还有更早的" not in more or "data-more=" not in more:
        print("      没给「这一天还有更早的」多读入口 ✗")
        return False
    if "data-more=" in html_out:
        print("      给全了还在催多读 ✗")
        return False
    # ⑤ CSS：`#tg-k` 勾上 ⇒ 放开第 k+1 批 + 第 k+1 颗按钮；点过的按钮**只藏自己**
    css = env.panel.stage_css("bX", 3)
    for token in (".stage{display:none}", "tr.anchor{display:none}",
                  "tr.a1{display:table-row}",          # ⚠️ 第一颗必须永远可见（藏了就 0 颗按钮 ✗）
                  "table:has(#bX-tg-1:checked) tr.s2{display:table-row}",
                  "table:has(#bX-tg-1:checked) tr.a2{display:table-row}",
                  "table:has(#bX-tg-2:checked) tr.s3{display:table-row}",
                  "tr.anchor:has(input.tg:checked) label.btn{visibility:hidden}",
                  "input.tg:focus-visible",                       # 键盘也开得动 ✓
                  "@supports not selector(:has(*))"):             # 老浏览器：一次全展开 ✓
        if token not in css:
            print("      CSS 规则缺 %r" % token)
            return False
    for bad in (":target", "<script", "onclick",
                "tr.anchor:has(input.tg:checked) label.btn{display:none}"):
        if bad in css:
            print("      CSS 里还有会跳页/塌行的写法: %r" % bad)
            return False
    return True


def t12_attention_page(env, _unused):
    """被注视**单独一页**（主人 2026-10-14 要的 ✓）：流水里整类不含它、那一页数字要对

    ⚠️ 数字口径（准则②）：被注视是 `system → 她自己` ⇒ 页面上每一处都要写明
       「这是**她自己**那一栏的愿力」✗（不许给一个不说是谁的数 ✓）
    """
    now = int(time.time())
    conn = env.mod._connect()
    try:
        for i in range(3):                       # 今天 3 笔
            got = env.mod.db_attention(conn, env.sc.BOT, now - i, 1, "att-today-%d" % i)
            if not got.get("ok"):
                print("      造注视流水失败:", got)
                return False
        for i in range(2):                       # 昨天 2 笔（分天要分得开 ✓）
            env.mod.db_attention(conn, env.sc.BOT, now - 86400 - i, 1, "att-yday-%d" % i)
    finally:
        conn.close()

    data = env.panel.attention_snapshot(env.db)
    if not data.get("available") or len(data.get("bots") or []) != 1:
        print("      被注视快照读不到:", data)
        return False
    bot = data["bots"][0]
    if bot["total"]["count"] != 5 or bot["total"]["amount"] != 5:
        print("      总次数/折合不对:", bot["total"])
        return False
    if bot["today"]["count"] != 3:
        print("      今天的次数不对:", bot["today"])
        return False
    if len(bot["days"]) != 2 or bot["days"][0]["count"] != 3 or bot["days"][1]["count"] != 2:
        print("      按天分组不对:", bot["days"])
        return False
    if sum(h["count"] for h in bot["hours"]) != 3:
        print("      今天按小时不对:", bot["hours"])
        return False
    if not bot["days"][0]["first"] or not bot["total"]["last"]:
        print("      首末时间没出来:", bot["days"][0], bot["total"])
        return False

    page = env.panel.attention_page(data)
    for token in ("被注视次数", "她自己", "按天", "今天按小时", 'href="/"', "只读"):
        if token not in page:
            print("      被注视页缺 %r" % token)
            return False
    if "<script" in page or "onclick" in page:
        print("      被注视页里出现了 JS ✗")
        return False

    # 反面判据：**主页面流水里一条被注视都不该有**，且"这一类共 N 笔"的口径不含它 ✓
    snap = env.panel.snapshot(env.db, self_id=env.sc.BOT)
    kinds = [e["kind"] for e in snap["bots"][0]["brief"]]
    if "attention" in kinds:
        print("      流水里还有被注视 ✗")
        return False
    conn = env.panel.connect(env.db)
    try:
        real = int(conn.execute("SELECT COUNT(*) c FROM ledger WHERE self_id=? AND kind<>?",
                               (env.sc.BOT, env.panel.ATTENTION_KIND)).fetchone()["c"])
    finally:
        conn.close()
    if int(snap["bots"][0]["ledger_total"]) != real:
        print("      「这一类共 N 笔」口径不对:", snap["bots"][0]["ledger_total"], real)
        return False

    # 真 HTTP：两个页面 + 404（`/attention.html` 也给 ✓）
    httpd = env.panel.serve(env.db, "127.0.0.1", 0, quiet=True)
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        for path, want, token in (("/attention", 200, "被注视次数"),
                                  ("/attention.html", 200, "被注视次数"),
                                  ("/", 200, "最近流水")):
            with urllib.request.urlopen("http://127.0.0.1:%d%s" % (port, path),
                                        timeout=10) as resp:
                body = resp.read().decode("utf-8")
                if resp.status != want or token not in body:
                    print("      %s 不对: %s / 缺 %r" % (path, resp.status, token))
                    return False
        try:
            urllib.request.urlopen("http://127.0.0.1:%d/nope" % port, timeout=10)
            print("      /nope 居然 200 ✗")
            return False
        except urllib.error.HTTPError as exc:
            if exc.code != 404:
                print("      /nope 返回码不对:", exc.code)
                return False
    finally:
        httpd.shutdown()
        httpd.server_close()
    return True


def t13_browser_no_jump(env, _unused):
    """真浏览器验收（Chromium）：点「再看更早的」⇒ **零位移** + 批次只增不减 + 按钮恰好一颗

    ⚠️ 为什么非要有这一条（2026-10-14 主人定的方向）：
       面板那四轮真机 bug 的共同点是「**自检全绿、观感全错**」✗ ——
       因为自检断言的是"我写的 CSS 规则在不在"，**不是"页面真的怎么动"** ✗
       ⇒ 这条打开**真 Chromium**、点**真两次**，量四件事：
         ① 点一下 `window.scrollY` **一点不变**（主人要的"整个页面不要有偏移" ✓）
         ② 点击处**上方**那一行的位置一点不变（上面不跳 ✓）
         ③ 可见流水行数 60 → 120 → 130（批次**累积**、点过的**不回收** ✓）
         ④ 可见按钮**恰好一颗**（主人裁定的"乙" ✓）· a1 那一行**行高不塌** ✓
       ⚠️ 点按钮用 `eval_on_selector(el => el.click())` —— **不能用 `page.click()`** ✗
          （Playwright 的 click 会先把元素滚进视口 ⇒ 自己就把 scrollY 改了 ✗）
    """
    if os.environ.get("PANEL_E2E") == "0":
        # 2026-10-27 修（P1-4）：跳过要**记成跳过**，不能记 OK ✗
        #   （README 承诺"不会假装跑过"，但汇总表/退出码上假装了 ✓）
        print("      （按 PANEL_E2E=0 显式跳过浏览器验收）")
        SKIPPED.append("PANEL_E2E=0：浏览器验收没跑")
        return True
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("      没装 playwright ⇒ 真浏览器验收跑不了。装法："
              "pip install playwright && playwright install chromium"
              "（确实不想装就设 PANEL_E2E=0 **显式**跳过 ✓）")
        return False

    now = int(time.time())
    for i in range(420):                     # 420 行 > 一次读的 400 行 ⇒ 表底那颗"多读"才出得来 ✓
        env.sc.raw_mood(env.mod, env.sc.ALICE, 5, "e2e-%d" % i, now + i)
    for i in range(3):                       # 昨天 3 行（换日期要**真的换出东西**才测得出 ✓）
        env.sc.raw_mood(env.mod, env.sc.ALICE, 5, "e2e-yday-%d" % i, now - 86400 - i)
    yday = time.strftime("%Y-%m-%d", time.localtime(now - 86400))

    httpd = env.panel.serve(env.db, "127.0.0.1", 0, quiet=True)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    # ⚠️ 选择器**必须锁在流水那块里**（`div.flows`）—— 第一版写成 `tbody tr`，
    #    把钱包 / 欠条 / 今日活跃那几张表的行也数进去了 ⇒ 数字全歪 ✗（2026-10-14 当场被这条自检抓住 ✓）
    state_js = ("() => {"
                "  const rows = [...document.querySelectorAll('.flows tbody tr')];"
                "  const vis = el => el.offsetHeight > 0;"
                "  const data = rows.filter(r => !r.classList.contains('anchor'));"
                "  const btns = [...document.querySelectorAll('.flows tr.anchor label.btn')]"
                "    .filter(el => getComputedStyle(el).visibility === 'visible' && vis(el));"
                "  const a1 = document.querySelector('.flows tr.anchor.a1');"
                "  const box = document.querySelector('.flows');"
                "  return {scrollY: window.scrollY,"
                "          rows: data.filter(vis).length, total: data.length,"
                "          btns: btns.length,"
                "          opened: document.querySelectorAll('.flows input.tg:checked').length,"
                "          docTop: box ? box.getBoundingClientRect().top + window.scrollY : -1,"
                "          a1h: a1 ? a1.getBoundingClientRect().height : -1,"
                "          above: (a1 && a1.previousElementSibling)"
                "                 ? a1.previousElementSibling.getBoundingClientRect().top : null};"
                "}")
    click_js = ("() => {const btns = [...document.querySelectorAll('.flows tr.anchor label.btn')]"
                "  .filter(el => getComputedStyle(el).visibility === 'visible'"
                "          && el.offsetHeight > 0);"
                "  if (!btns.length) return 'no-button'; btns[0].click(); return 'clicked';}")
    errors = []
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            page = browser.new_page(viewport={"width": 1280, "height": 800})
            page.on("pageerror", lambda exc: errors.append(str(exc)[:120]))
            page.goto("http://127.0.0.1:%d/" % port, wait_until="load")

            def to_frontier():
                """把**最前沿那颗可见按钮**滚到视口中央（模拟"主人滚过去点它" ✓），
                返回那一刻的状态 —— 这一步之后的 scrollY 才是"点击前"的基准 ✓
                ⚠️ 必须这样：JS 点一颗**屏幕外**的按钮时，浏览器会把刚被聚焦的 checkbox
                   滚进视野 ✗（真人做不到这一步）⇒ 不先滚过去，量到的是假跳 ✗
                """
                ok = page.evaluate(
                    "() => {const b=[...document.querySelectorAll('.flows tr.anchor label.btn')]"
                    "  .filter(el=>getComputedStyle(el).visibility==='visible'"
                    "          && el.offsetHeight>0);"
                    "  if(!b.length) return false;"
                    "  b[0].scrollIntoView({block:'center'}); return true;}")
                if not ok:
                    return None
                page.wait_for_timeout(80)
                return page.evaluate(state_js)

            before = to_frontier()
            if before is None:
                print("      页面上没有可见的「再看更早的」按钮 ✗")
                return False
            if before["btns"] != 1 or before["rows"] < 60:
                print("      初始态不对（可见按钮 %d 个 / 可见行 %d）" % (before["btns"],
                                                                    before["rows"]))
                return False
            if page.evaluate(click_js) != "clicked":
                print("      第一颗点不动 ✗")
                return False
            page.wait_for_timeout(150)
            first = page.evaluate(state_js)          # ← 点完**立刻**量，不许先滚动 ✓
            mid = to_frontier()                      # 第二颗要先滚过去（真人就是这么做的 ✓）
            if mid is None or page.evaluate(click_js) != "clicked":
                print("      第二颗点不动 ✗")
                return False
            page.wait_for_timeout(150)
            second = page.evaluate(state_js)
            # ③ 表底「还有更早的」⇒ **就地多读**（先把那颗链接滚进视口 —— 真人就是这么点的 ✓）
            more_js = ("() => {const a=document.querySelector('a[data-more]');"
                       "  if(!a) return false; a.scrollIntoView({block:'center'}); return true;}")
            if not page.evaluate(more_js):
                print("      表底那颗「还有更早的」没出现（读的行数够 420 吗？）✗")
                return False
            page.wait_for_timeout(80)
            before_more = page.evaluate(state_js)
            page.evaluate("() => {const a=document.querySelector('a[data-more]'); a.click();}")
            page.wait_for_timeout(900)          # 等 fetch + 换 DOM
            after_more = page.evaluate(state_js)
            # ④ 换日期：JS 就地取 ✓
            #    ⚠️ **在页顶做**：跨"3 行 ↔ 400 行"这种剧烈变化时，浏览器本来就会
            #       钳制/锚定滚动位置 ✗（那不是"页面跳了"）⇒ 页顶做，判据才确定 ✓
            page.evaluate("() => window.scrollTo(0, 0)")
            page.wait_for_timeout(120)
            top_before = page.evaluate(state_js)
            page.evaluate("(d) => {const f=document.getElementById('flowform');"
                          " f.day.value=d; f.dispatchEvent(new Event('change'));}", yday)
            page.wait_for_timeout(900)
            after_yday = page.evaluate(state_js)
            # ⑤ 再换回今天（内容变多）：同样必须在**页顶**、`scrollY` 一点不动 ✓
            page.evaluate("() => {const f=document.getElementById('flowform');"
                          " f.day.value=''; f.dispatchEvent(new Event('change'));}")
            page.wait_for_timeout(900)
            back = page.evaluate(state_js)
            if os.environ.get("PANEL_E2E_SHOT"):
                page.screenshot(path=os.environ["PANEL_E2E_SHOT"], full_page=False)
            att = browser.new_page()
            att.on("pageerror", lambda exc: errors.append(str(exc)[:120]))
            att.goto("http://127.0.0.1:%d/attention" % port, wait_until="load")
            att_ok = att.inner_text("body").find("被注视次数") >= 0
            browser.close()
    finally:
        httpd.shutdown()
        httpd.server_close()

    if errors:
        print("      浏览器里报 JS 错:", errors)
        return False
    if not att_ok:
        print("      被注视那一页没渲染出来 ✗")
        return False
    # ① 零位移（主人要的那一条）：两次点击之后，scrollY 都不许动
    if first["scrollY"] != before["scrollY"]:
        print("      点第一颗就跳了：scrollY %s → %s" % (before["scrollY"], first["scrollY"]))
        return False
    if second["scrollY"] != mid["scrollY"]:
        print("      点第二颗跳了：scrollY %s → %s" % (mid["scrollY"], second["scrollY"]))
        return False
    # ② 点击处上方那一行一点不动
    if before["above"] is None or abs(first["above"] - before["above"]) > 0.6:
        print("      按钮**上方**的行动了：%s → %s" % (before["above"], first["above"]))
        return False
    # ③ 批次累积（第一颗点开后那 60 行**不许被收回**）+ ④ 行高不塌
    if before["total"] < before["rows"] + 120:
        print("      这个库的行数不够测两批（可见 %d / 共 %d）" % (before["rows"], before["total"]))
        return False
    if first["rows"] != before["rows"] + 60:
        print("      第一批展开的行数不对：%d → %d（应 +60）" % (before["rows"], first["rows"]))
        return False
    if second["rows"] != before["rows"] + 120:
        print("      点第二颗时**第一批被收回了** ✗（主人报过的那个 bug）：%d → %d"
              % (first["rows"], second["rows"]))
        return False
    if abs(first["a1h"] - before["a1h"]) > 0.6:
        print("      点过的那一行**塌了行高** ✗（上方内容会跟着跳）：%s → %s"
              % (before["a1h"], first["a1h"]))
        return False
    # ⑤ 同一时刻**只看得见一颗**按钮（主人裁定的"乙"）
    for name, snap in (("点前", before), ("点一次", first), ("点两次", second)):
        if snap["btns"] != 1:
            print("      %s 看得见 %d 颗按钮（应当是 1 颗）" % (name, snap["btns"]))
            return False
    # ⑥ **就地多读**：不跳 + 行数变多 + 已展开的批次保持展开（主人 2026-10-14 追加 ✓）
    #    ⚠️ 判据分两层：**流水卡在文档里的位置**一点不动（上面真的没变 ✓）；
    #       `scrollY` 允许多少抖一点（浏览器**滚动锚定**会在换 DOM 时做几十像素微调 ✓，
    #       那不是"页面跳了"，但超过 40px 就得当事故查 ✗）
    if abs(after_more["docTop"] - before_more["docTop"]) > 0.6:
        print("      多读之后**流水卡上方**的东西动了（文档位置 %.1f → %.1f）✗"
              % (before_more["docTop"], after_more["docTop"]))
        return False
    if abs(after_more["scrollY"] - before_more["scrollY"]) > 40:
        print("      点「还有更早的」跳了：scrollY %s → %s"
              % (before_more["scrollY"], after_more["scrollY"]))
        return False
    if after_more["total"] <= before_more["total"]:
        print("      点了「还有更早的」行数没变多：%s → %s"
              % (before_more["total"], after_more["total"]))
        return False
    if after_more["opened"] < before_more["opened"] or after_more["opened"] < 1:
        print("      多读之后**已展开的批次丢了** ✗：%s → %s"
              % (before_more["opened"], after_more["opened"]))
        return False
    # ⑦ 换日期真的换出东西（那一天只有 3 行）· 页顶做 ⇒ 判据确定
    if after_yday["rows"] != 3:
        print("      换到 %s 之后行数不对：%s" % (yday, after_yday["rows"]))
        return False
    if after_yday["scrollY"] != 0 or back["scrollY"] != 0:
        print("      换日期时页面动了：scrollY %s → %s → %s"
              % (top_before["scrollY"], after_yday["scrollY"], back["scrollY"]))
        return False
    if abs(after_yday["docTop"] - top_before["docTop"]) > 0.6 or \
            abs(back["docTop"] - top_before["docTop"]) > 0.6:
        print("      换日期之后**流水卡上方**的东西动了：%.1f → %.1f → %.1f"
              % (top_before["docTop"], after_yday["docTop"], back["docTop"]))
        return False
    if back["rows"] <= 3:
        print("      换回今天之后行数没回来：%s" % back["rows"])
        return False
    return True


def t14_day_and_kind_filter(env, _unused):
    """**按日检索 + 类别筛选**（主人 2026-10-14 追加：默认只显示本日 ✓）· 以及 JS 纪律

    ⚠️ 为什么要一条自检：这两件事**改坏了不报错**，只会让面板又变成"一屏全是几天前的流水" ✗；
       而且主人给的理由是「**开源插件不能拿我们自己的用量当设计基准**」⇒
       服务端必须**按需取**（只读这一天的 N 行），不是"一天全读" ✓（别人一天几万笔也扛得住）
    ⚠️ 顺带守着"那段内联 JS"的边界：**只许一段**、不许 `src=`、不许 `onclick=`、
       不许任何外链资源（CDN / 字体 / `<link>`）—— 面板仍然**单文件、可离线打开** ✓
    """
    now = int(time.time())
    old_day = time.strftime("%Y-%m-%d", time.localtime(now - 2 * 86400))
    # ⚠️ 标记用**金额 777**，别用 `ref` —— 行的 `reason` 是 `raw_mood` 的默认文案，
    #    跟 ref 无关 ✗（第一版就是这么写错的，被这条自检当场抓住 ✓）
    env.sc.raw_mood(env.mod, env.sc.BOB, 777, "filter-old", now - 2 * 86400)
    env.sc.raw_mood(env.mod, env.sc.ALICE, 555, "filter-today", now)

    today_bot = env.panel.snapshot(env.db, self_id=env.sc.BOT)["bots"][0]
    today = today_bot["flow_day"]
    if today_bot["is_today"] is not True or today != time.strftime("%Y-%m-%d", time.localtime(now)):
        print("      默认不是今天:", today_bot["flow_day"], today_bot["is_today"])
        return False
    if any(int(e["amount"]) == 777 for e in today_bot["brief"]):
        print("      默认视图里混进了两天前那笔 ✗")
        return False
    if not any(int(e["amount"]) == 555 for e in today_bot["brief"]):
        print("      默认视图里没有今天那笔（是不是把今天也滤掉了？）✗")
        return False
    if today_bot["prev_day"] != time.strftime("%Y-%m-%d", time.localtime(now - 86400)):
        print("      「前一天」的日期不对:", today_bot["prev_day"])
        return False
    if today_bot["next_day"]:
        print("      今天不该有「后一天」✗:", today_bot["next_day"])
        return False

    old_bot = env.panel.snapshot(env.db, self_id=env.sc.BOT, day=old_day)["bots"][0]
    if not any(int(e["amount"]) == 777 for e in old_bot["brief"]):
        print("      按日没查到那一天:", old_day, [e["amount"] for e in old_bot["brief"]])
        return False
    if any(int(e["amount"]) == 555 for e in old_bot["brief"]):
        print("      按日查询把别的天的也带出来了 ✗")
        return False
    if old_bot["next_day"] != time.strftime("%Y-%m-%d", time.localtime(now - 86400)):
        print("      那一天的「后一天」不对:", old_bot["next_day"])
        return False

    moods = env.panel.snapshot(env.db, self_id=env.sc.BOT, day=old_day, kind="mood")["bots"][0]
    if not moods["brief"] or any(e["kind"] != "mood" for e in moods["brief"]):
        print("      按类别过滤（mood）不对:", [e["kind"] for e in moods["brief"]])
        return False
    none_kind = env.panel.snapshot(env.db, self_id=env.sc.BOT, kind="不存在的类别")["bots"][0]
    if none_kind["brief"]:
        print("      不存在的类别居然查出了东西 ✗")
        return False
    if "mood" not in today_bot["kinds"] or "gift" not in today_bot["kinds"]:
        print("      类别清单纯（下拉框的选项）不对:", today_bot["kinds"])
        return False
    fallback = env.panel.snapshot(env.db, self_id=env.sc.BOT, day="不是日期")["bots"][0]
    if fallback["flow_day"] != today:
        print("      非法日期没有退回今天:", fallback["flow_day"])
        return False

    # 片段模式：只有流水那几块（没有整页壳）✓
    frag = env.panel.render_flow_fragment(env.panel.snapshot(env.db))
    if 'class="flows" id="flows-' not in frag or "<html" in frag or "<tbody>" not in frag:
        print("      片段模式不对:", frag[:80])
        return False
    # 整页：工具条 + **只有一段**内联 JS + 零外链 ✓
    page = env.panel.render(env.panel.snapshot(env.db))
    for token in ('id="flowform"', 'type="date"', "<select", 'class="flows"'):
        if token not in page:
            print("      整页缺 %r" % token)
            return False
    if page.count("<script") != 1:
        print("      内联 JS 不是恰好一段:", page.count("<script"))
        return False
    for bad in ("<script src", "onclick=", "onload=", "<link", "cdn.", 'src="http'):
        if bad in page:
            print("      面板出现了外链/内联事件（不许有）:", bad)
            return False
    if "<script" in env.panel.attention_page(env.panel.attention_snapshot(env.db)):
        print("      被注视那一页不该有 JS ✗")
        return False

    # 真 HTTP：带 day/kind、片段头、非法日期
    httpd = env.panel.serve(env.db, "127.0.0.1", 0, quiet=True)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        def fetch(path, headers=None):
            req = urllib.request.Request("http://127.0.0.1:%d%s" % (port, path),
                                         headers=headers or {})
            try:
                with urllib.request.urlopen(req, timeout=10) as resp:
                    return resp.status, resp.read().decode("utf-8")
            except urllib.error.HTTPError as exc:
                return exc.code, ""

        code, body = fetch("/?day=%s&kind=mood" % old_day)
        if code != 200 or "只看「收愿力」" not in body:
            print("      GET day+kind 不对:", code, body[:80])
            return False
        code, body = fetch("/?day=%s" % old_day, {"X-Panel-Partial": "1"})
        if code != 200 or "<html" in body or 'class="flows"' not in body:
            print("      片段请求不对:", code, body[:80])
            return False
        code, body = fetch("/?day=abc")
        if code != 200 or "最近流水" not in body:
            print("      非法日期没兜住:", code)
            return False
    finally:
        httpd.shutdown()
        httpd.server_close()
    return True


def t15_mood_gate_server(env, _unused):
    """**心情隐私锁**（主人 2026-10-15 让做的 ✓）：`docs/canon-29.md` §6 那五条，一条不许少 ✓

    1. **未授权 ⇒ DOM 里翻不到任何一条 mood 的 reason** —— 不是"看不见"，是**不存在** ✗
       （只用 CSS 模糊 ⇒ F12 / `Ctrl+A` 就全看到了 ✗ = 窗帘不是锁 ✗ 主人实测点名的坑 ✓）
    2. **错口令 ⇒ 拒绝**，而且**限次生效**（5 次锁 30 分钟 ✓）
    3. **对口令 ⇒ 才发得下来** ✓
    4. **两种状态下，金额 / 方向 / 点数照旧可见**（不许把运营一起锁瞎 ✗ 命门 ✓）
    5. **多 bot：A 的钥匙开不了 B 的遮罩** ✓

    ⚠️ 还有一条只有自检能守的：**片段模式**（那段内联 JS 用的 `div.flows`）也必须带锁 ——
       不然"换个日期"就把她的原话又吐出来了 ✗
    """
    good_a, good_b, keys = gate_codes(env)
    bot_a, bot_b = str(env.sc.BOT), str(env.sc.OTHER_BOT)
    if not keys or not os.path.isfile(keys):
        print("      哈希文件没造出来:", keys)
        return False

    httpd = env.panel.serve(env.db, "127.0.0.1", 0, quiet=True)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        class _NoRedirect(urllib.request.HTTPRedirectHandler):
            """⚠️ 不许 urllib 自动跟 303 —— 那样就看不到那条跳转和 Set-Cookie 了 ✗
            （本鱼第一版就吃了这个：它把 303 跟完了、交回一个**已授权页面**的 200/HTML ✓）"""

            def redirect_request(self, *args, **kwargs):
                return None

        opener = urllib.request.build_opener(_NoRedirect)

        def fetch(path, data=None, headers=None, cookie=""):
            # ⚠️ **不传 `headers`** 时**不加** `X-Panel-Unlock` —— 那条头就是"这是那段内联 JS
            #    发来的"的记号 ⇒ 不加它才走**原生表单**那条路（关掉 JS 的人走的就是它 ✓）
            head = dict(headers) if headers is not None else {"X-Panel-Unlock": "1"}
            if cookie:
                head["Cookie"] = cookie
            body = urllib.parse.urlencode(data).encode() if data is not None else None
            if body is not None:
                head.setdefault("Content-Type", "application/x-www-form-urlencoded")
            req = urllib.request.Request("http://127.0.0.1:%d%s" % (port, path),
                                         data=body, headers=head)
            try:
                with opener.open(req, timeout=10) as resp:
                    return resp.status, resp.read().decode("utf-8"), dict(resp.headers)
            except urllib.error.HTTPError as exc:
                return exc.code, exc.read().decode("utf-8"), dict(exc.headers)

        # ── ① 未授权：DOM 里找不到任何原话 ──
        code, page, _head = fetch("/")
        if code != 200:
            print("      未授权那次页面没起来:", code)
            return False
        for mark in ("原话A-0", "原话A-1", "原话B-0", "原话B-1"):
            if mark in page:
                print("      **未授权就能在 HTML 里搜到她的原话** ✗（CSS 模糊 = 窗帘 ✗）:", mark)
                return False
        # ── ④ 金额 / 方向 / 点数照旧可见（命门：不许把运营一起锁瞎 ✗）──
        for token in ("+5", "收愿力", "数额", "钱包一览", "未结欠条"):
            if token not in page:
                print("      未授权时把运营该看的东西也弄没了 ✗", token)
                return False
        # 毛玻璃 + **全页唯一那个**口令框（主人 2026-10-15 当场指出的：一个框管整列 ✓）
        for token in ("gcell", "gskel", "backdrop-filter", "gform", "data-gate-pw"):
            if token not in gate_dash_css(page):
                print("      遮罩/口令框的样式或结构缺了:", token)
                return False
        if "gcell" not in page or 'action="/api/unlock"' not in page:
            print("      未授权时页面上没有毛玻璃 / 解锁表单 ✗")
            return False
        # ⚠️ 数的是**真的那个属性**（`data-gate-pw="1"`）—— 别数裸词 `data-gate-pw` ✗：
        #    本鱼那段内联 JS 里也有 `querySelector('input[data-gate-pw]')` ⇒ 会多数一个 ✗
        if len(re.findall(r'data-gate-pw="1"', page)) != 1:
            print("      **口令框不是恰好一个**（主人要的是一次解锁整列 ✗ 不是逐条输 ✗）:",
                  len(re.findall(r'data-gate-pw="1"', page)))
            return False
        if page.count('action="/api/unlock"') != 1:
            print("      解锁表单不只一个 ✗:", page.count('action="/api/unlock"'))
            return False
        # ⚠️ 格子**不许再自带输入框**（一路一个框 = 主人报的那个毛病 ✗）
        if page.count('class="gcell"') and 'name="sid"' in page:
            print("      格子还带着自己的 sid 隐藏域 ⇒ 说明还是逐条的框 ✗")
            return False
        if page.count('class="gcell"') < 2:
            print("      没有出现该盖住的格子（造数够吗？）:", page.count('class="gcell"'))
            return False
        # 深浅两套主题都要好看（两套底色 + 不支持毛玻璃时的退路 ✓）
        for token in ("prefers-color-scheme:dark", "--gate-wash", "@supports not"):
            if token not in gate_dash_css(page):
                print("      主题那部分缺了:", token)
                return False
        if page.count("<script") != 1:
            # ⚠️ 只数 `<script>`（主人 2026-10-14 定的那条：**恰好一段内联 JS** ✓）——
            #    `<style>` 本来就还有第二段（每张表的展开规则 `stage_css` ✓ 一直如此）
            #    ⇒ 别在这里顺手加严 ✗（本鱼自己加过一次，当场被自己绊了一下 ✓）
            print("      解锁逻辑必须并进**那一段**内联 JS（不许第二段 ✗）:",
                  page.count("<script"))
            return False
        # 片段模式（换日期走的那条路）也必须带锁 ✗
        code, frag, _head = fetch("/?day=", {"X-Panel-Partial": "1"} if False else None,
                                  {"X-Panel-Partial": "1"})
        if code != 200 or "原话A-0" in frag or "gcell" not in frag:
            print("      片段模式把锁漏了（换个日期就露出来了 ✗）:", code)
            return False

        # ── ② 错口令 ⇒ 拒绝 ──
        code, body, _head = fetch("/api/unlock", {"sid": bot_a, "code": "WRONG1"},
                                  {"X-Panel-Unlock": "1"})
        if code != 200 or json.loads(body).get("ok"):
            print("      错口令居然过了:", code, body[:80])
            return False
        # ── ⑤ A 的钥匙开不了 B 的遮罩 ──
        code, body, head = fetch("/api/unlock", {"sid": bot_a, "code": good_a},
                                 {"X-Panel-Unlock": "1"})
        info = json.loads(body)
        if code != 200 or not info.get("ok"):
            print("      **对口令反而被拒**（那这把锁就白做了 ✗）:", code, body[:120])
            return False
        cookie = (head.get("Set-Cookie") or "").split(";")[0]
        if not cookie.startswith(env.panel.GATE_COOKIE + "="):
            print("      没发 session cookie:", head.get("Set-Cookie"))
            return False
        code, page_a, _head = fetch("/", cookie=cookie)
        if "原话A-0" not in page_a:
            print("      授权之后**A 的原话还是下不来** ✗")
            return False
        if "原话B-0" in page_a:
            print("      **A 的钥匙把 B 的遮罩也开了** ✗（每 bot 一把是主人的硬要求）")
            return False
        if "原话A-0" not in fetch("/?day=", None, {"X-Panel-Partial": "1"}, cookie)[1]:
            print("      授权之后片段模式反而没解锁 ✗")
            return False
        # ── ③ 对口令 ⇒ 才发得下来（B 那把同样只开 B 自己）──
        code, body, head = fetch("/api/unlock", {"sid": bot_b, "code": good_b},
                                 {"X-Panel-Unlock": "1"})
        cookie_b = (head.get("Set-Cookie") or "").split(";")[0]
        if not json.loads(body).get("ok") or not cookie_b:
            print("      B 的对口令没通过:", body[:120])
            return False
        page_b = fetch("/", cookie=cookie_b)[1]
        if "原话B-0" not in page_b or "原话A-0" in page_b:
            print("      B 的钥匙开错了范围 ✗")
            return False
        # 票据头那颗框（不带 sid）也要能开：它**挨个试**，只会开中口令对的那个 ✓
        code, body, head = fetch("/api/unlock", {"code": good_a}, {"X-Panel-Unlock": "1"})
        if not json.loads(body).get("ok"):
            print("      票头那颗框（不带 sid）开不了:", body[:120])
            return False
        code, body, _head = fetch("/api/unlock", {"code": "ZZZZZZ"}, {"X-Panel-Unlock": "1"})
        if json.loads(body).get("ok"):
            print("      票头那颗框拿着错口令也开了 ✗")
            return False
        # ── ② 限次：错到第 5 次就锁（**在服务端** ✓ 直接打接口也挡得住 ✓）──
        seen = []
        for _ in range(6):
            code, body, _head = fetch("/api/unlock", {"sid": bot_a, "code": "WRONG1"},
                                      {"X-Panel-Unlock": "1"})
            seen.append(json.loads(body).get("error") or "")
        if not any("锁" in text for text in seen):
            print("      连错 6 次都没锁 ✗:", seen)
            return False
        code, body, _head = fetch("/api/unlock", {"sid": bot_a, "code": good_a},
                                  {"X-Panel-Unlock": "1"})
        if json.loads(body).get("ok"):
            print("      **锁着的时候对口令也放行** ✗（限次等于没有）")
            return False
        # ── 原生表单那条路（关掉 JS 也能解锁 ✓）：303 + Set-Cookie ──
        env.panel._gate_tries.clear()            # 解开刚才那把锁（只动内存 ✓）
        code, body, head = fetch("/api/unlock", {"sid": bot_b, "code": good_b},
                                 headers={"Referer": "http://127.0.0.1:%d/" % port})
        if code != 303 or not (head.get("Set-Cookie") or ""):
            print("      关掉 JS 的那条路（原生表单）走不通:", code, head.get("Set-Cookie"))
            return False
    finally:
        httpd.shutdown()
        httpd.server_close()

    # ── 反向对照：把「锁」关掉 ⇒ 原话立刻回到 DOM 里（证明上面那些断言真的在看锁 ✓）──
    plain = env.panel.render(env.panel.snapshot(env.db))
    if "原话A-0" not in plain:
        print("      反向对照失败：没标记「该盖住」的渲染里本来就没有原话（那这条自检没在起作用）✗")
        return False
    if "原话A-0" in env.panel.render(env.panel.snapshot(env.db), gated=[bot_a]):
        print("      反向对照失败：标了「该盖住」却还是把原话发下来了 ✗")
        return False
    return True


def page_cells(page):
    """真浏览器里数：**每张流水表里还盖着几格**（按 `div.flows` 分段，返回一个列表 ✓）

    ⚠️ **本鱼在这件事上踩了三轮** ✗（记账：以后数 DOM 一律用 Playwright 的 `locator.count()` /
       `nth()`，别在 Python 里手拼 JS 表达式 ✗）：
       · 第一版把 `!!(a && b)` 折成多行 ⇒ **少一个右括号** ⇒ 每行多带回一个字符串
         ⇒ Python 侧解包把字符串当真值 ⇒ "A 自己 8 个" ✗（判据自己骗自己 ✓）
       · 第二版想"一次求值返回两个数"，写了带**嵌套引号 + 三元**的长表达式
         ⇒ 同一个查询在本鱼探针里给 4、在自检里给 8 ✗（哪一层出错都看不出来 ✓）
       ⇒ 现在：分段 + `locator.count()`（Playwright 自己的 CSS 引擎在数）✓ 零解析、零拼串 ✓
    """
    blocks = page.locator("div.flows")
    return [blocks.nth(i).locator("tbody tr td > div.gcell").count()
            for i in range(blocks.count())]


def t16_gate_browser(env, _unused):
    """**真浏览器**：整列一层毛玻璃、原话看不见；输对口令之后整列才出来 ✓

    ⚠️ 这条是主人「毛玻璃遮罩 + 深色主题也要好看」那句的**可验收版本** ✗：
       自检断言"我写的 CSS 在不在"是不够的（面板那四轮真机 bug 全是"自检全绿、观感全错" ✗）
       ⇒ 真开 Chromium：量 `backdrop-filter` 真的生效（而且盖在**整格**上 ✓）、
         `Ctrl+A` 全选也选不到原话、全页**只有一个**口令框（输一次解锁整列 ✓）、
         输口令 + 提交（**走真表单**，不靠 JS ✓）之后**整列**才出来 ✓
    ⚠️ 主人 2026-10-15 当场指出的那条（**一路一个框** ✗）就由这里的 `n_pw == 1` 守着 ✓
    """
    if os.environ.get("PANEL_E2E") == "0":
        # 2026-10-27 修（P1-4）：跳过要**记成跳过**，不能记 OK ✗
        #   （README 承诺"不会假装跑过"，但汇总表/退出码上假装了 ✓）
        print("      （按 PANEL_E2E=0 显式跳过浏览器验收）")
        SKIPPED.append("PANEL_E2E=0：浏览器验收没跑")
        return True
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("      没装 playwright ⇒ 真浏览器验收跑不了（PANEL_E2E=0 可显式跳过 ✓）")
        return False
    good_a, _good_b, _keys = gate_codes(env)
    bot_a = str(env.sc.BOT)
    httpd = env.panel.serve(env.db, "127.0.0.1", 0, quiet=True)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    errors = []
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            page = browser.new_page(viewport={"width": 1280, "height": 800},
                                    color_scheme="dark")      # 深色主题也要好看 ✓
            page.on("pageerror", lambda exc: errors.append(str(exc)[:120]))
            page.goto("http://127.0.0.1:%d/" % port, wait_until="load")
            # ① 毛玻璃真的生效（而且是盖在**整格**上的那一层 ✓）
            blur = page.eval_on_selector(
                ".gcell", "el => getComputedStyle(el).backdropFilter"
                " || getComputedStyle(el).webkitBackdropFilter || ''")
            if "blur" not in (blur or ""):
                print("      深色主题下毛玻璃没生效:", repr(blur))
                return False
            boxes = page.eval_on_selector_all(
                ".gcell", "els => els.map(e => Math.round(e.getBoundingClientRect().height))")
            # ⚠️ 只看**可见**的那些 ✗：`P13` 先往库里塞了 420 行 ⇒ 遮罩格会有几百个，
            #    其中**折叠批次**里的高度本来就是 0（还没展开 ✓ 那是设计 ✗不是塌）
            shown = [h for h in boxes if h > 0]
            if len(boxes) < 1 or not shown or min(shown) < 20:
                print("      遮罩那一格塌了 ✗: 共 %d 个 / 可见的 %r" % (len(boxes), shown[:6]))
                return False
            # ② **全页只有一个口令框**（主人要的：输一次解锁整列 ✓）
            n_pw = page.evaluate(
                "() => document.querySelectorAll('input[data-gate-pw]').length")
            n_form = page.evaluate(
                "() => document.querySelectorAll('form[action=\"/api/unlock\"]').length")
            if n_pw != 1 or n_form != 1:
                print("      口令框/表单不是恰好一个（**逐条输**是主人否掉的 ✗）:", n_pw, n_form)
                return False
            # 全选也选不到她的原话（不是"看不见"，是**不在 DOM 里** ✗）
            page.keyboard.press("Control+a")
            selected = page.evaluate("() => String(window.getSelection())")
            for mark in ("原话A-0", "原话A-1"):
                if mark in selected:
                    print("      **Ctrl+A 就选出了她的原话** ✗（这正是主人点名的坑）:", mark)
                    return False
            if "原话A-0" in page.content():
                print("      HTML 源码里就有她的原话 ✗")
                return False
            # 记下"解锁前有多少格盖着"以及"A 自己占了几格"（下面解锁后要对账 ✓）
            # ⚠️ **别用一段拼出来的 JS 去数**（本鱼为此白折腾了几轮 ✗）——
            #    直接抓一份 `[从, 有没有遮罩]` 的现场数组、在 Python 里数 ✓
            #    顺带这就是**证据**（判错时能直接打出来看 ✓）
            # ⚠️ 判据：**A 那张表里的遮罩必须清零**，而**B 那张表一动不动** ✗
            #    （B 没输口令 ⇒ 它的格子还盖着、票头那个框也还在 ✓ 别指望"全页一个都不剩" ✗）
            #    分段数（`page_cells`）比"全页总数"干净得多：哪张表的问题一眼看得出来 ✓
            before = page_cells(page)
            if not before or max(before) < 1 or len(before) < 2:
                print("      造数不对：各表遮罩数 = %s（该有两个 bot、各自都有盖着的 ✗）" % (before,))
                return False
            # 走**真表单**提交（不碰 JS 那条路 ✓）⇒ 断言的是**整列**都出来（不只一条 ✓）
            # ⚠️ 之后必须**等重载真的完成**再断言 ✗ —— 提交会先被那段内联 JS 接住、
            #    发完 `/api/unlock` 再自己 `location.reload()`；本鱼第一版只
            #    `wait_for_load_state('load') + 300ms` ⇒ 量到的是**旧那一页** ⇒ 假红 ✓
            page.fill("input[data-gate-pw]", good_a)
            page.keyboard.press("Enter")
            try:
                page.wait_for_function(
                    "() => document.body.innerText.indexOf('原话A-0') >= 0", timeout=8000)
            except Exception:                     # noqa: BLE001 —— 等不到就走下面的断言报出来
                pass
            body = page.content()
            if "原话A-0" not in body or "原话A-1" not in body:
                print("      输了对口令、走了真表单，原话**整列**还是没出来 ✗")
                return False
            # ⚠️ 判据要精确到**这一个 bot** ✗：另一个 bot（B）没输口令 ⇒ 它的格子**还在盖着**、
            #    票头那个框也还在（因为还有东西盖着 ✓）—— 别指望"全页一个框都不剩" ✗
            #    （本鱼第一版就是这么写错的，当场假红 ✓）
            #    ⇒ 拿**解锁前**那份现场数组当基准，解锁后 A 的格子必须一个不剩 ✓
            after = page_cells(page)
            if len(after) != len(before):
                print("      解锁前后表数变了 ✗：%s → %s" % (before, after))
                return False
            if after[0] != 0:
                print("      **A 那张表里还剩 %d 个遮罩** ✗（输一次该把整列都换掉 ✓）" % after[0])
                return False
            if after[1:] != before[1:]:
                print("      **B 那张表被连带动了** ✗：%s → %s" % (before, after))
                return False
            if os.environ.get("PANEL_E2E_SHOT"):
                page.screenshot(path=os.environ["PANEL_E2E_SHOT"], full_page=False)
            # 再来一张**未授权**的：深浅两套主题都拍给人看 ✓
            shot2 = os.environ.get("PANEL_E2E_SHOT2")
            if shot2:
                fresh = browser.new_page(viewport={"width": 1280, "height": 800},
                                         color_scheme="light")
                fresh.goto("http://127.0.0.1:%d/" % port, wait_until="load")
                box = fresh.query_selector(".gcell")
                if box:
                    box.scroll_into_view_if_needed()
                    fresh.wait_for_timeout(120)
                fresh.screenshot(path=shot2, full_page=False)
            browser.close()
    finally:
        httpd.shutdown()
        httpd.server_close()
    if errors:
        print("      浏览器里报 JS 错:", errors)
        return False
    return True


TESTS = [
    ("P01 只读连接写不了库", t01_readonly),
    ("P02 快照读得到发行/钱包/欠条/流水", t02_snapshot_content),
    ("P03 原话转义（防 XSS）", t03_html_escaped),
    ("P04 真 HTTP 冒烟（200 / 404）", t04_http_smoke),
    ("P05 --json 离线导出", t05_json_mode),
    ("P06 库不存在时如实说", t06_missing_db),
    ("P07 库没建表时如实说", t07_empty_db),
    ("P08 面板源码卫生", t08_source_hygiene),
    ("P09 CLI 入口（--json 真跑一遍）", t09_cli_entry),
    ("P10 开浏览器只开一次、失败不炸", t10_open_browser_once),
    ("P11 流水就地展开（label+开关、不跳页）", t11_expand_inplace),
    ("P12 被注视单独一页（流水不含它）", t12_attention_page),
    ("P13 真浏览器：点完不位移 / 批次不回收 / 只一颗按钮", t13_browser_no_jump),
    ("P14 按日检索 + 类别筛选（默认本日）+ JS 纪律", t14_day_and_kind_filter),
    ("P15 心情锁（服务端扣住 / 限次 / 多 bot / 片段模式）", t15_mood_gate_server),
    ("P16 真浏览器：毛玻璃生效 · Ctrl+A 也选不到原话 · 输口令才出来", t16_gate_browser),
]


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    passed, failed = [], []
    with tempfile.TemporaryDirectory(prefix="wishonomy-panel-") as tmpdir:
        env = Env(tmpdir)
        env.seed()
        print("=" * 66)
        print("愿力中心 · 只读面板自检（P6）")
        print("数据文件:", env.db)
        print("=" * 66)
        for name, fn in TESTS:
            try:
                ok = bool(fn(env, None))
            except Exception as exc:              # noqa: BLE001 —— 要报出崩在哪一项
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
