# -*- coding: utf-8 -*-
"""一键拉取：把容器里的**只读副本**取到本机（管理台那个「刷新」按钮调它）。

为什么不把这段塞进 `panel.py`：面板要能**离线**看一份副本，
而"去服务器取新的"是一次**外部动作**（会碰 ssh / docker）——
分成两个模块，出问题时一眼知道是哪一半 ✓

安全底线：
  * 只 **读** 远端（`docker cp` 出来，不动容器里的文件）✓
  * 不打印任何凭据；ssh 走本机 `~/.ssh/config` 里的别名 ✓
  * 拉回来的目录固定在本机用户目录下，不写系统目录 ✓
"""
import os
import shlex
import shutil
import subprocess
import sys

CONTAINER = os.environ.get("WISHONOMY_CONTAINER", "qqbot-astrbot")
SSH_TARGET = os.environ.get("WISHONOMY_SSH", "srv")


def _safe(box, data, stage):
    """远端命令里要拼的三个变量 —— **白名单校验**，别把任意字符串塞进 shell（P2-5）。

    ⚠️ `ssh target <cmd>` 会在远端走 shell ⇒ 环境变量里的笔误/注入会变成远端执行 ✗
    这三样都该是"我们自己起的名字"：容器名只允许常见字符、路径只允许绝对路径 ✓
    """
    import re as _re
    if not _re.match(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,62}$", str(box or "")):
        raise ValueError("容器名不合法（只允许字母数字 _ . -）：%r" % box)
    for label, value in (("data", data), ("stage", stage)):
        if not str(value or "").startswith("/"):
            raise ValueError("%s 必须是绝对路径：%r" % (label, value))
        if any(ch in str(value) for ch in ("\ufeff", chr(0), chr(10), chr(13))):
            raise ValueError("%s 里有控制字符：%r" % (label, value))
    return (shlex.quote(str(box)), shlex.quote(str(data)), shlex.quote(str(stage)))
REMOTE_DATA = "/opt/astrbot/data/plugin_data/astrbot_plugin_wishonomy"
STAGE = "/tmp/wishonomy-copy"
# 心情锁那把钥匙的**哈希**文件（跟库放一起 ⇒ 一次拉全 ✓ 文件名与 `panel.GATE_FILE` 同源 ✓）
GATE_KEYS = "wish_gate_keys.json"

SSH = shutil.which("ssh") or r"C:\Windows\System32\OpenSSH\ssh.exe"
SCP = shutil.which("scp") or r"C:\Windows\System32\OpenSSH\scp.exe"


def copy_dir():
    return os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"),
                        "wishonomy-ledger-copy")


def _run(args, timeout=180):
    out = subprocess.run(args, capture_output=True, text=True, encoding="utf-8",
                         errors="replace", timeout=timeout)
    return out.returncode, (out.stdout or ""), (out.stderr or "")


def pull(dest_dir=None, ssh_target=None):
    """拉一份新的只读副本。返回 {ok, dir, db, usage, files, error, at}"""
    import time
    dest = dest_dir or copy_dir()
    target = ssh_target or SSH_TARGET
    os.makedirs(dest, exist_ok=True)
    info = {"ok": False, "dir": dest, "db": "", "usage": "", "files": [],
            "error": "", "at": int(time.time())}
    # ① 容器 → 宿主机临时目录（用 `docker cp`，**不走容器 shell**）
    #    ⚠️ 别写 `docker exec sh -c 'for f in $f …'`：ssh→docker→sh 三层下来
    #       变量会被吃掉（真机实测 `[: missing ]`、一个文件都没拷出来 ✗）
    # 2026-10-27 修（P2-5）：三个变量先过白名单、再 shlex.quote ⇒ 不会被当成 shell 语法 ✓
    try:
        _box, _data, _stage = _safe(CONTAINER, REMOTE_DATA, STAGE)
    except ValueError as exc:
        info["error"] = "远端参数不合法：%s" % exc
        return info
    cmd = ("rm -rf {stage} && mkdir -p {stage} && docker cp {box}:{data}/. {stage}/ "
           "&& ls -l {stage}").format(stage=_stage, box=_box, data=_data)
    code, out, err = _run([SSH, target, cmd])
    if code != 0:
        info["error"] = "从容器里拷不出来：%s" % (err or out).strip()[:200]
        return info
    # ② 宿主机 → 本机
    # ⚠️ `wish_gate_keys.json`（**心情锁那把钥匙的哈希** ✓）必须跟账本**同一次、同一批**拉回来 ✗：
    #    面板跑在本机、插件跑在容器，两边只靠这份副本说话 ⇒ 少拉它一次，
    #    面板就会拿着**旧哈希**去验**新口令**（她给对了也开不了门 ✗）
    #    （口径见 `canon/_29` §3 ✓ 明文永远只在她自己的账本里 ✓）
    names = ("ledger.db", "ledger.db-wal", "ledger.db-shm", "usage.jsonl",
             GATE_KEYS)
    for name in names:
        _run([SCP, "%s:%s/%s" % (target, STAGE, name), os.path.join(dest, name)])
    for name in names:
        path = os.path.join(dest, name)
        if os.path.exists(path):
            info["files"].append(name)
    info["db"] = os.path.join(dest, "ledger.db")
    usage = os.path.join(dest, "usage.jsonl")
    info["usage"] = usage if os.path.exists(usage) else ""
    if not os.path.exists(info["db"]):
        info["error"] = "本机没拿到 ledger.db（远端可能还没有账本文件）"
        return info
    info["ok"] = True
    return info


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    info = pull()
    if not info["ok"]:
        print("✗ %s" % info["error"])
        return 1
    print("✓ 已拉取 → %s" % info["dir"])
    for name in info["files"]:
        print("  %-20s %8d B" % (name, os.path.getsize(os.path.join(info["dir"], name))))
    return 0


if __name__ == "__main__":
    sys.exit(main())
