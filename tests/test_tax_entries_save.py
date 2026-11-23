# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Speichern von Rechnung und Angebot mit einem Steuersatz aus den Stammdaten (2.8.0, Etappe 3: ``server/handlers.py`` – ``_tax_fields``,
``recompute_invoice_totals``, ``_invoice_fields``, ``handle_quote_save`` –, ``db/invoice_lock.py`` und ``db/invoice_versions.py``: die Sperre). Das Stornieren eines
Belegs mit Momentaufnahme (``db/invoice_storno.py``: Wartekonto genau des Satzes, Schutz vor widersprüchlichen Angaben) steht in ``tests/test_tax_entries_storno.py``.

Das Formular nennt einen Eintrag der Steuersätze als ``taxRateId`` – im Kopf (alle Positionen, nur ohne ``taxMode``) oder je Position (nur mit ``taxMode``
``'mixed'``). Der Server liest den Eintrag im Moment des Speicherns (wählbar = gültig und aktiv), prüft, dass der Satz des Formulars der des Eintrags ist, und schreibt
an jede Position eine **Momentaufnahme** (``InvoiceItems.TaxCategory`` ``'E'`` bei steuerfrei, ``TaxRateBp`` der Satz in Hundertstel Prozent, ``TaxExemptReason``,
``RevenueCoaId``) – nie die ID des Eintrags. Die Summen rechnen eine Position mit Momentaufnahme einstufig (EN 16931, BR-CO-17), jede andere wie bisher.

* **Pflicht, je Variante ein Test:** Je Satz höchstens eine Momentaufnahme je Beleg (``TAX_SAME_RATE_TWICE``) – auch „mit“ gegen „ohne“. Sonst gäbe es in einem Beleg zwei 0-%-Arten,
  zwei Befreiungsgründe oder eine Satzgruppe mit gemischter Rundung, und die Dokumente und das Buchen (``group_snapshots``) könnten sie nicht lesen.
* Jede Ablehnung kommt mit ihrem festen Satz vor dem ersten Schreiben: nichts gespeichert, keine Nummer verbraucht. ``taxRateId: 0`` (der Wert „gespeicherter Stand, Eintrag nicht mehr
  vorhanden“) wird immer abgelehnt. Ohne Steuerausweis gibt es keinen Eintrag (``TAX_CHOICE_WITHOUT_TAX``).
* **Ein Beleg ohne ``taxRateId`` schreibt Zeile für Zeile dasselbe wie vor E3** (die neuen Spalten NULL, ``TaxCategory`` ``'S'``) – auch das erneute Speichern eines Entwurfs, der vorher
  Momentaufnahmen trug: Sie entfallen mit der Wahl der Vorbelegung. Dazu ``tests/test_mixed_tax_save.py``, ``tests/test_single_rate_snapshots.py`` (unverändert).
* „Position n“ in jeder Meldung ist die Zeile der Maske (``item['position']``, eine ganze Zahl von 1 bis 9999), sonst die laufende Nummer.
* Die Sperre einer versendeten Rechnung vergleicht die vier Spalten der Momentaufnahme mit (``_items_differ``).
* Zum ersten Mal laufen Speichern, Buchen und Dokumente zusammen: Entwurf speichern → versenden → Zahlung zuordnen → PDF und E-Rechnung.
* **Die Vorbelegung (19 und 7 % ohne Erlöskonto, ``legacy_expressible``) schreibt auch mit ID keine Momentaufnahme** (2.8.2, Befund N1): Der Beleg ist Spalte für Spalte der ohne ID –
  die Maske schickt für diese Einträge nie eine, und „öffnen und unverändert speichern“ ändert so keine Spalte (der Rundlauf mit der Maske: ``tests/test_tax_entries_mask.py``). Alle
  Prüfungen der ID bleiben, ``TAX_SAME_RATE_TWICE`` gilt nach dem, was gespeichert wird.
* **Das Erlöskonto einer Momentaufnahme wird in der Schreibtransaktion geprüft** (Befund N3): Verschwindet es zwischen dem Lesen des Eintrags und dem Schreiben, lehnt ``TAX_CHOICE_CHANGED``
  ab – Zeile für Zeile alles wie vorher, keine Nummer verbraucht.
* **Kaputte Positionen** (``items`` ohne Objekte: ``[1]``, ``{"a": 1}``, ``[[]]``) bekommen wieder den Satz von 2.7.0 (Befund H1): die Angaben zum Steuersatz werden vor dem ersten Zugriff auf
  eine Position geprüft.

