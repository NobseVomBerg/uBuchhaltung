# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""„Projekt in Zahlen“ auf der About-Seite: das Zählwerkzeug (tools/project_stats.py), das strenge Lesen seiner Datei
(server/project_stats.py) und die Anzeige (``pages.PageAbout``).

Neun Teile:
  1. Python zählen: ohne Leerzeilen, Kommentare und Docstrings (``python_code_lines``),
  2. CSS und .mjs zählen (``css_code_lines``, ``mjs_code_lines``),
  3. die Auswahl der Dateien an einem Temp-Baum (``project_files``, ``count_project``),
  4. Testfunktionen (``count_test_functions``),
  5. Testfälle einsammeln (``collect_cases``, ``subprocess.run`` ersetzt),
  6. ``main``: Schreiben, ``--check``, ``--print`` und der Aufruf als Skript,
  7. die Datei im Repository gehört zur Version,
  8. ``load_stats`` liest streng,
  9. die About-Seite.

``tools/`` ist kein Paket, das Werkzeug wird per Dateipfad geladen. Die Zählfunktionen und ``main`` nehmen Projektordner und Zielpfad als
Parameter: Die Tests schreiben nur unter ``tmp_path``, nie die Datei des Projekts, und zählen nie das echte Projekt neu (seine Zeilen
ändern sich mit jeder Änderung). Dateien, Zeilen und Zahlen sind erfundene Testwerte; eine Uhr kommt nicht vor.
"""
import importlib.util
import json
import os
import re
import subprocess
import sys
import textwrap
from types import SimpleNamespace

import pytest

import version
from server import pages
from server import project_stats as server_stats
from tests.http_client import get

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOL_PATH = os.path.join(ROOT, 'tools', 'project_stats.py')
REAL_FILE = os.path.join(ROOT, 'project_stats.json')


def _load_tool():
    spec = importlib.util.spec_from_file_location('project_stats_tool', TOOL_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


tool = _load_tool()


def _src(text):
    """Quelltext aus einem eingerückten Block in Dreifach-Anführungszeichen: ohne Einrückung und ohne die erste leere Zeile."""
    return textwrap.dedent(text).lstrip('\n')


def _write(path, content):
    """``content`` (Text oder Bytes) nach ``path`` schreiben, fehlende Ordner anlegen."""
    path = os.fspath(path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'wb') as handle:
        handle.write(content if isinstance(content, bytes) else content.encode('utf-8'))


def _read(path):
    with open(path, 'rb') as handle:
        return handle.read()


def _tree(root, files):
    """Legt ``files`` (``{relativer Pfad mit /: Text oder Bytes}``) unter ``root`` an."""
    for rel, content in files.items():
        _write(os.path.join(root, *rel.split('/')), content)


# ── 1. Python zählen ─────────────────────────────────────────────────────────
PYTHON_CASES = [
    pytest.param('', 0, id='leere-Datei'),
    pytest.param('x = 1', 1, id='ohne-Zeilenende-am-Schluss'),
    pytest.param('\n   \n\t\n', 0, id='nur-Leerzeilen'),
    pytest.param('x = 1\n\n   \n\ty = 2\n', 2, id='Leerzeilen-zaehlen-nicht'),
    pytest.param('# a\nx = 1\n    # b\n', 1, id='Kommentarzeilen-zaehlen-nicht'),
    pytest.param('#!/usr/bin/env python3\n# SPDX-License-Identifier: X\n"""Doc."""\nimport os\n', 1, id='Kopfzeilen-und-Modul-Docstring'),
    pytest.param('x = 1  # Hinweis\n', 1, id='Code-mit-angehaengtem-Kommentar-zaehlt'),
    pytest.param('x = "# kein Kommentar"\n', 1, id='Raute-in-einer-Zeichenkette-zaehlt'),
    pytest.param('TEXT = """\n# Ueberschrift\nText\n"""\n', 4, id='Raute-in-mehrzeiliger-Zeichenkette-zaehlt'),
    pytest.param('TEXT = """a\n\nb"""\n', 2, id='Leerzeile-in-einer-Zeichenkette-zaehlt-nicht'),
    pytest.param('x = """a\nb"""  # c\n', 2, id='Kommentar-hinter-mehrzeiliger-Zeichenkette'),
    pytest.param('x = 1 + \\\n    2\n', 2, id='Zeilenfortsetzung'),
    pytest.param('"""Doc.\n\nMehr.\n"""\nx = 1\n', 1, id='Modul-Docstring-mehrzeilig-zaehlt-nicht'),
    pytest.param('"""Doc."""\nx = 1\n', 1, id='Modul-Docstring-einzeilig-zaehlt-nicht'),
    pytest.param(_src('''
        """Modul."""
        class A:
            """Klasse."""
            def f(self):
                """Funktion.

                Mehr.
                """
                return 1
        '''), 3, id='Klassen-und-Funktions-Docstring-zaehlen-nicht'),
    pytest.param('class A:\n    """Zeile 1.\n\n    Zeile 2.\n    """\n    x = 1\n', 2, id='Klassen-Docstring-mehrzeilig-zaehlt-nicht'),
    pytest.param('async def f():\n    """Doc."""\n    return 1\n', 2, id='Docstring-einer-async-Funktion'),
    pytest.param('def f():\n    x = 1\n    """spaeter"""\n    return x\n', 4, id='Zeichenkette-nach-Code-ist-kein-Docstring'),
    pytest.param('x = 1\n"""spaeter"""\n', 2, id='zweite-Zeichenkette-im-Modul-ist-kein-Docstring'),
    pytest.param('def f():\n    f"""{1}"""\n', 2, id='f-String-ist-kein-Docstring'),
    pytest.param('def f(): """Doc."""\n', 1, id='Docstring-in-der-Zeile-der-def-zaehlt-mit'),
    pytest.param('def f():\n    """Doc."""; return 1\n', 2, id='Docstring-vor-Code-in-derselben-Zeile-zaehlt-mit'),
    pytest.param('def f():\n    """Doc."""  # Hinweis\n    return 1\n', 2, id='Docstring-mit-Kommentar-dahinter-zaehlt-nicht'),
    pytest.param('def f():\n    """Größe: ä ö ü."""  # Maß\n    return 1\n', 2, id='Docstring-mit-Umlauten-UTF-8-Spalten'),
    pytest.param('def broken(:\n    # Hinweis\n\n    x = 1\n', 2, id='Syntaxfehler-einfache-Regel'),
    pytest.param('"""Doc."""\nx = (\n', 2, id='Syntaxfehler-Docstring-zaehlt-in-der-einfachen-Regel'),
    pytest.param('x = """offen\n# Zeile\n', 1, id='offene-Zeichenkette-einfache-Regel'),
    pytest.param('x = 1\r\n# c\r\n\r\ny = 2\r\n', 2, id='Windows-Zeilenenden'),
    pytest.param('x = 1\r# c\ry = 2\r', 2, id='einzelnes-CR-als-Zeilenende'),
]


