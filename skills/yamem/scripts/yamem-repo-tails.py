#!/usr/bin/env python3
"""Хвосты в репозиториях, которые трогали сессии: незакоммиченное, непушенное, тайники.

Зачем: стартер следит только за памятью и банками, а сессии работают и в других
репозиториях — сам проект, паки навыков, репозитории 1С, ops. Правка, брошенная там,
не видна никому: «коммит на главном агенте» забывается, pre-commit падает молча,
субагент заканчивает без push. Прогоняется на каждой оптимизации памяти
(OPTIMIZATION.md, фаза 1).

Какие репозитории «трогали» — из транскриптов Claude Code за окно: рабочий каталог
записей (`cwd`), пути Edit/Write/NotebookEdit и каталоги из `cd`/`git -C`/`Set-Location`
в командах. Дисциплины агента не требует. Плюс всегда — репозиторий памяти и банки.
По тем же транскриптам у каждого грязного файла видно, какая сессия правила его
последней, — с нее и начинается разбор «почему не закоммичено».

Запуск:
    python yamem-repo-tails.py --memory <путь к .agents/memory> [--since YYYY-MM-DD]
    python yamem-repo-tails.py --memory <путь> --since <сегодня> --sid <свой sid>
По умолчанию окно — с даты «Последняя оптимизация:» в MEMORY.md; второй вид —
своя проверка сессии перед завершением. Только читает.
Код возврата 1, если хвосты есть.
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from yamem_common import read_banks  # noqa: E402

CWD_RE = re.compile(r'"cwd":"((?:[^"\\]|\\.)*)"')
TS_RE = re.compile(r'"timestamp":"([^"]+)"')
CMD_PATH_RE = re.compile(
    r'(?:\bgit\s+-C|\bcd|\bpushd|\bSet-Location(?:\s+-Path)?)\s+'
    r'("[^"]+"|\'[^\']+\'|[^\s;&|)]+)')
EDIT_TOOLS = {"Edit", "Write", "NotebookEdit", "MultiEdit"}
SHELL_TOOLS = {"Bash", "PowerShell"}
# правка моложе — скорее всего, сессия еще в работе; тот же порог, что у стартера
FRESH_HOURS = 3


def mangle(project: Path) -> str:
    """Имя каталога транскриптов проекта: всё, кроме букв и цифр, — дефис."""
    return re.sub(r"[^A-Za-z0-9]", "-", str(project))


def transcript_dirs(project: Path, override: str) -> list:
    if override:
        return [Path(override)]
    root = Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude")) / "projects"
    want = mangle(project).lower()
    # регистр буквы диска плавает (`c--` и `C--`) — сравниваем без регистра
    return [d for d in root.glob("*") if d.is_dir() and d.name.lower() == want]


def norm_path(raw: str, base: str = "") -> str:
    """Абсолютный путь без `..`, с заглавной буквой диска; пустая строка — не путь."""
    raw = (raw or "").strip("'\"")
    if not raw or raw[0] in "$%~-" or "*" in raw:
        return ""
    msys = re.match(r"^/([a-zA-Z])(/.*)?$", raw)  # /c/Users/... из Git Bash
    if msys:
        raw = f"{msys.group(1)}:{msys.group(2) or '/'}"
    if not os.path.isabs(raw):
        if not base:
            return ""
        raw = os.path.join(base, raw)
    path = os.path.normpath(raw)
    return path[0].upper() + path[1:] if re.match(r"^[a-z]:", path) else path


def key(path: str) -> str:
    return os.path.normcase(path)


def collect_touches(dirs: list, since: datetime):
    """По транскриптам окна, включая субагентов.

    Возврат: ({каталог или файл: {sid: последнее время}}, {файл: (время, sid)} —
    кто правил файл последним).
    """
    since_ts, since_iso = since.timestamp(), since.strftime("%Y-%m-%dT%H:%M")
    touches, edits = {}, {}
    for tdir in dirs:
        for jsonl in tdir.rglob("*.jsonl"):
            if jsonl.stat().st_mtime < since_ts:
                continue
            # субагент пишет в <sid>/subagents/ — его правки числим за родителем
            sid = (jsonl.parent.parent.name if jsonl.parent.name == "subagents"
                   else jsonl.stem)[:8]
            with open(jsonl, encoding="utf-8", errors="replace") as transcript:
                for line in transcript:
                    stamp = TS_RE.search(line)
                    if not stamp or stamp.group(1) < since_iso:
                        continue
                    when = stamp.group(1)
                    cwd_match = CWD_RE.search(line)
                    cwd = norm_path(json.loads(f'"{cwd_match.group(1)}"')) if cwd_match else ""
                    paths = [cwd]
                    if '"tool_use"' in line:
                        edited, visited = tool_paths(line, cwd)
                        paths += edited + visited
                        for path in edited:
                            if path and when > edits.get(key(path), ("",))[0]:
                                edits[key(path)] = (when, sid)
                    for path in filter(None, paths):
                        per_sid = touches.setdefault(path, {})
                        per_sid[sid] = max(per_sid.get(sid, ""), when)
    return touches, edits


def tool_paths(line: str, cwd: str):
    """([правленные файлы], [каталоги из команд]) одной записи транскрипта."""
    try:
        rec = json.loads(line)
    except ValueError:
        return [], []
    edited, visited = [], []
    content = (rec.get("message") or {}).get("content")
    for block in content if isinstance(content, list) else []:
        if not isinstance(block, dict) or block.get("type") != "tool_use":
            continue
        tool_input = block.get("input") or {}
        if block.get("name") in EDIT_TOOLS:
            edited.append(norm_path(tool_input.get("file_path")
                                    or tool_input.get("notebook_path"), cwd))
        elif block.get("name") in SHELL_TOOLS:
            visited += [norm_path(raw, cwd)
                        for raw in CMD_PATH_RE.findall(tool_input.get("command") or "")]
    return edited, visited


def repo_root(path: Path, cache: dict):
    """Ближайший вверх каталог с `.git` (каталог или файл-ссылка субмодуля)."""
    probe = path
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    if probe.is_file():
        probe = probe.parent
    chain = []
    while True:
        probe_key = key(str(probe))
        if probe_key in cache:
            root = cache[probe_key]
            break
        chain.append(probe_key)
        if (probe / ".git").exists():
            root = probe
            break
        if probe == probe.parent:
            root = None
            break
        probe = probe.parent
    for chain_key in chain:
        cache[chain_key] = root
    return root


def run(cmd, cwd):
    proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=60)
    return proc.returncode, proc.stdout, proc.stderr


def stashes(repo: Path) -> list:
    """[(дата, сообщение, уже_в_HEAD)]: тайник, чьи файлы совпадают с HEAD, можно снять."""
    code, out, _ = run(["git", "stash", "list", "--format=%gd%x1f%ci%x1f%gs"], repo)
    found = []
    for line in out.splitlines() if code == 0 else []:
        ref, date, message = (line.split("\x1f") + ["", ""])[:3]
        _, names, _ = run(["git", "-c", "core.quotepath=off", "stash", "show",
                           "--name-only", ref], repo)
        files = names.split()
        same = bool(files) and run(["git", "diff", "--quiet", ref, "HEAD", "--"] + files,
                                   repo)[0] == 0
        found.append((date[:16], message, same))
    return found


def inspect(repo: Path) -> dict:
    """Состояние рабочей копии: грязь, ahead, upstream, тайники, прерванные операции."""
    code, out, err = run(["git", "-c", "core.quotepath=off", "status", "--porcelain=v1",
                          "-b", "--untracked-files=normal", "--ignore-submodules=dirty"],
                         repo)
    if code != 0 and "not a git repository" in err:
        return {"skip": True}  # пустой `.git` (так бывает в профиле) — не репозиторий
    if code != 0:
        return {"error": ((err or out).strip().splitlines() or ["?"])[0][:120]}
    lines = out.splitlines()
    head = lines[0][3:] if lines and lines[0].startswith("## ") else ""
    state = {"branch": head.split("...")[0].split(" ")[0], "dirty": [], "pointers": [],
             "upstream": "..." in head, "detached": "no branch" in head, "op": ""}
    ahead = re.search(r"ahead (\d+)", head)
    state["ahead"] = int(ahead.group(1)) if ahead else 0
    now = time.time()
    for line in lines[1:]:
        rel = line[3:].split(" -> ")[-1].strip('"')
        if (repo / rel).is_dir() and (repo / rel / ".git").exists():
            # указатель субмодуля: сам субмодуль проверяется отдельной строкой отчета
            state["pointers"].append(rel)
            continue
        try:
            age = (now - (repo / rel).stat().st_mtime) / 3600
        except OSError:
            age = 0.0  # удаленный файл: возраста нет
        state["dirty"].append((line[:2], rel, age))
    state["stash"] = stashes(repo)
    code, gitdir, _ = run(["git", "rev-parse", "--absolute-git-dir"], repo)
    gitdir = Path(gitdir.strip()) if code == 0 else repo / ".git"
    for marker, name in (("rebase-merge", "rebase"), ("rebase-apply", "rebase/am"),
                         ("MERGE_HEAD", "merge"), ("CHERRY_PICK_HEAD", "cherry-pick")):
        if (gitdir / marker).exists():
            state["op"] = name
    return state


def session_label(mem: Path, sid: str, cache: dict) -> str:
    """Тема сессии: заголовок ее дневника или поле topic на доске."""
    if sid in cache:
        return cache[sid]
    label = "дневника нет"
    for diary in sorted(mem.glob(f"diary/*/*.{sid}.md"), reverse=True):
        try:
            label = diary.read_text(encoding="utf-8").splitlines()[0].lstrip("# ").strip()[:90]
            break
        except (OSError, IndexError):
            continue
    else:
        board = mem / ".sessions" / f"{sid}.md"
        topic = (re.search(r"^topic:\s*(.+)$", board.read_text(encoding="utf-8"), re.M)
                 if board.exists() else None)
        if topic:
            label = topic.group(1)[:90]
    cache[sid] = label
    return label


def last_optimization(mem: Path):
    try:
        text = (mem / "MEMORY.md").read_text(encoding="utf-8")
    except OSError:
        return None
    found = re.search(r"Последняя оптимизация:\s*(\d{4}-\d{2}-\d{2})", text)
    return found.group(1) if found else None


def describe_dirty(repo: str, dirty: list, edits: dict) -> list:
    """Строки про грязные файлы: брошенные отдельно от свежих, с автором правки."""
    lines = []
    stale = [item for item in dirty if item[2] >= FRESH_HOURS]
    fresh = len(dirty) - len(stale)
    if stale:
        untracked = sum(1 for status, _, _ in stale if status == "??")
        lines.append(f"незакоммичено {len(stale)} старше {FRESH_HOURS} ч "
                     f"(из них неотслеживаемых {untracked}), старейшее "
                     f"{max(age for _, _, age in stale):.0f} ч:")
        for status, rel, age in sorted(stale, key=lambda item: -item[2])[:8]:
            author = edits.get(key(norm_path(os.path.join(repo, rel))))
            who = f" ← `{author[1]}` {author[0][:16].replace('T', ' ')}" if author else ""
            lines.append(f"  - `{status.strip() or '?'}` `{rel}` ({age:.0f} ч){who}")
        if len(stale) > 8:
            lines.append(f"  - … и еще {len(stale) - 8}")
    if fresh:
        lines.append(f"свежих правок (< {FRESH_HOURS} ч, возможно, сессия в работе): {fresh}")
    return lines


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--memory", required=True, help="путь к .agents/memory")
    ap.add_argument("--since", help="YYYY-MM-DD; по умолчанию — последняя оптимизация")
    ap.add_argument("--project", help="корень проекта; по умолчанию — текущий каталог")
    ap.add_argument("--transcripts", default="", help="каталог транскриптов вместо вычисленного")
    ap.add_argument("--sid", help="только репозитории, которые трогала эта сессия (8 символов)")
    args = ap.parse_args()
    if hasattr(sys.stdout, "reconfigure"):  # эмодзи и кириллица в консоли Windows
        sys.stdout.reconfigure(encoding="utf-8")

    mem = Path(args.memory).resolve()
    project = Path(args.project or os.getcwd()).resolve()
    since_day = args.since or last_optimization(mem) or "1970-01-01"
    since = datetime.strptime(since_day, "%Y-%m-%d").replace(tzinfo=timezone.utc)

    dirs = transcript_dirs(project, args.transcripts)
    touches, edits = collect_touches(dirs, since)
    cache, repos = {}, {}
    for path, per_sid in touches.items():
        root = repo_root(Path(path), cache)
        if root is None:
            continue
        merged = repos.setdefault(norm_path(str(root)), {})
        for sid, when in per_sid.items():
            merged[sid] = max(merged.get(sid, ""), when)
    if args.sid:
        # своя проверка перед концом сессии: чужие хвосты — дело оптимизации
        repos = {repo: per_sid for repo, per_sid in repos.items() if args.sid[:8] in per_sid}
    for base in [] if args.sid else [mem] + [bank_dir for _, bank_dir, _ in read_banks(mem)]:
        root = repo_root(base.resolve(), cache)
        if root is not None:
            repos.setdefault(norm_path(str(root)), {})
    # одна и та же рабочая копия могла прийти путями в разном регистре
    unique = {}
    for repo, per_sid in repos.items():
        merged = unique.setdefault(key(repo), (repo, {}))[1]
        for sid, when in per_sid.items():
            merged[sid] = max(merged.get(sid, ""), when)
    repos = dict(unique.values())

    with ThreadPoolExecutor(max_workers=8) as pool:
        states = dict(zip(repos, pool.map(inspect, [Path(r) for r in repos])))

    print(f"# Хвосты в репозиториях с {since_day}")
    print(f"транскрипты: {', '.join(str(d) for d in dirs) or 'не найдены'} · "
          f"репозиториев: {len(repos)}\n")
    tails, labels = 0, {}
    for repo in sorted(repos, key=str.lower):
        state = states[repo]
        if state.get("skip"):
            continue
        problems = []
        if state.get("error"):
            problems.append(f"git: {state['error']}")
        else:
            if state["op"]:
                problems.append(f"🔴 прерван {state['op']} — синхронизация памяти встанет у всех")
            problems += describe_dirty(repo, state["dirty"], edits)
            if state["pointers"]:
                problems.append("указатель субмодуля не закоммичен: "
                                + ", ".join(f"`{rel}`" for rel in state["pointers"]))
            if state["detached"]:
                problems.append("detached HEAD")
            elif not state["upstream"]:
                problems.append(f"ветка `{state['branch']}` без upstream — push некуда")
            if state["ahead"]:
                problems.append(f"не запушено коммитов: {state['ahead']}")
            for date, message, same in state["stash"]:
                verdict = "содержимое уже в HEAD — снять" if same else "разобрать"
                problems.append(f"тайник {date} «{message[:70]}» — {verdict}")
        if not problems:
            continue
        tails += 1
        print(f"## {repo}")
        for problem in problems:
            print(problem if problem.startswith("  ") else f"- {problem}")
        for sid, when in sorted(repos[repo].items(), key=lambda item: item[1],
                                reverse=True)[:5]:
            print(f"  ↳ `{sid}` {when[:16].replace('T', ' ')} — "
                  f"{session_label(mem, sid, labels)}")
        print()
    print(f"итого: с хвостами {tails}, чистых {len(repos) - tails}")
    return 1 if tails else 0


if __name__ == "__main__":
    sys.exit(main())
