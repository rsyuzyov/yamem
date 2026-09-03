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
import datetime
import re
import sys
from pathlib import Path

from yamem_common import (journal_root, md_cell, parse_frontmatter, splice,
                          validate_frontmatter)

BACKLOG_BEGIN = "<!-- yamem:backlog:begin -->"
BACKLOG_END = "<!-- yamem:backlog:end -->"
ARCHIVE_BEGIN = "<!-- yamem:archive:begin -->"
ARCHIVE_END = "<!-- yamem:archive:end -->"
GENERATED_NOTE = "<!-- сгенерировано scripts/gen-backlog.py — руками не править -->"

STATUSES = ("new", "in-progress", "waiting", "done")
REQUIRED = ("title", "created", "updated", "status")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
MONTH_RE = re.compile(r"^\d{4}-\d{2}$")
TODAY = datetime.date.today().isoformat()

# Имя файла-промпта, по которому задача продолжается в новой сессии. Канон один,
# но прежние имена распознаём тоже: они уже лежат в задачах, и потерять их нельзя.
PROMPT_CANON = "prompt-next-session.md"
PROMPT_RE = re.compile(r"(?i)prompt")

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
    """[(путь от tasks/, поля)] + жалобы + мягкие замечания.

    Жалобы (`complaints`) — дефекты формы, они валят `--check` и держат коммит.
    Мягкие (`soft`) — подсказки по СОДЕРЖАНИЮ (просроченный срок у незакрытой
    задачи): печатаются, но кода возврата не портят. Разделение намеренное:
    pre-commit не должен вставать из-за того, что оператор ещё не решил,
    закрывать задачу или двигать срок.
    """
    rows, complaints, soft = [], [], []
    if not tasks_dir.is_dir():
        return rows, complaints, soft  # памяти без задач ещё нет — это не дефект

    def take(folder: Path, rel: str, month: str):
        task_md = folder / "task.md"
        if not task_md.is_file():
            complaints.append(f"tasks/{rel}/: нет task.md")
            return
        # 🔑 форму проверяем строго, а читаем толерантно: `title: ЭДО prodline: адреса`
        # без кавычек разбирается «наполовину» и молча уезжает в бэклог обрезанным
        for problem in validate_frontmatter(task_md):
            complaints.append(f"tasks/{rel}/task.md: {problem}")
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
        for key in ("created", "updated", "deadline"):
            if fm.get(key) and not DATE_RE.match(fm[key]):
                complaints.append(
                    f"tasks/{rel}/task.md: {key} `{fm[key]}` не в формате YYYY-MM-DD"
                )
        # 🎯 Просроченный срок у незакрытой задачи — почти всегда не «горит»,
        # а «работа сделана, а снять срок забыли»: замер 03.09.2026 показал, что
        # закрытия идут пачками на ревизиях, а между ними просроченное копится
        # и каждое утро подаётся как срочное. Это ЖАЛОБА, а не ошибка: коммит
        # не останавливаем — решить, закрыть задачу или сдвинуть срок, может
        # только человек с фактом на руках.
        deadline = fm.get("deadline", "")
        if (DATE_RE.match(deadline) and fm.get("status") != "done"
                and deadline < TODAY):
            soft.append(
                f"tasks/{rel}/task.md: срок {deadline} прошёл, а status "
                f"`{fm.get('status')}` ⟹ закрыть задачу либо сдвинуть `deadline:`"
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
    return rows, complaints, soft


def prompt_of(tasks_dir: Path, rel: str):
    """Файл-промпт задачи: (имя, дата правки) либо None.

    🔑 Это точка входа в незакрытую задачу: с ним «продолжаем X» решается одним
    чтением, без него — гаданием по заголовкам. Канон — `prompt-next-session.md`,
    но задачи с прежними именами (`next-session-prompt.md`, `prompt-<дата>.md`)
    уже существуют ⟹ ищем по вхождению `prompt`, иначе они молча пропадут из вида.
    """
    folder = tasks_dir / rel
    if not folder.is_dir():
        return None
    canon = folder / PROMPT_CANON
    found = canon if canon.is_file() else next(
        (p for p in sorted(folder.glob("*.md")) if PROMPT_RE.search(p.name)), None)
    if not found:
        return None
    from datetime import datetime
    return (found.name, datetime.fromtimestamp(found.stat().st_mtime).strftime("%Y-%m-%d"))


def epics(rows, complaints):
    """{rel зонтика: [подзадачи]} + жалобы на битые ссылки `epic`.

    Ссылка пишется коротким именем папки (`epic: perehod-na-platformu-edo`), без
    месяца: месяц зонтика знать не нужно, а имена папок по парку уникальны.
    Отдельного поля «я зонтик» нет намеренно — зонтик тот, на кого сослались.
    """
    by_slug = {}
    for rel, _fm in rows:
        by_slug.setdefault(rel.split("/")[-1], []).append(rel)

    index, fm_of = {}, dict(rows)
    for rel, fm in rows:
        ref = (fm.get("epic") or "").strip().strip("/")
        if not ref:
            continue
        ref = ref.split("/")[-1]
        targets = by_slug.get(ref, [])
        if not targets:
            complaints.append(f"tasks/{rel}/task.md: epic `{ref}` — такой задачи нет")
            continue
        if len(targets) > 1:
            complaints.append(
                f"tasks/{rel}/task.md: epic `{ref}` неоднозначен: {', '.join(targets)}")
            continue
        target = targets[0]
        if target == rel:
            complaints.append(f"tasks/{rel}/task.md: epic ссылается на саму задачу")
            continue
        # Один уровень намеренно: дерево тем даёт ту же навигацию, но требует обхода
        # и порождает циклы, а чинить их будет человек посреди рабочей задачи.
        if (fm_of.get(target, {}).get("epic") or "").strip():
            complaints.append(
                f"tasks/{rel}/task.md: epic `{ref}` сам входит в тему — вложенность в один уровень")
            continue
        index.setdefault(target, []).append((rel, fm))

    for target, kids in index.items():
        open_kids = [k for k, f in kids if f.get("status") != "done"]
        if fm_of.get(target, {}).get("status") == "done" and open_kids:
            complaints.append(
                f"tasks/{target}/task.md: тема закрыта, а подзадач открыто {len(open_kids)}")
    return index


def epic_line(rel, fm, kids, tasks_dir: Path) -> str:
    """Строка темы: счётчики по статусам подзадач + признак промпта."""
    n = lambda s: sum(1 for _, f in kids if f.get("status") == s)  # noqa: E731
    parts = [f"{n('in-progress')} в работе", f"{n('waiting')} ждут",
             f"{n('new')} новых", f"{n('done')} закрыто"]
    counts = ", ".join(p for p in parts if not p.startswith("0 "))
    pr = prompt_of(tasks_dir, rel)
    tail = f" · 📝 промпт {pr[1]}" if pr else ""
    return (f"- [{md_cell(fm.get('title', rel))}](tasks/{rel}/) — "
            f"{len(kids)} подзадач: {counts or 'нет открытых'}{tail}")


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


def render_backlog(rows, index=None, tasks_dir: Path = None) -> str:
    """Незакрытые задачи: темы → в работе → квадранты Эйзенхауэра → ожидание.

    ⚠️ Подзадачи темы остаются и в своих секциях: раздел тем — оглавление, а не
    отдельное хранилище. Убрать их оттуда значило бы спрятать задачу от того, кто
    читает бэклог по срочности, а не по теме.
    """
    index = index or {}
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

    fm_of = dict(rows)
    live = {rel: kids for rel, kids in index.items()
            if fm_of.get(rel, {}).get("status") != "done"
            or any(f.get("status") != "done" for _, f in kids)}
    if live:
        out.append(f"## ☂ Темы ({len(live)})")
        out.append("")
        out.append("Точка входа в длинную работу: читать промпт темы, а не выбирать "
                   "подзадачу по заголовку.")
        out.append("")
        for rel in sorted(live, key=lambda r: fm_of.get(r, {}).get("updated", ""),
                          reverse=True):
            out.append(epic_line(rel, fm_of.get(rel, {}), live[rel], tasks_dir))
        out.append("")

    # колонка темы появляется, только когда темы заведены — иначе лишний столбец «—»
    of_epic = {kid: rel for rel, kids in index.items() for kid, _ in kids}
    columns = BACKLOG_COLUMNS if not index else BACKLOG_COLUMNS[:1] + [
        ("тема", lambda s, fm, _m=of_epic: f"`{_m[s].split('/')[-1]}`" if s in _m else "—")
    ] + BACKLOG_COLUMNS[1:]

    for title, section in sections:
        if not section:
            continue
        out.append(f"## {title} ({len(section)})")
        out.append("")
        out += table(order(section), columns)
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

    # ⚠️🔴 Консоль Windows по умолчанию не UTF-8 (cp1251/cp866), и `print` с «⚠️»
    # на ней падает с UnicodeEncodeError. Ловушка в том, ЧТО именно печатается:
    # эмодзи стоит только в строках жалоб ⟹ на чистой памяти проверка проходит
    # молча и выглядит рабочей, а умирает ровно тогда, когда ей есть что сказать.
    # Тот же вызов уже стоит в `yamem-start.py`.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    mem = Path(args.memory).resolve()
    if not mem.is_dir():
        sys.exit(f"нет каталога памяти: {mem}")
    root = journal_root(mem)
    if not root.is_dir():
        sys.exit(f"нет каталога журнала памяти: {root}")

    rows, complaints, soft = collect(root / "tasks")
    index = epics(rows, complaints)
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

    build("backlog.md", BACKLOG_BEGIN, BACKLOG_END,
          render_backlog(rows, index, root / "tasks"), BACKLOG_SKELETON)
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
    # ⚠️ Мягкие замечания печатаем ПОСЛЕ жалоб и отдельным заголовком: их адресат —
    # человек, решающий судьбу задачи, а не автор коммита.
    if soft:
        print(f"  🔚 задач с прошедшим сроком: {len(soft)} — закрыть или сдвинуть срок:")
        for s in soft[:10]:
            print("     ", s)
        if len(soft) > 10:
            print(f"      … ещё {len(soft) - 10}")
    for p in changed:
        print(("обновлён: " if args.apply else "изменится: ") + str(p))
    if not changed:
        print("представления актуальны")
    if args.check and (changed or complaints):
        sys.exit(1)


if __name__ == "__main__":
    main()
