# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Wächter für die Testsuite selbst: kein „heute“ beim Import.

Die Suite läuft Minuten (in der CI 15 bis 25). Was beim Import eines Testmoduls aus der Uhr gelesen wird – eine
Modulkonstante wie ``TODAY = date.today()``, ein Wert in einer ``parametrize``-Liste, ein Standardwert, ein
Klassenattribut –, ist beim Lauf des Tests veraltet, sobald die Suite über Mitternacht läuft: Die App rechnet mit dem
neuen Tag, der Test mit dem alten (Release-Lauf 2.3.4: 19 Tests, etwa die Dateinamen der Fassungen in
test_invoice_versions.py). Ebenso hält eine Fixture mit größerem Geltungsbereich als ``function``, die die Uhr liest,
den Tag über mehrere Tests hinweg fest.

Dieser Test sucht solche Stellen statisch (``ast``) in allen Modulen unter ``tests/``. Ein „heute“ gehört in den Test
oder in eine Fixture je Test: als Funktion, die bei jedem Aufruf frisch liest (``today()``), nie als Konstante. Auch
eine Funktion, die die Uhr liest, darf beim Import nicht aufgerufen werden (Modulebene, Dekorator, Standardwert,
``parametrize``) – bei ``parametrize`` stattdessen einen Namen oder Abstand als Wert nehmen und das Datum im Test bilden.

