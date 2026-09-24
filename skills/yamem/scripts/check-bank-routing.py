#!/usr/bin/env python3
"""Гейт памяти: routing банков по маркерам контура.

🎯 Мотив. Правило «сначала реши, в какой банк» ничем не проверялось: факт про чужую площадку
молча оседал в `local`, а наш хост так же молча уезжал в отчуждаемый банк, который читают
посторонние. Имена хостов и доменов уникальны ⟹ детектор по маркерам дешёвый и точный.

Маркеры задаются в `yamem.config.yaml` памяти, у каждого банка своей секцией — в скрипте
их нет, и без маркеров в конфиге он ничего не проверяет:

    banks:
      - name: lion-site
        path: banks/lion-site
        alienable: true            # банк читают посторонние (ремесло, общий с чужой командой)
        markers: ['lion\\.local', '(?<![a-z0-9])pee-']

Маркер — регулярное выражение Python, без учёта регистра. Границы слова пишутся в самом
маркере: `sts` без них ловит «posts», `ag` — «diag.local».

Два класса:
  1. маркер банка X в добавленной строке банка Y → «факту место в банке X» (предупреждение).
     Строка, где есть и маркер самого Y, не считается: сравнение площадок — законный факт Y.
  2. маркер любого другого банка в добавленной строке ОТЧУЖДАЕМОГО банка (`alienable: true`)
     → утечка, код возврата 1. Обход разово: YAMEM_ALLOW_ROUTING=1 git commit …

Проверяются только ДОБАВЛЯЕМЫЕ строки, а не банк целиком: иначе старое наследие
заблокировало бы все коммиты. Журнал проекта (MEMORY.md, tasks/, diary/) не проверяется —
он не отчуждается, и факты любой площадки в нём законны.

    python check-bank-routing.py                   # staged-diff текущего репозитория
    python check-bank-routing.py --history 7.days  # замер по git log -p памяти и банков
"""
import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

ENV_ALLOW = "YAMEM_ALLOW_ROUTING"
SHOW_LINES = 3          # сколько строк показывать на файл и класс


# ---------- конфиг ----------

def _strip_comment(val: str) -> str:
    """Хвостовой `# комментарий` вне кавычек."""
    q = None
    for i, ch in enumerate(val):
        if q:
            if ch == q:
                q = None
        elif ch in "'\"":
            q = ch
        elif ch == "#" and (i == 0 or val[i - 1] in " \t"):
            return val[:i].rstrip()
    return val.strip()


def _scalar(tok: str) -> str:
    tok = tok.strip()
    if len(tok) >= 2 and tok[0] == tok[-1] == "'":
        return tok[1:-1].replace("''", "'")
    if len(tok) >= 2 and tok[0] == tok[-1] == '"':
        return tok[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    return tok


def _flow_list(val: str) -> list:
    """`[a, 'b,c', "d"]` → список; запятые внутри кавычек не делят."""
    body = val.strip()[1:-1]
    out, cur, q = [], "", None
    for ch in body:
        if q:
            cur += ch
            if ch == q:
                q = None
        elif ch in "'\"":
            q = ch
            cur += ch
        elif ch == ",":
            out.append(cur)
            cur = ""
        else:
            cur += ch
    out.append(cur)
    return [_scalar(t) for t in out if t.strip()]


def _banks_heuristic(text: str) -> list:
    """Секция `banks:` без PyYAML: плоские ключи элемента и список `markers`."""
    m = re.search(r"^banks:\s*$(.*?)(?=^\S|\Z)", text, re.M | re.S)
    if not m:
        return []
    items, cur, list_key, list_indent = [], None, None, 0
    for line in m.group(1).splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())
        if list_key and indent > list_indent and line.lstrip().startswith("- "):
            cur[list_key].append(_scalar(_strip_comment(line.lstrip()[2:])))
            continue
        list_key = None
        im = re.match(r"^\s*-\s+([A-Za-z_]\w*):\s*(.*)$", line)
        km = re.match(r"^\s+([A-Za-z_]\w*):\s*(.*)$", line)
        if im:
            cur = {}
            items.append(cur)
            key, val = im.group(1), im.group(2)
        elif km and cur is not None:
            key, val = km.group(1), km.group(2)
        else:
            continue
        val = _strip_comment(val)
        if val.startswith("["):
            cur[key] = _flow_list(val)
        elif val == "":
            cur[key], list_key, list_indent = [], key, indent
        else:
            cur[key] = _scalar(val)
    return items


def read_banks(mem: Path) -> list:
    """[(имя, каталог, alienable, [скомпилированные маркеры])]."""
    text = (mem / "yamem.config.yaml").read_text(encoding="utf-8")
    try:
        import yaml
        items = (yaml.safe_load(text) or {}).get("banks") or []
    except ImportError:
        items = _banks_heuristic(text)
    out = []
    for it in items:
        if not isinstance(it, dict) or not it.get("name"):
            continue
        markers = it.get("markers") or []
        if isinstance(markers, str):
            markers = [markers]
        rx = []
        for mk in markers:
            try:
                rx.append((mk, re.compile(str(mk), re.I)))
            except re.error as e:
                print("yamem: маркер банка %s не регулярка (%s): %s" % (it["name"], e, mk))
        out.append((str(it["name"]), (mem / str(it.get("path", ""))).resolve(),
                    str(it.get("alienable", "")).lower() in ("true", "yes", "1", "да"), rx))
    return out


