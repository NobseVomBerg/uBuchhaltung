# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Invoice pages: list and create/edit – extracted from pages.py"""
import datetime
import json
import html as _html
import re
import traceback
from typing import NamedTuple, Optional
from urllib.parse import quote
from db import Database
from db.invoices import PAYMENT_MEANS, PAYMENT_MEANS_DEFAULT, SERVICE_PERIOD_MESSAGE, InvoiceCol, is_storno_row, payment_means_code
from db.invoice_lock import (CLOSED, EDITABLE, LOCKED_STATUSES, NUMBER_KEPT_MESSAGE, STATUS_LABELS,
                             STORNO_LABEL, STORNO_LOCKED_MESSAGE, allowed_targets, as_document_version,
                             buyer_snapshot, canonical_status, status_group)
from db.invoice_payments import MIXED_REVENUE_MESSAGE, PAYABLE_STATUSES, SKONTO_NO_TERMS, has_mixed_rates, uses_small_business_rule
from db.invoice_versions import DELIVERY_DATE_MESSAGE, PROTOCOL_LABELS, local_date, local_datetime
from db.payment_matching import reason_text
from db.tax_rates import entry_snapshot, legacy_expressible
from money import fmt_eur, format_plain, from_minor, round_minor, to_minor
import tax_groups
from tax_groups import EXEMPT, bp_pct, group_label, rate_pct
from export.einvoice_model import (SCOPE_EN16931, SCOPE_XRECHNUNG, build_einvoice, einvoice_gaps, gap_texts,
                                   skonto_until, unit_display)
from export.invoice_files import stored_invoice_path, version_files, wants_xrechnung
from .pages import (
    Header1, Header2, Header3, Footer, logo_url, script_json,
)
from .period import period_filter_widget


def _json_for_script(obj) -> str:
    """json.dumps, aber sicher zum Einbetten in <script>: neutralisiert
    </script> sowie HTML-Sonderzeichen (json.dumps escaped '<' '>' '&' nicht)."""
    return (json.dumps(obj)
            .replace('<', '\\u003c')
            .replace('>', '\\u003e')
            .replace('&', '\\u0026'))

# Invoice status constants
INVOICE_STATUS_COLORS: dict = {
    'draft':           '#888',
    'finalized':       '#0066cc',
    'sent':            '#ff9900',
    'partial_payment': '#e69500',
    'overdue':         '#b71c1c',
    'paid':            '#00aa00',
    'cancelled':       '#cc0000',
}
INVOICE_STATUS_LABELS: dict = STATUS_LABELS     # eine Quelle: db/invoice_lock.py
STORNO_COLOR = INVOICE_STATUS_COLORS['cancelled']   # eine Stornorechnung trägt statt des Status ihre Kennzeichnung


# Löschen eines Entwurfs: Rückfrage, und – war die Rechnung schon einmal versendet – der Hinweis, was bleibt
DELETE_DRAFT_QUESTION = 'Entwurf wirklich löschen?'
DELETE_SENT_BEFORE_NOTE = ('Diese Rechnung war bereits versendet. Die versendete Fassung bleibt im Ordner '
                           '„Fassungen“ erhalten, die Nummer wird nicht neu vergeben.')

# Versendete Rechnung: was änderbar ist und was eine Änderung bewirkt (Berichtigen, db/invoice_versions.py)
SENT_HINT = ('Diese Rechnung ist versendet. Änderbar sind Zahlungshinweis, Anschrift, Bestellnummer, Leitweg-ID '
             'und Leistungsdatum. Jede Änderung wird protokolliert; die bisherige Fassung bleibt als Datei '
             'erhalten, und es entsteht eine neue Fassung, die dem Kunden erneut zugehen muss.')
CORRECT_QUESTION = ('Rechnung berichtigen? Die bisherige Fassung bleibt als Datei erhalten, die Änderung wird '
                    'protokolliert, und die neue Fassung muss dem Kunden erneut zugehen.')

# Neue Rechnung oder neues Angebot ohne Quelle, dessen Häkchen „Umsatzsteuer ausweisen“ aus der Vorbelegung abgehakt startet
# (Database.last_document_shows_tax): Ein Beleg ohne Steuer entsteht nicht unbemerkt. Feste Zeichenfolge – kein Wert aus der
# Datenbank –, mit der vorhandenen Klasse für Warnhinweise
TAX_PREFILL_NOTE = ('<p class="warnColor" id="tax_prefill_note">Vorbelegt wie die letzte Rechnung: ohne Umsatzsteuer '
                    '(Kleinunternehmer, §&nbsp;19 UStG).</p>')


# ── Steuersatz je Position (seit 2.7.0) und Steuersätze der Stammdaten (seit 2.8.0) ──────────────────────────
# Das Häkchen „Steuersatz je Position“, die Spalte „USt“ der Positionstabelle und das Auswahlfeld „Mehrwertsteuer“ im Kopf – für Rechnung UND Angebot (``pages_quote``
# nimmt dieselben Bausteine). Wählbar sind die Einträge der Stammdaten (``entry_choices``: aktiv und gültig, in ihrer Reihenfolge): Die Auswahlfelder, die der Server schreibt
# (Kopf und Zeilen), und die der neuen Zeilen, die der Browser anlegt (die Liste als JSON im Skriptblock, ``MIXED_TAX_JS``), lesen beide diese eine Liste. Kein Satz, keine
# ID und keine Bezeichnung steht im Quelltext der Maske. Ohne Liste (``choices=None``) gelten die Sätze von ``tax_groups.MIXED_TAX_RATES`` – die Liste vor 2.8.0.
#
# **Was die Maske schickt:** Eine Option trägt nur einen Satz (``Choice.entry_id`` ist ``None``), wenn der bisherige Payload den Eintrag ohne Verlust trägt
# (``db.tax_rates.legacy_expressible``: die Vorbelegung 19 und 7 Prozent ohne Erlöskonto): Dann geht, was die Maske immer schickte – kein ``taxRateId``. Jeder andere Eintrag
# (steuerfrei, mit Erlöskonto, mit Nachkommastellen im Satz) schickt seine ID: im Kopf (ohne Häkchen) als letzter Schlüssel des Kopfs, mit dem Häkchen als letzter Schlüssel
# der Position. Eine Option mit Eintrag trägt ihre Angaben als ``data-``Attribute, eine ohne hat nur ihren Wert – den Satz.

MIXED_RATE_UNSET = '– wählen –'        # die Auswahl einer Zeile, deren gespeicherter Satz sich nicht lesen lässt: nichts wird still umgedeutet
GONE_VALUE = 'e0'                      # die Option „gespeichert …“: eine Position mit Momentaufnahme, deren Eintrag es nicht mehr gibt – sie schickt ``taxRateId: 0``
GONE_TEXT = 'gespeichert, in den Stammdaten nicht mehr vorhanden'

# Die Spalten einer gespeicherten Position (``Database.get_invoice_items``, ``SELECT *``): Art, Satz, Satz in Hundertstel Prozent, Grund der Befreiung, Erlöskonto
ITEM_TAX_CATEGORY, ITEM_TAX_RATE, ITEM_TAX_RATE_BP, ITEM_TAX_EXEMPT_REASON, ITEM_REVENUE_COA = 9, 10, 11, 12, 13


def rate_key(value):
    """Ein Steuersatz als Wert eines Auswahlfelds: die Prozentzahl als Text ('19', '7', '5.5'), oder ``None``, wenn er sich nicht lesen lässt. Gespeichert ist
    er teils als Bruch (0,19), teils als Prozentzahl (19), auch als Text: ``tax_groups.rate_pct`` bringt alles auf dieselbe Form; ``'f'`` schreibt nie ``1E+1``."""
    try:
        percent = rate_pct(value)
    except (ArithmeticError, ValueError, TypeError):
        return None
    return format(percent, 'f') if percent is not None and percent.is_finite() else None


def rate_label(key):
    """Die Beschriftung eines Satzes in Auswahl und Summenzeile, mit Dezimalkomma: '19 %', '5,5 %'."""
    return key.replace('.', ',') + ' %'


def article_json(article):
    """Ein Artikel (eine Zeile von ``Database.fetch_articles`` bzw. ``get_article_by_id``) als Eintrag von ``articlesData`` – für die Masken von Rechnung und Angebot und für die Antwort von
    ``POST /articles/quick-add``: EINE Form für beide. ``mixRate``: der Auswahlwert für „Steuersatz je Position“ (``rate_key``; ``None``, wenn der Satz sich nicht lesen lässt)."""
    return {
        'id': article[0],
        'name': article[1] or '',
        'unit': unit_display(article[2]),
        'price': float(article[3]) if article[3] is not None else 0,  # Decimal -> JSON-fähig
        'taxRate': article[4] if article[4] is not None else 19,  # 0% nicht zu 19% verfälschen
        'mixRate': rate_key(article[4]),
        'description': article[5] or ''
    }


def mixed_rate_choices():
    """Die wählbaren Sätze als ``(Wert, Beschriftung)`` in der Reihenfolge von ``tax_groups.MIXED_TAX_RATES`` – bei jedem Aufruf frisch aus dem Modul gelesen."""
    keys = (rate_key(rate) for rate in tax_groups.MIXED_TAX_RATES)
    return [(key, rate_label(key)) for key in keys if key is not None]


class Choice(NamedTuple):
    """Eine wählbare Option der Maske. ``value``: ihr Wert – der Satz selbst (``'19'``, ``'5.5'``), wenn sie nur einen Satz trägt, sonst ``'e<ID>'``; ``label``: die
    Beschriftung; ``rate``: der Satz als Prozentzahl (``rate_key``). ``entry_id``: die ID, die das Speichern als ``taxRateId`` mitschickt – ``None`` bei einer Option, die nur
    einen Satz trägt (die Vorbelegung und jeder Satz ohne Eintrag: der Payload von früher); ``0`` bei „gespeichert …“ (der Server lehnt sie immer ab). ``exempt`` und
    ``reason``: steuerfrei und der Grund der Befreiung, der auf der Rechnung steht. ``entry``: der Eintrag der Stammdaten, zu dem die Option gehört (damit sich eine gespeicherte
    Position ihm zuordnen lässt), sonst ``None``."""
    value: str
    label: str
    rate: str
    entry_id: Optional[int] = None
    exempt: bool = False
    reason: str = ''
    entry: Optional[object] = None


def legacy_choices():
    """Die Sätze von ``tax_groups.MIXED_TAX_RATES`` als Optionen ohne Eintrag der Stammdaten – die Liste vor 2.8.0 und die Vorgabe der Funktionen dieses Abschnitts."""
    return [Choice(value, label, value) for value, label in mixed_rate_choices()]


def entry_choices(db):
    """Die wählbaren Steuersätze aus den Stammdaten (``db.fetch_tax_rates(active_only=True)``: aktiv und gültig, in der Reihenfolge der Stammdaten) als Optionen. Ein Eintrag, den
    der bisherige Payload trägt (``legacy_expressible``), bekommt als Wert seinen Satz und schickt keine ID; jeder andere den Wert ``e<ID>`` und seine ID."""
    choices = []
    for entry in db.fetch_tax_rates(active_only=True):
        rate = rate_key(bp_pct(entry.bp))
        if legacy_expressible(entry):
            choices.append(Choice(rate, entry.label, rate, entry=entry))
        else:
            exempt = entry.kind == EXEMPT
            choices.append(Choice(f'e{entry.id}', entry.label, rate, entry.id, exempt, (entry.reason or '') if exempt else '', entry))
    return choices


def choice_json(choice):
    """Die Option für das Skript (``MIX_RATES``): Wert und Beschriftung – bei einer Option mit Eintrag dazu Satz und ID, bei einer steuerfreien auch der Grund."""
    data = {'value': choice.value, 'label': choice.label}
    if choice.entry_id is not None:
        data.update(rate=choice.rate, id=choice.entry_id)
        if choice.exempt:
            data.update(exempt=True, reason=choice.reason)
    return data


def item_snapshot(item):
    """Die Momentaufnahme einer gespeicherten Position – ``(Art, Satz in Hundertstel Prozent, Grund, Erlöskonto)`` – oder ``None``, wenn sie keine trägt (``TaxRateBp`` leer: der
    Bestand und jede Position, die ihr Satz allein beschreibt; ein Wert, der keine ganze Zahl ist, gilt als keine)."""
    if len(item) <= ITEM_REVENUE_COA or type(item[ITEM_TAX_RATE_BP]) is not int:
        return None
    return item[ITEM_TAX_CATEGORY], item[ITEM_TAX_RATE_BP], item[ITEM_TAX_EXEMPT_REASON], item[ITEM_REVENUE_COA]


def _entry_snapshot(entry):
    """Die Momentaufnahme, die ein Speichern mit diesem Eintrag an eine Position schriebe, in der Form von ``item_snapshot``."""
    stored = entry_snapshot(entry)
    return stored['tax_category'], stored['tax_rate_bp'], stored['tax_exempt_reason'], stored['revenue_coa_id']


def _choice_state(choice):
    """Was eine Position speichert, die diese Option gewählt hat: ``(Satz, Momentaufnahme)`` – ohne Momentaufnahme bei einer Option, die nur einen Satz trägt."""
    return (choice.rate, None) if choice.entry_id is None else (choice.rate, _entry_snapshot(choice.entry))


def _attributes(choice):
    """Die Angaben einer Option mit Eintrag als Attribute (maskiert), das Skript liest sie von der gewählten Option; leer bei einer Option, die nur einen Satz trägt."""
    if choice.entry_id is None:
        return ''
    html = f' data-rate="{_html.escape(choice.rate, quote=True)}" data-entry="{int(choice.entry_id)}"'
    if choice.exempt:
        html += f' data-exempt="1" data-reason="{_html.escape(choice.reason, quote=True)}"'
    return html


def entry_options(choices, rate, snapshot=None):
    """Die Optionen eines Auswahlfelds als ``(Wert, Beschriftung, Attribute)`` und der gewählte Wert – zu dem, was eine Position speichert: ``rate`` (ihr Satz, in jeder Schreibweise)
    und ``snapshot`` (ihre Momentaufnahme, ``item_snapshot``; ``None`` ohne). **Nichts wird still umgedeutet:**

    * Mit Momentaufnahme gilt der Eintrag der Liste, der dieselbe Momentaufnahme schriebe (Satz, Art, Grund, Erlöskonto – ``db.tax_rates.entry_snapshot``); gibt es ihn nicht mehr,
      kommt die Option „gespeichert …“ dazu und ist gewählt (Wert ``e0``): Sie schickt ``taxRateId: 0``, der Server lehnt ab, bis der Nutzer neu wählt.
    * Ohne Momentaufnahme gilt die Option, die nur den Satz trägt; kennt die Liste ihn nicht (ein Bestandsbeleg mit 16 %), kommt er als eigene, gewählte Option dazu (der Payload
      von früher, ohne ID); ein Satz, der sich nicht lesen lässt, steht als leere Auswahl da."""
    options = [(choice.value, choice.label, _attributes(choice)) for choice in choices]
    if snapshot is not None:
        for choice in choices:
            if choice.entry is not None and _entry_snapshot(choice.entry) == snapshot:
                return options, choice.value
        category, bp, reason, _coa = snapshot
        exempt = category == 'E'
        gone = Choice(GONE_VALUE, f'{group_label(bp_pct(bp), exempt)} – {GONE_TEXT}', rate_key(bp_pct(bp)) or '', 0, exempt, (reason or '') if exempt else '')
        return options + [(gone.value, gone.label, _attributes(gone))], gone.value
    key = rate_key(rate)
    if key is None:
        return [('', MIXED_RATE_UNSET, '')] + options, ''
    for choice in choices:
        if choice.entry_id is None and choice.rate == key:
            return options, choice.value
    return options + [(key, rate_label(key), '')], key


def default_choice(choices, head):
    """Die Option, die eine neue Zeile (und ein neuer Beleg im Kopf) bekommt: die, die nur den Satz des Belegs (``head``, in jeder Schreibweise) trägt, wenn die Liste sie kennt, sonst die
    erste der Liste (``None`` bei einer leeren Liste)."""
    key = rate_key(head)
    for choice in choices:
        if choice.entry_id is None and choice.rate == key:
            return choice
    return choices[0] if choices else None


def mixed_default_key(head, choices=None):
    """Der Wert, den eine neue Zeile bekommt: die Option für den Satz des Belegs (``head``, in jeder Schreibweise), wenn die Liste ihn kennt, sonst die erste der Liste
    (``choices``: Vorgabe die Sätze von ``tax_groups.MIXED_TAX_RATES``)."""
    found = default_choice(legacy_choices() if choices is None else choices, head)
    return found.value if found is not None else ''


def mixed_select_html(selected, disabled='', choices=None, snapshot=None, opening='<select class="item-tax"'):
    """Das Auswahlfeld „USt“ einer Zeile: die wählbaren Steuersätze (``choices``: die Einträge der Stammdaten, ``entry_choices(db)``; Vorgabe die Sätze von
    ``tax_groups.MIXED_TAX_RATES`` ohne Eintrag), davon gewählt, was die Position speichert – ``selected`` (ihr Satz, in jeder Schreibweise) und ``snapshot`` (ihre Momentaufnahme,
    ``item_snapshot``; ``None`` ohne). Steht der Satz nicht in der Liste, kommt er als eigene, gewählte Option dazu – nichts wird still umgedeutet, der Server lehnt beim Speichern
    ab und sagt es; ein Satz, der sich nicht lesen lässt, steht als leere Auswahl da; zu einer Momentaufnahme, deren Eintrag es nicht mehr gibt, kommt „gespeichert …“
    (``entry_options``). ``disabled``: ``' disabled'`` (gesperrte Rechnung) oder leer; ``opening``: der Anfang des ``<select>``-Tags (das Feld im Kopf nimmt einen anderen)."""
    rendered, key = entry_options(legacy_choices() if choices is None else choices, selected, snapshot)
    chosen = next((i for i, (value, _label, _extra) in enumerate(rendered) if value == key), -1)       # nur die erste mit diesem Wert ist gewählt
    options = ''.join(f'<option value="{_html.escape(value, quote=True)}"{attributes}{" selected" if i == chosen else ""}>'
                      f'{_html.escape(label)}</option>' for i, (value, label, attributes) in enumerate(rendered))
    return f'{opening}{disabled}>{options}</select>'


MIXED_HEAD_CELL = '<th class="mixedTaxCol">USt</th>'
HEAD_SELECT_OPENING = '<select id="tax_choice" class="taxChoice" onchange="mixHeadChange()"'


def mixed_cell_html(selected, disabled='', choices=None, snapshot=None):
    """Die Zelle „USt“ einer Zeile der Positionstabelle (sichtbar nur mit dem Häkchen: ``buch.css``, Klasse ``mixedTaxOn`` an der Tabelle)."""
    return f'<td class="mixedTaxCol">{mixed_select_html(selected, disabled, choices, snapshot)}</td>'


class HeadSelect(NamedTuple):
    """Das Auswahlfeld „Mehrwertsteuer“ im Kopf: ``html`` das Feld, ``rate`` der Satz (Prozent) für das versteckte Feld ``#tax_rate``, ``row_state`` was eine neue Zeile speichert
    (``(Satz, Momentaufnahme)``, wie ``mixed_select_html`` es nimmt)."""
    html: str
    rate: float
    row_state: tuple


def head_select(choices, items, tax_pct, loaded, show_tax, disabled=''):
    """Das Auswahlfeld „Mehrwertsteuer“ im Kopf (``#tax_choice``) von Rechnung und Angebot: Es ersetzt das Zahlenfeld, das versteckte Feld ``#tax_rate`` behält seine ID und trägt den Satz.

    Ein geladener Beleg (``loaded``: Bearbeiten, Kopie, Angebot → Rechnung, Ersatzrechnung) mit Steuerausweis zeigt, was er speichert: Satz ``tax_pct`` (der Satz im Kopf) und die
    Momentaufnahme der ersten Position mit diesem Satz – bei einem Beleg mit einem Satz also die der ersten Position, bei einem mit Satz je Position (der Kopf ist dort der höchste Satz)
    die der ersten Position zu diesem. Ein neuer Beleg und einer ohne Steuerausweis (dort bleibt der Satz im Feld die Vorbelegung) starten mit der Vorgabe (``default_choice``).
    ``items``: die gespeicherten Positionen."""
    if loaded and show_tax:
        probe = next((item for item in items if rate_key(item[ITEM_TAX_RATE]) == rate_key(tax_pct)), None)
        rate, snapshot = (probe[ITEM_TAX_RATE], item_snapshot(probe)) if probe is not None else (tax_pct, None)
        head_rate = tax_pct
    else:
        start = default_choice(choices, tax_pct)
        rate, snapshot = _choice_state(start) if start is not None else (tax_pct, None)
        head_rate = float(start.rate) if start is not None else tax_pct
    _rendered, value = entry_options(choices, rate, snapshot)
    chosen = next((choice for choice in choices if choice.value == value), None)       # „gespeichert …“, ein fremder Satz und die leere Auswahl sind keine Vorgabe für eine neue Zeile
    row = _choice_state(chosen if chosen is not None else default_choice(choices, tax_pct)) if choices else (tax_pct, None)
    return HeadSelect(mixed_select_html(rate, disabled, choices, snapshot, HEAD_SELECT_OPENING), head_rate, row)


def mixed_checkbox_html(checked, disabled):
    """Das Häkchen „Steuersatz je Position“ unter „Umsatzsteuer ausweisen“ – in derselben Zelle, die Zeile ist ``no-pdf``. ``disabled``: ``' disabled'`` oder leer."""
    return ('<br><label><input type="checkbox" id="mixed_tax"' + (' checked' if checked else '') + disabled
            + ' onchange="mixToggle()"> Steuersatz je Position</label>')


# Der Link zu den Stammdaten (hinter dem Häkchen „Steuersatz je Position“, in derselben Zeile) und der Hinweis zum Grund einer Steuerbefreiung (das Skript füllt ihn: ``mixNote``)
# – beide in der Zelle der Häkchen, die Zeile ist ``no-pdf``. Der Link öffnet in einem neuen Tab: Ein Beleg, der noch nicht gespeichert ist, ginge bei einem Wechsel der Seite
# verloren. Feste Zeichenfolgen, kein Wert aus der Datenbank.
TAX_LINK_HTML = '<small class="hint"><a href="/masterdata/taxrates" target="_blank" rel="noopener">Steuersätze verwalten</a></small>'
TAX_NOTE_HTML = '<p class="hint" id="tax_note" hidden></p>'


# Die Bausteine im Browser: Auswahl je Zeile, Häkchen, Summenzeilen je Satz. Nur var und function, Werte nur über .value, .textContent und createElement (nie als
# HTML-Text). Der Block steht VOR dem Skript der Seite und ruft dessen Funktionen (calculateTotals, fmtDe, parseDe) erst beim Aufruf. ``__MIX_RATES__`` ist die
# Liste der wählbaren Steuersätze als JSON (``script_json``).
#
# Zwei Teile in EINEM Block (``mixed_tax_script``): ``MIXED_ENTRY_JS`` liest die Angaben der Einträge der Stammdaten von den Optionen (Satz, ID, steuerfrei, Grund) und
# trägt, was nur mit ihnen zu tun hat – das Feld „Mehrwertsteuer“ im Kopf, den Hinweis zum Befreiungsgrund, die ID für den Payload; ``MIXED_TAX_JS`` ist der Steuersatz je
# Position (2.7.0) und nimmt seine Sätze von den Optionen, nicht mehr von ihrem Wert.
MIXED_ENTRY_JS = r'''
        // Steuersätze aus den Stammdaten (seit 2.8.0). Eine Option der Auswahl trägt, was ihr Eintrag mitbringt, in Attributen: data-rate (der Satz als Prozentzahl), data-entry
        // (die ID des Eintrags; 0: der gespeicherte Stand, dessen Eintrag es nicht mehr gibt – der Server lehnt ihn immer ab), data-exempt und data-reason (steuerfrei und sein
        // Grund). Eine Option ohne diese Attribute trägt nur einen Satz: ihr Wert (so schickt die Maske den Payload von früher, ohne ID).

        // Der Satz einer Option als Text (Prozentzahl).
        function mixOptionRate(option) {
            var rate = option.getAttribute('data-rate');
            return rate === null ? option.value : rate;
        }

        // Die ID des Eintrags einer Option (0: gespeicherter Stand) oder null: Die Option trägt nur einen Satz.
        function mixOptionEntry(option) {
            var entry = option.getAttribute('data-entry');
            return entry === null ? null : parseInt(entry, 10);
        }

        function mixOptionExempt(option) {
            return option.hasAttribute('data-exempt');
        }

        // Die gewählte Option eines Auswahlfelds oder null.
        function mixChosen(select) {
            return select ? (select.options[select.selectedIndex] || null) : null;
        }

        // Gibt es unter den Optionen der Liste eine mit diesem Wert?
        function mixListed(value) {
            for (var i = 0; i < MIX_RATES.length; i++) {
                if (MIX_RATES[i].value === value) return true;
            }
            return false;
        }

        // Das Feld „Mehrwertsteuer“ im Kopf (ohne das Häkchen „Steuersatz je Position“): die gewählte Option, ihre ID für den Payload und der Satz im versteckten Feld.
        function mixHeadChosen() {
            return mixChosen(document.getElementById('tax_choice'));
        }

        // Die ID des Eintrags, den der Kopf gewählt hat, oder null (ein Satz ohne Eintrag): Nur sie geht als taxRateId mit.
        function mixHeadEntry() {
            var option = mixHeadChosen();
            return option ? mixOptionEntry(option) : null;
        }

        // Die ID des Eintrags einer Zeile oder null.
        function mixRowEntry(row) {
            var option = mixChosen(row.querySelector('.item-tax'));
            return option ? mixOptionEntry(option) : null;
        }

        // Wählt der Nutzer im Kopf einen Eintrag, trägt das versteckte Feld seinen Satz, und die Summen ziehen nach.
        function mixHeadChange() {
            var option = mixHeadChosen();
            var field = document.getElementById('tax_rate');
            if (option && field) field.value = mixOptionRate(option);
            calculateTotals();
        }

        // Der Hinweis unter den Auswahlfeldern: der Grund jedes gewählten steuerfreien Eintrags, so wie er auf der Rechnung steht (ohne Umsatzsteuerausweis nichts).
        function mixNote() {
            var note = document.getElementById('tax_note');
            if (!note) return;
            var tax = document.getElementById('show_tax');
            var chosen = [];
            if (tax && tax.checked) {
                if (mixOn()) {
                    var rows = document.querySelectorAll('.invoice-item-row');
                    for (var i = 0; i < rows.length; i++) chosen.push(mixChosen(rows[i].querySelector('.item-tax')));
                } else {
                    chosen.push(mixHeadChosen());
                }
            }
            var reasons = [];
            for (var j = 0; j < chosen.length; j++) {
                var reason = chosen[j] && mixOptionExempt(chosen[j]) ? chosen[j].getAttribute('data-reason') : '';
                if (reason && reasons.indexOf(reason) < 0) reasons.push(reason);
            }
            note.textContent = reasons.length ? 'Grund der Steuerbefreiung (steht so auf der Rechnung): ' + reasons.join(' | ') : '';
            note.hidden = !reasons.length;
        }

        // Ohne das Häkchen mit einem Eintrag im Kopf (er schickt eine ID: ein Satz mit Nachkommastellen, steuerfrei, mit eigenem Erlöskonto): Die Steuer ist die einer Gruppe mit
        // Momentaufnahme – das Netto mal der Satz, EINMAL auf den Cent gerundet (EN 16931). Ein Satz ohne Eintrag bleibt bei der Rechnung von calculateTotals, wie sie war.
        function mixHeadTotals() {
            var option = mixHeadChosen();
            var tax = document.getElementById('show_tax');
            if (!option || !tax || !tax.checked || mixOptionEntry(option) === null) return;
            var net = Math.round((parseDe(document.getElementById('sum_net').textContent) || 0) * 100);
            var cents = mixTax(net, parseFloat(mixOptionRate(option)), true);
            document.getElementById('tax_amount').textContent = fmtDe(cents / 100) + ' €';
            var gross = document.getElementById('sum_gross');
            gross.textContent = '';
            var strong = document.createElement('strong');
            strong.textContent = fmtDe((net + cents) / 100) + ' €';
            gross.appendChild(strong);
        }
'''

