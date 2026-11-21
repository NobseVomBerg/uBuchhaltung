# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Zählt das Projekt für „Projekt in Zahlen“ auf der About-Seite – nur Standardbibliothek.

Aufruf:

* ``python tools/project_stats.py``          zählt, sammelt die Testfälle ein und schreibt ``project_stats.json`` neben ``version.py``;
                                             scheitert das Einsammeln, bricht es mit Rückgabecode 1 ab und schreibt nichts
* ``python tools/project_stats.py --check``  schreibt nichts; Rückgabecode 1, wenn die Datei fehlt, unlesbar ist oder nicht zur
                                             Version (``version.APP_VERSION``) gehört
* ``python tools/project_stats.py --print``  gibt die frisch gezählten Werte aus, ohne zu schreiben und ohne Testfälle einzusammeln
                                             (``cases`` kommt dann aus der vorhandenen Datei, sonst ``null``)

Die About-Seite liest nur diese Datei (``server/project_stats.py``, streng); zur Laufzeit wird nichts gezählt – im Docker-Image fehlt
``tests/``. Bei jedem Versionssprung neu erzeugen und mit committen (``tests/test_project_stats.py`` verlangt, dass sie zur Version
gehört).

Was gezählt wird (die Auswahl steht als Daten unten: ``EXCLUDED_DIR_NAMES``, ``EXCLUDED_DIR_PATHS``, ``classify``):

