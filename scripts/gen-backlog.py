#!/usr/bin/env python3
"""Сборка `backlog.md` и `archive.md` из задач.

Источник правды — фронтматтеры `tasks/<slug>/task.md`. Эти два файла лишь
показывают их человеку, поэтому правится задача, а представление собирается:

    python scripts/gen-backlog.py [--memory <path>] [--apply] [--check]

Без `--apply` — сухой прогон: печатает, что изменится, и ничего не пишет.
`--check` даёт ненулевой код, если представление разошлось с задачами или
в задачах есть дефекты фронтматтера (для pre-commit).

Разметка внутри файлов — блоки по маркерам, всё вне них не трогается:

    <!-- yamem:backlog:begin -->  ...  <!-- yamem:backlog:end -->
    <!-- yamem:archive:begin -->  ...  <!-- yamem:archive:end -->
"""
import argparse
import re
import sys
from pathlib import Path

from yamem_common import local_root, md_cell, parse_frontmatter, splice

BACKLOG_BEGIN = "<!-- yamem:backlog:begin -->"
BACKLOG_END = "<!-- yamem:backlog:end -->"
ARCHIVE_BEGIN = "<!-- yamem:archive:begin -->"
ARCHIVE_END = "<!-- yamem:archive:end -->"
GENERATED_NOTE = "<!-- сгенерировано scripts/gen-backlog.py — руками не править -->"

STATUSES = ("new", "in-progress", "waiting", "done")
REQUIRED = ("title", "created", "updated", "status")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
MONTH_RE = re.compile(r"^\d{4}-\d{2}$")

BACKLOG_SKELETON = f"""# Бэклог

> Представление задач из `tasks/*/task.md`. Правится задача, а не этот файл:
> сборка — `python scripts/gen-backlog.py --memory <путь> --apply`.

{BACKLOG_BEGIN}
{BACKLOG_END}

Завершённые задачи: см. `archive.md`
"""

ARCHIVE_SKELETON = f"""# Архив

> Представление завершённых задач (`status: done`) из `tasks/*/task.md`.
> Правится задача, а не этот файл.

{ARCHIVE_BEGIN}
{ARCHIVE_END}
"""


def truthy(value: str) -> bool:
    return str(value).strip().lower() in ("true", "yes", "да", "1", "+")


def collect(tasks_dir: Path):
    """[(путь от tasks/, поля)] + жалобы. Задача = папка с task.md в каталоге месяца."""
    rows, complaints = [], []
    if not tasks_dir.is_dir():
        return rows, complaints  # памяти без задач ещё нет — это не дефект

    def take(folder: Path, rel: str, month: str):
        task_md = folder / "task.md"
        if not task_md.is_file():
            complaints.append(f"tasks/{rel}/: нет task.md")
            return
        fm = parse_frontmatter(task_md)
        if not fm:
            complaints.append(f"tasks/{rel}/task.md: нет фронтматтера")
            return

        missing = [k for k in REQUIRED if not fm.get(k)]
        if missing:
            complaints.append(
                f"tasks/{rel}/task.md: нет обязательных полей: {', '.join(missing)}"
            )
        if fm.get("status") and fm["status"] not in STATUSES:
            complaints.append(
                f"tasks/{rel}/task.md: status `{fm['status']}` вне набора {'/'.join(STATUSES)}"
            )
        for key in ("created", "updated"):
            if fm.get(key) and not DATE_RE.match(fm[key]):
                complaints.append(
                    f"tasks/{rel}/task.md: {key} `{fm[key]}` не в формате YYYY-MM-DD"
                )
        created, updated = fm.get("created", ""), fm.get("updated", "")
        if DATE_RE.match(created) and DATE_RE.match(updated) and updated < created:
            complaints.append(
                f"tasks/{rel}/task.md: updated {updated} раньше created {created}"
            )
        # каталог назван месяцем заведения — иначе путь задачи врёт
        if month and DATE_RE.match(created) and created[:7] != month:
            complaints.append(
                f"tasks/{rel}/task.md: лежит в каталоге {month}, а created {created}"
            )
        rows.append((rel, fm))

    for entry in sorted(tasks_dir.iterdir()):
        if entry.name.startswith("."):
            continue
        if entry.is_file():
            if entry.suffix == ".md":
                complaints.append(
                    f"tasks/{entry.name}: файл вне папки задачи — "
                    f"задача должна быть папкой с task.md"
                )
            continue
        if MONTH_RE.match(entry.name):
            for folder in sorted(entry.iterdir()):
                if folder.is_dir() and not folder.name.startswith("."):
                    take(folder, f"{entry.name}/{folder.name}", entry.name)
            continue
        take(entry, entry.name, "")  # задача в корне tasks/ — старая раскладка
    return rows, complaints


def order(rows):
    """Свежие сверху, при равной дате — по названию."""
    return sorted(rows, key=lambda r: (r[1].get("updated", ""), r[1].get("title", "")),
                  reverse=True)


def table(rows, columns) -> list:
    """columns: [(заголовок, функция от (slug, поля))]."""
    head = "| " + " | ".join(t for t, _ in columns) + " |"
    sep = "| " + " | ".join("---" for _ in columns) + " |"
    out = [head, sep]
    for slug, fm in rows:
        out.append("| " + " | ".join(fn(slug, fm) for _, fn in columns) + " |")
    out.append("")
    return out


