# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Die Workflows, die pytest ausführen, tun es alle auf dieselbe Weise.

Drei Workflows fahren die Suite: bei jedem Push (``tests.yml``), vor dem Bauen des Images (``docker-publish.yml`` – die Schritte sind dort
bewusst kopiert, damit sich die Läufe nicht gegenseitig abbrechen) und einmal in der Woche mit neueren Python-Versionen
(``tests-python-versions.yml``). Weil die Schritte kopiert sind, hält dieser Test fest, dass sie gleich bleiben: derselbe Aufruf von pytest
und davor der Schritt, der die Arbeitsdaten der Tests (``data/tests/``) in den Arbeitsspeicher des Runners legt. Jeder Test legt eine eigene
SQLite-Datenbank an; auf der Platte wartet jedes Speichern, im Arbeitsspeicher nicht – gemessen dauert die Suite so rund ein Drittel der Zeit.
Der Schritt arbeitet mit einem Verweis statt mit ``--basetemp``: So bleibt ``tests/conftest.py`` zuständig, und der Test, der die Ablage unter
``data/tests/tmp`` prüft (``tests/test_test_isolation.py``), wird nicht übersprungen.

Die Suite läuft dort auf vier Prozessen (``-n 4``, ``pytest-xdist``): gemessen knapp 13 Minuten auf einem Prozess mit Arbeitsdaten im Arbeitsspeicher,
knapp 4 Minuten auf vieren. Das Plugin steht neben ``pytest`` in ``requirements-dev.txt``, woraus alle drei Workflows ihre Entwicklungs-Abhängigkeiten
installieren. Das ``-n`` gehört nur in den Aufruf der CI, nie in die ``pytest.ini``: ``python -m pytest`` bleibt lokal ein Prozess und läuft auch ohne das
Plugin, und ``tools/project_stats.py`` zählt mit ``pytest --collect-only`` ohne Worker.
"""
import configparser
import glob
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKFLOWS = os.path.join(ROOT, '.github', 'workflows')
PYTEST_CALL = 'python -m pytest -q -rs -n 4'
INSTALL_STEP = 'run: pip install -r requirements-dev.txt'
MEMORY_STEP = ('      - name: Arbeitsdaten der Tests in den Arbeitsspeicher legen\n'
               '        run: |\n'
               '          mkdir -p /dev/shm/ubuchhaltung-tests data\n'
               '          ln -s /dev/shm/ubuchhaltung-tests data/tests\n')
EXPECTED = {'tests.yml', 'docker-publish.yml', 'tests-python-versions.yml'}


def _workflows():
    for path in sorted(glob.glob(os.path.join(WORKFLOWS, '*.yml'))):
        with open(path, encoding='utf-8') as handle:
            yield os.path.basename(path), handle.read()


def _pytest_calls(text):
    """Die Zeilen eines Workflows, die pytest starten (ohne Kommentare)."""
    return [line.strip() for line in text.splitlines()
            if re.search(r'\bpytest\b', line) and line.strip().startswith('run:')]


def test_the_suite_runs_in_exactly_the_expected_workflows():
    running = {name for name, text in _workflows() if _pytest_calls(text)}
    assert running == EXPECTED, 'Ein Workflow mit pytest kam dazu oder fiel weg: hier eintragen und den Schritt für den Arbeitsspeicher mitnehmen.'


def test_every_workflow_calls_pytest_the_same_way_once():
    for name, text in _workflows():
        if name in EXPECTED:
            assert _pytest_calls(text) == ['run: ' + PYTEST_CALL], name


def test_the_working_data_of_the_tests_lies_in_memory_before_pytest_starts():
    for name, text in _workflows():
        if name not in EXPECTED:
            continue
        assert text.count(MEMORY_STEP) == 1, f'{name}: der Schritt für den Arbeitsspeicher fehlt oder weicht ab'
        assert text.index(MEMORY_STEP) < text.index('run: ' + PYTEST_CALL), f'{name}: der Schritt steht nicht vor pytest'


def test_the_plugin_for_the_parallel_run_is_installed_the_same_way_as_pytest():
    """``-n 4`` braucht ``pytest-xdist``; ohne das Plugin bräche der Aufruf mit „unrecognized arguments“ ab. Alle drei Workflows installieren ihre
    Entwicklungs-Abhängigkeiten aus ``requirements-dev.txt`` und tun es vor pytest – dort steht das Plugin neben ``pytest`` selbst."""
    for name, text in _workflows():
        if name in EXPECTED:
            assert INSTALL_STEP in text, f'{name}: die Entwicklungs-Abhängigkeiten werden nicht aus requirements-dev.txt installiert'
            assert text.index(INSTALL_STEP) < text.index('run: ' + PYTEST_CALL), f'{name}: die Installation steht nicht vor pytest'
    with open(os.path.join(ROOT, 'requirements-dev.txt'), encoding='utf-8') as handle:
        requirements = handle.read()
    for package in ('pytest', 'pytest-xdist'):
        assert re.search(rf'^{package}(?![\w-])', requirements, re.MULTILINE), f'requirements-dev.txt führt {package} nicht'


def test_the_parallel_run_is_not_the_default_of_a_local_call():
    """Ohne ``-n`` läuft ``python -m pytest`` auf einem Prozess und braucht das Plugin nicht; ``tools/project_stats.py`` zählt die Testfälle mit
    ``pytest --collect-only`` und bekäme sonst Worker. Deshalb steht kein ``-n``/``--numprocesses``/``--dist`` in ``addopts`` der ``pytest.ini``."""
    parser = configparser.ConfigParser(interpolation=None)
    parser.read(os.path.join(ROOT, 'pytest.ini'), encoding='utf-8')
    addopts = parser.get('pytest', 'addopts', fallback='').split()
    assert [word for word in addopts if re.match(r'(-n|--numprocesses|--maxprocesses|--dist)', word) or word == 'xdist'] == []


def test_the_link_points_where_the_tests_keep_their_working_data():
    """Der Verweis ersetzt genau den Ordner, den ``conftest.py`` und ``pytest.ini`` für Arbeitsdaten und Cache vorsehen."""
    with open(os.path.join(ROOT, 'tests', 'conftest.py'), encoding='utf-8') as handle:
        conftest = handle.read()
    with open(os.path.join(ROOT, 'pytest.ini'), encoding='utf-8') as handle:
        ini = handle.read()
    assert "'data', 'tests', 'tmp'" in conftest or '"data", "tests", "tmp"' in conftest or 'data/tests/tmp' in conftest
    assert 'cache_dir = data/tests/cache' in ini
    assert 'ln -s /dev/shm/ubuchhaltung-tests data/tests' in MEMORY_STEP
