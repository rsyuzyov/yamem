#!/usr/bin/env python3
"""Гейт памяти: в индекс не должен попадать текст с CRLF там, где `.gitattributes` требует LF.

🎯 Мотив. Файл, закоммиченный с CRLF или смешанными переводами строк при `text eol=lf`, git считает
измененным у ВСЕХ сессий навсегда: рабочая копия при выписке приводится к LF и уже не совпадает
с индексом. `yamem_sync.py` прячет такие файлы в autostash перед ребейзом и не может вернуть их
обратно («NOT synced»), заодно уводя в тайник и чужие незакоммиченные записи. Прецедент 16.10.2026:
шесть отчетов в папке задачи — 14 тайников за вечер, записи памяти дважды уезжали в тайник.

Проверяется только то, что идет в коммит (`git diff --cached`), по колонке индекса `git ls-files --eol`.
Починка — `git add --renormalize -- <файлы>`: меняются только переводы строк.

Без аргументов — застейдженные файлы (режим pre-commit), `--all` — весь индекс (разовый аудит).
Обойти разово: YAMEM_ALLOW_EOL=1 git commit …
"""
import os
import subprocess
import sys

BAD_INDEX_EOL = ('i/crlf', 'i/mixed')
CHUNK = 200


def git_output(arguments):
    return subprocess.run(['git', '-c', 'core.quotePath=false'] + arguments, capture_output=True).stdout


def staged_paths():
    output = git_output(['diff', '--cached', '--name-only', '-z', '--diff-filter=ACMR'])
    return [path for path in output.decode('utf-8', 'replace').split('\0') if path]


def eol_records(paths):
    """Записи `git ls-files --eol`: (eol индекса, атрибуты, путь); paths=None — весь индекс."""
    chunks = [None] if paths is None else [paths[start:start + CHUNK] for start in range(0, len(paths), CHUNK)]
    records = []
    for chunk in chunks:
        if chunk == []:
            continue
        output = git_output(['ls-files', '--eol', '-z'] + ([] if chunk is None else ['--'] + chunk))
        for entry in output.decode('utf-8', 'replace').split('\0'):
            if '\t' not in entry:
                continue
            info, path = entry.split('\t', 1)
            fields = info.split()
            records.append((fields[0] if fields else '', info, path))
    return records


def main():
    if os.environ.get('YAMEM_ALLOW_EOL'):
        return 0
    whole_index = '--all' in sys.argv[1:]
    paths = None if whole_index else staged_paths()
    if paths == []:
        return 0
    bad_paths = [path for index_eol, info, path in eol_records(paths)
                 if index_eol in BAD_INDEX_EOL and 'eol=lf' in info]
    if not bad_paths:
        return 0
    print('yamem: в индексе текст с CRLF при `eol=lf` в .gitattributes - git будет считать эти файлы')
    print('       измененными у всех сессий, а синхронизация памяти - прятать их в autostash:')
    for path in bad_paths[:20]:
        print('         ' + path)
    if len(bad_paths) > 20:
        print('         … и еще %d' % (len(bad_paths) - 20))
    print('       Починка (меняются только переводы строк):')
    print('         git add --renormalize -- <файлы выше>')
    print('       Обойти разово: YAMEM_ALLOW_EOL=1 git commit …')
    return 1


if __name__ == '__main__':
    sys.exit(main())
