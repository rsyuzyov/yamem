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
        break
    fi
done