MIXED_TAX_JS = r'''
        // Steuersatz je Position: Bausteine für die Masken von Rechnung und Angebot. Die wählbaren Steuersätze kommen vom Server (die Einträge der Stammdaten), hier steht
        // kein Satz im Quelltext: [{value, label}, …] – bei einem Eintrag, den der Satz allein nicht trägt, dazu rate, id und – steuerfrei – exempt und reason
        var MIX_RATES = __MIX_RATES__;

        // Ist „Steuersatz je Position“ eingeschaltet? Das Häkchen gilt nur zusammen mit „Umsatzsteuer ausweisen“.
        function mixOn() {
            var box = document.getElementById('mixed_tax');
            var tax = document.getElementById('show_tax');
            return !!(box && tax && box.checked && tax.checked);
        }

        // Ein Satz als Auswahlwert (die Prozentzahl als Text): dieselbe Regel wie tax_groups.rate_pct (ein Wert zwischen 0 und 1 gilt als Bruch);
        // ohne lesbare Zahl ''.
        function mixKey(value) {
            var number = parseFloat(value);
            if (isNaN(number)) return '';
            if (number > 0 && number < 1) number = number * 100;
            return String(Math.round(number * 100) / 100);
        }

        // Kennt die Liste diesen Satz als Option, die nur einen Satz trägt (ohne ID: die Vorbelegung)? Nur so eine lässt sich über den Satz allein wählen – einen Eintrag mit
        // eigenem Konto oder Grund wählt der Nutzer selbst.
        function mixKnown(key) {
            for (var i = 0; i < MIX_RATES.length; i++) {
                if (MIX_RATES[i].id === undefined && MIX_RATES[i].value === key) return true;
            }
            return false;
        }

        // Der Wert, den eine neue Zeile bekommt: die Option, die das Feld „Mehrwertsteuer“ im Kopf gewählt hat (auch ein Eintrag mit ID), solange das versteckte Feld noch ihren
        // Satz trägt; sonst die Option für den Satz im Feld, wenn die Liste ihn kennt, sonst die erste der Liste.
        function mixDefaultRate() {
            var field = document.getElementById('tax_rate');
            var key = field ? mixKey(field.value) : '';
            var head = mixHeadChosen();
            if (head && mixListed(head.value) && mixKey(mixOptionRate(head)) === key) return head.value;
            if (mixKnown(key)) return key;
            return MIX_RATES.length ? MIX_RATES[0].value : '';
        }

        // Der Wert einer Artikelzeile: Trägt GENAU EINE Option der Liste den Satz des Artikels (vom Server als Satz geliefert) – mit oder ohne ID eines Eintrags der Stammdaten –, ist es
        // diese. Bei keiner oder mehreren die Option, die den Satz allein trägt (die Vorbelegung), sonst wie eine neue Zeile.
        function mixArticleRate(article) {
            var key = article.mixRate;
            var wanted = key ? mixKey(key) : '';
            var found = '';
            var count = 0;
            for (var i = 0; wanted !== '' && i < MIX_RATES.length; i++) {
                var rate = MIX_RATES[i].rate === undefined ? MIX_RATES[i].value : MIX_RATES[i].rate;
                if (mixKey(rate) === wanted) {
                    found = MIX_RATES[i].value;
                    count++;
                }
            }
            if (count === 1) return found;
            return (key && mixKnown(key)) ? key : mixDefaultRate();
        }

        // Die Zelle „USt“ einer neuen Zeile: dieselben Optionen wie die Zeilen, die der Server schreibt, mit denselben Attributen; ``key`` ist der Wert der gewählten.
        function mixAddCell(row, key) {
            var select = document.createElement('select');
            select.className = 'item-tax';
            for (var i = 0; i < MIX_RATES.length; i++) {
                var option = document.createElement('option');
                option.value = MIX_RATES[i].value;
                option.textContent = MIX_RATES[i].label;
                if (MIX_RATES[i].id !== undefined) {
                    option.setAttribute('data-rate', MIX_RATES[i].rate);
                    option.setAttribute('data-entry', String(MIX_RATES[i].id));
                }
                if (MIX_RATES[i].exempt) {
                    option.setAttribute('data-exempt', '1');
                    option.setAttribute('data-reason', MIX_RATES[i].reason);
                }
                select.appendChild(option);
            }
            select.value = key;
            var cell = document.createElement('td');
            cell.className = 'mixedTaxCol';
            cell.appendChild(select);
            row.insertBefore(cell, row.querySelector('.item-price').parentNode);
        }

        // Wählt der Nutzer in einer Zeile einen Satz, hält die Zeile ihn fest (auch wenn das Häkchen später neu gesetzt wird), und die Summen ziehen nach.
        function mixAttach(row) {
            var select = row.querySelector('.item-tax');
            if (!select) return;
            select.addEventListener('change', function () {
                row.setAttribute('data-mix-pinned', '1');
                calculateTotals();
            });
        }

        // Das Häkchen wurde gesetzt oder entfernt. Beim Einschalten bekommt jede Zeile, deren Satz der Nutzer nicht selbst gewählt hat, den Vorgabesatz.
        function mixToggle() {
            var box = document.getElementById('mixed_tax');
            if (box && box.checked) {
                var key = mixDefaultRate();
                var rows = document.querySelectorAll('.invoice-item-row');
                for (var i = 0; i < rows.length; i++) {
                    var select = rows[i].querySelector('.item-tax');
                    if (select && key && !rows[i].hasAttribute('data-mix-pinned')) select.value = key;
                }
            }
            calculateTotals();
        }

        // Hält die Maske in Ordnung: Das Häkchen gibt es nur mit „Umsatzsteuer ausweisen“ (ohne dieses ist es aus und gesperrt, in einer gesperrten Rechnung
        // bleibt es gesperrt), die Spalte „USt“ steht nur mit dem Häkchen da (Klasse an der Tabelle), und die Summenzeilen der Tabelle werden um die Spalte breiter.
        function mixSync() {
            var box = document.getElementById('mixed_tax');
            var tax = document.getElementById('show_tax');
            var table = document.querySelector('.invoice-items');
            if (!box || !tax || !table) return;
            if (!tax.checked) box.checked = false;
            box.disabled = !!(tax.disabled || !tax.checked);
            var on = mixOn();
            if (on) table.classList.add('mixedTaxOn'); else table.classList.remove('mixedTaxOn');
            var cells = table.querySelectorAll('tfoot td[colspan]');
            for (var i = 0; i < cells.length; i++) {
                if (!cells[i].hasAttribute('data-span')) cells[i].setAttribute('data-span', cells[i].getAttribute('colspan'));
                cells[i].setAttribute('colspan', String(Number(cells[i].getAttribute('data-span')) + (on ? 1 : 0)));
            }
        }

        // ``value`` durch ``divisor`` (ganze Zahlen, nicht negativ), kaufmännisch gerundet (ab der Hälfte auf) – ohne Fließkomma-Division.
        function mixRoundDiv(value, divisor) {
            var rest = value % divisor;
            var quotient = (value - rest) / divisor;
            return rest * 2 >= divisor ? quotient + 1 : quotient;
        }

        // Die Steuer einer Satzgruppe in Cent: Nettosumme (Cent) mal Satz, kaufmännisch gerundet (halb weg von null) – so, wie der Server es tut, und nur mit ganzen Zahlen, nie mit
        // Zwischenwerten als Kommazahl. Zweistufig (money.tax_from_net rundet auf Minor Units, round_minor danach auf den Cent) für eine Gruppe ohne Eintrag der Stammdaten, wie
        // immer; EINMAL gerundet (``exact``, EN 16931: das Netto mal der Satz, dann auf den Cent) für eine Gruppe, deren Positionen die Momentaufnahme eines Eintrags tragen.
        function mixTax(net, percent, exact) {
            if (isNaN(percent)) return 0;
            var basis = Math.round(percent * 100);
            var cents = exact ? mixRoundDiv(Math.abs(net) * basis, 10000) : mixRoundDiv(mixRoundDiv(Math.abs(net) * basis, 100), 100);
            return net < 0 ? -cents : cents;
        }

        // Die Beschriftung des Satzes der gewählten Option in der Summenzeile: „steuerfrei“ für einen steuerfreien Eintrag, sonst der Satz mit Komma (ohne lesbaren Satz der Text
        // der Option, bei der leeren Auswahl „– wählen –“).
        function mixLabel(select) {
            var option = mixChosen(select);
            if (!option) return '';
            if (mixOptionExempt(option)) return 'steuerfrei';
            var rate = mixOptionRate(option);
            return isNaN(parseFloat(rate)) ? option.textContent : rate.replace('.', ',') + ' %';
        }

        // Die Satzgruppen, nach Satz absteigend; Beträge in Cent (ganze Zahlen). Das Netto einer Position ist die Zahl, die ihre Zeile zeigt. Eine Gruppe rechnet einstufig
        // (``exact``), wenn jede ihrer Positionen die Momentaufnahme eines Eintrags trägt (die Option hat eine ID) – wie der Server.
        function mixGroups() {
            var rows = document.querySelectorAll('.invoice-item-row');
            var byKey = {};
            var groups = [];
            for (var i = 0; i < rows.length; i++) {
                var select = rows[i].querySelector('.item-tax');
                if (!select) continue;
                var option = mixChosen(select);
                var key = option ? mixOptionRate(option) : select.value;
                if (!Object.prototype.hasOwnProperty.call(byKey, key)) {
                    byKey[key] = {key: key, percent: parseFloat(key), label: mixLabel(select), net: 0, tax: 0, exact: true};
                    groups.push(byKey[key]);
                }
                if (!option || mixOptionEntry(option) === null) byKey[key].exact = false;
                byKey[key].net += Math.round((parseDe(rows[i].querySelector('.item-total').textContent) || 0) * 100);
            }
            groups.sort(function (a, b) {
                return (isNaN(b.percent) ? -1e9 : b.percent) - (isNaN(a.percent) ? -1e9 : a.percent);
            });
            for (var j = 0; j < groups.length; j++) groups[j].tax = mixTax(groups[j].net, groups[j].percent, groups[j].exact);
            return groups;
        }

        function mixRateRow(group) {
            var row = document.createElement('tr');
            row.className = 'totals-row mixRateRow';
            var name = document.createElement('td');
            name.setAttribute('colspan', '6');
            name.setAttribute('data-span', '5');
            name.textContent = (group.label === 'steuerfrei' ? group.label.charAt(0).toUpperCase() + group.label.slice(1) : 'Mehrwertsteuer ' + group.label) + ' auf ' + fmtDe(group.net / 100) + ' € netto:';
            var amount = document.createElement('td');
            var strong = document.createElement('strong');
            strong.textContent = fmtDe(group.tax / 100) + ' €';
            amount.appendChild(strong);
            var spare = document.createElement('td');
            spare.className = 'no-pdf';
            row.appendChild(name);
            row.appendChild(amount);
            row.appendChild(spare);
            return row;
        }

        // Der Summenblock, den calculateTotals am Ende ruft: Mit dem Häkchen steht an der Stelle der einen Steuerzeile je Satz eine Zeile; Netto, Steuer und
        // Gesamtbetrag kommen aus den Gruppen (die Steuer je Gruppe auf den Cent gerundet, nie je Position). Das ist nur die Anzeige: Maßgeblich rechnet der Server.
        // Ohne das Häkchen bleibt die Steuerzeile, wie calculateTotals sie rechnet – nur ein Eintrag im Kopf (``mixHeadTotals``) rechnet sie einstufig; der Hinweis zum Grund
        // einer Steuerbefreiung (``mixNote``) folgt in beiden Fällen dem Stand.
        function mixTotals() {
            mixSync();
            var table = document.querySelector('.invoice-items');
            if (!table) return;
            var old = table.querySelectorAll('.mixRateRow');
            for (var i = 0; i < old.length; i++) old[i].parentNode.removeChild(old[i]);
            var anchor = document.getElementById('tax_row');
            mixNote();
            var selects = table.querySelectorAll('.item-tax');
            for (var k = 0; k < selects.length; k++) {         // eine lange Bezeichnung zeigt das Feld nur zum Teil: ganz im Tooltip
                var chosen = mixChosen(selects[k]);
                selects[k].title = chosen ? chosen.textContent : '';
            }
            if (!mixOn() || !anchor) {
                mixHeadTotals();
                return;
            }
            anchor.style.display = 'none';
            var groups = mixGroups();
            var net = 0;
            var tax = 0;
            for (var j = 0; j < groups.length; j++) {
                net += groups[j].net;
                tax += groups[j].tax;
                anchor.parentNode.insertBefore(mixRateRow(groups[j]), anchor);
            }
            document.getElementById('sum_net').textContent = fmtDe(net / 100) + ' €';
            document.getElementById('tax_base').textContent = fmtDe(net / 100);
            document.getElementById('tax_amount').textContent = fmtDe(tax / 100) + ' €';
            var gross = document.getElementById('sum_gross');
            gross.textContent = '';
            var strong = document.createElement('strong');
            strong.textContent = fmtDe((net + tax) / 100) + ' €';
            gross.appendChild(strong);
        }

        // Der Satz einer Zeile als Zahl (Prozent), so, wie das Formular ihn schickt – ohne Wahl NaN (im JSON null: der Server fragt dann nach).
        function mixRowRate(row) {
            var select = row.querySelector('.item-tax');
            var option = mixChosen(select);
            return parseFloat(option ? mixOptionRate(option) : select.value);
        }

        // Beim Laden: Eine Rechnung mit Steuersatz je Position behält die Sätze ihrer Zeilen, auch wenn das Häkchen aus- und wieder eingeschaltet wird.
        function mixInit() {
            if (!mixOn()) return;
            var rows = document.querySelectorAll('.invoice-item-row');
            for (var i = 0; i < rows.length; i++) rows[i].setAttribute('data-mix-pinned', '1');
        }
        mixInit();
'''


# Abweichung vom Artikel (Rechnung und Angebot): Der Block steht im selben Skript wie die Bausteine oben (``mixed_tax_script``) und nutzt sie (``mixOn``, ``mixKey``, ``mixChosen``, ``mixRowRate``).
# Nur die Anzeige: Er liest die Zeilen und schreibt nur Klassen und Tooltips – Payload, Summen und Sperre bleiben, wie sie waren. Nur var und function (Präfix art), Werte nur über .title,
# .textContent und createElement; die Texte kommen vom Server (``ARTICLE_TEXTS`` über ``script_json``, ``__ART_TEXTS__`` wird in der Konstante ersetzt, nie über eine fertige Seite), die
# Artikel stehen in ``articlesData`` im Skript der Seite (die aktiven Artikel; der Block liest sie erst beim Aufruf). Die Seiten rufen ``artMark`` nach dem Laden und nach dem Hinzufügen und
# Entfernen einer Zeile; die Ereignisse der Tabelle (``input``, ``change``) lösen es für alles andere aus: Eingaben in einer Zeile, „Steuersatz je Position“, „Umsatzsteuer ausweisen“ und
# das Feld „Mehrwertsteuer“ im Kopf liegen alle in ihr. Das „+“ (``POST /articles/quick-add``) gibt es nur an einer Zeile OHNE Artikel und legt aus ihr einen NEUEN Artikel an – ein vorhandener
# wird nie geändert, und an einer Zeile, die von ihrem Artikel abweicht, übernähme es den Tippfehler als neuen Artikel und schaltete die Warnung ab.
ARTICLE_TEXTS = {
    'article': 'Artikel: ',                                     # Anfang des Tooltips eines Felds, das vom Artikel abweicht
    'document': ' – Beleg: ',                                   # Satz des Belegs hinter dem Satz des Artikels
    'add': 'Als neuen Artikel speichern',                       # Tooltip des „+“
    'question': 'Als neuen Artikel speichern? ',                # Anfang der Rückfrage
    'saved': 'Artikel gespeichert.',
    'failed': 'Der Artikel konnte nicht gespeichert werden.',   # fester Satz, wenn der Server nichts Lesbares antwortet
}