Alle Namen und Daten sind erfundene Testwerte.
"""
import ast
import pathlib

TESTS = pathlib.Path(__file__).resolve().parent

CLOCK_METHODS = frozenset({'today', 'now', 'utcnow'})                    # date.today(), datetime.now(), …
TIME_NOW = frozenset({'time', 'localtime', 'gmtime', 'ctime', 'asctime'})    # time.<Name>() ohne Argument: jetzt


def _is_clock_call(call, readers):
    """Liest dieser Aufruf die Uhr – selbst oder über eine Funktion aus ``readers`` (Namen, die sie lesen)?"""
    func = call.func
    if isinstance(func, ast.Attribute):
        if func.attr in CLOCK_METHODS:
            return True
        if isinstance(func.value, ast.Name) and func.value.id == 'time':
            return (func.attr in TIME_NOW and not call.args) or (func.attr == 'strftime' and len(call.args) == 1)
        return False
    return isinstance(func, ast.Name) and func.id in readers


class _ImportTime(ast.NodeVisitor):
    """Besucht, was beim Import ausgeführt wird: Modul- und Klassenkörper samt Dekoratoren, Basisklassen und
    Standardwerten – nicht die Körper von Funktionen und Lambdas, die laufen erst beim Aufruf."""

    def __init__(self, readers):
        self.readers = readers
        self.found = []

    def visit_Call(self, node):
        if _is_clock_call(node, self.readers):
            self.found.append((node.lineno, f'{ast.unparse(node)} wird beim Import ausgeführt'))
        self.generic_visit(node)

    def _defaults(self, args):
        for default in [*args.defaults, *(d for d in args.kw_defaults if d is not None)]:
            self.visit(default)

    def visit_FunctionDef(self, node):
        for decorator in node.decorator_list:
            self.visit(decorator)
        self._defaults(node.args)

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Lambda(self, node):
        self._defaults(node.args)


def _fixture_scope(function):
    """Der Geltungsbereich einer ``@pytest.fixture(scope=…)``-Fixture, wenn er größer ist als ``function``."""
    for decorator in function.decorator_list:
        if not isinstance(decorator, ast.Call):
            continue
        name = decorator.func.attr if isinstance(decorator.func, ast.Attribute) else getattr(decorator.func, 'id', '')
        if name != 'fixture':
            continue
        for keyword in decorator.keywords:
            if (keyword.arg == 'scope' and isinstance(keyword.value, ast.Constant)
                    and keyword.value.value != 'function'):
                return keyword.value.value
    return None


def _defined_functions(tree):
    return {node.name: node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}


def _imported_names(tree):
    """``{lokaler Name: (Modul unter tests/, Name dort)}`` für ``from tests.<Modul> import …`` auf Modulebene."""
    names = {}
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.level == 0 and (node.module or '').startswith('tests.'):
            for alias in node.names:
                names[alias.asname or alias.name] = (node.module.split('.', 1)[1], alias.name)
    return names


def _reader_names(trees):
    """Je Modul die Namen der Funktionen, die die Uhr lesen – auch mittelbar über andere solcher Funktionen und über
    ``from tests.<Modul> import …`` (Fixpunkt: so lange, bis nichts mehr dazukommt)."""
    readers = {module: set() for module in trees}
    functions = {module: _defined_functions(tree) for module, tree in trees.items()}
    imported = {module: _imported_names(tree) for module, tree in trees.items()}
    changed = True
    while changed:
        changed = False
        for module in trees:
            for local, (source, original) in imported[module].items():
                if original in readers.get(source, ()) and local not in readers[module]:
                    readers[module].add(local)
                    changed = True
            for name, function in functions[module].items():
                if name not in readers[module] and any(
                        isinstance(call, ast.Call) and _is_clock_call(call, readers[module])
                        for call in ast.walk(function)):
                    readers[module].add(name)
                    changed = True
    return readers


def clock_reads_at_import(sources):
    """Stellen, die beim Import die Uhr lesen oder eine Fixture mit größerem Geltungsbereich als ``function`` die Uhr
    lesen lassen. ``sources``: ``{Modulname: Quelltext}``; Ergebnis: ``{Modulname: [(Zeile, Beschreibung), …]}``,
    nur Module mit Fund."""
    trees = {module: ast.parse(text) for module, text in sources.items()}
    readers = _reader_names(trees)
    result = {}
    for module, tree in trees.items():
        walker = _ImportTime(readers[module])
        walker.visit(tree)
        found = list(walker.found)
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and (scope := _fixture_scope(node)):
                found += [(call.lineno, f'Fixture {node.name} (scope={scope!r}) liest die Uhr: {ast.unparse(call)}')
                          for call in ast.walk(node)
                          if isinstance(call, ast.Call) and _is_clock_call(call, readers[module])]
        if found:
            result[module] = sorted(found)
    return result


def _own_sources():
    return {path.stem: path.read_text(encoding='utf-8') for path in sorted(TESTS.glob('*.py'))}


def test_no_test_module_reads_the_clock_at_import():
    """Kein Modul unter tests/ liest beim Import die Uhr (Modulebene, Klassenkörper, Dekoratoren, Standardwerte,
    ``parametrize``) oder lässt eine Fixture mit Geltungsbereich über den Test hinaus sie lesen."""
    found = clock_reads_at_import(_own_sources())
    report = '\n'.join(f'  tests/{module}.py:{line}: {what}' for module, items in found.items() for line, what in items)
    assert not found, ('Ein „heute“ aus der Uhr gehört in den Test, nicht in eine Konstante, einen Standardwert oder '
                       'eine parametrize-Liste (siehe Kopf dieser Datei):\n' + report)


# ── der Wächter selbst ───────────────────────────────────────────────────────

def _src(*lines):
    """Quelltext aus Zeilen (so steht ein ``@`` nie hinter einem ``\\n`` im Text: der Anonymisierungs-Wächter
    läse ``n@pytest…`` als E-Mail-Adresse)."""
    return '\n'.join(lines) + '\n'


def _found(source, **others):
    """Funde im Modul ``m`` (Quelltext ``source``); ``others``: weitere Module unter tests/ (Name -> Quelltext)."""
    return [what for _line, what in clock_reads_at_import({'m': source, **others}).get('m', [])]


def test_the_guard_finds_a_constant_taken_at_import():
    assert _found(_src('import datetime', 'TODAY = datetime.date.today()')) == [
        'datetime.date.today() wird beim Import ausgeführt']
    assert len(_found(_src('from datetime import date, datetime', 'YEAR = date.today().year',
                           'NOW = datetime.now()'))) == 2
    assert len(_found(_src('import datetime', 'STAMP = datetime.datetime.utcnow().isoformat()'))) == 1
    assert len(_found(_src('import time', 'START = time.time()', 'LOCAL = time.localtime()',
                           'LABEL = time.strftime("%Y")'))) == 3


def test_the_guard_finds_a_reading_in_a_default_a_decorator_a_class_body_and_a_module_block():
    assert len(_found(_src('def f(day=date.today().isoformat()):', '    return day'))) == 1
    assert len(_found(_src('def f(*, day=date.today()):', '    return day'))) == 1
    assert len(_found(_src('import pytest', '@pytest.mark.parametrize("d", [date.today()])',
                           'def test_x(d):', '    pass'))) == 1
    assert len(_found(_src('class Marks:', '    day = date.today()', '    def method(self, other=datetime.now()):',
                           '        pass'))) == 2
    assert len(_found(_src('if True:', '    TODAY = date.today()'))) == 1
    assert len(_found(_src('ALL = [date.today() for _ in range(2)]'))) == 1
    assert len(_found(_src('LATER = lambda day=date.today(): day'))) == 1


def test_the_guard_lets_a_reading_inside_a_function_or_lambda_body_pass():
    assert _found(_src('def today():', '    return date.today()', '', 'def test_x():',
                       '    assert today() == date.today()')) == []
    assert _found(_src('NOW = lambda: datetime.now()')) == []
    assert _found(_src('class Marks:', '    def method(self):', '        return date.today()')) == []
    assert _found(_src('FIXED = date(2026, 1, 1)', 'STEP = datetime.timedelta(days=1)',
                       'DAY = date.fromisoformat("2026-01-01")', 'WHEN = time.perf_counter()')) == []
    assert _found(_src('SOON = date.today')) == []                          # die Funktion, nicht ihr Ergebnis


def test_the_guard_finds_a_call_of_a_function_that_reads_the_clock_even_through_another_module():
    reading = _src('import datetime', 'def today():', '    return datetime.date.today()', '', 'def today_de():',
                   '    return today().strftime("%d.%m.%Y")')
    assert _found(reading + 'TODAY = today()\n') == ['today() wird beim Import ausgeführt']
    assert _found(reading + 'TODAY_DE = today_de()\n') == ['today_de() wird beim Import ausgeführt']   # mittelbar
    assert len(_found(reading + _src('import pytest', '@pytest.mark.parametrize("d", [today()])',
                                     'def test_x(d):', '    pass'))) == 1
    assert _found(reading + _src('def test_x():', '    assert today() == today()')) == []        # im Test: richtig
    assert _found(_src('from tests.helper import today', 'TODAY = today()'), helper=reading) == [
        'today() wird beim Import ausgeführt']
    assert _found(_src('from tests.helper import other', 'TODAY = other()'), helper=reading) == []
    assert _found(_src('def today():', '    return 5', 'TODAY = today()'), helper=reading) == []   # liest nichts


def test_the_guard_finds_a_fixture_with_a_scope_wider_than_the_test_that_reads_the_clock():
    def fixture(decorator):
        return _src('import pytest', decorator, 'def day():', '    return date.today()')

    for scope in ('module', 'session', 'class', 'package'):
        found = _found(fixture(f'@pytest.fixture(scope="{scope}")'))
        assert len(found) == 1 and f"scope='{scope}'" in found[0], scope
    assert _found(fixture('@pytest.fixture(scope="function")')) == []
    assert _found(fixture('@pytest.fixture')) == []
    assert _found(fixture('@pytest.fixture(autouse=True)')) == []
    assert _found(_src('import pytest', '@pytest.fixture(scope="module")', 'def rows():', '    return [1, 2]')) == []
