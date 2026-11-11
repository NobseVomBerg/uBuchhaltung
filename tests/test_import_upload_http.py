# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""
Kontoauszug-Import über HTTP und die Vorschau-Seite (2.0.6).

* Upload zweier überlappender CAMT-Dateien über ``/upload_receipts``: die Vorschau-Antwort nennt je
  Umsatz ``dup_source`` ("db" | "upload") und ``text_differs``, je Datei ``dup_upload_count`` und
  ``dup_upload_differs_count``.
* ``/confirm_transactions`` ohne ``file_index`` („Alle importieren“) legt nichts doppelt an und nennt
  ``skipped_upload``/``skipped_invalid``; mit ``file_index`` nacheinander kommt dasselbe heraus.
* Die Seite enthält die neuen Texte, und die Karte einer Datei wird nicht mehr zu früh geschlossen:
  ein überzähliges ``</div>`` im Block „Auffällige Buchungen“ ließ den Import-Knopf nach dem
  Einzelimport stehen (die Karte wurde entfernt, der Knopf lag außerhalb).

Das JavaScript der Seite läuft in Node (``vm``, ohne Browser): ``renderPreview``/``renderCard``
liefern das HTML, ``importAll``/``importBeleg`` die Meldungen – gegen Stubs für ``document``,
``fetch`` und ``appMsg``. Ohne Node laufen nur die statischen Prüfungen.