def link(slug, fm) -> str:
    return f"[{md_cell(fm.get('title', slug))}](tasks/{slug}/)"


BACKLOG_COLUMNS = [
    ("задача", link),
    ("направление", lambda s, fm: md_cell(fm.get("area", "")) or "—"),
    ("клиент", lambda s, fm: md_cell(fm.get("client", "")) or "—"),
    ("обновлена", lambda s, fm: fm.get("updated", "—")),
]

ARCHIVE_COLUMNS = [
    ("задача", link),
    ("направление", lambda s, fm: md_cell(fm.get("area", "")) or "—"),
    ("клиент", lambda s, fm: md_cell(fm.get("client", "")) or "—"),
    ("заведена", lambda s, fm: fm.get("created", "—")),
    ("закрыта", lambda s, fm: fm.get("updated", "—")),
]


def render_backlog(rows) -> str:
    """Незакрытые задачи: в работе → квадранты Эйзенхауэра → ожидание."""
    active = [r for r in rows if r[1].get("status") != "done"]
    working = [r for r in active if r[1].get("status") == "in-progress"]
    waiting = [r for r in active if r[1].get("status") == "waiting"]
    rest = [r for r in active if r[1].get("status") not in ("in-progress", "waiting")]

    def quadrant(urgent: bool, important: bool):
        return [r for r in rest
                if truthy(r[1].get("urgent")) == urgent
                and truthy(r[1].get("important")) == important]

    sections = [
        ("🔧 В работе", working),
        ("🔥 Срочно и важно", quadrant(True, True)),
        ("⭐ Важно, не срочно", quadrant(False, True)),
        ("⚡ Срочно, не важно", quadrant(True, False)),
        ("💤 Идеи — ни срочно, ни важно", quadrant(False, False)),
        ("⏸ Ожидает внешнего действия", waiting),
    ]

    out = [GENERATED_NOTE, ""]
    out.append(
        f"Открытых задач: **{len(active)}** — в работе {len(working)}, "
        f"ждут {len(waiting)}, прочих {len(rest)}."
    )
    out.append("")
    for title, section in sections:
        if not section:
            continue
        out.append(f"## {title} ({len(section)})")
        out.append("")
        out += table(order(section), BACKLOG_COLUMNS)
    if not active:
        out.append("Открытых задач нет.")
        out.append("")
    return "\n".join(out).rstrip() + "\n"


def render_archive(rows) -> str:
    """Завершённые задачи по месяцу закрытия (`updated` у `status: done`)."""
    done = [r for r in rows if r[1].get("status") == "done"]
    by_month = {}
    for slug, fm in done:
        month = (fm.get("updated") or "")[:7] or "без даты"
        by_month.setdefault(month, []).append((slug, fm))

    out = [GENERATED_NOTE, ""]
    out.append(f"Завершённых задач: **{len(done)}**.")
    out.append("")
    for month in sorted(by_month, reverse=True):
        section = by_month[month]
        out.append(f"## {month} ({len(section)})")
        out.append("")
        out += table(order(section), ARCHIVE_COLUMNS)
    if not done:
        out.append("Завершённых задач пока нет.")
        out.append("")
    return "\n".join(out).rstrip() + "\n"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--memory", default=".agents/memory", help="каталог памяти проекта")
    ap.add_argument("--apply", action="store_true", help="записать изменения")
    ap.add_argument("--check", action="store_true",
                    help="ненулевой код, если представление устарело или задачи дефектны")
    args = ap.parse_args()

    mem = Path(args.memory).resolve()
    if not mem.is_dir():
        sys.exit(f"нет каталога памяти: {mem}")
    root = local_root(mem)
    if not root.is_dir():
        sys.exit(f"нет каталога локальной памяти: {root}")

    rows, complaints = collect(root / "tasks")
    changed, legacy = [], []

    def build(name, begin, end, body, skeleton):
        path = root / name
        # Файл есть, а маркеров нет — память ещё не переведена на задачи-папки.
        # Такой файл не трогаем и коммит не блокируем: это долг миграции, а не поломка.
        if path.is_file() and begin not in path.read_text(encoding="utf-8"):
            legacy.append(name)
            return
        if splice(path, begin, end, body, args.apply, skeleton):
            changed.append(path)

    build("backlog.md", BACKLOG_BEGIN, BACKLOG_END, render_backlog(rows), BACKLOG_SKELETON)
    build("archive.md", ARCHIVE_BEGIN, ARCHIVE_END, render_archive(rows), ARCHIVE_SKELETON)

    # Память ещё не переведена на задачи-папки: представления не собираем и не ругаемся
    # на каждую старую папку без task.md — это долг миграции, а не поломка коммита.
    if legacy:
        print(f"{', '.join(legacy)}: старый формат без маркеров — память ещё не переведена "
              f"на задачи-папки, файлы не трогаю")
        return

    done = sum(1 for _, fm in rows if fm.get("status") == "done")
    print(f"задач: {len(rows)} (открытых {len(rows) - done}, завершённых {done})")
    for c in complaints:
        print("  ⚠️", c)
    for p in changed:
        print(("обновлён: " if args.apply else "изменится: ") + str(p))
    if not changed:
        print("представления актуальны")
    if args.check and (changed or complaints):
        sys.exit(1)


if __name__ == "__main__":
    main()