@pytest.mark.parametrize('source, expected', PYTHON_CASES)
def test_python_lines_count_without_blank_lines_comments_and_docstrings(source, expected):
    """Eine Zeile zählt nicht, wenn sie leer ist, nur aus einem Kommentar besteht oder ganz zu einem Docstring gehört; ein ``#`` in einer
    Zeichenkette ist kein Kommentar. Lässt sich die Datei nicht parsen, gilt die einfache Regel – ohne Absturz."""
    assert tool.python_code_lines(source) == expected


def test_a_line_separator_inside_a_string_is_not_a_line_for_the_code_count():
    """U+2028 in einer Zeichenkette trennt für ``str.splitlines()`` (``lines``), nicht für Python: Die Zeilennummern von ``ast`` und
    ``tokenize`` – und damit ``code_lines`` – folgen Python. ``code_lines`` bleibt so nie über ``lines``."""
    text = 'x = "a\u2028b"\ny = 2\n'
    assert tool.count_lines(text) == 3
    assert tool.python_code_lines(text) == 2


def test_line_counts_follow_str_splitlines():
    """``lines`` ist ``len(text.splitlines())``: auch ohne Zeilenende am Schluss, mit Leerzeilen und mit Windows-Zeilenenden."""
    assert [tool.count_lines(text) for text in ('', 'a', 'a\n', 'a\n\n', 'a\r\nb\r\n', '\n')] == [0, 1, 1, 2, 2, 1]


def test_a_python_file_with_invalid_escapes_parses_without_a_warning(recwarn):
    """Ein ungültiges Escape (``"\\d"``) warnt beim Parsen (SyntaxWarning); beim Zählen des ganzen Projekts wäre das Lärm – und mit
    ``-W error`` ein Syntaxfehler, der die Datei in die einfache Regel kippte."""
    assert tool.python_code_lines('import re\nx = re.compile("\\d+")\n# Hinweis\n') == 2
    assert not [w for w in recwarn.list if issubclass(w.category, SyntaxWarning)]


# ── 2. CSS und .mjs zählen ───────────────────────────────────────────────────
CSS_CASES = [
    pytest.param('', 0, id='leere-Datei'),
    pytest.param('a { color: red; }\n\n   \nb { }\n', 2, id='Code-und-Leerzeilen'),
    pytest.param('/* nur Kommentar */\na { }\n', 1, id='Kommentarzeile'),
    pytest.param('/* mehrzeilig\n   weiter\n   Ende */\na { }\n', 1, id='Blockkommentar-ueber-mehrere-Zeilen'),
    pytest.param('a { } /* hinten */\n', 1, id='Code-mit-Kommentar-dahinter-zaehlt'),
    pytest.param('/* vorn */ a { }\n', 1, id='Kommentar-vor-Code-zaehlt-als-Code'),
    pytest.param('a { /* Beginn\n Mitte\n */ color: red; }\n', 2, id='Block-beginnt-und-endet-mit-Code-in-der-Zeile'),
    pytest.param('/* a */ /* b */\n', 0, id='zwei-Blockkommentare-in-einer-Zeile'),
    pytest.param('/* a\nb */ /* c\nd */ x { }\n', 1, id='zweiter-Block-nach-dem-Ende-des-ersten'),
    pytest.param('// in CSS kein Kommentar\na { }\n', 2, id='doppelter-Schraegstrich-ist-in-CSS-Code'),
    pytest.param('a { background: url(//cdn.example/x.png); }\n', 1, id='Adresse-ohne-Protokoll'),
    pytest.param('a::before { content: "/*"; }\nb { }\n', 2, id='Blockbeginn-in-Anfuehrungszeichen-ist-kein-Kommentar'),
    pytest.param("a::before { content: '/*'; }\nb { }\n", 2, id='Blockbeginn-in-einfachen-Anfuehrungszeichen'),
]
MJS_CASES = [
    pytest.param('', 0, id='leere-Datei'),
    pytest.param('// Kommentar\nconst a = 1;\n', 1, id='Zeilenkommentar'),
    pytest.param('    // eingerueckt\nconst a = 1;\n', 1, id='eingerueckter-Zeilenkommentar'),
    pytest.param('const a = 1; // hinten\n', 1, id='Code-mit-Zeilenkommentar-dahinter-zaehlt'),
    pytest.param('/* Block\n   mehrzeilig */\nconst a = 1;\n', 1, id='Blockkommentar-ueber-mehrere-Zeilen'),
    pytest.param('const a = 1; /* Beginn\n Mitte\n Ende */ const b = 2;\n', 2, id='Code-vor-dem-Beginn-und-nach-dem-Ende'),
    pytest.param('/* a */ // b\n', 0, id='Block-und-Zeilenkommentar-in-einer-Zeile'),
    pytest.param('// siehe docs/*.png\nconst a = 1;\n', 1, id='Blockbeginn-in-einem-Zeilenkommentar-oeffnet-keinen-Block'),
    pytest.param('const s = "/*.png";\nconst t = 2;\n', 2, id='Blockbeginn-in-einer-Zeichenkette-oeffnet-keinen-Block'),
    pytest.param("const u = 'http://x.example';\n", 1, id='Adresse-in-einfachen-Anfuehrungszeichen'),
    pytest.param('const u = `http://x.example/${a}`;\n', 1, id='Adresse-in-einer-Vorlage'),
    pytest.param("// don't\nx();\n", 1, id='Apostroph-im-Zeilenkommentar'),
    pytest.param('const s = "a\\"/*";\nnext();\n', 2, id='maskiertes-Anfuehrungszeichen-in-einer-Zeichenkette'),
    pytest.param(_src('''
        const shrink = spawn('python3', ['-c',
          'for f in glob.glob(sys.argv[1]+"/*.png"):\\n' +
          '    pass', OUT]);
        await done();
        '''), 4, id='Zeichenkette-mit-Blockbeginn-wie-in-screenshots-mjs'),
]


@pytest.mark.parametrize('source, expected', CSS_CASES)
def test_css_lines_count_without_blank_lines_and_block_comments(source, expected):
    """Nicht leer und nicht ganz innerhalb eines Blockkommentars; eine Zeile mit Code UND Kommentar zählt als Code. ``//`` ist in CSS kein
    Kommentar, ein ``/*`` in Anführungszeichen öffnet keinen."""
    assert tool.css_code_lines(source) == expected


