#!/usr/bin/env python3
"""Генерация индекса топиков из OKF-фронтматтера.

Собирает `type` и `description` из топиков всех банков и подставляет готовый
список в размеченные блоки:

    <!-- yamem:topics-index:begin -->   ...   <!-- yamem:topics-index:end -->

Куда пишет:
  * `MEMORY.md` в корне памяти — раздел «База знаний», все банки;
  * README каждого банка       — только топики этого банка.

Блоки с маркерами должны уже существовать в файле — скрипт не угадывает, куда
вставлять, и не трогает ничего за их пределами. Запуск без `--apply` — сухой
прогон: показывает, что изменится, и ничего не пишет.

    python scripts/gen-topics-index.py [--memory <path>] [--apply] [--check]

`--check` завершается с ненулевым кодом, если индекс разошёлся с топиками либо
у топика битый фронтматтер (для проверки перед коммитом). ⚠️ Проверка формы —
СТРОГАЯ, в отличие от чтения: толерантный разбор `ключ: значение` пропускал
невалидный YAML, индекс собирался зелёным, а сторонний парсер на том же файле
падал. Вкусовые замечания печатаются, но кода возврата не портят.
"""
import argparse
import os
import sys
from pathlib import Path

from yamem_common import (journal_root, parse_frontmatter, read_banks, splice,
                          validate_frontmatter)

BEGIN = "<!-- yamem:topics-index:begin -->"
END = "<!-- yamem:topics-index:end -->"
GENERATED_NOTE = "<!-- сгенерировано scripts/gen-topics-index.py — руками не править -->"

# порядок разделов в индексе; `type` вне списка попадает в конец, своей группой
TYPE_ORDER = ["rule", "recipe", "reference", "incident"]
# ниже этой длины `description` перестаёт отвечать на вопрос «когда сюда идти»
DESC_MIN = 25
TYPE_TITLE = {
    "rule": "Правила и договорённости",
    "recipe": "Рецепты — как сделать",
    "reference": "Справка — состав, инвентарь, эталоны",
    "incident": "Разборы инцидентов — прецеденты",
}


def collect(topics_dir: Path, bank_root: Path):
    """Топики банка: (rows, дефекты, замечания).

    Дефект — то, из-за чего индекс врёт или файл не читается сторонним инструментом;
    от него `--check` краснеет. Замечание — вкусовое, печатается и код не портит:
    pre-commit не должен вставать из-за формулировки.
    """
    rows, defects, notes = [], [], []
    for f in sorted(topics_dir.glob("*.md")):
        if f.name in ("index.md", "log.md", "README.md"):
            continue
        rel = f.relative_to(bank_root).as_posix()
        where = f"{bank_root.name}/{rel}"
        # 🔑 сначала строгая проверка формы, потом толерантное чтение: генератор обязан
        # собраться и на кривом файле, но `--check` про кривизну знать обязан
        defects += [f"{where}: {p}" for p in validate_frontmatter(f)]
        fm = parse_frontmatter(f)
        typ = fm.get("type", "").strip()
        if not typ:
            defects.append(f"{where}: нет `type` во фронтматтере")
            continue
        if typ not in TYPE_TITLE:
            defects.append(
                f"{where}: `type: {typ}` вне набора {'/'.join(TYPE_ORDER)} — "
                "топик-солянка расщепляется, а не получает пятое значение")
        desc = fm.get("description", "").strip()
        if not desc:
            defects.append(f"{where}: нет `description`")
        elif len(desc) < DESC_MIN:
            notes.append(
                f"{where}: `description` короче {DESC_MIN} символов — это условие обращения "
                "(«когда сюда идти»), а не заголовок")
        rows.append((rel, typ, desc))
    return rows, defects, notes


def render(groups, link_prefix: str) -> str:
    """groups: [(заголовок группы|None, [(rel, type, desc)])] → markdown."""
    out = [GENERATED_NOTE, ""]
    for title, rows in groups:
        by_type = {}
        for rel, typ, desc in rows:
            by_type.setdefault(typ, []).append((rel, desc))
        if not by_type:
            continue
        if title:
            out.append(f"### {title}")
            out.append("")
        types = [t for t in TYPE_ORDER if t in by_type]
        types += sorted(t for t in by_type if t not in TYPE_ORDER)
        for typ in types:
            out.append(f"**{TYPE_TITLE.get(typ, typ)}**")
            out.append("")
            for rel, desc in sorted(by_type[typ]):
                name = rel.rsplit("/", 1)[-1].removesuffix(".md")
                out.append(f"- [{name}]({link_prefix}{rel}) — {desc}")
            out.append("")
    return "\n".join(out).rstrip() + "\n"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--memory", default=".agents/memory", help="каталог памяти проекта")
    ap.add_argument("--apply", action="store_true", help="записать изменения")
    ap.add_argument("--check", action="store_true", help="ненулевой код, если индекс устарел")
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
    banks = read_banks(mem)
    if not banks:
        print(f"в {mem} нет ни одного банка с topics/ — индексировать нечего")
        return

    all_groups, defects, notes, changed = [], [], [], []
    for name, root, topics in banks:
        rows, bad, warn = collect(topics, root)
        defects += bad
        notes += warn
        all_groups.append((f"Банк `{name}`", rows, root))

    # 1) сводный индекс в MEMORY.md — только если там ещё стоят маркеры.
    # По умолчанию индекс живёт в README банков: в MEMORY он весил 23 КБ и грузился
    # каждой сессией, хотя нужен точечно — когда выбираешь, какой топик открыть.
    journal = journal_root(mem)
    memory_md = journal / "MEMORY.md"
    if memory_md.is_file() and BEGIN in memory_md.read_text(encoding="utf-8"):
        groups = []
        for title, rows, root in all_groups:
            # ссылки даём относительно MEMORY.md: банки лежат где угодно от него
            prefix = os.path.relpath(root, journal).replace(os.sep, "/")
            prefix = "" if prefix == "." else prefix + "/"
            groups.append((title, [(prefix + rel, t, d) for rel, t, d in rows]))
        if splice(memory_md, BEGIN, END, render(groups, ""), args.apply):
            changed.append(memory_md)

    # 2) свой список в README каждого банка
    for title, rows, root in all_groups:
        readme = root / "README.md"
        if not readme.is_file() or BEGIN not in readme.read_text(encoding="utf-8"):
            continue
        if splice(readme, BEGIN, END, render([(None, rows)], ""), args.apply):
            changed.append(readme)

    total = sum(len(rows) for _, rows, _ in all_groups)
    print(f"топиков: {total} в {len(banks)} банках")
    for c in defects:
        print("  🔴", c)
    for c in notes:
        print("  ⚠️", c)
    for p in changed:
        print(("обновлён: " if args.apply else "изменится: ") + str(p))
    if not changed:
        print("индекс актуален")
    if args.check and (changed or defects):
        sys.exit(1)


if __name__ == "__main__":
    main()
