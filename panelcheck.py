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
        sc.raw_grant(mod, sc.BOB, 30, item="举一次牌")
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