@pytest.mark.parametrize('source, expected', MJS_CASES)
def test_mjs_lines_count_without_blank_lines_block_and_line_comments(source, expected):
    """Wie CSS, dazu keine Zeile, die nur aus ``//`` bis zum Zeilenende besteht. Ein ``/*`` in einer Zeichenkette (``"/*.png"`` in
    tools/screenshots.mjs) oder in einem ``//``-Kommentar öffnet keinen Block, der die Zeilen danach verschluckte."""
    assert tool.mjs_code_lines(source) == expected


# ── 3. Die Auswahl der Dateien ───────────────────────────────────────────────
SELECTION_FILES = {
    # zählen
    'main.py': 'x = 1\n',
    'buch.css': 'a { }\n',
    'server/app.py': 'y = 2\n',
    'tools/x.mjs': 'const a = 1;\n',
    'tools/z.py': 'z = 3\n',
    'tests/test_a.py': 'def test_a():\n    pass\n',
    'tests/helpers.py': 'def test_not_counted():\n    pass\n',
    'tests/conftest.py': 'import pytest\n',
    'seed_data/a.json': '{}\n',
    'seed_data/test/b.json': '[\n  1\n]\n',
    # zählen nicht: Dateien außerhalb der Gruppen
    'tools/deep/y.mjs': 'const b = 2;\n',
    'other.css': 'a { }\n',
    'docs/notes.json': '{}\n',
    'seed_data/notes.txt': 'x\n',
    'README.md': '# x\n',
    # zählen nicht: ausgeschlossene Ordner
    'seed_data/private/c.json': '{}\n',
    'seed_data/private/x.py': 'x = 1\n',
    'data/d.json': '{}\n',
    'data/e.py': 'x = 1\n',
    '.git/f.py': 'x = 1\n',
    '.claude/g.py': 'x = 1\n',
    '.venv/h.py': 'x = 1\n',
    '.vscode/i.py': 'x = 1\n',
    '.pytest_cache/j.py': 'x = 1\n',
    'node_modules/k.py': 'x = 1\n',
    'license-agpl/l.py': 'x = 1\n',
    'backup/m.py': 'x = 1\n',
    'server/__pycache__/n.py': 'x = 1\n',
    'tests/__pycache__/o.py': 'x = 1\n',
    'nested/data/p.py': 'x = 1\n',
    'nested/deeper/backup/q.py': 'x = 1\n',
}
SELECTED = {
    ('code', 'buch.css'), ('code', 'main.py'), ('code', 'server/app.py'), ('code', 'tools/x.mjs'), ('code', 'tools/z.py'),
    ('tests', 'tests/conftest.py'), ('tests', 'tests/helpers.py'), ('tests', 'tests/test_a.py'),
    ('data', 'seed_data/a.json'), ('data', 'seed_data/test/b.json'),
}


def _selected(root):
    return {(group, rel) for group, _, rel in tool.project_files(root)}


def test_files_are_sorted_into_code_tests_and_data_and_excluded_folders_stay_out(tmp_path):
    """Tests getrennt vom Programm; ``buch.css`` und ``tools/*.mjs`` zählen zum Programm, JSON nur unter ``seed_data/``; nicht gezählt
    werden ``seed_data/private``, ``data``, ``.git``, ``__pycache__`` & Co. (an jeder Stelle des Baums) und alles andere."""
    _tree(tmp_path, SELECTION_FILES)
    assert _selected(tmp_path) == SELECTED


def test_the_project_folder_itself_may_sit_under_an_excluded_name(tmp_path):
    """Die Ausschlüsse gelten für Ordner im Projekt, nicht für den Pfad des Projektordners: Arbeitsbäume liegen unter ``.claude/``, die
    Testdaten unter ``data/``."""
    root = tmp_path / '.claude' / 'worktrees' / 'data' / 'proj'
    _tree(root, {'main.py': 'x = 1\n', 'tests/test_a.py': 'def test_a():\n    pass\n'})
    assert _selected(root) == {('code', 'main.py'), ('tests', 'tests/test_a.py')}


def test_links_are_neither_followed_nor_counted(tmp_path):
    """Ein Link auf eine Datei und einer auf einen Ordner zählen nicht (und der Ordner wird nicht betreten): Sonst stünde eine Datei
    zweimal da – oder eine ganze Fremdablage im Zähler."""
    _tree(tmp_path, {'main.py': 'x = 1\n', 'server/app.py': 'y = 2\n'})
    try:
        os.symlink(tmp_path / 'main.py', tmp_path / 'link.py')
        os.symlink(tmp_path / 'server', tmp_path / 'linked_folder')
    except (OSError, NotImplementedError):
        pytest.skip('Symbolische Links sind hier nicht möglich')
    assert _selected(tmp_path) == {('code', 'main.py'), ('code', 'server/app.py')}


def test_counts_per_group_over_the_selected_files(tmp_path):
    """Dateien, Zeilen und Zeilen ohne Leerzeilen und Kommentare je Gruppe; die Testfunktionen zählen nur in ``tests/test_*.py``
    (``tests/helpers.py`` und ``tests/conftest.py`` zählen bei Dateien und Zeilen mit, nicht bei den Funktionen)."""
    _tree(tmp_path, SELECTION_FILES)
    assert tool.count_project(tmp_path) == {
        'code': {'files': 5, 'lines': 5, 'code_lines': 5},
        'tests': {'files': 3, 'lines': 5, 'code_lines': 5, 'functions': 1},
        'data': {'files': 2, 'lines': 4, 'non_blank_lines': 4},
    }


def test_data_counts_non_blank_lines_separately(tmp_path):
    """Für Daten zählen ``lines`` alle Zeilen und ``non_blank_lines`` die mit Inhalt: Leerzeilen (auch nur Leerraum) nicht."""
    _tree(tmp_path, {'seed_data/a.json': '[\n\n  1,\n   \n  2\n]\n'})
    assert tool.count_project(tmp_path)['data'] == {'files': 1, 'lines': 6, 'non_blank_lines': 4}


def test_a_file_that_is_not_utf8_still_counts(tmp_path):
    """Eine Datei, die sich nicht als UTF-8 lesen lässt, zählt mit ersetzten Zeichen – das Zählen bricht nicht ab. Eine Marke am Anfang
    (BOM) gehört nicht zum Text: Sonst wäre die erste Zeile ein Syntaxfehler."""
    _tree(tmp_path, {'main.py': b'x = "\xff\xfe"\ny = 2\n', 'tools/bom.py': b'\xef\xbb\xbfx = 1\n'})
    assert tool.count_project(tmp_path)['code'] == {'files': 2, 'lines': 3, 'code_lines': 3}
    assert tool.read_text(tmp_path / 'tools' / 'bom.py') == 'x = 1\n'


def test_a_test_module_that_does_not_parse_still_counts_its_lines(tmp_path):
    """Ein Modul mit Syntaxfehler zählt bei den Zeilen mit (einfache Regel), hat aber keine Testfunktionen – das Zählen bricht nicht ab."""
    _tree(tmp_path, {'tests/test_broken.py': 'def test_a(:\n    pass\n'})
    assert tool.count_project(tmp_path)['tests'] == {'files': 1, 'lines': 2, 'code_lines': 2, 'functions': 0}