ARTICLE_MARK_JS = r'''
        // Abweichung vom Artikel: eine Position, die vom gespeicherten Artikel abweicht, fällt auf (nur die Anzeige, siehe pages_invoice.py).
        var ART_TEXTS = __ART_TEXTS__;

        // Die Artikel nach normalisiertem Namen (artNames), einmal aufgebaut und nicht je Zeile und Tastendruck; null: noch nicht aufgebaut. Wer articlesData ändert, setzt es zurück (artSend).
        var artIndex = null;

        // Ein Text zum Vergleich der Namen: Leerraum zusammengefasst und außen entfernt, Kleinbuchstaben (dieselbe Regel wie article_key auf dem Server beim Anlegen).
        function artNorm(text) {
            return String(text === null || text === undefined ? '' : text).replace(/\s+/g, ' ').trim().toLowerCase();
        }

        // Eine Einheit zum Vergleich: außen ohne Leerraum, Kleinbuchstaben.
        function artLow(text) {
            return String(text === null || text === undefined ? '' : text).trim().toLowerCase();
        }

        // Beginnt der Text mit einem Buchstaben, einer Ziffer oder einem Zeichen, das zum Buchstaben gehört (Akzent)? Dann endet ein Name davor nicht an einer Wortgrenze.
        function artWordStart(text) {
            return /^[\p{L}\p{M}\p{N}]/u.test(text);
        }

        // Beginnt der Text (normalisiert) mit diesem Namen (normalisiert), und danach folgt das Ende oder ein Zeichen, das kein Buchstabe und keine Ziffer ist?
        function artStarts(text, name) {
            return name !== '' && text.indexOf(name) === 0 && !artWordStart(text.slice(name.length));
        }

        // Die Artikel nach normalisiertem Namen. Namen unter 3 Zeichen fehlen (sie nehmen an der Namenssuche nicht teil); tragen mehrere Artikel denselben normalisierten Namen, gilt der mit der
        // kleinsten ID (zwei verschiedene Namen derselben Länge können nie beide der Anfang desselben Textes sein: weitere gleich lange Treffer gibt es nicht). Aufgebaut wird beim ersten Gebrauch
        // und neu, wenn articlesData ein anderes Objekt ist (die Tests setzen es neu) oder artSend einen Artikel dazugelegt hat.
        function artNames() {
            if (artIndex !== null && artIndex.source === articlesData) return artIndex.names;
            var names = new Map();
            var keys = Object.keys(articlesData);
            for (var i = 0; i < keys.length; i++) {
                var article = articlesData[keys[i]];
                var name = artNorm(article.name);
                if (Array.from(name).length < 3) continue;
                var known = names.get(name);
                if (known === undefined || Number(article.id) < Number(known.id)) names.set(name, article);
            }
            artIndex = {source: articlesData, names: names};
            return names;
        }

        // Der Artikel einer Zeile oder null. 1. Trägt die Zeile data-article-id (frisch aus der Artikelsuche gewählt), gibt es den Artikel und beginnt die Beschreibung noch mit seinem Namen
        // (artStarts): dieser – wer die Beschreibung auf etwas anderes umschreibt, hat einen anderen Artikel gemeint. 2. Sonst der Artikel mit dem LÄNGSTEN Namen, mit dem die Beschreibung
        // beginnt und nach dem das Ende oder ein Zeichen folgt, das kein Buchstabe und keine Ziffer ist („Beratung“ trifft nicht „Beratungspaket“). Dafür wird nur gefragt, ob ein Anfang der
        // Beschreibung bis zu einer Wortgrenze ein Name ist – der Aufwand hängt an der Länge des Texts, nicht an der Zahl der Artikel. Rein: nichts wird geschrieben.
        function artFind(row) {
            var field = row.querySelector('.item-description');
            var text = artNorm(field ? field.value : '');
            var id = row.getAttribute('data-article-id');
            if (id !== null && Object.prototype.hasOwnProperty.call(articlesData, id) && artStarts(text, artNorm(articlesData[id].name))) return articlesData[id];
            var names = artNames();
            var ends = [];
            var boundary = /[^\p{L}\p{M}\p{N}]/gu;
            var hit;
            while ((hit = boundary.exec(text)) !== null) ends.push(hit.index);
            ends.push(text.length);
            for (var i = ends.length - 1; i >= 0; i--) {
                var article = names.get(text.slice(0, ends[i]));
                if (article !== undefined) return article;
            }
            return null;
        }

        // Eine Dezimalzahl – der Text eines Zahlenfelds oder eine Zahl – als ganze Zahl (BigInt) und Zahl der Nachkommastellen: {units, places}. Der Text wird Ziffer für Ziffer gelesen, nie als
        // Gleitkommazahl gerechnet (1.005 ist 1005 mit 3 Stellen, nicht 1.00499…); auch die Schreibweise mit Exponent (8.5e1). Leer oder keine Zahl: 0, wie die Summenrechnung der Maske es liest.
        function artDecimal(value) {
            var match = /^\s*([+-]?)(\d*)(?:\.(\d*))?(?:[eE]([+-]?\d+))?\s*$/.exec(String(value));
            var fraction = match && match[3] ? match[3] : '';
            if (!match || match[2] + fraction === '') return {units: BigInt(0), places: 0};
            var places = fraction.length - (match[4] ? parseInt(match[4], 10) : 0);
            var units = BigInt(match[2] + fraction);
            if (places < 0) {
                units = units * BigInt('1' + '0'.repeat(Math.min(-places, 400)));
                places = 0;
            } else if (places > 400) {
                units = BigInt(0);
                places = 0;
            }
            return {units: match[1] === '-' ? -units : units, places: places};
        }

        // Eine Dezimalzahl als ganze Zahl mit genau so vielen Nachkommastellen (places ist mindestens ihre eigene Zahl).
        function artScale(decimal, places) {
            return decimal.units * BigInt('1' + '0'.repeat(places - decimal.places));
        }

        // Weicht der Preis der Zeile vom Preis des Artikels ab? Genau dann, wenn der Unterschied mindestens einen halben Cent beträgt – in Dezimalzahlen gerechnet, ohne die Gleitkomma-Falle
        // (85.005 gegen 85.01: als Gleitkomma ist die Differenz 0.00499…, in Dezimalzahlen genau 0.005).
        function artPriceDiffers(rowValue, articleValue) {
            var row = artDecimal(rowValue);
            var article = artDecimal(articleValue);
            var places = Math.max(row.places, article.places, 3);
            var difference = artScale(row, places) - artScale(article, places);
            if (difference < BigInt(0)) difference = -difference;
            return difference * BigInt(2) >= BigInt('1' + '0'.repeat(places - 2));
        }

        // Ein Preis auf zwei Nachkommastellen, ab der Hälfte aufgerundet (wie money.round_to_cent auf dem Server: einmal und exakt), als Zahl; leer oder keine Zahl: 0. Das ist der Wert, den das
        // „+“ schickt – und den die Rückfrage zeigt: EINE Funktion für beides.
        function artPrice(value) {
            var decimal = artDecimal(value);
            var cents = decimal.units;
            if (decimal.places < 2) {
                cents = cents * BigInt('1' + '0'.repeat(2 - decimal.places));
            } else if (decimal.places > 2) {
                var unit = BigInt('1' + '0'.repeat(decimal.places - 2));
                var magnitude = cents < BigInt(0) ? -cents : cents;
                var quotient = magnitude / unit;
                if (magnitude % unit * BigInt(2) >= unit) quotient = quotient + BigInt(1);
                cents = cents < BigInt(0) ? -quotient : quotient;
            }
            return Number(cents) / 100;
        }

        // Ein Satz (Prozentzahl als Text, wie mixKey ihn schreibt) für einen Tooltip, mit Dezimalkomma.
        function artRateText(key) {
            return key.replace('.', ',') + ' %';
        }

        // Der Satz des Artikels als Auswahlwert: mixRate vom Server, sonst aus taxRate; '' – er lässt sich nicht lesen, dann gibt es nichts zu vergleichen.
        function artArticleRate(article) {
            return article.mixRate ? mixKey(article.mixRate) : mixKey(article.taxRate);
        }

        // Die Bezeichnung der ersten Option der Auswahl einer Zeile, die diesen Satz trägt – sonst der Satz selbst.
        function artOptionLabel(select, key) {
            var options = select ? select.options : [];
            for (var i = 0; i < options.length; i++) {
                if (mixKey(mixOptionRate(options[i])) === key) return options[i].textContent;
            }
            return artRateText(key);
        }

        // Die Abweichungen einer Zeile vom Artikel als Tooltip-Texte ('' = keine): Einheit, Netto-Einzelpreis, Satz der Auswahl der Zeile (tax, nur mit „Steuersatz je Position“) und
        // Satz des Belegs (head, ohne das Häkchen; markiert wird die Zelle der Positionsnummer). Menge und Beschreibung werden nie verglichen; ohne Steuerausweis gibt es keine Steuer-Markierung.
        function artDeviations(row, article) {
            var found = {unit: '', price: '', tax: '', head: ''};
            var unit = row.querySelector('.item-unit');
            if (unit && artLow(unit.value) !== artLow(article.unit)) found.unit = ART_TEXTS.article + article.unit;
            var price = row.querySelector('.item-price');
            if (price && artPriceDiffers(price.value, article.price)) found.price = ART_TEXTS.article + fmtDe(article.price);
            var tax = document.getElementById('show_tax');
            var wanted = artArticleRate(article);
            if (!tax || !tax.checked || wanted === '') return found;
            if (mixOn()) {
                var select = row.querySelector('.item-tax');
                var option = mixChosen(select);
                if (select && mixKey(option ? mixOptionRate(option) : select.value) !== wanted) found.tax = ART_TEXTS.article + artOptionLabel(select, wanted);
            } else {
                var own = mixKey(document.getElementById('tax_rate').value);
                if (own !== '' && own !== wanted) found.head = ART_TEXTS.article + artRateText(wanted) + ART_TEXTS.document + artRateText(own);
            }
            return found;
        }

        // Ein Feld markieren (text = der Tooltip bei einer Abweichung) oder die Markierung löschen (text = ''). Ohne Abweichung verschwinden Klasse und Tooltip; das Auswahlfeld des Satzes
        // bekommt den Tooltip zurück, den mixTotals ihm gibt (die Bezeichnung der gewählten Option), die übrigen Felder haben keinen. Ein Feld ohne Markierung bleibt unberührt.
        function artSet(field, text) {
            if (!field) return;
            if (text) {
                field.classList.add('itemDeviates');
                field.title = text;
            } else if (field.classList.contains('itemDeviates')) {
                field.classList.remove('itemDeviates');
                var chosen = field.classList.contains('item-tax') ? mixChosen(field) : null;
                field.title = chosen ? chosen.textContent : '';
            }
        }

        // Das „+“ einer Zeile (type=button) in ihrer letzten Zelle hinter dem ✕: angelegt wird es einmal, sichtbar ist es nur an einer Zeile mit itemCanSave (buch.css).
        function artButton(row) {
            var found = row.querySelector('.itemAddArticle');
            if (found) return found;
            var cell = row.querySelector('td.no-pdf');
            if (!cell) return null;
            var button = document.createElement('button');
            button.type = 'button';
            button.className = 'itemAddArticle';
            button.textContent = '+';
            button.title = ART_TEXTS.add;
            button.addEventListener('click', function () { artAdd(row); });
            cell.appendChild(button);
            return button;
        }

        // Eine Zeile markieren: Mit Beschreibung und ohne Artikel trägt sie itemNoArticle (Rand links) und itemCanSave (zeigt das „+“), mit Artikel bekommt jedes Feld, das abweicht, itemDeviates und
        // seinen Tooltip – aber kein „+“: Es übernähme den Tippfehler als neuen Artikel. Eine Zeile ohne Beschreibung bleibt ohne Markierung (das Speichern übergeht sie).
        function artMarkRow(row) {
            var description = row.querySelector('.item-description');
            var named = !!description && artNorm(description.value) !== '';
            var article = named ? artFind(row) : null;
            var found = article ? artDeviations(row, article) : {unit: '', price: '', tax: '', head: ''};
            artSet(row.querySelector('.item-unit'), found.unit);
            artSet(row.querySelector('.item-price'), found.price);
            artSet(row.querySelector('.item-tax'), found.tax);
            artSet(row.querySelector('td:first-child'), found.head);
            var without = named && !article;
            if (without) row.classList.add('itemNoArticle'); else row.classList.remove('itemNoArticle');
            if (without) row.classList.add('itemCanSave'); else row.classList.remove('itemCanSave');
            artButton(row);
        }

        // Alle Zeilen markieren – nicht in einer gesperrten Rechnung (versendet, storniert, Stornorechnung): dort läuft nichts, und es gibt kein „+“.
        function artMark() {
            var locked = document.getElementById('invoice_locked');
            if (locked && locked.value === 'true') return;
            var rows = document.querySelectorAll('.invoice-item-row');
            for (var i = 0; i < rows.length; i++) artMarkRow(rows[i]);
        }

        // Der Satz einer Zeile als Zahl (Prozent), wie das Speichern ihn schickt: ohne Steuerausweis 0, mit „Steuersatz je Position“ der der Auswahl, sonst der Satz des Belegs; ohne lesbaren Satz NaN.
        function artRowRate(row) {
            var tax = document.getElementById('show_tax');
            if (!tax || !tax.checked) return 0;
            return mixOn() ? mixRowRate(row) : parseFloat(document.getElementById('tax_rate').value);
        }

        // Das „+“ wurde geklickt: nach Rückfrage (die Zeile als Text) die Position als neuen Artikel anlegen – nur eine Zeile ohne Artikel (itemCanSave), auch wenn das Stylesheet das „+“ nicht
        // versteckt. Der Name ist die Beschreibung (Leerraum zusammengefasst, höchstens 200 Zeichen), der Preis der auf zwei Stellen gerundete (artPrice), den die Rückfrage zeigt.
        function artAdd(row) {
            if (!row.classList.contains('itemCanSave')) return;
            var name = Array.from(row.querySelector('.item-description').value.replace(/\s+/g, ' ').trim()).slice(0, 200).join('').trim();
            var unit = row.querySelector('.item-unit').value.trim();
            var price = artPrice(row.querySelector('.item-price').value);
            var rate = artRowRate(row);
            var text = ART_TEXTS.question + name + ' · ' + unit + ' · ' + fmtDe(price) + ' € · ' + (isNaN(rate) ? '?' : String(rate).replace('.', ',')) + ' %';
            appConfirm(text, function () { artSend(row, {name: name, unit: unit, price: price, taxRate: rate}); });
        }

        // Den Artikel anlegen lassen (ein JSON-POST, das Token hängt die Fußzeile an). Gelingt es, kennt die Zeile ihn (data-article-id) und artMark läuft neu; sonst steht der Satz des Servers
        // oder der feste Satz in der Meldung – nie der Text einer Ausnahme. Gibt das Versprechen zurück (die Tests warten darauf).
        function artSend(row, article) {
            return fetch('/articles/quick-add', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(article)})
                .then(function (response) { return response.json(); })
                .then(function (data) {
                    if (!data || !data.success || !data.article) {
                        appMsg((data && data.error) || ART_TEXTS.failed, 'error');
                        return;
                    }
                    articlesData[String(data.article.id)] = data.article;
                    artIndex = null;
                    row.setAttribute('data-article-id', String(data.article.id));
                    artMark();
                    appMsg(ART_TEXTS.saved, 'success');
                })
                .catch(function () { appMsg(ART_TEXTS.failed, 'error'); });
        }

        // Die Tabelle der Positionen hört auf input und change: Jede Eingabe in einer Zeile und jedes Umschalten im Fuß der Tabelle markiert neu (nach den Zuhörern der Felder selbst).
        function artInit() {
            var table = document.querySelector('.invoice-items');
            if (!table) return;
            table.addEventListener('input', artMark);
            table.addEventListener('change', artMark);
        }
        artInit();
'''


def mixed_tax_script(choices=None):
    """Der Skriptblock mit den Bausteinen für die Steuersätze der Stammdaten (``MIXED_ENTRY_JS``), für „Steuersatz je Position“ (``MIXED_TAX_JS``) und für die Abweichung vom Artikel
    (``ARTICLE_MARK_JS`` mit seinen Texten ``ARTICLE_TEXTS``) und der Liste der wählbaren Steuersätze (``choices``: ``entry_choices(db)``; Vorgabe die Sätze von
    ``tax_groups.MIXED_TAX_RATES`` ohne Eintrag), vor dem Skript der Maske."""
    rates = script_json([choice_json(choice) for choice in (legacy_choices() if choices is None else choices)])
    return ('<script>' + MIXED_ENTRY_JS + MIXED_TAX_JS.replace('__MIX_RATES__', rates)
            + ARTICLE_MARK_JS.replace('__ART_TEXTS__', script_json(ARTICLE_TEXTS)) + '    </script>\n')


# Stornieren (db/invoice_storno.py): Rückfrage mit Stornodatum; eine Stornorechnung lässt sich nicht kopieren
STORNO_DATE_ERROR = 'Bitte ein Stornodatum angeben, das weder vor dem Rechnungsdatum noch in der Zukunft liegt.'
STORNO_COPY_MESSAGE = 'Eine Stornorechnung lässt sich nicht kopieren.'


def cancel_question(number):
    return (f'Rechnung {number} stornieren? Es entsteht eine Stornorechnung mit eigener Nummer, die dem Kunden '
            'zugehen muss. Das lässt sich nicht rückgängig machen.')


def reissue_question(number):
    """Rückfrage zu „Stornieren und neu ausstellen“: Stornorechnung und Entwurf der neuen Rechnung."""
    return (f'Rechnung {number} stornieren und neu ausstellen? Es entsteht eine Stornorechnung mit eigener Nummer, '
            'die dem Kunden zugehen muss, und ein Entwurf einer neuen Rechnung mit den Angaben dieser Rechnung. '
            'Das Stornieren lässt sich nicht rückgängig machen.')


def new_draft_question(number):
    """Rückfrage zu „Neu ausstellen“ an einer stornierten Rechnung mit Stornorechnung: der Entwurf der neuen Rechnung."""
    return (f'Zur stornierten Rechnung {number} einen neuen Entwurf anlegen? Er übernimmt Kunde und Positionen, '
            'bekommt eine neue Nummer und das heutige Datum. Beim Versenden gehen die Zahlungen der stornierten '
            'Rechnung auf ihn über.')


def late_cancel_question(number):
    """Rückfrage zur nachträglichen Stornorechnung für eine von Hand stornierte Rechnung (Altbestand)."""
    return (f'Zur stornierten Rechnung {number} eine Stornorechnung erstellen? Sie bekommt eine eigene Nummer und muss '
            'dem Kunden zugehen. Steht die Forderung noch in den Büchern, wird sie ausgeglichen. Das lässt sich nicht '
            'rückgängig machen.')


def _address_text(snapshot, separator):
    """Anschrift aus ``buyer_snapshot``-Feldern als Text (Firma oder Name, Straße, PLZ Ort, Land
    außerhalb Deutschlands), getrennt durch ``separator``."""
    place = f"{snapshot.get('buyer_postal_code') or ''} {snapshot.get('buyer_city') or ''}".strip()
    country = snapshot.get('buyer_country') or 'DE'
    parts = [snapshot.get('buyer_company') or snapshot.get('buyer_name') or '',
             snapshot.get('buyer_street') or '', place, country if country != 'DE' else '']
    return separator.join(part for part in parts if part)


# Stornieren: Rückfrage mit Stornodatum (appConfirm mit Datumsfeld), dann POST /invoice/cancel und weiter auf die
# Seite der Stornorechnung – mit `reissue` („Stornieren und neu ausstellen“) auf die des Entwurfs der neuen Rechnung.
# Die Platzhalter ersetzt `script_json` (Rechnungsnummer und Datum stehen nie als Markup da).
_CANCEL_SCRIPT = """// Stornieren: Es entsteht eine Stornorechnung (eigener Beleg); danach öffnet sich ihre Seite
// (mit reissue: die des Entwurfs der neuen Rechnung)
function cancelInvoice(invId, reissue) {
  appConfirm(reissue ? __REISSUE_QUESTION__ : __QUESTION__, function(day) {
    var request = reissue ? {invoiceId: invId, date: day, reissue: true} : {invoiceId: invId, date: day};
    fetch('/invoice/cancel', {method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(request)}).then(r => r.json())
    .then(data => {
      if (!data.success) { showMessage('Fehler: ' + (data.error || '?'), 'error'); return; }
      var target = '/invoice?id=' + (data.replacement_id ? data.replacement_id : data.storno_id);
      var note = [data.warning, data.hint].filter(Boolean).join(' ');
      if (note) { showMessage(note, data.warning ? 'warn' : 'info'); setTimeout(function() { window.location.href = target; }, 3500); }
      else { window.location.href = target; }
    })
    .catch(function(err) { showMessage('Fehler: ' + err.message, 'error'); });
  }, __DATE_FIELD__);
}
"""

# Neu ausstellen: Zur stornierten Rechnung (mit Stornorechnung) entsteht der Entwurf einer neuen Rechnung
# (POST /invoice/reissue); danach öffnet sich seine Seite.
_REISSUE_SCRIPT = """// Neu ausstellen: Es entsteht der Entwurf einer neuen Rechnung; danach öffnet sich seine Seite
function reissueInvoice(invId) {
  appConfirm(__QUESTION__, function() {
    fetch('/invoice/reissue', {method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({invoiceId: invId})}).then(r => r.json())
    .then(data => {
      if (!data.success) { showMessage('Fehler: ' + (data.error || '?'), 'error'); return; }
      window.location.href = '/invoice?id=' + data.replacement_id;
    })
    .catch(function(err) { showMessage('Fehler: ' + err.message, 'error'); });
  });
}
"""


# ── Skonto beim Zuordnen einer Zahlung (seit 2.9.0) ──────────────────────────
# Das Häkchen „Rest als Skonto ausbuchen“ steht an drei Stellen (Rechnungsseite je Kandidat, Buchungsmaske, und – als Knopf „Mit Skonto zuordnen“ – im Kasten der Seite Buchungen):
# Zugeordnet wird mit Skonto nur auf diesen Klick, nie ohne, und nichts ist vorgewählt (das Häkchen ist beim Laden nie angehakt, ``autocomplete="off"`` hält auch den Browser davon ab,
# einen alten Stand wiederherzustellen). Es erscheint nur, wo der Rest nach der Zahlung (offen − Zahlung) größer 0 und höchstens das Skonto der Rechnung ist; der Server prüft dasselbe
# noch einmal (``Database.assign_invoice_payment(…, skonto=True)``) und bleibt maßgeblich. Die Frist wird gezeigt, nie erzwungen.
SKONTO_EXPIRED = ' (Frist abgelaufen)'
SKONTO_CHECK_TITLE = 'Der Rest der Rechnung nach dieser Zahlung wird als Skonto ausgebucht – nichts geschieht ohne dieses Häkchen.'


def skonto_check_html(row_id, box_id, rest_id, name='', data=''):
    """Das Häkchen „Rest … als Skonto ausbuchen“ als versteckte Zeile (``hidden``: Das Skript zeigt sie, wenn der Rest passt, und nimmt den Haken beim Verstecken zurück). ``row_id``, ``box_id``
    und ``rest_id``: die ``id`` der Zeile, des Kästchens und der Stelle für den Betrag – feste Texte oder mit einer Zahl gebaut, nie ein Wert aus der Datenbank; ``name``: ein Formularfeld
    (``skonto`` mit dem Wert ``1``: die Buchungsmaske), sonst keines (die Rechnungsseite schickt es per ``fetch``); ``data``: fertige ``data-``-Attribute der Zeile."""
    field = ' name="skonto" value="1"' if name else ''
    return (f'<div id="{row_id}"{data} hidden><label class="hint" title="{_html.escape(SKONTO_CHECK_TITLE, quote=True)}">'
            f'<input type="checkbox" id="{box_id}"{field} autocomplete="off"> Rest <span id="{rest_id}"></span> als Skonto ausbuchen</label></div>')


# Die Bausteine der beiden Skripte: Beträge in ganzen Cent (aus den ``data-``-Attributen im Punkt-Format und aus dem Betragsfeld), nur ``var`` und ``function`` (Präfix ``sko``), gesetzt wird
# nur über ``textContent``/``hidden``/``checked``. ``skoRest`` ist die EINE Regel: Rest nach der Zahlung, wenn er größer 0 und höchstens das Skonto ist, sonst 0.
SKONTO_JS = """// Skonto: Beträge in ganzen Cent. skoRest: der Rest nach der Zahlung (offen minus Zahlung), wenn er größer 0 und höchstens das Skonto ist – sonst 0
function skoCents(text) {
  var value = parseFloat(text);
  return isNaN(value) ? 0 : Math.round(value * 100);
}
function skoEuro(cents) {
  var abs = Math.abs(cents), whole = String(Math.floor(abs / 100)), fraction = ('0' + (abs % 100)).slice(-2), grouped = '';
  while (whole.length > 3) { grouped = '.' + whole.slice(-3) + grouped; whole = whole.slice(0, -3); }
  return (cents < 0 ? '-' : '') + whole + grouped + ',' + fraction + ' €';
}
function skoRest(open, limit, amount) {
  var paid = Math.min(open, amount), rest = open - paid;
  return paid > 0 && rest > 0 && rest <= limit ? rest : 0;
}
function skoShow(row, box, label, rest) {
  row.hidden = rest <= 0;
  if (rest <= 0) { box.checked = false; }
  label.textContent = rest > 0 ? skoEuro(rest) : '';
}
"""

# Rechnungsseite: je Kandidat der Zeile „Vorhandene Buchung verknüpfen“ ein Häkchen (``paySkonto_<ID der Bewegung>``). Die Werte stehen als ``data-``-Attribute: am Absatz mit der Frist
# (``paySkontoInfo``: ``data-open`` der offene Betrag, ``data-limit`` das Skonto, Euro im Punkt-Format) und an der Zeile des Häkchens (``data-free``: der freie Rest der Bewegung – der
# Betrag der Zahlung ist höchstens er, wie beim Zuordnen). ``SKO_ROWS``: die IDs der Bewegungen (``script_json``).
SKONTO_INVOICE_JS = """function skoInvoiceRow(id) {
  var info = document.getElementById('paySkontoInfo'), row = document.getElementById('paySkontoRow_' + id);
  var box = document.getElementById('paySkonto_' + id), input = document.getElementById('payAmount_' + id);
  var label = document.getElementById('paySkontoRest_' + id);
  if (!info || !row || !box || !input || !label) { return; }
  var paid = Math.min(skoCents(input.value), skoCents(row.getAttribute('data-free')));
  skoShow(row, box, label, skoRest(skoCents(info.getAttribute('data-open')), skoCents(info.getAttribute('data-limit')), paid));
}
function skoInvoiceInit() {
  for (var i = 0; i < SKO_ROWS.length; i++) { skoInvoiceRow(SKO_ROWS[i]); }
}
"""


def _skonto_info_html(offer, invoice_date, open_due, today_iso):
    """Die Zeile über der Liste „Vorhandene Buchung verknüpfen“: was die Rechnung an Skonto vorsieht – ``Skonto: 2 % = 2,38 € bis 10.03.2025``, nach der Frist mit dem Zusatz „(Frist abgelaufen)“
    (kein Verbot) – oder, wenn sie Bedingungen trägt, aber kein Skonto zulässt (eine Gruppe oder ein Kopfsatz ohne Skonto-Konto, ein Erlöskonto der Stammdaten, das selbst ein Skonto-Konto ist, Positionen, die nicht zu den Summen passen), der feste Satz der Ablehnung. Ohne Bedingungen (oder mit einem
    Skonto von 0,00 €) steht nichts da. ``offer``: ``Database.skonto_offer``; ``invoice_date``: ``Invoices.InvoiceDate`` (ISO-Text); ``open_due``: der offene Betrag (Euro); ``today_iso``: heute.
    Der Absatz einer Rechnung mit Skonto trägt die Werte für das Skript (``paySkontoInfo``, ``data-open``, ``data-limit``: Euro im Punkt-Format). Jeder Wert ist maskiert.
    Returns ``(HTML, buchbar)``: ``buchbar``, wenn der Absatz mit den Werten dasteht – nur dann bekommt jeder Kandidat sein Häkchen."""
    if offer is None or offer.error == SKONTO_NO_TERMS or (offer.error is None and not offer.limit):
        return '', False
    if offer.error is not None:
        return f'        <p class="hint">{_html.escape(offer.error)}</p>\n', False
    percent = f'{offer.percent.normalize():f}'.replace('.', ',')
    text = f'Skonto: {percent} % = {fmt_eur(from_minor(offer.limit))}'
    try:
        until = skonto_until(datetime.date.fromisoformat(str(invoice_date)), offer.days)
    except ValueError:
        until = None
    if until is not None:
        text += f' bis {until.strftime("%d.%m.%Y")}'
        if until.isoformat() < today_iso:
            text += SKONTO_EXPIRED
    return (f'        <p class="hint" id="paySkontoInfo" data-open="{format_plain(round_minor(to_minor(open_due), 2))}" '
            f'data-limit="{format_plain(offer.limit)}">{_html.escape(text)}</p>\n'), True


def delete_question(sent_before):
    return f'{DELETE_DRAFT_QUESTION} {DELETE_SENT_BEFORE_NOTE}' if sent_before else DELETE_DRAFT_QUESTION


def normalize_status(status):
    """Alt-Wert 'partial' (vor todo #2 geschrieben) auf den Kanon abbilden."""
    return 'partial_payment' if status == 'partial' else status


def _revenue_coa_select(db, tax_rate, selected=None, field_id='revenueCoa'):
    """Das Auswahlfeld „Erlöskonto“ (``revenue_coa_id``, Pflichtfeld) mit den wählbaren Erlöskonten
    (``Database.payment_revenue_choices``: ohne die Skonto-Konten). Vorgewählt ist ``selected`` (wenn es ein wählbares Erlöskonto ist), sonst der
    Vorschlag ``Database.suggest_revenue_coa`` – eine Vorauswahl, keine Entscheidung; ohne beides steht „bitte wählen“
    da. Alle Werte sind maskiert. ``field_id``: das ``id`` des Felds – eine Seite trägt mehrere Auswahlen (Zahlung, Neuaufbau
    der Zuordnung), jede mit eigener ID."""
    accounts = db.payment_revenue_choices()
    if selected not in {coa_id for coa_id, _number, _name in accounts}:
        selected = db.suggest_revenue_coa(tax_rate) if tax_rate is not None else None
    options = '<option value="">— bitte wählen —</option>'
    for coa_id, number, name in accounts:
        mark = ' selected' if coa_id == selected else ''
        label = _html.escape(f'{number} {name}')
        options += f'<option value="{int(coa_id)}"{mark}>{label}</option>'
    return f'<select name="revenue_coa_id" id="{_html.escape(field_id, quote=True)}" required>{options}</select>'


def revenue_coa_choice(db, tax_rate, selected=None, teaches=True, mixed=False, entry=False):
    """Pflichtfeld „Erlöskonto“ für eine Rechnungszahlung – nur, wenn für den Steuersatz der
    Rechnung kein Erlöskonto hinterlegt ist (``Database.revenue_coa_choice_needed``, z. B. 5 %
    oder 16 %), sonst ``None``: Dann ergibt sich die Kontierung aus der Rechnung.

    Returns ``(select_html, hint_html)`` (Auswahl: ``_revenue_coa_select``). ``teaches``: Die Wahl einer Zahlung gilt
    künftig als Vorgabe für diesen Steuersatz (so lernt ``resolve_revenue_coa``) – der Hinweis sagt es; die Wahl einer
    Erstattung lehrt nichts (``teaches=False``, die Sätze einer Erstattung zählen nicht). Alle Werte sind maskiert.

    ``mixed``: Die Rechnung trägt einen Steuersatz je Position (``Invoices.MixedTaxRates``). Dann gibt es nie eine Auswahl (immer ``None``): Ihr
    Kopfsatz ist nur ein Platzhalter, und jeder Satz hat sein Erlöskonto – fehlt eines, lehnt die Zahlung mit einem festen Satz ab
    (``MIXED_NO_REVENUE_COA``), ein Konto wählen lässt sich nicht.

    ``entry``: Die Rechnung trägt ein Erlöskonto aus den Stammdaten (``Database.invoice_entry_accounts``, die Momentaufnahme eines Steuersatzes, 2.8.0). Dann
    gilt dieses Konto, und es gibt nie eine Auswahl (immer ``None``): Eine Wahl widerspräche dem Konto, das die Rechnung beim Versenden zugesichert hat."""
    if mixed or entry or tax_rate is None or not db.revenue_coa_choice_needed(tax_rate):
        return None
    percent = tax_rate * 100 if tax_rate <= 1 else tax_rate
    percent_text = f'{percent:g}'.replace('.', ',')
    hint = f'Für {percent_text} % ist kein Erlöskonto hinterlegt.'
    if teaches:
        hint += ' Die Wahl gilt künftig als Vorgabe für diesen Steuersatz.'
    return _revenue_coa_select(db, tax_rate, selected), _html.escape(hint)


