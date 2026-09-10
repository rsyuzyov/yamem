#!/usr/bin/env python3
"""Гейт памяти: управляющие символы и съеденные пути в текстовых файлах.

🎯 Мотив. Запись в память идёт через оболочку, и `\1`, `\b`, `\v`, `\t` в пути Windows
съедаются ДО записи файла: `C:\1cv8` становится `C:` + 0x01 + `cv8`, `C:\temp` — `C:` +
табуляция + `emp`. В markdown это не видно: путь читается как правильный, а по факту он
нерабочий, и следующая сессия идёт по нему в никуда. Прецеденты: 50 мест в 26 файлах
(02.09.2026), ещё 18 путей с табуляцией (10.09.2026).

⚠️ Табуляция сама по себе легальна (отступ, блок кода, TSV) — она порча только внутри слова,
где слева стоит `<буква>:` или буква/слеш пути. Проверка судит по контексту, а не по символу.

Файл, которому управляющие символы нужны по смыслу (иллюстрация разделителя, цитата вывода
терминала с ESC), помечается строкой `yamem:allow-control-chars` в любом месте — тогда он
пропускается целиком.
"""
import io
import os
import re
import subprocess
import sys

ALLOW_MARK = 'yamem:allow-control-chars'
TAB = '\t'
# управляющие, кроме табуляции (9), перевода строки (10) и возврата каретки (13)
CTRL = re.compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f]')
# табуляция внутри пути: слева `C:` либо буква/цифра/слеш, справа продолжение слова
TAB_IN_WORD = re.compile(r'(?:[A-Za-z]:|[\/A-Za-zА-Яа-я0-9_.-])' + TAB + r'(?=[A-Za-z0-9_])')
TEXT_EXT = ('.md', '.py', '.sh', '.ps1', '.yaml', '.yml', '.conf')


def staged_files():
    out = subprocess.run(['git', 'diff', '--cached', '--name-only', '--diff-filter=ACM'],
                         capture_output=True).stdout.decode('utf-8', 'replace')
    return [p.strip() for p in out.split('\n') if p.strip().endswith(TEXT_EXT)]


def check(paths, read=None):
    bad = []
    for p in paths:
        try:
            text = read(p) if read else io.open(p, encoding='utf-8', errors='replace').read()
        except Exception:
            continue
        if ALLOW_MARK in text:
            continue
        for m in CTRL.finditer(text):
            bad.append((p, 'символ 0x%02x' % ord(m.group()), text[max(0, m.start() - 40):m.start() + 20]))
            break
        for m in TAB_IN_WORD.finditer(text):
            bad.append((p, 'табуляция внутри пути (съеденный \t)',
                        text[max(0, m.start() - 40):m.end() + 20]))
            break
    return bad


def main():
    args = [a for a in sys.argv[1:] if not a.startswith('-')]
    if args:
        paths = []
        for a in args:
            if os.path.isdir(a):
                for root, _, files in os.walk(a):
                    paths += [os.path.join(root, f) for f in files if f.endswith(TEXT_EXT)]
            else:
                paths.append(a)
        bad = check(paths)
    else:
        files = staged_files()
        if not files:
            return 0
        # содержимое берём ИЗ ИНДЕКСА: коммитится оно, а не то, что сейчас в рабочей копии
        def from_index(p):
            r = subprocess.run(['git', 'show', ':' + p], capture_output=True)
            return r.stdout.decode('utf-8', 'replace')
        bad = check(files, read=from_index)

    if not bad:
        return 0
    print('yamem: в тексте управляющие символы — почти всегда это путь, съеденный оболочкой:')
    for p, what, ctx in bad:
        print('  %s: %s' % (p.replace(os.sep, '/'), what))
        print('      …%s…' % ctx.replace('\n', ' ').replace(TAB, '<TAB>'))
    print()
    print('       Чинить: собирать путь из chr(92) вместо экранирования в литерале,')
    print('       после записи проверять файл на символы < 32 и табуляцию внутри слов.')
    print('       Если символ нужен по смыслу — пометить файл строкой ' + ALLOW_MARK + '.')
    return 1


if __name__ == '__main__':
    sys.exit(main())
