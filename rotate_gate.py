# -*- coding: utf-8 -*-
"""**钥匙轮换器**：立刻给配置里的每个 bot 转一把今天的钥匙（不用等她被唤醒）。

为什么要有它（`docs/canon-29.md` §3 说"生成时机：跟日结同一处 **或独立脚本** —— 落码时定" ✓）：
  钥匙**平时**是她今天第一次被唤醒时转的（与日结同一处 ✓ 零额外开销）——
  但"她今天一句话都没说"的时候（半夜、她不理人、刚部署完）就**一把都没有** ⇒
  运营那边谁也开不了门 ✗ ⇒ 补这个**手动那一档**：想验、想救急，就显式跑一次 ✓
  （它跟插件**同一个模块里的同一段代码**，不另写一份口径 ⇒ 不会两边跑偏 ✓）

跑法（**在容器里**跑，因为账本与配置都在容器里）：
    docker exec -it qqbot-astrbot \
        -e WISH_GATE_NOTES=/path/to/notes.md -e WISH_GATE_SELF_ID=<bot 号> \
        /opt/astrbot/.venv/bin/python \
        /opt/astrbot/data/plugins/astrbot_plugin_wishonomy/rotate_gate.py

⚠️ **这个脚本里不写死任何路径与 bot 号** ✗（公开仓铁律）——
   路径与号一律从**插件配置**或**环境变量**来（`WISH_GATE_NOTES` / `WISH_GATE_SELF_ID` ✓）。
"""
import asyncio
import importlib.util
import os
import sys
import tempfile
import types

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def data_dir():
    """插件的**数据目录**（账本与键文件都落在这儿 ✓）。

    ⚠️ 必须与框架的 `StarTools.get_data_dir()` 一致 ✗ —— 本鱼第一版随手写成
       "脚本所在目录" ⇒ **哈希被写进了插件代码目录**、而库在 `plugin_data/` 里 ⇒
       面板永远拿不到它 ✗（差一个目录就是"看得见门、找不到钥匙" ✓）
    ⚠️ 这里也**不写死插件名之下那个目录**：用 `WISH_GATE_DATA_DIR` 覆盖，
       或者按 `<ASTRBOT_ROOT>/data/plugin_data/<插件目录名>` 这个**通用**约定推 ✓
    """
    override = str(os.environ.get("WISH_GATE_DATA_DIR") or "").strip()
    if override:
        return override
    base = os.environ.get("ASTRBOT_ROOT") or "/opt/astrbot"
    guess = os.path.join(base, "data", "plugin_data", os.path.basename(HERE))
    return guess if os.path.isdir(guess) else HERE


def load_plugin(config):
    """把 `main.py` 加载起来（**给它铺一层最小 astrbot 桩** —— 与自检同一招 ✓）"""
    def hook(*a, **k):
        return lambda fn: fn

    api = types.ModuleType("astrbot.api")
    event_api = types.ModuleType("astrbot.api.event")
    star_api = types.ModuleType("astrbot.api.star")
    root = types.ModuleType("astrbot")
    core = types.ModuleType("astrbot.core")
    agent = types.ModuleType("astrbot.core.agent")
    agent_message = types.ModuleType("astrbot.core.agent.message")
    api.AstrBotConfig = dict
    api.llm_tool = lambda *a, **k: (lambda fn: fn)
    api.logger = types.SimpleNamespace(info=lambda *a, **k: None,
                                       warning=lambda *a, **k: None,
                                       error=lambda *a, **k: None)
    api.star = star_api
    star_api.Star = type("Star", (), {"__init__": lambda self, context=None: None})
    star_api.Context = object
    star_api.StarTools = types.SimpleNamespace(get_data_dir=lambda name: data_dir())
    event_api.AstrMessageEvent = object
    event_api.filter = types.SimpleNamespace(on_llm_request=hook, on_llm_response=hook,
                                             on_agent_done=hook)
    agent_message.TextPart = type("TextPart", (), {
        "__init__": lambda self, text: setattr(self, "text", text)})
    for name, mod in {"astrbot": root, "astrbot.api": api, "astrbot.api.event": event_api,
                      "astrbot.api.star": star_api, "astrbot.core": core,
                      "astrbot.core.agent": agent,
                      "astrbot.core.agent.message": agent_message}.items():
        sys.modules[name] = mod
    spec = importlib.util.spec_from_file_location("wishonomy_main", os.path.join(HERE, "main.py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules["wishonomy_main"] = module
    spec.loader.exec_module(module)
    return module


def read_config():
    """读插件配置（与 `main.config_paths()` 同一套候选路径 ✓）"""
    import json
    for path in (os.environ.get("WISHONOMY_CONFIG") or "",) + tuple(
            os.path.join(root, "data", "config",
                         "astrbot_plugin_wishonomy_config.json")
            for root in (os.environ.get("ASTRBOT_ROOT") or "", "/opt/astrbot", os.getcwd())):
        if not path:
            continue
        try:
            with open(path, encoding="utf-8-sig") as handle:
                data = json.load(handle)
            if isinstance(data, dict):
                return data
        except (OSError, ValueError):
            continue
    return {}


def main():
    conf = read_config()
    if not conf:
        print("✗ 读不到插件配置（设 WISHONOMY_CONFIG 指路）")
        return 1
    mod = load_plugin(conf)
    merged = mod.merge_config(conf)
    pairs = mod._gate_dict(mod._cfg(merged, "notes_path", {}))
    if not pairs and not os.environ.get("WISH_GATE_NOTES"):
        print("✗ 配置里没配 notes_path，也没给 WISH_GATE_NOTES ⇒ 不知道该给谁转（整套是关着的 ✓）")
        return 1
    done = []
    for sid in sorted(pairs) or [""]:
        notes, sid, entry = mod.gate_default_notes(sid, merged)
        if not notes:
            print("・ bot=%s 没配笔记路径 ⇒ 跳过" % (sid or "?"))
            continue
        code = mod.gate_rotate_for(sid, merged)
        if not code:
            print("✗ bot=%s 轮换失败（账本读不到？）: %s" % (sid, notes))
            continue
        done.append(sid)
        print("✅ bot=%s 今天的钥匙已就位 · 笔记=%s · 哈希=%s（明文不入日志 ✓）"
              % (sid, notes, entry))
    return 0 if done else 1


if __name__ == "__main__":
    sys.exit(main())