# ── 4. Testfunktionen ────────────────────────────────────────────────────────
def test_test_functions_at_module_level_and_in_classes_count():
    """Jede ``def test…`` und ``async def test…`` auf Modulebene oder als Methode einer Klasse (auch einer verschachtelten, wie bei
    pytest); Hilfsfunktionen und Methoden ohne ``test`` nicht; eine parametrisierte oder dekorierte Funktion einmal."""
    source = _src('''
        import pytest

        def test_a():
            pass

        async def test_b():
            pass

        def helper():
            pass

        @pytest.mark.parametrize('x', [1, 2, 3])
        def test_c(x):
            pass

        class TestD:
            def test_e(self):
                pass

            async def test_f(self):
                pass

            def not_a_test(self):
                pass

            class TestInner:
                def test_g(self):
                    pass
        ''')
    assert tool.count_test_functions(source) == 6


def test_a_test_function_inside_a_function_does_not_count():
    """Eine Hilfsfunktion ``def test_x`` INNERHALB einer Funktion (oder einer Klasse darin) sammelt pytest nicht ein – gleich, ob die äußere
    Funktion selbst ein Test ist oder eine Hilfsfunktion."""
    source = _src('''
        def test_outer():
            def test_nested():
                pass

            class TestLocal:
                def test_method(self):
                    pass

            return test_nested

        def helper():
            def test_in_helper():
                pass

            class TestInHelper:
                def test_method(self):
                    pass

            return test_in_helper
        ''')
    assert tool.count_test_functions(source) == 1


def test_a_syntax_error_counts_no_test_functions():
    """Ein Modul, das sich nicht parsen lässt, hat für das Werkzeug keine Testfunktionen – kein Absturz."""
    assert tool.count_test_functions('def test_a(:\n    pass\n') == 0


@pytest.mark.parametrize('rel, expected', [
    ('tests/test_a.py', True), ('tests/test_a_b.py', True), ('tests/helpers.py', False), ('tests/conftest.py', False),
    ('tests/test_a.txt', False), ('tools/test_a.py', False), ('test_a.py', False), ('tests/data_test.py', False)])
def test_only_test_modules_in_the_tests_folder_count_functions(rel, expected):
    """Testfunktionen zählen nur in ``tests/test_*.py``: Hilfsmodule (``tests/helpers.py``) und Dateien anderswo nicht."""
    assert tool.is_test_module(rel) is expected


# ── 5. Testfälle einsammeln ──────────────────────────────────────────────────
def _fake_run(monkeypatch, returncode=0, stdout='', error=None):
    """Ersetzt ``subprocess.run``; liefert die Liste der Aufrufe ``(Befehl, Schlüsselwörter)``."""
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        if error is not None:
            raise error
        return SimpleNamespace(returncode=returncode, stdout=stdout, stderr='')

    monkeypatch.setattr(tool.subprocess, 'run', run)
    return calls


@pytest.mark.parametrize('stdout, expected', [
    pytest.param('tests/test_a.py::test_x\n\n123 tests collected in 1.0s\n', 123, id='mehrere'),
    pytest.param('1 test collected in 0.01s\n', 1, id='einer'),
    pytest.param('19193 tests collected in 13.23s', 19193, id='ohne-Zeilenende'),
    pytest.param('x\n\n=== warnings summary ===\n  Unknown config option\n\n-- Docs: https://docs.pytest.org\n7 tests collected in 0.10s\n', 7,
                 id='Warnungen-vor-der-Schlusszeile'),
    pytest.param('2/3 tests collected (1 deselected) in 0.10s\n', 2, id='abgewaehlte-zaehlen-nicht'),
    pytest.param('tests/test_a.py::test_a[3 tests collected]\n5 tests collected in 0.1s\n', 5, id='die-letzte-Zeile-gilt'),
])
def test_collect_cases_reads_the_last_line_of_pytest(monkeypatch, tmp_path, stdout, expected):
    """Die Schlusszeile von ``pytest --collect-only -q`` nennt die Zahl der Testfälle (``N tests collected``, bei abgewählten Fällen
    ``A/B tests collected``: ``A`` läuft); es gilt die letzte passende Zeile, nicht ein Knotenname davor."""
    _fake_run(monkeypatch, 0, stdout)
    assert tool.collect_cases(tmp_path) == expected


@pytest.mark.parametrize('kwargs', [
    pytest.param({'returncode': 2, 'stdout': '1 test collected, 1 error in 0.1s\n'}, id='Rueckgabecode-2-Sammelfehler'),
    pytest.param({'returncode': 1, 'stdout': '5 tests collected in 0.1s\n'}, id='Rueckgabecode-1'),
    pytest.param({'returncode': 5, 'stdout': 'no tests collected in 0.01s\n'}, id='Rueckgabecode-5-nichts-gesammelt'),
    pytest.param({'returncode': 0, 'stdout': ''}, id='ohne-Ausgabe'),
    pytest.param({'returncode': 0, 'stdout': 'tests/test_a.py::test_x\n'}, id='ohne-Schlusszeile'),
    pytest.param({'error': subprocess.TimeoutExpired('pytest', 600)}, id='Zeitueberschreitung'),
    pytest.param({'error': FileNotFoundError('python')}, id='Interpreter-nicht-startbar'),
    pytest.param({'error': subprocess.SubprocessError('kaputt')}, id='anderer-Fehler-des-Prozesses'),
])
def test_collect_cases_is_none_when_collecting_did_not_work(monkeypatch, tmp_path, kwargs):
    """Rückgabecode ≠ 0 (auch bei einer sonst lesbaren Schlusszeile), keine Schlusszeile, Zeitüberschreitung oder ein Prozess, der nicht
    startet: ``None`` – das Werkzeug schreibt dann nichts, statt eine falsche Zahl festzuhalten."""
    _fake_run(monkeypatch, **kwargs)
    assert tool.collect_cases(tmp_path) is None


def test_collect_cases_runs_pytest_in_the_project_folder_with_a_time_limit(monkeypatch, tmp_path):
    """Der Aufruf: derselbe Interpreter, ``tests`` einsammeln ohne Cache-Ordner, im Projektordner, Ausgabe erfasst, Zeitlimit 600 s."""
    calls = _fake_run(monkeypatch, 0, '3 tests collected in 0.1s\n')
    assert tool.collect_cases(tmp_path) == 3
    (command, kwargs), = calls
    assert command == [sys.executable, '-m', 'pytest', 'tests', '--collect-only', '-q', '-p', 'no:cacheprovider']
    assert kwargs['cwd'] == tmp_path and kwargs['timeout'] == 600 and kwargs['capture_output'] is True


