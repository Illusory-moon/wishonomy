# NEXT.md —— 下一步（★ 先读这一页再动手）

> 2026-10-13 由「大肥鱼」留下。**下面两件原来欠着的事，当晚都做完了** ✓
> 这一页留着当**交接底稿**：写清它们当时为什么欠着、判据是什么 ✓

## 现状（以**本文件所在 commit 的内容为准**）

- ✅ **P1 骨架可用**：四张表 + `user_version` 迁移 · 幂等 `UNIQUE(kind, ref)` · 身份闸门四条 ·
  工具四个（`wish_check` / `wish_mood` / `wish_gift` / `wish_audit`）· **一律返回非空字符串** ✓
- ✅ **自检 23 项全绿**：`python selfcheck.py` ⇒ `通过 23 项 / 失败 0 项` ✓
- ✅ **术语已统一为「愿力中心」**（不再叫「账本」—— 那个词在本项目里指 bot 的笔记层）✓
- ✅ **缓存友好的注入已落地**：常量进 `system_prompt` 尾部、每轮变的那一行挂
  `extra_user_content_parts` 的最后一个 part；没有账户就一个字都不注入 ✓（开关 `inject_money`）
- ✅ 已推上 `https://github.com/Illusory-moon/wishonomy`（`main...origin/main` **0/0** ✓）

## 两件已做完的事（留档：当时的判据）

### ① 术语改名「账本」→「愿力中心」✅

判据（已满足）：`grep -c 账本` 只剩**解释撞名**的那三处（`main.py` / `README.md` /
`docs/ARCHITECTURE.md` 各一处）+ `NEXT.md` 自己 ✓；自检 23/23 ✓

### ② 「把当前说话人的愿力摆到它眼前」的注入 ✅

| 内容 | 稳定性 | 位置 |
|---|---|---|
| 工具说明（docstring） | 永远不变 | 工具定义里（稳定前缀 ✓） |
| `INJECT_TAIL`（常量） | 永远不变 | `system_prompt` 的**尾部** |
| 「他现在有多少愿力 / 信誉分」 | 每轮可能变 | `extra_user_content_parts` 的**最后一个 part** |

行为断言（`selfcheck.py` 的 **R23**，六条）：
① 没账户 ⇒ 不注入 ② 数字**只在 parts 里**、**不在** `system_prompt` 里
③ 换个人 ⇒ `system_prompt` 一字不变、只有 part 变 ④ 注入不写库 ⑤ 关掉开关 ⇒ 什么都不注入
⑥ 身份闸门照旧管着注入 ✓
**反面判据**：把变动数字塞进 `system_prompt` ⇒ 当场红 ✓

## 方法论提醒（这一轮真踩的，别重复）

- ⚠️ **`git reset --hard` 会把没提交的改动一起清掉** ✗ —— 注入代码曾因此丢了两遍
  ⇒ 动手重构历史之前，先 `git stash` 或先提交一版可用的 ✓
- ⚠️ **别用"字符串手术脚本"去拆 commit** ✗ —— 那会把工作区改回半成品，
  比"两条 commit 语义不干净"糟糕得多 ✓
- ✅ 拆 commit 的正路：**在干净点上重放**（先改名提交 → 再注入提交），每步都跑自检 ✓

