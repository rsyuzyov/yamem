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


def read_config(mem: Path):
    """(путь local относительно mem, [(имя shared, путь)]) из yamem.config.yaml."""
    cfg = mem / "yamem.config.yaml"
    local_path, shared = "local", []
    if cfg.is_file():
        text = cfg.read_text(encoding="utf-8")
        m = re.search(r"^local:\s*$.*?^\s*path:\s*(\S+)", text, re.M | re.S)
        if m:
            local_path = m.group(1)
        block = re.search(r"^shared:\s*$(.*?)(?=^\S|\Z)", text, re.M | re.S)
        if block:
            for name, path in re.findall(
                r"-\s*name:\s*(\S+)\s*\n\s*path:\s*(\S+)", block.group(1)
            ):
                shared.append((name, path))
    return local_path, shared


def read_banks(mem: Path):
    """[(имя банка, каталог банка, путь к topics/)] — local первым."""
    local_path, shared = read_config(mem)
    banks = [("local", mem / local_path)]
    banks += [(name, mem / path) for name, path in shared]
    return [(n, d, d / "topics") for n, d in banks if (d / "topics").is_dir()]


def local_root(mem: Path) -> Path:
    """Каталог локальной памяти проекта."""
    local_path, _ = read_config(mem)
    return mem / local_path


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