# Erstattung auf der Seite der Stornorechnung (Database.assign_storno_refund): Rückfrage beim Entfernen und das Skript, das
# zuordnet und entfernt. Es nutzt die Routen der Zahlungen (/invoice/link-payment, /invoice/delete-payment); die Platzhalter
# ersetzt der Aufrufer (Rechnungs-ID als Ganzzahl, Rückfrage über script_json).
REFUND_CONFIRM_QUESTION = ('Erstattung wirklich entfernen? Die von der App dafür angelegten Buchungssätze werden mit '
                           'entfernt; das Guthaben des Kunden steht danach wieder aus.')

_REFUND_SCRIPT = """    <script>
        // Das gewählte Erlöskonto ('' = es gibt kein Feld: die Kontierung ergibt sich aus den Zahlungen; null = Feld
        // ohne Wahl, der Nutzer wurde darauf hingewiesen)
        function revenueCoaChoice() {
            const field = document.getElementById('revenueCoa');
            if (!field) return '';
            if (!field.value) {
                showMessage('Bitte ein Erlöskonto wählen.', 'error');
                return null;
            }
            return field.value;
        }
        // Eine ausgehende Bankbewegung als Erstattung dieser Stornorechnung zuordnen
        function linkRefund(bookingId, button) {
            const amount = parseFloat(document.getElementById('refundAmount_' + bookingId).value);
            if (!amount || amount <= 0) {
                showMessage('Betrag muss größer 0 sein.', 'error');
                return;
            }
            const coa = revenueCoaChoice();
            if (coa === null) return;
            const payload = {invoice_id: __INVOICE_ID__, transaction_id: bookingId, amount_paid: amount};
            if (coa) payload.revenue_coa_id = parseInt(coa, 10);
            button.disabled = true;   // ein Doppelklick ordnet nicht doppelt zu
            fetch('/invoice/link-payment', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(payload)
            })
            .then(r => r.json())
            .then(data => {
                if (data.success) { location.reload(); }
                else {
                    button.disabled = false;
                    showMessage('Fehler: ' + (data.error || 'Unbekannt'), 'error');
                }
            })
            .catch(() => {
                button.disabled = false;
                showMessage('Fehler: Die Erstattung konnte nicht zugeordnet werden.', 'error');
            });
        }
        function deleteRefund(paymentId) {
            showConfirm(__CONFIRM__, function() {
                fetch('/invoice/delete-payment', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({payment_id: paymentId})
                })
                .then(r => r.json())
                .then(data => {
                    if (data.success) { location.reload(); }
                    else { showMessage('Fehler: ' + (data.error || 'Unbekannt'), 'error'); }
                });
            });
        }
    </script>
"""


def _refund_box_html(db, invoice_id, invoice, refunds, credit):
    """Kasten der Stornorechnung zu ihren Zahlungen: Solange das Guthaben des Kunden offen ist (``credit`` > 0) oder es
    Erstattungen gibt, der Kasten „Erstattung“ (``Database.assign_storno_refund``) – solange Guthaben bleibt, zuerst die
    ausgehenden Bankbewegungen mit freiem Rest zum Zuordnen (Betrag vorbelegt mit dem kleineren von Guthaben und freiem
    Rest; Erlöskonto zu wählen, wenn sich keines ermitteln lässt), darunter die zugeordneten Erstattungen mit „Entfernen“.
    Sonst nur der Hinweis, dass einer Stornorechnung keine Zahlung zugeordnet wird. ``invoice``: ihre Zeile, ``refunds``:
    ihre Zuordnungen (``Database.get_invoice_payments``), ``credit``: das Guthaben (Euro, ``Database.get_storno_credit``).
    Alle Werte aus der Datenbank sind maskiert; die Seite bleibt sonst schreibgeschützt."""
    credit = credit or 0
    if credit <= 0 and not refunds:
        return ('    <div class="rectRounded no-pdf"><h3>Zahlungsverknüpfungen</h3>\n'
                '        <p class="hint">Einer Stornorechnung lässt sich keine Zahlung zuordnen.</p>\n    </div>\n')
    h = '    <div class="rectRounded no-pdf" id="refund_box">\n        <h3>Erstattung</h3>\n'
    findings = db.find_payment_inconsistencies(invoice_id)
    if findings:
        h += ('        <div class="warnColor"><strong>Zahlungszuordnung prüfen:</strong><ul>\n'
              + ''.join(f'            <li>{_html.escape(f["text"])}</li>\n' for f in findings)
              + '        </ul>Alle Rechnungen mit Unstimmigkeiten zeigt die Prüfung unter '
              '<a href="/miscellaneous?check_payments=1">Daten</a>.</div>\n')
    if credit > 0:
        # Erlöskonto zu wählen: für den Steuersatz ist keines hinterlegt (die Wahl lehrt nichts), oder die Zahlungen liegen auf
        # verschiedenen Erlöskonten – dann sagt der Satz, warum. Bei einem Steuersatz je Position (Kennzeichen der Stornorechnung)
        # gibt es nie eine Wahl: Das Erlöskonto je Satz ist das der Zahlungen (``refund_revenue_coas_differ`` ist dort immer falsch)
        choice = revenue_coa_choice(db, invoice[35], teaches=False,
                                    mixed=has_mixed_rates(invoice[InvoiceCol.MIXED_TAX_RATES]),
                                    entry=db.invoice_entry_accounts(invoice_id))
        if db.refund_revenue_coas_differ(invoice_id):
            choice = (_revenue_coa_select(db, invoice[35]), _html.escape(MIXED_REVENUE_MESSAGE))
        if choice:
            h += (f'        <p class="hint">{choice[1]}</p>\n'
                  f'        <p><label>Erlöskonto: {choice[0]}</label></p>\n')
        linked = {refund[2] for refund in refunds}
        candidates = [c for c in db.get_unallocated_outgoing_bookings(invoice[13]) if c[0] not in linked]
        if candidates:
            h += ('        <h4>Ausgehende Bankbewegung als Erstattung zuordnen</h4>\n'
                  '        <table class="refundTable">\n'
                  '            <tr><th>Datum</th><th>Empfänger/Auftragg.</th><th class="num">Betrag</th>'
                  '<th class="num">Frei</th><th class="num">Erstatten</th><th></th></tr>\n')
            for b_id, b_date, b_recipient, _contact, b_amount, b_free in candidates:
                match_mark = ' ✓' if b_free == credit else ''
                h += ('            <tr><td>%s</td><td>%s</td><td class="num">%s</td><td class="num">%s</td>'
                      '<td class="num"><input type="number" step="0.01" min="0.01" class="noButtons" '
                      'id="refundAmount_%d" value="%s"></td>'
                      '<td><button onclick="linkRefund(%d, this)" class="coloredButton btn-sm bg-blue">'
                      'Als Erstattung zuordnen</button></td></tr>\n'
                      % (_html.escape(str(b_date or '–')), _html.escape(str(b_recipient or '')),
                         _html.escape(fmt_eur(b_amount)), _html.escape(fmt_eur(b_free) + match_mark), int(b_id),
                         format_plain(to_minor(min(credit, b_free))), int(b_id)))
            h += '        </table>\n'
        else:
            h += ('        <p class="hint">Es gibt keine ausgehende Bankbewegung mit freiem Rest, die sich als '
                  'Erstattung zuordnen ließe.</p>\n')
    elif credit == 0:
        h += '        <p class="hint">Das Guthaben ist vollständig erstattet.</p>\n'
    if refunds:
        h += ('        <h4>Zugeordnete Erstattungen</h4>\n'
              '        <table class="refundTable">\n'
              '            <tr><th>Datum</th><th>Buchung</th><th class="num">Betrag</th><th></th></tr>\n')
        for payment_id, _invoice, booking_id, amount, pay_date, _notes, booking_ref in refunds:
            h += ('            <tr><td>%s</td><td><a href="/transactions/edit?id=%d">#%d %s</a></td>'
                  '<td class="num">%s</td><td><a href="javascript:void(0);" onclick="deleteRefund(%d)" '
                  'class="action-icon delete-icon">&#10005; Entfernen</a></td></tr>\n'
                  % (_html.escape(str(pay_date or '–')), int(booking_id), int(booking_id),
                     _html.escape(str(booking_ref or '')), _html.escape(fmt_eur(amount)), int(payment_id)))
        h += '        </table>\n'
    h += '    </div>\n'
    return h + _REFUND_SCRIPT.replace('__INVOICE_ID__', str(int(invoice_id))).replace(
        '__CONFIRM__', script_json(REFUND_CONFIRM_QUESTION))


# Zuordnung neu aufbauen (Database.rebuild_invoice_payments, POST /invoice/rebuild-payments): Probelauf, Plan ansehen, bestätigen.
# Die Rückfrage ist fest; das Skript bekommt sie über script_json (Platzhalter __CONFIRM__).
REBUILD_CONFIRM_QUESTION = ('Zuordnung jetzt neu aufbauen? Es geschieht genau das, was der Probelauf zeigt; hat sich der Bestand '
                            'inzwischen geändert, wird abgelehnt.')

# Nur var und function, nie innerHTML: Jeder Wert der Antwort (Texte der Sätze, Befunde) wird als Text eingesetzt (textContent) –
# ein Verwendungszweck mit Markup bleibt Text. Die Prüfsumme des gezeichneten Plans steht am Kasten (data-check) und geht mit der
# Ausführung zurück; jede Zeichnung ersetzt sie, ein Plan ohne Aktionen bekommt keinen Knopf zum Ausführen. Der Hinweis zu EÜR und
# Umsatzsteuer (plan.warning) steht nur, wenn der Plan neue Buchungssätze enthält; der Knopf immer, wenn es Aktionen gibt.
_REBUILD_SCRIPT = """// Zuordnung neu aufbauen: Probelauf (ändert nichts), Plan ansehen, dann bestätigen
function rebuildElement(tag, text, className) {
  var node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = text;
  return node;
}
function rebuildList(texts) {
  var list = rebuildElement('ul');
  texts.forEach(function(text) { list.appendChild(rebuildElement('li', text)); });
  return list;
}
function rebuildTable(titles, lines, numeric) {
  var table = rebuildElement('table', null, 'refundTable');
  var head = rebuildElement('tr');
  titles.forEach(function(title, column) { head.appendChild(rebuildElement('th', title, numeric.indexOf(column) >= 0 ? 'num' : '')); });
  table.appendChild(head);
  lines.forEach(function(cells) {
    var line = rebuildElement('tr');
    cells.forEach(function(cell, column) { line.appendChild(rebuildElement('td', cell, numeric.indexOf(column) >= 0 ? 'num' : '')); });
    table.appendChild(line);
  });
  return table;
}
function renderRebuild(plan, invId) {
  var box = document.getElementById('rebuildPreview');
  box.textContent = '';
  box.setAttribute('data-check', plan.check);
  box.appendChild(rebuildElement('h4', 'Probelauf \\u2013 es wurde nichts ge\\u00e4ndert'));
  if (plan.nothingToDo) box.appendChild(rebuildElement('p', 'Es gibt nichts, was sich automatisch neu aufbauen l\\u00e4sst.'));
  else box.appendChild(rebuildList(plan.actions.map(function(action) { return action.text; })));
  if (plan.rows.length) {
    box.appendChild(rebuildElement('h4', 'Neue Buchungss\\u00e4tze'));
    box.appendChild(rebuildTable(['Datum', 'Konto', 'Gegenkonto', 'Betrag', 'Steuer', 'Text', 'in der E\\u00dcR'],
      plan.rows.map(function(row) { return [row.date, row.account, row.counterAccount, row.amount, row.tax, row.text, row.euer ? 'ja' : '\\u2013']; }), [3, 4]));
  }
  if (plan.euer.length) {
    box.appendChild(rebuildElement('h4', 'Wirkung auf die E\\u00dcR'));
    box.appendChild(rebuildTable(['Datum', 'Erl\\u00f6skonto', 'netto', 'Umsatzsteuer'],
      plan.euer.map(function(effect) { return [effect.date, effect.account, effect.net, effect.tax]; }), [2, 3]));
  }
  var invoice = plan.invoice;
  box.appendChild(rebuildElement('p', 'Offen: ' + invoice.dueBefore + ' \\u2192 ' + invoice.dueAfter + ', Status: ' + invoice.statusBefore
    + (invoice.statusAfter !== invoice.statusBefore ? ' \\u2192 ' + invoice.statusAfter : '')));
  if (plan.skipped.length) {
    box.appendChild(rebuildElement('h4', 'Nicht automatisch'));
    box.appendChild(rebuildList(plan.skipped));
  }
  if (plan.remaining.length) {
    box.appendChild(rebuildElement('h4', 'Danach meldet die Pr\\u00fcfung weiterhin'));
    box.appendChild(rebuildList(plan.remaining));
  }
  if (!plan.nothingToDo) {
    if (plan.rows.length) box.appendChild(rebuildElement('p', plan.warning, 'warnColor'));
    var button = rebuildElement('button', 'Neu aufbauen', 'coloredButton btn-sm bg-green');
    button.setAttribute('type', 'button');
    button.addEventListener('click', function() { appConfirm(__CONFIRM__, function() { rebuildPayments(invId, true); }); });
    box.appendChild(button);
  }
}
function rebuildPayments(invId, execute) {
  var box = document.getElementById('rebuildPreview');
  var request = {invoiceId: invId, execute: !!execute};
  var coa = document.getElementById('rebuildRevenueCoa');
  if (coa && coa.value) request.revenueCoaId = Number(coa.value);
  if (execute) request.check = box.getAttribute('data-check');
  fetch('/invoice/rebuild-payments', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(request)})
    .then(function(response) { return response.json().then(function(data) { return {status: response.status, data: data}; }); })
    .then(function(answer) {
      var data = answer.data;
      if (data.executed) {
        showMessage(data.message, 'info');
        setTimeout(function() { location.reload(); }, 1500);
        return;
      }
      if (data.plan) renderRebuild(data.plan, invId);
      if (data.success) return;
      if (data.needsRevenueCoa) showMessage(data.error, 'error');
      else if (answer.status === 409) showMessage(data.error, 'warn');
      else showMessage('Fehler: ' + (data.error || '?'), 'error');
    })
    .catch(function(err) { showMessage('Fehler: ' + err.message, 'error'); });
}
"""


def _rebuild_box_html(db, invoice_id, tax_rate, mixed=False, entry=False):
    """Der Neuaufbau der Zuordnung unter der Befundliste einer Rechnung (``Database.rebuild_invoice_payments``): der Knopf für den
    Probelauf, der Kasten für den Plan (ihn zeichnet das Skript) und – nur wenn für den Steuersatz kein Erlöskonto hinterlegt ist
    (die Reparatur legt nie eines an) – die Auswahl des Erlöskontos. Das Feld hat eine eigene ID (``rebuildRevenueCoa``): Die
    Zahlung („Verknüpfen“) trägt ihre eigene Auswahl. Die ID der Rechnung steht als Ganzzahl, sonst kein Wert aus der Datenbank.

    ``mixed``: Die Rechnung trägt einen Steuersatz je Position (``Invoices.MixedTaxRates``). Dann gibt es nie eine Auswahl: Ihr Kopfsatz ist nur
    ein Platzhalter, und der Neuaufbau rechnet für sie nur den offenen Betrag neu – er braucht kein Erlöskonto. ``entry``: Die Rechnung trägt ein
    Erlöskonto aus den Stammdaten (``Database.invoice_entry_accounts``): ebenso – der Neuaufbau rechnet nur den offenen Betrag neu (Grund ``tax_entry``).
    Eine Rechnung ohne Steuerausweis (§ 19, ``uses_small_business_rule``) zeigt ebenfalls nie eine Auswahl: Ihr Erlöskonto hängt am Datum der Bankbewegung jeder Zuordnung
    (4185 bis 2025, 4184 ab 2026), eine Wahl hinge vom heutigen Datum ab und bliebe wirkungslos – fehlt ein Konto, bleibt die Zuordnung mit festem Grund liegen."""
    h = ''
    if not mixed and not entry and not uses_small_business_rule(tax_rate) and db.resolve_revenue_coa(tax_rate, create=False) is None:
        h += ('        <p class="hint">Für den Neuaufbau ist kein Erlöskonto hinterlegt – bitte wählen.</p>\n'
              f'        <p><label>Erlöskonto: {_revenue_coa_select(db, tax_rate, field_id="rebuildRevenueCoa")}</label></p>\n')
    return h + ('        <p><button type="button" class="coloredButton btn-sm bg-blue" '
                f'onclick="rebuildPayments({int(invoice_id)}, false)">Zuordnung prüfen und neu aufbauen &hellip;</button></p>\n'
                '        <div id="rebuildPreview" class="no-pdf"></div>\n')


def document_submenu(active):
    """Gemeinsames Header2-Submenü für Rechnung / Angebot / Mahnwesen.

    active: 'invoice' | 'quote' | 'reminders'
    """
    def part(href, label, key):
        if key == active:
            return f'<span id="ActivePage">{label}</span>'
        return f'<a href="{href}">{label}</a>'
    return ' | '.join([
        part('/invoice', 'Rechnungen', 'invoice'),
        part('/quote', 'Angebote', 'quote'),
        part('/invoice/reminders', 'Mahnwesen', 'reminders'),
    ])


def PageInvoice(db: Database, filters: dict = None, invoice_id=None, copy_from=None, err=None):
    """Generate invoice list page with search and filter

    copy_from: ID einer Quellrechnung – das Formular öffnet sich dann als
    neue Rechnung mit deren Inhalten (todo #4).
    err: Grund, warum eine Aktion abgelehnt wurde (z. B. das Löschen einer versendeten Rechnung) –
    Klartext, kommt als Meldung auf die Seite.
    """
    filters = filters or {}

    # Eine Stornorechnung lässt sich nicht kopieren: Das Formular bleibt leer, mit Hinweis
    if copy_from and not invoice_id and is_storno_row(db.get_document(copy_from, 'invoice')):
        copy_from, err = None, err or STORNO_COPY_MESSAGE

    # Get filter parameters
    status_filter = filters.get('status', '')
    search_query = filters.get('search', '')
    date_from = filters.get('date_from', '')
    date_to = filters.get('date_to', '')
    
    # Fetch invoices with optional status filter
    if status_filter and status_filter != 'all':
        invoices = db.fetch_invoices(status=status_filter)
    else:
        invoices = db.fetch_invoices()
    
    # Apply additional filters in Python
    if search_query:
        search_lower = search_query.lower()
        invoices = [inv for inv in invoices
                   if search_lower in str(inv[InvoiceCol.NUMBER] or '').lower()
                   or search_lower in str(inv[InvoiceCol.BUYER_COMPANY] or '').lower()
                   or search_lower in str(inv[InvoiceCol.BUYER_NAME] or '').lower()]
    
    if date_from:
        invoices = [inv for inv in invoices if inv[2] >= date_from]  # InvoiceDate
    
    if date_to:
        invoices = [inv for inv in invoices if inv[2] <= date_to]
    
    s = Header1('invoice')

    # Status- und Such-Filter wandern nach Header2; der Zeitraum (Von/Bis + Jahr +
    # Monat) wird zentral in Header3 über period_filter_widget angeboten.
    status_options = {
        'all': 'Alle',
        'draft': 'Entwurf',
        'finalized': 'Abgeschlossen',
        'sent': 'Versendet',
        'paid': 'Bezahlt',
        'cancelled': 'Storniert'
    }

    status_dropdown = '<select id="statusFilter" onchange="applyInvoiceFilters()">'
    for value, label in status_options.items():
        selected = 'selected' if status_filter == value or (not status_filter and value == 'all') else ''
        status_dropdown += f'<option value="{value}" {selected}>{label}</option>'
    status_dropdown += '</select>'

    header2_content = (
        '<div class="rowWithObjects">'
        f'<div>{document_submenu("invoice")}</div>'
        f'<div><label>🔍 Suche:</label> <input type="text" id="searchQuery" '
        f'value="{_html.escape(search_query)}" placeholder="RNr. oder Kunde" '
        'onchange="applyInvoiceFilters()" style="width: 200px;"></div>'
        f'<div><label>Status:</label> {status_dropdown}</div>'
        '</div>'
    )
    s += Header2(header2_content)

    # Header3: zentraler Zeitraum-Filter (erhält Status/Suche/ausgewählte Rechnung)
    s += Header3(period_filter_widget(
        date_from, date_to, '/invoice',
        extra_params={'status': status_filter, 'search': search_query,
                      'id': invoice_id if invoice_id else ''}))

    # Statistics summary
    # Spalten laut Invoices-Schema: 38 = SumGross, 39 = AmountDue, 40 = Status
    def _gross(inv):
        return inv[38] or 0

    def _due(inv):
        return inv[39] if inv[39] is not None else _gross(inv)

    # Stornorechnungen zählen in keiner Summe mit (negative Beträge, kein Rest, keine Zahlung); ihre Zahl steht dabei
    counted = [inv for inv in invoices if not is_storno_row(inv)]
    storno_count = len(invoices) - len(counted)
    total_count = len(counted)
    total_sum = sum(_gross(inv) for inv in counted)
    paid_sum = sum(_gross(inv) - _due(inv) for inv in counted if inv[40] != 'cancelled')
    open_sum = sum(_due(inv) for inv in counted if inv[40] not in ('paid', 'cancelled'))
    storno_note = (f' | Stornorechnungen (nicht mitgezählt): {storno_count}' if storno_count else '')

    s+= '<div class="grid2Cols gridDocForm gridMain"><div class="gridLeftCol" style="order:1">'
    s+= f'''
    <div class="rectRounded">
        <strong>Statistik:</strong> 
        {total_count} Rechnung(en) | 
        Gesamtsumme: {fmt_eur(total_sum)} | 
        Bezahlt: {fmt_eur(paid_sum)} | 
        Offen: {fmt_eur(open_sum)}{storno_note}
    </div>
    '''
    if not invoices:
        s+= "<p><em>Keine Rechnungen gefunden.</em></p>"
    else:
        s+= '<table class="docList">'
        s+= ('<tr><th>RE-Nr.</th><th>Datum</th><th>Kunde</th><th class="num docWide">Netto</th><th class="num">Brutto</th>'
             '<th>Status</th><th>Aktionen</th></tr>')
        storno_credits = db.get_storno_credits()        # eine Abfrage für die ganze Liste

        for invoice in invoices:
            # invoice: ID=0, InvoiceNumber=1, InvoiceDate=2, OwnCompanyId=3, SellerName=4, SellerCompany=5, ..., CustomerId=13, BuyerName=14, BuyerCompany=15, ..., TaxRate=35, SumNet=36, TaxAmount=37, SumGross=38, AmountDue=39, Status=40, PDFPath=41
            inv_id = invoice[0]
            inv_number = _html.escape(str(invoice[1] or ''))
            inv_date = _html.escape(str(invoice[2] or ''))
            buyer_name = _html.escape(str(invoice[14] or '')[:30])  # BuyerName is now at index 14 (was 13)
            buyer_title = _html.escape(str(invoice[14] or ''), quote=True)      # die Spalte kürzt, der Tooltip nennt den Namen
            sum_net = invoice[36]  # SumNet is at index 36 (was 34)
            sum_gross = invoice[38]  # SumGross is at index 38 (was 36)
            status = normalize_status(invoice[InvoiceCol.STATUS])
            
            # Status color and label from shared module constants; eine Stornorechnung trägt statt des Status
            # (versendet) ihre Kennzeichnung – und, solange der Kunde ein Guthaben aus Zahlungen auf die stornierte
            # Rechnung hat, den Betrag, der noch zu erstatten bleibt
            if is_storno_row(invoice):
                credit = storno_credits.get(inv_id)
                status_color = STORNO_COLOR
                status_label = _html.escape(f'Erstattung offen: {fmt_eur(credit)}' if credit and credit > 0
                                            else STORNO_LABEL)
            else:
                status_color = INVOICE_STATUS_COLORS.get(status, '#888')
                status_label = _html.escape(str(INVOICE_STATUS_LABELS.get(status, status)))
            
            s+= f"<tr>"
            s+= f"<td>{inv_number}</td>"
            s+= f"<td>{inv_date}</td>"
            s+= f'<td class="docName" title="{buyer_title}">{buyer_name}</td>'
            s+= f'<td class="num docWide">{fmt_eur(sum_net)}</td>'
            s+= f'<td class="num"><strong>{fmt_eur(sum_gross)}</strong></td>'
            s+= f'<td class="docStatus" style="color: {status_color};"><strong>{status_label}</strong></td>'
            action_icon  = '&#9998;'    if status == 'draft' else '&#128065;'
            action_title = 'Bearbeiten' if status == 'draft' else 'Ansehen'
            s+= f"<td>"
            # PDF-Symbol laedt nur herunter, was abgelegt ist (todo #3) – gefragt wird dasselbe
            # wie beim Download (geprüfter Pfad mit Fallback auf das Namensschema)
            has_pdf = "true" if stored_invoice_path(db, inv_id, 'pdf', invoice)[0] else "false"
            has_xml = "true" if stored_invoice_path(db, inv_id, 'xml', invoice)[0] else "false"
            pdf_title = 'PDF herunterladen' if has_pdf == "true" else 'Kein PDF vorhanden'
            s+= (f'<a href="javascript:void(0);" onclick="downloadInvoiceDocs({inv_id}, '
                 f'{has_pdf}, {has_xml})" class="action-icon" title="{pdf_title}">&#128196;</a> ')
            s+= f"<a href='/invoice?id={inv_id}' class='action-icon' title='{action_title}'>{action_icon}</a>"
            if status == 'draft':
                sent_before = 'true' if db.was_sent_before(inv_id) else 'false'
                s+= (f' <a href="javascript:void(0);" onclick="deleteInvoiceDraft({int(inv_id)}, {sent_before})" '
                     'class="action-icon delete-icon" title="Entwurf löschen">&#128465;</a>')
            s+= f"</td>"
            s+= f"</tr>"
        
        s+= "</table>"
    
    s+= '''
    <script>
        // Vorhandene Dokumente herunterladen - hier wird nichts neu erzeugt
        // (todo #3). Erzeugt wird im Formular bzw. automatisch beim Versenden.
        function downloadInvoiceDocs(invoiceId, hasPdf, hasXml) {
            if (!hasPdf && !hasXml) {
                appMsg('Kein PDF vorhanden - bitte in der Rechnung erzeugen.', 'error');
                return;
            }
            if (hasPdf) appDownload('/invoice/pdf_download?id=' + invoiceId);
            if (hasXml) appDownload('/invoice/xml_download?id=' + invoiceId);
        }
        
        // Löschen gibt es nur für Entwürfe (POST, nach Rückfrage); war die Rechnung schon einmal versendet,
        // sagt die Rückfrage, was bleibt
        function deleteInvoiceDraft(invoiceId, sentBefore) {
            appConfirmPost('/invoice/delete?id=' + invoiceId,
                           sentBefore ? __DELETE_SENT_BEFORE__ : __DELETE_QUESTION__);
        }

        function applyInvoiceFilters() {
            const dateFrom = document.getElementById('dateFrom').value;
            const dateTo = document.getElementById('dateTo').value;
            const status = document.getElementById('statusFilter').value;
            const search = document.getElementById('searchQuery').value;
            
            // Build query string
            const params = new URLSearchParams();
            if (dateFrom) params.append('date_from', dateFrom);
            if (dateTo) params.append('date_to', dateTo);
            if (status && status !== 'all') params.append('status', status);
            if (search) params.append('search', search);
            
            // Preserve currently selected invoice in grid view
            const currentId = new URLSearchParams(window.location.search).get('id');
            if (currentId) params.append('id', currentId);
            // Reload page with filters
            window.location.href = '/invoice' + (params.toString() ? '?' + params.toString() : '');
        }
        
        function resetInvoiceFilters() {
            window.location.href = '/invoice';
        }
    </script>
    '''.replace('__DELETE_QUESTION__', script_json(DELETE_DRAFT_QUESTION)
                ).replace('__DELETE_SENT_BEFORE__', script_json(delete_question(True)))
    s+= '</div><!-- Ende gridLeftCol -->'
    s+= '<div class="gridRightCol" style="order:2">'
    s+= _invoice_form_html(db, invoice_id, copy_from)
    s+= '</div><!-- Ende gridRightCol --></div><!-- Ende grid2Cols -->'
    if err:
        s += ('<script>document.addEventListener("DOMContentLoaded",function(){'
              'appMsg(' + script_json(err) + ',"error");});</script>')
    s+= Footer()
    return s