Zugangsdaten, Namen und Beträge sind erfunden; die IBAN ist die erlaubte Test-IBAN.
"""
import json
import re
import shutil
import subprocess
import uuid
from decimal import Decimal
from html.parser import HTMLParser

import pytest

from server.import_preview import build_import_preview, match_account
from server.pages_transactions import PageTransactions
from tests.http_client import own_origin, post, send
from tests.test_confirm_import import (IBAN_A, _txn,  # noqa: F401
                                       db_two_accounts)

OWN_IBAN = "DE89370400440532013000"
NODE = shutil.which("node")
NO_NODE = pytest.mark.skipif(NODE is None, reason="Node.js nicht installiert – die statischen "
                                                  "Prüfungen derselben Datei decken das Wichtigste ab")

E1 = ("119.00", "CRDT", "2026-03-05", "Rechnung R26001")
E1_OTHER_TEXT = ("119.00", "CRDT", "2026-03-05", "Ganz anderer Verwendungszweck")
E2 = ("23.45", "DBIT", "2026-03-10", "Bueromaterial")


# ── CAMT-Dateien und Anfragen ────────────────────────────────────────────────

def _camt(statement_id, entries):
    """Minimaler CAMT.053-Auszug (Aufbau wie ``tests/fixtures/camt``) mit gebuchten Umsätzen
    ``(Betrag, CRDT|DBIT, Datum, Verwendungszweck)``; der Schlusssaldo stimmt."""
    opening = Decimal("1000.00")
    closing = opening + sum((Decimal(amount) if sign == "CRDT" else -Decimal(amount)
                             for amount, sign, _date, _text in entries), Decimal(0))
    body = "".join(
        f'<Ntry><Amt Ccy="EUR">{amount}</Amt><CdtDbtInd>{sign}</CdtDbtInd><Sts>BOOK</Sts>'
        f'<BookgDt><Dt>{date}</Dt></BookgDt><ValDt><Dt>{date}</Dt></ValDt><NtryDtls><TxDtls>'
        f'<RltdPties><Dbtr><Nm>Beispiel Kunde GmbH</Nm></Dbtr></RltdPties>'
        f'<RmtInf><Ustrd>{text}</Ustrd></RmtInf></TxDtls></NtryDtls></Ntry>\n'
        for amount, sign, date, text in entries)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<Document xmlns="urn:iso:std:iso:20022:tech:xsd:camt.053.001.02">\n<BkToCstmrStmt>\n'
        '<GrpHdr><MsgId>MSG1</MsgId><CreDtTm>2026-03-31T08:00:00</CreDtTm></GrpHdr>\n'
        f'<Stmt><Id>{statement_id}</Id><CreDtTm>2026-03-31T08:00:00</CreDtTm>'
        f'<Acct><Id><IBAN>{OWN_IBAN}</IBAN></Id></Acct>\n'
        '<Bal><Tp><CdOrPrtry><Cd>OPBD</Cd></CdOrPrtry></Tp><Amt Ccy="EUR">'
        f'{opening}</Amt><CdtDbtInd>CRDT</CdtDbtInd><Dt><Dt>2026-03-01</Dt></Dt></Bal>\n'
        '<Bal><Tp><CdOrPrtry><Cd>CLBD</Cd></CdOrPrtry></Tp><Amt Ccy="EUR">'
        f'{closing}</Amt><CdtDbtInd>CRDT</CdtDbtInd><Dt><Dt>2026-03-31</Dt></Dt></Bal>\n'
        f'{body}</Stmt>\n</BkToCstmrStmt>\n</Document>\n').encode("utf-8")


def _multipart(files):
    boundary = "GRENZE" + uuid.uuid4().hex
    body = b""
    for name, content in files:
        body += (f'--{boundary}\r\nContent-Disposition: form-data; name="files"; '
                 f'filename="{name}"\r\nContent-Type: text/xml\r\n\r\n').encode() + content + b"\r\n"
    body += f"--{boundary}--\r\n".encode()
    return f"multipart/form-data; boundary={boundary}", body


@pytest.fixture
def giro(server_port, auto_login):
    """Der Server läuft für den automatisch angemeldeten Administrator, dessen Datenbank das
    Bankkonto zur IBAN der CAMT-Dateien hat."""
    auto_login.db.insert_account("Giro", "Owner", OWN_IBAN, "TESTDEFF", "Testbank", skr_account=1800)
    return auto_login


def _upload(port, login, *files):
    content_type, body = _multipart(files)
    response = send(port, "POST", "/upload_receipts",
                    {"Origin": own_origin(port), "X-CSRF-Token": login.csrf,
                     "Content-Type": content_type}, body)
    assert response.status == 200, response.text
    data = json.loads(response.text)
    assert data["import_id"] and not data["other_files"], data
    return data


def _confirm(port, login, import_id, file_index=None):
    form = {"import_id": import_id}
    if file_index is not None:
        form["file_index"] = str(file_index)
    response = post(port, "/confirm_transactions", headers={"Origin": own_origin(port)},
                    form=form, csrf=login.csrf)
    assert response.status == 200, response.text
    data = json.loads(response.text)
    assert data["ok"], data
    return data


def _bookings(login):
    conn = login.db._get_connection()
    try:
        return sorted(conn.execute("SELECT DateBooking, Amount FROM Bookings"
                                   " WHERE BookingType = 'bank'").fetchall())
    finally:
        conn.close()


DAY = ("tagesauszug.xml", _camt("S1", [E1]))
MONTH = ("monatsauszug.xml", _camt("S2", [E1, E2]))
MONTH_OTHER_TEXT = ("monatsauszug.xml", _camt("S2", [E1_OTHER_TEXT, E2]))
BOOKED = [("2026-03-05", 1190000), ("2026-03-10", -234500)]


# ── Upload und Vorschau ──────────────────────────────────────────────────────

def test_upload_of_overlapping_camt_files_marks_upload_duplicates(server_port, giro):
    """Tagesauszug (x1) und Monatsauszug (x1, x2): die Vorschau sagt bei der zweiten Datei, dass der
    Umsatz schon in einer anderen Datei dieses Uploads steckt – nicht im Bestand."""
    data = _upload(server_port, giro, DAY, MONTH)
    first, second = data["files"]

    assert (first["total"], first["new_count"], first["dup_count"], first["dup_upload_count"],
            first["dup_upload_differs_count"]) == (1, 1, 0, 0, 0)
    assert (second["total"], second["new_count"], second["dup_count"], second["dup_upload_count"],
            second["dup_upload_differs_count"]) == (2, 1, 1, 1, 0)
    assert [t["dup_source"] for t in first["transactions"]] == [None]
    assert [t["dup_source"] for t in second["transactions"]] == ["upload", None]
    assert [t["text_differs"] for t in second["transactions"]] == [False, False]
    assert [t["dup"] for t in second["transactions"]] == [True, False]
    assert second["status"] == "warn" and second["account_name"] == "Giro"
    assert _bookings(giro) == []                              # die Vorschau schreibt nichts


def test_preview_flags_an_overlapping_transaction_with_another_text(server_port, giro):
    """Derselbe Umsatz mit anderem Verwendungszweck in der zweiten Datei: „Text weicht ab“."""
    first, second = _upload(server_port, giro, DAY, MONTH_OTHER_TEXT)["files"]
    assert second["transactions"][0]["dup_source"] == "upload"
    assert [t["text_differs"] for t in second["transactions"]] == [True, False]
    assert (second["dup_upload_count"], second["dup_upload_differs_count"]) == (1, 1)
    assert first["dup_upload_differs_count"] == 0


def test_same_camt_file_twice_in_one_upload(server_port, giro):
    first, second = _upload(server_port, giro, MONTH, ("kopie.xml", MONTH[1]))["files"]
    assert (first["new_count"], first["dup_count"]) == (2, 0)
    assert (second["new_count"], second["dup_count"], second["dup_upload_count"]) == (0, 2, 2)
    assert [t["dup_source"] for t in second["transactions"]] == ["upload", "upload"]


# ── Bestätigen ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("mode", ["all", "single"])
def test_confirm_imports_each_transaction_once(server_port, giro, mode):
    """Ohne ``file_index`` („Alle importieren“) und mit ``file_index`` nacheinander: dieselben zwei
    Buchungen, nichts doppelt. ``skipped_upload`` kennt nur der Lauf über alle Dateien."""
    data = _upload(server_port, giro, DAY, MONTH)
    if mode == "all":
        answer = _confirm(server_port, giro, data["import_id"])
        results, done = answer["results"], answer["all_done"]
        assert done is True
    else:
        answers = [_confirm(server_port, giro, data["import_id"], index) for index in (0, 1)]
        assert [a["all_done"] for a in answers] == [False, True]
        results = [a["results"][0] for a in answers]

    assert [(r["inserted"], r["skipped"], r["skipped_invalid"]) for r in results] == [
        (1, 0, 0), (1, 1, 0)]
    assert [r["skipped_upload"] for r in results] == ([0, 1] if mode == "all" else [0, 0])
    assert all(r["account_found"] and r["error"] is None for r in results)
    assert _bookings(giro) == BOOKED


def test_a_second_identical_upload_inserts_nothing(server_port, giro):
    """Wer denselben Upload noch einmal schickt, sieht alles als Duplikat aus dem Bestand – und der
    Import legt nichts an."""
    _confirm(server_port, giro, _upload(server_port, giro, DAY, MONTH)["import_id"])
    assert _bookings(giro) == BOOKED

    again = _upload(server_port, giro, DAY, MONTH)
    first, second = again["files"]
    assert (first["new_count"], first["dup_count"], first["dup_upload_count"]) == (0, 1, 0)
    assert (second["new_count"], second["dup_count"], second["dup_upload_count"]) == (0, 2, 0)
    assert [t["dup_source"] for t in second["transactions"]] == ["db", "db"]

    results = _confirm(server_port, giro, again["import_id"])["results"]
    assert [(r["inserted"], r["skipped"], r["skipped_upload"]) for r in results] == [
        (0, 1, 0), (0, 2, 0)]
    assert _bookings(giro) == BOOKED


def test_confirm_leaves_the_text_of_the_first_file(server_port, giro):
    """Der Hinweis „Text weicht ab“ ändert den Import nicht: Maximum über die Dateien, der Text der
    ersten Datei bleibt."""
    data = _upload(server_port, giro, DAY, MONTH_OTHER_TEXT)
    results = _confirm(server_port, giro, data["import_id"])["results"]
    assert [(r["inserted"], r["skipped"], r["text_updated"]) for r in results] == [
        (1, 0, 0), (1, 1, 0)]
    conn = giro.db._get_connection()
    try:
        texts = sorted(row[0] for row in conn.execute(
            "SELECT Text FROM Bookings WHERE BookingType = 'bank'"))
    finally:
        conn.close()
    assert texts == ["Bueromaterial", "Rechnung R26001"]


# ── Die Seite: Texte ─────────────────────────────────────────────────────────

def _page_script(db):
    """Das Skript der Transaktionsseite, das Vorschau und Import steuert."""
    page = PageTransactions(db, date_from="2026-01-01", date_to="2026-12-31")
    (script,) = [s for s in re.findall(r"<script>(.*?)</script>", page, re.S)
                 if "function renderCard(" in s]
    return script


def _preview_source(db):
    """Vorschau-Teil des Skripts: ab ``currentImportId`` bis vor das Bearbeitungsformular. Er
    enthält nur Definitionen (und einen Klick-Hörer), lässt sich also ohne Seite ausführen."""
    script = _page_script(db)
    return script[script.index("let currentImportId = null;"):script.index("function openEditForm(")]


def test_page_contains_the_texts_for_upload_duplicates_and_incomplete_rows(tmp_db):
    script = _page_script(tmp_db)
    for text in ("bereits in anderer Datei dieses Uploads", "mit abweichendem Text",
                 "'in anderer Datei dieses Uploads'", "Text weicht ab, bitte prüfen",
                 "ohne Datum/Betrag – wird nicht importiert", "aus anderer Datei dieses Uploads",
                 "ohne Datum/Betrag nicht importiert"):
        assert text in script, text
    assert "f.dup_upload_differs_count" in script and "p.text_differs" in script
    assert "res.skipped_invalid" in script and "r.skipped_invalid" in script


def test_card_problem_block_no_longer_closes_the_card(tmp_db):
    """Der Block „Auffällige Buchungen“ schließt nur seine Tabelle – ein ``</div>`` dort schlösse die
    Karte, und der Import-Knopf bliebe nach dem Einzelimport stehen."""
    script = _page_script(tmp_db)
    card = script[script.index("function renderCard("):script.index("document.addEventListener('click'")]
    problem_block = card[card.index("Auffällige Buchungen"):card.index("btnToggleDet")]
    assert "h += '</table>';" in problem_block
    assert "</table></div>" not in problem_block            # das war der Fehler
    assert card.count("</table></div>") == 1                # nur die Tabelle der Einzelbuchungen


# ── Die Seite: HTML und Meldungen im Skript (Node) ───────────────────────────

HARNESS = r"""
const vm = require('vm');
const fs = require('fs');
const [, , sourceFile, scenarioFile] = process.argv;
const scenario = JSON.parse(fs.readFileSync(scenarioFile, 'utf8'));
const messages = [];
const elements = {};
function element(id) {
  return elements[id] || (elements[id] = {id, innerHTML: '', style: {}, removed: false,
                                          remove() { this.removed = true; }});
}
const sandbox = {
  document: {getElementById: element, querySelectorAll: () => [], addEventListener: () => {}},
  window: {location: {href: 'http://127.0.0.1/transactions'}},
  appMsg: (text, kind) => messages.push([text, kind]),
  fetch: () => Promise.resolve({json: () => Promise.resolve(scenario.response || {}),
                                text: () => Promise.resolve('')}),
  DOMParser: class { parseFromString() { return {getElementById: () => null}; } },
  URLSearchParams, console,
};
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(sourceFile, 'utf8'), sandbox);
(async () => {
  const out = {};
  if (scenario.preview) {
    vm.runInContext('renderPreview(' + JSON.stringify(scenario.preview) + ')', sandbox);
    out.html = element('importPreview').innerHTML;
  }
  if (scenario.action) {
    vm.runInContext(scenario.action, sandbox);
    await new Promise(resolve => setTimeout(resolve, 50));
  }
  out.messages = messages;
  out.removed = Object.values(elements).filter(e => e.removed).map(e => e.id);
  console.log(JSON.stringify(out));
})();
"""


def _node(tmp_path, source, scenario):
    """Führt das Skript mit einem Szenario aus (Vorschau-Daten, Antwort des Servers, Aufruf)."""
    harness, script, data = (tmp_path / name for name in ("harness.js", "preview.js", "scenario.json"))
    harness.write_text(HARNESS, encoding="utf-8")
    script.write_text(source, encoding="utf-8")
    data.write_text(json.dumps(scenario), encoding="utf-8")
    done = subprocess.run([NODE, str(harness), str(script), str(data)], capture_output=True,
                          text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


class Tree(HTMLParser):
    """Baut aus erzeugtem HTML einen Baum. Ein Endtag ohne passenden Starttag und ein nicht
    geschlossenes Element stehen in ``errors`` – so sieht man, ob eine Karte zu früh endet."""

    VOID = {"br", "hr", "img", "input", "meta", "link"}

    def __init__(self, text):
        super().__init__(convert_charrefs=True)
        self.root = {"tag": "#root", "attrs": {}, "children": [], "text": ""}
        self.stack = [self.root]
        self.errors = []
        self.feed(text)
        self.close()
        if len(self.stack) > 1:
            self.errors.append("nicht geschlossen: " + ", ".join(n["tag"] for n in self.stack[1:]))

    def handle_starttag(self, tag, attrs):
        node = {"tag": tag, "attrs": dict(attrs), "children": [], "text": ""}
        self.stack[-1]["children"].append(node)
        if tag not in self.VOID:
            self.stack.append(node)

    def handle_endtag(self, tag):
        if len(self.stack) > 1 and self.stack[-1]["tag"] == tag:
            self.stack.pop()
        else:
            self.errors.append(f"unerwartetes </{tag}>")

    def handle_data(self, data):
        self.stack[-1]["text"] += data


def _walk(node):
    yield node
    for child in node["children"]:
        yield from _walk(child)


def _text(node):
    return "".join(n["text"] for n in _walk(node))


def _buttons(card, css_class):
    return [n for n in _walk(card) if n["tag"] == "button" and css_class in n["attrs"].get("class", "")]


@pytest.fixture
def preview_data(db_two_accounts):
    """Vorschau eines Uploads mit allem, was eine Karte zeigen kann: ein unvollständiger Umsatz, ein
    Duplikat aus einer anderen Datei mit abweichendem und eines mit passendem Text, ein Duplikat aus
    dem Bestand und eine Datei ohne Konto."""
    db_two_accounts.insert_booking("2026-03-20", -3.0, account_id=_account(db_two_accounts),
                                   text="Bestand", booking_type="bank")
    incomplete = _txn(None, 5.0, "ohne Datum")

    def one_file(name, transactions, iban=IBAN_A):
        return {"filename": name, "bank_code": "CAMT", "iban": iban, "document_date": "2026-03-31",
                "transactions": transactions}

    files = [
        one_file("eins.xml", [_txn("2026-03-15", -9.99, "Netflix"), incomplete]),
        one_file("zwei.xml", [_txn("2026-03-15", -9.99, "Spotify"), _txn("2026-03-16", -5.0, "Neu")]),
        one_file("drei.xml", [_txn("2026-03-15", -9.99, "Netflix Abo")]),
        one_file("vier.xml", [_txn("2026-03-01", -1.0, "Fremd")], iban="DE00000000000000000000"),
        one_file("fuenf.xml", [_txn("2026-03-20", -3.0, "Bestand")]),
    ]
    return build_import_preview(db_two_accounts, {"files": files})


def _account(db):
    return match_account(db.fetch_accounts(), IBAN_A)[0]


@NO_NODE
def test_preview_html_keeps_every_card_closed_and_its_import_button_inside(tmp_path, db_two_accounts,
                                                                           preview_data):
    """Das HTML von ``renderPreview`` hat gleich viele öffnende und schließende Elemente; jede Karte
    ist ein eigenes Element unterhalb der Vorschau und enthält ihren Import-Knopf – so verschwindet
    er mit der Karte nach dem Einzelimport."""
    out = _node(tmp_path, _preview_source(db_two_accounts), {"preview": {
        "files": preview_data["files"], "other_files": []}})
    tree = Tree(out["html"])
    assert tree.errors == []

    cards = [n for n in tree.root["children"] if n["attrs"].get("id", "").startswith("beleg-")]
    assert [c["attrs"]["id"] for c in cards] == [f"beleg-{i}" for i in range(5)]
    # nichts steht neben den Karten außer der Kopfzeile mit „Alle importieren“ / „Verwerfen“
    others = [n for n in tree.root["children"] if n not in cards]
    assert len(others) == 1 and _buttons(others[0], "btnImportAll") and _buttons(others[0], "btnDismiss")

    for index, card in enumerate(cards):
        imports = _buttons(card, "btnImportBeleg")
        if index == 3:                                            # Datei ohne Konto
            assert imports == [] and "Import nicht möglich" in _text(card)
        else:
            assert [b["attrs"]["data-idx"] for b in imports] == [str(index)]
        assert len(_buttons(card, "btnToggleDet")) == 1
        assert any(n["attrs"].get("id") == f"det-{index}" for n in _walk(card))   # Einzelbuchungen


@NO_NODE
def test_card_texts_name_where_a_duplicate_comes_from(tmp_path, db_two_accounts, preview_data):
    out = _node(tmp_path, _preview_source(db_two_accounts), {"preview": {
        "files": preview_data["files"], "other_files": []}})
    cards = [n for n in Tree(out["html"]).root["children"]
             if n["attrs"].get("id", "").startswith("beleg-")]
    texts = [re.sub(r"\s+", " ", _text(c)) for c in cards]

    assert "2 erkannt · 1 neu · 0 mögliche Duplikate" in texts[0]
    assert "(davon" not in texts[0]
    assert "ohne Datum/Betrag – wird nicht importiert" in texts[0]
    assert "2 erkannt · 1 neu · 1 mögliche Duplikate (davon 1 bereits in anderer Datei dieses Uploads, " \
           "davon 1 mit abweichendem Text)" in texts[1]
    assert "in anderer Datei dieses Uploads – Text weicht ab, bitte prüfen" in texts[1]
    assert "1 mögliche Duplikate (davon 1 bereits in anderer Datei dieses Uploads)" in texts[2]
    assert "mit abweichendem Text" not in texts[2] and "Text weicht ab" not in texts[2]
    assert "in anderer Datei dieses Uploads" in texts[2]
    assert "Duplikat" in texts[4] and "in anderer Datei" not in texts[4]
    assert "(davon" not in texts[4]


@NO_NODE
def test_card_marks_upload_duplicates_in_the_row_not_only_in_the_counter(tmp_path, db_two_accounts,
                                                                         preview_data):
    out = _node(tmp_path, _preview_source(db_two_accounts), {"preview": {
        "files": preview_data["files"], "other_files": []}})
    tree = Tree(out["html"])
    rows = [n for n in _walk(tree.root) if n["tag"] == "tr" and "row-open" in n["attrs"].get("class", "")
            and len([c for c in n["children"] if c["tag"] == "td"]) == 5]     # Tabelle der Auffälligen
    tags = [[td["text"] for td in r["children"] if td["tag"] == "td"][-1] for r in rows]
    assert len(tags) == 4
    assert tags[0].endswith("ohne Datum/Betrag – wird nicht importiert")     # evtl. mit „Datum?“ davor
    assert tags[1:] == ["in anderer Datei dieses Uploads – Text weicht ab, bitte prüfen",
                        "in anderer Datei dieses Uploads",
                        "Duplikat"]


@NO_NODE
def test_messages_after_the_import_name_upload_duplicates_and_incomplete_rows(tmp_path, db_two_accounts):
    """„Alle importieren“: Duplikate (davon aus anderer Datei dieses Uploads), unvollständige Umsätze
    und Dateien ohne Konto stehen in der Meldung."""
    response = {"ok": True, "all_done": False, "results": [
        {"file_index": 0, "inserted": 1, "skipped": 0, "skipped_upload": 0, "skipped_invalid": 1,
         "text_updated": 0, "account_found": True, "error": None},
        {"file_index": 1, "inserted": 0, "skipped": 2, "skipped_upload": 2, "skipped_invalid": 2,
         "text_updated": 1, "account_found": True, "error": None},
        {"file_index": 2, "inserted": 0, "skipped": 0, "account_found": False, "error": "kein Konto"},
    ]}
    out = _node(tmp_path, _preview_source(db_two_accounts), {"response": response,
                                                             "action": "importAll()"})
    ((text, kind),) = out["messages"]
    assert text == ("1 neu importiert, 2 Duplikate (davon 2 aus anderer Datei dieses Uploads), "
                    "1 Texte vervollständigt, 3 ohne Datum/Betrag nicht importiert, 1 ohne Konto")
    assert kind == "warn"


@NO_NODE
def test_message_without_extras_stays_short(tmp_path, db_two_accounts):
    response = {"ok": True, "results": [
        {"file_index": 0, "inserted": 3, "skipped": 0, "skipped_upload": 0, "skipped_invalid": 0,
         "text_updated": 0, "account_found": True, "error": None}]}
    out = _node(tmp_path, _preview_source(db_two_accounts), {"response": response,
                                                             "action": "importAll()"})
    assert out["messages"] == [["3 neu importiert", "success"]]


@NO_NODE
def test_single_import_message_and_the_card_is_removed(tmp_path, db_two_accounts):
    """Einzelimport: Meldung mit „davon aus anderer Datei“ und „ohne Datum/Betrag“; die Karte mit
    ihrem Knopf verschwindet."""
    response = {"ok": True, "results": [
        {"file_index": 1, "inserted": 0, "skipped": 2, "skipped_upload": 2, "skipped_invalid": 1,
         "text_updated": 0, "account_found": True, "error": None}]}
    out = _node(tmp_path, _preview_source(db_two_accounts), {"response": response,
                                                             "action": "importBeleg(1)"})
    assert out["messages"] == [[
        "0 neu importiert, 2 Duplikate übersprungen (davon 2 aus anderer Datei dieses Uploads), "
        "1 ohne Datum/Betrag nicht importiert", "success"]]
    assert out["removed"] == ["beleg-1"]