# ── 6. main: Schreiben, --check, --print ─────────────────────────────────────
SMALL_PROJECT = {
    'app.py': '"""Doc."""\nx = 1\n',
    'buch.css': 'a { }\n',
    'tests/test_a.py': 'def test_a():\n    pass\n\n\ndef test_b():\n    pass\n',
    'seed_data/a.json': '{}\n',
}
SMALL_STATS = {
    'version': '9.9.9',
    'code': {'files': 2, 'lines': 3, 'code_lines': 2},
    'tests': {'files': 1, 'lines': 6, 'code_lines': 4, 'functions': 2, 'cases': 42},
    'data': {'files': 1, 'lines': 1, 'non_blank_lines': 1},
}


@pytest.fixture
def small(tmp_path):
    """Ein kleines Projekt unter ``tmp_path/proj`` und der Zielpfad der Datei in ``tmp_path/out`` (der Ordner besteht schon);
    liefert ``(Projektordner, Zielpfad)``."""
    root = tmp_path / 'proj'
    _tree(root, SMALL_PROJECT)
    (tmp_path / 'out').mkdir()
    return str(root), str(tmp_path / 'out' / 'project_stats.json')


def _run_main(small, monkeypatch, *args, cases=42):
    """``main`` auf dem kleinen Projekt, Version ``9.9.9``; das Einsammeln ist durch ``cases`` ersetzt (``None`` = es scheitert)."""
    root, target = small
    monkeypatch.setattr(tool, 'collect_cases', lambda folder: cases)
    return tool.main(list(args), root=root, path=target, version='9.9.9')


def _stats_file(path, stats_version='9.9.9', cases=42):
    """Schreibt eine Datei wie das Werkzeug sie schreibt (für das kleine Projekt)."""
    data = json.loads(json.dumps(SMALL_STATS))
    data['version'], data['tests']['cases'] = stats_version, cases
    _write(path, tool.dump_stats(data))
    return data


def test_main_writes_the_counted_values_as_sorted_json_with_a_trailing_newline(small, monkeypatch, capsys):
    """Ohne Option zählt es, sammelt die Testfälle ein und schreibt die Datei: genau die Schlüssel des Formats, sortiert, mit
    Zeilenende, mit ``json.dumps(…, indent=2, sort_keys=True, ensure_ascii=False)``."""
    root, target = small
    assert _run_main(small, monkeypatch) == 0
    text = _read(target).decode('utf-8')
    assert json.loads(text) == SMALL_STATS
    assert text == json.dumps(SMALL_STATS, indent=2, sort_keys=True, ensure_ascii=False) + '\n'
    assert list(json.loads(text)) == ['code', 'data', 'tests', 'version']
    assert 'geschrieben' in capsys.readouterr().out


def test_main_writes_no_date_path_or_host_name(small, monkeypatch):
    """Die Datei soll sich nur ändern, wenn sich das Projekt ändert: keine Zeit, keine Pfade, keine Namen – nur die Version und Zahlen."""
    root, target = small
    assert _run_main(small, monkeypatch) == 0
    text = _read(target).decode('utf-8')
    assert not re.search(r'[0-9]{4}-[0-9]{2}-[0-9]{2}|[0-9]{2}:[0-9]{2}', text)
    assert os.path.dirname(root) not in text and '/' not in text and '\\' not in text
    assert set(re.findall(r'"([a-z_]+)"', text)) == {'version', 'code', 'tests', 'data', 'files', 'lines', 'code_lines', 'functions',
                                                      'cases', 'non_blank_lines'}


def test_main_writes_atomically_and_leaves_no_temporary_file(small, monkeypatch):
    """Geschrieben wird erst in ``<Datei>.tmp`` und dann umbenannt: Eine alte Datei wird ersetzt, nichts bleibt liegen."""
    root, target = small
    _write(target, 'alt')
    assert _run_main(small, monkeypatch) == 0
    assert json.loads(_read(target)) == SMALL_STATS
    assert os.listdir(os.path.dirname(target)) == ['project_stats.json']


def test_main_keeps_the_old_file_when_writing_fails(small, monkeypatch):
    """Scheitert das Schreiben, bleibt die alte Datei, wie sie war, und die Teildatei wird entfernt."""
    root, target = small
    _write(target, 'alt')

    def full_disk(*args, **kwargs):
        raise OSError('voll')

    monkeypatch.setattr(tool.os, 'replace', full_disk)
    with pytest.raises(OSError):
        _run_main(small, monkeypatch)
    assert _read(target) == b'alt'
    assert os.listdir(os.path.dirname(target)) == ['project_stats.json']


def test_main_without_collected_cases_stops_with_code_1_and_writes_nothing(small, monkeypatch, capsys):
    """Scheitert das Einsammeln der Testfälle, bricht es mit Rückgabecode 1 und einem Klartext ab – ohne Datei, und eine vorhandene bleibt
    unverändert (keine halbe Zahl)."""
    root, target = small
    assert _run_main(small, monkeypatch, cases=None) == 1
    assert not os.path.exists(target)
    captured = capsys.readouterr()
    assert 'einsammeln' in captured.err and captured.out == ''
    _write(target, 'alt')
    assert _run_main(small, monkeypatch, cases=None) == 1
    assert _read(target) == b'alt'


def test_the_tool_writes_exactly_what_the_page_reads(small, monkeypatch):
    """Werkzeug und Seite teilen das Format: Was ``main`` schreibt, liest ``load_stats`` streng und unverändert."""
    root, target = small
    assert _run_main(small, monkeypatch) == 0
    assert server_stats.load_stats(target) == SMALL_STATS


def test_check_passes_when_the_file_belongs_to_the_version(small, monkeypatch, capsys):
    """``--check`` schreibt nichts und gibt 0 zurück, wenn ``version`` in der Datei die des Projekts ist."""
    root, target = small
    _stats_file(target)
    before = _read(target)
    assert _run_main(small, monkeypatch, '--check') == 0
    assert _read(target) == before
    assert capsys.readouterr().err == ''


@pytest.mark.parametrize('stored, fragment', [
    pytest.param(None, 'fehlt', id='fehlt'),
    pytest.param(b'kein json', 'unlesbar', id='kein-JSON'),
    pytest.param(b'[]', 'unlesbar', id='Liste-statt-Objekt'),
    pytest.param(b'\xff\xfe', 'unlesbar', id='kein-UTF-8'),
    pytest.param(b'{"code": {}}', 'Version None', id='ohne-Version'),
    pytest.param(b'{"version": "2.6.0"}', "'2.6.0'", id='andere-Version'),
])
def test_check_fails_with_code_1_and_changes_nothing(small, monkeypatch, capsys, stored, fragment):
    """``--check``: Rückgabecode 1 mit Klartext, wenn die Datei fehlt, ungültig ist oder nicht zur Version gehört; geschrieben wird nichts
    (auch wird eine fehlende Datei nicht angelegt)."""
    root, target = small
    if stored is not None:
        _write(target, stored)
    assert _run_main(small, monkeypatch, '--check') == 1
    assert fragment in capsys.readouterr().err
    if stored is None:
        assert not os.path.exists(target)
    else:
        assert _read(target) == stored