* ``code``  – alle ``*.py`` außerhalb von ``tests/``, dazu ``buch.css`` und ``tools/*.mjs``
* ``tests`` – alle ``*.py`` unter ``tests/``
* ``data``  – alle ``*.json`` unter ``seed_data/`` (ohne ``seed_data/private``)

Je Gruppe ``files`` und ``lines`` (``str.splitlines()``); für ``code`` und ``tests`` ``code_lines`` (ohne Leerzeilen und Kommentare – bei
Python zählen Docstrings als Kommentar), für ``data`` ``non_blank_lines``; für ``tests`` dazu ``functions`` (statisch über ``ast``: jede
``def test…`` in ``tests/test_*.py`` auf Modulebene oder in einer Klasse) und ``cases`` (was ``pytest --collect-only`` einsammelt: eine
parametrisierte Funktion ergibt mehrere Fälle).

Gezählt wird mit ``os.walk`` im Projektordner, nicht mit ``git`` – so läuft es auch in einem Stand ohne ``.git`` und in den Tests.
Symbolische Links werden nicht verfolgt und nicht gezählt, eine Datei, die sich nicht als UTF-8 lesen lässt, zählt mit ersetzten Zeichen.
Die Datei trägt weder Datum noch Pfade noch Rechnernamen: Sie ändert sich nur, wenn sich das Projekt ändert.
"""
import argparse
import ast
import contextlib
import io
import json
import os
import re
import stat
import subprocess
import sys
import tokenize
import warnings

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from version import APP_VERSION  # noqa: E402

STATS_FILE = 'project_stats.json'
COLLECT_TIMEOUT = 600                       # Sekunden, die das Einsammeln der Testfälle höchstens dauern darf

# Welche Ordner nie gezählt werden: Namen an jeder Stelle des Baums, dazu Ordner relativ zum Projektordner (mit ``/``)
EXCLUDED_DIR_NAMES = frozenset({'.git', '.claude', '.venv', '.vscode', '.pytest_cache', '__pycache__', 'node_modules', 'data',
                                'license-agpl', 'backup'})
EXCLUDED_DIR_PATHS = frozenset({'seed_data/private'})


def classify(rel):
    """Zu welcher Gruppe (``code``, ``tests``, ``data``) die Datei mit diesem Pfad gehört (relativ zum Projektordner, mit ``/``);
    ``None``: Sie wird nicht gezählt."""
    parts = rel.split('/')
    if rel.endswith('.py'):
        return 'tests' if parts[0] == 'tests' else 'code'
    if rel == 'buch.css' or (len(parts) == 2 and parts[0] == 'tools' and rel.endswith('.mjs')):
        return 'code'
    if parts[0] == 'seed_data' and rel.endswith('.json'):
        return 'data'
    return None


def project_files(root):
    """``(Gruppe, Pfad, relativer Pfad)`` jeder zu zählenden Datei unter ``root``, in fester Reihenfolge. Ausgeschlossene Ordner
    werden nicht betreten, Links nicht verfolgt, und nur reguläre Dateien zählen."""
    root = os.fspath(root)
    for folder, dirnames, filenames in os.walk(root):
        rel_folder = os.path.relpath(folder, root).replace(os.sep, '/')
        prefix = '' if rel_folder == '.' else rel_folder + '/'
        dirnames[:] = sorted(name for name in dirnames
                             if name not in EXCLUDED_DIR_NAMES and prefix + name not in EXCLUDED_DIR_PATHS)
        for name in sorted(filenames):
            group = classify(prefix + name)
            path = os.path.join(folder, name)
            if group is not None and stat.S_ISREG(os.lstat(path).st_mode):
                yield group, path, prefix + name


def read_text(path):
    with open(path, 'rb') as handle:
        return handle.read().decode('utf-8-sig', errors='replace')


def count_lines(text):
    return len(text.splitlines())


def _physical_lines(text):
    """Die Zeilen, wie Python sie nummeriert (getrennt wird nur bei Zeilenvorschub, Wagenrücklauf und beidem): Darauf beziehen sich die
    Zeilennummern von ``ast`` und ``tokenize``. ``str.splitlines()`` trennte auch bei U+2028 in einer Zeichenkette und beim
    Seitenvorschub und zählte dann mehr Zeilen."""
    lines = re.split(r'\r\n|\r|\n', text)
    if lines[-1] == '':                      # der Text endet mit einem Zeilenumbruch (oder ist leer)
        lines.pop()
    return lines


def _parse(source):
    with warnings.catch_warnings():
        warnings.simplefilter('ignore')      # ein ungültiges Escape in einer Zeichenkette warnt schon beim Parsen
        return ast.parse(source)


# ── Python: ohne Leerzeilen, Kommentare und Docstrings ───────────────────────
_NO_CODE_TOKENS = frozenset({tokenize.NL, tokenize.NEWLINE, tokenize.INDENT, tokenize.DEDENT, tokenize.ENDMARKER})


def _docstring_lines(expr, lines):
    """Nummern der Zeilen einer Docstring-Zeichenkette, auf denen sonst nichts steht – nur Leerraum davor, dahinter höchstens ein
    Kommentar. Steht Code in derselben Zeile (davor: ``def f(): …``, dahinter: ``…; return 1``), bleibt sie eine Zeile mit Code. Die
    Spalten von ``ast`` zählen UTF-8-Bytes."""
    found = []
    for number in range(expr.lineno, expr.end_lineno + 1):
        raw = lines[number - 1].encode('utf-8')
        before = raw[:expr.col_offset] if number == expr.lineno else b''
        after = raw[expr.end_col_offset:].strip() if number == expr.end_lineno else b''
        if not before.strip() and (not after or after.startswith(b'#')):
            found.append(number)
    return found


def _python_skipped_lines(lines):
    """Nummern (ab 1) der Zeilen, die nur Kommentar sind oder ganz zu einem Docstring gehören. Ein ``#`` in einer Zeichenkette ist
    kein Kommentar: Kommentare kommen aus ``tokenize``, Docstrings aus ``ast``. Wirft ``SyntaxError`` oder ``tokenize.TokenError``,
    wenn sich der Text nicht lesen lässt."""
    source = '\n'.join(lines) + '\n'
    tree = _parse(source)
    commented, with_code = set(), set()
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type == tokenize.COMMENT:
            commented.add(token.start[0])
        elif token.type not in _NO_CODE_TOKENS:
            with_code.update(range(token.start[0], token.end[0] + 1))       # eine Zeichenkette über mehrere Zeilen: alle
    skipped = commented - with_code
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(first.value.value, str):
                skipped.update(_docstring_lines(first, lines))
    return skipped


def python_code_lines(text):
    """Zeilen ohne Leerzeilen, ohne Kommentarzeilen und ohne Docstrings. Lässt sich die Datei nicht parsen, gilt die einfache Regel:
    nicht leer und nach Leerraum kein ``#``."""
    lines = _physical_lines(text)
    try:
        skipped = _python_skipped_lines(lines)
    except (SyntaxError, tokenize.TokenError):
        return sum(1 for line in lines if line.strip() and not line.lstrip().startswith('#'))
    return sum(1 for number, line in enumerate(lines, 1) if line.strip() and number not in skipped)


# ── CSS und JavaScript (.mjs): ohne Leerzeilen und Kommentare ────────────────
def _after_string(line, start):
    """Index hinter der Zeichenkette, die bei ``start`` beginnt (``len(line)``, wenn sie in der Zeile nicht endet)."""
    quote = line[start]
    i = start + 1
    while i < len(line):
        if line[i] == '\\':
            i += 2
        elif line[i] == quote:
            return i + 1
        else:
            i += 1
    return len(line)


def _braced_code_lines(lines, line_comments):
    """Zeilen mit Code neben ``/* … */`` (auch über mehrere Zeilen) und – mit ``line_comments`` – ``//`` bis zum Zeilenende. Ein
    einfacher Zustandsautomat: Der Inhalt einer Zeichenkette (einfache und doppelte Anführungszeichen, in JavaScript auch Backticks)
    ist kein Kommentar, solange sie in ihrer Zeile endet – ``"/*.png"`` in ``screenshots.mjs`` öffnete sonst einen Block, der die
    restliche Datei verschluckt; eine Zeichenkette über mehrere Zeilen erkennt er nicht. Eine Zeile mit Code UND Kommentar zählt
    als Code."""
    quotes = '\'"`' if line_comments else '\'"'
    count = 0
    in_block = False
    for line in lines:
        has_code = False
        i = 0
        while i < len(line):
            if in_block:
                end = line.find('*/', i)
                if end < 0:
                    break
                in_block = False
                i = end + 2
            elif line.startswith('/*', i):
                in_block = True
                i += 2
            elif line_comments and line.startswith('//', i):
                break
            elif line[i] in quotes:
                has_code = True
                i = _after_string(line, i)
            else:
                has_code = has_code or not line[i].isspace()
                i += 1
        count += has_code
    return count


def css_code_lines(text):
    return _braced_code_lines(_physical_lines(text), line_comments=False)


def mjs_code_lines(text):
    return _braced_code_lines(_physical_lines(text), line_comments=True)


_CODE_LINE_COUNTERS = {'.py': python_code_lines, '.css': css_code_lines, '.mjs': mjs_code_lines}


# ── Testfunktionen und Testfälle ─────────────────────────────────────────────
def is_test_module(rel):
    """Ein Modul ``tests/test_*.py`` (relativer Pfad mit ``/``): nur dort werden Testfunktionen gezählt."""
    return rel.startswith('tests/') and rel.endswith('.py') and rel.rsplit('/', 1)[-1].startswith('test_')


def _count_tests(body):
    """``def test…`` im Körper eines Moduls oder einer Klasse – in verschachtelten Klassen auch, in Funktionen nie."""
    total = 0
    for node in body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith('test'):
            total += 1
        elif isinstance(node, ast.ClassDef):
            total += _count_tests(node.body)
    return total


def count_test_functions(text):
    """Zahl der Testfunktionen eines Moduls; eines, das sich nicht parsen lässt, zählt 0."""
    try:
        return _count_tests(_parse(text).body)
    except SyntaxError:
        return 0


def collect_cases(root):
    """Zahl der Testfälle, wie ``pytest --collect-only`` sie einsammelt, oder ``None`` (Rückgabecode ≠ 0, keine Schlusszeile,
    Zeitüberschreitung, pytest nicht startbar). Bei ``A/B tests collected (n deselected)`` zählt ``A``: was ein Lauf ausführt."""
    command = [sys.executable, '-m', 'pytest', 'tests', '--collect-only', '-q', '-p', 'no:cacheprovider']
    try:
        done = subprocess.run(command, cwd=root, capture_output=True, encoding='utf-8', errors='replace', timeout=COLLECT_TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0:
        return None
    for line in reversed(done.stdout.splitlines()):
        match = re.search(r'(?:(\d+)/)?(\d+) tests? collected', line)
        if match:
            return int(match.group(1) or match.group(2))
    return None


# ── Zählen, Schreiben, Prüfen ────────────────────────────────────────────────
def count_project(root):
    """Die Zahlen aller drei Gruppen, ohne ``cases`` (das Einsammeln braucht pytest: ``collect_cases``)."""
    counted = {
        'code': {'files': 0, 'lines': 0, 'code_lines': 0},
        'tests': {'files': 0, 'lines': 0, 'code_lines': 0, 'functions': 0},
        'data': {'files': 0, 'lines': 0, 'non_blank_lines': 0},
    }
    for group, path, rel in project_files(root):
        text = read_text(path)
        entry = counted[group]
        entry['files'] += 1
        entry['lines'] += count_lines(text)
        if group == 'data':
            entry['non_blank_lines'] += sum(1 for line in text.splitlines() if line.strip())
        else:
            entry['code_lines'] += _CODE_LINE_COUNTERS[os.path.splitext(rel)[1]](text)
        if is_test_module(rel):
            entry['functions'] += count_test_functions(text)
    return counted


def build_stats(counted, version, cases):
    """Der Inhalt der Datei: ``version``, die drei Gruppen und bei den Tests dazu ``cases`` (``None`` = unbekannt)."""
    return {'version': version, **counted, 'tests': {**counted['tests'], 'cases': cases}}


def dump_stats(stats):
    return json.dumps(stats, indent=2, sort_keys=True, ensure_ascii=False) + '\n'


def write_stats(path, stats):
    """``path`` atomar schreiben: erst ``<Name>.tmp`` daneben, dann ``os.replace``; scheitert es, bleibt die alte Datei, wie sie war."""
    temporary = os.fspath(path) + '.tmp'
    try:
        with open(temporary, 'w', encoding='utf-8', newline='\n') as handle:
            handle.write(dump_stats(stats))
        os.replace(temporary, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.remove(temporary)
        raise


def read_stats_file(path):
    """Der Inhalt der Datei als ``dict``; ``None``, wenn sie fehlt, unlesbar ist oder kein JSON-Objekt enthält."""
    try:
        with open(path, 'rb') as handle:
            data = json.loads(handle.read().decode('utf-8'))
    except (OSError, ValueError, RecursionError):
        return None
    return data if isinstance(data, dict) else None


def _known_cases(path):
    """``tests.cases`` aus der vorhandenen Datei, wenn dort eine ganze Zahl steht, sonst ``None``."""
    data = read_stats_file(path)
    tests = data.get('tests') if data else None
    cases = tests.get('cases') if isinstance(tests, dict) else None
    return cases if type(cases) is int else None


def _check(path, version):
    if not os.path.exists(path):
        print(f'{STATS_FILE} fehlt – bitte python tools/project_stats.py ausführen.', file=sys.stderr)
        return 1
    data = read_stats_file(path)
    if data is None:
        print(f'{STATS_FILE} ist unlesbar oder kein JSON-Objekt – bitte python tools/project_stats.py ausführen.', file=sys.stderr)
        return 1
    if data.get('version') != version:
        print(f'{STATS_FILE} stammt aus Version {data.get("version")!r}, das Projekt ist Version {version} – '
              'bitte python tools/project_stats.py ausführen und die Datei mit committen.', file=sys.stderr)
        return 1
    print(f'{STATS_FILE} gehört zur Version {version}.')
    return 0


def main(argv=None, root=ROOT, path=None, version=APP_VERSION):
    """Das Werkzeug; ``root`` (Projektordner), ``path`` (die Datei, Standard: ``<root>/project_stats.json``) und ``version`` sind
    Parameter, damit die Tests nur unter ``tmp_path`` schreiben. Rückgabe: Rückgabecode des Prozesses."""
    parser = argparse.ArgumentParser(prog='python tools/project_stats.py',
                                     description='Zählt das Projekt und schreibt project_stats.json (Zahlen der About-Seite).')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--check', action='store_true',
                      help='nichts schreiben; Rückgabecode 1, wenn die Datei fehlt, unlesbar ist oder nicht zur Version gehört')
    mode.add_argument('--print', dest='show', action='store_true',
                      help='frisch gezählte Werte ausgeben, ohne zu schreiben (Testfälle aus der vorhandenen Datei)')
    args = parser.parse_args(argv)
    path = os.path.join(root, STATS_FILE) if path is None else path
    if args.check:
        return _check(path, version)
    if args.show:
        print(dump_stats(build_stats(count_project(root), version, _known_cases(path))), end='')
        return 0
    cases = collect_cases(root)
    if cases is None:
        print(f'Die Testfälle ließen sich nicht einsammeln (python -m pytest tests --collect-only) – {STATS_FILE} bleibt unverändert.',
              file=sys.stderr)
        return 1
    write_stats(path, build_stats(count_project(root), version, cases))
    print(f'{STATS_FILE} geschrieben (Version {version}).')
    return 0


if __name__ == '__main__':
    sys.exit(main())
