# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Der Status des Angebots vor der Umwandlung (2.10.0, Schema 2010): Löscht man den Entwurf der Rechnung, geht das Angebot dorthin zurück.

* ``convert_quote_to_invoice`` hält den Status des Angebots an der neuen Rechnung fest (``Invoices.QuoteStatusBefore``: ``draft``, ``sent`` oder
  ``accepted``), gelesen in derselben Transaktion, die das Angebot umwandelt.
* ``delete_draft_invoice`` setzt das Angebot auf diesen Status zurück – nur, wenn es keine weitere Rechnung aus dem Angebot gibt (sonst bleibt es
  „umgewandelt“, wie bisher).
* Ein Bestand ohne den Wert (NULL), ein unlesbarer (per SQL-Konsole geschrieben) oder ein Status, der nicht zurückkommen darf (``converted``, ``rejected``),
  ergibt wie bisher ``accepted``.
* Die Maske kann den Wert nicht setzen: Er steht nicht unter den Feldern, die das Speichern kennt.

Alle Namen und Nummern sind erfundene Testwerte.
"""
import datetime
import sqlite3
from contextlib import closing

import pytest

from db.invoices import QUOTE_CONVERTIBLE_STATUSES, QuoteNotConvertible
from tests.document_helpers import add_contacts, add_document


DAY = datetime.date(2026, 6, 9)                 # der Tag der Umwandlung (``today=``): Die Rechnung bekommt ihre Nummer im Kreis dieses Jahres


@pytest.fixture
def world(tmp_db):
    tmp_db.insert_number_range('invoice', DAY.year, 'R', current_number=10)
    own, customer = add_contacts(tmp_db)
    return tmp_db, own, customer


def _quote(world, status='sent', number='26A001'):
    db, own, customer = world
    return add_document(db, number, 'quote', own, customer, status=status)


def _status(db, document_id):
    return db.get_invoice_by_id(document_id)[40]


def _before(db, invoice_id):
    with closing(sqlite3.connect(db.db_name)) as con:
        return con.execute('SELECT QuoteStatusBefore FROM Invoices WHERE ID = ?', (invoice_id,)).fetchone()[0]


def _set(db, document_id, **columns):
    """Eine Spalte per SQL setzen – wie ein Bestand aus 2.9.x (NULL) oder die SQL-Konsole es täte."""
    with closing(sqlite3.connect(db.db_name)) as con:
        for column, value in columns.items():
            con.execute(f'UPDATE Invoices SET {column} = ? WHERE ID = ?', (value, document_id))
        con.commit()


@pytest.mark.parametrize('status', QUOTE_CONVERTIBLE_STATUSES)
def test_deleting_the_draft_brings_the_quote_back_to_the_status_before(world, status):
    db = world[0]
    quote_id = _quote(world, status)

    invoice_id = db.convert_quote_to_invoice(quote_id, today=DAY)

    assert _status(db, quote_id) == 'converted' and _before(db, invoice_id) == status
    db.delete_draft_invoice(invoice_id)
    assert _status(db, quote_id) == status
    again = db.convert_quote_to_invoice(quote_id, today=DAY)                       # und lässt sich wieder umwandeln
    assert again and _status(db, quote_id) == 'converted' and _before(db, again) == status


def test_a_quote_without_a_status_counts_as_a_draft_and_comes_back_as_one(world):
    db = world[0]
    quote_id = _quote(world)
    _set(db, quote_id, Status=None)                                      # Altbestand: ohne Status

    invoice_id = db.convert_quote_to_invoice(quote_id, today=DAY)

    assert _before(db, invoice_id) == 'draft'
    db.delete_draft_invoice(invoice_id)
    assert _status(db, quote_id) == 'draft'


@pytest.mark.parametrize('stored', [None, 'xyz', '', 'converted', 'rejected', 'Sent', 1])
def test_a_stock_without_a_readable_value_goes_back_to_accepted_as_before(world, stored):
    db = world[0]
    quote_id = _quote(world, 'sent')
    invoice_id = db.convert_quote_to_invoice(quote_id, today=DAY)
    _set(db, invoice_id, QuoteStatusBefore=stored)                       # eine Rechnung aus 2.9.x trägt NULL, die SQL-Konsole kann alles schreiben

    db.delete_draft_invoice(invoice_id)

    assert _status(db, quote_id) == 'accepted'


def test_the_quote_stays_converted_while_another_invoice_comes_from_it(world):
    db, own, customer = world
    quote_id = _quote(world, 'sent')
    invoice_id = db.convert_quote_to_invoice(quote_id, today=DAY)
    other = add_document(db, '26R101', 'invoice', own, customer, status='sent')
    _set(db, other, SourceQuoteId=quote_id)

    db.delete_draft_invoice(invoice_id)

    assert _status(db, quote_id) == 'converted'


def test_deleting_an_invoice_without_a_quote_leaves_every_quote_alone(world):
    db, own, customer = world
    quote_id = _quote(world, 'sent')
    db.convert_quote_to_invoice(quote_id, today=DAY)
    plain = add_document(db, '26R102', 'invoice', own, customer, status='draft')

    db.delete_draft_invoice(plain)

    assert _status(db, quote_id) == 'converted'


def test_a_rejected_quote_is_not_converted_and_nothing_is_written(world):
    db = world[0]
    quote_id = _quote(world, 'rejected')
    with closing(sqlite3.connect(db.db_name)) as con:
        invoices = con.execute('SELECT COUNT(*) FROM Invoices').fetchone()[0]

    with pytest.raises(QuoteNotConvertible):
        db.convert_quote_to_invoice(quote_id, today=DAY)

    with closing(sqlite3.connect(db.db_name)) as con:
        assert con.execute('SELECT COUNT(*) FROM Invoices').fetchone()[0] == invoices
    assert _status(db, quote_id) == 'rejected'


def test_an_ordinary_invoice_carries_no_quote_status(world):
    db, own, customer = world
    invoice_id = add_document(db, '26R103', 'invoice', own, customer, status='draft')
    quote_id = _quote(world, 'sent')
    assert _before(db, invoice_id) is None and _before(db, quote_id) is None       # nur die Rechnung aus einem Angebot trägt ihn


def test_the_mask_cannot_set_the_value(world):
    """Das Speichern einer Rechnung kennt keinen Schlüssel dafür: ``update_invoice_fields`` lehnt ihn wie jeden unbekannten ab."""
    db = world[0]
    quote_id = _quote(world, 'sent')
    invoice_id = db.convert_quote_to_invoice(quote_id, today=DAY)

    with pytest.raises(ValueError, match='quote_status_before'):
        with db.transaction() as tx:
            db.update_invoice_fields(tx, invoice_id, {'quote_status_before': 'draft'})

    assert _before(db, invoice_id) == 'sent'

