#!/usr/bin/env python3
"""Аудит записей команды в банках памяти.

Три механические проверки + подсказка, что читать глазами:
  1) секреты и внутренние адреса в отчуждаемых банках;
  2) фронтматтер OKF строгим YAML-парсером (наш gen-topics-index толерантный и такое пропускает);
  3) дрейф топика в журнал: доля датированных строк и слова хроники.
Плюс объём по авторам за период и список файлов, правленных несколькими коммитами подряд,
— именно там живёт дефект «дописал знание, не сняв прежнее».

  python bank-audit.py --memory <путь к .agents/memory> [--since '24 hours ago']

Скоупа два, и это не одно и то же: `--leak-scope` — банки, где внутренняя фактура (IP, FQDN,
секреты) считается утечкой (по умолчанию отчуждаемые admin-recipes, onec); `--safe-scope` —
банки, где находкой считается указатель на запись сейфа (по умолчанию ВСЕ, кроме `--safe-skip`,
то есть кроме `local`): у каждого читателя раскладка сейфа своя, и в банке площадки такой
указатель так же не разыменовывается, как в отчуждаемом.

Ненулевой код возврата = есть на что посмотреть.
"""
import argparse, glob, os, re, subprocess, sys
from collections import Counter, defaultdict

# ⚠️ На Windows консоль по умолчанию cp1251, и первый же значок в выводе роняет прогон
# целиком (UnicodeEncodeError на \U0001f50e) — то есть аудит не запускается на машине,
# с которой его и зовут. Кодировку задаём явно, а не убираем значки из вывода.
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

try:
    import yaml
except ImportError:
    yaml = None

OK_TYPES = {'recipe', 'reference', 'incident', 'rule'}

# Внутреннее, чему не место в отчуждаемом банке. Банк площадки (lion) — законное исключение:
# он общий с командой этой площадки, фактура в нём и есть содержание.
LEAK = {
    'приватный IP':   re.compile(r'\b(?:192\.168|172\.(?:1[6-9]|2\d|3[01]))\.\d{1,3}\.\d{1,3}\b'),
    'наш FQDN':       re.compile(r'\b[\w-]+\.(?:prodline|moroz|sts|ag)\.local\b', re.I),
    # значение-литерал; подстановки ($var, Get-..., <плейсхолдер>, %ENV%) секретом не считаем
    'секрет?':        re.compile(r'(?i)(?:password|пароль|passwd|secret|token)\s*[:=]\s*[\x22\x27]?'
                                 r'(?![$<%]|Get-|New-)[A-Za-z0-9!@#$%^&*_+=-]{8,}'),
    'приватный ключ': re.compile(r'BEGIN (?:RSA |OPENSSH |EC )?PRIVATE KEY'),
}

# ⭐🔴 Указатель на запись ЛИЧНОГО сейфа — ОТДЕЛЬНЫЙ класс со СВОИМ скоупом, и вот почему.
# Остальные классы выше — внутренняя фактура: в банке площадки она законна, потому что банк
# общий с командой этой площадки и фактура в нём и есть содержание. Указатель на сейф так не
# работает: раскладка сейфа у КАЖДОГО читателя своя, поэтому и в банке площадки он одинаково
# бесполезен своим (заход встаёт на поиске записи, которой у читателя нет, — прецедент
# 27.08.2026, стоил захода) и полезен чужим. ⟹ прежний гейт исключал банки площадок ЦЕЛИКОМ,
# по банку, а не по классу, и этот класс в них не проверялся вовсе (находки 19.09: lion-site —
# 6 топиков, prodline-site — 1). Теперь скоуп задаётся отдельно: `--safe-scope` (по умолчанию
# все банки, кроме `local`, где запись сейфа как раз на своём месте).
# ⟹ ловим путь в сейфе, имя записи вида «логин@хост» рядом со словом о сейфе и ссылку
# на карту секретов личного банка. Само слово kdbx/KeePass без имени записи — не находка.
SAFE_POINTER = re.compile(
    r'(?i)(?:kdbx|keepass|сейфе?|сейфа)[^\n]{0,40}[`\x22\x27]?[\w.-]+@[\w.-]+'
    r'|[`\x22\x27][\w.-]+@[\w.-]+[`\x22\x27][^\n]{0,25}(?:в сейфе|из сейфа|kdbx)'
    r'|secrets-map'
    r'|(?:kdbx|keepass|сейф\w*)[^\n]{0,30}[`\x22\x27]?/[А-Яа-яA-Za-z][^\n`\x22\x27]{2,40}/')
# ⚠️ Законная формулировка, которой правило и требует взамен указателя: «запись сейфа —
# в личных банках». Без этого исключения гейт ругался ровно на ту строку, которую сам же
# предписывает писать (truenas.md, onesible.md — прогон 19.09), а ложная тревога в гейте
# стоит дороже пропуска: её учатся пролистывать вместе с настоящими находками.
SAFE_POINTER_OK = re.compile(r'(?i)в личн(?:ых|ом) банк')
# x.y.0.0 и x.y.255.255 — маски и broadcast из рецептов; 192.168.0.1/1.1 — заводские адреса
# железок в рецептах. Ни то, ни другое не наши хосты.
MASKISH = re.compile(r'\.(?:0\.0|255\.255)$|^192\.168\.[01]\.1$')
CHRON = re.compile(r'(?i)(сделано|закрыт|осталось|отдан[оы] PR|пункт \d+ закрыт|итог дня|на завтра)')
DATE = re.compile(r'\b(?:\d{1,2}\.\d{2}(?:\.\d{4})?|20\d\d-\d\d-\d\d)\b')
DRIFT_DATES, DRIFT_CHRON, DRIFT_TOP = 0.15, 6, 5


