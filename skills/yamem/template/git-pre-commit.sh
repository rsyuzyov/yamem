#!/bin/sh
# Глобальный pre-commit: проверка секретов + проверка представлений памяти.
#
# Ставится один раз:
#     git config --global core.hooksPath "$HOME/.git-hooks"
#     cp <навык>/template/git-pre-commit.sh "$HOME/.git-hooks/pre-commit"
#
# ⏱ Две вещи здесь сделаны ради скорости, и обе оплачены замером: этот хук
# срабатывает на КАЖДОМ старте сессии, потому что стартер коммитит отметку
# на доске, а на Windows любой лишний fork и glob стоят десятые доли секунды.
gitleaks protect --staged --redact -v || exit 1

# 1. Отметка на доске (`.sessions/<sid>.md`) не влияет ни на представления,
#    ни на индекс топиков — выходим ДО всякого поиска скрипта (−1.0 с).
staged=$(git diff --cached --name-only)
if [ -n "$staged" ] && ! printf '%s\n' "$staged" | grep -qv '\.sessions/'; then
    exit 0
fi

# 2. Путь к проверке кешируется: перебор кандидатов с globом по `~/repo/*`
#    стоит ~0.75 с (13 каталогов, NTFS). Кеш протухает при переезде рабочей
#    копии — тогда просто отрабатывает перебор и записывает новый путь.
# ⚠️ Пути с глобом обязательны: навык подключается junction'ом внутри проекта
# (`<проект>/.claude/skills/yamem`), и жёсткий список путей молча перестал
# работать, когда рабочая копия переехала — коммиты шли без проверки (17.08).
# 2a. Пак, поставленный маркетплейсом, живёт в кеше плагинов, и версий там НЕСКОЛЬКО:
#     обновление кладёт новый каталог рядом, старый остаётся лежать. Берём самый
#     свежий по mtime, а не первый по глобу — `0.1.0` сортируется раньше хеша
#     `9e20fb1b257c`, и хук иначе навсегда залипает на скриптах прошлой версии
#     (наблюдалось после обновления пака: проверки шли старым кодом, молча).
#     Кешировать этот путь нельзя по той же причине — он меняется при обновлении;
#     зато глоб узкий и стоит один fork, в отличие от перебора по `~/repo/*`.
yamem_hook=$(ls -t "$HOME"/.claude/plugins/cache/*/yamem/*/skills/yamem/scripts/yamem-precommit.sh 2>/dev/null | head -1)
if [ -n "$yamem_hook" ] && [ -f "$yamem_hook" ]; then
    sh "$yamem_hook" || exit 1
    exit 0
fi

cached=$(git config --global --get yamem.precommit 2>/dev/null)
if [ -n "$cached" ] && [ -f "$cached" ]; then
    sh "$cached" || exit 1
    exit 0
fi

for yamem_hook in \
    "$YAMEM_HOME/skills/yamem/scripts/yamem-precommit.sh" \
    "$HOME/.claude/skills/yamem/scripts/yamem-precommit.sh" \
    "$HOME"/repo/*/.claude/skills/yamem/scripts/yamem-precommit.sh \
    "$HOME"/repo/*/.agents/skills/yamem/skills/yamem/scripts/yamem-precommit.sh \
    "$HOME/repo/ai/yamem/skills/yamem/scripts/yamem-precommit.sh"
do
    if [ -f "$yamem_hook" ]; then
        git config --global yamem.precommit "$yamem_hook"
        sh "$yamem_hook" || exit 1
        exit 0
    fi
done

# 3. ⚠️🔴 Сюда мы попадаем, ТОЛЬКО если проверка не найдена. Раньше цикл здесь просто
#    заканчивался, хук выходил с кодом 0 и коммит уходил без проверки представлений —
#    молча и неотличимо от «всё чисто». Это ровно тот класс дефектов, ради которого
#    проверка и заводилась: отсутствие результата выглядит как хороший результат.
#    Поэтому дальше — вслух. Молчим лишь там, где проверять нечего: хук глобальный
#    и срабатывает во всех репозиториях, включая те, где памяти нет вовсе.
#    Признак «проверять есть что» берём тот же, по которому память ищет сам
#    `yamem-precommit.sh`: каталог `.agents/memory` в корне либо корень-память.
#    ⚠️ Не по именам путей (`tasks/`, `diary/`): в чужом репозитории такой каталог
#    — обычное дело, и хук начал бы останавливать людям посторонние коммиты.
root=$(git rev-parse --show-toplevel 2>/dev/null)
if [ -d "$root/.agents/memory" ]; then
    printf '%s\n' "$staged" | grep -q '^\.agents/memory/' || exit 0
elif [ ! -f "$root/yamem.config.yaml" ] && [ ! -f "$root/MEMORY.md" ]; then
    exit 0
fi
if [ -n "$YAMEM_SKIP_CHECK" ]; then
    echo "yamem: проверка представлений пропущена по YAMEM_SKIP_CHECK"
    exit 0
fi
echo "yamem: коммит трогает память, а проверка представлений НЕ НАЙДЕНА — остановлен."
echo "       Искали: кеш плагинов, \$YAMEM_HOME, ~/.claude/skills, ~/repo/*"
echo "       Починить одним из способов:"
echo "         YAMEM_HOME=<каталог репозитория yamem>  (в профиле оболочки)"
echo "         git config --global yamem.precommit <путь к yamem-precommit.sh>"
echo "       Разово пропустить: YAMEM_SKIP_CHECK=1 git commit ..."
exit 1