Alle Namen, Gründe, Konten und Beträge sind erfunden; ein „heute“ steht nie in einer Modulkonstante.
"""
import contextlib
import copy
import datetime
import json
import pathlib
import random
import sqlite3
from decimal import ROUND_HALF_UP, Decimal
from types import SimpleNamespace

import pytest

import server.handlers as handlers
import userctx
from db.invoice_lock import LOCKED_MESSAGE, NO_WAITING_HINT, InvoiceLocked, _items_differ, stored_items
from export.invoice_files import generate_invoice_documents
from export.pdf_quote import generate_quote_pdf
from money import to_minor
from server.handlers import (TAX_CHOICE_CHANGED, TAX_CHOICE_GONE, TAX_CHOICE_HEAD_GONE, TAX_CHOICE_RATE_DIFFERS, TAX_CHOICE_WITHOUT_TAX, TAX_ITEM_DIFFERS,
                             TAX_ITEM_INVALID, TAX_ITEM_MISSING, TAX_ITEM_NOT_ALLOWED, TAX_MODE_INVALID, TAX_PER_ITEM_WITHOUT_TAX, TAX_RATE_INVALID,
                             TAX_SAME_RATE_TWICE, _choice_id, _position_number, recompute_invoice_totals)
from tax_groups import EXEMPT, STANDARD, item_groups, mixed_groups
from tests.invoice_world import ITEMS, build_world, columns, make_payload, query_rows, run_sql, save_raw, set_invoice_status, snapshot
from tests.single_rate_helpers import document_facts, pdf_facts
from tests.test_mixed_tax_bookings import marked, pay_on, receivables, standing
from tests.test_tax_entries_documents import page_lines

USER = 'anna'
REASON = 'Steuerfrei nach dem Testgesetz Paragraf 4 (erfundener Grund)'
OTHER_REASON = 'Steuerfrei nach dem zweiten Testgesetz Paragraf 5 (erfundener Grund)'
KINDS = ('invoice', 'quote')
NO_SKONTO = {'discountPercentage': None, 'discountDays': None}      # Skonto gibt es nur bei einem positiven Rechnungsbetrag: ein Betrag von 0,0025 € rundet auf 0,00 €
D = Decimal


@pytest.fixture
def world(tmp_db, monkeypatch, tmp_path):
    monkeypatch.setattr(handlers, 'Database', lambda *a, **k: tmp_db)
    monkeypatch.chdir(tmp_path)
    userctx.set_user(USER)
    return build_world(tmp_db)


def account(db, number):
    """Das Erlöskonto ``number`` (COA-ID); führt der Standard-Kontenrahmen es nicht, entsteht es."""
    if db.get_coa_id_by_account_number(number) is None:
        db.insert_chart_of_accounts(4, number, f'Erlöse (erfundenes Testkonto {number})', 'Betriebliche Erträge')
    return db.get_coa_id_by_account_number(number)


@pytest.fixture
def entries(world):
    """Die Einträge der Steuersätze: die Vorbelegung (19 und 7 Prozent ohne Erlöskonto), ein steuerfreier mit Grund und Konto, ein zweiter mit anderem Grund, einer über 5,5 %,
    7,8 % und 19,25 % mit je eigenem Konto, ein Standardsatz 0 mit Konto, ein 19-%- und ein 7-%-Eintrag mit eigenem Konto und ein zweiter 7-%-Eintrag, der inhaltlich
    der Vorbelegung gleicht."""
    db = world.db
    coa = {number: account(db, number) for number in (4120, 4150, 4160, 4170, 4180, 4190)}
    preset = {entry.bp: entry.id for entry in db.fetch_tax_rates()}
    return SimpleNamespace(
        coa=coa, preset19=preset[1900], preset7=preset[700],
        free=db.insert_tax_rate(label='Steuerfrei (erfunden)', bp=0, kind=EXEMPT, reason=REASON, revenue_coa_id=coa[4120]),
        free_other=db.insert_tax_rate(label='Steuerfrei, andere Leistung (erfunden)', bp=0, kind=EXEMPT, reason=OTHER_REASON, revenue_coa_id=coa[4120]),
        zero_standard=db.insert_tax_rate(label='Standard 0 % (erfunden)', bp=0, kind=STANDARD, revenue_coa_id=coa[4120]),
        reduced=db.insert_tax_rate(label='Ermäßigt 5,5 % (erfunden)', bp=550, kind=STANDARD, revenue_coa_id=coa[4150]),
        seven_eight=db.insert_tax_rate(label='Satz 7,8 % (erfunden)', bp=780, kind=STANDARD, revenue_coa_id=coa[4160]),
        nineteen_quarter=db.insert_tax_rate(label='Satz 19,25 % (erfunden)', bp=1925, kind=STANDARD, revenue_coa_id=coa[4170]),
        nineteen_special=db.insert_tax_rate(label='19 % mit Sonderkonto (erfunden)', bp=1900, kind=STANDARD, revenue_coa_id=coa[4180]),
        seven_special=db.insert_tax_rate(label='7 % mit Sonderkonto (erfunden)', bp=700, kind=STANDARD, revenue_coa_id=coa[4190]),
        seven_twin=db.insert_tax_rate(label='7 % (zweiter Eintrag, erfunden)', bp=700, kind=STANDARD))


# ── Formulare und Antworten, für Rechnung und Angebot ────────────────────────────────────────────────────────────────────────────

def item(position, description, price, rate, quantity=1, **extra):
    """Eine Position, wie die Maske sie schickt (Satz als Prozentzahl); ``extra``: etwa ``taxRateId``."""
    return {'position': position, 'quantity': quantity, 'unit': 'C62', 'description': description, 'unitPrice': price, 'taxRate': rate, **extra}


def quote_payload(world, **over):
    payload = {'quoteNumber': '26A100', 'quoteDate': '2026-06-07', 'validUntil': '2026-07-07', 'customerId': world.cust, 'ownCompanyId': world.own,
               'taxRate': 0.19, 'status': 'draft', 'items': copy.deepcopy(ITEMS), 'introText': 'Guten Tag', 'closingText': 'Mit freundlichen Grüßen'}
    payload.update(over)
    return payload


def form(kind, world, items, **over):
    """Das Formular einer Rechnung bzw. eines Angebots mit diesen Positionen; ``over`` ersetzt einzelne Angaben (etwa ``taxRate``, ``taxMode``, ``taxRateId``)."""
    return make_payload(world, items=items, **over) if kind == 'invoice' else quote_payload(world, items=items, **over)


def post(kind, payload):
    """Das Speichern über den Handler: ``(Antwort als Dict, Statuscode)``."""
    if kind == 'invoice':
        raw = save_raw(payload)
        return json.loads(raw), raw.status
    return json.loads(handlers.handle_quote_save(json.dumps(payload).encode())), 200


def number_over(kind, count):
    """Die Nummer eines weiteren Belegs derselben Art (Rechnung: ``invoiceNumber``, Angebot: ``quoteNumber``) – eine eigene Nummer je Beleg."""
    return {'invoiceNumber': f'26R{count}'} if kind == 'invoice' else {'quoteNumber': f'26A{count}'}


def saved(kind, payload):
    """Speichern, das gelingt: die ID des Belegs."""
    answer, _status = post(kind, payload)
    assert answer['success'] is True, answer
    return answer['invoice_id'] if kind == 'invoice' else answer['quote_id']


def resave(kind, world, doc_id, items, **over):
    """Den Entwurf ``doc_id`` noch einmal speichern, wie die Maske es tut (dieselbe Nummer, dieselbe ID)."""
    if kind == 'invoice':
        return saved(kind, form(kind, world, items, invoiceId=doc_id, invoiceNumber=columns(world.db, doc_id)['InvoiceNumber'], **over))
    return saved(kind, form(kind, world, items, quoteId=doc_id, quoteNumber=columns(world.db, doc_id)['InvoiceNumber'], **over))


def refused(kind, world, payload):
    """Das Speichern lehnt ab: der Klartext der Antwort; nichts darf sich geändert haben – kein Beleg, keine Position, keine verbrauchte Nummer."""
    before = snapshot(world.db)
    answer, _status = post(kind, payload)
    assert answer['success'] is False, answer
    assert snapshot(world.db) == before
    return answer['error']


def head(db, doc_id):
    """Was der Kopf an Steuerangaben speichert: Satz, Kennzeichen und die Summen (Minor Units)."""
    row = columns(db, doc_id)
    return {key: row[key] for key in ('TaxRate', 'MixedTaxRates', 'SumNet', 'TaxAmount', 'SumGross', 'AmountDue')}


def minor(netto, steuer, brutto):
    return {'SumNet': to_minor(netto), 'TaxAmount': to_minor(steuer), 'SumGross': to_minor(brutto), 'AmountDue': to_minor(brutto)}


def stored(db, doc_id):
    """Die Positionen mit Satz und Momentaufnahme, wie sie in der Datenbank stehen."""
    return query_rows(db, 'SELECT Position, TotalNet, TaxCategory, TaxRate, TaxRateBp, TaxExemptReason, RevenueCoaId FROM InvoiceItems WHERE InvoiceId = ? '
                          'ORDER BY Position', doc_id)


def snapshot_columns(db, doc_id):
    """Nur die vier Spalten der Momentaufnahme je Position: ``(TaxCategory, TaxRateBp, TaxExemptReason, RevenueCoaId)``."""
    return [(r['TaxCategory'], r['TaxRateBp'], r['TaxExemptReason'], r['RevenueCoaId']) for r in stored(db, doc_id)]


def whole_document(db, doc_id):
    """Jede Spalte von ``Invoices`` und ``InvoiceItems`` des Belegs – ohne das, was sich je Beleg unterscheidet (ID, Nummer, Zeitstempel, die ID der Positionen und ihr Verweis)."""
    skip = {'ID', 'InvoiceNumber', 'CreatedAt', 'UpdatedAt'}
    positions = query_rows(db, 'SELECT * FROM InvoiceItems WHERE InvoiceId = ? ORDER BY Position', doc_id)
    return ({key: value for key, value in columns(db, doc_id).items() if key not in skip},
            [{key: value for key, value in row.items() if key not in ('ID', 'InvoiceId')} for row in positions])


def everything(db):
    """Alle Zeilen ALLER Tabellen (auch ``sqlite_sequence``), roh – zum Vergleich vorher/nachher: Eine Ablehnung ändert nichts, verbraucht keine Nummer und keinen Zähler."""
    with contextlib.closing(sqlite3.connect(db.db_name)) as con:
        names = [row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name")]
        return {name: con.execute(f'SELECT * FROM "{name}" ORDER BY rowid').fetchall() for name in names}


NO_SNAPSHOT = ('S', None, None, None)                      # eine Position wie bisher


def free_snapshot(entries, reason=REASON):
    return ('E', 0, reason, entries.coa[4120])


def two_rates(**over):
    """19 % und 7 % ohne Eintrag – das Formular von heute mit dem Häkchen „Steuersatz je Position“."""
    return [item(1, 'Beratung', 100.0, 19, **over), item(2, 'Fachbuch', 50.0, 7)]


# ═════════════════════════════════════════════════════════════════════════════
# Ein Eintrag im Kopf (ohne taxMode): für alle Positionen
# ═════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize('kind', KINDS)
def test_a_fully_exempt_document_carries_the_snapshot_of_the_exempt_entry_on_every_position(world, entries, kind):
    doc_id = saved(kind, form(kind, world, [item(1, 'Nachhilfe', 100.0, 0, quantity=2), item(2, 'Lernmaterial', 30.0, 0)], taxRate=0, taxRateId=entries.free))

    assert head(world.db, doc_id) == {'TaxRate': 0.0, 'MixedTaxRates': None, **minor('230.00', '0.00', '230.00')}
    assert stored(world.db, doc_id) == [
        {'Position': 1, 'TotalNet': to_minor('200.00'), 'TaxCategory': 'E', 'TaxRate': 0.0, 'TaxRateBp': 0, 'TaxExemptReason': REASON, 'RevenueCoaId': entries.coa[4120]},
        {'Position': 2, 'TotalNet': to_minor('30.00'), 'TaxCategory': 'E', 'TaxRate': 0.0, 'TaxRateBp': 0, 'TaxExemptReason': REASON, 'RevenueCoaId': entries.coa[4120]}]
    assert columns(world.db, doc_id)['TaxCategory'] == 'S'                         # der Kopf trägt weiter nur den Satz


@pytest.mark.parametrize('kind', KINDS)
def test_an_entry_in_the_head_gives_every_position_its_snapshot_and_the_rate_stays_as_the_form_sent_it(world, entries, kind):
    doc_id = saved(kind, form(kind, world, [item(1, 'Beratung', 100.0, 5.5), item(2, 'Material', 50.5, 5.5)], taxRate=0.055, taxRateId=entries.reduced))

    assert head(world.db, doc_id) == {'TaxRate': 0.055, 'MixedTaxRates': None, **minor('150.50', '8.28', '158.78')}          # 150,50 · 5,5 % = 8,2775 → 8,28
    assert [(r['TaxRate'], r['TaxRateBp']) for r in stored(world.db, doc_id)] == [(5.5, 550), (5.5, 550)]
    assert snapshot_columns(world.db, doc_id) == [('S', 550, None, entries.coa[4150])] * 2


@pytest.mark.parametrize('kind', KINDS)
def test_positions_without_a_rate_take_the_rate_of_the_head_with_an_entry_too(world, entries, kind):
    bare = {'position': 1, 'quantity': 3, 'description': 'Heft', 'unitPrice': 10.0}

    doc_id = saved(kind, form(kind, world, [bare], taxRate=0.055, taxRateId=entries.reduced))

    assert [(r['TaxRate'], r['TaxRateBp']) for r in stored(world.db, doc_id)] == [(0.055, 550)]
    assert head(world.db, doc_id) == {'TaxRate': 0.055, 'MixedTaxRates': None, **minor('30.00', '1.65', '31.65')}


@pytest.mark.parametrize('kind', KINDS)
def test_a_preset_entry_in_the_head_writes_no_snapshot_and_changes_no_sum(world, entries, kind):
    """Die Vorbelegung (19 % ohne Konto) trägt der bisherige Payload ohne Verlust (``legacy_expressible``): Mit ID geschickt, schreibt der Server – wie ohne – keine Momentaufnahme; die
    Summen sind die des heutigen Formulars. (Bis 2.8.1 bekam der Beleg hier ``TaxRateBp`` 1900, und „öffnen und unverändert speichern“ setzte es wieder auf NULL.)"""
    plain = saved(kind, form(kind, world, copy.deepcopy(ITEMS)))
    with_entry = saved(kind, form(kind, world, copy.deepcopy(ITEMS), taxRateId=entries.preset19, **number_over(kind, 101)))

    assert head(world.db, with_entry) == head(world.db, plain) == {'TaxRate': 0.19, 'MixedTaxRates': None, **minor('250.50', '47.60', '298.10')}
    assert snapshot_columns(world.db, plain) == [NO_SNAPSHOT] * 2
    assert snapshot_columns(world.db, with_entry) == [NO_SNAPSHOT] * 2


@pytest.mark.parametrize('kind', KINDS)
@pytest.mark.parametrize('preset, rate', [('preset19', 19), ('preset7', 7)])
def test_a_preset_entry_in_the_head_gives_the_document_that_the_form_without_an_id_gives_column_for_column(world, entries, kind, preset, rate):
    """Nicht nur die Momentaufnahme: jede Spalte von ``Invoices`` und ``InvoiceItems``. Die Summen sind zweistufig wie ohne ID (hier mit Cent-Beträgen und Mengen, die runden)."""
    items = [item(1, 'Beratung', 99.99, rate, quantity=3), item(2, 'Material', 0.09, rate), item(3, 'Stunden', 33.33, rate, quantity=1.5)]
    plain = saved(kind, form(kind, world, copy.deepcopy(items), taxRate=rate / 100))
    with_entry = saved(kind, form(kind, world, copy.deepcopy(items), taxRate=rate / 100, taxRateId=getattr(entries, preset), **number_over(kind, 101)))

    assert whole_document(world.db, with_entry) == whole_document(world.db, plain)
    assert snapshot_columns(world.db, with_entry) == [NO_SNAPSHOT] * 3


@pytest.mark.parametrize('kind', KINDS)
def test_preset_entries_at_the_positions_write_no_snapshot_either(world, entries, kind):
    """Mit dem Häkchen „Steuersatz je Position“: 19 und 7 % mit ihrer ID sind der Beleg ohne IDs, Spalte für Spalte – auch das Kennzeichen und der Kopf."""
    ids = [item(1, 'Beratung', 100.0, 19, taxRateId=entries.preset19), item(2, 'Fachbuch', 50.0, 7, taxRateId=entries.preset7),
           item(3, 'Stunden', 10.01, 19, taxRateId=entries.preset19)]
    without = [{key: value for key, value in row.items() if key != 'taxRateId'} for row in ids]
    plain = saved(kind, form(kind, world, without, taxMode='mixed'))
    with_ids = saved(kind, form(kind, world, ids, taxMode='mixed', **number_over(kind, 101)))

    assert whole_document(world.db, with_ids) == whole_document(world.db, plain)
    assert snapshot_columns(world.db, with_ids) == [NO_SNAPSHOT] * 3
    assert head(world.db, with_ids)['MixedTaxRates'] == 1


@pytest.mark.parametrize('kind', KINDS)
def test_only_the_positions_with_an_entry_the_payload_cannot_carry_get_a_snapshot_in_a_mixed_document(world, entries, kind):
    doc_id = saved(kind, form(kind, world, [item(1, 'Beratung', 100.0, 19, taxRateId=entries.preset19), item(2, 'Nachhilfe', 80.0, 0, taxRateId=entries.free),
                                            item(3, 'Heft', 10.0, 5.5, taxRateId=entries.reduced), item(4, 'Fachbuch', 50.0, 7, taxRateId=entries.preset7)],
                             taxMode='mixed'))

    assert snapshot_columns(world.db, doc_id) == [NO_SNAPSHOT, free_snapshot(entries), ('S', 550, None, entries.coa[4150]), NO_SNAPSHOT]


@pytest.mark.parametrize('kind', KINDS)
def test_two_positions_of_one_rate_with_the_preset_and_its_id_or_none_are_one_snapshot_and_no_conflict(world, entries, kind):
    """„Mit ID gegen ohne“ ist für die Vorbelegung kein Widerspruch mehr: Beide schreiben nichts (``TAX_SAME_RATE_TWICE`` gilt nach dem, was gespeichert wird). Ein Eintrag, der etwas
    schriebe (``nineteen_special``, mit Erlöskonto), gegen die Vorbelegung bleibt einer – siehe ``REFUSALS``."""
    ids = saved(kind, form(kind, world, [item(1, 'Beratung', 100.0, 19), item(2, 'Material', 20.0, 19, taxRateId=entries.preset19), item(3, 'Fachbuch', 5.0, 7)],
                           taxMode='mixed'))
    both = saved(kind, form(kind, world, [item(1, 'Beratung', 100.0, 19, taxRateId=entries.preset19), item(2, 'Material', 20.0, 19), item(3, 'Fachbuch', 5.0, 7)],
                            taxMode='mixed', **number_over(kind, 101)))

    for doc_id in (ids, both):
        assert snapshot_columns(world.db, doc_id) == [NO_SNAPSHOT] * 3
        assert head(world.db, doc_id) == {'TaxRate': 0.19, 'MixedTaxRates': 1, **minor('125.00', '23.15', '148.15')}          # 120,00 · 19 % = 22,80 und 5,00 · 7 % = 0,35


# Alle Prüfungen der ID gelten für die Vorbelegung unverändert – die Ausnahme betrifft nur, was geschrieben wird
PRESET_REFUSALS = {
    'kopf-satz-passt-nicht': lambda e: ([item(1, 'A', 10.0, 7), item(2, 'B', 10.0, 7)], dict(taxRate=0.07, taxRateId=e.preset19), TAX_CHOICE_RATE_DIFFERS.format(n=1)),
    'position-satz-passt-nicht': lambda e: ([item(1, 'A', 10.0, 19), item(2, 'B', 10.0, 19, taxRateId=e.preset7)], MIXED, TAX_CHOICE_RATE_DIFFERS.format(n=2)),
    'kopf-passt-nicht-zur-position': lambda e: ([item(1, 'A', 10.0, 19)], dict(taxRate=0.07, taxRateId=e.preset19), TAX_CHOICE_RATE_DIFFERS.format(n=1)),
    'ohne-steuerausweis': lambda e: ([item(1, 'A', 10.0, 0)], dict(showTax=False, taxRate=0, taxRateId=e.preset19), TAX_CHOICE_WITHOUT_TAX),
    'mit-haeckchen-im-kopf': lambda e: ([item(1, 'A', 10.0, 19)], dict(taxMode='mixed', taxRateId=e.preset19), TAX_MODE_INVALID),
    'position-ohne-haeckchen': lambda e: ([item(1, 'A', 10.0, 19, taxRateId=e.preset19)], dict(taxRate=0.19), TAX_MODE_INVALID),
    'id-0': lambda e: ([item(1, 'A', 10.0, 19)], dict(taxRate=0.19, taxRateId=0), TAX_CHOICE_HEAD_GONE),
    'position-id-0': lambda e: ([plain_19(), item(2, 'B', 10.0, 7, taxRateId=0)], MIXED, TAX_CHOICE_GONE.format(n=2)),
}


@pytest.mark.parametrize('kind', KINDS)
@pytest.mark.parametrize('case', PRESET_REFUSALS)
def test_the_checks_of_the_id_stay_for_the_preset(world, entries, kind, case):
    items, over, sentence = PRESET_REFUSALS[case](entries)

    assert refused(kind, world, form(kind, world, items, **over)) == sentence


@pytest.mark.parametrize('kind', KINDS)
def test_a_preset_that_is_switched_off_is_refused_by_its_id_in_the_head_and_at_a_position(world, entries, kind):
    world.db.set_tax_rate_active(entries.preset19, False)

    in_head = refused(kind, world, form(kind, world, [item(1, 'A', 10.0, 19)], taxRate=0.19, taxRateId=entries.preset19))
    at_position = refused(kind, world, form(kind, world, [item(1, 'A', 10.0, 7), item(2, 'B', 10.0, 19, taxRateId=entries.preset19)], taxMode='mixed'))

    assert in_head == TAX_CHOICE_HEAD_GONE and at_position == TAX_CHOICE_GONE.format(n=2)


@pytest.mark.parametrize('kind', KINDS)
def test_the_tax_of_a_group_with_a_snapshot_is_rounded_once_and_without_one_as_before(world, entries, kind):
    """0,09 € netto bei 5,5 %: zweistufig 0,01 € (wie bisher, ohne Eintrag), einstufig 0,00 € (EN 16931, mit Eintrag)."""
    without = saved(kind, form(kind, world, [item(1, 'Kleinteil', 0.09, 5.5)], taxRate=0.055))
    with_entry = saved(kind, form(kind, world, [item(1, 'Kleinteil', 0.09, 5.5)], taxRate=0.055, taxRateId=entries.reduced,
                                  **number_over(kind, 101)))

    assert head(world.db, without) == {'TaxRate': 0.055, 'MixedTaxRates': None, **minor('0.09', '0.01', '0.10')}
    assert head(world.db, with_entry) == {'TaxRate': 0.055, 'MixedTaxRates': None, **minor('0.09', '0.00', '0.09')}


@pytest.mark.parametrize('kind', KINDS)
def test_a_numeric_string_is_read_as_the_id_and_null_means_no_entry(world, entries, kind):
    as_text = saved(kind, form(kind, world, [item(1, 'Nachhilfe', 10.0, 0)], taxRate=0, taxRateId=str(entries.free)))
    as_null = saved(kind, form(kind, world, [item(1, 'Nachhilfe', 10.0, 0)], taxRate=0, taxRateId=None,
                               **number_over(kind, 101)))

    assert snapshot_columns(world.db, as_text) == [free_snapshot(entries)]
    assert snapshot_columns(world.db, as_null) == [NO_SNAPSHOT]


@pytest.mark.parametrize('kind', KINDS)
def test_the_snapshot_is_a_copy_a_change_or_the_deletion_of_the_entry_later_changes_no_document(world, entries, kind):
    doc_id = saved(kind, form(kind, world, [item(1, 'Nachhilfe', 100.0, 0)], taxRate=0, taxRateId=entries.free))
    before = (head(world.db, doc_id), stored(world.db, doc_id))

    world.db.update_tax_rate(entries.free, label='Steuerfrei (geändert)', bp=0, kind=EXEMPT, reason=OTHER_REASON, revenue_coa_id=entries.coa[4150])
    assert (head(world.db, doc_id), stored(world.db, doc_id)) == before
    world.db.delete_tax_rate(entries.free)

    assert (head(world.db, doc_id), stored(world.db, doc_id)) == before


# ═════════════════════════════════════════════════════════════════════════════
# Ein Eintrag je Position (taxMode 'mixed')
# ═════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize('kind', KINDS)
def test_19_percent_and_tax_free_is_a_flagged_document_and_only_the_exempt_position_carries_a_snapshot(world, entries, kind):
    doc_id = saved(kind, form(kind, world, [item(1, 'Beratung', 100.0, 19), item(2, 'Nachhilfe', 80.0, 0, taxRateId=entries.free)], taxMode='mixed'))

    assert head(world.db, doc_id) == {'TaxRate': 0.19, 'MixedTaxRates': 1, **minor('180.00', '19.00', '199.00')}
    assert snapshot_columns(world.db, doc_id) == [NO_SNAPSHOT, free_snapshot(entries)]
    assert [(r['TaxRate'], r['TaxRateBp']) for r in stored(world.db, doc_id)] == [(19.0, None), (0.0, 0)]


@pytest.mark.parametrize('kind', KINDS)
def test_19_and_5_5_percent_is_flagged_and_the_5_5_group_is_rounded_once(world, entries, kind):
    """Die 19-%-Gruppe rechnet wie bisher (zweistufig), die 5,5-%-Gruppe einstufig: 0,09 € ergeben 0,00 € Steuer – die Summen passen auf den Cent zu den Gruppen."""
    doc_id = saved(kind, form(kind, world, [item(1, 'Beratung', 100.0, 19), item(2, 'Kleinteil', 0.09, 5.5, taxRateId=entries.reduced)], taxMode='mixed'))

    assert head(world.db, doc_id) == {'TaxRate': 0.19, 'MixedTaxRates': 1, **minor('100.09', '19.00', '119.09')}
    rows = stored(world.db, doc_id)
    groups = mixed_groups(columns(world.db, doc_id)['SumNet'], columns(world.db, doc_id)['TaxAmount'], columns(world.db, doc_id)['SumGross'],
                          [(r['TaxRate'], r['TotalNet'], r['TaxRateBp']) for r in rows])
    assert [(g.pct, g.net, g.tax) for g in groups] == [(D(19), to_minor('100.00'), to_minor('19.00')), (D('5.5'), to_minor('0.09'), 0)]


@pytest.mark.parametrize('kind', KINDS)
def test_all_positions_with_the_same_entry_make_an_ordinary_document_even_with_the_mode(world, entries, kind):
    """Ganz steuerfrei (oder ganz 5,5 %): Kennzeichen NULL, der Kopf ist der Satz als Bruch, jede Position trägt die Momentaufnahme."""
    free = saved(kind, form(kind, world, [item(1, 'Nachhilfe', 80.0, 0, taxRateId=entries.free), item(2, 'Material', 20.0, 0, taxRateId=entries.free)],
                            taxMode='mixed'))
    reduced = saved(kind, form(kind, world, [item(1, 'Heft', 10.0, 5.5, taxRateId=entries.reduced), item(2, 'Buch', 20.0, 5.5, taxRateId=entries.reduced)],
                               taxMode='mixed', **number_over(kind, 101)))

    assert head(world.db, free) == {'TaxRate': 0.0, 'MixedTaxRates': None, **minor('100.00', '0.00', '100.00')}
    assert snapshot_columns(world.db, free) == [free_snapshot(entries)] * 2
    assert head(world.db, reduced) == {'TaxRate': 0.055, 'MixedTaxRates': None, **minor('30.00', '1.65', '31.65')}
    assert snapshot_columns(world.db, reduced) == [('S', 550, None, entries.coa[4150])] * 2


@pytest.mark.parametrize('kind', KINDS)
def test_the_same_entry_on_two_positions_and_two_entries_with_the_same_content_are_one_snapshot(world, entries, kind):
    """Mit Erlöskonto (``seven_special``) schreibt der Eintrag eine Momentaufnahme: derselbe Eintrag zweimal und zwei Einträge gleichen Inhalts sind eine."""
    twin = world.db.insert_tax_rate(label='7 % mit Sonderkonto, zweiter Eintrag (erfunden)', bp=700, kind=STANDARD, revenue_coa_id=entries.coa[4190])
    same = saved(kind, form(kind, world, [item(1, 'A', 10.0, 7, taxRateId=entries.seven_special), item(2, 'B', 20.0, 7, taxRateId=entries.seven_special),
                                          item(3, 'C', 30.0, 19)], taxMode='mixed'))
    twins = saved(kind, form(kind, world, [item(1, 'A', 10.0, 7, taxRateId=entries.seven_special), item(2, 'B', 20.0, 7, taxRateId=twin),
                                           item(3, 'C', 30.0, 19)], taxMode='mixed',
                             **number_over(kind, 101)))

    for doc_id in (same, twins):
        assert head(world.db, doc_id) == {'TaxRate': 0.19, 'MixedTaxRates': 1, **minor('60.00', '7.80', '67.80')}
        assert snapshot_columns(world.db, doc_id) == [('S', 700, None, entries.coa[4190])] * 2 + [NO_SNAPSHOT]


@pytest.mark.parametrize('kind', KINDS)
def test_the_preset_and_a_twin_without_an_account_write_no_snapshot_on_either_position(world, entries, kind):
    """``preset7`` und ``seven_twin`` (7 % ohne Erlöskonto) tragen beide der bisherige Payload: Zwei Positionen mit je einem von ihnen schreiben nichts – und widersprechen sich nicht."""
    doc_id = saved(kind, form(kind, world, [item(1, 'A', 10.0, 7, taxRateId=entries.preset7), item(2, 'B', 20.0, 7, taxRateId=entries.seven_twin),
                                            item(3, 'C', 30.0, 19)], taxMode='mixed'))

    assert head(world.db, doc_id) == {'TaxRate': 0.19, 'MixedTaxRates': 1, **minor('60.00', '7.80', '67.80')}
    assert snapshot_columns(world.db, doc_id) == [NO_SNAPSHOT] * 3


@pytest.mark.parametrize('kind', KINDS)
def test_the_rate_of_a_position_may_be_sent_as_a_fraction_too(world, entries, kind):
    doc_id = saved(kind, form(kind, world, [item(1, 'Beratung', 100.0, 0.19), item(2, 'Heft', 10.0, 0.055, taxRateId=entries.reduced)], taxMode='mixed'))

    assert head(world.db, doc_id)['MixedTaxRates'] == 1
    assert [(r['TaxRate'], r['TaxRateBp']) for r in stored(world.db, doc_id)] == [(0.19, None), (0.055, 550)]


# ═════════════════════════════════════════════════════════════════════════════
# Ein Beleg ohne taxRateId: Zeile für Zeile wie vor E3
# ═════════════════════════════════════════════════════════════════════════════

SHAPES = {
    'ein-satz-19': dict(taxRate=0.19, items=[item(1, 'A', 99.99, 19, quantity=3), item(2, 'B', 0.10, 19)]),
    'ein-satz-7': dict(taxRate=0.07, items=[item(1, 'A', 12.34, 7)]),
    'null-prozent': dict(taxRate=0, items=[item(1, 'A', 12.34, 0)]),
    'fuenf-komma-fuenf-im-kopf': dict(taxRate=0.055, items=[item(1, 'A', 0.09, 5.5)]),
    'prozent-im-kopf': dict(taxRate=19, items=[item(1, 'A', 9.99, 19)]),
    'satz-fehlt-an-der-position': dict(taxRate=0.19, items=[{'position': 1, 'quantity': 3, 'description': 'A', 'unitPrice': 0.10}]),
    'haeckchen-19-und-7': dict(taxRate=0.19, taxMode='mixed', items=two_rates()),
    'haeckchen-nur-19': dict(taxRate=0.19, taxMode='mixed', items=[item(1, 'A', 10.0, 19), item(2, 'B', 5.0, 19)]),
    'ohne-steuerausweis': dict(taxRate=0, showTax=False, items=[item(1, 'A', 99.99, 0), item(2, 'B', 0.01, 0)]),
    'ohne-steuerausweis-ohne-satz': dict(taxRate=0, showTax=False, items=[{'position': 1, 'quantity': 1, 'description': 'A', 'unitPrice': 99.99}]),
    'null-statt-id': dict(taxRate=0.19, taxRateId=None, items=[item(1, 'A', 10.0, 19, taxRateId=None)]),
}


@pytest.mark.parametrize('kind', KINDS)
@pytest.mark.parametrize('key', SHAPES)
def test_a_form_without_an_entry_writes_no_snapshot_and_the_category_s_exactly_as_before(world, kind, key):
    shape = copy.deepcopy(SHAPES[key])
    items = shape.pop('items')

    doc_id = saved(kind, form(kind, world, items, **shape))

    assert snapshot_columns(world.db, doc_id) == [NO_SNAPSHOT] * len(items)


@pytest.mark.parametrize('kind', KINDS)
def test_snapshot_keys_sent_by_a_client_are_never_stored_and_never_change_the_rounding(world, entries, kind):
    """Die Momentaufnahme kommt allein vom Eintrag der Stammdaten, nie vom Formular: Schlüssel der Positionsspalten – an der Position oder im Kopf – werden überhört. Sie ändern
    weder Art, Grund und Erlöskonto noch die Rundung (0,09 € bei 5,5 %: zweistufig 0,01 € wie bisher, nicht einstufig 0,00 €)."""
    forged = dict(tax_rate_bp=550, tax_category='E', tax_exempt_reason='gefälscht', revenue_coa_id=entries.coa[4150], taxRateBp=550, TaxRateBp=550, RevenueCoaId=1)

    doc_id = saved(kind, form(kind, world, [item(1, 'Kleinteil', 0.09, 5.5, **forged)], taxRate=0.055, **forged))

    assert snapshot_columns(world.db, doc_id) == [NO_SNAPSHOT]
    assert head(world.db, doc_id) == {'TaxRate': 0.055, 'MixedTaxRates': None, **minor('0.09', '0.01', '0.10')}


@pytest.mark.parametrize('kind', KINDS)
def test_saving_a_draft_again_without_an_entry_drops_the_snapshots_it_carried(world, entries, kind):
    """Der Nutzer wählt die Vorbelegung (die Maske schickt für sie keine ID): Die Momentaufnahme entfällt mit der Wahl."""
    doc_id = saved(kind, form(kind, world, [item(1, 'Nachhilfe', 100.0, 0)], taxRate=0, taxRateId=entries.free))
    assert snapshot_columns(world.db, doc_id) == [free_snapshot(entries)]

    resave(kind, world, doc_id, copy.deepcopy(ITEMS))

    assert snapshot_columns(world.db, doc_id) == [NO_SNAPSHOT] * 2
    assert head(world.db, doc_id) == {'TaxRate': 0.19, 'MixedTaxRates': None, **minor('250.50', '47.60', '298.10')}


@pytest.mark.parametrize('kind', KINDS)
def test_a_draft_goes_from_no_entry_to_an_entry_to_another_and_back_with_every_row_replaced(world, entries, kind):
    doc_id = saved(kind, form(kind, world, copy.deepcopy(ITEMS)))
    assert snapshot_columns(world.db, doc_id) == [NO_SNAPSHOT] * 2

    resave(kind, world, doc_id, [item(1, 'Nachhilfe', 100.0, 0)], taxRate=0, taxRateId=entries.free)
    assert snapshot_columns(world.db, doc_id) == [free_snapshot(entries)]

    resave(kind, world, doc_id, [item(1, 'Nachhilfe', 100.0, 0)], taxRate=0, taxRateId=entries.free_other)
    assert snapshot_columns(world.db, doc_id) == [free_snapshot(entries, OTHER_REASON)]

    resave(kind, world, doc_id, [item(1, 'Beratung', 100.0, 19), item(2, 'Nachhilfe', 80.0, 0, taxRateId=entries.free)], taxMode='mixed')
    assert snapshot_columns(world.db, doc_id) == [NO_SNAPSHOT, free_snapshot(entries)]
    assert head(world.db, doc_id)['MixedTaxRates'] == 1

    resave(kind, world, doc_id, copy.deepcopy(ITEMS))
    assert snapshot_columns(world.db, doc_id) == [NO_SNAPSHOT] * 2 and head(world.db, doc_id)['MixedTaxRates'] is None


# ═════════════════════════════════════════════════════════════════════════════
# Ablehnungen: genau ihr fester Satz, nichts gespeichert, keine Nummer verbraucht
# ═════════════════════════════════════════════════════════════════════════════

def test_the_new_sentences_are_the_fixed_texts_of_the_plan():
    assert TAX_CHOICE_WITHOUT_TAX == 'Ohne Umsatzsteuerausweis lässt sich kein Steuersatz aus den Stammdaten wählen.'
    assert TAX_CHOICE_HEAD_GONE == 'Der gewählte Steuersatz ist in den Stammdaten nicht mehr wählbar – bitte neu wählen.'
    assert TAX_CHOICE_GONE.format(n=2) == 'Position 2: Der gewählte Steuersatz ist in den Stammdaten nicht mehr wählbar – bitte neu wählen.'
    assert TAX_CHOICE_RATE_DIFFERS.format(n=2) == 'Position 2: Der Steuersatz passt nicht zum gewählten Eintrag der Stammdaten – bitte die Seite neu laden.'
    assert TAX_SAME_RATE_TWICE.format(n=2, rate='steuerfrei') == ('Position 2: Ein Beleg kann je Steuersatz nur einen Eintrag der Stammdaten tragen '
                                                                  '(steuerfrei) – zum Beispiel nur einen Befreiungsgrund.')


def plain_19():
    return item(1, 'Beratung', 100.0, 19)


MIXED = dict(taxMode='mixed')

# Je Fall (Schlüssel = Name des Tests): eine Funktion der Einträge, die ``(Positionen, Änderungen am Formular, erwarteter Satz)`` liefert
REFUSALS = {
    # Regel 0: die Angabe des Modus
    'kopf-id-mit-haeckchen': lambda e: (two_rates(), dict(taxMode='mixed', taxRateId=e.reduced), TAX_MODE_INVALID),
    'position-id-ohne-haeckchen': lambda e: ([item(1, 'A', 10.0, 5.5, taxRateId=e.reduced)], dict(taxRate=0.055), TAX_MODE_INVALID),
    # Regel 1: ohne Steuerausweis
    'ohne-steuer-kopf-id': lambda e: ([item(1, 'A', 10.0, 0)], dict(showTax=False, taxRate=0, taxRateId=e.free), TAX_CHOICE_WITHOUT_TAX),
    'ohne-steuer-position-id': lambda e: ([item(1, 'A', 10.0, 0, taxRateId=e.free)], dict(showTax=False, taxRate=0), TAX_CHOICE_WITHOUT_TAX),
    'ohne-steuer-haeckchen-und-position-id': lambda e: ([item(1, 'A', 10.0, 0, taxRateId=e.free)], dict(showTax=False, taxRate=0, taxMode='mixed'),
                                                        TAX_PER_ITEM_WITHOUT_TAX),
    'ohne-steuer-haeckchen-und-kopf-id': lambda e: ([item(1, 'A', 10.0, 0)], dict(showTax=False, taxRate=0, taxMode='mixed', taxRateId=e.free), TAX_MODE_INVALID),
    # Regel 2a: ein Eintrag im Kopf
    'kopf-satz-passt-nicht-zum-eintrag': lambda e: ([item(1, 'A', 10.0, 7), item(2, 'B', 10.0, 7)], dict(taxRate=0.07, taxRateId=e.reduced),
                                                    TAX_CHOICE_RATE_DIFFERS.format(n=1)),
    'zweite-position-passt-nicht': lambda e: ([item(1, 'A', 10.0, 5.5), item(2, 'B', 10.0, 7)], dict(taxRate=0.055, taxRateId=e.reduced),
                                              TAX_CHOICE_RATE_DIFFERS.format(n=2)),
    'kopf-passt-nicht-zum-eintrag': lambda e: ([item(1, 'A', 10.0, 5.5), item(2, 'B', 10.0, 5.5)], dict(taxRate=0.19, taxRateId=e.reduced),
                                               TAX_CHOICE_RATE_DIFFERS.format(n=1)),
    'steuerfreier-eintrag-mit-19-prozent': lambda e: ([item(1, 'A', 10.0, 19)], dict(taxRate=0.19, taxRateId=e.free), TAX_CHOICE_RATE_DIFFERS.format(n=1)),
    'position-ungueltig-mit-kopf-id': lambda e: ([item(1, 'A', 10.0, 5.5), item(2, 'B', 10.0, 'abc')], dict(taxRate=0.055, taxRateId=e.reduced),
                                                 TAX_ITEM_INVALID.format(n=2)),
    'kopf-ungueltig-mit-id': lambda e: ([item(1, 'A', 10.0, 5.5)], dict(taxRate='abc', taxRateId=e.reduced), TAX_RATE_INVALID),
    'unbekannter-eintrag-im-kopf': lambda e: ([item(1, 'A', 10.0, 5.5)], dict(taxRate=0.055, taxRateId=999999), TAX_CHOICE_HEAD_GONE),
    # Regel 3: ein Eintrag je Position
    'position-id-0': lambda e: ([plain_19(), item(2, 'Nachhilfe', 80.0, 0, taxRateId=0)], MIXED, TAX_CHOICE_GONE.format(n=2)),
    'position-id-unbekannt': lambda e: ([plain_19(), item(2, 'Nachhilfe', 80.0, 0, taxRateId=999999)], MIXED, TAX_CHOICE_GONE.format(n=2)),
    'position-id-kein-wert': lambda e: ([plain_19(), item(2, 'Nachhilfe', 80.0, 0, taxRateId='abc')], MIXED, TAX_CHOICE_GONE.format(n=2)),
    'position-satz-passt-nicht-zum-eintrag': lambda e: ([plain_19(), item(2, 'Heft', 10.0, 7, taxRateId=e.reduced)], MIXED, TAX_CHOICE_RATE_DIFFERS.format(n=2)),
    'position-ohne-satz-mit-id': lambda e: ([plain_19(), {'position': 2, 'quantity': 1, 'description': 'Heft', 'unitPrice': 10.0, 'taxRateId': e.reduced}], MIXED,
                                            TAX_ITEM_MISSING.format(n=2)),
    'position-satz-ungueltig-mit-id': lambda e: ([plain_19(), item(2, 'Heft', 10.0, 'abc', taxRateId=e.reduced)], MIXED, TAX_ITEM_INVALID.format(n=2)),
    'satz-ohne-eintrag-nicht-erlaubt': lambda e: ([plain_19(), item(2, 'Heft', 10.0, 5.5)], MIXED, TAX_ITEM_NOT_ALLOWED.format(n=2, rate='5,5')),
    'satz-ohne-eintrag-neben-einem-mit-eintrag': lambda e: ([item(1, 'A', 10.0, 5.5, taxRateId=e.reduced), item(2, 'Beratung', 100.0, 19), item(3, 'C', 10.0, 5.5)],
                                                            MIXED, TAX_ITEM_NOT_ALLOWED.format(n=3, rate='5,5')),
    # Je Satz höchstens eine Momentaufnahme – eine Probe je Variante
    'zwei-befreiungsgruende': lambda e: ([item(1, 'A', 80.0, 0, taxRateId=e.free), item(2, 'B', 20.0, 0, taxRateId=e.free_other)], MIXED,
                                         TAX_SAME_RATE_TWICE.format(n=2, rate='steuerfrei')),
    'steuerfrei-neben-standard-0': lambda e: ([item(1, 'A', 80.0, 0, taxRateId=e.free), item(2, 'B', 20.0, 0, taxRateId=e.zero_standard)], MIXED,
                                              TAX_SAME_RATE_TWICE.format(n=2, rate='steuerfrei')),
    'standard-0-neben-steuerfrei': lambda e: ([item(1, 'A', 80.0, 0, taxRateId=e.zero_standard), item(2, 'B', 20.0, 0, taxRateId=e.free)], MIXED,
                                              TAX_SAME_RATE_TWICE.format(n=2, rate='steuerfrei')),
    'mit-gegen-ohne': lambda e: ([plain_19(), item(2, 'B', 20.0, 19, taxRateId=e.nineteen_special)], MIXED, TAX_SAME_RATE_TWICE.format(n=2, rate='19 %')),
    'ohne-gegen-mit': lambda e: ([item(1, 'B', 20.0, 19, taxRateId=e.nineteen_special), item(2, 'A', 100.0, 19)], MIXED, TAX_SAME_RATE_TWICE.format(n=2, rate='19 %')),
    # Die Vorbelegung mit ID schreibt nichts (``legacy_expressible``), „mit ID gegen ohne“ ist also kein Widerspruch (``test_two_positions_of_one_rate_with_the_preset_…``) – gegen einen Eintrag
    # mit Erlöskonto aber schon: Verschieden ist, was gespeichert wird
    'vorbelegung-mit-id-gegen-eintrag-mit-konto': lambda e: ([item(1, 'A', 100.0, 19, taxRateId=e.preset19), item(2, 'B', 20.0, 19, taxRateId=e.nineteen_special)], MIXED,
                                                             TAX_SAME_RATE_TWICE.format(n=2, rate='19 %')),
    'eintrag-mit-konto-gegen-vorbelegung-mit-id': lambda e: ([item(1, 'B', 20.0, 19, taxRateId=e.nineteen_special), item(2, 'A', 100.0, 19, taxRateId=e.preset19)], MIXED,
                                                             TAX_SAME_RATE_TWICE.format(n=2, rate='19 %')),
    'zwei-erloeskonten-bei-7-prozent': lambda e: ([item(1, 'A', 10.0, 7, taxRateId=e.preset7), item(2, 'B', 20.0, 7, taxRateId=e.seven_special)], MIXED,
                                                  TAX_SAME_RATE_TWICE.format(n=2, rate='7 %')),
    'dritte-position-ist-die-genannte': lambda e: ([plain_19(), item(2, 'B', 10.0, 7), item(3, 'C', 20.0, 19, taxRateId=e.nineteen_special)], MIXED,
                                                   TAX_SAME_RATE_TWICE.format(n=3, rate='19 %')),
}


@pytest.mark.parametrize('kind', KINDS)
@pytest.mark.parametrize('case', REFUSALS)
def test_every_refusal_comes_with_exactly_its_sentence_and_nothing_is_saved(world, entries, kind, case):
    items, over, sentence = REFUSALS[case](entries)

    text = refused(kind, world, form(kind, world, items, **over))

    assert text == sentence


@pytest.mark.parametrize('kind', KINDS)
@pytest.mark.parametrize('case', REFUSALS)
def test_a_refused_update_leaves_the_stored_draft_as_it_was(world, entries, kind, case):
    doc_id = saved(kind, form(kind, world, [item(1, 'Nachhilfe', 100.0, 0)], taxRate=0, taxRateId=entries.free))
    items, over, sentence = REFUSALS[case](entries)
    over = {**over, **({'invoiceId': doc_id, 'invoiceNumber': columns(world.db, doc_id)['InvoiceNumber']} if kind == 'invoice' else {'quoteId': doc_id})}

    text = refused(kind, world, form(kind, world, items, **over))

    assert text == sentence
    assert snapshot_columns(world.db, doc_id) == [free_snapshot(entries)]


BAD_IDS = [0, -1, 999999, 'abc', '', '01', ' 1', '1 ', '0', 1.0, 1.5, True, False, [1], {'id': 1}, 2 ** 63, 10 ** 30, '9' * 30, '١']


@pytest.mark.parametrize('kind', KINDS)
@pytest.mark.parametrize('bad', BAD_IDS, ids=lambda value: json.dumps(value))
def test_a_value_that_is_no_id_is_refused_in_the_head_and_at_a_position_with_the_sentence_of_a_vanished_entry(world, entries, kind, bad):
    in_head = refused(kind, world, form(kind, world, [item(1, 'Nachhilfe', 10.0, 0)], taxRate=0, taxRateId=bad))
    at_position = refused(kind, world, form(kind, world, [item(1, 'Beratung', 100.0, 19), item(2, 'Nachhilfe', 10.0, 0, taxRateId=bad)], taxMode='mixed'))

    assert in_head == TAX_CHOICE_HEAD_GONE
    assert at_position == TAX_CHOICE_GONE.format(n=2)
    for leak in ('Traceback', 'Exception', 'Error', 'Decimal'):
        assert leak not in in_head + at_position


def make_inactive(db, entry_id):
    db.set_tax_rate_active(entry_id, False)


def make_deleted(db, entry_id):
    db.delete_tax_rate(entry_id)


def make_invalid(db, entry_id):
    run_sql(db, "UPDATE TaxRates SET Kind = 'frei' WHERE ID = ?", entry_id)             # wie ein Eingriff über die SQL-Konsole: eine Art, die es nicht gibt


@pytest.mark.parametrize('kind', KINDS)
@pytest.mark.parametrize('spoil', [make_inactive, make_deleted, make_invalid], ids=['abgeschaltet', 'geloescht', 'ungueltig'])
def test_an_entry_that_is_no_longer_selectable_is_refused_in_the_head_and_at_a_position(world, entries, kind, spoil):
    spoil(world.db, entries.free)

    in_head = refused(kind, world, form(kind, world, [item(1, 'Nachhilfe', 10.0, 0)], taxRate=0, taxRateId=entries.free))
    at_position = refused(kind, world, form(kind, world, [item(1, 'Beratung', 100.0, 19), item(2, 'Nachhilfe', 10.0, 0, taxRateId=entries.free)], taxMode='mixed'))

    assert in_head == TAX_CHOICE_HEAD_GONE and at_position == TAX_CHOICE_GONE.format(n=2)
    # ein anderer Eintrag mit demselben Satz bleibt wählbar
    saved(kind, form(kind, world, [item(1, 'Nachhilfe', 10.0, 0)], taxRate=0, taxRateId=entries.free_other))


@pytest.mark.parametrize('kind', KINDS)
def test_an_entry_whose_rate_changed_between_loading_and_saving_is_refused(world, entries, kind):
    """Die Maske hat 5,5 % mit dem Eintrag geladen; inzwischen ist er auf 6 % gestellt: Das Speichern lehnt ab (Seite neu laden), statt 6 % unter 5,5 % zu führen."""
    world.db.update_tax_rate(entries.reduced, label='Ermäßigt (jetzt 6 %)', bp=600, kind=STANDARD, revenue_coa_id=entries.coa[4150])

    in_head = refused(kind, world, form(kind, world, [item(1, 'Heft', 10.0, 5.5)], taxRate=0.055, taxRateId=entries.reduced))
    at_position = refused(kind, world, form(kind, world, [item(1, 'Beratung', 100.0, 19), item(2, 'Heft', 10.0, 5.5, taxRateId=entries.reduced)], taxMode='mixed'))

    assert in_head == TAX_CHOICE_RATE_DIFFERS.format(n=1)
    assert at_position == TAX_CHOICE_RATE_DIFFERS.format(n=2)
    saved(kind, form(kind, world, [item(1, 'Heft', 10.0, 6)], taxRate=0.06, taxRateId=entries.reduced))                  # mit dem neuen Satz geht es


@pytest.mark.parametrize('kind', KINDS)
@pytest.mark.parametrize('mode', ['MIXED', 'x', 1, True, []], ids=lambda value: json.dumps(value))
def test_the_mode_is_still_checked_first_and_an_entry_does_not_change_it(world, entries, kind, mode):
    text = refused(kind, world, form(kind, world, [item(1, 'A', 10.0, 0)], taxMode=mode, taxRate=0, taxRateId=entries.free))

    assert text == TAX_MODE_INVALID


@pytest.mark.parametrize('kind', KINDS)
def test_the_existing_checks_keep_their_text_and_order_for_a_form_without_an_entry(world, entries, kind):
    """Dieselben Sätze wie vor E3 – die Positionen tragen hier ihre Zeile (``position``), sonst die laufende Nummer."""
    assert refused(kind, world, form(kind, world, [item(1, 'A', 1.0, 19), item(2, 'B', 1.0, 7)])) == TAX_ITEM_DIFFERS.format(n=2)
    assert refused(kind, world, form(kind, world, two_rates()[:1] + [item(2, 'B', 1.0, 5)], taxMode='mixed')) == TAX_ITEM_NOT_ALLOWED.format(n=2, rate='5')
    assert refused(kind, world, form(kind, world, [item(1, 'A', 1.0, 19)], taxRate='abc')) == TAX_RATE_INVALID
    assert refused(kind, world, form(kind, world, [item(1, 'A', 1.0, 19)], showTax=False, taxRate=0, taxMode='mixed')) == TAX_PER_ITEM_WITHOUT_TAX
    assert refused(kind, world, form(kind, world, [item(1, 'A', 1.0, 19)], taxMode='x')) == TAX_MODE_INVALID


# ═════════════════════════════════════════════════════════════════════════════
# Kaputte Positionen: derselbe Satz wie in 2.7.0 (Befund H1)
# ═════════════════════════════════════════════════════════════════════════════

# Der allgemeine Satz von ``_save_error_text``: Der erste Zugriff auf eine Position (``.get`` an einem Nicht-Objekt) scheitert mit einem ``AttributeError`` bzw. ``TypeError``
GENERAL = 'Speichern fehlgeschlagen: Beträge, Mengen oder Positionen sind ungültig – es wurde nichts gespeichert.'
BROKEN_ITEMS = [[1], {'a': 1}, [[]]]                       # keine Liste von Objekten: eine Zahl, ein Objekt statt einer Liste, eine Liste in der Liste


@pytest.mark.parametrize('kind', KINDS)
@pytest.mark.parametrize('items', BROKEN_ITEMS, ids=json.dumps)
@pytest.mark.parametrize('over, sentence', [
    pytest.param({}, GENERAL, id='kopf-in-ordnung'),
    pytest.param(dict(taxRate='abc'), TAX_RATE_INVALID, id='kopfsatz-ungueltig'),
    pytest.param(dict(taxRate=None), TAX_RATE_INVALID, id='kopfsatz-fehlt'),
    pytest.param(dict(showTax=False, taxRate=0, taxMode='mixed'), TAX_PER_ITEM_WITHOUT_TAX, id='je-position-ohne-steuerausweis'),
    pytest.param(dict(taxMode='x'), TAX_MODE_INVALID, id='modus-ungueltig'),
    pytest.param(dict(taxMode='mixed'), GENERAL, id='je-position'),
    pytest.param(dict(taxRate=None, taxMode='mixed'), GENERAL, id='je-position-ohne-kopfsatz'),
    pytest.param(dict(showTax=False, taxRate=0), GENERAL, id='ohne-steuerausweis'),
    pytest.param(dict(taxRateId=1), GENERAL, id='id-im-kopf'),
])
def test_items_that_are_no_list_of_objects_get_the_sentence_of_2_7_0(world, entries, kind, items, over, sentence):
    """Seit 2.8.0 las ``_tax_fields`` die IDs der Positionen als Erstes – ein Nicht-Objekt scheiterte dort, und die Eingabe bekam den allgemeinen Satz statt des Satzes, den sie in 2.7.0
    bekam (Kopfsatz ungültig, „je Position“ ohne Steuerausweis): Die Angaben zum Steuersatz werden wieder vor dem ersten Zugriff auf eine Position geprüft. Abgelehnt wird in jedem Fall,
    nichts gespeichert (``refused``)."""
    assert refused(kind, world, form(kind, world, copy.deepcopy(items), **over)) == sentence


@pytest.mark.parametrize('kind', KINDS)
@pytest.mark.parametrize('over, sentence', [
    pytest.param({}, GENERAL, id='kopf-in-ordnung'),
    pytest.param(dict(taxRate='abc'), TAX_RATE_INVALID, id='kopfsatz-ungueltig'),
])
def test_an_object_with_an_id_next_to_a_non_object_is_no_list_of_objects_either(world, entries, kind, over, sentence):
    """Ein Objekt mit ID neben einem Nicht-Objekt: Die ID wird nicht gelesen (``item_choice``), der Beleg scheitert wie jeder mit einer kaputten Position."""
    items = [item(1, 'Heft', 10.0, 5.5, taxRateId=entries.reduced), 1]

    assert refused(kind, world, form(kind, world, items, **over)) == sentence


@pytest.mark.parametrize('kind', KINDS)
def test_the_new_checks_of_the_ids_are_not_weakened_by_the_order_of_2_7_0(world, entries, kind):
    """Bei einer Liste von Objekten bleibt es bei den Prüfungen der IDs – vor dem Kopfsatz (``TAX_MODE_INVALID``) wie seit 2.8.0."""
    assert refused(kind, world, form(kind, world, [item(1, 'A', 10.0, 5.5, taxRateId=entries.reduced)], taxRate='abc')) == TAX_MODE_INVALID
    assert refused(kind, world, form(kind, world, [item(1, 'A', 10.0, 0, taxRateId=entries.free)], showTax=False, taxRate=0)) == TAX_CHOICE_WITHOUT_TAX
    assert refused(kind, world, form(kind, world, [item(1, 'A', 10.0, 0)], showTax=False, taxRate=0, taxRateId=entries.free)) == TAX_CHOICE_WITHOUT_TAX


# ═════════════════════════════════════════════════════════════════════════════
# Der Eintrag wird in der Schreibtransaktion geprüft (Befund N3)
# ═════════════════════════════════════════════════════════════════════════════

def lose_the_account_after_reading(monkeypatch, db, how, entry_id, coa_id):
    """Der Haken: ``get_tax_rate`` liefert den Eintrag und verliert danach – genau einmal – sein Erlöskonto, wie ein zweiter Nutzer zwischen dem Lesen des Eintrags (``_tax_fields``, ohne
    Sperre) und dem Schreiben des Speicherns: ``how`` ``deleted`` (der Eintrag und das Konto sind weg) oder ``renumbered`` (das Konto liegt außerhalb von 4000–4999). Die Antwort des Hakens
    ist ``seen``; ``seen['dump']`` ist der Stand ALLER Tabellen unmittelbar danach – was die Ablehnung nicht mehr ändern darf."""
    seen = {}
    real = db.get_tax_rate

    def read_then_lose(requested, conn=None):
        found = real(requested, conn)
        monkeypatch.setattr(db, 'get_tax_rate', real)                       # nur das erste Lesen
        if how == 'deleted':
            run_sql(db, 'DELETE FROM TaxRates WHERE ID = ?', entry_id)
            run_sql(db, 'DELETE FROM ChartOfAccounts WHERE ID = ?', coa_id)
        else:
            run_sql(db, 'UPDATE ChartOfAccounts SET AccountNumber = 84150 WHERE ID = ?', coa_id)
        seen['dump'] = everything(db)
        return found

    monkeypatch.setattr(db, 'get_tax_rate', read_then_lose)
    return seen


@pytest.mark.parametrize('kind', KINDS)
@pytest.mark.parametrize('write', ['new', 'update'])
@pytest.mark.parametrize('place', ['head', 'position'])
@pytest.mark.parametrize('how', ['deleted', 'renumbered'])
def test_an_account_lost_between_reading_the_entry_and_writing_the_positions_is_refused_by_the_transaction(world, entries, monkeypatch, kind, write, place, how):
    """Ohne die Prüfung in der Transaktion trüge der Entwurf ``InvoiceItems.RevenueCoaId`` eines Kontos, das es nicht mehr gibt (die Spalte hat keinen Fremdschlüssel). Jetzt: ``TAX_CHOICE_CHANGED``,
    nichts gespeichert (keine Zeile, keine Nummer, kein Zähler, kein Protokoll – der Stand aller Tabellen bleibt), und der nächste Versuch bekommt den gewöhnlichen Satz „nicht mehr wählbar“."""
    db = world.db
    stored = saved(kind, form(kind, world, copy.deepcopy(ITEMS))) if write == 'update' else None
    if place == 'head':
        items, over = [item(1, 'Heft', 10.0, 5.5), item(2, 'Buch', 20.0, 5.5)], dict(taxRate=0.055, taxRateId=entries.reduced)
        expected_next = TAX_CHOICE_HEAD_GONE
    else:
        items, over = [item(1, 'Beratung', 100.0, 19), item(2, 'Heft', 10.0, 5.5, taxRateId=entries.reduced)], dict(MIXED)
        expected_next = TAX_CHOICE_GONE.format(n=2)
    if stored is not None:
        over.update({'invoiceId': stored, 'invoiceNumber': columns(db, stored)['InvoiceNumber']} if kind == 'invoice' else {'quoteId': stored})
    seen = lose_the_account_after_reading(monkeypatch, db, how, entries.reduced, entries.coa[4150])

    answer, _status = post(kind, form(kind, world, items, **over))

    assert 'dump' in seen, 'der Haken lief nicht'
    assert answer == {'success': False, 'error': TAX_CHOICE_CHANGED}
    assert everything(db) == seen['dump']
    assert refused(kind, world, form(kind, world, items, **over)) == expected_next


def test_the_sentence_of_the_race_is_the_fixed_text_of_the_plan():
    assert TAX_CHOICE_CHANGED == 'Ein gewählter Steuersatz wurde inzwischen geändert – bitte die Seite neu laden und den Steuersatz neu wählen.'


@pytest.mark.parametrize('kind', KINDS)
def test_a_save_whose_accounts_are_all_there_goes_through_the_check_and_writes_what_it_always_did(world, entries, kind):
    """Die Prüfung nimmt die Schreibsperre vor dem Schreiben (``BEGIN IMMEDIATE``), wenn eine Momentaufnahme ein Konto trägt – der Rest läuft unverändert: Nummer, Zeilen, Momentaufnahmen."""
    doc_id = saved(kind, form(kind, world, [item(1, 'Beratung', 100.0, 19), item(2, 'Nachhilfe', 80.0, 0, taxRateId=entries.free)], taxMode='mixed'))
    again = resave(kind, world, doc_id, [item(1, 'Heft', 10.0, 5.5)], taxRate=0.055, taxRateId=entries.reduced)

    assert again == doc_id
    assert snapshot_columns(world.db, doc_id) == [('S', 550, None, entries.coa[4150])]


@pytest.mark.parametrize('rows', [[], [None], [{}], [{'revenue_coa_id': None}], [None, {'tax_rate_bp': 550, 'revenue_coa_id': None}, {'description': 'Heft'}]],
                         ids=['leer', 'none', 'ohne-schluessel', 'konto-none', 'ohne-konto'])
def test_without_a_revenue_account_in_the_rows_the_check_takes_no_lock_and_reads_nothing(world, rows):
    """Der Beleg ohne Eintrag und die Vorbelegung (ohne Erlöskonto): Der Ablauf bleibt der bisherige – die Prüfung fasst die Verbindung nicht an."""
    class Untouchable:
        in_transaction = False

        def execute(self, *args):
            raise AssertionError(f'die Verbindung wurde angefasst: {args}')

    handlers._refuse_gone_revenue_accounts(world.db, SimpleNamespace(conn=Untouchable()), rows)


def test_missing_revenue_coas_names_the_accounts_that_are_no_income_account_on_the_connection(world, entries):
    """Dieselbe Regel wie beim Speichern eines Eintrags (``_income_coas``): im Kontenrahmen, 4000–4999, kein Wartekonto."""
    db = world.db
    expense = db.get_coa_id_by_account_number(6300)
    waiting = db.get_coa_id_by_account_number(4340)
    assert expense is not None and waiting is not None

    with contextlib.closing(db._get_connection()) as conn:
        assert db.missing_revenue_coas(conn, [entries.coa[4120], entries.coa[4150]]) == set()
        assert db.missing_revenue_coas(conn, {entries.coa[4120], 999999, expense, waiting}) == {999999, expense, waiting}
        assert db.missing_revenue_coas(conn, []) == set()


# ═════════════════════════════════════════════════════════════════════════════
# „Position n“ ist die Zeile der Maske
# ═════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize('position, number, expected', [
    (1, 1, 1), (3, 2, 3), (9999, 1, 9999), (2, 5, 2),                                  # eine ganze Zahl von 1 bis 9999: die Zeile der Maske
    (0, 2, 2), (-1, 2, 2), (10000, 2, 2), (3.0, 2, 2), ('3', 2, 2), (True, 2, 2), (None, 2, 2), ([3], 2, 2), (3.5, 2, 2)])       # sonst die laufende Nummer
def test_the_position_number_is_the_row_of_the_mask_when_it_is_a_whole_number_from_1_to_9999(position, number, expected):
    assert _position_number({'position': position}, number) == expected


def test_a_position_without_the_key_is_numbered_by_its_place():
    assert _position_number({}, 4) == 4


@pytest.mark.parametrize('kind', KINDS)
def test_every_message_names_the_row_of_the_mask_when_the_numbers_have_a_gap(world, entries, kind):
    """Die Maske schickt die sichtbare Zeile: Löscht der Nutzer die zweite, heißen die Zeilen 1 und 3 – und so nennt sie jede Meldung."""
    gap = lambda extra=None: {'position': 3, 'quantity': 1, 'unit': 'C62', 'description': 'Dritte', 'unitPrice': 1.0, **(extra or {})}   # noqa: E731
    first = item(1, 'Erste', 100.0, 19)

    assert refused(kind, world, form(kind, world, [first, gap({'taxRate': 7})])) == 'Position 3: Der Steuersatz weicht vom Steuersatz des Belegs ab.'
    assert refused(kind, world, form(kind, world, [first, gap({'taxRate': 'abc'})])) == 'Position 3: Ungültiger Steuersatz.'
    assert refused(kind, world, form(kind, world, [first, gap()], taxMode='mixed')) == 'Position 3: Bitte einen Steuersatz wählen.'
    assert refused(kind, world, form(kind, world, [first, gap({'taxRate': 5})], taxMode='mixed')) == (
        'Position 3: Der Steuersatz 5 % ist in einem Beleg mit Steuersatz je Position nicht wählbar.')
    assert refused(kind, world, form(kind, world, [first, gap({'taxRate': 0, 'taxRateId': 0})], taxMode='mixed')) == (
        'Position 3: Der gewählte Steuersatz ist in den Stammdaten nicht mehr wählbar – bitte neu wählen.')
    assert refused(kind, world, form(kind, world, [first, gap({'taxRate': 7, 'taxRateId': entries.reduced})], taxMode='mixed')) == (
        'Position 3: Der Steuersatz passt nicht zum gewählten Eintrag der Stammdaten – bitte die Seite neu laden.')
    assert refused(kind, world, form(kind, world, [first, gap({'taxRate': 19, 'taxRateId': entries.nineteen_special})], taxMode='mixed')) == (
        'Position 3: Ein Beleg kann je Steuersatz nur einen Eintrag der Stammdaten tragen (19 %) – zum Beispiel nur einen Befreiungsgrund.')
    assert refused(kind, world, form(kind, world, [item(1, 'Erste', 10.0, 5.5), gap({'taxRate': 7})], taxRate=0.055, taxRateId=entries.reduced)) == (
        'Position 3: Der Steuersatz passt nicht zum gewählten Eintrag der Stammdaten – bitte die Seite neu laden.')


@pytest.mark.parametrize('kind', KINDS)
def test_a_head_that_differs_names_the_first_row_whatever_its_number(world, entries, kind):
    start = {'position': 5, 'quantity': 1, 'unit': 'C62', 'description': 'Fünfte', 'unitPrice': 1.0, 'taxRate': 19}

    assert refused(kind, world, form(kind, world, [start], taxRate=0.07)) == 'Position 5: Der Steuersatz weicht vom Steuersatz des Belegs ab.'
    assert refused(kind, world, form(kind, world, [{**start, 'taxRate': 5.5}], taxRate=0.19, taxRateId=entries.reduced)) == (
        'Position 5: Der Steuersatz passt nicht zum gewählten Eintrag der Stammdaten – bitte die Seite neu laden.')


@pytest.mark.parametrize('kind', KINDS)
@pytest.mark.parametrize('position', [0, 10000, 3.0, '3', True, None])
def test_a_position_number_that_is_no_row_falls_back_to_the_running_number(world, entries, kind, position):
    first = item(1, 'Erste', 100.0, 19)
    odd = {'position': position, 'quantity': 1, 'unit': 'C62', 'description': 'Zweite', 'unitPrice': 1.0, 'taxRate': 7}

    assert refused(kind, world, form(kind, world, [first, odd])) == 'Position 2: Der Steuersatz weicht vom Steuersatz des Belegs ab.'


# ═════════════════════════════════════════════════════════════════════════════
# Die Zahl, die ein Eintrag liefert, und die Summen
# ═════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize('value, expected', [
    (1, 1), (42, 42), (2 ** 63 - 1, 2 ** 63 - 1), ('7', 7), ('9223372036854775807', 2 ** 63 - 1),
    (0, 0), (-1, 0), (2 ** 63, 0), ('0', 0), ('01', 0), (' 7', 0), ('7 ', 0), ('+7', 0), ('-7', 0), ('7.0', 0), ('', 0), ('abc', 0), ('٧', 0),
    ('9' * 20, 0), (7.0, 0), (1.5, 0), (True, 0), (False, 0), (None, 0), ([7], 0), ({}, 0)])
def test_an_id_is_read_like_the_strict_id_of_the_json_routes_and_anything_else_is_zero(value, expected):
    assert _choice_id(value) == expected


def test_the_totals_of_a_position_with_a_snapshot_are_rounded_once_and_every_other_exactly_as_before():
    rows = [{'quantity': 1, 'unitPrice': 0.09, 'taxRate': 5.5}]
    plain = recompute_invoice_totals(rows, D(0))

    assert plain[1] == D('0.01')                                                                   # zweistufig, wie bisher
    assert recompute_invoice_totals(rows, D(0), [None]) == plain == recompute_invoice_totals(rows, D(0), None)
    assert recompute_invoice_totals(rows, D(0), [{'tax_rate_bp': 550}])[1] == D('0.00')           # einstufig
    assert recompute_invoice_totals(rows, D(0), [{'tax_category': 'S'}]) == plain                  # eine Momentaufnahme ohne Satz ist keine


def test_the_snapshot_never_comes_from_the_form_only_from_the_list_the_caller_passes():
    """Ein Schlüssel ``tax_rate_bp`` in der Position des Formulars (ein Client kann ihn schicken) beeinflusst die Rundung nie."""
    rows = [{'quantity': 1, 'unitPrice': 0.09, 'taxRate': 5.5, 'tax_rate_bp': 550}]

    assert recompute_invoice_totals(rows, D(0))[1] == D('0.01')


def test_a_group_rounds_once_only_if_all_its_positions_carry_the_snapshot():
    """Zwei Positionen mit 0,04 € und 0,05 € bei 5,5 %: Die Gruppe hat 0,09 € netto – einstufig 0,00 € Steuer, zweistufig 0,01 €. Trägt nur eine der beiden die
    Momentaufnahme, rechnet die Gruppe wie bisher."""
    rows = [{'quantity': 1, 'unitPrice': 0.04, 'taxRate': 5.5}, {'quantity': 1, 'unitPrice': 0.05, 'taxRate': 5.5}]

    assert recompute_invoice_totals(rows, D(0), [{'tax_rate_bp': 550}] * 2)[1] == D('0.00')
    assert recompute_invoice_totals(rows, D(0), [{'tax_rate_bp': 550}, None])[1] == D('0.01')
    assert recompute_invoice_totals(rows, D(0), [None, {'tax_rate_bp': 550}])[1] == D('0.01')
    assert recompute_invoice_totals(rows, D(0), None)[1] == D('0.01')


def random_document(rng, e):
    """Ein Beleg aus Zufall: 1 bis 4 Sätze, je Satz EINE Wahl (Eintrag oder eine Position wie bisher), 1 bis 7 Positionen mit Beträgen in Cent und Mengen mit Nachkommastellen.
    Zurück: die Positionen."""
    options = [(D(19), [None, e.preset19, e.nineteen_special]), (D(7), [None, e.preset7, e.seven_special, e.seven_twin]),
               (D(0), [e.free, e.zero_standard]), (D('5.5'), [e.reduced]), (D('7.8'), [e.seven_eight]), (D('19.25'), [e.nineteen_quarter])]
    chosen = [(pct, rng.choice(ids)) for pct, ids in rng.sample(options, rng.randint(1, 4))]
    items = []
    for position in range(1, rng.randint(1, 7) + 1):
        pct, entry_id = rng.choice(chosen)
        price = rng.choice([0.09, 0.01, 0.05, round(rng.uniform(0.01, 999.99), 2), round(rng.uniform(1, 20), 2)])
        extra = {} if entry_id is None else {'taxRateId': entry_id}
        items.append(item(position, f'Posten {position}', price, float(pct), quantity=rng.choice([1, 1, 2, 3, 0.5, 1.5, 0.25, 10]), **extra))
    return items


def expected_tax_in_minor(db, doc_id):
    """Die Steuer des Belegs unabhängig gerechnet (EN 16931, BR-CO-17): je Satz aus der Summe der Nettobeträge, EINMAL auf den Cent, halb weg von null."""
    nets = {}
    for row in stored(db, doc_id):
        pct = D(str(row['TaxRate'])) if row['TaxRateBp'] is None else D(row['TaxRateBp']) / 100
        nets[pct] = nets.get(pct, 0) + row['TotalNet']
    return sum(int((D(net) * pct / 10000).quantize(D(1), rounding=ROUND_HALF_UP)) * 100 for pct, net in nets.items())


@pytest.mark.parametrize('kind', KINDS)
def test_for_random_documents_the_stored_sums_are_the_groups_of_the_stored_positions_to_the_cent(world, entries, kind):
    """Die Eigenschaft: die gespeicherten Summen sind auf den Minor-Wert die Gruppen, die ``item_groups`` aus den gespeicherten Positionen rechnet (die Quelle der E-Rechnung, des PDF
    und der Buchungen), und die Steuer ist die der Norm (BR-CO-17) – auch bei 5,5 %, 7,8 %, 19,25 % und mit dem Fall 0,09 €. ``mixed_groups`` – was das Buchen liest – lehnt keinen ab."""
    rng = random.Random(2800)
    for count in range(300):
        items = random_document(rng, entries)
        suffix = {'invoiceNumber': f'26R{1000 + count}'} if kind == 'invoice' else {'quoteNumber': f'26A{1000 + count}'}
        doc_id = saved(kind, form(kind, world, items, taxMode='mixed', **NO_SKONTO, **suffix))

        row = columns(world.db, doc_id)
        rows = stored(world.db, doc_id)
        pairs = [(r['TaxRate'], r['TotalNet'], r['TaxRateBp']) for r in rows]
        groups = item_groups(pairs)
        assert (row['SumNet'], row['TaxAmount'], row['SumGross']) == (sum(g.net for g in groups), sum(g.tax for g in groups), sum(g.gross for g in groups)), (count, items)
        assert row['TaxAmount'] == expected_tax_in_minor(world.db, doc_id), (count, items)
        assert row['SumNet'] == sum(r['TotalNet'] for r in rows)
        assert [g.pct for g in mixed_groups(row['SumNet'], row['TaxAmount'], row['SumGross'], pairs)] == [g.pct for g in groups]
        assert row['MixedTaxRates'] == (1 if len(groups) > 1 else None), (count, items)


# ═════════════════════════════════════════════════════════════════════════════
# Die Vorbelegung mit ID ergibt dieselben Summen, dasselbe PDF und dieselbe E-Rechnung wie der heutige Payload
# ═════════════════════════════════════════════════════════════════════════════

def documents_of(kind, world, doc_id):
    """Was der Beleg an Dokumenten ergibt: bei einer Rechnung PDF (Content-Streams), eingebettete CII und – falls es sie gibt – UBL, bei einem Angebot die PDF."""
    if kind == 'invoice':
        result = generate_invoice_documents(world.db, doc_id)
        assert result['pdf_path'] and (result['error'] is None or result['incomplete']), result
        return document_facts(world.db, doc_id)
    pdf, _path = generate_quote_pdf(world.db, doc_id)
    return {'pdf': pdf_facts(pdf)}


def random_preset_document(rng):
    """19 % und 7 % (die Vorbelegung) wie die Maske sie schickt: ein Satz oder zwei, 1 bis 6 Positionen. Zurück: ``(Positionen, Satz je Position)``."""
    rates = [rng.choice((19, 7)) for _ in range(rng.randint(1, 6))]
    items = [item(position, f'Posten {position}', rng.choice([0.09, 0.01, round(rng.uniform(0.01, 500), 2), round(rng.uniform(1, 30), 2)]), rate,
                  quantity=rng.choice([1, 2, 3, 0.5, 1.5, 10])) for position, rate in enumerate(rates, 1)]
    return items, rates


DOCUMENT_CHUNKS, DOCUMENT_CASES = 5, 60            # 5 × 60 = 300 Belege je Art; jeder kostet zwei Mal ein PDF samt E-Rechnung – in Stücken, damit die Prozesse der CI sie teilen


@pytest.mark.parametrize('chunk', range(DOCUMENT_CHUNKS))
@pytest.mark.parametrize('kind', KINDS)
def test_the_preset_with_its_id_gives_the_same_sums_the_same_pdf_and_the_same_e_invoice_as_todays_form(world, entries, kind, chunk):
    rng = random.Random(2801 + chunk)
    by_rate = {19: entries.preset19, 7: entries.preset7}
    for count in range(DOCUMENT_CASES):
        items, rates = random_preset_document(rng)
        suffix = number_over(kind, 2000 + count)
        single = len(set(rates)) == 1
        today = {'taxRate': rates[0] / 100} if single else {'taxMode': 'mixed'}        # das Formular von heute: ohne oder mit dem Häkchen
        doc_id = saved(kind, form(kind, world, copy.deepcopy(items), **today, **NO_SKONTO, **suffix))
        before = (head(world.db, doc_id), [(r['Position'], r['TotalNet'], r['TaxRate']) for r in stored(world.db, doc_id)], documents_of(kind, world, doc_id))

        with_ids = {'taxRateId': by_rate[rates[0]]} if single and count % 2 == 0 else {'taxMode': 'mixed'}      # Kopf-ID oder ID je Position
        send = [{**row, 'taxRateId': by_rate[rate]} if 'taxMode' in with_ids else row for row, rate in zip(copy.deepcopy(items), rates)]
        resave(kind, world, doc_id, send, **NO_SKONTO, **({'taxRate': rates[0] / 100} if 'taxRateId' in with_ids else {}), **with_ids)
        after = (head(world.db, doc_id), [(r['Position'], r['TotalNet'], r['TaxRate']) for r in stored(world.db, doc_id)], documents_of(kind, world, doc_id))

        assert after == before, (count, items, with_ids)
        assert snapshot_columns(world.db, doc_id) == [NO_SNAPSHOT] * len(items)               # und keine Position trägt eine Momentaufnahme: Die Vorbelegung braucht keine (2.8.2)


# ═════════════════════════════════════════════════════════════════════════════
# Die Sperre einer versendeten Rechnung vergleicht die Momentaufnahme mit
# ═════════════════════════════════════════════════════════════════════════════

LEGACY_ROW = ('Beratung', 1, 'C62', 1000000, 19.0)                          # eine gespeicherte Position mit den ersten fünf Werten (der Aufruf von früher)


def wanted(**snap):
    """Eine Position des Formulars (``insert_invoice_item``) mit den genannten Werten der Momentaufnahme."""
    return {'description': 'Beratung', 'quantity': 1, 'unit': 'C62', 'price_per_unit': 100.0, 'tax_rate': 19.0, **snap}


@pytest.mark.parametrize('stored_row, row, differs', [
    (LEGACY_ROW, wanted(), False),                                                                                   # der Aufruf von früher: ohne Momentaufnahme
    (LEGACY_ROW + ('S', None, None, None), wanted(), False),                                                         # gespeichert ohne, neu ohne
    (LEGACY_ROW + ('E', None, None, None), wanted(tax_category='S'), False),                                         # ``'E'`` allein ist keine Momentaufnahme (§ 19-Beispiel)
    (LEGACY_ROW + ('S', 550, None, 12), wanted(), True),                                                             # gespeichert mit, neu ohne
    (LEGACY_ROW + ('S', None, None, None), wanted(tax_rate_bp=550, tax_category='S', revenue_coa_id=12), True),      # gespeichert ohne, neu mit
    (LEGACY_ROW + ('S', 550, None, 12), wanted(tax_rate_bp=550, tax_category='S', revenue_coa_id=12), False),        # dieselbe
    (LEGACY_ROW + ('S', 550, None, 12), wanted(tax_rate_bp='550', tax_category='S', revenue_coa_id='12'), False),    # als Text geschickt: derselbe Wert
    (LEGACY_ROW + ('S', 550, None, 12), wanted(tax_rate_bp=600, tax_category='S', revenue_coa_id=12), True),         # anderer Satz
    (LEGACY_ROW + ('S', 550, None, 12), wanted(tax_rate_bp=550, tax_category='E', revenue_coa_id=12), True),         # andere Art
    (LEGACY_ROW + ('S', 550, None, 12), wanted(tax_rate_bp=550, tax_category='S', revenue_coa_id=13), True),         # anderes Erlöskonto
    (LEGACY_ROW + ('S', 550, None, 12), wanted(tax_rate_bp=550, tax_category='S'), True),                            # Erlöskonto fehlt
    (LEGACY_ROW + ('E', 0, 'Grund A', 12), wanted(tax_rate_bp=0, tax_category='E', tax_exempt_reason='Grund B', revenue_coa_id=12), True),    # anderer Grund
    (LEGACY_ROW + ('E', 0, 'Grund  A', 12), wanted(tax_rate_bp=0, tax_category='E', tax_exempt_reason='Grund A', revenue_coa_id=12), False),   # Leerraum zählt nicht
])
def test_the_comparison_of_positions_looks_at_the_four_columns_of_the_snapshot_too(stored_row, row, differs):
    assert _items_differ([stored_row], [row]) is differs


def test_a_position_without_a_snapshot_on_both_sides_is_never_different_whatever_the_category():
    assert _items_differ([LEGACY_ROW + ('E', None, 'irgendwas', None)], [wanted()]) is False


def test_the_stored_positions_come_with_the_snapshot_columns_in_the_order_the_comparison_reads(world, entries):
    doc_id = saved('invoice', form('invoice', world, [item(1, 'Nachhilfe', 100.0, 0)], taxRate=0, taxRateId=entries.free))

    with world.db.transaction() as tx:
        rows = stored_items(tx.conn, doc_id)

    assert len(rows) == 1 and len(rows[0]) == 9
    assert rows[0][5:] == ('E', 0, REASON, entries.coa[4120])


def sent_exempt_invoice(world, entries, number='26R100'):
    """Eine versendete Rechnung mit der steuerfreien Momentaufnahme (gespeichert über den Handler, versendet über die Route)."""
    doc_id = saved('invoice', form('invoice', world, [item(1, 'Nachhilfe', 100.0, 0)], taxRate=0, taxRateId=entries.free, invoiceNumber=number))
    code, body = set_invoice_status(doc_id, 'sent')
    assert code == 200 and 'warning' not in body, body
    return doc_id


def test_a_sent_invoice_resubmitted_unchanged_with_its_entry_changes_nothing(world, entries):
    doc_id = sent_exempt_invoice(world, entries)
    before = snapshot(world.db)

    answer, _status = post('invoice', form('invoice', world, [item(1, 'Nachhilfe', 100.0, 0)], taxRate=0, taxRateId=entries.free, invoiceId=doc_id, documentVersion=1))

    assert answer == {'success': True, 'invoice_id': doc_id} and snapshot(world.db) == before


@pytest.mark.parametrize('items, over', [
    pytest.param([item(1, 'Nachhilfe', 100.0, 0)], dict(taxRateId='free_other'), id='anderer-eintrag'),
    pytest.param([item(1, 'Nachhilfe', 100.0, 0)], {}, id='ohne-eintrag'),
    pytest.param([item(1, 'Nachhilfe', 100.0, 5.5)], dict(taxRate=0.055, taxRateId='reduced'), id='anderer-satz-mit-eintrag'),
])
def test_a_sent_invoice_refuses_a_deviating_snapshot_like_any_other_change_to_a_locked_position(world, entries, items, over):
    doc_id = sent_exempt_invoice(world, entries)
    before = snapshot(world.db)
    over = {key: getattr(entries, value) if key == 'taxRateId' else value for key, value in over.items()}
    over.setdefault('taxRate', 0)

    raw = save_raw(form('invoice', world, items, invoiceId=doc_id, documentVersion=1, **over))

    answer = json.loads(raw)
    assert raw.status == 409 and answer['success'] is False and answer['error'].startswith(LOCKED_MESSAGE)
    assert snapshot(world.db) == before


def test_a_sent_invoice_without_a_snapshot_refuses_an_entry_sent_with_it(world, entries):
    """Der Eintrag ``nineteen_special`` (19 % mit eigenem Erlöskonto) schriebe eine Momentaufnahme – die gesperrte Position trüge sie danach: abgelehnt."""
    doc_id = saved('invoice', form('invoice', world, copy.deepcopy(ITEMS)))
    assert set_invoice_status(doc_id, 'sent')[0] == 200
    before = snapshot(world.db)

    raw = save_raw(form('invoice', world, copy.deepcopy(ITEMS), invoiceId=doc_id, documentVersion=1, taxRateId=entries.nineteen_special))

    assert raw.status == 409 and json.loads(raw)['error'].startswith(LOCKED_MESSAGE) and snapshot(world.db) == before


def test_a_sent_invoice_without_a_snapshot_takes_the_preset_sent_with_its_id_as_unchanged(world, entries):
    """Die Vorbelegung schreibt mit ID keine Momentaufnahme (2.8.2): Für die gesperrte Rechnung ohne eine ist das Formular mit der ID das Formular ohne – nichts ändert sich."""
    doc_id = saved('invoice', form('invoice', world, copy.deepcopy(ITEMS)))
    assert set_invoice_status(doc_id, 'sent')[0] == 200
    before = snapshot(world.db)

    answer, _status = post('invoice', form('invoice', world, copy.deepcopy(ITEMS), invoiceId=doc_id, documentVersion=1, taxRateId=entries.preset19))

    assert answer == {'success': True, 'invoice_id': doc_id} and snapshot(world.db) == before


def test_the_database_layer_refuses_a_deviating_snapshot_of_a_sent_invoice_too(world, entries):
    doc_id = sent_exempt_invoice(world, entries)
    before = snapshot(world.db)
    same = wanted(description='Nachhilfe', price_per_unit=100.0, tax_rate=0, tax_category='E', tax_rate_bp=0, tax_exempt_reason=REASON, revenue_coa_id=entries.coa[4120])

    with world.db.transaction() as tx:
        assert world.db.update_invoice_fields(tx, doc_id, {}, [same]) == 'unchanged'
    with pytest.raises(InvoiceLocked):
        with world.db.transaction() as tx:
            world.db.update_invoice_fields(tx, doc_id, {}, [{**same, 'tax_exempt_reason': OTHER_REASON}])

    assert snapshot(world.db) == before


def test_an_open_field_of_a_sent_invoice_with_a_snapshot_is_still_corrected_without_touching_the_positions(world, entries):
    doc_id = sent_exempt_invoice(world, entries)
    columns_before = snapshot_columns(world.db, doc_id)

    raw = save_raw({'invoiceId': doc_id, 'documentVersion': 1, 'orderNumber': 'B-99'})

    assert json.loads(raw)['success'] is True and columns(world.db, doc_id)['OrderNumber'] == 'B-99'
    assert snapshot_columns(world.db, doc_id) == columns_before == [free_snapshot(entries)]


# ═════════════════════════════════════════════════════════════════════════════
# Kopieren: Angebot → Rechnung, Stornorechnung und Ersatzrechnung nehmen die Momentaufnahme mit
# ═════════════════════════════════════════════════════════════════════════════

def test_a_quote_with_snapshots_becomes_an_invoice_that_carries_them_and_books_its_receivable_on_4340(world, entries):
    quote_id = saved('quote', form('quote', world, [item(1, 'Nachhilfe', 100.0, 0)], taxRate=0, taxRateId=entries.free, status='accepted'))

    invoice_id = world.db.convert_quote_to_invoice(quote_id, today=datetime.date(2026, 7, 1))

    assert snapshot_columns(world.db, invoice_id) == [free_snapshot(entries)]
    assert head(world.db, invoice_id) == {'TaxRate': 0.0, 'MixedTaxRates': None, **minor('100.00', '0.00', '100.00')}
    assert world.db.set_invoice_status(invoice_id, 'sent', USER).ok
    assert [row[:3] for row in receivables(world.db, columns(world.db, invoice_id)['InvoiceNumber'])] == [(10000, 4340, 10000)]


def test_a_copied_draft_with_a_snapshot_saved_by_the_form_of_today_loses_it_as_the_plan_says(world, entries):
    """Das Formular schickt bis zur Maske von E6 keine ``taxRateId``: Ein Entwurf, der seine Momentaufnahme aus einem Angebot hat, verliert sie beim Speichern – sein Satz bleibt 0 %,
    aber ohne Art ``'E'`` und ohne Grund. Das ist gewollt (Regel 2: der Nutzer hat die Vorbelegung gewählt) und wird mit der Maske von E6 sichtbar vermieden."""
    quote_id = saved('quote', form('quote', world, [item(1, 'Nachhilfe', 100.0, 0)], taxRate=0, taxRateId=entries.free, status='accepted'))
    invoice_id = world.db.convert_quote_to_invoice(quote_id, today=datetime.date(2026, 7, 1))

    resave('invoice', world, invoice_id, [item(1, 'Nachhilfe', 100.0, 0)], taxRate=0)

    assert snapshot_columns(world.db, invoice_id) == [NO_SNAPSHOT]


# ═════════════════════════════════════════════════════════════════════════════
# Der ganze Weg: speichern → versenden → Zahlung zuordnen → PDF und E-Rechnung
# ═════════════════════════════════════════════════════════════════════════════

def test_a_tax_free_draft_is_saved_sent_paid_and_documented_with_its_reason_and_account(world, entries):
    invoice_id = saved('invoice', form('invoice', world, [item(1, 'Nachhilfestunde', 100.0, 0)], taxRate=0, taxRateId=entries.free, invoiceNumber='E-100'))

    code, body = set_invoice_status(invoice_id, 'sent')              # die Route: Forderung buchen, danach PDF und E-Rechnung erzeugen
    assert (code, body.get('warning'), body.get('hint')) == (200, None, None), body
    assert receivables(world.db, 'E-100') == [(10000, 4340, 10000, None, None, 'transfer', '2026-06-07', 'Rechnung E-100 Malermeister Pinsel')]
    assert standing(world.db, invoice_id) == ('sent', 10000)

    pay_on(world, invoice_id, 100.0)
    assert marked(world.db, 'E-100') == [(1800, 10000, 10000, None, None, 'liquid_first'), (4340, 4120, 10000, None, None, 'transfer')]    # Erlöskonto des Eintrags, nie 4185
    assert standing(world.db, invoice_id) == ('paid', 0) and world.db.find_payment_inconsistencies() == []

    facts = document_facts(world.db, invoice_id)
    assert facts['cii']['tax'] == [{'category': 'E', 'rate': '0.00', 'basis': '100.00', 'amount': '0.00', 'reason': REASON}]
    assert facts['ubl'] is None or facts['ubl']['tax'] == [{'basis': '100.00', 'amount': '0.00', 'ID': 'E', 'Percent': '0', 'TaxExemptionReason': REASON}]
    text = ' '.join(page_lines(pathlib.Path(columns(world.db, invoice_id)['PDFPath']).read_bytes()))
    assert REASON in text and 'Kleinunternehmer' not in text and '§ 19' not in text


def test_19_percent_and_5_5_percent_is_saved_sent_without_a_receivable_paid_per_rate_and_documented(world, entries):
    invoice_id = saved('invoice', form('invoice', world, [item(1, 'Beratung', 100.0, 19), item(2, 'Fachbuch', 200.0, 5.5, taxRateId=entries.reduced)], taxMode='mixed',
                                       invoiceNumber='H-100'))
    assert head(world.db, invoice_id) == {'TaxRate': 0.19, 'MixedTaxRates': 1, **minor('300.00', '30.00', '330.00')}

    code, body = set_invoice_status(invoice_id, 'sent')
    assert (code, body.get('warning')) == (200, None) and body['hint'] == NO_WAITING_HINT.format(rate='5,5 %')
    assert receivables(world.db, 'H-100') == []                                       # kein Wartekonto für 5,5 %: der Beleg bucht erst mit der Zahlung

    pay_on(world, invoice_id, 330.0)
    assert marked(world.db, 'H-100') == [(4400, 1800, 11900, 1900, 0.19, 'purpose_first'), (4150, 1800, 21100, 1100, 0.055, 'purpose_first')]
    assert standing(world.db, invoice_id) == ('paid', 0) and world.db.find_payment_inconsistencies() == []

    facts = document_facts(world.db, invoice_id)
    assert sorted(((t['category'], float(t['rate']), t['basis'], t['amount']) for t in facts['cii']['tax'])) == [
        ('S', 5.5, '200.00', '11.00'), ('S', 19.0, '100.00', '19.00')]
    assert facts['cii']['totals']['GrandTotalAmount'] == '330.00'


def test_19_percent_and_tax_free_is_saved_sent_with_two_receivables_paid_per_rate_and_documented(world, entries):
    invoice_id = saved('invoice', form('invoice', world, [item(1, 'Beratung', 100.0, 19), item(2, 'Nachhilfe', 80.0, 0, taxRateId=entries.free)], taxMode='mixed',
                                       invoiceNumber='G-100'))

    code, body = set_invoice_status(invoice_id, 'sent')
    assert (code, body.get('warning'), body.get('hint')) == (200, None, None)
    assert [row[:5] for row in receivables(world.db, 'G-100')] == [(10000, 4405, 11900, 0.19, 1900), (10000, 4340, 8000, None, None)]

    pay_on(world, invoice_id, 199.0)
    assert marked(world.db, 'G-100') == [(1800, 10000, 19900, None, None, 'liquid_first'), (4405, 4400, 11900, 1900, 0.19, 'transfer'),
                                         (4340, 4120, 8000, None, None, 'transfer')]
    assert standing(world.db, invoice_id) == ('paid', 0) and world.db.find_payment_inconsistencies() == []

    facts = document_facts(world.db, invoice_id)
    assert sorted(((t['category'], float(t['rate']), t['basis'], t['amount'], t['reason']) for t in facts['cii']['tax'])) == [
        ('E', 0.0, '80.00', '0.00', REASON), ('S', 19.0, '100.00', '19.00', None)]


def test_a_quote_with_an_entry_prints_the_reason_and_the_rate_of_its_group(world, entries):
    quote_id = saved('quote', form('quote', world, [item(1, 'Beratung', 100.0, 19), item(2, 'Nachhilfe', 80.0, 0, taxRateId=entries.free)], taxMode='mixed'))

    pdf, _path = generate_quote_pdf(world.db, quote_id)

    text = ' '.join(page_lines(pdf))
    assert REASON in text and 'steuerfrei' in text and '199,00' in text


def test_the_documents_of_a_draft_follow_a_second_save_with_another_entry(world, entries):
    """Der Entwurf ist nicht eingefroren: Wer ihn mit einem anderen Eintrag speichert, bekommt beim nächsten Erzeugen den neuen Grund."""
    invoice_id = saved('invoice', form('invoice', world, [item(1, 'Nachhilfe', 100.0, 0)], taxRate=0, taxRateId=entries.free))
    assert generate_invoice_documents(world.db, invoice_id)['pdf_path']
    assert document_facts(world.db, invoice_id)['cii']['tax'][0]['reason'] == REASON

    resave('invoice', world, invoice_id, [item(1, 'Nachhilfe', 100.0, 0)], taxRate=0, taxRateId=entries.free_other)

    assert generate_invoice_documents(world.db, invoice_id)['pdf_path']
    assert document_facts(world.db, invoice_id)['cii']['tax'][0]['reason'] == OTHER_REASON


def test_the_database_is_not_read_for_the_entries_when_the_form_names_none(world, monkeypatch):
    """Ohne ``taxRateId`` liest ``_tax_fields`` die Stammdaten nie – jeder heutige Payload bleibt, was er war."""
    def boom(*args, **kwargs):
        raise AssertionError('Die Steuersätze der Stammdaten werden ohne taxRateId nie gelesen.')
    monkeypatch.setattr(world.db, 'get_tax_rate', boom)
    monkeypatch.setattr(world.db, 'fetch_tax_rates', boom)

    for count, key in enumerate(('ein-satz-19', 'haeckchen-19-und-7', 'ohne-steuerausweis')):
        shape = copy.deepcopy(SHAPES[key])
        items = shape.pop('items')
        for kind in KINDS:
            assert saved(kind, form(kind, world, items, **shape, **number_over(kind, 300 + count)))


def test_each_entry_is_read_once_however_many_positions_name_it(world, entries, monkeypatch):
    calls = []
    original = world.db.get_tax_rate
    monkeypatch.setattr(world.db, 'get_tax_rate', lambda entry_id, conn=None: calls.append(entry_id) or original(entry_id, conn))
    items = [item(position, f'Posten {position}', 1.0, 0, taxRateId=entries.free) for position in range(1, 31)]

    saved('invoice', form('invoice', world, items, taxMode='mixed'))

    assert calls == [entries.free]


def test_the_whole_state_of_the_save_stays_in_one_transaction_a_failing_item_leaves_no_snapshot_behind(world, entries, monkeypatch):
    """Kopf und Positionen (samt Momentaufnahme) schreibt EINE Transaktion: Scheitert die zweite Position, ist nichts gespeichert, und die Nummer ist nicht verbraucht."""
    real = world.db.insert_invoice_item
    calls = []

    def failing(item_data, tx=None):
        calls.append(item_data)
        if len(calls) == 2:
            raise sqlite3.IntegrityError('NOT NULL constraint failed: InvoiceItems.Description')
        return real(item_data, tx=tx)
    monkeypatch.setattr(world.db, 'insert_invoice_item', failing)
    before = snapshot(world.db)

    answer, _status = post('invoice', form('invoice', world, [item(1, 'Nachhilfe', 100.0, 0, taxRateId=entries.free), item(2, 'Beratung', 10.0, 19)], taxMode='mixed'))

    assert answer['success'] is False and 'es wurde nichts gespeichert' in answer['error']
    assert snapshot(world.db) == before
