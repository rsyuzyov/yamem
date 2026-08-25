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
    --prune         снести записи неактивных сессий сразу (порог 6 ч вместо 26);
                    брошенное старше 26 ч часть 1 снимает и без этого ключа
    --part K        печатать часть дайджеста: 1 = ядро (синхронизация, активные
                    соседи строкой, горящее по задачам, раскладка частей),
                    2..N = обстановка, полный список задач, MEMORY.md, дневники.
                    Без ключа печатается всё одним выводом.
                    🔑 Ядро держим маленьким: это единственная часть, которую
                    нельзя нарезать. Списки в нём не живут — они пухнут.
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
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path

from yamem_common import journal_root, parse_frontmatter, read_config, truthy

MONTH_RE = re.compile(r"^\d{4}-\d{2}$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
SESSION_STALE_HOURS = 6      # позже этого сессия не считается активной
# 🔑 Два порога вместо одного. Отметка пишется при старте и смене темы, поэтому
# «6 часов молчания» ещё не значит «сессия ушла»: сессия, работающая восьмой час,
# при одном пороге молча исчезла бы с доски у соседей. Снимаем только с 26 ч —
# и это заведомо больше окна показа (24 ч), чтобы снятая запись не выпадала
# из списка «какие темы сегодня уже брали».
SESSION_DROP_HOURS = 26      # позже этого запись снимается с доски автоматически
SESSION_SHOW_HOURS = 24      # окно списка тем за сутки
SESSION_SHOW_MAX = 15
# связь «коммит памяти → сессия»: дневник называется diary/<месяц>/<дата>.<sid>.md
DIARY_SID = re.compile(r"diary/[^/]+/\d{4}-\d{2}-\d{2}\.([0-9a-f]{6,12})\.md$")
# сколько байт харнес пропускает в выводе инструмента, дальше подменяет превью;
# `--part-limit` держим ниже с запасом, а по этому порогу только предупреждаем
HARNESS_LIMIT = 28000
# ⏱ время меряет сам стартер: из сессии длительность вызова не видна вовсе,
# и каждый замер иначе приходится делать руками секундомером
TIMING = {"start": time.monotonic(), "pull": 0.0, "mark": 0.0}
RED = "🔴"
MARKS = ("🔴", "⭐", "⚠️", "🎯", "🔑", "✅")


def run(cmd, cwd=None, timeout=120):
    try:
        p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                           timeout=timeout, encoding="utf-8", errors="replace")
        return p.returncode, (p.stdout or "").strip(), (p.stderr or "").strip()
    except Exception as exc:  # noqa: BLE001 — стартер не должен падать из-за git
        return 1, "", str(exc)


def git_root(path: Path):
    """Корень work-tree, которому принадлежит каталог, либо `None`.

    ⚠️ Наличие `.git` рядом — не тот вопрос. У памяти-субмодуля `.git` это
    файл-gitfile, а у памяти-каталога ВНУТРИ репозитория проекта своего `.git`
    нет вовсе — хотя git ею управляет ничуть не меньше. Второй случай прежняя
    проверка считала «git не настроен»: отметка на доске не коммитилась, брошенные
    записи не снимались никогда и копились месяцами (замер на живой установке —
    48 записей за шесть дней при показанных «за сутки»). Спрашиваем сам git.
    """
    code, so, _ = run(["git", "rev-parse", "--show-toplevel"], cwd=path)
    if code != 0 or not so:
        return None
    return Path(so.strip())


def owns_repo(path: Path) -> bool:
    """Каталог САМ является корнем своего репозитория.

    🔑 Это граница для операций, которые задевают репозиторий ЦЕЛИКОМ — `pull`
    и `push`. Память-каталог внутри репозитория проекта остаётся пассажиром:
    старт сессии не вправе тянуть и пушить чужую ветку с чужими коммитами,
    защитами и CI. Запись своих файлов это не ограничивает — `add` и `commit`
    идут с pathspec и чужого не задевают.
    """
    root = git_root(path)
    return root is not None and root.resolve() == path.resolve()


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


