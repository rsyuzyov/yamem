#!/usr/bin/env python3
"""Стартер yamem: весь preflight одним вызовом.

Делает то, что раньше было списком из одиннадцати шагов и потому регулярно
выполнялось наполовину: синхронизирует банки, отмечает сессию на доске,
собирает дайджест задач, сводки дневников и коммиты памяти за сутки.

    python scripts/yamem-start.py --memory <path> --sid <sid> [--topic "..."]

Ключи:
    --days N        сколько дней дневника (по умолчанию из конфига)
    --diary РЕЖИМ   heads | red (по умолчанию) | marks | full — насколько подробно
    --no-sync       не трогать git (только чтение)
    --prune         снести записи брошенных сессий (старше 6 часов)

Печатает markdown в stdout. Задачи и дневники не пересказываются целиком:
источник правды — сами файлы, стартер лишь показывает, куда смотреть.
"""
import argparse
import io
import re
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

from yamem_common import local_root, parse_frontmatter, read_config

MONTH_RE = re.compile(r"^\d{4}-\d{2}$")
SESSION_STALE_HOURS = 6
RED = "🔴"
MARKS = ("🔴", "⭐", "⚠️", "🎯", "🔑", "✅")


def run(cmd, cwd=None, timeout=120):
    try:
        p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                           timeout=timeout, encoding="utf-8", errors="replace")
        return p.returncode, (p.stdout or "").strip(), (p.stderr or "").strip()
    except Exception as exc:  # noqa: BLE001 — стартер не должен падать из-за git
        return 1, "", str(exc)


def is_git(path: Path) -> bool:
    return (path / ".git").exists()


def sync(mem: Path, no_sync: bool, out: list):
    """git pull --rebase по банкам. Ошибка синхронизации не блокирует старт."""
    local_path, shared = read_config(mem)
    banks = [("local", mem / local_path, True)]
    banks += [(name, mem / path, True) for name, path in shared]

    out.append("## Синхронизация")
    for name, path, want in banks:
        if not path.is_dir():
            out.append(f"- {name}: ⚠️ нет каталога `{path}`")
            continue
        if not is_git(path):
            out.append(f"- {name}: обычный каталог, git не настроен")
            continue
        if no_sync:
            out.append(f"- {name}: пропущено (`--no-sync`)")
            continue
        code, so, se = run(["git", "pull", "--rebase", "--autostash"], cwd=path)
        if code == 0:
            state = "уже актуально" if "up to date" in so.lower() else so.splitlines()[-1][:80]
            out.append(f"- {name}: synced — {state}")
        else:
            out.append(f"- {name}: ⚠️ NOT synced — {(se or so).splitlines()[0][:100]}")
    out.append("")


