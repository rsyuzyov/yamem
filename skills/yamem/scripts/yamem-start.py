#!/usr/bin/env python3
"""Стартер yamem: весь preflight одним вызовом.

Делает то, что раньше было списком из одиннадцати шагов и потому регулярно
выполнялось наполовину: синхронизирует банки, отмечает сессию на доске,
собирает дайджест задач, сводки дневников и коммиты памяти за сутки.

    python scripts/yamem-start.py --memory <path> --sid <sid> [--topic "..."]

Ключи:
    --days N        сколько дней дневника (по умолчанию из конфига)
    --diary РЕЖИМ   heads | red (по умолчанию) | marks | full — насколько подробно
    --no-sync       не трогать git вовсе: ни pull банков, ни коммит отметки
                    (режим для прогона на копии памяти)
    --prune         снести записи брошенных сессий (старше 6 часов)
    --part K        печатать часть дайджеста: 1 = ядро (синхронизация, соседи,
                    задачи, банки, коммиты), 2..N = куски сводок дневников.
                    Без ключа печатается всё одним выводом.
    --part-limit N  потолок части в БАЙТАХ utf-8 (по умолчанию 20000)

Печатает markdown в stdout. Задачи и дневники не пересказываются целиком:
источник правды — сами файлы, стартер лишь показывает, куда смотреть.

🔑 Зачем `--part`: полный дайджест длиннее лимита вывода инструмента (~30 КБ),
и харнес молча подменяет его превью — данные до модели не доезжают. Части
запрашиваются **параллельно одним блоком**: это один раунд латентности, но весь
объём в контексте. Побочные действия (pull, отметка на доске, prune) делает
только часть 1 — параллельные git-операции в одном репозитории дерутся за индекс.
"""
import argparse
import io
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path

from yamem_common import journal_root, parse_frontmatter, read_config, truthy

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


def attach(path: Path, out: list, name: str) -> None:
    """Банк-субмодуль клонируется на detached HEAD, и `pull` в нём не проходит.

    Привязываем к ветке по origin/HEAD — но только если текущий коммит её предок,
    иначе тут лежит несохранённая работа и трогать её нельзя.
    """
    code, _, _ = run(["git", "symbolic-ref", "-q", "HEAD"], cwd=path)
    if code == 0:
        return
    code, branch, _ = run(["git", "rev-parse", "--abbrev-ref", "origin/HEAD"], cwd=path)
    if code != 0 or "/" not in branch:
        return
    branch = branch.split("/", 1)[1]
    code, _, _ = run(["git", "merge-base", "--is-ancestor", "HEAD", f"origin/{branch}"], cwd=path)
    if code != 0:
        out.append(f"- ⚠️ {name}: detached HEAD с своими коммитами — разобрать руками")
        return
    if run(["git", "checkout", "-q", branch], cwd=path)[0] == 0:
        out.append(f"- {name}: подхвачен с detached HEAD на `{branch}`")


def within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (ValueError, OSError):
        return False


def sync(mem: Path, no_sync: bool, out: list):
    """git pull --rebase по репозиториям памяти. Ошибка не блокирует старт."""
    cfg = read_config(mem)
    journal = cfg["journal"]
    targets = [("память", journal)]
    for name, path, _pull in cfg["banks"]:
        # в старой раскладке банк со своими топиками — это сам журнал
        if path.resolve() != journal.resolve():
            targets.append((name, path))

    out.append("## Синхронизация")
    inside, pullable = [], []
    for name, path in targets:
        if not path.is_dir():
            out.append(f"- {name}: ⚠️ нет каталога `{path}`")
            continue
        if not is_git(path):
            # банк внутри репозитория памяти едет с ним и своего pull не требует
            if path.resolve() != journal.resolve() and within(path, journal):
                inside.append(name)
            else:
                out.append(f"- {name}: обычный каталог, git не настроен")
            continue
        if no_sync:
            out.append(f"- {name}: пропущено (`--no-sync`)")
            continue
        attach(path, out, name)  # локально и быстро, до параллельной части
        pullable.append((name, path))

    # 🔑 Тянем ВСЕ репозитории разом. Последовательные `git pull` — это чистая
    # сеть, и они складываются: три банка давали ~18 с из 18.5 с всего старта,
    # причём каждый возвращал «уже актуально». Параллельно — по самому долгому.
    if pullable:
        with ThreadPoolExecutor(max_workers=len(pullable)) as pool:
            futures = {name: pool.submit(run, ["git", "pull", "--rebase", "--autostash"], path)
                       for name, path in pullable}
            for name, _ in pullable:
                code, so, se = futures[name].result()
                if code == 0:
                    state = ("уже актуально" if "up to date" in so.lower()
                             else so.splitlines()[-1][:80])
                    out.append(f"- {name}: synced — {state}")
                else:
                    out.append(f"- {name}: ⚠️ NOT synced — {(se or so).splitlines()[0][:100]}")
    if inside:
        out.append(f"- {', '.join(inside)}: в составе репозитория памяти")
    out.append("")


