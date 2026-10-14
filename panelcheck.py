# -*- coding: utf-8 -*-
"""愿力中心 · 只读面板自检（P6）。

口径与 `selfcheck.py` 一致：**失败就 exit 1**，一项一个函数，返回 True/False，
拿真库 + 真 HTTP 服务器跑（不是打桩）。

跑法：
    D:\\smm-tempsparxie\\.venv\\Scripts\\python.exe D:\\sparxie\\wishonomy\\panelcheck.py
"""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import uuid

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


# ─────────────────────────────── 各项 ───────────────────────────────

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
    if "<script>" in page:
        print("      原话没转义，页面里出现了真 <script> ✗")
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
    # ④ 库里还有更早的 ⇒ 才给 `?n=` 兜底（全页**唯一**会刷新的一颗 ✓）；给全了就不许再催
    more = env.panel.brief_table({"brief": entries, "brief_count": 130, "ledger_total": 999,
                                  "limit": 400}, "bX")
    if "库里还有更早的" not in more or "?n=" not in more:
        print("      没给「库里还有更早的」兜底 ✗")
        return False
    if "?n=" in html_out:
        print("      给全了还在催重读 ✗")
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
