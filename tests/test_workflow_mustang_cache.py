# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Der Job ``einvoice`` holt den Validator (Mustang) aus dem Zwischenspeicher – und prüft seine Prüfsumme immer.

Der Job lud die JAR bei jedem Lauf von Maven Central; ein Lauf scheiterte dort einmal an HTTP 429. Jetzt liegt sie im Zwischenspeicher (``actions/cache``, Schlüssel aus Version und
SHA-256) und wird nur bei einem Fehltreffer geladen. Der Zwischenspeicher ist nur ein Ablageort, kein Vertrauen: Die SHA-256-Prüfung steht in einem eigenen Schritt ohne Bedingung und läuft
auch nach einem Treffer. Dieser Test hält das fest – ein späteres „nur nach dem Laden prüfen“ ließe eine unbemerkt vertauschte Datei im Zwischenspeicher durch.
"""
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KEY = 'mustang-${{ env.MUSTANG_VERSION }}-${{ env.MUSTANG_SHA256 }}'


def _job():
    with open(os.path.join(ROOT, '.github', 'workflows', 'tests.yml'), encoding='utf-8') as handle:
        text = handle.read()
    return text[text.index('\n  einvoice:\n'):]


def _steps():
    """Die Schritte des Jobs ``einvoice`` in ihrer Reihenfolge, je als Text ihres Blocks (die erste Zeile ist ``- name: …``)."""
    return re.split(r'\n(?=      - name: )', _job())[1:]


def _step(fragment):
    found = [block for block in _steps() if fragment in block.splitlines()[0]]
    assert len(found) == 1, f'{fragment}: {len(found)} Schritte'
    return found[0]


def _own_lines(block):
    """Die Zeilen des Schritts selbst (Einrückung 8), ohne die Kommentare, die vor dem nächsten Schritt hängen."""
    return [line for line in block.splitlines() if line.startswith('        ') and not line.lstrip().startswith('#')]


def test_the_validator_comes_from_the_cache_keyed_by_version_and_checksum():
    cache = _own_lines(_step('Zwischenspeicher'))
    assert any(re.fullmatch(r'        uses: actions/cache@[0-9a-f]{40}  # v\d+\.\d+\.\d+', line) for line in cache)
    assert '          path: mustang.jar' in cache and f'          key: {KEY}' in cache
    job = _job()
    assert re.search(r"^      MUSTANG_VERSION: '[0-9.]+'$", job, re.M) and re.search(r"^      MUSTANG_SHA256: '[0-9a-f]{64}'$", job, re.M)


def test_the_download_runs_only_when_the_cache_missed():
    cache_id = next(line.split(': ', 1)[1] for line in _own_lines(_step('Zwischenspeicher')) if line.startswith('        id: '))
    download = _own_lines(_step('Mustang-CLI laden'))
    assert f"        if: steps.{cache_id}.outputs.cache-hit != 'true'" in download
    assert any('curl' in line and 'mustang.jar' in line for line in download) and not any('sha256sum' in line for line in download)


def test_the_checksum_is_checked_every_time_even_after_a_cache_hit():
    """Der Schritt trägt weder ``if`` noch ``continue-on-error``; er steht hinter dem Laden und vor dem Validieren."""
    check = _own_lines(_step('Mustang-CLI prüfen'))
    assert any('sha256sum -c' in line and '${MUSTANG_SHA256}' in line and 'mustang.jar' in line for line in check)
    assert not any(line.startswith(('        if:', '        continue-on-error:')) for line in check)
    names = [block.splitlines()[0] for block in _steps()]
    order = [next(i for i, name in enumerate(names) if fragment in name) for fragment in
             ('Zwischenspeicher', 'Mustang-CLI laden', 'Mustang-CLI prüfen', 'Mit Mustang validieren')]
    assert order == sorted(order)