def self_update_check(no_sync: bool, out: list):
    """Отстал ли САМ навык от своего origin. Проверяем, но не обновляем.

    🔑 Пак подключён git-субмодулем и junction'ами, а не маркетплейсом ⟹
    `claude plugin update` к нему неприменим, обновление — обычный `git pull`.
    Дату последней проверки хранить негде и не нужно: стартер знает свой путь
    (`__file__`), а `fetch` идёт в том же параллельном блоке, что и банки.

    ⚠️ Только сообщаем. Тянуть правки навыка ПОСРЕДИ сессии нельзя: SKILL.md уже
    прочитан, а скрипты сменятся под ногами — часть дайджеста соберётся старым
    кодом, часть новым. Обновление — отдельным решением, между сессиями.
    """
    if no_sync:
        return
    repo = Path(__file__).resolve().parent
    while repo != repo.parent and not (repo / ".git").exists():
        repo = repo.parent
    if not (repo / ".git").exists():
        return
    if run(["git", "fetch", "-q", "--no-tags"], cwd=repo)[0] != 0:
        return
    code, branch, _ = run(["git", "rev-parse", "--abbrev-ref", "origin/HEAD"], cwd=repo)
    if code != 0 or "/" not in branch:
        return
    code, counts, _ = run(["git", "rev-list", "--left-right", "--count",
                           f"HEAD...{branch}"], cwd=repo)
    if code != 0 or "\t" not in counts:
        return
    ahead, behind = (int(x) for x in counts.split("\t")[:2])
    if behind:
        code, subjects, _ = run(["git", "log", "--oneline", "-3", f"HEAD..{branch}"], cwd=repo)
        out.append(f"- ⚠️ **навык `{repo.name}` отстал на {behind} коммит(ов)** от `{branch}`"
                   + (f" (и {ahead} своих не запушено)" if ahead else ""))
        for line in subjects.splitlines()[:3]:
            out.append(f"  ↳ {line[:100]}")
        out.append(f"  обновить между сессиями: `git -C {repo} pull --rebase`")
    elif ahead:
        out.append(f"- ⚠️ навык `{repo.name}`: {ahead} коммит(ов) не запушено")


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
        if not owns_repo(path):
            # банк внутри репозитория памяти едет с ним и своего pull не требует
            if path.resolve() != journal.resolve() and within(path, journal):
                inside.append(name)
            elif (owner := git_root(path)) is not None:
                # ⚠️ Каталогом владеет ЧУЖОЙ репозиторий (обычно сам проект).
                # Коммитить свои файлы там можно — они идут с pathspec, — а `pull`
                # нельзя: он тянет чужую ветку целиком. Едем пассажиром, но вслух.
                out.append(f"- {name}: в составе репозитория `{owner.name}` — "
                           f"pull не наш, коммитим только свои файлы")
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
    # проверка своей версии едет тем же параллельным блоком — раунда не добавляет
    self_lines = []
    with ThreadPoolExecutor(max_workers=1) as selfpool:
        self_future = selfpool.submit(self_update_check, no_sync, self_lines)
        if pullable:
            t0 = time.monotonic()
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
                        out.append(f"- {name}: ⚠️ NOT synced — "
                                   f"{(se or so).splitlines()[0][:100]}")
            TIMING["pull"] = time.monotonic() - t0
        self_future.result()
    out += self_lines
    if inside:
        out.append(f"- {', '.join(inside)}: в составе репозитория памяти")
    out.append("")


def recent_commits(root: Path, hours: int = 24) -> list:
    """Коммиты памяти за окно, с привязкой к сессии по файлу дневника.

    🔑 Связь «коммит → сессия» в памяти уже есть: дневник называется
    `diary/<месяц>/<дата>.<sid>.md`. Поэтому «что сосед успел сделать» не требует
    ни нового поля в отметке, ни дисциплины его заполнять — только группировки
    того, что и так печаталось плоским списком (40 % ядра дайджеста на 17.08).
    """
    code, so, _ = run(["git", "log", f"--since={hours} hours ago",
                       "--format=%x1e%h%x1f%aI%x1f%s", "--name-only"], cwd=root)
    items = []
    if code != 0 or not so:
        return items
    for rec in so.split("\x1e"):
        rec = rec.strip("\n")
        if not rec:
            continue
        head, _, rest = rec.partition("\n")
        parts = head.split("\x1f")
        if len(parts) < 3:
            continue
        when = None
        try:
            when = datetime.fromisoformat(parts[1]).replace(tzinfo=None)
        except ValueError:
            pass
        files = [f for f in rest.split("\n") if f.strip()]
        sids = {m.group(1) for f in files for m in [DIARY_SID.search(f)] if m}
        items.append({"h": parts[0], "when": when, "subject": parts[2],
                      "files": files, "sids": sids,
                      "board": parts[2].startswith("sessions:")})
    return items