def sessions(root: Path, sid: str, topic: str, prune: bool, out: list):
    """Отметиться на доске и показать, кто ещё в работе."""
    board = root / ".sessions"
    board.mkdir(exist_ok=True)
    now = datetime.now()
    stamp = now.strftime("%Y-%m-%d %H:%M")

    mine = board / f"{sid}.md"
    started = stamp
    if mine.is_file():
        m = re.search(r"^started:\s*(.+)$", mine.read_text(encoding="utf-8"), re.M)
        if m:
            started = m.group(1).strip()
    mine.write_text(
        f"started: {started}\nupdated: {stamp}\ntopic: {topic or '—'}\nhosts: —\n",
        encoding="utf-8", newline="\n")

    if is_git(root):
        rel = f".sessions/{sid}.md"
        code, _, _ = run(["git", "add", rel], cwd=root)
        code, _, _ = run(["git", "diff", "--cached", "--quiet", "--", rel], cwd=root)
        if code == 1:  # есть что коммитить
            run(["git", "commit", "-q", "-m", f"sessions: {sid} в работе", "--", rel], cwd=root)
            pcode, _, perr = run(["git", "push", "-q"], cwd=root, timeout=60)
            if pcode != 0:
                out.append(f"- ⚠️ отметка закоммичена, но не запушена: {perr.splitlines()[0][:80]}"
                           if perr else "- ⚠️ отметка закоммичена, но не запушена")

    live, stale = [], []
    for f in sorted(board.glob("*.md")):
        if f.stem == sid:
            continue
        text = f.read_text(encoding="utf-8", errors="replace")
        m = re.search(r"^updated:\s*(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2})", text, re.M)
        upd = None
        if m:
            try:
                upd = datetime.strptime(m.group(1).replace("T", " "), "%Y-%m-%d %H:%M")
            except ValueError:
                upd = None
        t = re.search(r"^topic:\s*(.+)$", text, re.M)
        line = (f.stem, upd, (t.group(1).strip() if t else "—"))
        if upd and now - upd < timedelta(hours=SESSION_STALE_HOURS):
            live.append(line)
        else:
            stale.append(line)

    out.append("## Соседние сессии")
    if live:
        for name, upd, t in live:
            ago = int((now - upd).total_seconds() // 60)
            out.append(f"- 🔵 **{name}** (обновлена {ago} мин назад) — {t[:160]}")
        out.append("⚠️ Не бери задачи, которые уже ведёт соседняя сессия.")
    else:
        out.append("- активных нет")
    if stale:
        if prune:
            for name, _, _ in stale:
                (board / f"{name}.md").unlink(missing_ok=True)
            out.append(f"- снято брошенных записей: {len(stale)}")
        else:
            out.append(f"- брошенных записей (старше {SESSION_STALE_HOURS} ч): {len(stale)}"
                       f" — снять `--prune`")
    out.append(f"- своя отметка записана: `{sid}`")
    out.append("")


def truthy(v) -> bool:
    return str(v).strip().lower() in ("true", "yes", "да", "1", "+")


def tasks(root: Path, out: list):
    """Горячее — строкой, остальное — счётчиками. Тела задач не читаем."""
    rows = []
    tdir = root / "tasks"
    if tdir.is_dir():
        for month in sorted(tdir.iterdir()):
            if not (month.is_dir() and MONTH_RE.match(month.name)):
                continue
            for folder in sorted(month.iterdir()):
                f = folder / "task.md"
                if f.is_file():
                    rows.append((f"{month.name}/{folder.name}", parse_frontmatter(f)))

    openable = [r for r in rows if r[1].get("status") != "done"]
    by = lambda s: [r for r in openable if r[1].get("status") == s]  # noqa: E731
    working, waiting = by("in-progress"), by("waiting")
    rest = [r for r in openable if r[1].get("status") not in ("in-progress", "waiting")]
    hot = [r for r in rest if truthy(r[1].get("urgent")) and truthy(r[1].get("important"))]
    important = [r for r in rest if not truthy(r[1].get("urgent")) and truthy(r[1].get("important"))]
    ideas = [r for r in rest if not truthy(r[1].get("urgent")) and not truthy(r[1].get("important"))]
    urgent_only = [r for r in rest if truthy(r[1].get("urgent")) and not truthy(r[1].get("important"))]

    out.append(f"## Задачи: {len(rows)} (открытых {len(openable)})")
    out.append(f"в работе {len(working)} · ждут {len(waiting)} · срочно+важно {len(hot)} · "
               f"срочно {len(urgent_only)} · важно {len(important)} · идеи {len(ideas)}")
    out.append("")

    def block(title, section, limit=None):
        if not section:
            return
        section = sorted(section, key=lambda r: r[1].get("updated", ""), reverse=True)
        shown = section if limit is None else section[:limit]
        out.append(f"**{title}** ({len(section)})")
        for rel, fm in shown:
            area = fm.get("area", "")
            tail = f" · {area}" if area else ""
            out.append(f"- {fm.get('title', rel)} — `tasks/{rel}/` · {fm.get('updated', '—')}{tail}")
        if limit is not None and len(section) > limit:
            out.append(f"- … ещё {len(section) - limit}, весь список — `backlog.md`")
        out.append("")

    block("🔥 Срочно и важно", hot)
    block("⏸ Ожидает внешнего действия", waiting)
    block("🔧 В работе", working)
    block("⚡ Срочно, не важно", urgent_only, limit=5)


def diary(root: Path, days: int, mode: str, out: list):
    """Сводки дневников: заголовки дня плюс, по режиму, важные строки."""
    today = datetime.now().date()
    wanted = [(today - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(days)]
    files = []
    ddir = root / "diary"
    if ddir.is_dir():
        for day in wanted:
            month = day[:7]
            files += sorted((ddir / month).glob(f"{day}*.md")) if (ddir / month).is_dir() else []
            files += sorted(ddir.glob(f"{day}*.md"))  # старая плоская раскладка

    out.append(f"## Дневники за {days} дн. — {len(files)} файлов, режим `{mode}`")
    if not files:
        out.append("- записей нет")
        out.append("")
        return

    for f in files:
        text = io.open(f, encoding="utf-8", errors="replace").read()
        if mode == "full":
            body = text.strip()
        else:
            keep = []
            for line in text.split("\n"):
                if line.startswith("#"):
                    keep.append(line)
                elif mode == "red" and RED in line:
                    keep.append(line.strip())
                elif mode == "marks" and any(m in line for m in MARKS):
                    keep.append(line.strip())
            body = "\n".join(keep).strip()
        out.append(f"### {f.name}")
        out.append(body if body else "_(без заголовков)_")
        out.append("")
    out.append(f"⚠️ Это сводка. Полный текст дня — читать сам файл в `diary/`.")
    out.append("")


def commits(root: Path, out: list):
    code, so, _ = run(["git", "log", "--since=24 hours ago", "--format=%h %ad %s",
                       "--date=format:%m-%d %H:%M"], cwd=root)
    out.append("## Коммиты памяти за сутки")
    out.append(so if code == 0 and so else "- нет")
    out.append("")


def banks(mem: Path, out: list):
    local_path, shared = read_config(mem)
    names, counts = {}, []
    for name, path in [("local", mem / local_path)] + [(n, mem / p) for n, p in shared]:
        tdir = path / "topics"
        if not tdir.is_dir():
            continue
        files = sorted(f.stem for f in tdir.glob("*.md") if f.name != "README.md")
        counts.append(f"{name}: {len(files)}")
        for f in files:
            names.setdefault(f, []).append(name)
    dup = sorted(n for n, banks_ in names.items() if len(banks_) > 1)
    out.append("## Банки топиков")
    out.append("- " + " · ".join(counts))
    if dup:
        out.append(f"- ⚠️ одноимённые (читать **вместе**, это «универсальное + дельта»): "
                   f"{', '.join(dup)}")
    out.append("")


def maintenance(root: Path, mem: Path, out: list):
    memory_md = root / "MEMORY.md"
    out.append("## Дальше")
    if memory_md.is_file():
        size = memory_md.stat().st_size
        out.append(f"- прочитай `MEMORY.md` ({size // 1024} КБ) — стартер его не пересказывает")
        text = memory_md.read_text(encoding="utf-8", errors="replace")
        m = re.search(r"Последняя оптимизация:\s*(\d{4}-\d{2}-\d{2})", text)
        if m:
            last = datetime.strptime(m.group(1), "%Y-%m-%d").date()
            age = (datetime.now().date() - last).days
            cfg = (mem / "yamem.config.yaml")
            interval = 7
            if cfg.is_file():
                mi = re.search(r"interval_days:\s*(\d+)", cfg.read_text(encoding="utf-8"))
                interval = int(mi.group(1)) if mi else 7
            if age >= interval:
                out.append(f"- ⚠️ оптимизация памяти просрочена: последняя {m.group(1)} "
                           f"({age} дн. назад при интервале {interval})")
            else:
                out.append(f"- оптимизация памяти: последняя {m.group(1)}, "
                           f"следующая через {interval - age} дн.")
    out.append("")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--memory", default=".agents/memory")
    ap.add_argument("--sid", required=True, help="первые 8 символов id сессии")
    ap.add_argument("--topic", default="", help="чем занимается эта сессия")
    ap.add_argument("--days", type=int, default=0, help="дней дневника (0 = из конфига)")
    ap.add_argument("--diary", default="red", choices=("heads", "red", "marks", "full"))
    ap.add_argument("--no-sync", action="store_true")
    ap.add_argument("--prune", action="store_true", help="снести брошенные записи сессий")
    args = ap.parse_args()

    if hasattr(sys.stdout, "reconfigure"):  # эмодзи в выводе на Windows
        sys.stdout.reconfigure(encoding="utf-8")

    mem = Path(args.memory).resolve()
    if not mem.is_dir():
        sys.exit(f"нет каталога памяти: {mem}")
    root = local_root(mem)
    if not root.is_dir():
        sys.exit(f"нет каталога локальной памяти: {root}")

    days = args.days
    if not days:
        cfg = mem / "yamem.config.yaml"
        days = 7
        if cfg.is_file():
            m = re.search(r"diary_read_days:\s*(\d+)", cfg.read_text(encoding="utf-8"))
            days = int(m.group(1)) if m else 7

    out = [f"# yamem preflight — {datetime.now().strftime('%Y-%m-%d %H:%M')}", ""]
    sync(mem, args.no_sync, out)
    sessions(root, args.sid, args.topic, args.prune, out)
    tasks(root, out)
    banks(mem, out)
    commits(root, out)
    diary(root, days, args.diary, out)
    maintenance(root, mem, out)
    print("\n".join(out))


if __name__ == "__main__":
    main()
