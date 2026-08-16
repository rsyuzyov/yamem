#!/bin/sh
# yamem: pre-commit-проверка генерируемых представлений памяти.
#
# Ставится один раз в глобальный хук (или в .git/hooks/pre-commit репозитория памяти):
#
#     sh "$HOME/repo/ai/yamem/scripts/yamem-precommit.sh" || exit 1
#
# Смысл: `backlog.md`, `archive.md` и раздел «База знаний» в MEMORY.md собираются
# из фронтматтеров. Без проверки они устаревают молча — файл в git выглядит живым,
# а правду показывают только сами задачи и топики.
#
# Проверка НЕ трогает файлы: коммит останавливается с подсказкой, что пересобрать.
# Если генераторы не найдены, хук не мешает работать и просто выходит.
set -e

root=$(git rev-parse --show-toplevel 2>/dev/null) || exit 0

# Где память. Два случая: коммитим сам репозиторий памяти (в корне MEMORY.md,
# конфиг лежит уровнем выше) либо проект, внутри которого память обычным каталогом.
mem=""
if [ -f "$root/MEMORY.md" ] && [ -f "$root/../yamem.config.yaml" ]; then
    mem=$(cd "$root/.." && pwd)
elif [ -f "$root/.agents/memory/yamem.config.yaml" ] &&
     git diff --cached --name-only | grep -q '^\.agents/memory/'; then
    mem="$root/.agents/memory"
fi
[ -n "$mem" ] || exit 0

# Где генераторы: переменная окружения, привычная раскладка, каталог плагинов.
scripts=""
for candidate in \
    "$YAMEM_HOME/scripts" \
    "$HOME/repo/ai/yamem/scripts" \
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
