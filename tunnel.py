# -*- coding: utf-8 -*-
"""把容器里的**只读副本**拉到本机（给管理台看 ✓）。

为什么要这一层：账本文件在容器里（`qqbot-astrbot`），而插件目录**不能多放文件**
（AstrBot 只加载 main.py；`panel.py` 里那句 `import main` 在线上会导到框架的 main ✗）。
⇒ 做法：**只读拉一份副本到本机临时目录**，面板读副本 ✓

拉取清单（SQLite 三件套都要，否则可能读到半个事务 ✗）：
    ledger.db · ledger.db-wal · ledger.db-shm
另加用量流水 `usage.jsonl`（有就拉）✓

安全：只读复核，**不写容器**；不打印任何凭据 ✓
"""
import os
import shutil
import subprocess
import sys
import tempfile

CONTAINER = os.environ.get("WISHONOMY_CONTAINER", "qqbot-astrbot")
SSH_TARGET = os.environ.get("WISHONOMY_SSH", "srv")
REMOTE_DATA = "/opt/astrbot/data/plugin_data/astrbot_plugin_wishonomy"

SSH = shutil.which("ssh") or r"C:\Windows\System32\OpenSSH\ssh.exe"
SCP = shutil.which("scp") or r"C:\Windows\System32\OpenSSH\scp.exe"


def _run(args, timeout=120):
    out = subprocess.run(args, capture_output=True, text=True, encoding="utf-8",
                         errors="replace", timeout=timeout)
    return out.returncode, (out.stdout or ""), (out.stderr or "")


def pull_ledger(dest_dir=None, quiet=False):
    """把容器里的账本与用量流水拉成一份只读副本。

    返回 {ok, dir, db, usage, files, error}
    """
    dest = dest_dir or os.path.join(
        os.environ.get("LOCALAPPDATA") or tempfile.gettempdir(),
        "wishonomy-ledger-copy")
    os.makedirs(dest, exist_ok=True)
    stage = "/tmp/wishonomy-copy"
    # ① 容器 → 宿主机的临时目录（用 `docker cp`，不走容器 shell ✓）
    #    ⚠️ 别用 `docker exec sh -c 'for f in $f …'` —— ssh→docker→sh 三层下来
    #       变量会被吃掉（实测 `[: missing ]`，一个文件都没拷出来 ✗）
    cmd = ("rm -rf %s && mkdir -p %s && docker cp %s:%s/. %s/ && ls -l %s"
           % (stage, stage, CONTAINER, REMOTE_DATA, stage, stage))
    code, out, err = _run([SSH, SSH_TARGET, cmd])
    if code != 0:
        return {"ok": False, "dir": dest, "db": "", "usage": "",
                "files": [], "error": "容器内拷不出来: %s" % (err or out)[:200]}
    # ② 从宿主机拉回本机
    db = os.path.join(dest, "ledger.db")
    usage = os.path.join(dest, "usage.jsonl")
    for name in ("ledger.db", "ledger.db-wal", "ledger.db-shm", "usage.jsonl"):
        _run([SCP, "%s:%s/%s" % (SSH_TARGET, stage, name), os.path.join(dest, name)])
    files = [fn for fn in sorted(os.listdir(dest)) if fn.startswith(("ledger.", "usage."))]
    if not os.path.exists(db):
        return {"ok": False, "dir": dest, "db": "", "usage": "",
                "files": files, "error": "本地没拿到 ledger.db"}
    if not quiet:
        print("已拉取 → %s" % dest)
        for fn in files:
            print("  %-20s %8d B" % (fn, os.path.getsize(os.path.join(dest, fn))))
    return {"ok": True, "dir": dest, "db": db,
            "usage": usage if os.path.exists(usage) else "",
            "files": files, "error": ""}


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    info = pull_ledger()
    if not info["ok"]:
        print("✗ %s" % info["error"])
        return 1
    print("✓ 副本就绪：%s" % info["db"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
