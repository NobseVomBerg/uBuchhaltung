# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Belege und Buchungen verknüpfen (seit 2.5.12): Suchbegriffe lesen, Treffer und verknüpfte Listen als JSON-Daten, das gemeinsame
Skript der Seite eines Belegs und der Buchungsmaske. Die Routen stehen in ``server/handlers.py``."""
import re
from money import fmt_eur, from_minor, parse_amount, to_minor
from db.bookings import RELATION_LABELS
from db.core import SEARCH_LIMIT_MAX
from . import receipt_files

SEARCH_MAX_LENGTH = 100
SEARCH_LIMIT_DEFAULT = 20
TEXT_PREVIEW = 120
SEARCH_TOO_LONG = f'Der Suchbegriff ist zu lang (höchstens {SEARCH_MAX_LENGTH} Zeichen).'
SEARCH_FAILED = 'Die Suche ist fehlgeschlagen – Einzelheiten stehen im Server-Log.'
SEARCH_FOREIGN = 'Die Suche ist nur über die Seiten der App erreichbar.'
LINK_FAILED = 'Die Verknüpfung konnte nicht gespeichert werden. Einzelheiten stehen im Server-Log.'
UNLINK_FAILED = 'Die Verknüpfung konnte nicht gelöst werden. Einzelheiten stehen im Server-Log.'
KIND_LABELS = {'bank': 'Bankbewegung', 'child': 'Satz einer Bankbewegung', 'entry': 'Buchungssatz'}
_DATE_PREFIX = re.compile(r'[0-9]{4}(-[0-9]{2}(-[0-9]{2})?)?')


def _text(value):
    return '' if value is None else str(value)


def search_terms(q):
    """Was die Suche aus dem Suchbegriff liest: ``(text, date_prefix, amount_minor)``.

    ``text`` ist der Begriff ohne Rücksicht auf die Schreibweise (``casefold``); ``date_prefix`` der Begriff selbst, wenn er ein
    Datum oder dessen Anfang ist (``JJJJ``, ``JJJJ-MM``, ``JJJJ-MM-TT``), sonst ``None``; ``amount_minor`` der Betrag in Minor Units
    ohne Vorzeichen, wenn der Begriff eine Ziffer enthält und als Betrag lesbar ist (``money.parse_amount``: ``12,50``, ``-12.50``,
    ``12.5``), sonst ``None``."""
    text = q.casefold()
    date_prefix = q if _DATE_PREFIX.fullmatch(q) else None
    amount_minor = None
    if any('0' <= char <= '9' for char in q):
        try:
            amount_minor = to_minor(abs(parse_amount(q)))
        except ValueError:
            amount_minor = None
    return text, date_prefix, amount_minor


def file_state(stored):
    """Was zur Datei eines Belegs zu sagen ist, ohne den Pfad zu nennen: ``(Zustand, anzeigbar)`` – ``('none', False)``: kein Pfad
    gespeichert, ``('outside', False)``: ein Pfad, der nicht gilt oder dessen Datei fehlt, ``('ok', inline)``: es gibt die Datei
    (``inline``: der Browser darf sie anzeigen, ``receipt_files.inline_type``)."""
    info = receipt_files.file_info(stored)
    if info is None:
        return 'none', False
    if info is False:
        return 'outside', False
    return 'ok', bool(info.inline)


def receipt_payload(row):
    """Ein Beleg als JSON-Daten. ``row``: ``(ID, Number, Date, Filename, Path, Info)``, bei einem verknüpften Beleg mit der Art
    der Verknüpfung als siebter Spalte. Der Pfad steht nie in der Ausgabe – nur ``file`` (``none``/``outside``/``ok``) und
    ``inline``."""
    state, inline = file_state(row[4])
    payload = {'id': row[0], 'number': _text(row[1]), 'date': _text(row[2]), 'filename': _text(row[3]), 'info': _text(row[5]),
               'file': state, 'inline': inline}
    if len(row) > 6:
        payload['relation'] = _text(row[6])
        payload['relationLabel'] = RELATION_LABELS.get(row[6]) or _text(row[6]) or '–'
    return payload


def booking_payload(row):
    """Eine Buchung als JSON-Daten. ``row``: eine Zeile aus ``search_bookings`` (``ID, DateBooking, Amount, Currency,
    RecipientClient, Text, DocumentNumber, BookingType, ParentBooking_ID``) oder aus ``get_bookings_for_document`` (mit der Art der
    Verknüpfung als zehnter Spalte). Der Betrag steht als Anzeigetext (``12,50``) – nur, wenn er als ganze Zahl gespeichert ist,
    sonst leer; der Text ist auf ``TEXT_PREVIEW`` Zeichen gekürzt."""
    amount = row[2]
    amount_text = fmt_eur(from_minor(amount), symbol=False) if isinstance(amount, int) and not isinstance(amount, bool) else ''
    kind = 'bank' if row[7] == 'bank' else 'child' if row[8] else 'entry'
    text = _text(row[5])
    if len(text) > TEXT_PREVIEW:
        text = text[:TEXT_PREVIEW - 1] + '…'
    payload = {'id': row[0], 'date': _text(row[1]), 'amount': amount_text, 'currency': _text(row[3]) or 'EUR',
               'recipient': _text(row[4]), 'text': text, 'documentNumber': _text(row[6]), 'kind': kind,
               'kindLabel': KIND_LABELS[kind]}
    if len(row) > 9:
        payload['relation'] = _text(row[9])
        payload['relationLabel'] = RELATION_LABELS.get(row[9]) or _text(row[9]) or '–'
    return payload


def linked_documents(db, booking_id):
    """Die Belege einer Buchung als JSON-Daten (``receipt_payload`` mit der Art der Verknüpfung)."""
    return [receipt_payload(row) for row in db.get_documents_for_booking(booking_id)]


def linked_bookings(db, document_id):
    """Die Buchungen eines Belegs als JSON-Daten (``booking_payload`` mit der Art der Verknüpfung)."""
    return [booking_payload(row) for row in db.get_bookings_for_document(document_id)]


# Das Skript, das die Seite eines Belegs und die Buchungsmaske teilen. Werte aus der Datenbank gehen nur über textContent und
# createElement in die Seite, nie als Markup. Nur ``var`` und ``function``: die Maske spielt das Skript bei jedem Öffnen neu ein.
LINK_WIDGET_JS = r'''/* Belege und Buchungen verknüpfen (2.5.12) – gemeinsam für die Seite eines Belegs und die Buchungsmaske. Werte aus der
   Datenbank nur über textContent/createElement; nur var und function: Die Maske spielt das Skript bei jedem Öffnen neu ein. */
function linkEl(tag, text, className) {
    var el = document.createElement(tag);
    if (text !== undefined && text !== null) el.textContent = String(text);
    if (className) el.className = className;
    return el;
}
function linkRow(cells) {
    var tr = document.createElement('tr');
    cells.forEach(function (cell) {
        var td = document.createElement('td');
        if (cell instanceof Node) td.appendChild(cell);
        else td.textContent = (cell === undefined || cell === null) ? '' : String(cell);
        tr.appendChild(td);
    });
    return tr;
}
function linkButton(text, color, onClick) {
    var b = linkEl('button', text, 'coloredButton btn-sm ' + color);
    b.type = 'button';
    b.addEventListener('click', onClick);
    return b;
}
function linkAnchor(text, href, newTab) {
    var a = linkEl('a', text);
    a.href = href;
    if (newTab) { a.target = '_blank'; a.rel = 'noopener'; }
    return a;
}
function linkActions(nodes) {
    var span = document.createElement('span');
    nodes.forEach(function (node, i) { if (i) span.appendChild(document.createTextNode(' | ')); span.appendChild(node); });
    return span;
}
function linkWho(b) { return [b.recipient, b.text].filter(function (t) { return t; }).join(' · '); }
function linkTable(target, header, rows, emptyText) {
    target.textContent = '';
    if (!rows.length) { target.appendChild(linkEl('p', emptyText, 'muted')); return; }
    var table = document.createElement('table');
    var head = document.createElement('tr');
    header.forEach(function (name) { head.appendChild(linkEl('th', name)); });
    table.appendChild(head);
    rows.forEach(function (row) { table.appendChild(row); });
    target.appendChild(table);
}
function linkAnswer(response) {
    return response.text().then(function (text) {
        var data = null;
        try { var parsed = JSON.parse(text); if (parsed && typeof parsed === 'object') data = parsed; } catch (e) { data = null; }
        if (!response.ok || data === null || data.success !== true) {
            throw new Error(data && data.error ? data.error
                : 'Der Server hat die Anfrage abgelehnt (Status ' + response.status + ').');
        }
        return data;
    });
}
function linkFail(error) {
    appMsg(error instanceof TypeError || !error || !error.message
        ? 'Die Verbindung zum Server ist fehlgeschlagen.' : error.message, 'error');
}
function linkSearch(url, query, target, header, toRow, still) {
    /* still (optional): eine Funktion ohne Argumente – gibt sie false, gehört die Antwort nicht mehr zur geöffneten Seite, und nichts wird gezeichnet */
    fetch(url + '?q=' + encodeURIComponent(query.trim()))
        .then(linkAnswer)
        .then(function (data) {
            if (still && !still()) return;
            linkTable(target, header, (data.results || []).map(toRow), 'Keine Treffer.');
        })
        .catch(linkFail);
}
function linkPost(url, bookingId, documentId) {
    return fetch(url, {method: 'POST', headers: {'Content-Type': 'application/json'},
                       body: JSON.stringify({bookingId: bookingId, documentId: documentId})}).then(linkAnswer);
}
function linkStale(data) {
    /* Die Verknüpfung ist gespeichert, aber der Server konnte die Listen danach nicht lesen: nichts neu zeichnen, ein Hinweis genügt */
    if (!data.stale) return false;
    appMsg('Gespeichert – bitte die Seite neu laden.', 'info');
    return true;
}
function linkDoneText(data) {
    var text = data.created ? 'Zugeordnet' : 'Die Verknüpfung bestand schon';
    if (data.numberTaken && data.hasEntries) {
        return text + ' – die Bankbewegung trägt jetzt die Beleg-Nr. ' + data.documentNumber
            + '; ihre Buchungssätze behalten ihre eigenen Nummern.';
    }
    if (data.numberTaken) return text + ' – die Buchung trägt jetzt die Beleg-Nr. ' + data.documentNumber + '.';
    if (data.numberInUse) {
        return text + '. Die Beleg-Nr. wurde nicht übernommen: Eine andere Buchung trägt bereits diese oder eine ähnliche Nummer.';
    }
    return text + '.';
}
function linkUndoneText(data) {
    return data.removed ? 'Verknüpfung gelöst – die Beleg-Nr. der Buchung bleibt.' : 'Die Verknüpfung bestand nicht (mehr).';
}
function linkFileActions(doc) {
    var nodes = [];
    var id = encodeURIComponent(String(doc.id));
    if (doc.file === 'ok' && doc.inline) nodes.push(linkAnchor('Ansehen', '/receipts/file?id=' + id + '&inline=1', true));
    if (doc.file === 'ok') {
        var download = linkAnchor('Herunterladen', '#', false);
        download.addEventListener('click', function (e) { e.preventDefault(); appDownload('/receipts/file?id=' + id); });
        nodes.push(download);
    }
    return nodes;
}
'''
