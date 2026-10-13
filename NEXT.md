# NEXT.md —— 下一步（★ 先读这一页再动手）

> 2026-10-13 由「大肥鱼」留下。**本仓库当前是 P1 可用版**，但有**两件欠着的事** ✗ —— 写在这里防走丢 ✓

## 现状（以**本文件所在 commit 的内容为准**）

- ✅ **P1 骨架可用**：四张表 + `user_version` 迁移 · 幂等 `UNIQUE(kind, ref)` · 身份闸门四条 ·
  工具四个（`wish_check` / `wish_mood` / `wish_gift` / `wish_audit`）· **一律返回非空字符串** ✓
- ✅ **自检 23 项全绿**：`python selfcheck.py` ⇒ `通过 23 项 / 失败 0 项` ✓
- ⚠️ 已推上 `https://github.com/Illusory-moon/wishonomy`（`main...origin/main` **0/0** ✓）

## 欠着的事（两件，都很小）

### ① 术语改名「账本」→「愿力中心」（**没落地** ✗）

原因：「账本」在本项目里**已经指 bot 自己的笔记层**（`save_note`），两个东西撞名 ✗（主人 2026-10-13 指出）。

范围：`main.py`（约 19 处：模块 docstring、注释、工具返回值文案）、`selfcheck.py`、
`README.md`、`docs/ARCHITECTURE.md`、`docs/ROADMAP.md`、`metadata.yaml`、`_conf_schema.json`。
**做法**：逐处精确替换 + 读懂上下文（**别整文件重写** ✗ —— 本项目为此毁过三份大文档）。
**判据**：`grep -c 账本` 只应剩下**解释撞名**的那两三处；自检仍 23/23 ✓

### ② 「把当前说话人的愿力摆到它眼前」的注入（**没落地** ✗）

主人 2026-10-13 定的口径：**不是"不许注入"，而是"越不稳的放越后"** ✓

| 内容 | 稳定性 | 该放哪 |
|---|---|---|
| 工具说明（docstring） | 永远不变 | 工具定义里（稳定前缀 ✓） |
| 一段说明文字（常量） | 永远不变 | `system_prompt` 的**尾部** |
| 「他现在有多少愿力 / 信誉分」 | 每轮可能变 | `extra_user_content_parts` 的**最后一个 part**（框架接在用户消息之后） |

**做法**：
1. `try: from astrbot.core.agent.message import TextPart / except ImportError: TextPart = None`
2. 常量 `INJECT_TAIL` + 纯函数 `money_hint(snap, who_text)`（没账户就返回空 ⇒ 一个字都不注入）
3. `@filter.on_llm_request(priority=50)` 的 `inject_money`：判定 `identity()` → 读 `db_snapshot` →
   `parts.append(TextPart(text=hint))` → 幂等地补 `INJECT_TAIL` 到 `system_prompt` 尾部；异常一律吞掉
4. 配置项 `inject_money`（默认 `true`）
5. 自检换成**行为断言**（照 `astrbot_plugin_impression` 的现有做法）：
   ① 没账户 ⇒ 不注入 ② 变动那一行**只在 parts 里**、数字**不在** `system_prompt` 里
   ③ 换个人 ⇒ `system_prompt` **一字不变**、只有 part 变 ④ 注入不写库 ⑤ 关掉开关 ⇒ 什么都不注入
6. ✅ **反面判据**（别写成"禁止注入"✗）：把变动数字塞进 `system_prompt` 必须**当场红**

## 方法论提醒（这一轮真踩的）

- ⚠️ **`git reset --hard` 会把没提交的改动一起清掉** ✗ —— 本鱼这一轮就因此丢了两遍注入代码。
  ⇒ 动手前先 `git stash` 或先提交一版可用的 ✓
- ⚠️ **别用"字符串手术脚本"去拆 commit** ✗ —— 本鱼写的那版把工作区改回了半成品，
  比"两条 commit 语义不完全干净"糟糕得多。**宁可一条诚实的 commit，也不要一个坏掉的工作区** ✓
- ✅ 拆 commit 的正路：**在干净点上重放**（不是从成品往回剥）✓
