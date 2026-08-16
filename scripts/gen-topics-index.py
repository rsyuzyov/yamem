#!/usr/bin/env python3
"""Генерация индекса топиков из OKF-фронтматтера.

Собирает `type` и `description` из топиков всех банков (local + shared) и
подставляет готовый список в размеченные блоки:

    <!-- yamem:topics-index:begin -->   ...   <!-- yamem:topics-index:end -->

Куда пишет:
  * `<local>/MEMORY.md`      — раздел «База знаний», все банки;
  * README каждого банка     — только топики этого банка.

Блоки с маркерами должны уже существовать в файле — скрипт не угадывает, куда
вставлять, и не трогает ничего за их пределами. Запуск без `--apply` — сухой
прогон: показывает, что изменится, и ничего не пишет.

    python scripts/gen-topics-index.py [--memory <path>] [--apply] [--check]

`--check` завершается с ненулевым кодом, если индекс разошёлся с топиками
(для проверки перед коммитом).
"""
import argparse
import sys
from pathlib import Path

from yamem_common import parse_frontmatter, read_banks, splice

BEGIN = "<!-- yamem:topics-index:begin -->"
END = "<!-- yamem:topics-index:end -->"
GENERATED_NOTE = "<!-- сгенерировано scripts/gen-topics-index.py — руками не править -->"

# порядок разделов в индексе; `type` вне списка попадает в конец, своей группой
TYPE_ORDER = ["rule", "recipe", "reference", "incident"]
TYPE_TITLE = {
    "rule": "Правила и договорённости",
    "recipe": "Рецепты — как сделать",
    "reference": "Справка — состав, инвентарь, эталоны",
    "incident": "Разборы инцидентов — прецеденты",
}


def collect(topics_dir: Path, bank_root: Path):
    """Топики банка: (относительный путь, type, description). Без фронтматтера — в жалобы."""
    rows, complaints = [], []
    for f in sorted(topics_dir.glob("*.md")):
        if f.name in ("index.md", "log.md", "README.md"):
            continue
        fm = parse_frontmatter(f)
        rel = f.relative_to(bank_root).as_posix()
        if not fm.get("type"):
            complaints.append(f"{bank_root.name}/{rel}: нет `type` во фронтматтере")
            continue
        desc = fm.get("description", "").strip()
        if not desc:
            complaints.append(f"{bank_root.name}/{rel}: нет `description`")
        rows.append((rel, fm["type"], desc))
    return rows, complaints


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

    mem = Path(args.memory).resolve()
    if not mem.is_dir():
        sys.exit(f"нет каталога памяти: {mem}")
    banks = read_banks(mem)
    if not banks:
        print(f"в {mem} нет ни одного банка с topics/ — индексировать нечего")
        return

    all_groups, complaints, changed = [], [], []
    for name, root, topics in banks:
        rows, warn = collect(topics, root)
        complaints += warn
        title = "Память проекта" if name == "local" else f"Банк `{name}`"
        all_groups.append((title, rows, root))

    # 1) сводный индекс в MEMORY.md — пути относительно каталога local
    local_root = banks[0][1]
    memory_md = local_root / "MEMORY.md"
    groups = []
    for title, rows, root in all_groups:
        # ссылки даём относительно каталога local — банки лежат выше по дереву
        prefix = "" if root == local_root else f"../{root.relative_to(mem).as_posix()}/"
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
    for c in complaints:
        print("  ⚠️", c)
    for p in changed:
        print(("обновлён: " if args.apply else "изменится: ") + str(p))
    if not changed:
        print("индекс актуален")
    if args.check and (changed or complaints):
        sys.exit(1)


if __name__ == "__main__":
    main()