def find_memory(start: Path):
    """Корень памяти от корня репозитория: сама память, банк внутри неё или проект с .agents/memory."""
    for d in [start, *start.parents][:4]:
        if (d / "yamem.config.yaml").is_file():
            return d
    if (start / ".agents" / "memory" / "yamem.config.yaml").is_file():
        return start / ".agents" / "memory"
    return None


# ---------- проверка ----------

def bank_of(path: Path, banks):
    best = None
    for b in banks:
        try:
            path.relative_to(b[1])
        except ValueError:
            continue
        if best is None or len(str(b[1])) > len(str(best[1])):
            best = b
    return best


def hits_in_line(line: str, bank, banks):
    """[(класс, чей маркер, маркер)] для строки банка `bank`."""
    name, _, alienable, own = bank
    if not alienable and any(r.search(line) for _, r in own):
        return []
    out = []
    for other, _, _, rx in banks:
        if other == name:
            continue
        for mk, r in rx:
            if r.search(line):
                out.append((2 if alienable else 1, other, mk))
                break
    return out


def parse_diff(text: str):
    """(файл, добавленная строка, коммит) из `git diff`/`git log -p`."""
    commit, path = "", None
    for line in text.splitlines():
        if line.startswith("\x01commit "):
            commit = line[8:]
            continue
        if line.startswith("+++ "):
            p = line[4:]
            path = p[2:] if p.startswith("b/") else None
            continue
        if line.startswith("+") and path:
            yield path, line[1:], commit


def git(repo: Path, *args) -> str:
    r = subprocess.run(["git", "-C", str(repo), "-c", "core.quotepath=false", *args],
                       capture_output=True)
    return r.stdout.decode("utf-8", "replace")


def scan(repo: Path, diff: str, banks):
    found, cache = [], {}
    for rel, line, commit in parse_diff(diff):
        if rel not in cache:
            cache[rel] = bank_of((repo / rel).resolve(), banks)
        bank = cache[rel]
        if not bank:
            continue
        for cls, other, mk in hits_in_line(line, bank, banks):
            found.append((cls, bank[0], other, mk, rel, line.strip(), commit))
    return found


def report(found, limit=SHOW_LINES):
    groups = {}
    for cls, bank, other, mk, rel, line, commit in found:
        groups.setdefault((cls, bank, rel, other), []).append((line, commit))
    for (cls, bank, rel, other), rows in sorted(groups.items()):
        tag = "УТЕЧКА" if cls == 2 else "не тот банк"
        print("  [%s] %s: %s — маркер банка %s (%d стр.)" % (tag, bank, rel, other, len(rows)))
        for line, commit in rows[:limit]:
            print("      %s%s" % (commit[:8] + " " if commit else "", line[:160]))


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--memory", help="корень памяти (по умолчанию ищется от репозитория)")
    ap.add_argument("--history", metavar="SINCE",
                    help="замер по git log -p памяти и всех банков-репозиториев с даты (7.days, 2026-09-17)")
    ap.add_argument("--all", action="store_true", help="в --history показывать все строки, а не по 3")
    a = ap.parse_args()

    root = Path(git(Path.cwd(), "rev-parse", "--show-toplevel").strip() or os.getcwd()).resolve()
    mem = Path(a.memory).resolve() if a.memory else find_memory(root)
    if not mem:
        return 0
    banks = read_banks(mem)
    if not any(b[3] for b in banks):
        return 0            # маркеров в конфиге нет — проверять нечем

    if a.history:
        repos = [mem] + [b[1] for b in banks if (b[1] / ".git").exists()]
        found = []
        for repo in repos:
            # в самой памяти банки-каталоги — только их пути: журнал и доска весят
            # сотни коммитов в неделю, а проверять в них нечего
            paths = [str(b[1].relative_to(repo)) for b in banks
                     if b[1] != repo and b[1].is_relative_to(repo) and not (b[1] / ".git").exists()]
            if repo == mem and not paths:
                continue
            log = git(repo, "log", "-p", "--no-merges", "--no-color", "-U0",
                      "--since=" + a.history, "--format=%x01commit %H",
                      *(["--", *paths] if repo == mem else []))
            found += scan(repo, log, banks)
        c1 = [f for f in found if f[0] == 1]
        c2 = [f for f in found if f[0] == 2]
        print("yamem routing с %s: класс 1 (не тот банк) — %d строк в %d коммитах; "
              "класс 2 (утечка в отчуждаемый) — %d строк в %d коммитах"
              % (a.history, len(c1), len({f[6] for f in c1}), len(c2), len({f[6] for f in c2})))
        report(found, limit=10 ** 6 if a.all else SHOW_LINES)
        return 0

    diff = git(root, "diff", "--cached", "--no-color", "-U0", "--diff-filter=ACMR")
    found = scan(root, diff, banks)
    if not found:
        return 0
    c2 = [f for f in found if f[0] == 2]
    print("yamem: маркеры контура не в своем банке:")
    report(found)
    if c2 and not os.environ.get(ENV_ALLOW):
        print()
        print("yamem: маркер чужого контура в отчуждаемом банке — его читают посторонние, коммит остановлен.")
        print("       Обезличить (srv-db1.domain.local, «площадка A») или перенести факт в банк площадки.")
        print("       Маркер ложный — сузить его в yamem.config.yaml; обойти разово:")
        print("         %s=1 git commit …" % ENV_ALLOW)
        return 1
    if not c2:
        print("       (предупреждение: факту место в банке, чей маркер найден, — коммит не остановлен)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
