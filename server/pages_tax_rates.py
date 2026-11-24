# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Stammdaten: Steuersätze (2.8.0) – die Liste links, das Formular rechts (``GET /masterdata/taxrates``, ``/masterdata/taxrates/edit?id=``).

Die Seite zeigt und pflegt die Einträge von ``TaxRates`` (``db/tax_rates.py``): Bezeichnung, Satz, Art (Standard oder steuerfrei mit dem Grund der Befreiung), Erlöskonto,
freiwillig Umsatzsteuer- und Vorsteuerkonto, Reihenfolge und ob der Eintrag wählbar ist. Das Wartekonto eines Satzes wird nur angezeigt – es ist keine Eingabe: Die App kennt
es für ganze Sätze (``OPEN_REVENUE_ACCOUNTS``), für jeden anderen bucht sie erst mit der Zahlung. Gespeichert wird über die Formular-Routen (``handlers.handle_add_tax_rate`` und
``handle_update_tax_rate``), Umschalten und Löschen sind Aktionen per POST (``/masterdata/taxrates/toggle`` und ``/delete``). Was die Seite nur liest, schreibt nichts.

Jeder Wert aus der Datenbank steht maskiert da; die Meldung einer abgelehnten Aktion (``?err=``) kommt als JSON in einem Skriptblock und erscheint nur über ``appMsg``.
Kein Satz steht fest im Text: Die Sätze mit festen Steuerkonten und die Vorbelegung der Auswahl kommen aus den Tabellen der App.
"""
import html as _html
from urllib.parse import quote

from db import Database
from db.tax_rates import FIXED_TAX_BPS
from tax_groups import EXEMPT, MAX_LABEL_LENGTH, MAX_REASON_LENGTH, MIXED_TAX_RATES, STANDARD, bp_pct, pct_text
from .pages import Header1, Header2, Header3, Footer, script_json


def _percent_list(percents):
    """Sätze als Text für einen Satz: ``19, 16, 7 und 5 %`` (``percents`` absteigend, als Texte ohne Prozentzeichen)."""
    return (', '.join(percents[:-1]) + ' und ' if len(percents) > 1 else '') + percents[-1] + ' %'


#: Der feste Hinweis der Seite: keine Steuerberatung, und was eine Änderung bewirkt
RATES_HINT = ('Welcher Satz und welcher Befreiungsgrund gelten, klärt bitte das Steuerbüro. Änderungen wirken auf neue und neu gespeicherte Entwürfe – '
              'versendete Rechnungen bleiben, wie sie sind.')
#: Der Hinweis zu den Steuerkonten; die Sätze mit festen Konten kommen aus der Tabelle der Summen- und Saldenliste (``db.tax_rates.FIXED_TAX_BPS``)
ACCOUNTS_HINT = ('Ohne Steuerkonto steht die Steuer dieses Satzes in der Summen- und Saldenliste unter ‚Steuer ohne Steuerkonto‘ und in der EÜR unter '
                 f'‚Vorsteuer ohne Steuerkonto‘. Für {_percent_list([pct_text(bp_pct(bp)) for bp in sorted(FIXED_TAX_BPS, reverse=True)])} stehen die Steuerkonten fest.')
WAITING_NONE = 'keines – gebucht wird erst mit der Zahlung'
WAITING_HELP = 'Auf diesem Konto steht die Forderung einer Rechnung bis zur Zahlung. Nur eine Anzeige: Der Eintrag legt kein Wartekonto fest.'
INVALID_HELP = 'Dieser Eintrag erfüllt die Regeln nicht (etwa nach einer Änderung über die SQL-Konsole) und steht in keiner Auswahl. Er lässt sich korrigieren oder löschen.'
DELETE_QUESTION = 'Steuersatz wirklich löschen?'
REASON_SHOWN = 60                       # so viele Zeichen des Befreiungsgrunds zeigt die Liste, der ganze Text steht im Tooltip
KIND_LABELS = ((STANDARD, 'Standard'), (EXEMPT, 'Steuerfrei'))


def _esc(value):
    """Wert aus Datenbank oder Anfrage für HTML-Text und -Attribute maskieren (NULL = leer)."""
    return _html.escape(str(value if value is not None else ''), quote=True)


def _rate_text(bp):
    """Der Satz eines Eintrags für die Liste (maskiert): ``5,5 %`` mit geschütztem Leerzeichen, damit er in einer schmalen Spalte nicht mitten im Satz umbricht; was kein
    Satz ist (eine ungültige Zeile), steht, wie die Datenbank es liefert."""
    return f'{pct_text(bp_pct(bp))}\u00a0%' if type(bp) is int else _esc(bp)


def _account_text(accounts, account):
    """``Nummer Name`` des Kontos (maskiert), ``–`` ohne Konto; ein Konto, das der Kontenrahmen nicht führt, heißt ``Konto <ID>``."""
    if account is None:
        return '–'
    found = accounts.get(account)
    return _esc(f'{found[0]} {found[1]}') if found else _esc(f'Konto {account} (nicht im Kontenrahmen)')


def _options(choices, selected, accounts=None):
    """``<option>``-Zeilen aus ``(Wert, Beschriftung)`` – ``selected`` ist vorgewählt. Ein gespeichertes Konto, das nicht zur Auswahl steht (eine Zeile, die die Regeln
    nicht mehr erfüllt), kommt als eigene, gewählte Option dazu: Es verschwindet nicht still, das Speichern prüft es und lehnt mit dem Satz der Regel ab."""
    rows = list(choices)
    if accounts is not None and type(selected) is int and str(selected) not in [str(value) for value, _label in rows]:
        found = accounts.get(selected)
        rows.append((selected, f'{found[0]} {found[1]}' if found else f'Konto {selected} (nicht im Kontenrahmen)'))
    chosen = '' if selected is None else str(selected)                         # kein Konto: die leere Option
    return ''.join(f'<option value="{_esc(value)}"{" selected" if str(value) == chosen else ""}>{_esc(label)}</option>' for value, label in rows)


def _status_badges(entry):
    badges = ''
    if not entry.valid:
        badges += f' <span class="badge bg-red" title="{_esc(INVALID_HELP)}">ungültig</span>'
    if not entry.active:
        badges += ' <span class="badge bg-gray">inaktiv</span>'
    return badges


def _reason_cell(entry):
    if entry.kind != EXEMPT or not entry.reason:
        return '–'
    text = str(entry.reason)
    shown = text if len(text) <= REASON_SHOWN else text[:REASON_SHOWN - 1] + '…'
    return f'<span title="{_esc(text)}">{_esc(shown)}</span>'


def _kind_text(kind):
    return _esc(dict(KIND_LABELS).get(kind, kind))


def _waiting_cell(db, entry):
    """Das Wartekonto zum Satz des Eintrags – nur die Nummer –, ``keines …`` ohne; bei einer ungültigen Zeile ``–``."""
    if not entry.valid:
        return '–'
    found = db.tax_rate_waiting_account(entry.bp)
    return _esc(found[0]) if found else _esc(WAITING_NONE)


def _row_html(db, entry, accounts):
    # Die ID stammt aus der Datenbank (INTEGER PRIMARY KEY) und steht in Adressen innerhalb des onclick prozentkodiert
    url_id = quote(str(entry.id), safe='')
    toggle_title = 'Abschalten (nicht mehr wählbar)' if entry.active else 'Einschalten (wählbar)'
    toggle_icon = '&#9745;' if entry.active else '&#9744;'
    revenue = _account_text(accounts, entry.revenue_coa_id) if entry.revenue_coa_id is not None else '<span class="muted">Standard</span>'
    return (f"<tr><td>{_esc(entry.label)}{_status_badges(entry)}</td><td>{_rate_text(entry.bp)}</td><td>{_kind_text(entry.kind)}</td>"
            f"<td>{_reason_cell(entry)}</td><td>{revenue}</td>"
            f"<td>{_account_text(accounts, entry.vat_coa_id)}</td><td>{_account_text(accounts, entry.input_tax_coa_id)}</td>"
            f"<td title=\"{_esc(WAITING_HELP)}\">{_waiting_cell(db, entry)}</td>"
            f"<td><a href='javascript:void(0)' onclick=\"openEditForm('/masterdata/taxrates/edit?id={url_id}')\" class='action-icon' title='Bearbeiten'>&#9998;</a>"
            f"&nbsp;<a href='javascript:void(0)' onclick=\"appPost('/masterdata/taxrates/toggle?id={url_id}')\" class='action-icon' title='{toggle_title}'>{toggle_icon}</a>"
            f"&nbsp;<a href='javascript:void(0)' class='action-icon delete-icon' title='Löschen'"
            f" onclick=\"appConfirmPost('/masterdata/taxrates/delete?id={url_id}', '{DELETE_QUESTION}')\">&#128465;</a></td></tr>")


def PageTaxRates(db: Database, edit_id=None, err=None):
    """Steuersätze (Stammdaten): die Liste links, das Formular rechts.

    ``edit_id``: lädt den Eintrag ins Formular; ohne oder mit einer ID, die es nicht gibt, ist es das leere Formular „Neuer Steuersatz“ (Reihenfolge: zehn hinter dem
    größten Wert). ``err``: Klartext, warum eine Aktion abgelehnt wurde – er kommt aus der Adresse und erscheint nur als Meldung am Seitenrand (``appMsg``)."""
    entries = db.fetch_tax_rates()
    edit = db.get_tax_rate(edit_id) if edit_id is not None else None
    accounts = {row[0]: (row[2], row[3]) for row in db.fetch_chart_of_accounts()}
    # ohne die Skonto-Konten (``payment_revenue_choices``): Ein Satz, der eines als Erlöskonto trüge, buchte seinen Erlös auf eine Erlösschmälerung. Ein gespeicherter Eintrag, der eines trägt,
    # bleibt gültig; ``_options`` zeigt sein Konto als eigene, gewählte Option
    revenue_choices = [(coa, f'{number} {name}') for coa, number, name in db.payment_revenue_choices()]
    tax_choices = db.get_tax_account_choices()
    default_revenue = ' und '.join(f'{pct_text(rate)} %' for rate in MIXED_TAX_RATES)

    s = Header1('masterdata')
    s += Header2('<a href="/masterdata">Stammdaten</a> → <span id="ActivePage">🧾 Steuersätze</span>')
    s += Header3()

    if edit:
        title = 'Steuersatz bearbeiten'
        label = _esc(edit.label)
        rate = _esc(pct_text(bp_pct(edit.bp))) if type(edit.bp) is int else ''
        kind, reason = edit.kind, _esc(edit.reason if edit.kind == EXEMPT else '')
        revenue, vat, input_tax = edit.revenue_coa_id, edit.vat_coa_id, edit.input_tax_coa_id
        sort_order, active = edit.sort_order, edit.active
        id_row = f'<input type="hidden" name="id" value="{_esc(edit.id)}">'
        buttons = ('<input type="submit" value="Aktualisieren" class="coloredButton btn-sm bg-green">'
                   '<button type="button" onclick="window.location.href=\'/masterdata/taxrates\'" class="coloredButton btn-sm bg-gray">← Abbrechen</button>')
    else:
        title = 'Neuer Steuersatz'
        label = rate = reason = ''
        kind, revenue, vat, input_tax = STANDARD, None, None, None
        sort_order, active = max([e.sort_order for e in entries], default=0) + 10, True
        id_row = ''
        buttons = ('<input type="submit" value="Steuersatz hinzufügen" formaction="/masterdata/taxrates/add" class="coloredButton btn-sm bg-green">')

    kind_options = ''.join(f'<option value="{value}"{" selected" if value == kind else ""}>{text}</option>' for value, text in KIND_LABELS)
    revenue_options = _options([('', f'Standard (nur {default_revenue})'), *revenue_choices], revenue, accounts)
    vat_options = _options([('', '– keines –'), *[(coa, f'{number} {name}') for coa, number, name in tax_choices['vat']]], vat, accounts)
    input_options = _options([('', '– keines –'), *[(coa, f'{number} {name}') for coa, number, name in tax_choices['input']]], input_tax, accounts)

    s += f'''
    <div class="grid2Cols gridMain">
    <div class="gridLeftCol">
        <p class="hint">{_esc(RATES_HINT)}</p>
        <p class="hint">{_esc(ACCOUNTS_HINT)}</p>
        <table>
            <tr><th>Bezeichnung</th><th>Satz</th><th>Art</th><th>Befreiungsgrund</th><th>Erlöskonto</th><th>USt-Konto</th><th>VSt-Konto</th><th>Wartekonto</th><th>Aktionen</th></tr>
            {''.join(_row_html(db, entry, accounts) for entry in entries)}
        </table>
    </div><!-- Ende gridLeftCol -->
    <div class="gridRightCol">
        <div class="rectRounded">
        <h2>{title}</h2>
        <form method="POST" action="/masterdata/taxrates/update">
            {id_row}
            <input type="hidden" name="active_field" value="1">
            <table class="form-table">
                <tr><td>Bezeichnung:*</td><td><input type="text" name="label" value="{label}" maxlength="{MAX_LABEL_LENGTH}" required></td></tr>
                <tr><td>Steuersatz (%):*</td><td><input type="text" name="rate" value="{rate}" inputmode="decimal" required placeholder="z. B. 5,5"></td></tr>
                <tr><td>Art:*</td><td><select name="kind">{kind_options}</select></td></tr>
                <tr><td>Grund der Steuerbefreiung:</td><td><input type="text" name="reason" value="{reason}" maxlength="{MAX_REASON_LENGTH}">
                    <small class="hint">Nur bei „Steuerfrei“, dort Pflicht: Der Grund steht auf der Rechnung.</small></td></tr>
                <tr><td>Erlöskonto:</td><td><select name="revenue_coa_id">{revenue_options}</select>
                    <small class="hint">Für jeden Satz außer 19 % und 7 % und für jede steuerfreie Leistung Pflicht.</small></td></tr>
                <tr><td>Umsatzsteuerkonto:</td><td><select name="vat_coa_id">{vat_options}</select>
                    <small class="hint">Freiwillig, für die Summen- und Saldenliste. Nicht bei 0 %.</small></td></tr>
                <tr><td>Vorsteuerkonto:</td><td><select name="input_tax_coa_id">{input_options}</select>
                    <small class="hint">Freiwillig, für die Summen- und Saldenliste und die EÜR. Nicht bei 0 %.</small></td></tr>
                <tr><td>Reihenfolge:</td><td><input type="number" name="sort_order" value="{_esc(sort_order)}" min="0" max="9999"></td></tr>
                <tr><td>Wählbar:</td><td><input type="checkbox" name="active" value="1"{" checked" if active else ""}></td></tr>
                <tr><td></td><td>{buttons}</td></tr>
            </table>
        </form>
        </div>
    </div><!-- Ende gridRightCol -->
    </div><!-- Ende grid2Cols -->
    <script>
        function openEditForm(url) {{
            fetch(url)
                .then(r => {{
                    if (!r.ok) throw new Error('status ' + r.status);
                    return r.text();
                }})
                .then(html => {{
                    const doc = new DOMParser().parseFromString(html, 'text/html');
                    const newForm = doc.querySelector('.gridRightCol');
                    const curForm = document.querySelector('.gridRightCol');
                    if (newForm && curForm) {{
                        curForm.innerHTML = newForm.innerHTML;
                        history.pushState({{}}, '', url);
                    }} else {{
                        window.location.href = url;
                    }}
                }})
                .catch(() => {{ window.location.href = url; }});
        }}
    </script>
    '''
    if err:
        s += ('<script>document.addEventListener("DOMContentLoaded",function(){'
              'appMsg(' + script_json(err) + ',"error");});</script>')
    s += Footer()
    return s
