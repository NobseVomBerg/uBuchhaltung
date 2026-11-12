# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Versendete Dokumente gehen nicht verloren (2.1.2): „Zurückholen“ legt sie in „Fassungen“ ab.

Geht eine versendete Rechnung zurück auf Entwurf, hat der Kunde ihr PDF (und XML) schon:

* Beide wandern nach ``invoices/<Jahr>/Fassungen/<ID>/<Name> – versendet bis <JJJJ-MM-TT>.<Endung>``
  (verschoben, nie überschrieben, nie gelöscht), die Pfade der Rechnung werden geleert. Scheitert das
  Verschieben, bleibt alles, wie es war.
* „War schon versendet“ (``Database.was_sent_before``: Versandzeitpunkt, Protokoll oder – wie in 2.1.2 – eine
  Datei im Ordner): Löscht der Nutzer den Entwurf, bleibt die Nummer vergeben (die Lücke bleibt) und nichts in
  „Fassungen“ wird angefasst. Ein Entwurf ohne Vorgeschichte gibt seine Nummer weiter frei.
  (Fassung, Protokoll und Berichtigen: test_invoice_correction.py, test_invoice_sent_history.py.)
* Erneutes Versenden erzeugt eine neue Datei, die archivierte bleibt; zweimal zurückholen ergibt zwei
  Fassungen. Ein gespeicherter Pfad außerhalb des Dokumentordners wird nie verschoben.
* Datensicherung und Wiederherstellung nehmen den Ordner mit.