def test_check_and_print_never_collect_the_test_cases(small, monkeypatch):
    """``--check`` und ``--print`` sind schnell: Sie rufen das Einsammeln (ein Kindprozess mit pytest) nie auf."""
    root, target = small
    _stats_file(target)

    def collecting_is_forbidden(folder):
        raise AssertionError('collect_cases darf nicht laufen')

    monkeypatch.setattr(tool, 'collect_cases', collecting_is_forbidden)
    assert tool.main(['--check'], root=root, path=target, version='9.9.9') == 0
    assert tool.main(['--print'], root=root, path=target, version='9.9.9') == 0


def test_print_shows_fresh_counts_with_the_cases_of_the_existing_file(small, monkeypatch, capsys):
    """``--print`` zählt frisch, schreibt nichts und nimmt ``cases`` aus der vorhandenen Datei (ohne Einsammeln)."""
    root, target = small
    _stats_file(target, stats_version='1.0.0', cases=777)
    before = _read(target)
    assert _run_main(small, monkeypatch, '--print') == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown == {**SMALL_STATS, 'tests': {**SMALL_STATS['tests'], 'cases': 777}}      # Version des Projekts, nicht der Datei
    assert _read(target) == before


@pytest.mark.parametrize('stored', [
    pytest.param(None, id='ohne-Datei'),
    pytest.param(b'kaputt', id='kaputte-Datei'),
    pytest.param(b'{"tests": {"cases": "viele"}}', id='cases-ist-keine-Zahl'),
    pytest.param(b'{"tests": {"cases": true}}', id='cases-ist-ein-Wahrheitswert'),
    pytest.param(b'{"tests": []}', id='tests-ist-keine-Gruppe'),
])
def test_print_shows_null_for_the_cases_it_cannot_know(small, monkeypatch, capsys, stored):
    """Ohne brauchbare Datei gibt ``--print`` für ``cases`` ``null`` aus (und schreibt nichts)."""
    root, target = small
    if stored is not None:
        _write(target, stored)
    assert _run_main(small, monkeypatch, '--print') == 0
    assert json.loads(capsys.readouterr().out)['tests']['cases'] is None
    if stored is None:
        assert not os.path.exists(target)
    else:
        assert _read(target) == stored


def test_check_and_print_exclude_each_other(small, monkeypatch, capsys):
    """Eine Betriebsart je Aufruf: ``--check`` zusammen mit ``--print`` ist ein Fehler der Aufrufzeile (Rückgabecode 2, nichts geschrieben)."""
    root, target = small
    monkeypatch.setattr(tool, 'collect_cases', lambda folder: 42)
    with pytest.raises(SystemExit) as stop:
        tool.main(['--check', '--print'], root=root, path=target, version='9.9.9')
    assert stop.value.code == 2 and not os.path.exists(target)
    assert '--print' in capsys.readouterr().err


def test_the_script_runs_from_any_folder(tmp_path):
    """Als Skript aus einem fremden Arbeitsverzeichnis gestartet findet es Projekt, ``version.py`` und Datei über seinen eigenen Ort
    (nicht über den Arbeitsordner); ``--check`` gelingt, solange die Datei im Projekt zur Version gehört."""
    done = subprocess.run([sys.executable, TOOL_PATH, '--check'], cwd=tmp_path, capture_output=True, encoding='utf-8', timeout=60)
    assert (done.returncode, done.stderr) == (0, '')
    assert version.APP_VERSION in done.stdout


# ── 7. Die Datei im Repository ───────────────────────────────────────────────
def test_the_file_in_the_repository_belongs_to_the_version():
    """``project_stats.json`` liegt im Projekt, die Seite liest sie, und sie stammt aus der Version des Programms: Wer die Version hebt,
    führt ``python tools/project_stats.py`` aus und committet die Datei mit (CLAUDE.md, Abschnitt Versionierung). Verglichen wird nur
    die Version und die Größenordnung – frisch gezählte Zeilen wären nach jeder Änderung anders."""
    assert os.path.isfile(REAL_FILE)
    stats = server_stats.load_stats()
    assert stats is not None
    assert stats['version'] == version.APP_VERSION
    assert stats['code']['files'] > 50
    assert stats['tests']['functions'] > 1000
    assert stats['tests']['cases'] >= stats['tests']['functions']


def test_the_file_in_the_repository_is_what_the_tool_writes():
    """Keine Handarbeit an der Datei: Sie ist genau das, was ``dump_stats`` aus ihrem Inhalt macht (Schlüssel sortiert, zwei Leerzeichen,
    Zeilenende) – sonst entstünde bei jedem Lauf des Werkzeugs ein Unterschied ohne Grund."""
    text = _read(REAL_FILE).decode('utf-8')
    assert tool.dump_stats(json.loads(text)) == text


# ── 8. load_stats liest streng ───────────────────────────────────────────────
DELETE = object()


def _valid():
    return {'version': '2.6.1',
            'code': {'files': 1, 'lines': 2, 'code_lines': 3},
            'tests': {'files': 4, 'lines': 5, 'code_lines': 6, 'functions': 7, 'cases': 8},
            'data': {'files': 9, 'lines': 10, 'non_blank_lines': 11}}


def _with(**changes):
    """Eine gültige Datei, in der ``Gruppe__Schlüssel`` (oder ein Name der obersten Ebene) durch einen Wert ersetzt oder – bei
    ``DELETE`` – entfernt ist."""
    data = _valid()
    for name, value in changes.items():
        section, _, key = name.partition('__')
        target, key = (data[section], key) if key else (data, section)
        if value is DELETE:
            del target[key]
        else:
            target[key] = value
    return data


def _put(tmp_path, content):
    """Schreibt ``content`` (Bytes oder Daten für JSON) nach ``tmp_path/stats.json``; liefert den Pfad."""
    path = str(tmp_path / 'stats.json')
    _write(path, content if isinstance(content, bytes) else json.dumps(content))
    return path


def test_a_valid_file_is_read_as_it_is(tmp_path):
    """Eine gültige Datei ergibt genau ihre Werte."""
    assert server_stats.load_stats(_put(tmp_path, _valid())) == _valid()


