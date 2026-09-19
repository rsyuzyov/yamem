#!/usr/bin/env python3
"""Синхронизация репозитория памяти перед записью: fetch + rebase, безопасные при соседях.

Запуск руками — вместо `git fetch && git rebase @{u}`:

    python <навык>/scripts/yamem_sync.py <репозиторий> [<репозиторий> ...]

Без аргументов берётся текущий каталог. Код возврата 0 — синхронизировано.
Стартер зовёт отсюда же `safe_sync`, поэтому поведение у них одно.

🔑 Зачем отдельный шаг, а не голый `git rebase --autostash`. Рабочая копия памяти у сессий
ОБЩАЯ, и индекс в ней в любой момент может держать соседний `git commit`. Если ребейз
натыкается на чужой `index.lock` уже после того, как спрятал правки в автотайник, он падает
на `reset --hard` и оставляет `.git/rebase-merge/` с одним файлом `autostash`. Дальше:
- `git rebase` у ВСЕХ сессий падает «already a rebase-merge directory», синхронизация стоит;
- незакоммиченная работа соседей может остаться только в тайнике — из рабочей копии она
  пропадает, и сессии об этом не знают (прецедент 19.09.2026: 14 файлов нескольких сессий,
  два случая за вечер).

Что делается здесь:
1. нет отставания от upstream — ребейза нет вовсе (так в большинстве вызовов, и тогда
   столкнуться не с чем);
2. перед ребейзом ждём, пока соседский `index.lock` освободится;
3. оборванный ребейз (в `rebase-merge` только `autostash`) чинится без потери работы:
   тайник сохраняется в `git stash`, правки возвращаются в рабочую копию, если их там нет,
   каталог удаляется; ребейз повторяется.
"""
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

LOCK_WAIT_SEC = 15
ATTEMPTS = 3
# Живой ребейз тоже начинает с тайника, а остальные файлы состояния пишет следом.
# Каталог моложе этого возраста - возможно, соседский ребейз в работе, его не трогаем.
ORPHAN_MIN_AGE_SEC = 10


def run(cmd, cwd=None, timeout=120):
    try:
        proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                              timeout=timeout, encoding="utf-8", errors="replace")
        return proc.returncode, (proc.stdout or "").strip(), (proc.stderr or "").strip()
    except Exception as exc:  # noqa: BLE001 - синхронизация не должна ронять вызывающего
        return 1, "", str(exc)


def git_path(repo: Path, name: str) -> Path:
    """Путь внутри каталога git: у субмодуля `.git` - файл, а не каталог."""
    code, out, _ = run(["git", "rev-parse", "--git-path", name], cwd=repo)
    path = Path(out) if code == 0 and out else Path(".git") / name
    return path if path.is_absolute() else Path(repo) / path


def wait_index_lock(repo: Path, timeout: float = LOCK_WAIT_SEC) -> bool:
    lock = git_path(repo, "index.lock")
    deadline = time.monotonic() + timeout
    while lock.exists() and time.monotonic() < deadline:
        time.sleep(0.2)
    return not lock.exists()


def heal_orphan_autostash(repo: Path, min_age: float = ORPHAN_MIN_AGE_SEC):
    """Чинит оборванный ребейз. Возврат: строка-отчёт либо None, если чинить нечего."""
    state = git_path(repo, "rebase-merge")
    if not state.is_dir():
        return None
    names = {entry.name for entry in state.iterdir()}
    if names - {"autostash"}:
        return None   # настоящий ребейз (идёт или встал на конфликте) - решает человек
    if time.time() - state.stat().st_mtime < min_age:
        return None

    sha = ""
    if "autostash" in names:
        sha = (state / "autostash").read_text(encoding="utf-8", errors="replace").strip()
    if not sha:
        shutil.rmtree(state)
        return "оборванный ребейз без тайника - каталог снят"

    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    code, _, err = run(["git", "stash", "store", "-m", f"yamem: оборванный autostash {stamp}", sha], cwd=repo)
    if code != 0:
        return f"⚠️ оборванный ребейз НЕ починен: не удалось сохранить тайник {sha[:8]} ({err[:80]})"

    # Где сейчас правки из тайника: в рабочей копии, на месте прежнего состояния или частично.
    _, files, _ = run(["git", "diff", "--name-only", f"{sha}^1", sha], cwd=repo)
    present, missing, other = [], [], []
    for name in files.splitlines():
        if run(["git", "diff", "--quiet", sha, "--", name], cwd=repo)[0] == 0:
            present.append(name)
        elif run(["git", "diff", "--quiet", f"{sha}^1", "--", name], cwd=repo)[0] == 0:
            missing.append(name)
        else:
            other.append(name)

    shutil.rmtree(state)
    if missing and not other:
        code, _, err = run(["git", "stash", "apply", sha], cwd=repo)
        if code != 0:
            return (f"⚠️ оборванный ребейз: правки {len(missing)} файлов не вернулись сами - "
                    f"`git stash show -p stash@{{0}}` ({err[:80]})")
        return f"оборванный ребейз починен: {len(missing)} файлов возвращены из тайника (stash@{{0}})"
    if other:
        return (f"⚠️ оборванный ребейз: {len(other)} файлов изменились и после тайника - "
                f"сверить руками с `git stash show -p stash@{{0}}`: {', '.join(other[:5])}")
    return f"оборванный ребейз починен: правки уже в рабочей копии, тайник сохранён (stash@{{0}})"


def safe_sync(repo: Path):
    """fetch + rebase на upstream. Возврат: (код, отчёт, stderr)."""
    repo = Path(repo)
    notes = []
    healed = heal_orphan_autostash(repo)
    if healed:
        notes.append(healed)

    code, upstream, _ = run(["git", "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"], cwd=repo)
    if code != 0:
        return 1, "; ".join(notes), "у текущей ветки нет upstream"
    # Ребейзим на remote-tracking ref, а не через `pull`: цель `pull` лежит в общем
    # `.git/FETCH_HEAD`, и соседский fetch перезаписывает её посреди операции.
    code, out, err = run(["git", "fetch", "--quiet"], cwd=repo)
    if code != 0:
        return code, "; ".join(notes), err or out

    code, behind, _ = run(["git", "rev-list", "--count", f"HEAD..{upstream}"], cwd=repo)
    if code == 0 and behind.strip() == "0":
        return 0, "; ".join(notes + ["уже актуально"]), ""

    err = ""
    for _attempt in range(ATTEMPTS):
        wait_index_lock(repo)
        code, out, err = run(["git", "rebase", "--autostash", upstream], cwd=repo)
        if code == 0:
            lines = "\n".join(filter(None, [out, err])).splitlines()
            return 0, "; ".join(notes + [lines[-1][:80] if lines else "готово"]), ""
        healed = heal_orphan_autostash(repo, min_age=0)
        if healed:
            notes.append(healed)
        if "index.lock" not in err and not healed:
            break
        time.sleep(1)
    return code, "; ".join(notes), err


def main(argv):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")   # консоль Windows иначе бьёт кириллицу
    repos = [Path(arg) for arg in argv] or [Path.cwd()]
    worst = 0
    for repo in repos:
        code, report, err = safe_sync(repo)
        status = "synced" if code == 0 else "⚠️ NOT synced"
        tail = report if code == 0 else "; ".join(filter(None, [report, (err.splitlines() or [""])[0][:120]]))
        print(f"{repo}: {status} - {tail}")
        worst = max(worst, 1 if code else 0)
    return worst


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