def _einvoice_notice(db, invoice, invoice_id):
    """Was der E-Rechnung einer Rechnung fehlt, die sich noch ändern lässt: ``(Zeile, Rückfrage)``.

    Die Zeile („Für die XRechnung fehlt: …“ – nur wenn ein UBL entstehen soll, und nur, was allein ihm fehlt – und/oder „Für
    die E-Rechnung im PDF fehlt: …“) steht im Statuskasten und ist maskiert; sie ist leer, wenn nichts zu nennen ist. Die
    Rückfrage – ein Text oder ``None`` – stellt „Versenden“ vorher: Das Versenden erzeugt das Dokument, danach ist die
    Rechnung eingefroren. Die Prüfung ist dieselbe wie beim Erzeugen (``einvoice_gaps``) und die Aufteilung auch (keine Angabe
    steht doppelt); scheitert sie, lebt die Seite ohne die Zeile weiter."""
    try:
        gaps = einvoice_gaps(build_einvoice(db, invoice, db.get_invoice_items(invoice_id)))
    except Exception as e:
        print(f"E-Rechnung {invoice_id}: Prüfung nicht möglich: {e}")
        return '', None
    for_xrechnung = gap_texts(gaps, SCOPE_XRECHNUNG) if wants_xrechnung(invoice) else []
    for_pdf = gap_texts(gaps, SCOPE_EN16931)
    row = ' '.join(part for part in (
        f"Für die XRechnung fehlt: {', '.join(for_xrechnung)}." if for_xrechnung else '',
        f"Für die E-Rechnung im PDF fehlt: {', '.join(for_pdf)}." if for_pdf else '') if part)
    if not row:
        return '', None
    question = ' '.join(part for part in (
        f"Die XRechnung wird nicht erstellt – es fehlt: {', '.join(for_xrechnung)}." if for_xrechnung else '',
        f"Die E-Rechnung im PDF ist unvollständig – es fehlt: {', '.join(for_pdf)}." if for_pdf else '',
        'Trotzdem versenden?') if part)
    return f'<tr><td>E-Rechnung:</td><td>{_html.escape(row)}</td></tr>', question