@pytest.mark.parametrize('content', [
    pytest.param(b'', id='leer'),
    pytest.param(b'kein json', id='kein-JSON'),
    pytest.param(b'[]', id='Liste-statt-Objekt'),
    pytest.param(b'"text"', id='Text-statt-Objekt'),
    pytest.param(b'null', id='null'),
    pytest.param(b'\xff\xfe{}', id='kein-UTF-8'),
    pytest.param(b'{"version": "1"', id='abgeschnitten'),
    pytest.param(b'[' * 30000 + b']' * 30000, id='zu-tief-verschachtelt'),
    pytest.param(_with(code__files='5'), id='Zahl-als-Text'),
    pytest.param(_with(code__files=True), id='Wahrheitswert-statt-Zahl'),
    pytest.param(_with(code__files=False), id='falsch-statt-Zahl'),
    pytest.param(_with(tests__cases=1.0), id='Kommazahl'),
    pytest.param(_with(tests__cases=None), id='null-statt-Zahl'),
    pytest.param(_with(data__lines=-1), id='negative-Zahl'),
    pytest.param(_with(data__lines=1_000_000_001), id='Zahl-ueber-der-Grenze'),
    pytest.param(_with(code=[1, 2, 3]), id='Gruppe-ist-keine-Gruppe'),
    pytest.param(_with(code=DELETE), id='Gruppe-fehlt'),
    pytest.param(_with(data=DELETE), id='andere-Gruppe-fehlt'),
    pytest.param(_with(tests__cases=DELETE), id='Schluessel-fehlt'),
    pytest.param(_with(code__code_lines=DELETE), id='anderer-Schluessel-fehlt'),
    pytest.param(_with(version=DELETE), id='Version-fehlt'),
    pytest.param(_with(version=261), id='Version-ist-eine-Zahl'),
    pytest.param(_with(version=None), id='Version-ist-null'),
    pytest.param(_with(version=''), id='Version-leer'),
    pytest.param(_with(version='<script>alert(1)</script>'), id='Version-mit-Script'),
    pytest.param(_with(version='2.6.1"onload="x'), id='Version-mit-Anfuehrungszeichen'),
    pytest.param(_with(version='2.6.1 '), id='Version-mit-Leerzeichen'),
    pytest.param(_with(version='2.6.1\n'), id='Version-mit-Zeilenumbruch'),
    pytest.param(_with(version='V2.6.1ä'), id='Version-mit-Umlaut'),
    pytest.param(_with(version='1' * 21), id='Version-zu-lang'),
])
def test_load_stats_refuses_the_whole_file_for_anything_unusual(tmp_path, content):
    """Kein JSON-Objekt, ein Text oder ``bool`` oder eine Kommazahl statt einer ganzen Zahl, eine negative Zahl oder eine über der Grenze,
    ein fehlender Schlüssel oder eine fehlende Gruppe, eine Version mit Tags oder Anführungszeichen: ``None`` für die ganze Datei."""
    assert server_stats.load_stats(_put(tmp_path, content)) is None


def test_load_stats_accepts_the_limits(tmp_path):
    """Null und die Grenze selbst sind gültige Zahlen, eine Version aus 20 Zeichen von ``[0-9A-Za-z.+-]`` ebenso."""
    data = _with(code__files=0, tests__cases=server_stats.MAX_NUMBER, version='2.6.1-rc.1+build.5')
    assert server_stats.load_stats(_put(tmp_path, data)) == data
    assert server_stats.load_stats(_put(tmp_path, _with(version='1' * 20)))['version'] == '1' * 20


def test_load_stats_ignores_additional_keys_and_does_not_pass_them_on(tmp_path):
    """Zusätzliche Schlüssel (auch in einer Gruppe) werden überhört: Das Ergebnis trägt nur die bekannten, geprüften."""
    data = _valid()
    data['extra'] = '<b>'
    data['code']['extra'] = 'x'
    assert server_stats.load_stats(_put(tmp_path, data)) == _valid()


def test_load_stats_allows_at_most_64_kilobytes(tmp_path):
    """Höchstens 64 KB: Eine gültige Datei von genau 65536 Bytes wird gelesen, ein Byte mehr ergibt ``None``."""
    text = json.dumps(_valid())
    exact = (text + ' ' * (server_stats.MAX_BYTES - len(text.encode('utf-8')))).encode('utf-8')
    assert len(exact) == 64 * 1024
    assert server_stats.load_stats(_put(tmp_path, exact)) == _valid()
    assert server_stats.load_stats(_put(tmp_path, exact + b' ')) is None


def test_load_stats_is_none_for_a_missing_file_and_for_a_folder(tmp_path):
    """Eine fehlende Datei und ein Ordner statt der Datei sind kein Fehler der Seite, sondern ``None``."""
    assert server_stats.load_stats(str(tmp_path / 'fehlt.json')) is None
    assert server_stats.load_stats(str(tmp_path)) is None


def test_load_stats_reads_the_file_next_to_version_py():
    """Der Standardort ist der Ordner von ``version.py`` – dort, wo ``tools/project_stats.py`` die Datei hinschreibt."""
    assert server_stats.STATS_PATH == os.path.join(os.path.dirname(os.path.abspath(version.__file__)), 'project_stats.json')
    assert os.path.samefile(server_stats.STATS_PATH, REAL_FILE)


def test_the_server_finds_the_file_from_any_working_folder(tmp_path):
    """Ein frisch gestarteter Prozess mit einem anderen Arbeitsverzeichnis findet die Datei trotzdem: Der Ort kommt von ``version.py``,
    nicht vom Arbeitsordner (der Server läuft auch aus einem anderen – ein Test im selben Prozess sähe das nicht, die Konstante steht
    schon seit dem Import fest)."""
    code = 'import sys; sys.path.insert(0, sys.argv[1]); from server import project_stats; print(project_stats.load_stats() is not None)'
    done = subprocess.run([sys.executable, '-c', code, ROOT], cwd=tmp_path, capture_output=True, encoding='utf-8', timeout=120)
    assert (done.returncode, done.stdout.strip()) == (0, 'True'), done.stderr[-500:]


@pytest.mark.parametrize('number, expected', [
    (0, '0'), (7, '7'), (999, '999'), (1000, '1.000'), (12345, '12.345'), (1234567, '1.234.567'), (1_000_000_000, '1.000.000.000')])
def test_format_count_uses_the_thousands_point(number, expected):
    """Zahlen stehen deutsch mit Punkt je drei Stellen: ``1.234.567`` (``fmt_eur`` ist für Beträge und passt hier nicht)."""
    assert server_stats.format_count(number) == expected


# ── 9. Die About-Seite ───────────────────────────────────────────────────────
ABOUT_STATS = {'version': version.APP_VERSION,
               'code': {'files': 412, 'lines': 98765, 'code_lines': 80000},
               'tests': {'files': 590, 'lines': 1234567, 'code_lines': 200000, 'functions': 7500, 'cases': 19193},
               'data': {'files': 30, 'lines': 12000, 'non_blank_lines': 11000}}


