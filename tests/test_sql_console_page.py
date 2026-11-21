# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""SQL-Konsole: die Seite (Kästchen „Schreiben erlauben“, Skript) und das Lösch-Skript „Zeitbereich löschen“ (2.3.11).

Drei Teile:
  1. Die Seite zeigt unter dem SQL-Feld das Kästchen „Schreiben erlauben“ (nie angekreuzt beim Laden) mit dem Hinweis auf
     die beiden Modi; es braucht keine neue CSS-Klasse. Das Skript setzt Meldungen nur über ``textContent`` und sendet
     ``&write=1`` nur mit Haken – und erst nach der Rückfrage ``appConfirm``; nach der Antwort ist der Haken wieder weg.
     Statisch geprüft, und mit Node.js gegen einen gespielten Browser, wo es installiert ist,
  2. Das Lösch-Skript der Vorlage „Zeitbereich löschen“ enthält weder BEGIN/COMMIT noch ``PRAGMA foreign_keys`` – die
     Konsole legt beides selbst fest –, und es läuft im Schreibmodus in EINER Transaktion mit eingeschalteten
     Fremdschlüsseln durch: Belege, Buchungen, Rechnungen und Angebote des Zeitraums verschwinden samt ihren Verweisen,
     was außerhalb liegt, bleibt (auch eine Kindbuchung außerhalb des Zeitraums unter einer Bankbewegung des Zeitraums:
     sie wird abgehängt, sonst scheiterte das Löschen am Fremdschlüssel),
  3. scheitert ein Befehl des Skripts, bleibt alles, wie es war.