def _invoice_form_html(db: Database, invoice_id=None, copy_from=None):
    """Generate invoice form HTML block (no page wrapper).

    copy_from: ID einer Quellrechnung. Deren Inhalte füllen das Formular, es
    bleibt aber eine *neue* Rechnung – frisches Datum, frische Nummer, Entwurf
    (todo #4).
    """
    import datetime
    import json
    current_year = datetime.datetime.now().year
    today_iso = datetime.date.today().isoformat()

    cancel_script = ''           # nur eine versendete Rechnung kann storniert werden (Skript steht hinter dem Formular)
    rebuild_script = ''          # das Skript zum Neuaufbau der Zuordnung: nur mit Befund (steht ebenfalls hinter dem Formular)
    # Load existing invoice if invoice_id is provided
    existing_invoice = None
    existing_items = []
    existing_payments = []
    is_copy = bool(copy_from) and not invoice_id
    source_id = invoice_id or (copy_from if is_copy else None)
    if source_id:
        # Nur Rechnungen: die ID eines Angebots zeigt hier kein fremdes Dokument im falschen
        # Formular, sondern das leere Formular für eine neue Rechnung
        existing_invoice = db.get_document(source_id, 'invoice')
        if existing_invoice:
            existing_items = db.get_invoice_items(source_id)
            # Zahlungen gehören zur Quellrechnung und werden nicht mitkopiert
            existing_payments = [] if is_copy else db.get_invoice_payments(source_id)
        else:
            is_copy = False

    # Storno-Paar: Die Stornorechnung nennt ihre Ursprungsrechnung, die stornierte Rechnung ihre Stornorechnung
    is_storno = bool(existing_invoice) and not is_copy and is_storno_row(existing_invoice)
    storno_original = db.find_cancelled_invoice(invoice_id) if is_storno else None
    storno_invoice = (db.find_storno_invoice(invoice_id)
                      if existing_invoice and not is_copy and not is_storno else None)
    # Neu ausstellen: Die stornierte Rechnung kennt ihre Ersatzrechnung, die Ersatzrechnung die stornierte Rechnung
    replacement_invoice = db.find_replacement_invoice(invoice_id) if storno_invoice else None
    replaced_invoice = (db.find_replaced_invoice(invoice_id)
                        if existing_invoice and not is_copy and not is_storno else None)
    # Guthaben des Kunden aus Zahlungen auf die stornierte Rechnung (beide Belege des Paares zeigen es)
    storno_credit = db.get_storno_credit(invoice_id) if existing_invoice and not is_copy else None
    storno_refunded = db.get_storno_refunded(invoice_id) if existing_invoice and not is_copy else None
    doc_title = STORNO_LABEL if is_storno else 'Rechnung'

    # Get company data (own contact)
    own_contacts = db.fetch_contacts(contact_type='own')
    own_contact = own_contacts[0] if own_contacts else None
    
    # Get customers for selection
    customers = db.fetch_contacts(contact_type='customer')
    
    # Get bank accounts for selection
    accounts = db.fetch_accounts()
    
    # Get only active articles for selection in invoices
    articles = db.fetch_articles(active_only=True)
    
    # Steuersatz der Quelle (Bearbeiten wie Kopieren): gespeichert als Dezimal
    # (0.19) bzw. 0; Sentinel -1 = kein USt-Ausweis (Kleinunternehmer).
    tax_pct = 19
    tax_prefill_note = ''
    if existing_invoice:
        _r = (existing_invoice[35] if len(existing_invoice) > 35
              and existing_invoice[35] is not None else None)
        show_tax = not (_r is not None and float(_r) < 0)
        if _r is not None and float(_r) >= 0:
            tax_pct = float(_r) * 100 if 0 <= float(_r) <= 1 else float(_r)
    else:
        # Neue Rechnung ohne Quelle: das Häkchen übernimmt den Stand der jüngsten gewöhnlichen Rechnung; startet es
        # abgehakt, sagt die Maske es dazu
        show_tax = db.last_document_shows_tax()
        if not show_tax:
            tax_prefill_note = TAX_PREFILL_NOTE

    # Steuersatz je Position: Trägt der Beleg, den die Maske lädt (Bearbeiten, Kopie, Entwurf einer Ersatzrechnung, versendete Rechnung), das Kennzeichen, steht
    # das Häkchen da und jede Zeile zeigt ihren Satz. Ohne Steuerausweis gibt es das Häkchen nie.
    mixed_on = bool(existing_invoice) and show_tax and has_mixed_rates(
        existing_invoice[InvoiceCol.MIXED_TAX_RATES] if len(existing_invoice) > InvoiceCol.MIXED_TAX_RATES else None)

    # Determine invoice number and initial values
    number_hint = False
    if existing_invoice and not is_copy:
        # Editing existing invoice - use existing data
        # existing_invoice structure: ID=0, InvoiceNumber=1, InvoiceDate=2, OwnCompanyId=3, SellerName=4, SellerCompany=5, ..., CustomerId=13, BuyerName=14, BuyerCompany=15, ..., Status=40, PDFPath=41
        invoice_number = str(existing_invoice[1] or '')
        invoice_date = existing_invoice[2]
        selected_own_company_id = existing_invoice[3]
        selected_customer_id = existing_invoice[13]  # CustomerId is now at index 13 (was 12)
        invoice_status = normalize_status(existing_invoice[40])  # Status at index 40
        due_date_value = existing_invoice[27] if len(existing_invoice) > 27 else None
        page_title = (f"Stornorechnung {_html.escape(invoice_number)}" if is_storno
                      else f"Rechnung {_html.escape(invoice_number)} bearbeiten")
        is_edit_mode = True
    else:
        # Creating new invoice - Vorschlag aus dem Nummernkreis (Formatvorlage und Zusatz des Kreises,
        # eine schon belegte Nummer wird übersprungen); verbraucht wird die Nummer erst beim Speichern
        invoice_number, suggestion_range = db.peek_next_number('invoice', current_year)
        invoice_number = invoice_number or ''
        # Es gibt einen Kreis, aber keine Nummer daraus (Formatvorlage ohne Zähler): kein Vorschlag, dafür ein Hinweis
        number_hint = (suggestion_range is not None and not invoice_number)

        # Kopie: Datum von heute, alles andere aus der Quelle (todo #4)
        invoice_date = today_iso if is_copy else ''
        selected_own_company_id = (existing_invoice[3] if is_copy
                                   else (own_contact[0] if own_contact else None))
        selected_customer_id = existing_invoice[13] if is_copy else None
        invoice_status = 'draft'
        # Fälligkeitsdatum der Quelle ist verbraucht – wird aus dem neuen
        # Rechnungsdatum und dem Zahlungsziel neu berechnet
        due_date_value = None
        if is_copy:
            page_title = f"Neue Rechnung – Kopie von {_html.escape(str(existing_invoice[1] or ''))}"
        else:
            page_title = "Neue Rechnung erstellen"
        is_edit_mode = False
    
    # Versendet (gesperrt) oder storniert: Die Finanzangaben stehen im Formular nicht mehr offen
    # (db/invoice_lock.py; serverseitig entscheidet check_changes, das Formular zeigt es nur an).
    # Bei einer versendeten Rechnung bleiben Zahlungshinweis, Anschrift, Bestellnummer, Leitweg-ID und
    # Leistungsdatum änderbar (als Berichtigung), bei einer stornierten nichts.
    group = (CLOSED if is_storno else status_group(invoice_status)) if is_edit_mode else EDITABLE
    locked = group != EDITABLE
    closed = group == CLOSED
    lock_attr = ' disabled' if locked else ''
    closed_attr = ' disabled' if closed else ''
    mixed_attr = lock_attr if show_tax else ' disabled'      # das Häkchen „Steuersatz je Position“: ohne Steuerausweis gesperrt, in einer gesperrten Rechnung auch
    # Die wählbaren Steuersätze aus den Stammdaten und das Feld „Mehrwertsteuer“ im Kopf; eine neue Zeile startet wie das Feld
    choices = entry_choices(db)
    head = head_select(choices, existing_items, tax_pct, bool(existing_invoice), show_tax, lock_attr)
    tax_pct = head.rate
    # Eine Rechnung, die schon einmal versendet war, behält ihre Nummer auch als Entwurf (der Kunde kennt sie)
    number_kept = is_edit_mode and group == EDITABLE and db.was_sent_before(invoice_id)
    number_attr = lock_attr or (' readonly' if number_kept else '')
    # Nummer der Fassung, die diese Maske geladen hat: Beim Berichtigen schickt sie sie mit (Schutz davor, eine
    # inzwischen anderweitig berichtigte Rechnung zu überschreiben)
    document_version = (as_document_version(existing_invoice[InvoiceCol.DOCUMENT_VERSION])
                        if is_edit_mode and len(existing_invoice) > InvoiceCol.DOCUMENT_VERSION else '')
    # Leitweg-ID: im Entwurf folgt sie dem Kunden (schreibgeschützt); bei einer versendeten Rechnung steht
    # der gespeicherte Wert da und ist ein normales Eingabefeld
    route_attr = closed_attr if locked else ' readonly'
    route_help = ('Gespeicherter Wert; bei einer versendeten Rechnung frei änderbar (nur bei B2G-Rechnungen erforderlich)'
                  if locked else
                  'Wird automatisch vom Kunden übernommen (nur bei B2G-Rechnungen erforderlich)')

    # Die ID gehört nur in den Bearbeiten-Modus: ein unbekannter oder fremder Beleg (Angebot)
    # ergibt ein leeres Formular, das beim Speichern eine neue Rechnung anlegt
    s = f'<input type="hidden" id="invoice_id" value="{invoice_id if is_edit_mode else ""}">'
    s += f'<input type="hidden" id="is_edit_mode" value="{str(is_edit_mode).lower()}">'
    s += f'<input type="hidden" id="invoice_status_value" value="{_html.escape(str(invoice_status))}">'
    s += f'<input type="hidden" id="invoice_locked" value="{str(locked).lower()}">'
    s += f'<input type="hidden" id="document_version" value="{document_version}">'
    # Die Nummer im Feld ist nur ein Vorschlag, solange sie unverändert bleibt: Dann vergibt der Server beim
    # Speichern frisch (ein zweites Fenster kann den Vorschlag inzwischen verbraucht haben)
    s += ('<input type="hidden" id="number_suggestion" value="'
          f'{"" if is_edit_mode else _html.escape(invoice_number)}">')
    # Check if PDF/XML actually exist – dieselbe Frage wie beim Download (geprüfter Pfad mit Fallback)
    pdf_file_exists = bool(is_edit_mode and stored_invoice_path(db, invoice_id, 'pdf', existing_invoice)[0])
    xml_file_exists = bool(is_edit_mode and stored_invoice_path(db, invoice_id, 'xml', existing_invoice)[0])
    s += f'<input type="hidden" id="pdf_exists" value="{str(pdf_file_exists).lower()}">'
    s += f'<input type="hidden" id="xml_exists" value="{str(xml_file_exists).lower()}">'
    s += '<div id="invoice_msg" class="no-pdf" style="display:none; margin-bottom:10px;"></div>'
    if is_edit_mode:
        payment_count = len(existing_payments)
        if is_storno:
            status_color, status_label = STORNO_COLOR, _html.escape(STORNO_LABEL)
        else:
            status_color  = INVOICE_STATUS_COLORS.get(invoice_status, '#888')
            status_label  = _html.escape(str(INVOICE_STATUS_LABELS.get(invoice_status, invoice_status)))
        # Angeboten werden nur die Status, auf die die Rechnung gesetzt werden darf (Übergangstabelle); „storniert“
        # nur für eine Rechnung ohne positiven Betrag (sonst ist Stornieren ein eigener Knopf), und eine Rechnung
        # mit Stornorechnung bleibt storniert
        gross = existing_invoice[InvoiceCol.SUM_GROSS] or 0
        targets = allowed_targets(invoice_status, payment_count, storno_invoice is not None, gross)
        status_options_html = ''.join(
            '<option value="{k}"{sel}>{v}</option>'.format(
                k=_html.escape(k), v=_html.escape(str(INVOICE_STATUS_LABELS.get(k, k))),
                sel=' selected' if k == invoice_status else '')
            for k in [invoice_status, *targets]
        )
        set_status_attr = '' if targets else ' disabled'
        save_button = ('' if closed else
                       '<button onclick="saveInvoice()" class="coloredButton btn-sm bg-green">'
                       + ('✏ Berichtigen' if locked else '💾 Speichern') + '</button>')
        # Löschen gibt es nur für einen Entwurf (POST nach Rückfrage); sonst erst auf Entwurf zurücksetzen
        delete_button = ('' if invoice_status != 'draft' else
                         f'<button onclick="appConfirmPost(\'/invoice/delete?id={int(invoice_id)}\', '
                         + _html.escape(script_json(delete_question(db.was_sent_before(invoice_id))))
                         + ')" class="coloredButton btn-sm bg-red">🗑 Löschen</button>')
        # Stornieren gibt es für eine versendete Rechnung mit positivem Betrag (gesperrt, noch nicht storniert), nie für
        # eine Stornorechnung; eine ohne positiven Betrag wird über den Status storniert. Kopieren lässt sich eine
        # Stornorechnung nicht
        can_cancel = (not is_storno and not closed and canonical_status(invoice_status) in LOCKED_STATUSES
                      and gross > 0)
        # Eine von Hand stornierte Rechnung (Altbestand) mit positivem Betrag bekommt ihre Stornorechnung nachträglich;
        # zu einer stornierten Rechnung mit Stornorechnung und ohne Ersatzrechnung gibt es „Neu ausstellen“
        can_late_cancel = (not is_storno and canonical_status(invoice_status) == 'cancelled' and gross > 0
                           and storno_invoice is None)
        can_reissue = (storno_invoice is not None and canonical_status(invoice_status) == 'cancelled'
                       and replacement_invoice is None)
        cancel_button = ''
        if can_cancel:
            cancel_button = ('<button onclick="cancelInvoice(%d)" class="coloredButton btn-sm bg-red">'
                             '&#10060; Stornieren &hellip;</button>\n'
                             '                    <button onclick="cancelInvoice(%d, true)" '
                             'class="coloredButton btn-sm bg-red">&#10060; Stornieren und neu ausstellen '
                             '&hellip;</button>' % (int(invoice_id), int(invoice_id)))
        elif can_late_cancel:
            cancel_button = ('<button onclick="cancelInvoice(%d)" class="coloredButton btn-sm bg-red">'
                             '&#10060; Stornorechnung nachträglich erstellen &hellip;</button>' % int(invoice_id))
        elif can_reissue:
            cancel_button = ('<button onclick="reissueInvoice(%d)" class="coloredButton btn-sm bg-blue">'
                             '&#8635; Neu ausstellen &hellip;</button>' % int(invoice_id))
        copy_button = ('' if is_storno else
                       f'<button onclick="copyInvoiceAsNew({invoice_id})" class="coloredButton btn-sm bg-gray">📋 Als neu kopieren</button>')
        invoice_number_text = existing_invoice[1] or ''
        if can_cancel or can_late_cancel:
            invoice_day = str(existing_invoice[2])[:10]
            values = {'QUESTION': script_json(cancel_question(invoice_number_text) if can_cancel
                                              else late_cancel_question(invoice_number_text)),
                      'REISSUE_QUESTION': script_json(reissue_question(invoice_number_text)),
                      'DATE_FIELD': script_json({'label': 'Stornodatum:', 'min': invoice_day, 'max': today_iso,
                                                 'value': max(today_iso, invoice_day), 'error': STORNO_DATE_ERROR})}
            # in einem Durchgang: Die Rechnungsnummer in der Frage darf kein Platzhalter sein
            cancel_script = re.sub(r'__(QUESTION|REISSUE_QUESTION|DATE_FIELD)__', lambda m: values[m.group(1)],
                                   _CANCEL_SCRIPT)
        elif can_reissue:
            cancel_script = _REISSUE_SCRIPT.replace(
                '__QUESTION__', script_json(new_draft_question(invoice_number_text)))
        # Hinweiszeilen zum Storno-Paar (Link auf den jeweils anderen Beleg), zum Guthaben und zu Ersatzrechnung und
        # ersetzter Rechnung; alles maskiert
        storno_row = ''
        if is_storno and storno_original:
            storno_row = ('<tr><td>Storno:</td><td>Stornorechnung zu Rechnung <a href="/invoice?id=%d">%s</a> vom %s</td></tr>'
                          % (int(storno_original[0]), _html.escape(str(storno_original[1])),
                             _html.escape(_german_date(local_date(storno_original[2])))))
        elif storno_invoice:
            storno_row = ('<tr><td>Storno:</td><td>Storniert durch Stornorechnung <a href="/invoice?id=%d">%s</a> vom %s</td></tr>'
                          % (int(storno_invoice[0]), _html.escape(str(storno_invoice[1])),
                             _html.escape(_german_date(local_date(storno_invoice[2])))))
        if storno_credit is not None and storno_credit > 0:
            storno_row += ('<tr><td>Storno:</td><td>Guthaben des Kunden: %s – Zahlungen auf die stornierte Rechnung</td></tr>'
                           % _html.escape(fmt_eur(storno_credit)))
        elif storno_credit == 0 and storno_refunded:
            storno_row += '<tr><td>Storno:</td><td>Guthaben vollständig erstattet.</td></tr>'
        if replacement_invoice:
            storno_row += ('<tr><td>Neue Rechnung:</td><td><a href="/invoice?id=%d">%s</a></td></tr>'
                           % (int(replacement_invoice[0]), _html.escape(str(replacement_invoice[1]))))
        if replaced_invoice:
            replaced_storno = (' (Stornorechnung <a href="/invoice?id=%d">%s</a>)'
                               % (int(replaced_invoice[2]), _html.escape(str(replaced_invoice[3])))
                               if replaced_invoice[2] is not None else '')
            storno_row += ('<tr><td>Ersetzt:</td><td>stornierte Rechnung <a href="/invoice?id=%d">%s</a>%s</td></tr>'
                           % (int(replaced_invoice[0]), _html.escape(str(replaced_invoice[1])), replaced_storno))
        # Was der E-Rechnung fehlt: eine Zeile im Kasten und die Rückfrage vor dem Versenden – nur, solange sich an der
        # Rechnung etwas ändern lässt (Entwurf, abgeschlossen), also nie bei einer versendeten, stornierten oder Stornorechnung
        einvoice_row, einvoice_question = (_einvoice_notice(db, existing_invoice, invoice_id)
                                           if group == EDITABLE else ('', None))
        status_row = '' if is_storno else f'''
            <tr>
                <td>Neuer Status:</td>
                <td><div class="rowWithObjects">
                    <select id="statusChangeSelect">{status_options_html}</select>
                    <button onclick="setInvoiceStatus({invoice_id})" class="coloredButton btn-sm bg-blue"{set_status_attr}>&#9888; Status setzen</button>
                </div></td>
            </tr>'''
        s += '<div class="rectRounded no-pdf">' #right Column Headline, Status and Buttons
        s += f'<h2>{page_title}</h2>'
        s += f'''
        <table width="100%">
            <tr>
                <td>Status:</td>
                <td><div class="rowWithObjects">
                    '''
        if locked:
            s += '<div class="rectRounded bg-red">'
        s += f'<strong class="coloredButtonNoClick btn-smNoClick bg-back" style="color:{status_color}; hover:none;">{status_label}</strong>'
        if is_storno:
            s += f' &nbsp; &nbsp; &#9888; {_html.escape(STORNO_LOCKED_MESSAGE)}</div>'
        elif closed:
            s += ' &nbsp; &nbsp; &#9888; Storniert: Die Rechnung l&auml;sst sich nicht mehr &auml;ndern.</div>'
        elif locked:
            s += f' &nbsp; &nbsp; &#9888; {_html.escape(SENT_HINT)}</div>'
        s += f'''
                </div></td>
            </tr>{storno_row}{status_row}{einvoice_row}
            <tr>
                <td>Rechnung:</td>
                <td><div class="rowWithObjects">
                    {save_button}
                    <button onclick="window.location.href='/invoice'" class="coloredButton btn-sm bg-gray">← Abbrechen</button>
                    <button onclick="generatePDF()" class="coloredButton btn-sm bg-blue">📄 PDF + E-Rechnung</button>
                    <button onclick="downloadPDF()" class="coloredButton btn-sm bg-blue">⬇ Herunterladen</button>
                    {copy_button}
                    {cancel_button}
                    {delete_button}
                </div></td>
            </tr>
        </table>
    </div>
<script>
// Fehlt der E-Rechnung etwas (null: nichts, oder das Versenden erzeugt kein Dokument), fragt „Versenden“ vorher nach
const einvoiceSendQuestion = {script_json(einvoice_question)};
function setInvoiceStatus(invId) {{
  var ns = document.getElementById('statusChangeSelect').value;
  var change = function(confirmed) {{
    fetch('/invoice/status', {{method: 'POST', headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{invoice_id: invId, status: ns, confirm_exported: confirmed === true}})}}).then(r => r.json())
    .then(data => {{
      // Das Zurückholen einer Rechnung, deren Forderung schon in einem DATEV-Export stand, ist eine Rückfrage, kein Fehler
      if (!data.success && data.confirm === 'exported') {{ appConfirm(data.error, function() {{ change(true); }}); return; }}
      if (!data.success) {{ showMessage('Fehler: ' + (data.error || '?'), 'error'); return; }}
      // "Versendet" erzeugt das PDF automatisch (todo #2) - Hinweis, falls das
      // nicht geklappt hat; sonst direkt neu laden.
      var note = [data.warning, data.hint].filter(Boolean).join(' ');
      if (note) {{ showMessage(note, data.warning ? 'warn' : 'info'); setTimeout(function() {{ location.reload(); }}, 3500); }}
      else {{ location.reload(); }}
    }});
  }};
  if (ns === 'sent' && einvoiceSendQuestion) appConfirm(einvoiceSendQuestion, change); else change();
}}
// Identische Rechnung als neue Rechnung oeffnen - frisches Datum, neue
// Rechnungsnummer (todo #4)
function copyInvoiceAsNew(invId) {{
  window.location.href = '/invoice?copy_from=' + invId;
}}
</script>'''
    else:
        s += '<div class="rectRounded no-pdf">'
        s += f'<h2>{page_title}</h2>'
        s += '''
        <table width="100%">
            <tr>
                <td>Rechnung:</td>
                <td><div class="rowWithObjects">
                    <button onclick="saveInvoice()" class="coloredButton btn-sm bg-green">💾 Speichern</button>
                    <button onclick="window.location.href=\'/invoice\'" class="coloredButton btn-sm bg-gray">← Abbrechen</button>
                </div></td>
            </tr>
        </table>
    </div>'''
    s += '''<div class="invoice-container" id="invoice_container">
        <div class="invoice-header">
            <div class="invoice-logo">
                <img id="company_logo" src="" alt="Firmenlogo" style="max-width: 150px; max-height: 80px;" onerror="this.style.display='none';">
            </div>
            <div class="invoice-meta">
                <table class="invoice-meta-table">
                    <tr><td>Rechnungsdatum:</td><td><input type="date" id="invoice_date" value="'''
    s += _html.escape(str(invoice_date)) if invoice_date else ''
    s += '" style="width: 150px;"' + lock_attr + '></td></tr>\n    '
    # Leistungsdatum direkt unter dem Rechnungsdatum: die beiden Daten gehören
    # zusammen und werden zusammen gepflegt
    _dd = existing_invoice[25] if existing_invoice and len(existing_invoice) > 25 and existing_invoice[25] else ''
    s += ('<tr><td title="Datum der Lieferung/Leistung bzw. Leistungszeitraum (Pflichtangabe nach § 14 UStG)">Leistungsdatum:</td>'
          f'<td><input type="date" id="delivery_date" required value="{_html.escape(str(_dd))}" style="width: 150px;"{closed_attr}></td></tr>')
    # Ende des Leistungszeitraums (leer = nur das Leistungsdatum); bei einer versendeten Rechnung wie das Leistungsdatum änderbar (Berichtigung)
    _de = (existing_invoice[InvoiceCol.SERVICE_DATE_END]
           if existing_invoice and len(existing_invoice) > InvoiceCol.SERVICE_DATE_END and existing_invoice[InvoiceCol.SERVICE_DATE_END] else '')
    s += ('<tr><td title="Ende des Leistungszeitraums – leer, wenn die Leistung an einem Tag erbracht wurde">bis:</td>'
          f'<td><input type="date" id="service_date_end" value="{_html.escape(str(_de))}"{closed_attr}></td></tr>')
    s += f'<tr><td>Rechnungs-Nr.:</td><td><input type="text" id="invoice_number" value="{_html.escape(invoice_number)}" style="width: 150px;"{number_attr}></td></tr>'
    if number_kept:
        s += f'<tr class="no-pdf"><td colspan="2" class="hint">{_html.escape(NUMBER_KEPT_MESSAGE)}</td></tr>'
    if number_hint:
        s += ('<tr class="no-pdf"><td colspan="2" class="hint">Der Nummernkreis liefert keine Nummer – bitte die '
              'Formatvorlage unter Stammdaten &rarr; Nummernkreise pr&uuml;fen (genau ein Z&auml;hler: '
              '{n}, {nn} oder {nnn}).</td></tr>')
    s += '''
                    <tr><td>Kunden-Nr.:</td><td><input type="text" id="customer_number" readonly style="width: 150px; background-color: #f0f0f0;"></td></tr>
                </table>
            </div>
        </div>
        
        <div class="invoice-address-block">
            <div class="invoice-customer-address">
                <select id="own_company_select" onchange="updateOwnCompany()" style="margin-bottom: 10px; width: 100%;" class="no-pdf"''' + lock_attr + '''>
                    <option value="">-- Eigene Firma auswählen --</option>
    '''
    
    for own in own_contacts:
        own_name = _html.escape(str(own[3])) if own[3] else f"ID {own[0]}"  # display_name at index 3
        selected = 'selected' if (existing_invoice and own[0] == selected_own_company_id) or (not existing_invoice and own_contact and own[0] == own_contact[0]) else ''
        s += f'<option value="{own[0]}" {selected}>{own_name}</option>'
    
    s += '''
                </select>
            <div class="invoice-sender-line" id="sender_line">
    '''
    
    if own_contact:
        sender_street = _html.escape(str(own_contact[5] or ''))
        sender_postal = _html.escape(str(own_contact[6] or ''))
        sender_city   = _html.escape(str(own_contact[7] or ''))
        sender_name   = _html.escape(str(own_contact[3] or ''))  # display_name
        s += f'{sender_name} · {sender_street} · {sender_postal} {sender_city}'
    else:
        s += 'Eigene Adresse in Kontakte anlegen (Typ: own)'
    
    s += '''            </div>
                <select id="customer_select" onchange="updateCustomerAddress()" style="margin-bottom: 10px; width: 100%;" class="no-pdf"''' + lock_attr + '''>
                    <option value="">-- Kunde auswählen --</option>
    '''
    
    for customer in customers:
        cust_name = _html.escape(str(customer[3])) if customer[3] else f"ID {customer[0]}"  # display_name at index 3
        selected = 'selected' if existing_invoice and customer[0] == selected_customer_id else ''
        s += f'<option value="{customer[0]}" {selected}>{cust_name}</option>'
    
    # Eine versendete Rechnung zeigt ihre gespeicherte Anschrift (Momentaufnahme beim Erstellen), nicht die
    # heutigen Stammdaten; die Stammdaten übernimmt nur ein ausdrücklicher Wunsch (Kästchen unter der Anschrift)
    stored_address, refresh_html = '', ''
    if locked and existing_invoice:
        keys = ('buyer_name', 'buyer_company', 'buyer_street', 'buyer_postal_code', 'buyer_city', 'buyer_country')
        stored_snapshot = dict(zip(keys, existing_invoice[14:20]))
        stored_address = _html.escape(_address_text(stored_snapshot, '\n'))
        master = db.get_contact_by_id(existing_invoice[13]) if existing_invoice[13] and not closed else None
        if master:
            fresh = buyer_snapshot(master)
            if any((stored_snapshot[k] or ('DE' if k == 'buyer_country' else ''))
                   != (fresh[k] or '') for k in keys):
                refresh_html = ('<div class="no-pdf hint"><label><input type="checkbox" id="refresh_address"> '
                                'Anschrift aus den Stammdaten &uuml;bernehmen</label><br>'
                                + _html.escape(_address_text(stored_snapshot, ', ')) + ' &rarr; '
                                + _html.escape(_address_text(fresh, ', ')) + '</div>')
    s += '''                </select>
                <div id="customer_address_display" style="min-height: 80px; white-space: pre-line;">''' + stored_address + '''</div>''' + refresh_html + '''
            </div>
        </div>
        
        <h1 class="invoice-title">''' + doc_title + '''</h1>
        
        <table class="invoice-items''' + (' mixedTaxOn' if mixed_on else '') + '''" id="invoice_table">
            <thead>
                <tr>
                    <th style="width: 40px;">Pos.</th>
                    <th style="width: 70px;">Menge</th>
                    <th style="width: 70px;">Einheit</th>
                    <th>Bezeichnung</th>
                    ''' + MIXED_HEAD_CELL + '''
                    <th style="width: 100px;">Einzelpreis</th>
                    <th style="width: 100px;">Gesamt</th>
                    <th style="width: 30px;" class="no-pdf"></th>
                </tr>
            </thead>
            <tbody id="invoice_items_body">'''
    
    # Add existing items or one empty row
    if existing_items:
        for idx, item in enumerate(existing_items, 1):
            # item: ID=0, InvoiceId=1, Position=2, ArticleId=3, Description=4, Quantity=5, Unit=6, PricePerUnit=7, TotalNet=8, TaxCategory=9, TaxRate=10
            # Alles aus der Datenbank steht maskiert im Attribut: ein " im Text (Monitor 27")
            # schnitte ihn sonst ab, und der Rest ginge beim Speichern verloren
            quantity = _html.escape(str(item[5]))
            unit = _html.escape(str(unit_display(item[6])))
            description = _html.escape(str(item[4] or ''))
            price = _html.escape(str(item[7]))
            total = item[8]
            s += f'''
                <tr class="invoice-item-row" data-row="{idx}">
                    <td>{idx}</td>
                    <td><input type="number" class="item-quantity" value="{quantity}" min="0" step="0.01" style="width: 60px;"{lock_attr}></td>
                    <td><input type="text" class="item-unit" value="{unit}" style="width: 60px;"{lock_attr}></td>
                    <td><input type="text" class="item-description" value="{description}" style="width: 100%;"{lock_attr}></td>
                    {mixed_cell_html(item[ITEM_TAX_RATE], lock_attr, choices, item_snapshot(item))}
                    <td><input type="number" class="item-price" value="{price}" min="0" step="0.01" style="width: 80px;"{lock_attr}> €</td>
                    <td class="item-total" style="text-align: right;">{fmt_eur(total)}</td>
                    <td class="no-pdf"><button type="button" onclick="removeRow(this)" style="color: red;"{lock_attr}>✕</button></td>
                </tr>'''
    else:
        s += '''
                <tr class="invoice-item-row" data-row="1">
                    <td>1</td>
                    <td><input type="number" class="item-quantity" value="1" min="0" step="0.01" style="width: 60px;"></td>
                    <td><input type="text" class="item-unit" value="Stk." style="width: 60px;"></td>
                    <td><input type="text" class="item-description" style="width: 100%;"></td>
                    ''' + mixed_cell_html(head.row_state[0], '', choices, head.row_state[1]) + '''
                    <td><input type="number" class="item-price" value="0" min="0" step="0.01" style="width: 80px;"> €</td>
                    <td class="item-total" style="text-align: right;">0,00 €</td>
                    <td class="no-pdf"><button type="button" onclick="removeRow(this)" style="color: red;">✕</button></td>
                </tr>'''
    
    s += '''
            </tbody>
            <tfoot>
                <tr><td colspan="7" style="height: 10px; border: none;"></td></tr>
                <tr class="totals-row totals-row-border">
                    <td colspan="5" style="text-align: right; border: none;">Summe netto:</td>
                    <td id="sum_net" style="text-align: right; font-weight: bold;">0,00 €</td>
                    <td class="no-pdf" style="border: none;"></td>
                </tr>
                <tr class="no-pdf">
                    <td colspan="7" style="text-align: right; border: none;">
                        <label><input type="checkbox" id="show_tax" ''' + ('checked' if show_tax else '') + lock_attr + ''' onchange="calculateTotals()"> Umsatzsteuer ausweisen</label>
                        <small style="color:#888;">(aus = Kleinunternehmer §&nbsp;19 UStG; der Hinweis darauf wird automatisch gedruckt)</small>
                        ''' + mixed_checkbox_html(mixed_on, mixed_attr) + ' ' + TAX_LINK_HTML + '''
                        ''' + tax_prefill_note + '''
                        ''' + TAX_NOTE_HTML + '''
                    </td>
                </tr>
                <tr class="totals-row" id="tax_row">
                    <td colspan="4" style="text-align: right; border: none;">Mehrwertsteuer ''' + head.html + '''<input type="hidden" id="tax_rate" value="''' + f'{tax_pct:g}' + '"' + lock_attr + '''></td>
                    <td style="text-align: right; border: none;">auf <span id="tax_base">0,00</span> € netto:</td>
                    <td id="tax_amount" style="text-align: right; font-weight: bold;">0,00 €</td>
                    <td class="no-pdf" style="border: none;"></td>
                </tr>
                <tr class="totals-row totals-row-final">
                    <td colspan="5" style="text-align: right; border: none;"><strong>Gesamtbetrag:</strong></td>
                    <td id="sum_gross" style="text-align: right; font-weight: bold;"><strong>0,00 €</strong></td>
                    <td class="no-pdf" style="border: none;"></td>
                </tr>
            </tfoot>
        </table>
        
        <div style="margin: 10px 0;" class="no-pdf">
            <button type="button" onclick="addFreeRow()" style="margin-right: 10px;"''' + lock_attr + '''>+ Position frei editierbar hinzufügen</button>
            <span style="position:relative; display:inline-block;">
                <input type="text" id="articleComboInput" placeholder="Artikel suchen …"
                       style="width:280px; padding:4px 8px;" autocomplete="off"
                       oninput="filterArticles(this.value)" onfocus="filterArticles(this.value)"
                       onblur="setTimeout(()=>document.getElementById('articleDropdown').style.display='none',150)"''' + lock_attr + '''>
                <div id="articleDropdown" style="display:none; position:absolute; top:100%; left:0;
                     width:440px; background:#fff; border:1px solid #ccc; border-radius:4px;
                     max-height:280px; overflow-y:auto; z-index:999;
                     box-shadow:0 2px 8px rgba(0,0,0,0.18);"></div>
            </span>
        </div>
    '''

    s += '''
        
        <div class="invoice-payment-terms">'''
    
    # Payment terms text
    payment_terms_text = _html.escape(str(existing_invoice[26])) if existing_invoice and existing_invoice[26] else 'Bitte überweisen Sie den Gesamtbetrag ohne jeden Abzug unter Angabe der Rechnungsnummer innerhalb von 14 Tagen ab Rechnungsdatum auf das unten angegebene Konto. Vielen Dank.'
    s += f'<textarea id="payment_terms" rows="3" style="width: 100%;"{closed_attr}>{payment_terms_text}</textarea>'
    
    s += '''
        </div>
        
        <div class="invoice-footer">
            <hr>
            <table class="footer-table">
                <tr>
                    <td class="footer-col-left">
                        <strong>Anschrift</strong><br>
                        <div id="footer_company_address">
    '''
    
    if own_contact:
        addr_line1_html = f'{_html.escape(str(own_contact[25]))}<br>' if (len(own_contact) > 25 and own_contact[25]) else ''
        s += f'''                        {_html.escape(str(own_contact[3])) if own_contact[3] else 'Firma'}<br>
                        {addr_line1_html}                        {_html.escape(str(own_contact[5])) if own_contact[5] else 'Straße'}<br>
                        {_html.escape(str(own_contact[6])) if own_contact[6] else 'PLZ'} {_html.escape(str(own_contact[7])) if own_contact[7] else 'Ort'}'''
    else:
        s += '                        <em>Eigene Firmendaten in Kontakte anlegen</em>'
    
    s += '''                        </div>
                    </td>
                    <td class="footer-col-center">
                        <strong>Kontakt</strong><br>
                        <div id="footer_company_contact">
    '''
    
    if own_contact:
        s += f'''                        <span class="footer-label">Tel</span> {_html.escape(str(own_contact[10])) if own_contact[10] else '-'}<br>
                        <span class="footer-label">E-Mail</span> {_html.escape(str(own_contact[9])) if own_contact[9] else '-'}<br>
                        <span class="footer-label">UStIdNr</span> {_html.escape(str(own_contact[11])) if own_contact[11] else '-'}'''
    else:
        s += '                        <em>Kontaktdaten fehlen</em>'
    
    # Zahlungsart: vorgewählt nach der gespeicherten (NULL = Überweisung); ein gespeicherter Wert, der keine Zahlungsart ist, erscheint als eigene gewählte Option
    # mit dem Wert „?“ – nichts wird still zur Überweisung, und der Server lehnt „?“ beim Speichern ab, bis neu gewählt ist
    stored_means = (existing_invoice[InvoiceCol.PAYMENT_MEANS]
                    if existing_invoice and len(existing_invoice) > InvoiceCol.PAYMENT_MEANS else None)
    chosen_means = payment_means_code(stored_means)
    means_options = ''.join(
        f'<option value="{"" if code == PAYMENT_MEANS_DEFAULT else code}"{" selected" if code == chosen_means else ""}>{_html.escape(label)}</option>'
        for code, label in PAYMENT_MEANS.items())
    if chosen_means is None:
        means_options += '<option value="?" selected>(gespeicherter Wert nicht lesbar)</option>'
    means_html = (f'<div class="no-pdf">Zahlungsart: <select id="payment_means" class="no-pdf"{lock_attr}>{means_options}</select></div>')

    s += '''                        </div>
                    </td>
                    <td class="footer-col-right">
                        <strong>Bankverbindung</strong><br>
                        <select id="bank_account_select" onchange="updateBankDetails()" style="width: 100%; margin-top: 5px;" class="no-pdf"''' + lock_attr + '''>
                            <option value="">-- Konto auswählen --</option>
    '''
    
    # BankAccountId is now at index 30 (was 28)
    selected_bank_id = existing_invoice[30] if existing_invoice and len(existing_invoice) > 30 and existing_invoice[30] else None
    for account in accounts:
        if not account[6]:  # Skip cash accounts (IsCash=1)
            selected = 'selected' if selected_bank_id and account[0] == selected_bank_id else ''
            s += f'<option value="{account[0]}" {selected}>{_html.escape(str(account[1] or ""))}</option>'
    
    s += '''                        </select>
                        <div id="bank_details" style="margin-top: 5px;">
                            
                        </div>
                        ''' + means_html + '''
                    </td>
                </tr>
            </table>
        </div>
    </div>
    
    <!-- XRechnung / E-Rechnung Zusatzdaten (optional) -->
    <div class="rectRounded no-pdf">
        <button type="button" onclick="toggleXRechnungFields()" class="coloredButton">⚙️ XRechnung / E-Rechnung Zusatzdaten (optional) <span id="xrechnung_toggle">&#9654;</span></button>
        <div id="xrechnung_fields" style="display: none;">
            <table style="width: 100%; border-collapse: collapse;">
                <tr>
                    <td style="padding: 8px; width: 200px; vertical-align: top;"><strong>Bestellnummer:</strong></td>
                    <td style="padding: 8px;">
                        <input type="text" id="order_number" style="width: 300px;" placeholder="z.B. PO-2026-12345" value="'''
    s += _html.escape(str(existing_invoice[23])) if existing_invoice and len(existing_invoice) > 23 and existing_invoice[23] else ''  # OrderNumber is now at index 23 (was 21)
    s += '"' + closed_attr + '''>
                        <br><small style="color: #666;">Bestellnummer des Kunden (falls vorhanden)</small>
                    </td>
                </tr>
                <tr>
                    <td style="padding: 8px; vertical-align: top;"><strong>Leitweg-ID:</strong></td>
                    <td style="padding: 8px;">
                        <input type="text" id="buyer_route_id" style="width: 300px;" placeholder="z.B. 991-ABCDE-12"''' + route_attr + ''' value="'''
    s += _html.escape(str(existing_invoice[22])) if existing_invoice and len(existing_invoice) > 22 and existing_invoice[22] else ''  # BuyerRouteID is now at index 22 (was 20)
    s += '''">
                        <br><small style="color: #666;">''' + route_help + '''</small>
                    </td>
                </tr>
                <tr>
                    <td style="padding: 8px; vertical-align: top;"><strong>Zahlungsziel (Tage):</strong></td>
                    <td style="padding: 8px;">
                        <input type="number" id="payment_term_days" value="'''
    # Calculate payment term days from due date and invoice date if available
    if existing_invoice and existing_invoice[27] and existing_invoice[2]:  # PaymentDueDate is now at index 27 (was 25)
        from datetime import datetime
        try:
            due_date = datetime.strptime(existing_invoice[27], '%Y-%m-%d')
            inv_date = datetime.strptime(existing_invoice[2], '%Y-%m-%d')
            term_days = (due_date - inv_date).days
            s += str(term_days)
        except:
            s += '14'
    else:
        s += '14'
    s += '" min="0" style="width: 100px;"' + lock_attr + '''> Tage
                        <br><small style="color: #666;">Anzahl der Tage bis zur Fälligkeit (Standard: 14 Tage)</small>
                    </td>
                </tr>
                <tr>
                    <td style="padding: 8px; vertical-align: top;"><strong>Fälligkeitsdatum:</strong></td>
                    <td style="padding: 8px;">
                        <input type="date" id="payment_due_date" style="width: 200px;" value="'''
    s += _html.escape(str(due_date_value or ''))  # PaymentDueDate (bei einer Kopie leer)
    s += '"' + lock_attr + '''>
                        <br><small style="color: #666;">Wird automatisch berechnet oder kann manuell festgelegt werden</small>
                    </td>
                </tr>
                <tr>
                    <td style="padding: 8px; vertical-align: top;"><strong>Skonto:</strong></td>
                    <td style="padding: 8px;">
                        <input type="number" id="discount_percentage" min="0" max="100" step="0.1" style="width: 80px;" placeholder="z.B. 2" value="'''
    s += _html.escape(str(existing_invoice[29])) if existing_invoice and len(existing_invoice) > 29 and existing_invoice[29] else ''  # SkontoPercent is now at index 29 (was 27)
    s += '"' + lock_attr + '''> %
                        bei Zahlung innerhalb von
                        <input type="number" id="discount_days" min="0" style="width: 80px;" placeholder="z.B. 7" value="'''
    s += _html.escape(str(existing_invoice[28])) if existing_invoice and len(existing_invoice) > 28 and existing_invoice[28] else ''  # SkontoDays is now at index 28 (was 26)
    s += '"' + lock_attr + '''> Tagen
                        <br><small style="color: #666;">Skonto-Abzug bei frühzeitiger Zahlung (optional)</small>
                        <br><small id="skonto_hint" class="warnColor" hidden></small>
                    </td>
                </tr>
            </table>
        </div>
    </div>
    ''' + mixed_tax_script(choices) + '''
    <script>
        // Own company data from server
        const ownCompaniesData = '''
    
    # Build JavaScript own companies data object
    own_companies_dict = {}
    for own in own_contacts:
        own_companies_dict[str(own[0])] = {
            'id':      own[0],
            'name':    own[3] or '',             # display_name
            'company': own[4] or own[3] or '',   # company_name fallback to display_name
            'address_line1': own[25] if len(own) > 25 and own[25] else '',
            'street':  own[5] or '',
            'postal':  own[6] or '',
            'city':    own[7] or '',
            'email':   own[9] or '',
            'phone':   own[10] or '',
            'tax_id':  own[11] or '',
            'logo':    logo_url(own[13]) if len(own) > 13 and own[13] else ''
        }
    
    s += _json_for_script(own_companies_dict)
    
    s += ''';
        
        // Customer data from server
        const customersData = '''
    
    # Build JavaScript customer data object
    customers_dict = {}
    for customer in customers:
        customers_dict[str(customer[0])] = {
            'id':             customer[0],
            'customer_number':customer[2] or '',
            'name':           customer[3] or '',   # display_name
            'company':        customer[4] or '',   # company_name
            'address_line1':  customer[25] if len(customer) > 25 and customer[25] else '',
            'street':         customer[5] or '',
            'postal':         customer[6] or '',
            'city':           customer[7] or '',
            'buyer_route_id': customer[14] or ''
        }
    
    s += _json_for_script(customers_dict)
    
    s += ''';
        
        // Bank accounts data from server
        const banksData = '''
    
    # Build JavaScript bank data object
    banks_dict = {}
    for account in accounts:
        if not account[6]:  # Skip cash accounts
            banks_dict[str(account[0])] = {
                'name': account[1],
                'bank_name': account[5] or '',
                'iban': account[3] or '',
                'bic': account[4] or ''
            }
    
    s += _json_for_script(banks_dict)
    
    s += ''';
        
        // Articles data from server
        const articlesData = '''
    
    # Build JavaScript articles data object
    articles_dict = {str(article[0]): article_json(article) for article in articles}

    s += _json_for_script(articles_dict)
    
    s += ''';
        
        // Set today's date only for new invoices
        const invoiceDate = document.getElementById('invoice_date').value;
        if (!invoiceDate) {
            document.getElementById('invoice_date').valueAsDate = new Date();
        }

        // Leistungsdatum (Pflicht, § 14 UStG): mit dem Rechnungsdatum vorbelegen
        // und mitlaufen lassen, solange der Nutzer es nicht selbst angefasst hat.
        (function () {
            const inv = document.getElementById('invoice_date');
            const del = document.getElementById('delivery_date');
            let touched = !!del.value && del.value !== inv.value;
            // Eine versendete Rechnung ohne Leistungsdatum (Import) bleibt ohne: Vorbelegt nur im Entwurf
            if (!del.value && document.getElementById('invoice_locked').value !== 'true') del.value = inv.value;
            del.addEventListener('input', () => { touched = true; });
            inv.addEventListener('change', () => { if (!touched) del.value = inv.value; });
        })();
        
        // Vorausgewählte Werte beim Laden übernehmen – in JEDEM Modus: auch eine
        // neue Rechnung hat eine vorausgewählte eigene Firma (bisher fehlte dort
        // das Logo), eine Kopie zusätzlich Kunde und Bankverbindung.
        updateOwnCompany();
        updateCustomerAddress();
        updateBankDetails();
        calculateTotals();

        // Stand beim Laden: Eine versendete Rechnung schickt beim Speichern nur, was der Nutzer davon ändert
        const initialOpenFields = {
            paymentTerms: document.getElementById('payment_terms').value,
            orderNumber: document.getElementById('order_number').value,
            buyerReference: document.getElementById('buyer_route_id').value,
            deliveryDate: document.getElementById('delivery_date').value,
            serviceDateEnd: document.getElementById('service_date_end').value
        };
        
        // Kontakt- und Bankdaten stammen aus den Stammdaten und kommen als DOM-Knoten
        // in die Seite (Text bleibt Text, nie Markup). Eine Zeile ist ein Text oder
        // [Beschriftung, Wert]; zwischen den Zeilen steht je ein <br>-Knoten.
        function setLines(box, lines) {
            box.textContent = '';
            lines.forEach(function(line, i) {
                if (i > 0) box.appendChild(document.createElement('br'));
                if (Array.isArray(line)) {
                    const label = document.createElement('span');
                    label.className = 'footer-label';
                    label.textContent = line[0];
                    box.appendChild(label);
                    box.appendChild(document.createTextNode(' ' + line[1]));
                } else {
                    box.appendChild(document.createTextNode(line));
                }
            });
        }
        // Hinweistext (kursiv) statt Daten, z. B. wenn noch keine eigene Firma angelegt ist
        function setHint(box, text) {
            const hint = document.createElement('em');
            hint.textContent = text;
            box.textContent = '';
            box.appendChild(hint);
        }

        // Update own company data (logo, sender line, footer)
        function updateOwnCompany() {
            const companyId = document.getElementById('own_company_select').value;
            const logoImg = document.getElementById('company_logo');
            const senderLine = document.getElementById('sender_line');
            const footerAddress = document.getElementById('footer_company_address');
            const footerContact = document.getElementById('footer_company_contact');
            
            if (!companyId) {
                // Reset to default
                logoImg.src = '';
                logoImg.style.display = 'none';
                senderLine.textContent = 'Eigene Adresse in Kontakte anlegen (Typ: own)';
                setHint(footerAddress, 'Eigene Firmendaten in Kontakte anlegen');
                setHint(footerContact, 'Kontaktdaten fehlen');
                return;
            }
            
            const company = ownCompaniesData[companyId];
            console.log('Selected company:', company);
            
            if (company) {
                // Update logo
                if (company.logo) {
                    console.log('Loading logo from:', company.logo);
                    logoImg.src = company.logo;
                    logoImg.style.display = '';
                    logoImg.onerror = function() {
                        console.error('Failed to load logo from:', this.src);
                        this.style.display = 'none';
                    };
                } else {
                    console.log('No logo specified for this company');
                    logoImg.src = '';
                    logoImg.style.display = 'none';
                }
                
                // Update sender line (OHNE Zusatzzeile – aus Platzgründen)
                const displayName = company.company || company.name;
                senderLine.textContent = displayName + ' · ' + company.street + ' · ' + company.postal + ' ' + company.city;

                // Update footer address (MIT Zusatzzeile, falls vorhanden)
                const addressLines = [displayName];
                if (company.address_line1) addressLines.push(company.address_line1);
                addressLines.push(company.street);
                addressLines.push(company.postal + ' ' + company.city);
                setLines(footerAddress, addressLines);
                
                // Update footer contact
                setLines(footerContact, [
                    ['Tel', company.phone || '-'],
                    ['E-Mail', company.email || '-'],
                    ['UStIdNr', company.tax_id || '-']
                ]);
            }
        }
        
        // Update customer address
        function updateCustomerAddress() {
            const customerId = document.getElementById('customer_select').value;
            const addressDisplay = document.getElementById('customer_address_display');
            const customerNumberField = document.getElementById('customer_number');
            const buyerRouteIdField = document.getElementById('buyer_route_id');
            
            console.log('updateCustomerAddress called, customerId:', customerId);
            console.log('customersData:', customersData);

            // Eine versendete Rechnung behält ihre gespeicherte Anschrift und Leitweg-ID: Das Formular
            // überschreibt beim Laden nichts aus den Stammdaten (nur die Kundennummer wird angezeigt)
            if (document.getElementById('invoice_locked').value === 'true') {
                const savedCustomer = customersData[customerId];
                customerNumberField.value = savedCustomer ? savedCustomer.customer_number : '';
                return;
            }
            
            if (!customerId) {
                addressDisplay.innerHTML = '';
                customerNumberField.value = '';
                if (buyerRouteIdField) buyerRouteIdField.value = '';
                return;
            }
            
            const customer = customersData[customerId];
            console.log('Selected customer:', customer);
            if (customer) {
                const displayName = customer.company || customer.name;
                let address = displayName + '\\n';
                if (customer.address_line1) address += customer.address_line1 + '\\n';
                if (customer.street) address += customer.street + '\\n';
                if (customer.postal || customer.city) address += (customer.postal + ' ' + customer.city).trim();
                
                addressDisplay.textContent = address;
                customerNumberField.value = customer.customer_number;
                
                // Update Leitweg-ID from customer (for B2G invoices)
                if (buyerRouteIdField) {
                    buyerRouteIdField.value = customer.buyer_route_id || '';
                }
            }
        }
        
        // Update bank details
        function updateBankDetails() {
            const bankId = document.getElementById('bank_account_select').value;
            const bankDetails = document.getElementById('bank_details');
            
            if (!bankId) {
                bankDetails.textContent = '';
                return;
            }
            
            const bank = banksData[bankId];
            if (bank) {
                setLines(bankDetails, [
                    ['Bank', bank.bank_name],
                    ['IBAN', bank.iban],
                    ['BIC', bank.bic]
                ]);
            }
        }
        
        // Toggle XRechnung fields visibility
        function toggleXRechnungFields() {
            const fields = document.getElementById('xrechnung_fields');
            const toggle = document.getElementById('xrechnung_toggle');
            if (fields.style.display === 'none') {
                fields.style.display = 'block';
                toggle.textContent = '\u25BC';
            } else {
                fields.style.display = 'none';
                toggle.textContent = '\u25B6';
            }
        }
        
        // Auto-calculate payment due date when invoice date or payment terms change
        function updatePaymentDueDate() {
            const invoiceDate = document.getElementById('invoice_date').value;
            const paymentTermDays = parseInt(document.getElementById('payment_term_days').value) || 14;
            
            if (invoiceDate) {
                const date = new Date(invoiceDate);
                date.setDate(date.getDate() + paymentTermDays);
                const year = date.getFullYear();
                const month = String(date.getMonth() + 1).padStart(2, '0');
                const day = String(date.getDate()).padStart(2, '0');
                document.getElementById('payment_due_date').value = year + '-' + month + '-' + day;
            }
        }
        
        // Attach listeners for automatic due date calculation
        document.getElementById('invoice_date').addEventListener('change', updatePaymentDueDate);
        document.getElementById('payment_term_days').addEventListener('input', updatePaymentDueDate);
        
        // Initial due date calculation
        updatePaymentDueDate();

        // Skonto gesetzt, der Zahlungshinweis sagt aber „ohne Abzug“: Hinweis unter dem Skonto-Feld (Text über textContent)
        function updateSkontoHint() {
            const terms = document.getElementById('payment_terms');
            const text = terms.value.toLowerCase();
            const set = parseFloat(document.getElementById('discount_percentage').value) > 0
                        && parseInt(document.getElementById('discount_days').value) > 0;
            const contradicts = set && !terms.disabled && (text.includes('ohne jeden abzug') || text.includes('ohne abzug'));
            const hint = document.getElementById('skonto_hint');
            hint.textContent = contradicts ? 'Der Zahlungshinweis sagt „ohne Abzug“ – bitte zum Skonto passend ändern.' : '';
            hint.hidden = !contradicts;
        }
        ['discount_percentage', 'discount_days', 'payment_terms'].forEach(function(id) {
            document.getElementById(id).addEventListener('input', updateSkontoHint);
        });
        updateSkontoHint();
        
        // Artikel-Combobox. Name und Einheit der Artikel stammen aus den Stammdaten: Sie kommen
        // als DOM-Knoten (textContent) in die Seite, nie als Markup.
        function filterArticles(query) {
            const dropdown = document.getElementById('articleDropdown');
            const q = query.toLowerCase();
            const entries = Object.entries(articlesData).filter(([id, a]) =>
                !q || a.name.toLowerCase().includes(q)
            );
            if (!entries.length) { dropdown.style.display = 'none'; return; }
            dropdown.textContent = '';
            entries.forEach(([id, a]) => {
                const option = document.createElement('div');
                option.className = 'article-option';
                option.addEventListener('mousedown', () => selectArticle(id));
                const name = document.createElement('strong');
                name.textContent = a.name;
                const meta = document.createElement('span');
                meta.className = 'article-option-meta';
                meta.textContent = a.unit + ' · ' + fmtDe(a.price) + ' € · ' + a.taxRate + '%';
                option.appendChild(name);
                option.appendChild(meta);
                dropdown.appendChild(option);
            });
            dropdown.style.display = 'block';
        }
        function selectArticle(articleId) {
            addArticleRow(articleId);
            document.getElementById('articleComboInput').value = '';
            document.getElementById('articleDropdown').style.display = 'none';
        }
        
        let rowCounter = 1;
        
        // Add new free editable row
        function addFreeRow() {
            rowCounter++;
            const tbody = document.getElementById('invoice_items_body');
            const newRow = document.createElement('tr');
            newRow.className = 'invoice-item-row';
            newRow.setAttribute('data-row', rowCounter);
            newRow.innerHTML = `
                <td>${rowCounter}</td>
                <td><input type="number" class="item-quantity" value="1" min="0" step="0.01" style="width: 60px;"></td>
                <td><input type="text" class="item-unit" value="Stk." style="width: 60px;"></td>
                <td><input type="text" class="item-description" style="width: 100%;"></td>
                <td><input type="number" class="item-price" value="0" min="0" step="0.01" style="width: 80px;"> €</td>
                <td class="item-total" style="text-align: right;">0,00 €</td>
                <td class="no-pdf"><button type="button" onclick="removeRow(this)" style="color: red;">✕</button></td>
            `;
            mixAddCell(newRow, mixDefaultRate());
            tbody.appendChild(newRow);
            attachCalculationListeners(newRow);
            calculateTotals();
            artMark();
        }
        
        // Add row from article catalog (only quantity editable)
        function addArticleRow(articleId) {
            const article = articlesData[articleId];
            if (!article) return;
            
            rowCounter++;
            const tbody = document.getElementById('invoice_items_body');
            const newRow = document.createElement('tr');
            newRow.className = 'invoice-item-row';
            newRow.setAttribute('data-row', rowCounter);
            newRow.setAttribute('data-article-id', articleId);

            // Das Gerüst enthält keine Artikeldaten; Einheit, Text und Preis kommen als Wert in
            // die Felder (in das Markup eingesetzt, bräche ein " im Namen aus dem Attribut)
            newRow.innerHTML = `
                <td>${rowCounter}</td>
                <td><input type="number" class="item-quantity" value="1" min="0" step="0.01" style="width: 60px;"></td>
                <td><input type="text" class="item-unit" style="width: 60px;"></td>
                <td><input type="text" class="item-description" style="width: 100%;"></td>
                <td><input type="number" class="item-price" value="0" min="0" step="0.01" style="width: 80px;"> €</td>
                <td class="item-total" style="text-align: right;">0,00 €</td>
                <td class="no-pdf"><button type="button" onclick="removeRow(this)" style="color: red;">✕</button></td>
            `;
            newRow.querySelector('.item-unit').value = article.unit;
            newRow.querySelector('.item-description').value =
                article.description ? article.name + ' - ' + article.description : article.name;
            newRow.querySelector('.item-price').value = article.price;
            mixAddCell(newRow, mixArticleRate(article));
            tbody.appendChild(newRow);
            attachCalculationListeners(newRow);
            calculateTotals();
            artMark();
        }
        
        // Remove row
        function removeRow(button) {
            const row = button.closest('tr');
            row.remove();
            renumberRows();
            calculateTotals();
            artMark();
        }
        
        // Renumber rows
        function renumberRows() {
            const rows = document.querySelectorAll('.invoice-item-row');
            rows.forEach((row, index) => {
                row.querySelector('td:first-child').textContent = index + 1;
                row.setAttribute('data-row', index + 1);
            });
            rowCounter = rows.length;
        }
        
        // Betrags-Anzeige/-Parsing im deutschen Format (Tausenderpunkt, Dezimalkomma).
        // Die Anzeigefelder werden von den Berechnungen zurückgelesen – daher immer
        // über fmtDe/parseDe, nie nur '.'/',' tauschen.
        function fmtDe(n) { return n.toLocaleString('de-DE', {minimumFractionDigits: 2, maximumFractionDigits: 2}); }
        function parseDe(t) { return parseFloat(String(t).replace(/<[^>]*>/g, '').replace('€', '').replace(/\\./g, '').replace(',', '.').trim()); }

        // Calculate row total
        function calculateRowTotal(row) {
            const quantity = parseFloat(row.querySelector('.item-quantity').value) || 0;
            const price = parseFloat(row.querySelector('.item-price').value) || 0;
            const total = quantity * price;
            row.querySelector('.item-total').textContent = fmtDe(total) + ' €';
        }
        
        // Calculate all totals
        function calculateTotals() {
            // Der Hinweis „vorbelegt ohne Umsatzsteuer“ gilt nur, solange das Häkchen aus ist
            const taxNote = document.getElementById('tax_prefill_note');
            if (taxNote) taxNote.hidden = document.getElementById('show_tax').checked;
            // Calculate sum of all items
            let sumNet = 0;
            document.querySelectorAll('.invoice-item-row').forEach(row => {
                calculateRowTotal(row);
                const totalText = row.querySelector('.item-total').textContent;
                const total = parseDe(totalText) || 0;
                sumNet += total;
            });
            
            // Update net sum
            document.getElementById('sum_net').textContent = fmtDe(sumNet) + ' €';
            document.getElementById('tax_base').textContent = fmtDe(sumNet);

            // USt nur ausweisen, wenn Checkbox aktiv (Kleinunternehmer §19: keine Zeile)
            const showTax = document.getElementById('show_tax').checked;
            const taxRow = document.getElementById('tax_row');
            if (taxRow) taxRow.style.display = showTax ? '' : 'none';

            // Calculate tax
            const taxRate = showTax ? (parseFloat(document.getElementById('tax_rate').value) || 0) : 0;
            const taxAmount = sumNet * (taxRate / 100);
            document.getElementById('tax_amount').textContent = fmtDe(taxAmount) + ' €';

            // Calculate gross sum
            const sumGross = sumNet + taxAmount;
            document.getElementById('sum_gross').innerHTML = '<strong>' + fmtDe(sumGross) + ' €</strong>';

            // Steuersatz je Position: eine Steuerzeile je Satz (ohne das Häkchen räumt der Aufruf nur auf)
            mixTotals();
        }
        
        // Attach listeners to row inputs
        function attachCalculationListeners(row) {
            row.querySelector('.item-quantity').addEventListener('input', calculateTotals);
            row.querySelector('.item-price').addEventListener('input', calculateTotals);
            mixAttach(row);
        }
        
        // Attach listeners to tax rate
        document.getElementById('tax_rate').addEventListener('input', calculateTotals);
        
        // Attach listeners to initial row
        document.querySelectorAll('.invoice-item-row').forEach(attachCalculationListeners);
        
        // Initial calculation
        calculateTotals();
        artMark();
        
        // Initialize own company (load logo and data for preselected company)
        updateOwnCompany();
        
        // Notification helpers
        function showMessage(text, type) {
            type = type || 'success';
            var bar = document.getElementById('invoice_msg');
            if (!bar) return;
            var styles = {
                success: 'background:#d4edda;color:#155724;border:1px solid #c3e6cb',
                error:   'background:#f8d7da;color:#721c24;border:1px solid #f5c6cb',
                warn:    'background:#fff3cd;color:#856404;border:1px solid #ffc107',
                info:    'background:#d1ecf1;color:#0c5460;border:1px solid #bee5eb'
            };
            bar.setAttribute('style', (styles[type] || styles.info)
                + ';display:block;padding:10px 16px;border-radius:4px;margin-bottom:10px;font-size:14px;');
            bar.textContent = text;
            if (type !== 'error') setTimeout(function() { bar.style.display = 'none'; }, 6000);
        }
        function showConfirm(text, onOk) { appConfirm(text, onOk); }
        
        // Save invoice to database
        function saveInvoice() {
            // Check if we're editing an existing invoice
            const invoiceId = document.getElementById('invoice_id').value;
            const isEdit = invoiceId !== '';

            // Versendete Rechnung: Betraege, Positionen, Nummer, Datum und Kunde sind gesperrt (der Server
            // lehnt jede Aenderung daran ab) - geschickt wird nur, was sich noch aendern laesst UND was der
            // Nutzer geaendert hat; die Anschrift nur auf ausdruecklichen Wunsch (Kaestchen). Das Speichern
            // ist hier ein Berichtigen: nach Rueckfrage, mit der Fassung, die diese Maske geladen hat
            if (isEdit && document.getElementById('invoice_locked').value === 'true') {
                const current = {
                    paymentTerms: document.getElementById('payment_terms').value,
                    orderNumber: document.getElementById('order_number').value || null,
                    buyerReference: document.getElementById('buyer_route_id').value || null,
                    deliveryDate: document.getElementById('delivery_date').value || null,
                    serviceDateEnd: document.getElementById('service_date_end').value || null
                };
                const changes = {invoiceId: parseInt(invoiceId),
                                 documentVersion: parseInt(document.getElementById('document_version').value)};
                Object.keys(current).forEach(function(key) {
                    if ((current[key] || '') !== (initialOpenFields[key] || '')) changes[key] = current[key];
                });
                const refresh = document.getElementById('refresh_address');
                if (refresh && refresh.checked) changes.refreshAddress = true;
                if (Object.keys(changes).length === 2) {         // nur die ID und die Fassung: nichts geändert
                    showMessage('Es wurde nichts geändert.', 'info');
                    return;
                }
                // Das Leistungsdatum ist Pflicht: geleert wird es nicht (ein leeres, unverändertes - Import - schon)
                if ('deliveryDate' in changes && !changes.deliveryDate) {
                    showMessage(__DELIVERY_DATE_MESSAGE__, 'error');
                    document.getElementById('delivery_date').focus();
                    return;
                }
                // Das Ende des Leistungszeitraums liegt nicht vor dem Leistungsdatum (ein Ende am Beginn ist kein Zeitraum); geprüft nur, wenn eines von beiden geändert ist
                if (('serviceDateEnd' in changes || 'deliveryDate' in changes) && current.serviceDateEnd && current.deliveryDate
                        && current.serviceDateEnd < current.deliveryDate) {
                    showMessage(__SERVICE_PERIOD_MESSAGE__, 'error');
                    document.getElementById('service_date_end').focus();
                    return;
                }
                showConfirm(__CORRECT_QUESTION__, function() { sendInvoice(changes, isEdit, invoiceId); });
                return;
            }

            // Get selected own company ID
            const ownCompanyId = document.getElementById('own_company_select').value;
            if (!ownCompanyId) {
                showMessage('Bitte wählen Sie eine eigene Firma aus.', 'error');
                return;
            }
            
            // Get selected customer
            const customerId = document.getElementById('customer_select').value;
            if (!customerId) {
                showMessage('Bitte wählen Sie einen Kunden aus.', 'error');
                return;
            }
            
            // Get invoice number
            const invoiceNumber = document.getElementById('invoice_number').value;
            if (!invoiceNumber) {
                showMessage('Bitte geben Sie eine Rechnungsnummer ein.', 'error');
                return;
            }
            
            // Get invoice date
            const invoiceDate = document.getElementById('invoice_date').value;
            if (!invoiceDate) {
                showMessage('Bitte wählen Sie ein Rechnungsdatum.', 'error');
                return;
            }
            
            if (!document.getElementById('delivery_date').value) {
                showMessage('Bitte geben Sie das Leistungsdatum an (Pflichtangabe nach § 14 UStG).', 'error');
                document.getElementById('delivery_date').focus();
                return;
            }
            // Das Ende des Leistungszeitraums liegt nicht vor dem Leistungsdatum (der Server prüft es ebenso)
            const serviceDateEnd = document.getElementById('service_date_end').value;
            if (serviceDateEnd && serviceDateEnd < document.getElementById('delivery_date').value) {
                showMessage(__SERVICE_PERIOD_MESSAGE__, 'error');
                document.getElementById('service_date_end').focus();
                return;
            }

            // Steuersatz NaN-sicher lesen (0% darf nicht zu 19% werden).
            // Kleinunternehmer (§19): keine USt -> effektiver Satz 0, Flag showTax=false.
            const showTax = document.getElementById('show_tax').checked;
            const _tr = parseFloat(document.getElementById('tax_rate').value);
            const taxRate = showTax ? (isNaN(_tr) ? 19 : _tr) : 0;

            // Steuersatz je Position: Jede Position schickt den Satz ihrer Auswahl, dazu kommt taxMode; ohne das Häkchen bleibt alles, wie es war
            const mixed = mixOn();

            // Collect items
            const items = [];
            document.querySelectorAll('.invoice-item-row').forEach((row, index) => {
                const description = row.querySelector('.item-description').value;
                if (description) {  // Only include rows with description
                    const item = {
                        position: index + 1,
                        quantity: parseFloat(row.querySelector('.item-quantity').value) || 1,
                        unit: row.querySelector('.item-unit').value,
                        description: description,
                        unitPrice: parseFloat(row.querySelector('.item-price').value) || 0,
                        totalPrice: parseDe(row.querySelector('.item-total').textContent) || 0,
                        taxRate: mixed ? mixRowRate(row) : taxRate
                    };
                    // Ein Steuersatz aus den Stammdaten, den der Satz allein nicht trägt, geht mit seiner ID mit (als letzter Schlüssel der Position)
                    if (mixed && mixRowEntry(row) !== null) item.taxRateId = mixRowEntry(row);
                    items.push(item);
                }
            });
            
            if (items.length === 0) {
                showMessage('Bitte fügen Sie mindestens eine Position hinzu.', 'error');
                return;
            }
            
            // Get bank account ID
            const bankAccountId = document.getElementById('bank_account_select').value || null;
            
            // Calculate amounts (replace comma with dot for German number format)
            const netAmount = parseDe(document.getElementById('sum_net').textContent);
            const taxAmount = parseDe(document.getElementById('tax_amount').textContent);
            const grossAmount = parseDe(document.getElementById('sum_gross').textContent);
            
            // Get payment terms
            const paymentTermDays = parseInt(document.getElementById('payment_term_days').value) || 14;
            const paymentTerms = document.getElementById('payment_terms').value;
            
            // Calculate due date
            const dueDateField = document.getElementById('payment_due_date').value;
            let dueDate = dueDateField;
            if (!dueDate && invoiceDate) {
                const d = new Date(invoiceDate);
                d.setDate(d.getDate() + paymentTermDays);
                dueDate = d.toISOString().split('T')[0];
            }
            
            // Collect invoice data
            const invoiceData = {
                invoiceNumber: invoiceNumber,
                // Die vorgeschlagene Nummer: bleibt sie unverändert, vergibt der Server beim Speichern frisch
                numberSuggestion: isEdit ? null : (document.getElementById('number_suggestion').value || null),
                invoiceDate: invoiceDate,
                customerId: parseInt(customerId),
                customerNumber: document.getElementById('customer_number').value,
                ownCompanyId: parseInt(ownCompanyId),
                buyerReference: document.getElementById('buyer_route_id').value || null,
                paymentTerms: paymentTerms,
                paymentTermsDays: paymentTermDays,
                dueDate: dueDate,
                bankAccountId: bankAccountId ? parseInt(bankAccountId) : null,
                netAmount: netAmount,
                taxRate: taxRate / 100,  // Convert percentage to decimal
                showTax: showTax,        // false = Kleinunternehmer (§19), keine USt-Zeile
                taxAmount: taxAmount,
                grossAmount: grossAmount,
                currency: 'EUR',
                items: items,
                // XRechnung optional fields
                orderNumber: document.getElementById('order_number').value || null,
                deliveryDate: document.getElementById('delivery_date').value || null,
                discountPercentage: parseFloat(document.getElementById('discount_percentage').value) || null,
                discountDays: parseInt(document.getElementById('discount_days').value) || null
            };
            
            // Leistungszeitraum und Zahlungsart gehen nur mit, wenn gesetzt (kein Ende bzw. Überweisung heißt: nichts schicken – der Server liest fehlend wie leer);
            // so bleibt der Payload ohne sie, wie er war
            if (serviceDateEnd) invoiceData.serviceDateEnd = serviceDateEnd;
            if (document.getElementById('payment_means').value) invoiceData.paymentMeans = document.getElementById('payment_means').value;

            if (mixed) invoiceData.taxMode = 'mixed';

            // Add invoice ID if editing
            if (isEdit) {
                invoiceData.invoiceId = parseInt(invoiceId);
            }
            // Ohne das Häkchen schickt der Kopf den Eintrag, den der Satz allein nicht trägt, als letzten Schlüssel (alle Positionen tragen ihn); ohne Steuerausweis nie
            if (!mixed && showTax && mixHeadEntry() !== null) invoiceData.taxRateId = mixHeadEntry();

            sendInvoice(invoiceData, isEdit, invoiceId);
        }

        // Send to server
        function sendInvoice(invoiceData, isEdit, invoiceId) {
            fetch('/invoice/save', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(invoiceData)
            })
            .then(response => response.json())
            .then(data => {
                if (data.success) {
                    // Stay in grid view with this invoice selected
                    const redirectId = isEdit ? invoiceId : String(data.invoice_id);
                    const target = '/invoice?id=' + redirectId;
                    // Berichtigt: die Maske kennt jetzt die neue Fassung (die Seite wird gleich neu geladen)
                    if (data.documentVersion) document.getElementById('document_version').value = data.documentVersion;
                    // Gespeichert, aber mit Hinweis (z. B. Nummernkreis): kurz anzeigen, dann weiter
                    if (data.warning) {
                        showMessage(data.warning, 'warn');
                        setTimeout(function() { window.location.href = target; }, 4000);
                    } else if (!isEdit && data.invoice_number && data.invoice_number !== invoiceData.invoiceNumber) {
                        // Der Vorschlag war inzwischen vergeben: die Maske zeigt die Nummer, die gilt
                        document.getElementById('invoice_number').value = data.invoice_number;
                        showMessage('Gespeichert mit der Rechnungsnummer ' + data.invoice_number
                                    + ' (der Vorschlag ' + invoiceData.invoiceNumber + ' war inzwischen vergeben).', 'info');
                        setTimeout(function() { window.location.href = target; }, 3500);
                    } else if (data.documentVersion) {
                        showMessage('Berichtigt: Fassung ' + data.documentVersion
                                    + '. Die neue Fassung muss dem Kunden erneut zugehen.', 'success');
                        setTimeout(function() { window.location.href = target; }, 2500);
                    } else {
                        window.location.href = target;
                    }
                } else {
                    showMessage('Fehler beim Speichern: ' + (data.error || 'Unbekannter Fehler'), 'error');
                }
            })
            .catch(err => {
                showMessage('Fehler beim Speichern: ' + err, 'error');
                console.error('Save error:', err);
            });
        }
        
        // PDF (und E-Rechnung) erzeugen und anschliessend echt herunterladen
        function generatePDF() {
            const invoiceId = document.getElementById('invoice_id').value;
            if (!invoiceId) {
                showMessage('Bitte speichern Sie die Rechnung zuerst, bevor Sie ein PDF erstellen.', 'warn');
                return;
            }
            const pdfExists = document.getElementById('pdf_exists').value === 'true';
            if (pdfExists) {
                showConfirm('PDF-Datei existiert bereits. Überschreiben und neu generieren?', function() {
                    _runPDFGeneration(invoiceId);
                });
            } else {
                _runPDFGeneration(invoiceId);
            }
        }
        function _runPDFGeneration(invoiceId) {
            fetch('/invoice/pdf_generate?id=' + invoiceId, {method: 'POST'})
                .then(response => response.json())
                .then(data => {
                    if (data.success) {
                        document.getElementById('pdf_exists').value = 'true';
                        showMessage('PDF erstellt: ' + data.filename
                                    + (data.warning ? ' (' + data.warning + ')' : ''), 'success');
                        downloadInvoiceFiles(invoiceId, !!data.xml_filename);
                    } else {
                        showMessage('Fehler beim Erstellen der PDF: ' + (data.error || 'Unbekannter Fehler'), 'error');
                    }
                })
                .catch(function(err) {
                    showMessage('Fehler: ' + err.message, 'error');
                });
        }
        // Echter Download der abgelegten Dateien (todo #1) - kein Server-Link,
        // sondern die Datei selbst.
        function downloadInvoiceFiles(invoiceId, withXml) {
            const notify = function(text, type) { showMessage(text, type); };
            appDownload('/invoice/pdf_download?id=' + invoiceId, notify);
            if (withXml) appDownload('/invoice/xml_download?id=' + invoiceId, notify);
        }
        // Herunterladen ohne Neuerzeugung (todo #3)
        function downloadPDF() {
            const invoiceId = document.getElementById('invoice_id').value;
            if (!invoiceId) {
                showMessage('Bitte speichern Sie die Rechnung zuerst.', 'warn');
                return;
            }
            if (document.getElementById('pdf_exists').value !== 'true') {
                showMessage('Kein PDF vorhanden - bitte zuerst erzeugen.', 'error');
                return;
            }
            downloadInvoiceFiles(invoiceId,
                document.getElementById('xml_exists')?.value === 'true');
        }
    </script>
    '''.replace('__CORRECT_QUESTION__', script_json(CORRECT_QUESTION)
                ).replace('__DELIVERY_DATE_MESSAGE__', script_json(DELIVERY_DATE_MESSAGE)
                ).replace('__SERVICE_PERIOD_MESSAGE__', script_json(SERVICE_PERIOD_MESSAGE))

    # Einer Stornorechnung lassen sich keine Zahlungen zuordnen: statt des Kastens ein Hinweis – solange das Guthaben des
    # Kunden aber offen ist oder es Erstattungen gibt, der Kasten „Erstattung“
    if is_storno:
        s += _refund_box_html(db, invoice_id, existing_invoice, existing_payments, storno_credit)

    # Payment history section (edit mode only)
    if is_edit_mode and invoice_id and not is_storno:
        amount_due = existing_invoice[39] if existing_invoice else 0
        skonto_of = db.get_invoice_skonto(invoice_id)          # das Skonto, das Zuordnungen dieser Rechnung ausgebucht haben: {ID der Zuordnung: Euro}
        s += f'''
    <div style="margin: 30px 0; padding: 20px; border: 1px solid #ddd; border-radius: 4px;">
        <h3>Zahlungsverknüpfungen</h3>
'''
        findings = db.find_payment_inconsistencies(invoice_id)
        if findings:
            s += ('        <div class="warnColor"><strong>Zahlungszuordnung prüfen:</strong><ul>\n'
                  + ''.join(f'            <li>{_html.escape(f["text"])}</li>\n' for f in findings)
                  + '        </ul>Alle Rechnungen mit Unstimmigkeiten zeigt die Prüfung unter '
                  '<a href="/miscellaneous?check_payments=1">Daten</a>.</div>\n')
            s += _rebuild_box_html(db, invoice_id, existing_invoice[35],
                                   mixed=has_mixed_rates(existing_invoice[InvoiceCol.MIXED_TAX_RATES]),
                                   entry=db.invoice_entry_accounts(invoice_id))
            rebuild_script = _REBUILD_SCRIPT.replace('__CONFIRM__', script_json(REBUILD_CONFIRM_QUESTION))
        if existing_payments:
            total_paid = sum(p[3] for p in existing_payments)
            s += '''        <table style="width:100%; border-collapse:collapse;">
            <tr style="background:#f5f5f5;">
                <th style="padding:6px 8px; text-align:left;">Datum</th>
                <th style="padding:6px 8px; text-align:left;">Buchung</th>
                <th style="padding:6px 8px; text-align:right;">Betrag</th>
                <th style="padding:6px 8px;"></th>
            </tr>
'''
            for p in existing_payments:
                pid, _, booking_id, amount, pay_date, notes, booking_ref = p
                # Eine Zuordnung, die mit Skonto geschlossen hat: unter dem Geld, das geflossen ist, der Betrag, der als Skonto ausgebucht ist
                skonto_note = f'<div class="hint">+ Skonto {fmt_eur(skonto_of[pid])}</div>' if pid in skonto_of else ''
                s += f'''            <tr>
                <td style="padding:6px 8px;">{_html.escape(str(pay_date or "–"))}</td>
                <td style="padding:6px 8px;">#{booking_id} {_html.escape(str(booking_ref or ""))}</td>
                <td style="padding:6px 8px; text-align:right;">{fmt_eur(amount)}{skonto_note}</td>
                <td style="padding:6px 8px; text-align:center;">
                    <a href="javascript:void(0);" onclick="deletePayment({pid})" style="color:#c00;">✕ Entfernen</a>
                </td>
            </tr>
'''
            s += f'''            <tr style="font-weight:bold; border-top:2px solid #ccc;">
                <td colspan="2" style="padding:6px 8px;">Gezahlt gesamt</td>
                <td style="padding:6px 8px; text-align:right;">{fmt_eur(total_paid)}</td>
                <td></td>
            </tr>
'''
            total_skonto = sum(skonto_of.values())
            if total_skonto > 0:
                s += f'''            <tr style="font-weight:bold;">
                <td colspan="2" style="padding:6px 8px;">Skonto</td>
                <td style="padding:6px 8px; text-align:right;">{fmt_eur(total_skonto)}</td>
                <td></td>
            </tr>
'''
            s += f'''            <tr style="font-weight:bold;">
                <td colspan="2" style="padding:6px 8px;">Noch offen</td>
                <td style="padding:6px 8px; text-align:right; color:{"#c00" if (amount_due or 0) > 0.01 else "#2a2"};">{fmt_eur(amount_due or 0)}</td>
                <td></td>
            </tr>
        </table>
'''
        else:
            s += '        <p><em>Keine Zahlungen verknüpft.</em></p>\n'

        # Zahlungs-Aktionen (todo #2): neue Zahlung verbuchen (vorbefüllte
        # Buchungsmaske) oder eine vorhandene Buchung mit freiem Rest zuordnen
        # (Import-Fall). "Bezahlt" ergibt sich automatisch aus der Zuordnung.
        is_invoice_doc = (len(existing_invoice) <= 45
                          or existing_invoice[45] != 'quote')
        # Zahlbar ist eine Rechnung erst ab „versendet“ (PAYABLE_STATUSES; 'partial' ist
        # schon auf 'partial_payment' abgebildet): Davor steht sie noch nicht in den Büchern.
        payable = (is_invoice_doc
                   and invoice_status in PAYABLE_STATUSES
                   and (amount_due is None or amount_due > 0))
        if is_invoice_doc and invoice_status in ('draft', 'finalized'):
            s += ('        <p class="hint">Zahlungen lassen sich erst zuordnen, wenn die '
                  'Rechnung versendet ist.</p>\n')
        if payable:
            open_due = (amount_due if amount_due is not None
                        else (existing_invoice[38] or 0))
            # Ist für den Steuersatz kein Erlöskonto hinterlegt (z. B. 5 %, 16 %), wählt der
            # Nutzer es hier – für „Verknüpfen“ und, per Adresse mitgegeben, für die Maske
            choice = revenue_coa_choice(db, existing_invoice[35],
                                        mixed=has_mixed_rates(existing_invoice[InvoiceCol.MIXED_TAX_RATES]),
                                        entry=db.invoice_entry_accounts(invoice_id))
            if choice:
                s += (f'        <p class="hint">{choice[1]}</p>\n'
                      f'        <p><label>Erlöskonto: {choice[0]}</label></p>\n')
            s += (f'        <p><a href="/transactions?from_invoice={invoice_id}" '
                  'class="coloredButton btn-sm bg-green" onclick="return bookPayment(this)">'
                  '💶 Zahlung verbuchen</a></p>\n')
            # Skonto (2.9.0): was die Rechnung vorsieht (mit der Frist), oder der feste Satz, warum sich keines ausbuchen lässt. Nur mit Skonto-Bedingungen steht hier etwas;
            # die Häkchen der Kandidaten gibt es nur, wo die Rechnung ein Skonto zulässt (``skonto_bookable``). Die Seite hängt nicht daran.
            try:
                skonto_html, skonto_bookable = _skonto_info_html(db.skonto_offer(invoice_id), existing_invoice[2], open_due, today_iso)
            except Exception:
                traceback.print_exc()               # der volle Fehler nur ins Server-Log
                skonto_html, skonto_bookable = '', False
            s += skonto_html
            linked_booking_ids = {p[2] for p in existing_payments}
            candidates = [c for c in
                          db.get_unallocated_bank_bookings(existing_invoice[13])
                          if c[0] not in linked_booking_ids]
            # Die Bewegungen, die laut Rechnungsnummer im Verwendungszweck, Betrag und Kunde zu dieser Rechnung passen, stehen zuerst (in der Rangfolge
            # von ``rank_movements``), danach die übrigen wie bisher; die Begründung steht klein unter dem Auftraggeber. Der Haken „✓“ bei
            # gleichem Betrag bleibt. Es wird nur vorgeschlagen – zugeordnet wird mit „Verknüpfen“.
            try:
                suggested = [(pair, movement) for pair, movement in db.suggest_bookings_for_invoice(invoice_id)
                             if movement['id'] not in linked_booking_ids]
            except Exception:                       # die Rechnungsseite hängt nicht an den Vorschlägen
                traceback.print_exc()               # der volle Fehler nur ins Server-Log
                suggested = []
            reasons = {movement['id']: reason_text(pair) for pair, movement in suggested}
            candidates = ([(m['id'], m['date'], m['recipient'], m['contact_id'], from_minor(m['total_minor']), from_minor(m['amount_minor']))
                           for _pair, m in suggested]
                          + [c for c in candidates if c[0] not in reasons])
            if candidates:
                s += '''        <h4>Vorhandene Buchung verknüpfen</h4>
        <table style="width:100%; border-collapse:collapse;">
            <tr style="background:#f5f5f5;">
                <th style="padding:6px 8px; text-align:left;">Datum</th>
                <th style="padding:6px 8px; text-align:left;">Empfänger/Auftragg.</th>
                <th style="padding:6px 8px; text-align:right;">Betrag</th>
                <th style="padding:6px 8px; text-align:right;">Frei</th>
                <th style="padding:6px 8px; text-align:right;">Zuordnen</th>
                <th style="padding:6px 8px;"></th>
            </tr>
'''
                for c in candidates:
                    b_id, b_date, b_recipient, _, b_amount, b_free = c
                    default_alloc = min(open_due, b_free)
                    match_mark = ' ✓' if b_free == open_due else ''
                    reason_html = f'<div class="hint">{_html.escape(reasons[b_id])}</div>' if b_id in reasons else ''
                    # Das Häkchen „Rest … als Skonto ausbuchen“ (versteckt, nie angehakt): das Skript zeigt es, wenn der Rest nach dem eingegebenen Betrag ins Skonto passt
                    skonto_row = oninput = ''
                    if skonto_bookable:
                        skonto_row = skonto_check_html(f'paySkontoRow_{int(b_id)}', f'paySkonto_{int(b_id)}', f'paySkontoRest_{int(b_id)}',
                                                       data=f' data-free="{format_plain(round_minor(to_minor(b_free), 2))}"')
                        oninput = f' oninput="skoInvoiceRow({int(b_id)})"'
                    s += f'''            <tr>
                <td style="padding:6px 8px;">{_html.escape(str(b_date or "–"))}</td>
                <td style="padding:6px 8px;">{_html.escape(str(b_recipient or ""))}{reason_html}</td>
                <td style="padding:6px 8px; text-align:right;">{fmt_eur(b_amount)}</td>
                <td style="padding:6px 8px; text-align:right;">{fmt_eur(b_free)}{match_mark}</td>
                <td style="padding:6px 8px; text-align:right;"><input type="number" step="0.01" class="noButtons" id="payAmount_{b_id}" value="{default_alloc:.2f}" style="width:90px; text-align:right;"{oninput}>{skonto_row}</td>
                <td style="padding:6px 8px;"><button onclick="linkPayment({b_id}, this)" class="coloredButton btn-sm bg-blue">Verknüpfen</button></td>
            </tr>
'''
                s += '        </table>\n'
                if skonto_bookable:
                    s += ('        <script>var SKO_ROWS = ' + script_json([int(c[0]) for c in candidates]) + ';\n'
                          + SKONTO_JS + SKONTO_INVOICE_JS + 'skoInvoiceInit();\n        </script>\n')

        s += f'''    </div>
    <script>
        // Das gewählte Erlöskonto ('' = es gibt kein Feld: die Kontierung ergibt sich aus der
        // Rechnung; null = Feld ohne Wahl, der Nutzer wurde darauf hingewiesen)
        function revenueCoaChoice() {{
            const field = document.getElementById('revenueCoa');
            if (!field) return '';
            if (!field.value) {{
                showMessage('Bitte ein Erlöskonto wählen.', 'error');
                return null;
            }}
            return field.value;
        }}
        function bookPayment(link) {{
            const coa = revenueCoaChoice();
            if (coa === null) return false;
            link.href = '/transactions?from_invoice={invoice_id}'
                + (coa ? '&revenue_coa_id=' + encodeURIComponent(coa) : '');
            return true;
        }}
        function linkPayment(bookingId, button) {{
            const amount = parseFloat(document.getElementById('payAmount_' + bookingId).value);
            if (!amount || amount <= 0) {{
                showMessage('Betrag muss größer 0 sein.', 'error');
                return;
            }}
            const coa = revenueCoaChoice();
            if (coa === null) return;
            const payload = {{invoice_id: {invoice_id}, transaction_id: bookingId,
                             amount_paid: amount}};
            if (coa) payload.revenue_coa_id = parseInt(coa, 10);
            const skontoBox = document.getElementById('paySkonto_' + bookingId);
            if (skontoBox && skontoBox.checked) payload.skonto = true;   // nur mit dem Haken, nie von sich aus
            button.disabled = true;   // ein Doppelklick ordnet nicht doppelt zu
            fetch('/invoice/link-payment', {{
                method: 'POST',
                headers: {{'Content-Type': 'application/json'}},
                body: JSON.stringify(payload)
            }})
            .then(r => r.json())
            .then(data => {{
                if (data.success) {{ location.reload(); }}
                else {{
                    button.disabled = false;
                    showMessage('Fehler: ' + (data.error || 'Unbekannt'), 'error');
                }}
            }})
            .catch(() => {{
                button.disabled = false;
                showMessage('Fehler: Die Zahlung konnte nicht verknüpft werden.', 'error');
            }});
        }}
        function deletePayment(paymentId) {{
            showConfirm('Zahlungsverkn\u00fcpfung wirklich entfernen? Die von der App daf\u00fcr angelegten Buchungss\u00e4tze werden mit entfernt.', function() {{
                fetch('/invoice/delete-payment', {{
                    method: 'POST',
                    headers: {{'Content-Type': 'application/json'}},
                    body: JSON.stringify({{payment_id: paymentId}})
                }})
                .then(r => r.json())
                .then(data => {{
                    if (data.success) {{ location.reload(); }}
                    else {{ showMessage('Fehler: ' + (data.error || 'Unbekannt'), 'error'); }}
                }});
            }});
        }}
    </script>
'''

    # Steuersatz-Feld, USt-Häkchen und Vorbelegungshinweis stehen an ihrer Stelle im Formular (oben, beim Aufbau): Eine Ersetzung von Platzhaltern über die fertige
    # Seite träfe auch jeden Wert aus der Datenbank, der einen Platzhaltertext enthält (eine Bezeichnung, einen Kunden, einen Artikel).
    # Der Kasten steht voller Daten aus der Datenbank: Er kommt erst am Ende dazu – ebenso das
    # Skript zum Stornieren, das die Rechnungsnummer in seiner Rückfrage trägt
    if is_edit_mode and invoice_id:
        s += _versions_box_html(db, invoice_id)
        if cancel_script:
            s += '<script>\n' + cancel_script + '</script>\n'
        if rebuild_script:
            s += '<script>\n' + rebuild_script + '</script>\n'
    return s