def _point_the_page_at(monkeypatch, tmp_path, content):
    """Die Seite liest ``content`` (Bytes, Daten für JSON oder ``None`` = keine Datei) statt der Datei des Projekts."""
    path = str(tmp_path / 'about_stats.json')
    if content is None:
        if os.path.exists(path):
            os.remove(path)
    else:
        _write(path, content if isinstance(content, bytes) else json.dumps(content))
    monkeypatch.setattr(server_stats, 'STATS_PATH', path)


def test_about_shows_the_figures_with_thousands_points(monkeypatch, tmp_path):
    """Mit Datei steht „Projekt in Zahlen“ unter dem Text: Programmcode, Tests, Testfunktionen und -fälle, Beispieldaten – Zahlen mit
    Tausenderpunkt – und darunter der Hinweis auf Stand und Werkzeug."""
    _point_the_page_at(monkeypatch, tmp_path, ABOUT_STATS)
    html = pages.PageAbout()
    assert '<h2>Projekt in Zahlen</h2>' in html
    assert html.index('Lizenz: ') < html.index('Projekt in Zahlen') < html.index('app_confirm')       # unter dem Text, vor dem Fuß
    assert '<td>Programmcode</td><td>412 Dateien &middot; 98.765 Zeilen &middot; davon 80.000 ohne Leerzeilen und Kommentare</td>' in html
    assert '<td>Tests</td><td>590 Dateien &middot; 1.234.567 Zeilen &middot; davon 200.000 ohne Leerzeilen und Kommentare</td>' in html
    assert '<td>7.500 Testfunktionen &middot; 19.193 Testfälle</td>' in html
    assert '<td>Seed- und Beispieldaten</td><td>30 Dateien &middot; 12.000 Zeilen</td>' in html
    assert (f'<p class="hint">Stand: Version {version.APP_VERSION}. '
            'Gezählt mit tools/project_stats.py; Docstrings zählen als Kommentar.</p>') in html


def test_about_uses_existing_classes_and_no_inline_style(monkeypatch, tmp_path):
    """Kein neues CSS: Der Abschnitt nutzt ``grid1Col700``, ``reportTable`` und ``hint`` aus ``buch.css`` und setzt weder ``style`` noch ein
    Skript."""
    _point_the_page_at(monkeypatch, tmp_path, ABOUT_STATS)
    html = pages.PageAbout()
    start = html.index('<br><br><div class="grid1Col700">')
    section = html[start:html.index('</p></div>', start)]
    assert 'style=' not in section and '<script' not in section
    css = _read(os.path.join(ROOT, 'buch.css')).decode('utf-8')
    for name in ('grid1Col700', 'reportTable', 'hint'):
        assert f'.{name}' in css
        assert f'class="{name}"' in section


def test_about_escapes_every_value_from_the_file(monkeypatch):
    """Jeder Wert aus der Datei geht durch ``html.escape`` – auch die Version. Das Lesen lässt so etwas gar nicht durch; hier ersetzt ein
    Ersatz für ``load_stats`` die Datei: Die Seite verlässt sich nicht allein darauf."""
    hostile = {**ABOUT_STATS, 'version': '<b onclick="x">9</b> & "'}
    monkeypatch.setattr(server_stats, 'load_stats', lambda path=None: hostile)
    html = pages.PageAbout()
    assert 'Die Zahlen stammen aus Version &lt;b onclick=&quot;x&quot;&gt;9&lt;/b&gt; &amp; &quot;.' in html
    assert '<b onclick' not in html


def test_about_names_the_version_the_figures_come_from_when_it_differs(monkeypatch, tmp_path):
    """Gehört die Datei nicht zur Version des Programms, sagt der Hinweis es – statt „Stand: Version …“ steht „Die Zahlen stammen aus
    Version …“; die Zahlen selbst stehen weiter da."""
    _point_the_page_at(monkeypatch, tmp_path, {**ABOUT_STATS, 'version': '0.0.1'})
    html = pages.PageAbout()
    assert 'Die Zahlen stammen aus Version 0.0.1. Gezählt mit tools/project_stats.py; Docstrings zählen als Kommentar.' in html
    assert 'Stand: Version' not in html
    assert '98.765 Zeilen' in html


@pytest.mark.parametrize('content', [
    pytest.param(None, id='ohne-Datei'),
    pytest.param(b'kein json', id='kein-JSON'),
    pytest.param({**ABOUT_STATS, 'version': '<script>'}, id='ungueltige-Version'),
    pytest.param({**ABOUT_STATS, 'code': {'files': 1}}, id='Schluessel-fehlt'),
])
def test_about_works_without_a_usable_file_and_leaves_the_section_out(monkeypatch, tmp_path, content):
    """Fehlt die Datei oder ist sie nicht streng genug, kommt die Seite trotzdem – ohne den Abschnitt, mit allem anderen."""
    _point_the_page_at(monkeypatch, tmp_path, content)
    html = pages.PageAbout()
    assert 'Projekt in Zahlen' not in html and 'project_stats' not in html
    assert 'Version ' + version.APP_VERSION in html and 'licenseContact' in html
    assert html.rstrip().endswith('</html>')


def test_about_answers_200_over_http_with_the_section(server_port, auto_login):
    """Über HTTP (echter Server): ``/about`` antwortet 200 und zeigt mit der Datei des Projekts den Abschnitt „Projekt in Zahlen“."""
    page = get(server_port, '/about')
    assert page.status == 200
    assert 'Projekt in Zahlen' in page.text and 'Testfunktionen' in page.text
    assert f'Stand: Version {version.APP_VERSION}.' in page.text


def test_about_answers_200_over_http_without_the_file(server_port, auto_login, monkeypatch, tmp_path):
    """Fehlt die Datei, antwortet ``/about`` trotzdem 200 – ohne den Abschnitt."""
    _point_the_page_at(monkeypatch, tmp_path, None)
    page = get(server_port, '/about')
    assert page.status == 200 and 'Projekt in Zahlen' not in page.text and 'uBuchhaltung' in page.text


def test_about_over_http_reads_the_file_on_every_request(server_port, auto_login, monkeypatch, tmp_path):
    """Nichts wird beim Start gezählt oder zwischengespeichert: Der Server liest die Datei bei jeder Anfrage, eine geänderte Datei zeigt
    die Seite beim nächsten Aufruf."""
    _point_the_page_at(monkeypatch, tmp_path, ABOUT_STATS)
    assert '1.234.567 Zeilen' in get(server_port, '/about').text
    _point_the_page_at(monkeypatch, tmp_path, {**ABOUT_STATS, 'tests': {**ABOUT_STATS['tests'], 'lines': 7654321}})
    assert '7.654.321 Zeilen' in get(server_port, '/about').text