Alle Namen, Nummern und Beträge sind erfunden.
"""
import contextlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
from types import SimpleNamespace

import pytest

from server import handlers
from server.pages_miscellaneous import PageMiscellaneous
from tests import installation
from tests.document_helpers import add_contacts, add_document
from tests.dom_helpers import Dom
from tests.test_depreciation_booking import _range_delete_script
from tests.test_sql_console import _query, _run, probe_db  # noqa: F401 (probe_db: Fixture)

CONFIRM_TEXT = "Die Befehle können Daten ändern oder löschen. Vorher eine Datensicherung anlegen. Ausführen?"
HINT_TEXT = ("Ohne Haken läuft die Konsole nur lesend. Mit Haken laufen alle Befehle in einer Transaktion: "
             "Scheitert einer, wird nichts geändert.")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="Node.js nicht installiert – die statischen Tests decken nur die Form ab")


def _scripts(page):
    return re.findall(r"<script>(.*?)</script>", page, re.S)


def _console_scripts(page):
    return [script for script in _scripts(page) if "sqlStatus" in script or "buildRangeDeleteSql" in script]


# ── Teil 1: die Seite ────────────────────────────────────────────────────────
def test_the_page_shows_the_unchecked_write_box_with_its_hint_below_the_sql_field(probe_db):
    page = PageMiscellaneous(probe_db)
    box = Dom(page).one("input", id="sql_write")
    assert box["type"] == "checkbox" and "checked" not in box        # bei jedem Laden nicht angekreuzt
    assert box.get("autocomplete") == "off"                          # auch nicht vom Browser nach einem Neuladen wiederhergestellt
    assert "&nbsp;Schreiben erlauben</label>" in page
    assert f'<p class="hint">{HINT_TEXT}</p>' in page
    assert page.index('id="sql_input"') < page.index('id="sql_write"') < page.index('onclick="executeSql()"')


def test_the_write_box_needs_no_new_css_class(probe_db):
    page = PageMiscellaneous(probe_db)
    with open(os.path.join(ROOT, "buch.css"), encoding="utf-8") as handle:
        css = handle.read()
    area = page[page.index('id="sql_input"'):page.index('onclick="executeSql()"')]
    classes = set(re.findall(r'class="([^"]+)"', area)) - {"textareaSql"}
    assert classes == {"rowWithObjects", "hint"}
    for name in classes:
        assert re.search(rf"^\.{name}\b", css, re.M), name


def test_the_range_delete_template_names_the_box(probe_db):
    page = PageMiscellaneous(probe_db)
    assert "Zum Ausführen „Schreiben erlauben“ ankreuzen und obigen Button klicken." in page
    assert "bitte prüfen, „Schreiben erlauben“ ankreuzen und „SQL ausführen“ klicken." in page


def test_the_script_sets_messages_only_through_text_content_and_asks_before_writing(probe_db):
    scripts = _console_scripts(PageMiscellaneous(probe_db))
    assert len(scripts) == 2
    text = "\n".join(scripts)
    for forbidden in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write"):
        assert forbidden not in text, forbidden
    assert "textContent" in text and "createElement" in text
    assert f"appConfirm('{CONFIRM_TEXT}'" in text
    assert text.count("&write=1") == 1 and "(write ? '&write=1' : '')" in text
    assert "/execute_sql" in text


RUNNER = r"""
const vm = require('vm');
const fs = require('fs');
const script = fs.readFileSync(process.argv[2], 'utf8');
const scenario = JSON.parse(process.argv[3]);
const log = {confirms: [], fetches: []};
class TextNode { constructor(text) { this.text = text; } }
class Element {
  constructor(id) { this.id = id; this.value = ''; this.checked = false; this.className = ''; this.style = {display: 'none'};
                    this.children = []; this._text = ''; }
  get textContent() { return this._text; }
  set textContent(value) { this._text = String(value); this.children = []; }
  appendChild(child) { this.children.push(child); return child; }
}
const elements = {};
for (const id of ['sql_input', 'sql_write', 'sql_output', 'sql_status', 'range_del_from', 'range_del_to']) elements[id] = new Element(id);
elements.sql_input.value = scenario.input || '';
elements.sql_write.checked = !!scenario.checked;
elements.range_del_from.value = scenario.from || '';
elements.range_del_to.value = scenario.to || '';
let confirmCallback = null;
const sandbox = {
  document: {getElementById: id => elements[id], createElement: () => new Element('span'), createTextNode: text => new TextNode(text)},
  encodeURIComponent,
  appConfirm: (text, onOk) => { log.confirms.push(text); confirmCallback = onOk; },
  fetch: async (url, init) => {
    log.fetches.push({url, method: init.method, headers: init.headers, body: init.body, checkedWhenSent: elements.sql_write.checked});
    if (scenario.fetchError) throw new Error(scenario.fetchError);
    return {json: async () => scenario.response};
  },
};
vm.runInNewContext(script, sandbox);
function statusParts() {
  return elements.sql_status.children.filter(child => child instanceof Element).map(child => ({className: child.className, text: child.textContent}));
}
(async () => {
  if (scenario.action === 'build') {
    sandbox.buildRangeDeleteSql();
  } else {
    await sandbox.executeSql();
    log.fetchesBeforeConfirm = log.fetches.length;
    log.checkedBeforeConfirm = elements.sql_write.checked;
    if (scenario.confirm === 'ok' && confirmCallback) await confirmCallback();
  }
  log.status = statusParts();
  log.statusText = elements.sql_status.textContent;
  log.checked = elements.sql_write.checked;
  log.output = {display: elements.sql_output.style.display, value: elements.sql_output.value};
  log.input = elements.sql_input.value;
  console.log(JSON.stringify(log));
})();
"""


def _run_script(tmp_path, page, **scenario):
    """Die Skripte der Konsole in Node, mit einem gespielten Browser (Elemente, ``fetch``, ``appConfirm``): liefert, was sie
    getan haben – gesendete Anfragen, Rückfragen, Meldungen und den Zustand des Kästchens."""
    source, runner = tmp_path / "console.js", tmp_path / "run_console.js"
    source.write_text("\n".join(_console_scripts(page)), encoding="utf-8")
    runner.write_text(RUNNER, encoding="utf-8")
    done = subprocess.run([NODE, str(runner), str(source), json.dumps(scenario)], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


READ_ANSWER = {"success_count": 1, "total": 1, "errors": [], "output": "1\n1"}


@needs_node
def test_without_the_tick_the_script_sends_no_write_field_and_asks_nothing(probe_db, tmp_path):
    log = _run_script(tmp_path, PageMiscellaneous(probe_db), input="SELECT 1", checked=False, response=READ_ANSWER)
    assert log["confirms"] == []
    (fetch,) = log["fetches"]
    assert (fetch["url"], fetch["method"]) == ("/execute_sql", "POST")
    assert fetch["headers"] == {"Content-Type": "application/x-www-form-urlencoded"}
    assert fetch["body"] == "sql_commands=SELECT%201"                       # kein write
    assert log["status"] == [{"className": "successColor", "text": "1 von 1 Befehlen erfolgreich."}]
    assert log["output"] == {"display": "block", "value": "1\n1"}
    assert log["checked"] is False


@needs_node
def test_with_the_tick_the_script_asks_first_and_sends_write_1_only_after_the_answer_yes(probe_db, tmp_path):
    log = _run_script(tmp_path, PageMiscellaneous(probe_db), input="INSERT INTO T VALUES (1)", checked=True, confirm="ok",
                      response={"success_count": 1, "total": 1, "errors": [], "output": "", "rolled_back": False})
    assert log["confirms"] == [CONFIRM_TEXT]
    assert log["fetchesBeforeConfirm"] == 0 and log["checkedBeforeConfirm"] is True      # bis zur Antwort nichts gesendet
    (fetch,) = log["fetches"]
    assert fetch["body"] == "sql_commands=INSERT%20INTO%20T%20VALUES%20(1)&write=1"
    assert log["checked"] is False                                         # nach der Antwort ist der Haken wieder weg
    assert log["output"]["display"] == "none"


@needs_node
def test_cancelling_the_question_sends_nothing_and_keeps_the_tick(probe_db, tmp_path):
    log = _run_script(tmp_path, PageMiscellaneous(probe_db), input="DELETE FROM T", checked=True, confirm="cancel")
    assert log["confirms"] == [CONFIRM_TEXT] and log["fetches"] == []
    assert log["checked"] is True and log["status"] == []


@needs_node
def test_the_script_shows_errors_the_rolled_back_sentence_and_warnings_as_text(probe_db, tmp_path):
    answer = {"success_count": 0, "total": 3, "output": "", "rolled_back": True,
              "errors": ["Befehl 2: <img src=x onerror=alert(1)>", "Nichts wurde geändert – im Schreibmodus laufen alle "
                         "Befehle einer Anfrage in einer Transaktion."],
              "warnings": ["Die Änderungen sind gespeichert, ließen sich aber nicht ins SQL-Protokoll schreiben."]}
    log = _run_script(tmp_path, PageMiscellaneous(probe_db), input="X", checked=True, confirm="ok", response=answer)
    assert log["status"] == [
        {"className": "errorColor", "text": "Befehl 2: <img src=x onerror=alert(1)>; Nichts wurde geändert – im "
                                            "Schreibmodus laufen alle Befehle einer Anfrage in einer Transaktion."},
        {"className": "warnColor", "text": "Die Änderungen sind gespeichert, ließen sich aber nicht ins SQL-Protokoll "
                                           "schreiben."}]                                  # der Text steht roh da, nie als HTML
    assert log["checked"] is False


@needs_node
def test_a_failed_request_is_reported_and_the_tick_is_gone(probe_db, tmp_path):
    log = _run_script(tmp_path, PageMiscellaneous(probe_db), input="X", checked=True, confirm="ok", fetchError="Netz weg")
    assert log["status"] == [{"className": "errorColor", "text": "Fehler: Netz weg"}]
    assert log["checked"] is False


@needs_node
@pytest.mark.parametrize("checked", [False, True])
def test_an_empty_field_sends_and_asks_nothing(probe_db, tmp_path, checked):
    log = _run_script(tmp_path, PageMiscellaneous(probe_db), input="  \n ", checked=checked, confirm="ok")
    assert log["confirms"] == [] and log["fetches"] == []
    assert log["status"] == [{"className": "errorColor", "text": "Keine SQL-Befehle eingegeben."}]
    assert log["checked"] is checked


@needs_node
def test_the_template_fills_the_field_with_the_script_the_tests_evaluate_and_leaves_the_tick_alone(probe_db, tmp_path):
    page = PageMiscellaneous(probe_db)
    log = _run_script(tmp_path, page, action="build", **{"from": "2026-01-01", "to": "2026-12-31"})
    assert log["input"] == _range_delete_script(probe_db, "2026-01-01", "2026-12-31")
    assert log["input"].startswith("-- Zeitbereich löschen: 2026-01-01 bis 2026-12-31\n")
    assert log["status"] == [{"className": "", "text": "Lösch-Skript eingefügt — bitte prüfen, „Schreiben erlauben“ ankreuzen "
                                                       "und „SQL ausführen“ klicken."}]
    assert log["checked"] is False and log["fetches"] == []                 # das Skript wird nur eingefügt, nie ausgeführt


@needs_node
@pytest.mark.parametrize("dates, message", [
    pytest.param({"from": "", "to": "2026-12-31"}, "Bitte Von- und Bis-Datum angeben.", id="Von-fehlt"),
    pytest.param({"from": "2026-01-01", "to": ""}, "Bitte Von- und Bis-Datum angeben.", id="Bis-fehlt"),
    pytest.param({"from": "2026-12-31", "to": "2026-01-01"}, "Von-Datum liegt nach dem Bis-Datum.", id="verdreht"),
])
def test_the_template_asks_for_both_dates_in_order(probe_db, tmp_path, dates, message):
    log = _run_script(tmp_path, PageMiscellaneous(probe_db), action="build", **dates)
    assert log["status"] == [{"className": "errorColor", "text": message}] and log["input"] == ""


# ── Teil 2: das Lösch-Skript ─────────────────────────────────────────────────
PERIOD = ("2026-01-01", "2026-12-31")


def _script(db):
    return _range_delete_script(db, *PERIOD)


def _commands(db):
    return handlers._console_statements(_script(db))


def test_the_delete_script_has_no_transaction_commands_and_no_foreign_key_pragma(tmp_db):
    script = _script(tmp_db)
    code = "\n".join(handlers._console_log_text(command) for command in _commands(tmp_db))
    for word in ("BEGIN", "COMMIT", "ROLLBACK", "SAVEPOINT", "RELEASE", "PRAGMA", "foreign_keys"):
        assert not re.search(rf"\b{word}\b", code, re.I), word                   # END als Befehl schließt die Prüfung unten aus
    assert "foreign_keys" not in script.lower()                                          # auch nicht in den Kommentaren
    assert len(_commands(tmp_db)) == 17
    assert all(re.match(r"(DELETE FROM|UPDATE) ", handlers._console_log_text(command)) for command in _commands(tmp_db))


def _raw(db):
    """Verbindung mit Fremdschlüsseln, wie die App sie hat – so entsteht ein Bestand ohne Waisen."""
    con = sqlite3.connect(db.db_name)
    con.execute("PRAGMA foreign_keys = ON")
    return contextlib.closing(con)


def _one(con, sql, *args):
    return con.execute(sql, args).fetchone()[0]


@pytest.fixture
def books(tmp_db):
    """Ein Bestand mit allem, was das Skript anfasst – im Zeitraum 2026 und davor/danach:

    Belege ``d_in``/``d_out``; eine Bankbewegung ``bank_in`` des Zeitraums mit einem Kind im Zeitraum (``child_in``) und einem
    außerhalb (``child_out``, 2027); Einzelbuchungen ``entry_in``/``entry_out``; eine AfA-Buchung ``afa_in`` mit Planjahr;
    zwei Anlagen mit Verweisen auf Beleg und Buchung; eine Fahrt am Beleg; Rechnung und Angebot im Zeitraum, eine Rechnung
    danach, die das Angebot als Quelle und die Rechnung des Zeitraums als Storno-Ziel nennt, dazu Zahlungen, Protokoll
    und Fassungen."""
    db = tmp_db
    own, customer = add_contacts(db)
    db.insert_receipt("B-1", "2026-05-02", "b1.pdf", "belege/b1.pdf", "im Zeitraum")
    db.insert_receipt("B-2", "2025-12-30", "b2.pdf", "belege/b2.pdf", "davor")
    inv_in = add_document(db, "26R001", "invoice", own, customer, invoice_date="2026-03-04")
    quote_in = add_document(db, "26A001", "quote", own, customer, invoice_date="2026-03-05")
    inv_out = add_document(db, "27R001", "invoice", own, customer, invoice_date="2027-01-10", source_quote_id=quote_in)
    with _raw(db) as con:
        d_in, d_out = (_one(con, "SELECT ID FROM Documents WHERE Number = ?", n) for n in ("B-1", "B-2"))

        def booking(date, amount, kind="entry", parent=None, status=None):
            return con.execute("INSERT INTO Bookings (DateBooking, Amount, BookingType, ParentBooking_ID, Status) "
                               "VALUES (?, ?, ?, ?, ?)", (date, amount, kind, parent, status)).lastrowid

        bank_in = booking("2026-02-02", -1190, "bank")
        child_in = booking("2026-02-02", -1000, parent=bank_in, status="resolved")
        child_out = booking("2027-01-03", -190, parent=bank_in, status="resolved")
        entry_in = booking("2026-06-01", -500)
        entry_out = booking("2027-02-01", -700)
        afa_in = booking("2026-12-31", -300)
        con.executemany("INSERT INTO BookingDocuments (Booking_ID, Document_ID, RelationType) VALUES (?, ?, 'receipt')",
                        [(entry_in, d_in), (entry_in, d_out), (entry_out, d_in), (entry_out, d_out)])
        a1 = con.execute("INSERT INTO Assets (InventoryNumber, Name, PurchaseDate, PurchasePrice, UsefulLifeYears, Status, "
                         "Document_ID, Booking_ID) VALUES ('A-1', 'Laptop', '2026-01-10', 100000, 3, 'active', ?, ?)",
                         (d_in, entry_in)).lastrowid
        a2 = con.execute("INSERT INTO Assets (InventoryNumber, Name, PurchaseDate, PurchasePrice, UsefulLifeYears, Status, "
                         "Document_ID, Booking_ID) VALUES ('A-2', 'Drucker', '2027-01-10', 50000, 3, 'active', ?, ?)",
                         (d_out, entry_out)).lastrowid
        con.execute("INSERT INTO AssetDepreciations (Asset_ID, Year, DepreciationAmount, BookValue, Booking_ID, Status, "
                    "BookedAt) VALUES (?, 2026, 30000, 70000, ?, 'posted', CURRENT_TIMESTAMP)", (a1, afa_in))
        con.execute("INSERT INTO AssetDepreciations (Asset_ID, Year, DepreciationAmount, BookValue, Status) "
                    "VALUES (?, 2027, 30000, 40000, 'planned')", (a1,))
        trip = con.execute("INSERT INTO Trips (DriverID, StartDate, Destination, DocumentID) "
                           "VALUES (?, '2026-06-01', 'Kundenhausen', ?)", (customer, d_in)).lastrowid
        con.executemany("INSERT INTO InvoicePayments (InvoiceID, BookingID, Amount, PaymentDate) VALUES (?, ?, 11900, ?)",
                        [(inv_in, entry_in, "2026-04-01"), (inv_out, entry_in, "2026-04-02"), (inv_out, entry_out, "2027-02-01")])
        con.execute("UPDATE Invoices SET CancelsInvoiceId = ? WHERE ID = ?", (inv_in, inv_out))
        for invoice, number in ((inv_in, "26R001"), (inv_out, "27R001")):
            con.execute("INSERT INTO InvoiceCorrections (InvoiceID, InvoiceNumber, Version, Action, Field, OldValue, NewValue) "
                        "VALUES (?, ?, 2, 'correct', 'OrderNumber', 'a', 'b')", (invoice, number))
            con.execute("INSERT INTO InvoiceDocumentVersions (InvoiceID, Version, Kind, RelPath, Reason) "
                        "VALUES (?, 1, 'pdf', '2026/Fassungen/x/y.pdf', 'corrected')", (invoice,))
        con.commit()
    return SimpleNamespace(db=db, d_in=d_in, d_out=d_out, bank_in=bank_in, child_in=child_in, child_out=child_out,
                           entry_in=entry_in, entry_out=entry_out, afa_in=afa_in, a1=a1, a2=a2, trip=trip,
                           inv_in=inv_in, quote_in=quote_in, inv_out=inv_out)


def _ids(db, table):
    return [row[0] for row in _query(db, f"SELECT ID FROM {table} ORDER BY ID")]


def test_the_delete_script_runs_in_one_transaction_with_foreign_keys_on_and_clears_the_period(books):
    db = books.db
    commands = _commands(db)
    result = _run(db, _script(db), write=True)
    assert result == {"success_count": len(commands), "total": len(commands), "errors": [], "output": "",
                      "rolled_back": False}

    assert _ids(db, "Documents") == [books.d_out]                                                # Beleg des Zeitraums weg
    assert _ids(db, "Bookings") == [books.child_out, books.entry_out]                            # nur, was außerhalb liegt
    # abgehängt wie im Buchungssatz-Editor (``remove_split_row``): ohne Bankbewegung, ein Status ``resolved`` zurückgesetzt
    assert _query(db, f"SELECT ParentBooking_ID, Status, Amount FROM Bookings WHERE ID = {books.child_out}") == [(None, None, -190)]
    assert _query(db, "SELECT Booking_ID, Document_ID FROM BookingDocuments") == [(books.entry_out, books.d_out)]
    assert _ids(db, "Invoices") == [books.inv_out]                                               # Rechnung und Angebot weg
    assert _query(db, "SELECT SourceQuoteId, CancelsInvoiceId, ReplacesInvoiceId FROM Invoices") == [(None, None, None)]
    assert {row[0] for row in _query(db, "SELECT InvoiceId FROM InvoiceItems")} == {books.inv_out}
    assert _query(db, "SELECT InvoiceID, BookingID FROM InvoicePayments") == [(books.inv_out, books.entry_out)]
    assert _query(db, "SELECT InvoiceID FROM InvoiceCorrections") == [(books.inv_out,)]
    assert _query(db, "SELECT InvoiceID FROM InvoiceDocumentVersions") == [(books.inv_out,)]
    assert _query(db, "SELECT Booking_ID, Document_ID FROM Assets ORDER BY ID") == [(None, None), (books.entry_out, books.d_out)]
    assert _query(db, "SELECT Year, Booking_ID, Status, BookedAt FROM AssetDepreciations ORDER BY Year") == [
        (2026, None, "planned", None), (2027, None, "planned", None)]                            # das Jahr ist wieder offen
    assert _query(db, "SELECT DocumentID FROM Trips") == [(None,)]
    assert _query(db, "PRAGMA foreign_key_check") == []                                          # keine Waise bleibt zurück
    assert _query(db, "PRAGMA integrity_check") == [("ok",)]


def test_a_second_run_finds_nothing_left_to_delete(books):
    db = books.db
    assert _run(db, _script(db), write=True)["errors"] == []
    assert _run(db, _script(db), write=True)["errors"] == []


def test_the_script_is_refused_while_reading_and_changes_nothing(books):
    db = books.db
    before = (installation.file_digest(db.db_name), installation.dump(db.db_name))
    result = _run(db, _script(db))                                       # ohne „Schreiben erlauben“
    assert result["success_count"] == 0 and result["errors"][0].endswith("zum Ändern „Schreiben erlauben“ ankreuzen")
    assert (installation.file_digest(db.db_name), installation.dump(db.db_name)) == before


# ── Teil 3: scheitert ein Befehl, bleibt alles ───────────────────────────────
def test_a_failing_command_in_the_middle_of_the_script_undoes_all_the_others(books):
    """Eine Tabelle, die das Skript nicht kennt und die auf einen Beleg des Zeitraums verweist: Das Löschen der Belege am
    Ende des Skripts scheitert am Fremdschlüssel – und alles, was davor gelöscht wurde, steht wieder da."""
    db = books.db
    with _raw(db) as con:
        con.execute("CREATE TABLE Zusatz (Beleg INTEGER REFERENCES Documents (ID))")
        con.execute("INSERT INTO Zusatz VALUES (?)", (books.d_in,))
        con.commit()
    before = (installation.file_digest(db.db_name), installation.dump(db.db_name))
    commands = _commands(db)

    result = _run(db, _script(db), write=True)

    assert result == {"success_count": 0, "total": len(commands), "output": "", "rolled_back": True,
                      "errors": [f"Befehl {len(commands)}: FOREIGN KEY constraint failed",
                                 "Nichts wurde geändert – im Schreibmodus laufen alle Befehle einer Anfrage in einer "
                                 "Transaktion."]}
    assert (installation.file_digest(db.db_name), installation.dump(db.db_name)) == before
    assert _ids(db, "Invoices") == [books.inv_in, books.quote_in, books.inv_out]
    assert len(_ids(db, "Bookings")) == 6 and len(_ids(db, "Documents")) == 2
