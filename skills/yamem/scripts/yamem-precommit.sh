#!/bin/sh
# yamem: pre-commit-проверка генерируемых представлений памяти.
#
# Ставится один раз в глобальный хук (или в .git/hooks/pre-commit репозитория памяти):
#
#     sh "$HOME/.claude/skills/yamem/scripts/yamem-precommit.sh" || exit 1
#
# Смысл: `backlog.md`, `archive.md` и раздел «База знаний» в MEMORY.md собираются
# из фронтматтеров. Без проверки они устаревают молча — файл в git выглядит живым,
# а правду показывают только сами задачи и топики.
#
# Проверка НЕ трогает файлы: коммит останавливается с подсказкой, что пересобрать.
# Если генераторы не найдены, хук не мешает работать и просто выходит.
set -e

root=$(git rev-parse --show-toplevel 2>/dev/null) || exit 0

# Где память. Три случая: репозиторий памяти целиком (конфиг и MEMORY.md в корне),
# старая раскладка (репозиторий = каталог local, конфиг уровнем выше) либо проект,
# внутри которого память лежит обычным каталогом.
mem=""
if [ -f "$root/yamem.config.yaml" ] && [ -f "$root/MEMORY.md" ]; then
    mem="$root"
elif [ -f "$root/MEMORY.md" ] && [ -f "$root/../yamem.config.yaml" ]; then
    mem=$(cd "$root/.." && pwd)
elif [ -f "$root/.agents/memory/yamem.config.yaml" ] &&
     git diff --cached --name-only | grep -q '^\.agents/memory/'; then
    mem="$root/.agents/memory"
fi
[ -n "$mem" ] || exit 0

# ⏱ Отметка на доске сессий (`.sessions/<sid>.md`) не влияет ни на представления,
# ни на индекс топиков — проверять тут нечего. А полный прогон генераторов читает
# все задачи и топики и стоит ~3 с, которые платит КАЖДЫЙ старт сессии.
staged=$(git diff --cached --name-only)
if [ -n "$staged" ] && ! printf '%s\n' "$staged" | grep -qv '\.sessions/'; then
    exit 0
fi

# Где генераторы. 🔑 С 2026-08-17 они лежат ВНУТРИ каталога навыка (`skills/yamem/scripts`),
# чтобы ехать вместе с ним при любом способе подключения; старые пути оставлены как фолбэк.
scripts=""
for candidate in \
    "$(dirname "$0")" \
    "$YAMEM_HOME/skills/yamem/scripts" \
    "$YAMEM_HOME/scripts" \
    "$HOME/.claude/skills/yamem/scripts" \
    "$HOME"/repo/*/.claude/skills/yamem/scripts \
    "$HOME"/repo/*/.agents/skills/yamem/skills/yamem/scripts \
    "$HOME/repo/ai/yamem/skills/yamem/scripts" \
    "$HOME/repo/ai/yamem/scripts" \
    "$HOME"/.claude/plugins/cache/*/yamem/*/skills/yamem/scripts \
    "$HOME"/.claude/plugins/*/yamem/skills/yamem/scripts \
    "$HOME"/.claude/plugins/*/yamem/scripts \
    "$HOME"/.claude/plugins/yamem/scripts
do
    if [ -f "$candidate/gen-backlog.py" ]; then
        scripts="$candidate"
        break
    fi
done
if [ -z "$scripts" ]; then
    echo "yamem: генераторы не найдены — проверка представлений пропущена"
    echo "       (задай YAMEM_HOME=<каталог репозитория yamem>)"
    exit 0
fi

# ⚠️ Выбирать интерпретатор проверкой запуском, а не `command -v`: на Windows
# в PATH лежит заглушка `python3` из Microsoft Store — она существует, печатает
# «Python» и возвращает 49, из-за чего проверка ложно «падала».
python=""
for candidate in python3 python py; do
    if command -v "$candidate" >/dev/null 2>&1 &&
       "$candidate" -c "import sys; sys.exit(0)" >/dev/null 2>&1; then
        python="$candidate"
        break
    fi
done
[ -n "$python" ] || exit 0

rc=0
PYTHONIOENCODING=utf-8 "$python" "$scripts/gen-backlog.py" --memory "$mem" --check || rc=1
PYTHONIOENCODING=utf-8 "$python" "$scripts/gen-topics-index.py" --memory "$mem" --check || rc=1

if [ "$rc" -ne 0 ]; then
    echo
    echo "yamem: представления разошлись с источником — коммит остановлен."
    echo "       Пересобрать:"
    echo "         $python \"$scripts/gen-backlog.py\" --memory \"$mem\" --apply"
    echo "         $python \"$scripts/gen-topics-index.py\" --memory \"$mem\" --apply"
    echo "       Жалобы на фронтматтер выше чинятся в самих задачах и топиках."
    exit 1
fi
exit 0