# ── Fassungen und Protokoll ──────────────────────────────────────────────────

_REASON_LABELS = {'corrected': 'berichtigt', 'reopened': 'zurückgeholt'}
_SENT_UNTIL = re.compile(r'versendet bis (\d{4})-(\d{2})-(\d{2})')


def _german_date(day):
    return day.strftime('%d.%m.%Y') if day else '–'


def _download_link(invoice_id, year, filename, label):
    """Link, der eine abgelegte Fassung über ``appDownload`` lädt (``/invoice/version_download``). Er trägt den
    Jahresordner: Dieselbe Datei kann in zwei Jahresordnern liegen, der Name allein träfe den ersten."""
    url = (f'/invoice/version_download?id={int(invoice_id)}&file={quote(filename, safe="")}'
           f'&year={quote(year, safe="")}')
    return (f'<a href="javascript:void(0);" onclick="{_html.escape("appDownload(" + script_json(url) + ")")}">'
            f'{_html.escape(label)}</a>')


def _version_rows(db, invoice_id):
    """Die abgelegten Fassungen als Zeilen ``(Fassung, versendet bis, Anlass, [(Art, Jahresordner, Dateiname,
    vorhanden)])``: die in ``InvoiceDocumentVersions`` festgehaltenen (der Jahresordner steht im ``RelPath``), dazu
    die Dateien in ``Fassungen/<ID>``, zu denen es keine Tabellenzeile gibt (Altbestand aus 2.1.2) – die erscheinen
    ohne Fassungsnummer, mit dem Jahresordner, in dem sie liegen. „Vorhanden“ heißt: im Jahresordner der Zeile."""
    present = {(year, name) for name, _path, year in version_files(invoice_id)}
    grouped, known = {}, set()
    for version, kind, rel_path, _sha, archived_at, reason in db.get_document_versions(invoice_id):
        year, filename = rel_path.split('/', 1)[0], rel_path.rsplit('/', 1)[-1]
        known.add((year, filename))
        row = grouped.setdefault((version, archived_at, reason), [])
        row.append((kind.upper(), year, filename, (year, filename) in present))
    rows = [(version, _german_date(local_date(archived_at)), _REASON_LABELS.get(reason, reason or '–'), files)
            for (version, archived_at, reason), files in sorted(
                grouped.items(), key=lambda kv: (kv[0][0], str(kv[0][1] or '')))]
    loose = {}
    for year, filename in sorted(present - known):
        stem, extension = filename.rsplit('.', 1) if '.' in filename else (filename, '')
        loose.setdefault((year, stem), []).append((extension.upper(), year, filename, True))
    for (_year, stem), files in loose.items():
        found = _SENT_UNTIL.search(stem)
        until = '.'.join(reversed(found.groups())) if found else '–'
        rows.append(('–', until, '–', files))
    return rows