def ago(now, when) -> str:
    mins = max(0, int((now - when).total_seconds() // 60))
    if mins < 90:
        return f"{mins} мин"
    if mins < 24 * 60:
        return f"{mins / 60:.1f} ч"
    return f"{mins / 1440:.1f} дн"


def sessions(root: Path, sid: str, topic: str, prune: bool, no_sync: bool,
             out: list, log: list, mark: bool = True, quiet: bool = False) -> set:
    """Отметиться на доске, показать соседей С РЕЗУЛЬТАТОМ и снять брошенное.

    Возвращает sid'ы, чьи коммиты уже показаны здесь, — чтобы блок коммитов
    не печатал их второй раз.
    """
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
    if mark:
        mine.write_text(
            f"started: {started}\nupdated: {stamp}\ntopic: {topic or '—'}\nhosts: —\n",
            encoding="utf-8", newline="\n")

    notes = []  # служебные строки печатаются в конце блока, а не между блоками
    # результат сессии = её коммиты в память; служебные отметки в счёт не идут
    by_sid = {}
    for c in log:
        if c["board"]:
            continue
        for s in c["sids"]:
            by_sid.setdefault(s, []).append(c)

    rows, removed = [], []
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
        acts = sorted(by_sid.get(f.stem, []), key=lambda c: c["when"] or now)
        # 🔑 Живость — по ФАКТУ работы, а не только по полю `updated`: коммит
        # с дневником сессии моложе её отметки, а дисциплины он не требует.
        seen = [d for d in [upd] + [c["when"] for c in acts] if d]
        last = max(seen) if seen else datetime.fromtimestamp(f.stat().st_mtime)
        rows.append({"sid": f.stem, "topic": (t.group(1).strip() if t else "—"),
                     "upd": upd or last, "last": last, "acts": acts, "file": f})

    # sid, у которого работа в памяти есть, а запись уже снята (руками или нами
    # вчера): результат показать надо, иначе он молча исчезнет из дайджеста
    on_board = {r["sid"] for r in rows} | {sid}
    for s, acts in sorted(by_sid.items()):
        if s in on_board:
            continue
        acts = sorted(acts, key=lambda c: c["when"] or now)
        last = max([c["when"] for c in acts if c["when"]], default=now)
        rows.append({"sid": s, "topic": "— (отметка снята)", "upd": last,
                     "last": last, "acts": acts, "file": None})

    rows.sort(key=lambda r: r["last"], reverse=True)
    drop_after = timedelta(hours=SESSION_STALE_HOURS if prune else SESSION_DROP_HOURS)
    for r in rows:
        # ⚠️ Свежий коммит НЕ делает сессию активной, если её запись уже снята:
        # снятая отметка — это «сессия закрыта», и звать её соседом нельзя.
        r["live"] = bool(r["file"]) and now - r["last"] < timedelta(hours=SESSION_STALE_HOURS)
        if r["file"] and not r["live"] and now - r["last"] >= drop_after:
            removed.append(r["sid"])

    # ⚠️ `--no-sync` глушит и запись отметки в git: копия памяти для проверок несёт
    # с собой `.git` с боевым remote, и прогон на ней иначе пушит мусор в боевой
    # репозиторий (было 2026-08-16). По той же причине на копии ничего не сносим.
    if mark and git_root(root) is not None and not no_sync:
        t0 = time.monotonic()
        paths = [f".sessions/{sid}.md"]
        for name in removed:
            (board / f"{name}.md").unlink(missing_ok=True)
            paths.append(f".sessions/{name}.md")
        run(["git", "add", "--"] + paths, cwd=root)
        code, _, _ = run(["git", "diff", "--cached", "--quiet", "--"] + paths, cwd=root)
        if code == 1:  # есть что коммитить
            msg = f"sessions: {sid} в работе"
            if removed:
                msg += f"; снято брошенных: {len(removed)}"
            run(["git", "commit", "-q", "-m", msg, "--"] + paths, cwd=root)
            # ⏱ push отметки — 5 с сетевого ожидания, которых старт не должен ждать:
            # соседи читают доску не в эту же секунду, а нам она уже записана локально.
            # Отпускаем в фон; провалившийся push всплывёт при следующем pull.
            # ⚠️🔑 Но пушим ТОЛЬКО свой репозиторий. Если памятью владеет репозиторий
            # проекта, `push` отправил бы чужую ветку целиком — вместе с чужими
            # коммитами, защитами и CI, и делал бы это на каждом старте сессии.
            if owns_repo(root):
                try:
                    subprocess.Popen(["git", "push", "-q"], cwd=root,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    notes.append("- отметка закоммичена, push ушёл в фоне")
                except Exception as exc:  # noqa: BLE001 — старт не должен падать из-за git
                    notes.append(f"- ⚠️ отметка закоммичена, но не запушена: {exc}")
            else:
                notes.append("- отметка закоммичена; push не наш — памятью владеет "
                             "репозиторий проекта")
            TIMING["mark"] = time.monotonic() - t0
    elif mark and removed:  # не коммитим — файлы не трогаем, но молчать об этом нельзя
        # ⚠️ Причину называем настоящую. Раньше здесь всегда стояло «(`--no-sync`)»,
        # и установка, где флага никто не передавал, читала про него в каждом старте.
        why = "`--no-sync`" if no_sync else "git не управляет каталогом памяти"
        notes.append(f"- к снятию брошенных: {len(removed)} — не сняты, {why}")
        removed = []

    live = [r for r in rows if r["live"]]
    past = [r for r in rows
            if not r["live"] and now - r["last"] < timedelta(hours=SESSION_SHOW_HOURS)]
    if quiet:
        # ⚠️ Активных соседей называем прямо в ядре, одной строкой: это единственное
        # из блока, что меняет решение «брать ли задачу», а не описывает прошедший день.
        names = ", ".join(f"`{r['sid']}` ({r['topic'][:40]})" for r in live) or "нет"
        out.append(f"## Сессии: {len(rows)} за сутки · активны {len(live)}")
        out.append(f"- сейчас в работе: {names}")
        if live:
            out.append("⚠️ Не бери задачи, которые уже ведёт соседняя сессия.")
        out += notes
        out.append(f"- своя отметка записана: `{sid}`")
        out.append("")
        return {r["sid"] for r in rows} | {sid}

    out.append(f"## Сессии за сутки ({len(rows)} · активны {len(live)})")
    for r in live:
        out.append(f"- 🔵 **{r['sid']}** · {ago(now, r['last'])} · {r['topic'][:120]}")
        for c in reversed(r["acts"][-2:]):
            out.append(f"  ↳ {c['subject'][:100]}")
        if not r["acts"]:
            out.append("  ↳ ⚠️ в памяти пока ничего не оставила")
    if live:
        out.append("⚠️ Не бери задачи, которые уже ведёт соседняя сессия.")
    else:
        out.append("- активных нет")
    for r in past[:SESSION_SHOW_MAX]:
        badge = "⚫" if r["file"] is None or r["sid"] in removed else "⚪"
        tail = (f" ↳ {r['acts'][-1]['subject'][:70]}" if r["acts"]
                else " ⚠️ **без следа**")
        # ⚠️ Возраст, а не время суток. Строка попадает сюда и сортируется по
        # ПОСЛЕДНЕЙ АКТИВНОСТИ (`last`), а печаталось время отметки (`upd`) — и без
        # даты. 19.08 так соврали все семь строк разом: отметки были вчерашние и
        # позавчерашние, пять из них показывали время ПОЗЖЕ текущего (20:50 в 12:46),
        # а подпись под списком уверяла, что тему брали сегодня. Один счёт с 🔵.
        out.append(f"- {badge} {r['sid']} · {ago(now, r['last'])} · "
                   f"{r['topic'][:70]} —{tail}")
    if len(past) > SESSION_SHOW_MAX:
        out.append(f"- …ещё {len(past) - SESSION_SHOW_MAX} сессий за сутки")
    if past:
        out.append("🔁 Темы выше УЖЕ брали за последние сутки; «без следа» = следа "
                   "в памяти нет, а не «не сделано».")
    if mark and removed:
        out.append(f"- снято брошенных записей (старше "
                   f"{int(drop_after.total_seconds() // 3600)} ч): {len(removed)}")
    out += notes
    if mark:
        out.append(f"- своя отметка записана: `{sid}`")
    out.append("")
    return {r["sid"] for r in rows} | {sid}


# 🎯 «Горит» определяется НЕ статусом, а сроком, и срок лежит в теле задачи:
# `urgent: false` спокойно стоит у задачи с истёкшей лицензией. Дайджест поэтому
# вытаскивает даты из тел — иначе агент идёт грепать их сам, а это лишние раунды.
# ⚠️ Маркеры узкие намеренно. Широкое «срок» ловило даты ПОСТАНОВКИ задач
# («вопрос оператора 09.08», «политика от 2026-08-02») и печатало их как
# ПРОСРОЧЕННЫЕ — ложная тревога дороже пропуска: она заставляет идти проверять.
DEADLINE_HINT = re.compile(
    r"(?i)(истека\w*|истёк\w*|истек\w*|не позднее|дедлайн|deadline|expires?|"
    r"срок до|годн\w+ до|действует до|продлить до|перевыпуст\w+ до)")
# насколько близко к маркеру должна стоять дата, чтобы считаться сроком
DEADLINE_NEAR = 60
ISO_DATE = re.compile(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b")
DMY_DATE = re.compile(r"\b(\d{1,2})[.\-](\d{1,2})(?:[.\-](\d{2,4}))?\b")


def dates_in(line: str, today):
    """[(дата, позиция в строке)]. Без года — ближайшая трактовка: год текущий,
    а если это уводит больше чем на месяц в прошлое, значит речь о следующем."""
    found = []
    for m in ISO_DATE.finditer(line):
        try:
            found.append((datetime(int(m.group(1)), int(m.group(2)),
                                   int(m.group(3))).date(), m.start()))
        except ValueError:
            pass
    for m in DMY_DATE.finditer(line):
        day, month, year = m.group(1), m.group(2), m.group(3)
        try:
            if year:
                y = int(year) + (2000 if len(year) == 2 else 0)
                found.append((datetime(y, int(month), int(day)).date(), m.start()))
            else:
                d = datetime(today.year, int(month), int(day)).date()
                if (today - d).days > 31:
                    d = datetime(today.year + 1, int(month), int(day)).date()
                found.append((d, m.start()))
        except ValueError:
            pass
    return found


def deadline_of(path: Path, today, title: str = ""):
    """Ближайший срок задачи: (дата, контекст) либо None.

    Ищем в заголовке и в теле. Дата считается сроком, только если стоит рядом
    с маркером срока (`DEADLINE_NEAR` символов) — иначе это дата постановки,
    прецедента или решения, и её нельзя показывать как дедлайн.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    lines = [title] + text.split("\n---\n", 1)[-1].split("\n")
    best = None
    for line in lines:
        s = line.strip()
        if not s:
            continue
        hints = [(m.start(), m.end()) for m in DEADLINE_HINT.finditer(s)]
        if not hints:
            continue
        for d, pos in dates_in(s, today):
            if not any(min(abs(pos - h_end), abs(pos - h_start)) <= DEADLINE_NEAR
                       for h_start, h_end in hints):
                continue
            if best is None or d < best[0]:
                # контекст — окно вокруг самой даты, а не начало строки
                lo, hi = max(0, pos - 70), min(len(s), pos + 70)
                best = (d, ("…" if lo else "") + s[lo:hi] + ("…" if hi < len(s) else ""))
    return best


def deadlines_block(rows: list, root: Path, out: list, horizon: int = 45, limit: int = 12):
    """Задачи со сроком: просроченные и ближайшие. Строкой, с датой и контекстом."""
    today = datetime.now().date()
    found = []
    for rel, _fm, title in rows:
        d = deadline_of(root / "tasks" / rel / "task.md", today, title)
        if d and (d[0] - today).days <= horizon:
            found.append((d[0], rel, title, d[1]))
    if not found:
        return
    found.sort()
    out.append(f"**⏰ Со сроком в теле задачи** ({len(found)})")
    for date, rel, title, ctx in found[:limit]:
        left = (date - today).days
        mark = "🔴 ПРОСРОЧЕН" if left < 0 else ("🟠" if left <= 7 else "🟡")
        when = f"{-left} дн. назад" if left < 0 else (f"через {left} дн." if left else "сегодня")
        out.append(f"- {mark} {date:%d.%m} ({when}) — {title} — `tasks/{rel}/`")
        # контекст не печатаем, если срок и так виден в заголовке
        plain = ctx.strip("…").strip()
        if plain[:60] not in title:
            out.append(f"  ↳ {plain[:150]}")
    if len(found) > limit:
        out.append(f"- … ещё {len(found) - limit} со сроком в пределах {horizon} дн.")
    out.append("")


PROMPT_RE = re.compile(r"(?i)prompt")


def prompt_of(tasks_dir: Path, rel: str):
    """Дата правки файла-промпта задачи либо None — точка входа в длинную работу.

    Канон — `prompt-next-session.md`; прежние имена (`next-session-prompt.md`,
    `prompt-<дата>.md`) распознаём тоже, иначе уже написанные промпты пропадут из вида.
    """
    folder = tasks_dir / rel
    if not folder.is_dir():
        return None
    canon = folder / "prompt-next-session.md"
    found = canon if canon.is_file() else next(
        (p for p in sorted(folder.glob("*.md")) if PROMPT_RE.search(p.name)), None)
    if not found:
        return None
    return datetime.fromtimestamp(found.stat().st_mtime).strftime("%d.%m")


def epic_index(rows):
    """{rel темы: [(rel, поля) подзадач]}. Тема — та задача, на которую сослались."""
    by_slug = {}
    for rel, _fm in rows:
        by_slug.setdefault(rel.split("/")[-1], []).append(rel)
    index = {}
    for rel, fm in rows:
        ref = (fm.get("epic") or "").strip().strip("/").split("/")[-1]
        if not ref:
            continue
        targets = by_slug.get(ref, [])
        if len(targets) == 1 and targets[0] != rel:
            index.setdefault(targets[0], []).append((rel, fm))
    return index


def epics_block(rows, index, tasks_dir: Path, out: list):
    """Темы — одной строкой каждая: счётчики подзадач и признак промпта.

    🎯 Это и ответ на «продолжаем X», и то, что снимает разбухание дайджеста:
    22 строки одной темы сворачиваются в одну, а подзадачи остаются доступны
    в полном списке задач (он едет отдельными частями и не усекается).
    """
    fm_of = dict(rows)
    live = {rel: kids for rel, kids in index.items()
            if fm_of.get(rel, {}).get("status") != "done"
            or any(f.get("status") != "done" for _, f in kids)}
    if not live:
        return
    out.append(f"**☂ Темы** ({len(live)})")
    for rel in sorted(live, key=lambda r: fm_of.get(r, {}).get("updated", ""), reverse=True):
        kids, fm = live[rel], fm_of.get(rel, {})
        n = lambda s: sum(1 for _, f in kids if f.get("status") == s)  # noqa: E731
        parts = [f"{n('in-progress')} в работе", f"{n('waiting')} ждут", f"{n('new')} новых"]
        counts = ", ".join(p for p in parts if not p.startswith("0 "))
        pr = prompt_of(tasks_dir, rel)
        tail = f" · 📝 промпт {pr}" if pr else " · ⚠️ промпта нет"
        out.append(f"- {fm.get('title', rel)} — `tasks/{rel}/` · "
                   f"{len(kids)} подзадач: {counts or 'все закрыты'}{tail}")
    out.append("↳ «Продолжаем <тема>» — читать промпт темы, а не выбирать подзадачу по заголовку.")
    out.append("")


def read_tasks(root: Path):
    """[(rel, поля)] по всем задачам памяти. Тела не читаем."""
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
    return rows


def task_line(rel, fm) -> str:
    area = fm.get("area", "")
    tail = f" · {area}" if area else ""
    return f"- {fm.get('title', rel)} — `tasks/{rel}/` · {fm.get('updated', '—')}{tail}"


def task_entries(root: Path) -> list:
    """Полный список открытых задач кусками для частей: [(имя секции, текст)].

    🔑 Заводится ради того, чтобы список НЕ усекался. Раньше ядро подбирало,
    сколько задач «в работе» влезает в потолок вывода, и остаток пропадал —
    задача из хвоста была невидима сессии, пока та не откроет `backlog.md` руками.
    """
    rows = read_tasks(root)
    openable = [r for r in rows if r[1].get("status") != "done"]
    index = epic_index(rows)
    of_epic = {kid: rel.split("/")[-1] for rel, kids in index.items() for kid, _ in kids}
    by = lambda s: [r for r in openable if r[1].get("status") == s]  # noqa: E731
    rest = [r for r in openable if r[1].get("status") not in ("in-progress", "waiting")]
    quad = lambda u, i: [r for r in rest if truthy(r[1].get("urgent")) == u  # noqa: E731
                         and truthy(r[1].get("important")) == i]

    entries = []
    for title, section in (("🔧 В работе", by("in-progress")),
                           ("⏸ Ожидает внешнего действия", by("waiting")),
                           ("🔥 Срочно и важно", quad(True, True)),
                           ("⭐ Важно, не срочно", quad(False, True)),
                           ("⚡ Срочно, не важно", quad(True, False)),
                           ("💤 Идеи — ни срочно, ни важно", quad(False, False))):
        if not section:
            continue
        section = sorted(section, key=lambda r: r[1].get("updated", ""), reverse=True)
        # Секцию режем на куски по 40 строк: одна «Важно, не срочно» на 158 задач
        # иначе едет неделимой записью и сама перерастает потолок части.
        for i in range(0, len(section), 40):
            piece = section[i:i + 40]
            head = f"### {title} ({len(section)})" + (
                f" — {i + 1}–{i + len(piece)}" if len(section) > 40 else "")
            body = [head]
            for rel, fm in piece:
                line = task_line(rel, fm)
                if rel in of_epic:
                    line += f" · ☂ `{of_epic[rel]}`"
                body.append(line)
            entries.append((title, "\n".join(body) + "\n"))
    return entries


def tasks(root: Path, out: list, hot_limit=None, part_hint=""):
    """Только то, что горит: счётчики, сроки, темы, срочное+важное.

    🔑 Списки задач в ядре НЕ печатаем — они едут частями целиком. Ядро обязано
    оставаться маленьким: это единственная часть, которую нельзя нарезать, и она
    же растёт сама по себе (сессии за сутки, банки, коммиты). Прежде здесь жили
    «в работе» и «ожидает» — 30 строк, ~8 КБ из 28 КБ потолка, и ядро подходило
    к обрезанию вплотную.

    ⚠️ Сокращение здесь больше не равно потере: `part_hint` называет части,
    в которых лежит полный список.
    """
    rows = read_tasks(root)

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

    # 🎯 «В работе 57» — цифра, в которую не верит и сам оператор: столько задач
    # одновременно не ведут. Значит статус протух, а не работа идёт. Показываем
    # это счётчиком, иначе протухшее неотличимо от живого и занимает место
    # в каждом дайджесте до следующей оптимизации памяти.
    today = datetime.now().date()

    def stale(section, days):
        n = 0
        for _rel, fm in section:
            u = fm.get("updated", "")
            if DATE_RE.match(u) and (today - datetime.strptime(u, "%Y-%m-%d").date()).days > days:
                n += 1
        return n

    st7, st14 = stale(working, 7), stale(working, 14)
    if st7:
        out.append(f"⚠️ из «в работе» без движения: **{st7}** больше недели, **{st14}** больше "
                   f"двух ⟹ статус протух, а не работа идёт. Живых за 3 дня: "
                   f"{len(working) - stale(working, 3)}.")
    out.append("")

    def block(title, section, limit=None):
        if not section:
            return
        section = sorted(section, key=lambda r: r[1].get("updated", ""), reverse=True)
        shown = section if limit is None else section[:limit]
        out.append(f"**{title}** ({len(section)})")
        for rel, fm in shown:
            out.append(task_line(rel, fm))
        if limit is not None and len(section) > limit:
            # ⚠️ Не «смотри backlog.md»: остаток едет частями, читать его отдельным
            # файлом = лишний раунд. Куда именно — говорит part_hint.
            out.append(f"- … ещё {len(section) - limit} — полный список {part_hint}"
                       if part_hint else
                       f"- … ещё {len(section) - limit}, весь список — `backlog.md`")
        out.append("")

    block("🔥 Срочно и важно", hot, limit=hot_limit)
    deadlines_block([(rel, fm, fm.get("title", rel)) for rel, fm in openable], root, out)
    epics_block(rows, epic_index(rows), root / "tasks", out)
    if part_hint:
        out.append(f"📋 Списки задач — «в работе» ({len(working)}), «ожидает» ({len(waiting)}), "
                   f"«важно» ({len(important)}), «идеи» ({len(ideas)}) — целиком {part_hint}.")
        out.append("")
    else:
        block("⏸ Ожидает внешнего действия", waiting)
        block("🔧 В работе", working)
        block("⚡ Срочно, не важно", urgent_only, limit=5)


def memory_entries(root: Path) -> list:
    """`MEMORY.md` кусками по смысловым блокам: [(раздел, текст), ...].

    🔑 Файл читается КАЖДОЙ сессией целиком — это не «куда посмотреть», а часть
    preflight. Раньше стартер его намеренно не отдавал, и модель читала сама:
    90 КБ ≈ 33К токенов при потолке `Read` в 25К ⟹ три вызова подряд, три раунда
    латентности (~35 с) на ровном месте. Теперь он едет теми же частями.

    Границы блоков — строки верхнего уровня (`#`, `- `, `> `), чтобы кусок не
    рвался посреди факта, а продолжения с отступом ехали со своим пунктом.
    """
    f = root / "MEMORY.md"
    if not f.is_file():
        return []
    blocks, cur, section = [], [], "начало"
    for line in f.read_text(encoding="utf-8", errors="replace").split("\n"):
        if cur and (line.startswith("#") or line.startswith("- ") or line.startswith("> ")):
            blocks.append((section, "\n".join(cur).rstrip() + "\n"))
            cur = []
        if line.startswith("#"):
            section = line.lstrip("# ").strip() or section
        cur.append(line)
    if cur:
        blocks.append((section, "\n".join(cur).rstrip() + "\n"))
    return [b for b in blocks if b[1].strip()]


# начало нового блока: за продолжение абзаца такую строку принимать нельзя
NEW_BLOCK = re.compile(r"\s*(?:[-*+]\s|\d+[.)]\s|\||>)")
WRAP_TAIL = 4  # сколько физических строк абзаца забирать вместе с маркером


def wrapped_paragraph(src: list, start: int) -> list:
    """Строка с маркером плюс продолжение её абзаца — один факт, а не обрывок.

    🔑 Тела дневников свёрстаны по ~100 колонок, и маркер стоит в ПЕРВОЙ физической
    строке абзаца. Фильтр по одной строке отдавал огрызок вида «…вызывающий
    низкоуровневый метод»: место в сводке занято, а прочитать нечего. Замер 24.08:
    таких обрывков в недельной сводке была примерно половина всех строк с 🔴.

    ⚠️ Абзац кончается пустой строкой, заголовком или началом нового блока (список,
    таблица, цитата) — считать их продолжением нельзя, иначе в сводку затягивается
    соседний пункт целиком.
    """
    parts = [src[start].strip()]
    j = start + 1
    while j < len(src) and len(parts) < WRAP_TAIL:
        nxt = src[j]
        if not nxt.strip() or nxt.startswith("#") or NEW_BLOCK.match(nxt):
            break
        parts.append(nxt.strip())
        j += 1
    return parts


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
            src = text.split("\n")
            i = 0
            while i < len(src):
                line = src[i]
                if line.startswith("#"):
                    keep.append(line)
                    i += 1
                    continue
                hit = (mode == "red" and RED in line) or (
                    mode == "marks" and any(m in line for m in MARKS)
                )
                if not hit:
                    i += 1
                    continue
                para = wrapped_paragraph(src, i)
                keep.append(" ".join(para))
                i += len(para)
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


COMMITS_MAX = 25


def commits(root: Path, out: list, log: list, shown: set):
    """Остаток коммитов: то, что не легло ни под одну сессию.

    ⚠️ Служебные `sessions: … в работе` отсюда убраны совсем — на 17.08 это была
    треть блока (27 строк из 80), а доска печатается выше и полнее.
    """
    rest = [c for c in log if not c["board"] and not (c["sids"] & shown)]
    out.append("## Прочие коммиты памяти за сутки (вне сессий выше)")
    if not rest:
        out.append("- нет")
    for c in rest[:COMMITS_MAX]:
        when = c["when"].strftime("%m-%d %H:%M") if c["when"] else "—"
        out.append(f"{c['h']} {when} {c['subject']}")
    if len(rest) > COMMITS_MAX:
        out.append(f"- …ещё {len(rest) - COMMITS_MAX} — `git log --since='24 hours ago'`")
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


def memory_age_days(root: Path):
    """Сколько дней памятью пользуются, по самому раннему файлу дневника.

    Дата стоит в имени (`diary/<месяц>/<дата>.<sid>.md`) ⟹ ни git, ни mtime не нужны:
    mtime врёт после любого клона, а git стоит вызова. `None` — дневников нет вовсе,
    памятью ещё не пользовались.
    """
    ddir = root / "diary"
    if not ddir.is_dir():
        return None
    best = None
    for f in ddir.rglob("*.md"):
        m = re.match(r"(\d{4})-(\d{2})-(\d{2})", f.name)
        if not m:
            continue
        try:
            d = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3))).date()
        except ValueError:
            continue
        if best is None or d < best:
            best = d
    return None if best is None else (datetime.now().date() - best).days


def maintenance(root: Path, mem: Path, out: list, part_mode: bool = False):
    memory_md = root / "MEMORY.md"
    out.append("## Дальше")
    if memory_md.is_file():
        kb = memory_md.stat().st_size // 1024
        if part_mode:
            out.append(f"- `MEMORY.md` ({kb} КБ) приезжает частями выше — отдельно не читай")
        else:
            out.append(f"- прочитай `MEMORY.md` ({kb} КБ) — стартер его не пересказывает")
        text = memory_md.read_text(encoding="utf-8", errors="replace")
        m = re.search(r"Последняя оптимизация:\s*(\d{4}-\d{2}-\d{2})", text)
        # ⚠️ Шаблон `MEMORY.md` кладёт сюда тире, а не дату. Прежний regex ждал
        # только дату ⟹ на свежей установке блок пропускался ЦЕЛИКОМ и оптимизация
        # не предлагалась никогда — ровно там, где напоминание и нужно. Тире (и любую
        # другую не-дату) читаем как «не проводилась ни разу».
        never = None if m else re.search(r"Последняя оптимизация:\s*(\S+)", text)
        if m or never:
            cfg = (mem / "yamem.config.yaml")
            interval = 7
            if cfg.is_file():
                mi = re.search(r"interval_days:\s*(\d+)", cfg.read_text(encoding="utf-8"))
                interval = int(mi.group(1)) if mi else 7
        if m:
            last = datetime.strptime(m.group(1), "%Y-%m-%d").date()
            age = (datetime.now().date() - last).days
            if age >= interval:
                out.append(f"- ⚠️ оптимизация памяти просрочена: последняя {m.group(1)} "
                           f"({age} дн. назад при интервале {interval})")
            else:
                out.append(f"- оптимизация памяти: последняя {m.group(1)}, "
                           f"следующая через {interval - age} дн.")
        elif never:
            # 🔑 Возраст памяти берём по самому раннему дневнику: дата лежит прямо
            # в имени файла, лишнего вызова не нужно. Пустую память не дёргаем —
            # в только что развёрнутой оптимизировать нечего.
            age = memory_age_days(root)
            if age is None:
                pass  # памятью ещё не пользовались
            elif age >= interval:
                out.append(f"- ⚠️ оптимизация памяти не проводилась ни разу "
                           f"(в `MEMORY.md` стоит `{never.group(1)}`), а памяти уже "
                           f"{age} дн. при интервале {interval}")
            else:
                out.append(f"- оптимизация памяти ещё не проводилась — памяти "
                           f"{age} дн. из {interval}")
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

    def situation_entries():
        """Блоки «что вокруг»: доска сессий, банки, коммиты за сутки.

        🔑 Вынесены из ядра замером: на 21.08 они давали 78 % его веса (сессии 34 %,
        банки 22 %, коммиты 22 %) и растут сами — от числа сессий за день, банков
        и активности. Ядро от них не должно зависеть: это единственная часть,
        которую нельзя нарезать. ⚠️ Читаются заново и БЕЗ побочных действий
        (`mark=False`): отметку на доске ставит только часть 1.
        """
        out, log = [], recent_commits(root)
        seen = sessions(root, args.sid, args.topic, args.prune, args.no_sync,
                        out, log, mark=False)
        items = [("сессии", "\n".join(out))]
        out = []
        banks(mem, out)
        items.append(("банки", "\n".join(out)))
        out = []
        commits(root, out, log, seen)
        items.append(("коммиты", "\n".join(out)))
        return items

    entries = diary_entries(root, days, args.diary)
    sit_chunks = split_entries(situation_entries(), args.part_limit) if args.part else []
    task_chunks = split_entries(task_entries(root), args.part_limit) if args.part else []
    mem_chunks = split_entries(memory_entries(root), args.part_limit) if args.part else []
    chunks = split_entries(entries, args.part_limit) if args.part else []
    # ядро + обстановка + полный список задач + MEMORY.md + дневники
    total = 1 + len(sit_chunks) + len(task_chunks) + len(mem_chunks) + len(chunks)
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    sit_first = 2
    task_first = sit_first + len(sit_chunks)
    mem_first = task_first + len(task_chunks)
    diary_first = mem_first + len(mem_chunks)

    # ⚠️ Побочные действия (pull банков, отметка на доске, prune) делает ТОЛЬКО часть 1:
    # части запрашиваются параллельно, и три одновременных git-операции в одном
    # репозитории дерутся за индекс. Остальные части — чистое чтение.
    if args.part >= 2:
        idx = args.part - 2
        if idx < len(sit_chunks):  # обстановка → задачи → MEMORY.md → дневники
            out = [f"# yamem preflight — часть {args.part}/{total}: обстановка", ""]
            out += [text for _, text in sit_chunks[idx]]
            out.append(f"— конец части {args.part}/{total} —")
            print("\n".join(out))
            return
        idx -= len(sit_chunks)
        if idx < len(task_chunks):
            piece = task_chunks[idx]
            out = [f"# yamem preflight — часть {args.part}/{total}: задачи", ""]
            out.append(f"## Полный список открытых задач (кусок {idx + 1} из {len(task_chunks)})")
            out.append("")
            out += [text for _, text in piece]
            if idx + 1 == len(task_chunks):
                out.append("⚠️ Это полный список открытых задач — он не усечён, "
                           "`backlog.md` отдельно читать не нужно.")
            out.append(f"— конец части {args.part}/{total} —")
            print("\n".join(out))
            return
        idx -= len(task_chunks)
        if idx < len(mem_chunks):
            piece = mem_chunks[idx]
            secs = list(dict.fromkeys(s for s, _ in piece))
            out = [f"# yamem preflight — часть {args.part}/{total}: MEMORY.md", ""]
            out.append(f"## MEMORY.md, разделы: {', '.join(secs)}"
                       f" (кусок {idx + 1} из {len(mem_chunks)})")
            out.append("")
            out += [text for _, text in piece]
            if idx + 1 == len(mem_chunks):
                out.append("⚠️ Это полный текст `MEMORY.md`, читать его отдельно не нужно.")
        else:
            idx -= len(mem_chunks)
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

    head = [f"# yamem preflight — {stamp}"
            + (f" — часть 1/{total}" if args.part else ""), ""]
    sync(mem, args.no_sync, head)
    log = recent_commits(root)
    shown = sessions(root, args.sid, args.topic, args.prune, args.no_sync, head, log,
                     quiet=bool(args.part))

    # 🔑 Ядро — единственная часть, которую нельзя нарезать: она растёт вместе
    # со списком задач в работе (42 строки на 17.08) и однажды упрётся в потолок
    # вывода молча. Поэтому подбираем, сколько задач влезает, и говорим об этом.
    hint = (f"— части `--part {task_first}`…`--part {mem_first - 1}`"
            if task_chunks else "")
    # Последний рубеж: если ядро всё равно не влезает (разрослись банки, сессии,
    # коммиты) — режем и срочное. Полный список к этому моменту уже в частях,
    # поэтому резать здесь безопасно, в отличие от прежней схемы.
    out, limit_used = None, None
    for cand in (None, 5, 3, 1):
        trial = list(head)
        tasks(root, trial, hot_limit=cand, part_hint=hint)
        if not args.part:  # без частей резать некуда — печатаем всё подряд
            banks(mem, trial)
            commits(root, trial, log, shown)
        out, limit_used = trial, cand
        # ⚠️ Запас на футер (раскладка частей, напоминания, тайминг) — и на рост
        # соседних блоков между прогонами: банки и список сессий пухнут сами.
        # 🔴 На 600 Б ядро выходило 27.5 КБ при потолке 28 КБ — один новый банк
        # и вывод молча обрезался бы, а обрезание ядра не видно из сессии.
        if not args.part or size("\n".join(trial)) + 2500 <= HARNESS_LIMIT:
            break
    if limit_used is not None:
        out.append(f"ℹ️ даже срочное урезано до {limit_used}: ядро иначе не проходит потолок "
                   f"вывода {HARNESS_LIMIT} Б. Ничего не потеряно — всё {hint}.")
        out.append("")
    if args.part:
        out.append(f"## Остальные части — вызывать одним блоком")
        if total > 1:
            if sit_chunks:
                out.append(f"- `--part {sit_first}`…`--part {task_first - 1}` — **обстановка**: "
                           f"доска сессий с результатами, банки топиков, коммиты за сутки")
            if task_chunks:
                out.append(f"- `--part {task_first}`…`--part {mem_first - 1}` — **полный список "
                           f"открытых задач** ({len(task_chunks)} шт.), он не усечён")
            if mem_chunks:
                out.append(f"- `--part {mem_first}`…`--part {diary_first - 1}` — **MEMORY.md** "
                           f"целиком ({len(mem_chunks)} шт.), отдельно его читать не нужно")
            if chunks:
                out.append(f"- `--part {diary_first}`…`--part {total}` — сводки дневников"
                           f" за {days} дн. ({len(entries)} файлов, режим `{args.diary}`)")
            out.append(f"- всего частей **{total}** — это один раунд, если отправить их вместе")
        else:
            out.append("- больше частей нет")
        out.append("")
    else:
        # Запуск руками в терминале: части не нарезаются, поэтому печатаем всё подряд
        full = task_entries(root)
        if full:
            out.append("## Полный список открытых задач")
            out.append("")
            out += [text for _, text in full]
        out.append(f"## Дневники за {days} дн. — {len(entries)} файлов, режим `{args.diary}`")
        out.append("")
        out += [text for _, text in entries] or ["- записей нет", ""]
        out.append("⚠️ Это сводка. Полный текст дня — читать сам файл в `diary/`.")
        out.append("")
    maintenance(root, mem, out, part_mode=bool(args.part))
    elapsed = time.monotonic() - TIMING["start"]
    out.append(f"- ⏱ стартер {elapsed:.1f} с: `git pull` {TIMING['pull']:.1f} с, "
               f"отметка на доске (commit+push) {TIMING['mark']:.1f} с, "
               f"сборка {elapsed - TIMING['pull'] - TIMING['mark']:.1f} с"
               f"{' — git пропущен (`--no-sync`)' if args.no_sync else ''}")
    out.append("")
    if args.part:
        core = size("\n".join(out))
        if core > HARNESS_LIMIT:
            out.append(f"⚠️ ядро дайджеста {core} Б > потолка вывода {HARNESS_LIMIT} Б — "
                       f"вывод могло обрезать")
        out.append(f"— конец части 1/{total} —")
    print("\n".join(out))


if __name__ == "__main__":
    main()