Alle Namen, Adressen und Nummern sind erfundene Testwerte.
"""
import datetime
import os
import shutil
import sqlite3

import pytest

import server.handlers as handlers
import userctx
from db.invoice_lock import ARCHIVE_FAILED_MESSAGE
from server import backup as backup_mod
from server.pages import script_json
from server.pages_invoice import PageInvoice, delete_question
from tests.document_helpers import read_file, write_file
from tests.dom_helpers import Dom
from tests.invoice_world import (build_world, columns, make_invoice, make_payload, run_sql, save,
                                 set_invoice_status, snapshot)

TODAY = datetime.date.today().isoformat()


@pytest.fixture
def world(tmp_db, monkeypatch):
    monkeypatch.setattr(handlers, 'Database', lambda *a, **k: tmp_db)
    w = build_world(tmp_db)
    tmp_db.insert_number_range('invoice', 2026, 'R', current_number=0)
    return w


def new_invoice(w, **over):
    """Eine neue Rechnung mit der Nummer, die der Kreis vorschlägt (wie das Formular)."""
    suggestion = w.db.peek_next_number('invoice', 2026)[0]
    resp = save(make_payload(w, invoiceNumber=suggestion, numberSuggestion=suggestion, **over))
    assert resp['success'] is True, resp
    return resp['invoice_id']


def send(invoice_id):
    code, body = set_invoice_status(invoice_id, 'sent')
    assert (code, body['success']) == (200, True), body
    return body


def reopen(invoice_id, status='draft'):
    return set_invoice_status(invoice_id, status)


def number_of(w, invoice_id):
    return columns(w.db, invoice_id)['InvoiceNumber']


def counter(db):
    return db.fetch_number_ranges('invoice')[0][5]


def stored(w, invoice_id):
    cols = columns(w.db, invoice_id)
    return cols['PDFPath'], cols['XMLPath']


def folder(invoice_id, year='2026'):
    return os.path.join(userctx.user_data_dir(), 'invoices', year, 'Fassungen', str(invoice_id))


def archived(invoice_id, year='2026'):
    path = folder(invoice_id, year)
    return sorted(os.listdir(path)) if os.path.isdir(path) else []


def delete(w, invoice_id):
    return handlers.handle_delete_invoice(w.db, invoice_id)


# ── Zurückholen legt die versendeten Dokumente ab ────────────────────────────

def test_reopening_moves_the_sent_documents_into_the_versions_folder(world):
    invoice_id = new_invoice(world)
    send(invoice_id)
    pdf, xml = stored(world, invoice_id)
    assert os.path.isfile(pdf) and os.path.isfile(xml)
    sent_pdf, sent_xml = read_file(pdf), read_file(xml)

    code, body = reopen(invoice_id)
    assert (code, body['success']) == (200, True)
    assert 'Fassungen' in body['hint']                                   # die Oberfläche sagt, wo sie liegen
    cols = columns(world.db, invoice_id)
    assert (cols['Status'], cols['PDFPath'], cols['XMLPath']) == ('draft', None, None)
    assert not os.path.exists(pdf) and not os.path.exists(xml)           # verschoben, nicht kopiert

    stem = os.path.splitext(os.path.basename(pdf))[0]
    names = [f'{stem} – versendet bis {TODAY}.pdf', f'{stem} – versendet bis {TODAY}.xml']
    assert archived(invoice_id) == sorted(names)
    assert read_file(os.path.join(folder(invoice_id), names[0])) == sent_pdf     # byte-gleich
    assert read_file(os.path.join(folder(invoice_id), names[1])) == sent_xml


def test_reopening_to_finalized_archives_as_well(world):
    invoice_id = new_invoice(world)
    send(invoice_id)
    assert reopen(invoice_id, 'finalized')[0] == 200
    assert len(archived(invoice_id)) == 2


def test_an_overdue_invoice_is_archived_too(world):
    invoice_id = new_invoice(world)
    send(invoice_id)
    assert set_invoice_status(invoice_id, 'overdue')[0] == 200
    assert reopen(invoice_id)[0] == 200
    assert len(archived(invoice_id)) == 2


def test_an_invoice_without_documents_leaves_no_folder(world):
    """Ein Bestand ohne abgelegte Dokumente: nichts zu verschieben, kein Ordner, keine Fassung – die Dateiprobe
    findet nichts. Dass die Rechnung versendet war, hält seit 2.2.0 das Protokoll fest (test_invoice_sent_history.py)."""
    invoice_id = new_invoice(world)
    from tests.invoice_world import archived_versions, columns, force_status
    force_status(world.db, invoice_id, 'sent')
    assert reopen(invoice_id)[0] == 200
    assert archived(invoice_id) == [] and not os.path.exists(folder(invoice_id))
    from export.invoice_files import was_sent_before
    assert was_sent_before(invoice_id) is False
    assert archived_versions(world.db, invoice_id) == [] and columns(world.db, invoice_id)['DocumentVersion'] is None
    assert world.db.was_sent_before(invoice_id) is True              # das Protokoll kennt den Statuswechsel


# ── Die Nummer bleibt vergeben (Reproduktion des Prüfers) ─────────────────────

def test_a_reopened_and_deleted_invoice_never_gives_its_number_back(world):
    """Neue Rechnung 26R001 → versendet → zurück auf Entwurf → löschen: Früher war die PDF weg, der
    Zähler zurück, und die nächste Rechnung an einen anderen Kunden hieß wieder 26R001."""
    invoice_id = new_invoice(world)
    assert number_of(world, invoice_id) == '26R001' and counter(world.db) == 1
    send(invoice_id)
    pdf, _xml = stored(world, invoice_id)
    sent_pdf = read_file(pdf)
    assert reopen(invoice_id)[0] == 200

    assert delete(world, invoice_id) == (303, '/invoice')
    assert columns_exist(world, invoice_id) is False                     # der Entwurf ist weg …
    assert counter(world.db) == 1                                        # … die Nummer nicht zurückgegeben
    other = new_invoice(world, customerId=world.cust2)
    assert number_of(world, other) == '26R002'                           # nie wieder dieselbe Nummer
    names = archived(invoice_id)
    assert len(names) == 2                                               # die Fassung ist unberührt
    pdf_name = next(n for n in names if n.endswith('.pdf'))
    assert read_file(os.path.join(folder(invoice_id), pdf_name)) == sent_pdf


def columns_exist(w, invoice_id):
    con = sqlite3.connect(w.db.db_name)
    try:
        return con.execute('SELECT 1 FROM Invoices WHERE ID = ?', (invoice_id,)).fetchone() is not None
    finally:
        con.close()


def test_a_draft_without_a_history_still_gives_its_number_back(world):
    invoice_id = new_invoice(world)
    from export.invoice_files import generate_invoice_documents
    assert generate_invoice_documents(world.db, invoice_id)['error'] is None    # ein Entwurfs-PDF
    assert counter(world.db) == 1
    assert delete(world, invoice_id) == (303, '/invoice')
    assert counter(world.db) == 0
    assert number_of(world, new_invoice(world)) == '26R001'
    assert not os.path.exists(folder(invoice_id))


def test_deleting_the_draft_removes_only_a_file_made_after_reopening(world):
    """Nach dem Zurückholen ein neues Entwurfs-PDF: Das geht mit dem Löschen, die Fassung bleibt."""
    invoice_id = new_invoice(world)
    send(invoice_id)
    assert reopen(invoice_id)[0] == 200
    from export.invoice_files import generate_invoice_documents
    fresh = generate_invoice_documents(world.db, invoice_id)['pdf_path']
    before = archived(invoice_id)
    assert os.path.isfile(fresh)
    assert delete(world, invoice_id) == (303, '/invoice')
    assert not os.path.exists(fresh)
    assert archived(invoice_id) == before and len(before) == 2


def test_nothing_in_the_versions_folder_is_ever_removed_by_the_app(world, tmp_path):
    """Selbst ein auf die Fassung umgebogener Pfad eines Entwurfs löscht sie nicht."""
    invoice_id = new_invoice(world)
    send(invoice_id)
    assert reopen(invoice_id)[0] == 200
    version = os.path.join(folder(invoice_id), next(n for n in archived(invoice_id) if n.endswith('.pdf')))
    kept = read_file(version)
    other = new_invoice(world, customerId=world.cust2)
    world.db.update_invoice_pdf_path(other, version)                     # SQL-Konsole / zurückgespielte Sicherung
    assert delete(world, other) == (303, '/invoice')
    assert read_file(version) == kept


# ── Erneut versenden, mehrfach zurückholen ───────────────────────────────────

def test_sending_again_writes_a_new_file_and_keeps_the_archived_one(world):
    invoice_id = new_invoice(world)
    send(invoice_id)
    pdf, _xml = stored(world, invoice_id)
    first = read_file(pdf)
    assert reopen(invoice_id)[0] == 200
    version = os.path.join(folder(invoice_id), next(n for n in archived(invoice_id) if n.endswith('.pdf')))

    send(invoice_id)
    new_pdf, new_xml = stored(world, invoice_id)
    assert new_pdf == pdf and os.path.isfile(new_pdf) and os.path.isfile(new_xml)      # die Datei liegt wieder am alten Ort
    assert read_file(version) == first                                                 # die Fassung blieb stehen
    assert len(archived(invoice_id)) == 2


def test_reopening_twice_leaves_two_versions(world):
    invoice_id = new_invoice(world)
    send(invoice_id)
    assert reopen(invoice_id)[0] == 200
    send(invoice_id)
    assert reopen(invoice_id)[0] == 200
    names = archived(invoice_id)
    assert len(names) == 4                                                              # PDF und XML, zweimal
    stem = os.path.splitext(os.path.basename(
        next(n for n in names if n.endswith('.pdf'))))[0]
    assert f'{stem[:stem.index(" – versendet")]} – versendet bis {TODAY} (2).pdf' in names   # Zähler bei Kollision


def test_a_taken_name_gets_a_counter_instead_of_being_overwritten(world):
    """Liegt unter dem Zielnamen schon etwas (hier von Hand abgelegt), bekommt die neue Fassung einen Zähler."""
    invoice_id = new_invoice(world)
    send(invoice_id)
    pdf, _xml = stored(world, invoice_id)
    stem = os.path.splitext(os.path.basename(pdf))[0]
    taken = write_file(os.path.join(folder(invoice_id), f'{stem} – versendet bis {TODAY}.pdf'), b'Vorgefunden')
    assert reopen(invoice_id)[0] == 200
    assert read_file(taken) == b'Vorgefunden'
    assert f'{stem} – versendet bis {TODAY} (2).pdf' in archived(invoice_id)


# ── Pfade ausserhalb, Fehler beim Verschieben ─────────────────────────────────

def test_a_stored_path_outside_the_document_folder_is_not_moved(world, tmp_path):
    invoice_id = new_invoice(world)
    send(invoice_id)
    outside = write_file(str(tmp_path / 'fremd' / 'wichtig.pdf'), b'%PDF-1.4 nicht anfassen')
    world.db.update_invoice_pdf_path(invoice_id, outside)
    assert reopen(invoice_id)[0] == 200
    assert read_file(outside) == b'%PDF-1.4 nicht anfassen'                              # unberührt, am alten Platz
    assert stored(world, invoice_id) == (None, None)                                     # der Entwurf verweist nicht mehr darauf
    assert not any(n.endswith('wichtig.pdf') for n in archived(invoice_id))


def test_only_an_outside_path_and_no_file_at_the_name_moves_nothing(world, tmp_path):
    invoice_id = new_invoice(world)
    send(invoice_id)
    pdf, xml = stored(world, invoice_id)
    os.remove(pdf), os.remove(xml)
    outside = write_file(str(tmp_path / 'fremd' / 'wichtig.pdf'), b'%PDF-1.4 nicht anfassen')
    world.db.update_invoice_pdf_path(invoice_id, outside)
    assert reopen(invoice_id)[0] == 200
    assert read_file(outside) == b'%PDF-1.4 nicht anfassen' and archived(invoice_id) == []


def test_a_file_that_another_document_points_to_is_not_moved(world):
    first = new_invoice(world)
    send(first)
    pdf, _xml = stored(world, first)
    second = new_invoice(world, customerId=world.cust2)
    world.db.update_invoice_pdf_path(second, pdf)                        # eine Datei, zwei Belege
    assert reopen(first)[0] == 200
    assert os.path.isfile(pdf)                                           # sie bleibt für den anderen Beleg liegen


def test_a_bent_path_never_moves_the_version_of_another_invoice(world):
    """Der gespeicherte Pfad zeigt auf die Fassung eines anderen Belegs (SQL-Konsole, zurückgespielte
    Sicherung): Sie bleibt, wo sie ist – sonst verlöre der andere Beleg sein Merkmal „war schon
    versendet“ und seine Nummer würde beim Löschen wieder frei. Abgelegt wird die eigene Datei."""
    from export.invoice_files import was_sent_before
    first = new_invoice(world)
    send(first)
    own_pdf, _own_xml = stored(world, first)
    second = new_invoice(world, customerId=world.cust2)
    send(second)
    assert reopen(second)[0] == 200
    versions_of_second = archived(second)
    foreign = os.path.join(folder(second), next(n for n in versions_of_second if n.endswith('.pdf')))
    world.db.update_invoice_pdf_path(first, foreign)

    assert reopen(first)[0] == 200
    assert archived(second) == versions_of_second and was_sent_before(second) is True
    assert not os.path.exists(own_pdf)                                   # die eigene versendete Datei ist abgelegt
    assert len(archived(first)) == 2 and all(n.count('versendet bis') == 1 for n in archived(first))

    number = number_of(world, second)
    assert delete(world, second) == (303, '/invoice')
    assert counter(world.db) == 2                                        # die Nummer bleibt vergeben
    assert number not in (world.db.peek_next_number('invoice', 2026)[0],)


def test_a_failing_move_refuses_the_reopening_and_changes_nothing(world, monkeypatch):
    """Das XML lässt sich nicht verschieben: Das PDF geht zurück, Status, Pfade, Forderung bleiben."""
    invoice_id = new_invoice(world)
    send(invoice_id)
    pdf, xml = stored(world, invoice_id)
    before, files = snapshot(world.db), (read_file(pdf), read_file(xml))
    real_move, calls = shutil.move, []

    def flaky(src, dst, *args, **kwargs):
        calls.append(src)
        if len(calls) == 2:
            raise OSError('Platte voll')
        return real_move(src, dst, *args, **kwargs)
    monkeypatch.setattr('export.invoice_files.shutil.move', flaky)

    code, body = reopen(invoice_id)
    assert (code, body['success'], body['error']) == (409, False, ARCHIVE_FAILED_MESSAGE)
    assert 'Platte' not in str(body)                                     # keine Ausnahmetexte in der Antwort
    assert snapshot(world.db) == before                                  # Status, Pfade, Forderung, Zähler
    assert (read_file(pdf), read_file(xml)) == files                     # beide Dateien am alten Platz
    assert archived(invoice_id) == [] and not os.path.exists(folder(invoice_id))


def test_a_failing_commit_puts_the_documents_back(world, monkeypatch):
    invoice_id = new_invoice(world)
    send(invoice_id)
    pdf, xml = stored(world, invoice_id)
    before, files = snapshot(world.db), (read_file(pdf), read_file(xml))

    from db.core import Transaction

    def broken_commit(self, *exc):
        self.conn.rollback()
        self.conn.close()
        raise sqlite3.OperationalError('disk I/O error')
    with monkeypatch.context() as patched:
        patched.setattr(Transaction, '__exit__', broken_commit)
        with pytest.raises(sqlite3.OperationalError):
            world.db.set_invoice_status(invoice_id, 'draft')
    assert (read_file(pdf), read_file(xml)) == files
    assert archived(invoice_id) == []
    assert snapshot(world.db) == before


def test_a_failing_version_row_refuses_the_reopening_and_puts_the_documents_back(world):
    """Das Verschieben gelang, das Festhalten in ``InvoiceDocumentVersions`` nicht (hier weist ein Trigger das
    ``INSERT`` ab): Das Zurückholen wird mit Klartext abgelehnt (409), keine Ausnahme bis in den Handler; die
    Dokumente liegen wieder am alten Platz, Status, Pfade und Fassung sind unverändert."""
    invoice_id = new_invoice(world)
    send(invoice_id)
    pdf, xml = stored(world, invoice_id)
    before, files = snapshot(world.db), (read_file(pdf), read_file(xml))
    run_sql(world.db, "CREATE TRIGGER boom BEFORE INSERT ON InvoiceDocumentVersions "
                      "BEGIN SELECT RAISE(ABORT, 'boom'); END")

    code, body = reopen(invoice_id)

    assert (code, body['success'], body['error']) == (409, False, ARCHIVE_FAILED_MESSAGE)
    assert 'boom' not in str(body)
    assert (read_file(pdf), read_file(xml)) == files
    assert archived(invoice_id) == [] and not os.path.exists(folder(invoice_id))
    assert snapshot(world.db) == before                    # Status, Pfade, Fassung, Forderung, Protokoll, Fassungstabelle
    cols = columns(world.db, invoice_id)
    assert (cols['Status'], cols['PDFPath'], cols['XMLPath'], cols['DocumentVersion']) == ('sent', pdf, xml, None)


def test_an_unexpected_error_while_holding_the_documents_puts_them_back_for_the_reopening_too(world, monkeypatch):
    invoice_id = new_invoice(world)
    send(invoice_id)
    pdf, xml = stored(world, invoice_id)
    before, files = snapshot(world.db), (read_file(pdf), read_file(xml))

    def broken(path):
        raise RuntimeError('Programmfehler')
    monkeypatch.setattr('db.invoice_versions._sha256', broken)

    with pytest.raises(RuntimeError):
        world.db.set_invoice_status(invoice_id, 'draft')

    assert (read_file(pdf), read_file(xml)) == files and archived(invoice_id) == []
    assert snapshot(world.db) == before


# ── Das Merkmal „war schon versendet“ ──────────────────────────────────────

def test_was_sent_before_looks_for_a_file_in_any_year_folder(world):
    from export.invoice_files import was_sent_before
    assert was_sent_before(7) is False                                   # kein Ordner
    root = os.path.join(userctx.user_data_dir(), 'invoices')
    os.makedirs(os.path.join(root, '2026', 'Fassungen', '7'))
    assert was_sent_before(7) is False                                   # ein leerer Ordner ist keine Fassung
    write_file(os.path.join(root, '2025', 'Fassungen', '7', 'alt.pdf'))
    assert was_sent_before(7) is True                                    # das Rechnungsdatum kann sich geändert haben
    assert was_sent_before(8) is False and was_sent_before(70) is False


@pytest.mark.parametrize('given', [7, '7', 7.0, '07', ' 7', '+7'])
def test_the_file_probes_read_the_id_as_a_number_so_the_same_folder_is_meant(given):
    """Die Datenbank findet die Rechnung 7 auch mit ``7.0`` oder ``'07'``; der Ordner heißt ``7`` – die Proben lesen
    die ID als Ganzzahl, damit ``Fassungen/7.0`` und ``Fassungen/07`` nicht der Weg an der Nummernsperre vorbei sind."""
    from export.invoice_files import version_files, was_sent_before
    write_file(os.path.join(userctx.user_data_dir(), 'invoices', '2026', 'Fassungen', '7', 'alt.pdf'))
    assert was_sent_before(given) is True
    assert [name for name, _path, _year in version_files(given)] == ['alt.pdf']


@pytest.mark.parametrize('given', ['abc', '', None, [7], {'id': 7}, True, 'x7', '7;', '7.0', float('nan'),
                                   float('inf')])
def test_an_id_that_is_no_number_has_no_files(given):
    from export.invoice_files import version_files, was_sent_before
    write_file(os.path.join(userctx.user_data_dir(), 'invoices', '2026', 'Fassungen', '7', 'alt.pdf'))
    write_file(os.path.join(userctx.user_data_dir(), 'invoices', '2026', 'Fassungen', '1', 'alt.pdf'))
    assert was_sent_before(given) is False and version_files(given) == []


def test_version_files_name_the_year_folder_and_a_year_picks_exactly_that_folder():
    from export.invoice_files import version_file_path, version_files
    root = os.path.join(userctx.user_data_dir(), 'invoices')
    write_file(os.path.join(root, '2025', 'Fassungen', '7', 'a.pdf'), b'A25')
    write_file(os.path.join(root, '2026', 'Fassungen', '7', 'a.pdf'), b'A26')
    write_file(os.path.join(root, '2026', 'Fassungen', '7', 'b.xml'), b'<b/>')
    write_file(os.path.join(root, '2026', 'Fassungen', '8', 'a.pdf'), b'andere Rechnung')

    assert [(name, year) for name, _path, year in version_files(7)] == [
        ('a.pdf', '2025'), ('a.pdf', '2026'), ('b.xml', '2026')]
    assert read_file(version_file_path(7, 'a.pdf', '2025')) == b'A25'
    assert read_file(version_file_path(7, 'a.pdf', '2026')) == b'A26'
    assert read_file(version_file_path(7, 'a.pdf')) == b'A25'              # ohne Jahr: der erste Treffer, wie bisher
    assert version_file_path(7, 'b.xml', '2025') is None                   # der Ordner 2025 hat die Datei nicht
    assert version_file_path(7, 'a.pdf', '2024') is None                   # es gibt keinen Ordner 2024
    for year in ('..', '../2026', '20a6', '', ' 2026', '2026 ', '2026/', '\u0662\u0660\u0662\u0666', 2026, b'2026'):
        assert version_file_path(7, 'a.pdf', year) is None, year


# ── Die Oberfläche beim Löschen ──────────────────────────────────────────────

def delete_onclick(html, invoice_id):
    dom = Dom(html)
    found = [a['onclick'] for a in dom.all('button')
             if f"/invoice/delete?id={invoice_id}'" in a.get('onclick', '')]
    assert len(found) == 1
    return found[0]


def test_the_form_of_a_reopened_draft_says_what_stays(world):
    invoice_id = new_invoice(world)
    plain = new_invoice(world, customerId=world.cust2)
    send(invoice_id)
    assert reopen(invoice_id)[0] == 200
    html = PageInvoice(world.db, {}, invoice_id)
    assert script_json(delete_question(True)) in delete_onclick(html, invoice_id)
    assert 'Diese Rechnung war bereits versendet. Die versendete Fassung bleibt im Ordner „Fassungen“ ' \
           'erhalten, die Nummer wird nicht neu vergeben.' in delete_question(True)
    assert script_json('Entwurf wirklich löschen?') in delete_onclick(PageInvoice(world.db, {}, plain), plain)


def test_the_list_marks_reopened_drafts_for_the_confirmation(world):
    invoice_id = new_invoice(world)
    plain = new_invoice(world, customerId=world.cust2)
    send(invoice_id)
    assert reopen(invoice_id)[0] == 200
    list_part = PageInvoice(world.db, {}, None).split('Ende gridLeftCol')[0]
    assert f'onclick="deleteInvoiceDraft({invoice_id}, true)"' in list_part
    assert f'onclick="deleteInvoiceDraft({plain}, false)"' in list_part
    assert script_json(delete_question(True)) in list_part               # der Text steht im Skript der Liste


# ── Datensicherung ──────────────────────────────────────────────────────────

@pytest.fixture(params=['zip', '7z'])
def archive_format(request, monkeypatch):
    if request.param == 'zip':
        monkeypatch.setattr(backup_mod, '_find_sevenzip', lambda: None)
    elif not backup_mod._find_sevenzip():
        pytest.skip('7-Zip nicht installiert')
    return request.param


def test_backup_and_restore_keep_the_versions_folder(archive_format, tmp_path):
    user = tmp_path / 'user'
    user.mkdir()
    con = sqlite3.connect(user / 'buch.db')
    con.execute('CREATE TABLE T (X TEXT)')
    con.commit()
    con.close()
    rel = os.path.join('invoices', '2026', 'Fassungen', '7', '26R001 Müller & Söhne – versendet bis 2026-11-12.pdf')
    write_file(str(user / rel), b'%PDF-1.4 Fassung')
    archive, _ = backup_mod.create_backup(str(user), str(user / 'buch.db'), 'all')

    restored = tmp_path / 'entpackt'
    os.makedirs(restored / 'backup')
    shutil.copy(archive, restored / 'backup')
    backup_mod.restore_backup(str(restored), os.path.basename(archive))
    assert read_file(str(restored / rel)) == b'%PDF-1.4 Fassung'