def _protocol_value(field, value):
    if value is None or value == '':
        return '–'
    return STATUS_LABELS.get(value, value) if field == 'Status' else value


def _versions_box_html(db, invoice_id):
    """Kasten „Fassungen und Protokoll“ auf der Rechnungsseite: die abgelegten Fassungen (mit Download je
    Datei) und das Protokoll der Berichtigungen und Statuswechsel. Leer, wenn es nichts gibt. Jeder Wert aus
    der Datenbank ist maskiert, Dateinamen stehen prozentkodiert in der Adresse."""
    versions = _version_rows(db, invoice_id)
    protocol = db.get_invoice_protocol(invoice_id)
    if not versions and not protocol:
        return ''
    h = '    <div class="rectRounded no-pdf" id="invoice_versions">\n        <h3>Fassungen und Protokoll</h3>\n'
    if versions:
        h += ('        <h4>Abgelegte Fassungen</h4>\n        <table class="versionTable">\n'
              '            <tr><th>Fassung</th><th>Versendet bis</th><th>Anlass</th><th>Dateien</th></tr>\n')
        for number, until, reason, files in versions:
            links = ' '.join(_download_link(invoice_id, year, name, kind) if present
                             else f'<span class="muted" title="Datei nicht mehr vorhanden">{_html.escape(kind)}</span>'
                             for kind, year, name, present in files)
            h += (f'            <tr><td>{_html.escape(str(number))}</td><td>{_html.escape(until)}</td>'
                  f'<td>{_html.escape(reason)}</td><td>{links}</td></tr>\n')
        h += '        </table>\n'
    if protocol:
        h += ('        <h4>Protokoll</h4>\n        <table class="versionTable">\n'
              '            <tr><th>Datum</th><th>Benutzer</th><th>Feld</th><th>Alt &rarr; Neu</th></tr>\n')
        for changed_at, user, _action, field, old, new, _version in protocol:
            moment = local_datetime(changed_at)
            when = moment.strftime('%d.%m.%Y %H:%M') if moment else str(changed_at or '–')
            change = f'{_protocol_value(field, old)} \u2192 {_protocol_value(field, new)}'
            h += (f'            <tr><td>{_html.escape(when)}</td><td>{_html.escape(user or "–")}</td>'
                  f'<td>{_html.escape(PROTOCOL_LABELS.get(field, field or "–"))}</td>'
                  f'<td>{_html.escape(change)}</td></tr>\n')
        h += '        </table>\n'
    return h + '    </div>\n'
