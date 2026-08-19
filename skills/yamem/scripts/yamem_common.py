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


# символы, с которых значение в YAML начинать нельзя без кавычек. `[` и `{` сюда НЕ входят:
# это валидные flow-коллекции (`keywords: [a, b]` — легальное расширение фронтматтера)
_YAML_SPECIAL_START = "*&!%@`>|"


def _first_line(block: str) -> str:
    """Первая строка сообщения YAML-парсера — для случая, когда эвристики промолчали."""
    import yaml
    try:
        yaml.safe_load(block)
    except Exception as e:
        return str(e).splitlines()[0]
    return "неизвестная причина"


def validate_frontmatter(path: Path) -> list:
    """Структурные дефекты фронтматтера — то, из-за чего значение молча теряется.

    🔑 Проверяем СТРОГО, а не так, как читаем. `parse_frontmatter` намеренно
    толерантен: вытаскивает всё, что похоже на `ключ: значение`, и едет дальше.
    Из-за этого невалидный YAML проходил генерацию зелёным, а сторонний инструмент
    на том же файле падал — прецедент 2026-08-20: `description` с двоеточием без
    кавычек в двух топиках, индекс собирался, `yaml.safe_load` падал.

    Судит настоящий парсер, если PyYAML в среде есть; жёсткой зависимости не
    заводим — скрипты обязаны работать на голом python, на машине коллеги пакета
    может не быть. Без PyYAML те же дефекты ловятся эвристиками (грубее, но ловятся).

    Возвращает список сообщений; пустой список — файл в порядке.
    """
    raw = path.read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        return ["BOM в начале файла — фронтматтер не распознаётся ни одним парсером"]
    # ⚠️ CRLF нормализуем сами: читаем байтами (ради BOM), а universal newlines тут
    # не работает — файл из Windows-редактора иначе выглядит как «нет фронтматтера»
    text = raw.decode("utf-8", "replace").replace("\r\n", "\n")
    if not text.startswith("---\n"):
        if text.lstrip().startswith("---\n"):
            return ["фронтматтер не в первой строке файла (перед ним пустые строки)"]
        return ["нет фронтматтера"]
    end = text.find("\n---\n", 4)
    if end == -1:
        return ["фронтматтер не закрыт строкой `---`"]
    block = text[4:end + 1]

    try:
        import yaml
    except ImportError:
        return _frontmatter_heuristics(block)

    try:
        data = yaml.safe_load(block)
    except Exception:
        # ⚠️ Текст ошибки YAML указывает на СЛЕДСТВИЕ («mapping values are not allowed»),
        # а править надо причину — её точнее называют эвристики, поэтому их и печатаем
        return _frontmatter_heuristics(block) or ["YAML не парсится: %s" % _first_line(block)]
    if data is None:
        return ["фронтматтер пуст"]
    if not isinstance(data, dict):
        return ["фронтматтер разобрался не в набор ключей — проверь пробел после `ключ:`"]
    return []


def _frontmatter_heuristics(block: str) -> list:
    """Фолбэк без PyYAML: те же дефекты, но распознанные грубо."""
    problems = []
    for n, line in enumerate(block.splitlines(), start=2):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line[0] in " \t-":            # продолжение блока или список — плоский разбор их не берёт
            continue
        m = re.match(r"^([A-Za-z_][\w-]*):(\s*)(.*)$", line)
        if not m:
            problems.append("строка %d: не `ключ: значение` — `%s`" % (n, line.strip()[:60]))
            continue
        key, gap, val = m.group(1), m.group(2), m.group(3).strip()
        if val and not gap:
            problems.append("строка %d: после `%s:` нужен пробел" % (n, key))
        quoted = len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'"
        if not val or quoted or val[0] in "[{":
            continue
        if ": " in val or val.endswith(":"):
            problems.append("`%s`: значение содержит двоеточие — взять в кавычки, "
                            "иначе YAML читает его как вложенный ключ" % key)
        elif val[0] in _YAML_SPECIAL_START:
            problems.append("`%s`: значение начинается с `%s` — взять в кавычки" % (key, val[0]))
        elif val[0] in "\"'" and not val.endswith(val[0]):
            problems.append("`%s`: значение открыто кавычкой и не закрыто" % key)
    return problems


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
    if items is None:
        # ⚠️ Старая раскладка (`local:` + `shared:`) не поддерживается, и промолчать
        # тут нельзя: без `banks:` скрипты просто не нашли бы топиков и бодро
        # отчитались бы «0 банков» вместо того, чтобы сказать, что конфиг устарел.
        if re.search(r"^(local|shared):\s*$", text, re.M):
            sys.exit(
                f"{cfg}: конфиг старого формата (`local:` / `shared:`).\n"
                "Раскладка памяти теперь плоская: журнал проекта в корне памяти,\n"
                "знание — в banks/. Переведи конфиг на секцию `banks:` и перенеси\n"
                "local/topics → banks/local/topics, shared/<банк> → banks/<банк>."
            )
        items = [{"name": "local", "path": "banks/local"}]

    return {"journal": mem, "banks": [
        (i.get("name", "?"), mem / i.get("path", ""),
         truthy(i.get("pull_on_start", "true"))) for i in items
    ]}


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