def sessions(root: Path, sid: str, topic: str, prune: bool, no_sync: bool, out: list):
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

    # ⚠️ `--no-sync` глушит и запись отметки в git: копия памяти для проверок несёт
    # с собой `.git` с боевым remote, и прогон на ней иначе пушит мусор в боевой
    # репозиторий (было 2026-08-16).
    if is_git(root) and not no_sync:
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


def diary_entries(root: Path, days: int, mode: str) -> list:
    """Сводки дневников по одной записи на файл: [(имя, текст), ...].

    Собирается отдельно от печати: дайджест может не пройти лимит вывода
    инструмента целиком, и тогда записи раскладываются по частям (см. `--part`).
    """
    today = datetime.now().date()
    wanted = [(today - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(days)]
    files = []
    ddir = root / "diary"
    if ddir.is_dir():
        for day in wanted:
            month = day[:7]
            files += sorted((ddir / month).glob(f"{day}*.md")) if (ddir / month).is_dir() else []
            files += sorted(ddir.glob(f"{day}*.md"))  # старая плоская раскладка

    entries = []
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
        entries.append((f.name, f"### {f.name}\n{body or '_(без заголовков)_'}\n"))
    return entries


def size(text: str) -> int:
    """Вес куска так, как его считает харнес — в БАЙТАХ UTF-8.

    ⚠️🔴 Не в символах: кириллица даёт ×1.59 байта на символ, и часть, честно
    уложенная в 23 000 «символов», весит 36 КБ и не проходит лимит вывода
    инструмента. Ровно на этом первая версия `--part` и провалилась (18.08).
    """
    return len(text.encode("utf-8"))


def split_entries(entries: list, limit: int) -> list:
    """Разложить записи по кускам не тяжелее `limit` байт.

    Детерминированно: одни и те же записи всегда лягут одинаково, поэтому части
    можно запрашивать независимыми параллельными вызовами. Запись тяжелее лимита
    едет отдельным куском — резать её нельзя.

    Куски выравниваются по весу: сначала считаем, сколько частей вообще нужно,
    и набиваем до `общий вес / число частей`, а не до потолка. Иначе жадная
    набивка даёт перекос вроде 33 записей в одной части и 4 в следующей.
    """
    total = sum(size(t) for _, t in entries)
    if not entries:
        return []
    parts = max(1, -(-total // limit))  # ceil
    target = max(1, -(-total // parts))

    chunks, cur, cur_len = [], [], 0
    for name, text in entries:
        w = size(text)
        if cur and (cur_len + w > limit or cur_len >= target):
            chunks.append(cur)
            cur, cur_len = [], 0
        cur.append((name, text))
        cur_len += w
    if cur:
        chunks.append(cur)
    return chunks


def commits(root: Path, out: list):
    code, so, _ = run(["git", "log", "--since=24 hours ago", "--format=%h %ad %s",
                       "--date=format:%m-%d %H:%M"], cwd=root)
    out.append("## Коммиты памяти за сутки")
    out.append(so if code == 0 and so else "- нет")
    out.append("")


def banks(mem: Path, out: list):
    names, counts, per_bank = {}, [], []
    for name, path, _pull in read_config(mem)["banks"]:
        tdir = path / "topics"
        if not tdir.is_dir():
            continue
        files = sorted(f.stem for f in tdir.glob("*.md") if f.name != "README.md")
        counts.append(f"{name}: {len(files)}")
        per_bank.append((name, files))
        for f in files:
            names.setdefault(f, []).append(name)
    dup = sorted(n for n, banks_ in names.items() if len(banks_) > 1)
    out.append("## Банки топиков")
    out.append("- " + " · ".join(counts))
    for bank, files in per_bank:
        out.append(f"- **{bank}**: {', '.join(files)}")
    if dup:
        out.append(f"- ⚠️ одноимённые (читать **вместе**, это «универсальное + дельта»): "
                   f"{', '.join(dup)}")
    out.append("- когда по имени не ясно, идти ли в топик, — `description` каждого лежит "
               "в `README.md` его банка")
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
    ap.add_argument("--part", type=int, default=0,
                    help="1 = ядро дайджеста, 2..N = куски дневников, 0 = всё одним выводом")
    ap.add_argument("--part-limit", type=int, default=20000,
                    help="потолок части в БАЙТАХ utf-8 (лимит вывода инструмента ~30 КБ)")
    args = ap.parse_args()

    if hasattr(sys.stdout, "reconfigure"):  # эмодзи в выводе на Windows
        sys.stdout.reconfigure(encoding="utf-8")

    mem = Path(args.memory).resolve()
    if not mem.is_dir():
        sys.exit(f"нет каталога памяти: {mem}")
    root = journal_root(mem)
    if not root.is_dir():
        sys.exit(f"нет каталога журнала памяти: {root}")

    days = args.days
    if not days:
        cfg = mem / "yamem.config.yaml"
        days = 7
        if cfg.is_file():
            m = re.search(r"diary_read_days:\s*(\d+)", cfg.read_text(encoding="utf-8"))
            days = int(m.group(1)) if m else 7

    entries = diary_entries(root, days, args.diary)
    chunks = split_entries(entries, args.part_limit) if args.part else []
    total = 1 + len(chunks)  # ядро + куски дневников
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")

    # ⚠️ Побочные действия (pull банков, отметка на доске, prune) делает ТОЛЬКО часть 1:
    # части запрашиваются параллельно, и три одновременных git-операции в одном
    # репозитории дерутся за индекс. Остальные части — чистое чтение.
    if args.part >= 2:
        idx = args.part - 2
        out = [f"# yamem preflight — часть {args.part}/{total}: дневники", ""]
        if idx < len(chunks):
            names = [n for n, _ in chunks[idx]]
            out.append(f"## Дневники за {days} дн., записи {names[0]} … {names[-1]}"
                       f" ({len(names)} из {len(entries)}), режим `{args.diary}`")
            out.append("")
            out += [text for _, text in chunks[idx]]
            out.append("⚠️ Это сводка. Полный текст дня — читать сам файл в `diary/`.")
        else:
            out.append(f"_(пусто: всего частей {total})_")
        out.append(f"— конец части {args.part}/{total} —")
        print("\n".join(out))
        return

    out = [f"# yamem preflight — {stamp}"
           + (f" — часть 1/{total}" if args.part else ""), ""]
    sync(mem, args.no_sync, out)
    sessions(root, args.sid, args.topic, args.prune, args.no_sync, out)
    tasks(root, out)
    banks(mem, out)
    commits(root, out)
    if args.part:
        out.append(f"## Дневники за {days} дн. — {len(entries)} файлов, режим `{args.diary}`")
        if total > 1:
            rest = ", ".join(f"`--part {i}`" for i in range(2, total + 1))
            out.append(f"- сводки приходят частями: {rest} — вызывать **одним блоком**, "
                       f"это один раунд")
            if args.part == 1 and total > 3:
                out.append(f"- ⚠️ частей больше трёх ({total}): дневники разрослись, "
                           f"стоит уменьшить `diary_read_days` или почистить")
        else:
            out.append("- записей нет")
        out.append("")
    else:
        out.append(f"## Дневники за {days} дн. — {len(entries)} файлов, режим `{args.diary}`")
        out.append("")
        out += [text for _, text in entries] or ["- записей нет", ""]
        out.append("⚠️ Это сводка. Полный текст дня — читать сам файл в `diary/`.")
        out.append("")
    maintenance(root, mem, out)
    if args.part:
        # ядро не режется на части: если оно само переросло лимит — сказать об этом
        # вслух, иначе харнес молча подменит вывод превью (как было с частью 2 18.08)
        core = size("\n".join(out))
        if core > args.part_limit:
            out.append(f"⚠️ ядро дайджеста {core} Б > лимита {args.part_limit} Б — "
                       f"вывод могло обрезать; сократить задачи в работе или коммиты за сутки")
        out.append(f"— конец части 1/{total} —")
    print("\n".join(out))


if __name__ == "__main__":
    main()
