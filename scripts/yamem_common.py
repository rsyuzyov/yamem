#!/usr/bin/env python3
"""Общее для генераторов представлений yamem.

Держим здесь ровно то, что нужно и `gen-topics-index.py`, и `gen-backlog.py`:
разбор плоского YAML-фронтматтера, поиск банков памяти и подстановка текста
между маркерами. Без зависимостей — скрипты должны запускаться на голом python.
"""
import re
import sys
from pathlib import Path


def parse_frontmatter(path: Path) -> dict:
    """Плоский YAML-фронтматтер файла. Без зависимостей: ключ: значение."""
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        return {}
    end = text.find("\n---\n", 4)
    if end == -1:
        return {}
    out = {}
    for line in text[4:end + 1].splitlines():
        m = re.match(r"^([A-Za-z_][\w-]*):\s*(.*)$", line)
        if not m:
            continue
        key, val = m.group(1), m.group(2).strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1].replace('\\"', '"').replace("\\\\", "\\")
        out[key] = val
    return out


def truthy(v) -> bool:
    return str(v).strip().lower() in ("true", "yes", "да", "1", "+")


def _list_items(text: str, key: str):
    """Элементы YAML-списка `key:` как плоские словари; None — секции нет.

    Хватает `- ключ: значение` и продолжающие строки того же элемента. Значение
    берётся до первого пробела, поэтому хвостовой комментарий в него не попадает.
    """
    block = re.search(rf"^{key}:\s*$(.*?)(?=^\S|\Z)", text, re.M | re.S)
    if not block:
        return None
    items, cur = [], None
    for line in block.group(1).splitlines():
        m = re.match(r"^\s*-\s*([A-Za-z_]\w*):\s*(\S+)", line)
        if m:
            cur = {m.group(1): m.group(2)}
            items.append(cur)
            continue
        m = re.match(r"^\s+([A-Za-z_]\w*):\s*(\S+)", line)
        if m and cur is not None:
            cur[m.group(1)] = m.group(2)
    return items


def read_config(mem: Path) -> dict:
    """{"journal": каталог журнала, "banks": [(имя, каталог, pull_on_start)]}.

    Плоская раскладка: журнал проекта (MEMORY.md, diary/, tasks/, представления)
    лежит в корне памяти, отчуждаемое знание — в банках из секции `banks:`.
    Банк со своими топиками зовётся `local` и ничем не выделен среди прочих.
    """
    cfg = mem / "yamem.config.yaml"
    text = cfg.read_text(encoding="utf-8") if cfg.is_file() else ""

    items = _list_items(text, "banks")
    if items is not None:
        return {"journal": mem, "banks": [
            (i.get("name", "?"), mem / i.get("path", ""),
             truthy(i.get("pull_on_start", "true"))) for i in items
        ]}

    # ⚠️ Ветка старой раскладки (`local:` + `shared:`) держится только ради окна
    # переезда на плоскую и снимается, когда памяти в старом виде не остаётся.
    m = re.search(r"^local:\s*$.*?^\s*path:\s*(\S+)", text, re.M | re.S)
    journal = mem / (m.group(1) if m else "local")
    banks = [("local", journal, True)]
    for i in _list_items(text, "shared") or []:
        banks.append((i.get("name", "?"), mem / i.get("path", ""),
                      truthy(i.get("pull_on_start", "true"))))
    return {"journal": journal, "banks": banks}


def read_banks(mem: Path):
    """[(имя банка, каталог банка, путь к topics/)] — только банки с топиками."""
    return [(n, d, d / "topics") for n, d, _ in read_config(mem)["banks"]
            if (d / "topics").is_dir()]


def journal_root(mem: Path) -> Path:
    """Каталог журнала проекта: MEMORY.md, diary/, tasks/, представления."""
    return read_config(mem)["journal"]


def splice(path: Path, begin: str, end: str, body: str, apply: bool,
           skeleton: str = "") -> bool:
    """Подставить body между маркерами. True, если содержимое изменилось.

    Если файла нет и передан skeleton — файл создаётся из скелета (в нём
    маркеры уже должны быть). Всё, что вне маркеров, не трогается никогда.
    """
    if not path.is_file():
        if not skeleton:
            sys.exit(f"{path}: файла нет и нечем его создать")
        if not apply:
            return True
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(skeleton, encoding="utf-8", newline="\n")
    text = path.read_text(encoding="utf-8")
    i, j = text.find(begin), text.find(end)
    if i == -1 or j == -1:
        sys.exit(f"{path}: нет маркеров {begin} / {end} — добавь их один раз вручную")
    if j < i:
        sys.exit(f"{path}: маркер end раньше begin")
    new = text[:i + len(begin)] + "\n\n" + body + "\n" + text[j:]
    if new == text:
        return False
    if apply:
        path.write_text(new, encoding="utf-8", newline="\n")
    return True


def md_cell(value: str) -> str:
    """Значение внутрь ячейки таблицы: вертикальная черта ломает разметку."""
    return (value or "").replace("|", "\\|").strip()
