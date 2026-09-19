#!/usr/bin/env python3
"""Хук PostToolUse: дописать в поле `hosts` своей отметки на доске хосты из ssh/scp.

Зачем: стартер пишет отметку при старте, когда сессия ещё не знает, куда пойдёт, —
и `hosts` у всех оставался прочерком. Поле должно отвечать «кто трогал хост», а не
«что сессия планировала» ⟹ источник — собственные ssh/scp-вызовы сессии, без
дисциплины агента.

Подключение (settings.json проекта), для Bash и PowerShell:
    "PostToolUse": [{"matcher": "Bash|PowerShell", "hooks": [{"type": "command",
      "command": "python \"<проект>/.agents/skills/yamem/skills/yamem/scripts/yamem-hosts-hook.py\"",
      "timeout": 5}]}]

Хук молчит и всегда выходит с 0: отметка — справочная, ломать ход сессии она не должна.
Пишет только локальный файл; в git отметка уезжает со следующим стартом (стартер
сохраняет накопленные `hosts`).
"""
import json
import os
import re
import shlex
import sys
from datetime import datetime
from pathlib import Path

# ssh-опции, у которых есть аргумент (man ssh)
SSH_ARG_OPTS = set("BbcDEeFIiJLlmOoPpQRSWw")
HOST_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.\-]*$")
MAX_HOSTS = 30


def _clean(dest: str) -> str:
    dest = dest.strip("'\"")
    if dest.startswith("ssh://"):
        dest = dest[6:]
    dest = dest.rsplit("@", 1)[-1]
    if dest.startswith("["):  # [host]:port
        dest = dest[1:].split("]", 1)[0]
    dest = dest.split(":", 1)[0].lower()
    return dest if HOST_RE.match(dest) and not dest.startswith("-") else ""


def _tokens(segment: str) -> list:
    try:
        return shlex.split(segment, posix=True)
    except ValueError:
        return segment.split()


def hosts_from_command(cmd: str) -> list:
    found = []
    # делим по разделителям команд, чтобы `ssh a && ssh b` дал оба хоста
    for seg in re.split(r"&&|\|\||[;|\n]|\$\(|`", cmd):
        toks = _tokens(seg)
        for i, t in enumerate(toks):
            base = os.path.basename(t).lower()
            if base in ("ssh", "ssh.exe"):
                j = i + 1
                while j < len(toks):
                    o = toks[j]
                    if o.startswith("-") and len(o) > 1:
                        # -p22 — аргумент слитно; -p 22 — отдельным токеном
                        if o[-1] in SSH_ARG_OPTS and len(o) == 2:
                            j += 2
                        else:
                            j += 1
                        continue
                    h = _clean(o)
                    if h:
                        found.append(h)
                    break
            elif base in ("scp", "scp.exe", "rsync", "sftp"):
                for o in toks[i + 1:]:
                    if o.startswith("-") or ":" not in o or re.match(r"^[A-Za-z]:[\\/]", o):
                        continue
                    h = _clean(o)
                    if h and "." in h:  # host:path; C:\… отсечено выше
                        found.append(h)
    # только похожее на имя хоста: FQDN или IP (одиночное слово — скорее переменная)
    return [h for h in dict.fromkeys(found) if "." in h]


def memory_root(data: dict) -> Path | None:
    cands = [os.environ.get("CLAUDE_PROJECT_DIR"), data.get("cwd"), os.getcwd()]
    for c in cands:
        if not c:
            continue
        p = Path(c).resolve()
        for d in [p, *p.parents]:
            if (d / ".agents" / "memory" / ".sessions").is_dir():
                return d / ".agents" / "memory"
    return None


def main() -> int:
    try:
        data = json.load(sys.stdin)
    except Exception:
        return 0
    cmd = (data.get("tool_input") or {}).get("command") or ""
    sid = (data.get("session_id") or "")[:8]
    if not cmd or not sid or not re.search(r"\b(ssh|scp|rsync|sftp)\b", cmd):
        return 0
    new = hosts_from_command(cmd)
    if not new:
        return 0
    root = memory_root(data)
    if root is None:
        return 0
    mine = root / ".sessions" / f"{sid}.md"
    if not mine.is_file():
        return 0  # отметку создаёт стартер; без неё не заводим
    text = mine.read_text(encoding="utf-8")
    m = re.search(r"^hosts:[ \t]*(.*)$", text, re.M)
    have = [] if not m else [h.strip() for h in m.group(1).split(",")
                             if h.strip() and h.strip() not in ("—", "-")]
    add = [h for h in new if h not in have]
    if not add:
        return 0
    line = "hosts: " + ", ".join((have + add)[-MAX_HOSTS:])
    text = text[:m.start()] + line + text[m.end():] if m else text.rstrip("\n") + f"\n{line}\n"
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    text = re.sub(r"^updated:.*$", f"updated: {stamp}", text, count=1, flags=re.M)
    mine.write_text(text, encoding="utf-8", newline="\n")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        sys.exit(0)