def git(repo, *args):
    r = subprocess.run(['git', '-C', repo, *args], capture_output=True, text=True, encoding='utf-8')
    return r.stdout or ''


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--memory', required=True)
    ap.add_argument('--since', default='24 hours ago')
    ap.add_argument('--leak-scope', default='admin-recipes,onec',
                    help='банки, где внутренняя фактура = утечка (через запятую)')
    ap.add_argument('--safe-scope', default='*',
                    help='банки, где указатель на сейф = находка; "*" — все, кроме --safe-skip')
    ap.add_argument('--safe-skip', default='local',
                    help='банки, где запись сейфа на своём месте (через запятую)')
    a = ap.parse_args()
    strict = set(a.leak_scope.split(','))
    safe_scope = set(a.safe_scope.split(','))
    safe_skip = set(a.safe_skip.split(','))
    banks = sorted(d for d in glob.glob(os.path.join(a.memory, 'banks', '*')) if os.path.isdir(d))
    rc = 0

    for b in banks:
        name = os.path.basename(b)
        print('\n=== банк %s ===' % name)

        if os.path.exists(os.path.join(b, '.git')):
            log = git(b, 'log', '--since=%s' % a.since, '--pretty=%an', '--name-only')
            authors = Counter(l for l in log.splitlines() if l and not l.endswith('.md'))
            touched = defaultdict(int)
            for l in log.splitlines():
                if l.endswith('.md'):
                    touched[l] += 1
            if authors:
                print('  правок за период:', ', '.join('%s — %d' % kv for kv in authors.most_common()))
            hot = [f for f, n in sorted(touched.items(), key=lambda x: -x[1]) if n > 1]
            if hot:
                print('  🔎 читать глазами (правлен несколькими коммитами):', ', '.join(hot[:8]))

        drift = []
        for f in sorted(glob.glob(os.path.join(b, 'topics', '*.md'))):
            base = os.path.basename(f)
            t = open(f, encoding='utf-8').read()
            lines = t.splitlines()

            m = re.match(r'^---\n(.*?)\n---\n', t, re.S)
            if not m:
                print('  🔴 %s: нет фронтматтера' % base); rc = 1
            elif yaml:
                try:
                    fm = yaml.safe_load(m.group(1)) or {}
                    if fm.get('type') not in OK_TYPES:
                        print('  🔴 %s: type=%r вне набора' % (base, fm.get('type'))); rc = 1
                    if not (fm.get('description') or '').strip():
                        print('  🔴 %s: нет description' % base); rc = 1
                except Exception as e:
                    print('  🔴 %s: YAML не парсится — %s' % (base, str(e).splitlines()[0])); rc = 1

            if name in strict:
                for label, pat in LEAK.items():
                    hits = [h for h in sorted(set(pat.findall(t))) if not MASKISH.search(str(h))][:3]
                    if hits:
                        print('  ⚠️ %s: %s — %s' % (base, label, hits)); rc = 1

            # 🔑 Указатель на сейф ищем ПОСТРОЧНО, а не по всему тексту: только так видно
            # и номер строки (иначе находку ещё надо искать глазами), и соседний текст,
            # по которому отличается законная отсылка «в личных банках» от самого указателя.
            if name not in safe_skip and ('*' in safe_scope or name in safe_scope):
                for i, line in enumerate(lines, 1):
                    if SAFE_POINTER_OK.search(line):
                        continue
                    m2 = SAFE_POINTER.search(line)
                    if m2:
                        print('  ⚠️ %s:%d: указатель на сейф — %r'
                              % (base, i, m2.group(0)[:70])); rc = 1

            if len(lines) > 60:
                d = sum(1 for l in lines if DATE.search(l))
                c = sum(1 for l in lines if CHRON.search(l))
                if d / len(lines) > DRIFT_DATES or c >= DRIFT_CHRON:
                    drift.append((d / len(lines), c, len(lines), base))

        # 🔑 показываем ту величину, по которой отбираем, и честно говорим, сколько не влезло
        drift.sort(reverse=True)
        for share, c, n, base in drift[:DRIFT_TOP]:
            print('  🟡 %s: дрейф в журнал — дат %.0f%% строк, хроники %d на %d' % (base, share * 100, c, n))
        if len(drift) > DRIFT_TOP:
            print('  🟡 …ещё %d топиков сверх порога (дат >%.0f%% либо хроники ≥%d)'
                  % (len(drift) - DRIFT_TOP, DRIFT_DATES * 100, DRIFT_CHRON))

    print('\nбитых мест:', 'нет' if rc == 0 else 'см. выше')
    return rc


if __name__ == '__main__':
    sys.exit(main())
