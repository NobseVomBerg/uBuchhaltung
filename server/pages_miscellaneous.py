# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""
Sonstiges page – database overview, SQL console, misc tools.
"""
import html as _html
import re
from datetime import datetime as _datetime
from urllib.parse import quote as _quote
import demo as _demo
from db import Database
from db.wiso_fdb_import import MAX_ADOPTED_ROWS, MAX_LIQUID_SUBACCOUNTS
from money import fmt_eur
from . import wiso_upload as _wiso_upload
from .period import parse_iso_date


def Header1(active_page=None):
    from server.pages import Header1 as _H
    return _H(active_page)

def Header2(content=""):
    from server.pages import Header2 as _H
    return _H(content)

def Header3(content=""):
    from server.pages import Header3 as _H
    return _H(content)

def Footer():
    from server.pages import Footer as _F
    return _F()


#: Längste Meldung (``?msg=``), die die Seite anzeigt – die längste Meldung des Programms (der Hinweis des DATEV-Exports mit allen
#: drei Hinweisen) hat gut 1500 Zeichen; was darüber liegt, kann nur eine von Hand gebaute Adresse sein.
MSG_MAX_LENGTH = 2000


def _size_label(size):
    """Dateigröße für die Liste der Sicherungen: KB, MB oder GB mit Dezimalkomma (unter 1 MB mindestens 1 KB)."""
    if size < 1024 ** 2:
        return f'{max(1, round(size / 1024))} KB'
    if size < 1024 ** 3:
        return f'{size / 1024 ** 2:.1f} MB'.replace('.', ',')
    return f'{size / 1024 ** 3:.1f} GB'.replace('.', ',')


def _date_label(timestamp):
    """Zeitpunkt einer Datei als ``TT.MM.JJJJ HH:MM`` in Ortszeit; ``–``, wo sich der Zeitstempel nicht darstellen lässt
    (eine Datei mit absurdem Datum darf die Seite nicht kosten)."""
    try:
        return _datetime.fromtimestamp(timestamp).strftime('%d.%m.%Y %H:%M')
    except (OverflowError, OSError, ValueError):
        return '–'


def _fdb_result_rows(rows):
    """Zeilen einer gespeicherten Ergebnisliste (``adopted_rows``, ``possible_duplicates``).

    Die Ergebnisdatei liegt im Datenordner und kann von Hand verändert sein: Was keine
    Liste ist, gilt als leer, Einträge, die kein Objekt sind, übergeht die Seite –
    statt mit einem Fehler abzubrechen (samt SQL-Konsole).
    """
    if not isinstance(rows, list):
        return []
    return [row for row in rows if isinstance(row, dict)]


def _fdb_rows_table(rows):
    """Datum, Betrag, Text und Buchungs-ID der Zeilen aus :func:`_fdb_result_rows` als Tabelle."""
    s = '<table><tr><th>Datum</th><th>Betrag</th><th>Text</th><th>Buchungs-ID</th></tr>'
    for row in rows:
        try:
            amount = fmt_eur(row.get('amount'))
        except Exception:                       # Ergebnisdatei von Hand verändert
            amount = str(row.get('amount'))
        s += (f'<tr><td>{_html.escape(str(row.get("date") or ""))}</td>'
              f'<td>{_html.escape(amount)}</td>'
              f'<td>{_html.escape(str(row.get("text") or ""))}</td>'
              f'<td>{_html.escape(str(row.get("id") or ""))}</td></tr>')
    return s + '</table>'


def _result_count(value):
    """Zähler aus einer gespeicherten Ergebnisdatei: eine ganze Zahl ab 1, sonst 0 (die Datei
    liegt im Datenordner und kann von Hand verändert sein)."""
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def _orphaned_rows_table(rows):
    """Die verwaisten Zeilen eines WISO-Imports (``orphaned_rows``: ``id``, ``date``, ``amount``, ``account``,
    ``counter_account``, ``text``) als Tabelle; die Buchungs-ID führt zur Buchung. Alles maskiert: Die ID wird nur
    dann ein Link, wenn sie eine ganze Zahl ist."""
    s = ('<table><tr><th>Datum</th><th>Betrag</th><th>Konto</th><th>Gegenkonto</th><th>Text</th>'
         '<th>Buchungs-ID</th></tr>')
    for row in rows:
        try:
            amount = fmt_eur(row.get('amount'))
        except Exception:                       # Ergebnisdatei von Hand verändert
            amount = str(row.get('amount'))
        row_id = row.get('id')
        if isinstance(row_id, int) and not isinstance(row_id, bool):
            id_cell = f'<a href="/transactions/edit?id={row_id}">{row_id}</a>'
        else:
            id_cell = _html.escape(str(row_id or ''))
        s += (f'<tr><td>{_html.escape(str(row.get("date") or ""))}</td>'
              f'<td>{_html.escape(amount)}</td>'
              f'<td>{_html.escape(str(row.get("account") if row.get("account") is not None else ""))}</td>'
              f'<td>{_html.escape(str(row.get("counter_account") if row.get("counter_account") is not None else ""))}</td>'
              f'<td>{_html.escape(str(row.get("text") or ""))}</td>'
              f'<td>{id_cell}</td></tr>')
    return s + '</table>'


#: Hinweise über dem Absenden des Imports aus der WISO-Datenbank, wenn der Bestand nicht leer ist (``{n}``: die Zahl der
#: Buchungen bzw. der aus einem früheren Import stammenden), und die Beschriftung des Pflicht-Kästchens.
STOCK_NOTICE_BOOKINGS = ('Es gibt bereits {n} Buchungen. Der Import ist für einen leeren Bestand gedacht: Buchungen, die '
                         'schon erfasst sind (von Hand, aus Kontoauszügen oder einem Textimport), können danach doppelt '
                         'vorhanden sein.')
STOCK_NOTICE_WISO_ROWS = ('Es gibt bereits {n} Buchungen aus einem Import der WISO-Datenbank. Ein weiterer Import erkennt '
                          'sie wieder – auch Sätze, die WISO inzwischen neu angelegt hat (etwa nach einem '
                          'DATEV-Export). Buchungen, die es in WISO nicht mehr gibt, stehen danach in einer Liste und '
                          'müssen von Hand bereinigt werden; in WISO nachträglich geänderte Beträge übernimmt der Import '
                          'nicht.')
STOCK_CONFIRM_LABEL = 'Trotzdem in den vorhandenen Bestand importieren'


def _wiso_stock_confirmation(stock):
    """Hinweis und Pflicht-Kästchen (``confirm_existing``) für das Formular „Import aus der Datenbank“, wenn der Bestand nicht
    leer ist – ``stock`` ist ``Database.wiso_import_stock()``: ``bookings`` (alle Zeilen) und ``wiso_rows`` (aus einem Import der
    WISO-Datenbank). Gibt es Zeilen aus einem solchen Import, nennt der Hinweis deren Zahl, sonst die aller Buchungen. Bei
    leerem Bestand kommt nichts (leerer Text)."""
    if not stock['bookings']:
        return ''
    if stock['wiso_rows']:
        notice = STOCK_NOTICE_WISO_ROWS.format(n=stock['wiso_rows'])
    else:
        notice = STOCK_NOTICE_BOOKINGS.format(n=stock['bookings'])
    return (f'<p class="warnColor">\u26a0\ufe0f {_html.escape(notice)}</p>'
            '<div class="rowWithObjects"><label><input type="checkbox" name="confirm_existing" value="1" required>'
            f'&nbsp;{_html.escape(STOCK_CONFIRM_LABEL)}</label></div>')


#: Hinweise im DATEV-Formular: ``DATEV_TRANSFER_NOTE`` steht dauernd dort (der Export liefert eine Datei, kein Skript sieht
#: seine Antwort); ``DATEV_CONFIRM_NOTICE`` (``{von}``/``{bis}``: der Zeitraum, für den der Hinweis kam) und das Pflicht-Kästchen
#: kommen über das Absenden, nachdem der Export Hinweise zum Stapel gemeldet hat (Meldung darunter).
DATEV_TRANSFER_NOTE = ('Überträge zwischen eigenen Konten, die auf beiden Bankbewegungen kontiert sind, stehen nur einmal im '
                       'Stapel (Ausgangsseite). Wo das nicht sicher zu erkennen ist, bleibt jede Zeile stehen und der Export '
                       'weist darauf hin.')
DATEV_CONFIRM_NOTICE = 'Für den Zeitraum {von} bis {bis} gibt es Hinweise zum Stapel (Meldung unten).'
DATEV_CONFIRM_LABEL = 'Trotzdem exportieren'

#: Hinweis unter dem DATEV-Formular, solange an der eigenen Firma Soll-Versteuerung eingetragen ist (seit 2.4.0): Die Angabe
#: wird nur gespeichert, der Stapel bleibt, wie er ist. Bei Ist-Versteuerung oder ohne Angabe steht dort nichts.
DATEV_VAT_SOLL_NOTE = ('Für die eigene Firma ist Soll-Versteuerung eingetragen. Die App weist Erlös und Umsatzsteuer '
                       'weiterhin mit dem Zahlungseingang aus – auch im DATEV-Stapel.')

#: Die Prüfsumme des Hinweises (``handlers._datev_check``): 16 Kleinbuchstaben und Ziffern, sonst gilt sie nicht
_DATEV_CHECK = re.compile(r'[0-9a-f]{16}')


def _datev_confirmation(date_from, date_to, check):
    """Hinweis, Pflicht-Kästchen (``confirm_export``) und das versteckte Feld ``confirmed_check`` im DATEV-Formular, nachdem der
    Export Hinweise zum Stapel gemeldet hat. Die Bestätigung gilt nur für genau diesen Hinweis: Ändert sich der Zeitraum oder
    der Bestand, passt die Prüfsumme nicht mehr, und der Hinweis kommt neu. Alle Werte sind geprüft (ISO-Daten, 16 Zeichen
    Prüfsumme) und maskiert."""
    return (f'<input type="hidden" name="confirmed_check" value="{_html.escape(check, quote=True)}">'
            f'<p class="warnColor">\u26a0\ufe0f {_html.escape(DATEV_CONFIRM_NOTICE.format(von=date_from, bis=date_to))}</p>'
            '<div class="rowWithObjects"><label><input type="checkbox" name="confirm_export" value="1" required>'
            f'&nbsp;{_html.escape(DATEV_CONFIRM_LABEL)}</label></div>')


def _import_notices(result):
    """Hinweise zu Vorzeichen, Konten 1000–1099, unbekannten Steuerschlüsseln, älteren
    Überträgen, Unterkonten und AfA aus dem Ergebnis eines WISO-Imports (Text- und Datenbank-Import
    melden beide ``negative_gross``, ``stock_range_accounts``, ``legacy_stock_rows`` und
    ``legacy_mirrors``; ``unknown_tax_keys`` – eine Liste von ``{'schluessel', 'anzahl'}`` – kommt
    nur vom Textimport, vom Datenbank-Import kommen ``subaccount_rows``,
    ``future_depreciations_skipped`` (mit ``depreciations_planned``), ``future_depreciations_kept``,
    ``depreciation_conflicts`` mit der Liste ``depreciation_conflict_rows`` aus ``{'anlage', 'jahr'}``, die Liste
    ``liquid_subaccounts`` aus ``{'nummer', 'basis', 'anzahl'}``, ``regrouped_rows`` (mit ``regrouped_text_differs``) und
    ``orphaned_rows_count`` mit der Liste ``orphaned_rows`` aus ``{'id', 'date', 'amount', 'account', 'counter_account',
    'text'}``; ebenfalls nur vom Datenbank-Import: ``uncommitted_records``, ``unreadable_versions`` und ``unknown_tx_state`` –
    die Sätze der WISO-Datei, die nicht abgeschlossene Vorgänge betreffen) als HTML. Die Ergebnisdatei kann
    von Hand verändert sein: Was nicht passt, gilt als leer bzw. 0, und jeder Wert aus ihr wird
    maskiert."""
    s = ''
    uncommitted = _result_count(result.get('uncommitted_records'))
    if uncommitted:
        records = ('1 Satz aus einem nicht abgeschlossenen Vorgang' if uncommitted == 1
                   else f'{uncommitted} Sätze aus nicht abgeschlossenen Vorgängen')
        s += (f'<p>\u2139\ufe0f Die WISO-Datenbank enthielt {records} (WISO war beim Kopieren geöffnet oder wurde nicht '
              'sauber beendet). Übernommen wurde jeweils der letzte abgeschlossene Stand.</p>')
    unreadable = _result_count(result.get('unreadable_versions'))
    if unreadable:
        records = ('1 Satz ließ sich nicht auf einen abgeschlossenen Stand zurückführen und fehlt' if unreadable == 1
                   else f'{unreadable} Sätze ließen sich nicht auf einen abgeschlossenen Stand zurückführen und fehlen')
        s += (f'<p class="warnColor">\u26a0\ufe0f {records} im Import – bitte WISO schließen, die Datei neu kopieren '
              'und den Import wiederholen.</p>')
    unknown = _result_count(result.get('unknown_tx_state'))
    if unknown:
        text = ('Bei 1 Satz ließ sich nicht feststellen, ob sein Vorgang abgeschlossen ist; er wurde übernommen, wie er '
                'in der Datei steht.' if unknown == 1 else
                f'Bei {unknown} Sätzen ließ sich nicht feststellen, ob ihr Vorgang abgeschlossen ist; sie wurden '
                'übernommen, wie sie in der Datei stehen.')
        s += f'<p class="warnColor">\u26a0\ufe0f {text}</p>'
    negative = _result_count(result.get('negative_gross'))
    if negative:
        s += (f'<h3>\u26a0\ufe0f Negative Bruttobeträge &ndash; {negative} Sätze</h3>'
              '<p>Bei diesen Sätzen steht genau ein Zahlungskonto (Bank, Kasse oder '
              'Verrechnungskonto) auf einer Seite. Das Vorzeichen richtet sich dann nach der '
              'Geldbewegung, nicht nach dem negativen Bruttobetrag. WISO schreibt Bruttobeträge '
              'üblicherweise nicht negativ &ndash; Stornos und Erstattungen stehen dort mit '
              'vertauschten Konten. Diese Sätze fallen aus dem Muster (bearbeitete Datei, andere '
              'Programmversion) &ndash; bitte nachsehen.</p>')
    accounts = [row for row in (result.get('stock_range_accounts') or []) if isinstance(row, dict)]
    if accounts:
        s += (f'<h3>\u26a0\ufe0f Konten 1000&ndash;1099 &ndash; {len(accounts)}</h3>'
              '<p>Konten 1000&ndash;1099 sind im SKR04 Vorräte; ältere Versionen haben sie als '
              'Kasse gelesen &ndash; bitte prüfen, ob hier eine Kasse gemeint ist. Buchungen gegen '
              'diese Konten werden als Umbuchung gelesen und erscheinen nicht in der EÜR.</p>'
              '<table><tr><th>Konto</th><th>Bezeichnung</th><th>Buchungen</th></tr>')
        for row in accounts:
            s += (f'<tr><td>{_html.escape(str(row.get("nummer", "")))}</td>'
                  f'<td>{_html.escape(str(row.get("bezeichnung", "")))}</td>'
                  f'<td>{_html.escape(str(row.get("anzahl", "")))}</td></tr>')
        s += '</table>'
    tax_keys = _fdb_result_rows(result.get('unknown_tax_keys'))
    if tax_keys:
        total = sum(_result_count(row.get('anzahl')) for row in tax_keys)
        s += (f'<h3>\u26a0\ufe0f Unbekannte Steuerschlüssel &ndash; {total} Zeilen</h3>'
              '<p>Diese Zeilen tragen einen Steuerschlüssel, den die App nicht kennt. Sie wurden '
              'ohne Steuersatz importiert &ndash; bitte prüfen, ob dort Steuer zu buchen ist.</p>'
              '<table><tr><th>Schlüssel</th><th>Zeilen</th></tr>')
        for row in tax_keys:
            s += (f'<tr><td>{_html.escape(str(row.get("schluessel", "")), quote=True)}</td>'
                  f'<td>{_html.escape(str(_result_count(row.get("anzahl"))), quote=True)}</td></tr>')
        s += '</table>'
    liquid = _fdb_result_rows(result.get('liquid_subaccounts'))
    if liquid:
        s += (f'<h3>\u26a0\ufe0f Unterkonten von Zahlungskonten &ndash; {len(liquid)}</h3>'
              '<p>Diese Konten sind in WISO Unterkonten eines Bank-, Kassen- oder Verrechnungskontos, '
              'in der App aber kein Zahlungskonto. Buchungen darauf werden als Umbuchung gelesen und '
              'erscheinen nicht in der EÜR. Bitte das Konto vor dem Import als Bank- oder Kassenkonto '
              'anlegen und neu importieren.</p>'
              '<table><tr><th>Konto</th><th>Unterkonto von</th><th>Buchungen</th></tr>')
        for row in liquid:
            s += (f'<tr><td>{_html.escape(str(row.get("nummer", "")), quote=True)}</td>'
                  f'<td>{_html.escape(str(row.get("basis", "")), quote=True)}</td>'
                  f'<td>{_html.escape(str(row.get("anzahl", "")), quote=True)}</td></tr>')
        s += '</table>'
        if len(liquid) >= MAX_LIQUID_SUBACCOUNTS:
            s += f'<p>Genannt werden die ersten {MAX_LIQUID_SUBACCOUNTS}.</p>'
    conflicts = _fdb_result_rows(result.get('depreciation_conflict_rows'))
    conflict_count = _result_count(result.get('depreciation_conflicts')) or len(conflicts)
    if conflict_count:
        s += (f'<h3>\u26a0\ufe0f AfA schon in der App gebucht &ndash; {conflict_count}</h3>'
              '<p>Für diese Jahre ist die AfA schon in der App gebucht; der AfA-Satz aus der '
              'WISO-Datenbank wurde nicht übernommen.</p>')
        if conflicts:
            s += '<table><tr><th>Anlage</th><th>Jahr</th></tr>'
            for row in conflicts:
                s += (f'<tr><td>{_html.escape(str(row.get("anlage", "")), quote=True)}</td>'
                      f'<td>{_html.escape(str(row.get("jahr", "")), quote=True)}</td></tr>')
            s += '</table>'
            if conflict_count > len(conflicts):
                s += f'<p>Genannt werden die ersten {len(conflicts)}.</p>'
    orphans = _fdb_result_rows(result.get('orphaned_rows'))
    orphan_count = _result_count(result.get('orphaned_rows_count')) or len(orphans)
    if orphan_count:
        s += (f'<h3>\u26a0\ufe0f Nicht mehr in WISO vorhandene Buchungen &ndash; {orphan_count}</h3>'
              f'<p>{orphan_count} Buchungen aus einem früheren Import stehen nicht mehr in der WISO-Datenbank '
              '(dort gelöscht oder mit anderem Inhalt neu angelegt). Sie zählen in den Auswertungen weiter mit '
              '&ndash; bitte prüfen und löschen, was doppelt ist.</p>')
        if orphans:
            s += _orphaned_rows_table(orphans)
            if orphan_count > len(orphans):
                s += f'<p>Genannt werden die ersten {len(orphans)}.</p>'
    regrouped = _result_count(result.get('regrouped_rows'))
    if regrouped:
        s += (f'<p>\u2139\ufe0f {regrouped} neu angelegte WISO-Buchungen wurden wiedererkannt und nicht doppelt '
              'angelegt')
        regrouped_text = _result_count(result.get('regrouped_text_differs'))
        if regrouped_text:
            s += f', davon {regrouped_text} mit geändertem Text (der Text der App bleibt)'
        s += '.</p>'
    stock_legacy = _result_count(result.get('legacy_stock_rows'))
    if stock_legacy:
        s += (f'<p>\u2139\ufe0f {stock_legacy} Buchungen auf Konten 1000&ndash;1099 stammen aus einer '
              'älteren Version und sind nach deren Lesart gespeichert; ihre Form und ihr Betrag '
              'wurden nicht verändert, und sie wurden nicht erneut angelegt.</p>')
    legacy = _result_count(result.get('legacy_mirrors'))
    if legacy:
        s += (f'<p>\u2139\ufe0f {legacy} ältere Überträge sind noch nach der alten Lesart '
              'gespeichert; ihre Form und ihr Betrag wurden nicht verändert.</p>')
    subaccounts = _result_count(result.get('subaccount_rows'))
    if subaccounts:
        s += (f'<p>\u2139\ufe0f {subaccounts} Buchungen stehen aus einem älteren Import noch auf dem '
              'Basiskonto statt auf dem eigenen Unterkonto. Sie bleiben unverändert &ndash; für die '
              'getrennten Konten die Daten in eine frische Datenbank importieren.</p>')
    future = _result_count(result.get('future_depreciations_skipped'))
    if future:
        s += (f'<p>\u2139\ufe0f {future} vorausgeplante AfA-Sätze mit einem Datum nach dem Importtag '
              'wurden nicht als Buchung übernommen.')
        planned = _result_count(result.get('depreciations_planned'))
        if planned:
            s += (f' {planned} Jahre stehen als geplant im AfA-Plan der Anlagen; die AfA ist im '
                  'jeweiligen Jahr in der App zu buchen.')
        s += '</p>'
    kept = _result_count(result.get('future_depreciations_kept'))
    if kept:
        s += (f'<p>\u2139\ufe0f {kept} AfA-Buchungen mit einem Datum nach dem Importtag stammen aus einem '
              'früheren Import. Sie bleiben unverändert in den Büchern; ihre Jahre gelten als gebucht.</p>')
    keys_filled = _result_count(result.get('datev_keys_filled'))
    if keys_filled:
        s += (f'<p>\u2139\ufe0f {keys_filled} schon importierte Buchungen haben den BU-Schlüssel aus der '
              'WISO-Datenbank bekommen (für den DATEV-Export); sonst blieb an ihnen nichts geändert.</p>')
    mismatches = _result_count(result.get('datev_key_rate_mismatches'))
    if mismatches:
        s += (f'<p>\u2139\ufe0f {mismatches} Buchungen tragen in der App einen anderen Steuersatz als in WISO &ndash; '
              'ihr DATEV-Schlüssel wurde nicht übernommen, der Export leitet ihn aus dem Satz der App ab.</p>')
    if _result_count(result.get('datev_numbers_set')):
        s += ('<p>\u2139\ufe0f Berater- und Mandantennummer für den DATEV-Export stehen jetzt in den Stammdaten der '
              'eigenen Firma (aus der WISO-Datenbank).</p>')
    return s


def PageMiscellaneous(db: Database, check_payments: bool = False, datev_confirm: bool = False,
                      datev_from=None, datev_to=None, datev_check=None, msg=None):
    """Generate Sonstiges page (database overview, SQL console, misc tools).

    ``check_payments``: die Prüfliste der Zahlungszuordnungen mit anzeigen. Sie läuft nur auf
    Klick (``?check_payments=1``), nicht bei jedem Laden der Seite – bei einem großen Bestand
    braucht sie einen Moment.

    ``datev_confirm``, ``datev_from``, ``datev_to``, ``datev_check``: Der DATEV-Export hat Hinweise zum Stapel gemeldet
    (``?datev_export=confirm&datev_from=…&datev_to=…&datev_check=…``). Sind beide Daten ISO-Daten im selben Jahr und die
    Prüfsumme 16 Kleinbuchstaben oder Ziffern, trägt das Formular den Zeitraum und verlangt die ausdrückliche Bestätigung
    (Pflicht-Kästchen); sonst bleibt es bei der Vorgabe des laufenden Jahres und ohne Kästchen. Die Seite schreibt dabei nichts.

    ``msg``: die Meldung einer Weiterleitung (``?msg=``, vom Server schon einmal dekodiert; höchstens ``MSG_MAX_LENGTH`` Zeichen).
    Sie steht nur als JSON im Skriptblock ``miscMsg`` (``script_json``), die Skripte der Kästen zeigen sie über ``textContent``
    bzw. ``appMsg`` – nie als HTML."""
    import urllib.parse
    from urllib.parse import parse_qs, urlparse
    from server.pages import script_json as _script_json
    s = Header1('miscellaneous')
    s += Header2()
    s += Header3()
    s += '<script>var miscMsg = ' + _script_json(msg[:MSG_MAX_LENGTH] if isinstance(msg, str) else '') + ';</script>'

    s += '<div class="grid3Cols gridMain">'
    # ── Database statistics ───────────────────────────────────────────────────
    s += '\t<div class="rectRounded">'
    stats = db.get_table_statistics()
    s += "\t\t<h2>Datenbank-Übersicht</h2>"
    s += "\t\t<table>"
    s += "\t\t\t<tr><th>Tabelle</th><th>Anzahl Einträge</th></tr>"
    for table_name, count in stats:
        s += f"\t\t\t<tr><td>{table_name}</td><td style='text-align: right;'>{count}</td></tr>"
    s += "\t\t</table>"
    s += '\t</div>'
    # ── SQL Input Field ───────────────────────────────────────────────────────
    import auth as _auth
    if _auth.sql_console_allowed():
        s += '\t<div class="rectRounded" style="grid-column: span 2">'
        s += '''
        <h2>SQL-Befehle ausführen</h2>
        Gib hier SQL-Befehle ein (mehrere Befehle durch Semikolon getrennt). Eine Anfrage läuft höchstens 60 Sekunden, sehr große Ausgaben werden gekürzt:<br>
        <textarea id="sql_input" rows="15" cols="100" class="textareaSql" placeholder="SELECT * FROM ChartOfAccounts WHERE AccountNumber = 6805;"></textarea>
        <div class="rowWithObjects">
            <label><input type="checkbox" id="sql_write" autocomplete="off">&nbsp;Schreiben erlauben</label>
        </div>
        <p class="hint">Ohne Haken läuft die Konsole nur lesend. Mit Haken laufen alle Befehle in einer Transaktion: Scheitert einer, wird nichts geändert.</p>
        <div>
            <button type="button" onclick="executeSql()" class="coloredButton bg-orange">SQL ausführen</button>
            <span style="color: red; margin-left: 20px;">⚠️ Achtung: SQL-Befehle werden direkt ausgeführt!</span>
        </div>
        <br><br>
        <h3>Zeitbereich löschen</h3>
            <div>Erzeugt ein Lösch-Skript für Belege, Buchungen, Rechnungen und Angebote im Zeitraum und fügt es oben ins SQL-Feld ein.
                Zum Ausführen „Schreiben erlauben“ ankreuzen und obigen Button klicken. Dateien im Dateisystem bleiben erhalten.</div>
        <div>
            <label>Von:&nbsp;<input type="date" id="range_del_from"></label>
            <label>Bis:&nbsp;<input type="date" id="range_del_to"></label>
            <button type="button" onclick="buildRangeDeleteSql()" class="coloredButton bg-blue">SQL erzeugen</button>
        </div>
        <script>
        function buildRangeDeleteSql() {
            const from = document.getElementById('range_del_from').value;
            const to   = document.getElementById('range_del_to').value;
            if (!from || !to) { sqlStatus([['errorColor', 'Bitte Von- und Bis-Datum angeben.']]); return; }
            if (from > to)    { sqlStatus([['errorColor', 'Von-Datum liegt nach dem Bis-Datum.']]); return; }
            const b = "SELECT ID FROM Bookings  WHERE DateBooking BETWEEN '" + from + "' AND '" + to + "'";
            const d = "SELECT ID FROM Documents WHERE Date        BETWEEN '" + from + "' AND '" + to + "'";
            const i = "SELECT ID FROM Invoices  WHERE InvoiceDate BETWEEN '" + from + "' AND '" + to + "'";
            const sql =
                "-- Zeitbereich löschen: " + from + " bis " + to + "\\n" +
                "-- Reihenfolge beachtet Fremdschlüssel. Dateien im Dateisystem bleiben erhalten.\\n" +
                "DELETE FROM InvoicePayments WHERE InvoiceID IN (" + i + ") OR BookingID IN (" + b + ");\\n" +
                "DELETE FROM InvoiceItems WHERE InvoiceId IN (" + i + ");\\n" +
                "DELETE FROM InvoiceCorrections WHERE InvoiceID IN (" + i + ");\\n" +
                "DELETE FROM InvoiceDocumentVersions WHERE InvoiceID IN (" + i + ");\\n" +
                "UPDATE Invoices SET SourceQuoteId = NULL WHERE SourceQuoteId IN (" + i + ");\\n" +
                "UPDATE Invoices SET CancelsInvoiceId = NULL WHERE CancelsInvoiceId IN (" + i + ");\\n" +
                "UPDATE Invoices SET ReplacesInvoiceId = NULL WHERE ReplacesInvoiceId IN (" + i + ");\\n" +
                "DELETE FROM Invoices WHERE InvoiceDate BETWEEN '" + from + "' AND '" + to + "';  -- Rechnungen UND Angebote\\n" +
                "DELETE FROM BookingDocuments WHERE Booking_ID IN (" + b + ") OR Document_ID IN (" + d + ");\\n" +
                "UPDATE Assets SET Booking_ID = NULL WHERE Booking_ID IN (" + b + ");\\n" +
                "UPDATE Assets SET Document_ID = NULL WHERE Document_ID IN (" + d + ");\\n" +
                "UPDATE AssetDepreciations SET Booking_ID = NULL, Status = 'planned', BookedAt = NULL WHERE Booking_ID IN (" + b + ");\\n" +
                "UPDATE Trips SET DocumentID = NULL WHERE DocumentID IN (" + d + ");\\n" +
                "-- Sätze außerhalb des Zeitraums unter einer Bankbewegung des Zeitraums bleiben erhalten (abgehängt, wie im Buchungssatz-Editor).\\n" +
                "UPDATE Bookings SET ParentBooking_ID = NULL, Status = CASE WHEN Status = 'resolved' THEN NULL ELSE Status END WHERE ParentBooking_ID IN (" + b + ") AND DateBooking NOT BETWEEN '" + from + "' AND '" + to + "';\\n" +
                "DELETE FROM Bookings WHERE DateBooking BETWEEN '" + from + "' AND '" + to + "';\\n" +
                "DELETE FROM Documents WHERE Date BETWEEN '" + from + "' AND '" + to + "';";
            document.getElementById('sql_input').value = sql;
            sqlStatus([['', 'Lösch-Skript eingefügt — bitte prüfen, „Schreiben erlauben“ ankreuzen und „SQL ausführen“ klicken.']]);
        }
        </script>
        <div id="sql_status" style="margin-top: 10px;"></div>
        <textarea id="sql_output" rows="20" cols="100" readonly class="textareaSql" style="display: none;"></textarea>
        <script>
        // Meldungen nur über textContent: Fehlertexte kommen aus der Datenbank bzw. dem Server
        function sqlStatus(parts) {
            const status = document.getElementById('sql_status');
            status.textContent = '';
            parts.forEach(function(part) {
                const span = document.createElement('span');
                if (part[0]) span.className = part[0];
                span.textContent = part[1];
                status.appendChild(span);
                status.appendChild(document.createTextNode(' '));
            });
        }
        async function sendSql(input, write) {
            const output = document.getElementById('sql_output');
            sqlStatus([['', '⏳ Wird ausgeführt...']]);
            try {
                const resp = await fetch('/execute_sql', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/x-www-form-urlencoded'},
                    body: 'sql_commands=' + encodeURIComponent(input) + (write ? '&write=1' : '')
                });
                const data = await resp.json();
                const parts = [];
                if (data.success_count > 0) parts.push(['successColor', data.success_count + ' von ' + data.total + ' Befehlen erfolgreich.']);
                if (data.errors && data.errors.length) parts.push(['errorColor', data.errors.join('; ')]);
                if (data.warnings && data.warnings.length) parts.push(['warnColor', data.warnings.join('; ')]);
                sqlStatus(parts);
                if (data.output) {
                    output.style.display = 'block';
                    output.value = data.output;
                } else {
                    output.style.display = 'none';
                    output.value = '';
                }
            } catch(e) {
                sqlStatus([['errorColor', 'Fehler: ' + e.message]]);
            } finally {
                if (write) document.getElementById('sql_write').checked = false;
            }
        }
        function executeSql() {
            const input = document.getElementById('sql_input').value;
            if (!input.trim()) { sqlStatus([['errorColor', 'Keine SQL-Befehle eingegeben.']]); return; }
            if (!document.getElementById('sql_write').checked) return sendSql(input, false);
            appConfirm('Die Befehle können Daten ändern oder löschen. Vorher eine Datensicherung anlegen. Ausführen?',
                       function() { return sendSql(input, true); });
        }
        </script>
        '''
        s += '\t</div>'
    else:
        s += '\t<div class="rectRounded" style="grid-column: span 2">'
        s += ('<h2>SQL-Befehle ausführen</h2>'
              '<p>Die SQL-Konsole steht nur Administratoren zur Verfügung.</p>')
        s += '\t</div>'

    # Both Exports together in one DIV
    s += '\t<div class="grid1Col gridMiddle">'
    # ── DB Export as SQL ──────────────────────────────────────────────────────
    s += '\t\t<div class="rectRounded">'
    s += '''
        <h2>DB-Export</h2>
        <p>Exportiert alle Tabelleninhalte als INSERT-Statements in die Datei
        <code>db-export.sql</code> im Datenverzeichnis.'''
    if _auth.sql_console_allowed():
        s += '''
        Ein kleinerer Export lässt sich im SQL-Konsolenbereich einfügen (mit „Schreiben erlauben“);
        für ganze Bestände gibt es die Datensicherung.'''
    s += '''</p>
        <form method="POST" action="/db_export">
            <button type="submit" class="coloredButton bg-blue">&#x1F4BE; DB-Export</button>
        </form>
        '''
    # Show export result message if redirected back with status
    s += '''
        <script>
        (function() {
            const p = new URLSearchParams(window.location.search);
            const status = p.get('export');
            if (status === 'ok') {
                const div = document.createElement('div');
                div.style = 'margin-top:10px; padding:8px 14px; background:#d4edda; color:#155724; border-radius:4px; display:inline-block;';
                div.textContent = '✅ Export erfolgreich: ' + p.get('tables') + ' Tabellen, ' + p.get('rows') + ' Zeilen.';
                document.currentScript.parentNode.insertBefore(div, document.currentScript);
            } else if (status === 'error') {
                const div = document.createElement('div');
                div.style = 'margin-top:10px; padding:8px 14px; background:#f8d7da; color:#721c24; border-radius:4px; display:inline-block;';
                div.textContent = '❌ Fehler beim Export: ' + miscMsg;
                document.currentScript.parentNode.insertBefore(div, document.currentScript);
            }
        })();
        </script>
        '''
    s += '\t\t</div>'
    # ── DATEV Export ──────────────────────────────────────────────────────────
    s += '\t\t<div class="rectRounded">'
    import datetime
    cur_year = datetime.date.today().year
    datev_hint = ''
    if db.get_datev_numbers() == (None, None):
        datev_hint = ('<p class="hint warnColor">'
                      + _html.escape('Berater- und Mandantennummer fehlen – in den Stammdaten der eigenen Firma '
                                     'eintragen (Kontakt als Unternehmen angelegt). Der Stapel trägt bis dahin '
                                     'Platzhalter.', quote=True)
                      + '</p>')
    vat_hint = ''
    if db.get_vat_method() == 'soll':
        vat_hint = f'<p class="hint warnColor">{_html.escape(DATEV_VAT_SOLL_NOTE, quote=True)}</p>'
    # Nach den Hinweisen zum Stapel trägt das Formular den Zeitraum, für den sie kamen, und verlangt die Bestätigung – nur bei
    # zwei ISO-Daten im selben Jahr und einer Prüfsumme in der erwarteten Form; alles andere (auch ein untergeschobener Wert)
    # bleibt unbeachtet.
    form_from, form_to, confirmation = f'{cur_year}-01-01', f'{cur_year}-12-31', ''
    asked_from, asked_to = parse_iso_date(datev_from), parse_iso_date(datev_to)
    if (datev_confirm and asked_from and asked_to and asked_from[:4] == asked_to[:4]
            and isinstance(datev_check, str) and _DATEV_CHECK.fullmatch(datev_check)):
        form_from, form_to = asked_from, asked_to
        confirmation = _datev_confirmation(asked_from, asked_to, datev_check)
    transfer_note = _html.escape(DATEV_TRANSFER_NOTE, quote=True)
    s += f'''
        <h2>DATEV Export</h2>
        <p>Exportiert Buchungen als <strong>DATEV Buchungsstapel-CSV</strong> (EXTF 700, Encoding CP1252).<br>
        Das <em>Steuerdatum</em> der exportierten Buchungen wird auf das <strong>heutige Datum</strong> gesetzt.</p>
        <p class="hint">{transfer_note}</p>
        <form method="POST" action="/datev/export">
            <div class="rowWithObjects">
                <label>Von:&nbsp;<input type="date" name="date_from" value="{_html.escape(form_from, quote=True)}" required></label>
                <label>Bis:&nbsp;<input type="date" name="date_to" value="{_html.escape(form_to, quote=True)}" required></label>
            </div>
            {confirmation}
            <br>
            <button type="submit" class="coloredButton bg-blue">&#x1F4E5; DATEV-CSV exportieren</button>
        </form>
        {datev_hint}
        {vat_hint}
        <script>
        (function() {{
            const p = new URLSearchParams(window.location.search);
            const status = p.get('datev_export');
            if (status === 'error') {{
                const div = document.createElement('div');
                div.style = 'margin-top:10px; padding:8px 14px; background:#f8d7da; color:#721c24; border-radius:4px; display:inline-block;';
                div.textContent = '❌ DATEV-Export Fehler: ' + miscMsg;
                document.currentScript.parentNode.insertBefore(div, document.currentScript);
            }} else if (status === 'confirm') {{
                const div = document.createElement('div');
                div.style = 'margin-top:10px; padding:8px 14px; background:#fff3cd; color:#856404; border-radius:4px; display:inline-block;';
                div.textContent = '⚠️ DATEV-Export: ' + miscMsg;
                document.currentScript.parentNode.insertBefore(div, document.currentScript);
            }}
        }})();
        </script>
        '''
    s += '\t\t</div>'
    # ── Zahlungszuordnungen prüfen (nur Anzeige) ──────────────────────────────
    s += '\t\t<div class="rectRounded">'
    s += '<h2>Zahlungszuordnungen</h2>'
    s += ('<p>Prüft, ob Zuordnungen, Buchungssätze und offener Rest der Rechnungen '
          'zusammenpassen. Die Prüfung ändert nichts.</p>'
          '<form method="GET" action="/miscellaneous">'
          '<input type="hidden" name="check_payments" value="1">'
          '<button type="submit" class="coloredButton bg-blue">Zahlungszuordnungen prüfen</button>'
          '</form>')
    if check_payments:
        findings = db.find_payment_inconsistencies()
        if not findings:
            s += '<p class="successColor">Keine Unstimmigkeiten gefunden.</p>'
        else:
            shown = findings[:50]
            s += ('<p>Bei diesen Rechnungen passen Zuordnung, Buchungssätze oder offener Rest '
                  'nicht zusammen. Die Liste ändert nichts &ndash; die Rechnung öffnen und die '
                  'Buchungen prüfen.</p><table><tr><th>Rechnung</th><th>Befund</th></tr>')
            for finding in shown:
                number = _html.escape(finding['invoice_number'] or '–')
                if finding['invoice_id'] is not None:
                    number = f'<a href="/invoice/edit?id={int(finding["invoice_id"])}">{number}</a>'
                s += f'<tr><td>{number}</td><td>{_html.escape(finding["text"])}</td></tr>'
            s += '</table>'
            if len(findings) > len(shown):
                s += f'<p>und {len(findings) - len(shown)} weitere</p>'
    s += '\t\t</div>'
    s += '\t</div>'

    s += '\t<div class="rectRounded">'
    # ── WISO Mein Büro Import ─────────────────────────────────────────────────
    import json as _json, os as _os
    from urllib.parse import parse_qs as _parse_qs, urlparse as _urlparse

    # Letztes Import-Ergebnis einlesen (falls vorhanden)
    import userctx as _userctx
    _result_path = _os.path.join(_userctx.user_data_dir(), 'wiso_import_result.json')
    _last_result = None
    if _os.path.exists(_result_path):
        try:
            with open(_result_path, encoding='utf-8') as _f:
                _last_result = _json.load(_f)
        except Exception:
            _last_result = None

    s += '''
        <h2>WISO Mein Büro Import</h2>
        <p>Importiert Buchungen aus <strong>WISO Mein Büro</strong>.<br>
        1. Bewegungsdaten-Export
        (Datei &rarr; Export &rarr; &bdquo;Buchungsdaten als CSV&ldquo;).<br>
        2. Tabellen-Export (nur ergänzend, direkt aus der Tabellenansicht exportieren)<br>
        Voraussetzung: Jedes Konto in der <strong>Kontenverwaltung</strong>
        muss ein <em>SKR-Gegenkonto</em> hinterlegt haben (z.B. 1810 für 2. Bankkonto, 1600 für Kasse;
        das Verrechnungskonto 1460 gilt als Geldtransit).<br>
        Duplikate (gleiche Referenznummer + Datum + Konto + Betrag) werden übersprungen &ndash;
        auch, wenn die Buchungen schon aus dem Datenbank-Import stammen.</p>
        <form method="POST" action="/wiso/import" enctype="multipart/form-data">
            <div class="rowWithObjects">CSV-Datei:&nbsp;<input type="file" name="csvfile" accept=".txt,.csv" required></div>
            <br>
            <button type="submit" class="coloredButton bg-green">&#x1F4C2; WISO Import starten</button>
        </form>
        <script>
        (function() {
            const p = new URLSearchParams(window.location.search);
            const status = p.get('wiso_import');
            if (!status) return;
            const file = p.get('file');
            const div = document.createElement('div');
            div.style = 'margin-top:10px; padding:8px 14px; border-radius:4px; display:inline-block;';
            let msg;
            if (status === 'ok') {
                const ec  = parseInt(p.get('err_count')    || '0');
                const mc  = parseInt(p.get('missing_coa')  || '0');
                const ms  = parseInt(p.get('missing_skr')  || '0');
                const nf  = parseInt(p.get('not_found')    || '0');
                const warn = ec > 0 || mc > 0 || ms > 0 || nf > 0;
                msg = '\u2705 Import abgeschlossen: '
                        + p.get('imported') + ' importiert, '
                        + p.get('updated')  + ' aktualisiert, '
                        + p.get('skipped')  + ' \u00fcbersprungen';
                const lk = parseInt(p.get('linked') || '0');
                const rv = parseInt(p.get('resolved') || '0');
                if (lk > 0) msg += ', ' + lk + ' verkn\u00fcpft';
                if (rv > 0) msg += ', ' + rv + ' Debitoren aufgel\u00f6st';
                if (nf > 0) msg += ', ' + nf + ' nicht gefunden';
                if (mc > 0) msg += ', ' + mc + ' fehlende SKR-Konten';
                if (ms > 0) msg += ', ' + ms + ' fehlende Gegenkonten';
                if (ec > 0) msg += ', ' + ec + ' Fehler';
                msg += '. Siehe Details unten.';
                div.style.background = warn ? '#fff3cd' : '#d4edda';
                div.style.color      = warn ? '#856404' : '#155724';
            } else {
                div.style.background = '#f8d7da';
                div.style.color = '#721c24';
                msg = '\u274c Fehler: ' + miscMsg;
            }
            // Dateiname als erste Zeile in derselben Box (append = Textknoten, kein HTML)
            if (file) {
                div.append('\U0001F4C4 Verarbeitete Datei: ' + file);
                div.appendChild(document.createElement('br'));
            }
            div.append(msg);
            document.currentScript.parentNode.insertBefore(div, document.currentScript);
        })();
        </script>
        '''

    # Detail-Tabellen aus letztem Ergebnis rendern (Dateiname steht in der
    # Import-abgeschlossen-Box; hier nicht erneut anzeigen)
    if _last_result:
        _skipped_rows = _last_result.get('skipped_rows', [])
        _not_found    = _last_result.get('not_found', [])
        _missing_coa  = _last_result.get('missing_coa', [])
        _missing_skr  = _last_result.get('missing_skr', [])
        _errors       = _last_result.get('errors', [])

        if _skipped_rows:
            s += f'<h3>\u26a0\ufe0f Übersprungene Zeilen (Duplikate) &ndash; {len(_skipped_rows)} Einträge</h3>'
            s += '<table><tr><th>CSV-Zeile</th><th>Datum</th><th>Referenz</th><th>KONTO</th><th>Betrag</th><th>Text</th></tr>'
            for _r in _skipped_rows:
                s += (f"<tr><td>{_r.get('zeile','')}</td><td>{_r.get('datum','')}</td>"
                      f"<td>{_r.get('ref','')}</td><td>{_r.get('konto','')}</td>"
                      f"<td>{_r.get('betrag','')}</td><td>{_r.get('text','')}</td></tr>")
            s += '</table>'

        if _missing_coa:
            s += f'<h3>\u26a0\ufe0f Fehlende SKR-Konten (KONTO nicht in Kontenrahmen) &ndash; {len(_missing_coa)} Einträge</h3>'
            s += '<p>Diese Kontonummern wurden in den Buchungen verwendet, sind aber noch nicht im Kontenrahmen hinterlegt.<br>'
            s += 'Die Buchungen wurden trotzdem importiert (ohne COA-Zuweisung). Bitte <a href="/masterdata/skr">in der SKR-Verwaltung</a> ergänzen:</p>'
            s += '<p style="font-family:monospace;">'
            s += ', '.join(_html.escape(str(_n)) for _n in _missing_coa)
            s += '</p>'

        if _missing_skr:
            s += f'<h3>\u26a0\ufe0f Fehlende Gegenkonten (GEGENKONTO nicht in Kontenverwaltung) &ndash; {len(_missing_skr)} Einträge</h3>'
            s += '<p>Diese SKR-Nummern stehen in der GEGENKONTO-Spalte, sind aber keinem Konto in der <a href="/masterdata/bankaccounts">Kontenverwaltung</a> zugewiesen:</p>'
            s += '<p style="font-family:monospace;">'
            s += ', '.join(_html.escape(str(_n)) for _n in _missing_skr)
            s += '</p>'
        if _not_found:
            s += f'<h3>\u26a0\ufe0f Nicht eindeutig zuordenbar &ndash; {len(_not_found)} Eintr\u00e4ge</h3>'
            s += ('<p>F\u00fcr diese Zeilen wurde keine <em>eindeutige</em> bestehende Buchung gefunden '
                  '(kein Treffer oder mehrere Kandidaten \u00fcber Datum/Beleg-Nr./Betrag/Text). '
                  'Es wurde nichts ge\u00e4ndert &ndash; die Buchungen k\u00f6nnen bereits vollst\u00e4ndig '
                  'vorhanden sein; bei Bedarf manuell pr\u00fcfen.</p>')
            s += '<table><tr><th>CSV-Zeile</th><th>Datum</th><th>Beleg-Nr.</th><th>Betrag</th><th>Text</th></tr>'
            for _r in _not_found:
                s += (f"<tr><td>{_html.escape(str(_r.get('zeile','')))}</td><td>{_html.escape(str(_r.get('datum','')))}</td>"
                      f"<td>{_html.escape(str(_r.get('beleg','')))}</td><td>{_html.escape(str(_r.get('betrag','')))}</td>"
                      f"<td>{_html.escape(str(_r.get('text','')))}</td></tr>")
            s += '</table>'
        s += _import_notices(_last_result)
        if _errors:
            s += f'<h3>\u274c Parse-Fehler &ndash; {len(_errors)} Einträge</h3><ul>'
            for _e in _errors:
                s += f'<li>{_html.escape(str(_e))}</li>'
            s += '</ul>'
    s += '\t</div>'

    # ── WISO-Datenbank (Firebird) ─────────────────────────────────────────────
    _fdb_path = _os.path.join(_userctx.user_data_dir(), 'wiso_fdb_result.json')
    _fdb = None
    if _os.path.exists(_fdb_path):
        try:
            with open(_fdb_path, encoding='utf-8') as _f:
                _fdb = _json.load(_f)
        except Exception:
            _fdb = None

    s += '\t<div class="rectRounded">'
    s += '''
        <h2>WISO Mein Büro &ndash; Import aus der Datenbank</h2>
        <p>Genauer als die Textexporte: die Datenbank nennt mit einer eigenen
        Kennung, <strong>welche Teilbuchungen zusammengehören</strong>, bringt das
        <strong>Anlagenverzeichnis samt AfA-Plan</strong> mit und umfasst alle Jahre.
        Gelesen wird nur &ndash; an der WISO-Datenbank ändert sich nichts.</p>
        <p>Beide Dateien liegen im <strong>DB-Verzeichnis</strong> von WISO Mein Büro: die
        Mandantendatei <code>DB1.FDB</code> und, optional, der Standard-Kontenrahmen
        <code>DB0.FDB</code>. Sie werden hochgeladen, gelesen und danach vom Server wieder
        gelöscht. WISO sollte dabei geschlossen sein.<br>
        Voraussetzung wie beim CSV-Import: jedes Konto in der
        <strong>Kontenverwaltung</strong> braucht ein hinterlegtes
        <em>SKR-Gegenkonto</em>. Ein zweiter Lauf verdoppelt nichts. Buchungen, die schon
        über den CSV-Import kamen, ergänzt der Import, statt sie neu anzulegen;
        wiedererkannt wird ein Satz nur, wenn Beleg-Nr., Datum, Konto, Betrag und Text übereinstimmen (und das Gegenkonto nicht widerspricht); alles andere wird neu angelegt und, wo eine ähnliche Buchung schon da ist, als möglicherweise doppelt gemeldet.</p>
        '''
    if _demo.is_active():
        # Wie die Route: Im Demo-Modus gibt es den Import nicht (keine Dateien auf dem Server eines Besuchers)
        s += f'<p class="hint">{_html.escape(_wiso_upload.DEMO_REFUSED)}</p>'
    else:
        s += '''
        <form method="POST" action="/wiso/fdb-import" id="wisoFdbForm">
            <div class="rowWithObjects">
                <label>Mandantendatei (<code>DB1.FDB</code>):&nbsp;
                <input type="file" id="wisoFdbClient" accept=".fdb" required></label>
            </div>
            <div class="rowWithObjects">
                <label>Standard-Kontenrahmen (<code>DB0.FDB</code>, optional):&nbsp;
                <input type="file" id="wisoFdbStandard" accept=".fdb"></label>
            </div>
            <input type="hidden" name="upload" id="wisoFdbUpload" value="" autocomplete="off">
            <div class="rowWithObjects">
                <label><input type="checkbox" name="with_assets" value="1" checked>
                &nbsp;Anlagenverzeichnis und AfA-Plan mitnehmen</label>
            </div>
            <div class="rowWithObjects">
                <label><input type="checkbox" name="with_invoices" value="1" checked>
                &nbsp;Kunden und Rechnungen mitnehmen (Kundennummern bleiben erhalten)</label>
            </div>
        '''
        # Der Import ist für einen leeren Bestand gedacht: Ist er nicht leer, steht über dem Absenden ein Hinweis mit Pflicht-Kästchen
        s += _wiso_stock_confirmation(db.wiso_import_stock())
        s += '''
            <br>
            <button type="submit" id="wisoFdbButton" class="coloredButton bg-green">&#x1F5C4;&#xFE0F; Aus WISO-Datenbank importieren</button>
            <p id="wisoFdbStatus" role="status" hidden></p>
        </form>
        <script>
        (function() {
            // Erst die Dateien hochladen (roh, je eine Anfrage; die Antwort nennt die Kennung des Zwischenordners), dann
            // das Formular mit dieser Kennung absenden. Der Dateiname des Browsers geht nie an den Server.
            const MAX_BYTES = __MAX_BYTES__;
            const form = document.getElementById('wisoFdbForm');
            const client = document.getElementById('wisoFdbClient');
            const standard = document.getElementById('wisoFdbStandard');
            const hidden = document.getElementById('wisoFdbUpload');
            const button = document.getElementById('wisoFdbButton');
            const status = document.getElementById('wisoFdbStatus');
            const meta = document.querySelector('meta[name="csrf-token"]');
            let busy = false;        // ein Upload läuft
            let sending = false;     // zweiter submit-Durchlauf: Das Formular geht jetzt wirklich ab

            function show(text, isError) {
                status.hidden = false;
                status.className = isError ? 'errorColor' : 'hint';
                status.textContent = text;
            }
            function fail(text) {
                busy = false;
                button.disabled = false;
                show(text, true);
            }
            function upload(file, slot, token, onDone) {
                const name = slot === 'client' ? 'DB1.FDB' : 'DB0.FDB';
                const xhr = new XMLHttpRequest();
                xhr.open('POST', '/wiso/fdb-upload?slot=' + slot + (token ? '&upload=' + encodeURIComponent(token) : ''));
                xhr.setRequestHeader('Content-Type', 'application/octet-stream');
                if (meta && meta.content) xhr.setRequestHeader('X-CSRF-Token', meta.content);
                xhr.upload.addEventListener('progress', function(event) {
                    if (event.lengthComputable && event.total > 0) {
                        show(name + ' wird hochgeladen … ' + Math.floor(100 * event.loaded / event.total) + ' %', false);
                    }
                });
                function cancelled() {
                    fail('Der Upload wurde abgebrochen. Ist die Datei größer, als ein vorgeschalteter Proxy erlaubt?');
                }
                xhr.addEventListener('error', cancelled);
                xhr.addEventListener('abort', cancelled);
                xhr.addEventListener('load', function() {
                    let answer = null;
                    try { answer = JSON.parse(xhr.responseText); } catch (e) { answer = null; }
                    if (xhr.status === 200 && answer && answer.success === true && typeof answer.upload === 'string') {
                        onDone(answer.upload);
                    } else {
                        fail(answer && typeof answer.error === 'string' && answer.error
                             ? answer.error : 'Der Upload ist fehlgeschlagen (Status ' + xhr.status + ').');
                    }
                });
                xhr.send(file);
            }
            function send(token) {
                hidden.value = token;
                show('Import läuft …', false);
                if (meta && meta.content) {
                    let field = form.querySelector('input[name="csrf"]');
                    if (!field) {
                        field = document.createElement('input');
                        field.type = 'hidden';
                        field.name = 'csrf';
                        form.appendChild(field);
                    }
                    field.value = meta.content;
                }
                if (form.checkValidity && !form.checkValidity()) {
                    // Während des Uploads geändert (z. B. das Pflicht-Kästchen): nichts absenden, neu versuchen lassen
                    fail('Bitte die Angaben im Formular prüfen und erneut absenden.');
                    if (form.reportValidity) form.reportValidity();
                    return;
                }
                sending = true;
                if (form.requestSubmit) form.requestSubmit(); else form.submit();
            }
            form.addEventListener('submit', function(event) {
                if (sending) return;
                event.preventDefault();
                if (busy) return;
                const first = client.files && client.files[0];
                const second = standard.files && standard.files[0];
                if (!first) {
                    show('Bitte die Mandantendatei DB1.FDB wählen.', true);
                    return;
                }
                const limit = Math.floor(MAX_BYTES / 1073741824) + ' GB';
                if (first.size > MAX_BYTES || (second && second.size > MAX_BYTES)) {
                    show((first.size > MAX_BYTES ? 'DB1.FDB' : 'DB0.FDB') + ' ist größer als ' + limit
                         + ' und lässt sich nicht hochladen.', true);
                    return;
                }
                busy = true;
                button.disabled = true;
                upload(first, 'client', '', function(token) {
                    if (second) upload(second, 'standard', token, function() { send(token); });
                    else send(token);
                });
            });
        })();
        </script>
        '''.replace('__MAX_BYTES__', _script_json(_wiso_upload.MAX_WISO_DB_BODY))
    s += '''
        <p class="hint">Der Import ist auf den Stand abgestimmt, den WISO Mein Büro
        derzeit schreibt (Firebird ODS&nbsp;12). Weicht Ihre Version davon ab, meldet
        die Prüfung vor dem Schreiben, welche Tabellen oder Spalten fehlen &ndash; es
        wird dann <strong>nichts</strong> importiert. In dem Fall lässt sich das
        Importmodul unter <code>importers/wiso_fdb/</code> mit KI-Unterstützung an
        Ihre Version anpassen; die Meldung nennt genau die betroffenen Stellen.</p>
        <script>
        (function() {
            const p = new URLSearchParams(window.location.search);
            const status = p.get('wiso_fdb');
            if (!status) return;
            const div = document.createElement('div');
            div.style = 'margin-top:10px; padding:8px 14px; border-radius:4px; display:inline-block;';
            let msg;
            if (status === 'ok') {
                const missing = parseInt(p.get('missing_coa') || '0');
                const hints   = parseInt(p.get('hints') || '0');
                msg = '\\u2705 Import abgeschlossen: ' + p.get('imported')
                    + ' Buchungss\\u00e4tze, ' + p.get('skipped') + ' \\u00fcbersprungen, '
                    + p.get('assets') + ' Anlagen mit ' + p.get('afa') + ' AfA-Zeilen';
                const lk = parseInt(p.get('linked') || '0');
                const cc = parseInt(p.get('created_coa') || '0');
                const rn = parseInt(p.get('invoices') || '0');
                const kd = parseInt(p.get('customers') || '0');
                const zl = parseInt(p.get('payments') || '0');
                const ad = parseInt(p.get('adopted') || '0');
                const pd = parseInt(p.get('possible') || '0');
                const rg = parseInt(p.get('regrouped') || '0');
                const op = parseInt(p.get('orphans') || '0');
                if (ad > 0) msg += ', ' + ad + ' aus dem CSV-Import wiedererkannt';
                if (pd > 0) msg += ', ' + pd + ' vorhandene Buchungen m\\u00f6glicherweise doppelt \\u2013 siehe Liste unten';
                if (rg > 0) msg += ', ' + rg + ' neu angelegte Buchungen wiedererkannt';
                if (op > 0) msg += ', ' + op + ' Buchungen nicht mehr in WISO vorhanden \\u2013 siehe Liste unten';
                if (rn > 0) msg += ', ' + rn + ' Rechnungen mit '
                    + p.get('items') + ' Positionen';
                if (kd > 0) msg += ', ' + kd + ' Kunden';
                if (zl > 0) msg += ', ' + zl + ' Zahlungen zugeordnet';
                if (lk > 0) msg += ', ' + lk + ' Bankbewegungen verkn\\u00fcpft';
                if (cc > 0) msg += ', ' + cc + ' SKR-Konten angelegt';
                if (missing > 0) msg += ', ' + missing + ' Konten ohne Zuordnung';
                div.style.background = (missing || hints || op) ? '#fff3cd' : '#d4edda';
                div.style.color      = (missing || hints || op) ? '#856404' : '#155724';
            } else if (status === 'incompatible') {
                div.style.background = '#fff3cd';
                div.style.color = '#856404';
                msg = '\\u26a0\\ufe0f Diese WISO-Datenbank passt nicht zum Importmodul: '
                    + miscMsg
                    + '. Es wurde nichts importiert.';
            } else {
                div.style.background = '#f8d7da';
                div.style.color = '#721c24';
                msg = '\\u274c Fehler: ' + miscMsg;
            }
            div.append(msg);
            document.currentScript.parentNode.insertBefore(div, document.currentScript);
        })();
        </script>
        '''

    if _fdb:
        _blocker = _fdb.get('blocker') or []
        _hints = _fdb.get('hints') or []
        _missing = _fdb.get('missing_coa') or {}
        _created = _fdb.get('created_coa') or {}
        if _fdb.get('file'):
            s += (f'<p>\U0001F4C4 Zuletzt gelesen: '
                  f'<code>{_html.escape(str(_fdb["file"]))}</code></p>')
        if _fdb.get('adopted'):
            s += (f'<p>✅ {_html.escape(str(_fdb["adopted"]))} Buchungssätze waren '
                  'schon aus dem CSV-Import da: Sie haben jetzt die WISO-Kennung; '
                  'leere Konten, Steuer und Empfänger wurden ergänzt, sie wurden aber nicht '
                  'doppelt angelegt.</p>')
            _adopted_rows = _fdb_result_rows(_fdb.get('adopted_rows'))
            if _adopted_rows:
                s += ('<details><summary>Welche Buchungen das waren</summary>'
                      + _fdb_rows_table(_adopted_rows))
                if len(_adopted_rows) >= MAX_ADOPTED_ROWS:
                    s += f'<p>Genannt werden die ersten {MAX_ADOPTED_ROWS}.</p>'
                s += '</details>'
        _possible_rows = _fdb_result_rows(_fdb.get('possible_duplicates'))
        if _possible_rows:
            _possible_count = _fdb.get('possible_duplicates_count', len(_possible_rows))
            s += ('<p>Diese Buchungen gleichen einem neu angelegten Satz in Datum, Konto und '
                  'Betrag, aber nicht im Text bzw. Gegenkonto. Bitte prüfen und eine von beiden '
                  'löschen, falls es derselbe Vorgang ist.</p>'
                  f'<details><summary>Möglicherweise doppelt ({_html.escape(str(_possible_count))})'
                  '</summary>' + _fdb_rows_table(_possible_rows))
            if len(_possible_rows) >= MAX_ADOPTED_ROWS:
                s += f'<p>Genannt werden die ersten {MAX_ADOPTED_ROWS}.</p>'
            s += '</details>'
        s += _import_notices(_fdb)
        _conflict_rows = _fdb_result_rows(_fdb.get('form_conflicts'))
        if _conflict_rows:
            _conflict_count = _result_count(_fdb.get('form_conflicts_count')) or len(_conflict_rows)
            s += ('<p>Diese schon importierten Sätze sind anders gespeichert, als dieser Import sie '
                  'schreiben würde (z.&nbsp;B. weil ein Konto inzwischen ein Geldkonto ist oder nicht '
                  'mehr). Sie wurden <strong>nicht</strong> geändert &ndash; bitte prüfen.</p>'
                  f'<details><summary>Abweichende Form ({_html.escape(str(_conflict_count))})'
                  '</summary><table><tr><th>Datum</th><th>Betrag</th><th>Text</th>'
                  '<th>Buchungs-ID</th><th>Gespeichert</th><th>Neu</th></tr>')
            for _row in _conflict_rows:
                try:
                    _amount = fmt_eur(_row.get('amount'))
                except Exception:                       # Ergebnisdatei von Hand verändert
                    _amount = str(_row.get('amount'))
                s += (f'<tr><td>{_html.escape(str(_row.get("date") or ""))}</td>'
                      f'<td>{_html.escape(_amount)}</td>'
                      f'<td>{_html.escape(str(_row.get("text") or ""))}</td>'
                      f'<td>{_html.escape(str(_row.get("id") or ""))}</td>'
                      f'<td>{_html.escape(str(_row.get("stored") or ""))}</td>'
                      f'<td>{_html.escape(str(_row.get("fresh") or ""))}</td></tr>')
            s += '</table>'
            if len(_conflict_rows) >= MAX_ADOPTED_ROWS:
                s += f'<p>Genannt werden die ersten {MAX_ADOPTED_ROWS}.</p>'
            s += '</details>'
        if _blocker:
            s += ('<h3>\u26a0\ufe0f Diese Datenbank passt nicht zum Importmodul</h3>'
                  '<p>Es wurde <strong>nichts</strong> importiert. Die folgenden '
                  'Tabellen oder Spalten erwartet <code>importers/wiso_fdb/</code>, '
                  'Ihre WISO-Version liefert sie aber nicht. Mit dieser Liste lässt '
                  'sich das Modul per KI anpassen:</p><ul>')
            for _b in _blocker:
                s += f'<li>{_html.escape(str(_b))}</li>'
            s += '</ul>'
        if _hints:
            s += (f'<h3>\u2139\ufe0f Teilweise übersprungen &ndash; {len(_hints)} Hinweise</h3>'
                  '<p>Diese Teile fehlen in Ihrer WISO-Datenbank; der Rest wurde '
                  'importiert.</p><ul>')
            for _h in _hints:
                s += f'<li>{_html.escape(str(_h))}</li>'
            s += '</ul>'
        _invoice_warnings = _fdb.get('invoice_warnings') or []
        if _invoice_warnings:
            s += (f'<h3>⚠️ Auffälligkeiten bei Rechnungen '
                  f'&ndash; {len(_invoice_warnings)}</h3>'
                  '<p>Die Rechnungen sind übernommen; diese Punkte lohnen '
                  'einen Blick:</p><ul>')
            for _w in _invoice_warnings[:40]:
                s += f'<li>{_html.escape(str(_w))}</li>'
            if len(_invoice_warnings) > 40:
                s += f'<li>… und {len(_invoice_warnings) - 40} weitere</li>'
            s += '</ul>'
        _asset_warnings = _fdb.get('asset_warnings') or []
        if _asset_warnings:
            s += (f'<h3>\u26a0\ufe0f Auff\u00e4lligkeiten im Anlagenverzeichnis '
                  f'&ndash; {len(_asset_warnings)}</h3>'
                  '<p>Diese Anlageg\u00fcter sind in WISO in sich nicht stimmig oder lassen sich '
                  'nicht mit dem AfA-Plan der App abgleichen. Wo ein Wert gerechnet werden '
                  'musste, wurde der gerechnete \u00fcbernommen; nachsehen lohnt sich '
                  'trotzdem:</p><ul>')
            for _w in _asset_warnings:
                s += f'<li>{_html.escape(str(_w))}</li>'
            s += '</ul>'
        if _missing:
            s += (f'<h3>\u26a0\ufe0f Konten ohne SKR04-Zuordnung &ndash; {len(_missing)}</h3>'
                  '<p>WISO führt diese Konten intern in SKR03, nennt dafür aber keine '
                  'SKR04-Entsprechung. Die Buchungen sind importiert, aber auf dieser '
                  'Seite unkontiert. Bitte <a href="/masterdata/skr">im Kontenrahmen</a> '
                  'anlegen und den Buchungen zuweisen:</p>')
            s += '<table><tr><th>SKR03</th><th>Bezeichnung in WISO</th><th>Buchungen</th></tr>'
            for _nr, _info in _missing.items():
                if isinstance(_info, dict):
                    _anz, _bez = _info.get('anzahl', ''), _info.get('bezeichnung', '')
                else:
                    _anz, _bez = _info, ''
                s += (f'<tr><td>{_html.escape(str(_nr))}</td>'
                      f'<td>{_html.escape(str(_bez))}</td>'
                      f'<td>{_html.escape(str(_anz))}</td></tr>')
            s += '</table>'
        if _created:
            s += (f'<h3>\u2705 Neu angelegte SKR-Konten &ndash; {len(_created)}</h3>'
                  '<p>Nummer und Bezeichnung stammen aus WISOs eigener '
                  'Umschlüsselung.</p><table><tr><th>SKR04</th><th>Bezeichnung</th></tr>')
            for _nr, _name in _created.items():
                s += (f'<tr><td>{_html.escape(str(_nr))}</td>'
                      f'<td>{_html.escape(str(_name))}</td></tr>')
            s += '</table>'
    s += '\t</div>'

    # ── Datensicherung ────────────────────────────────────────────────────────
    from server.backup import downloadable_backups as _downloadable_backups, list_backups as _list_backups
    _backups = _list_backups(_userctx.user_data_dir())

    s += '\t<div class="rectRounded">'
    s += '''
        <h2>&#x1F4BE; Datensicherung</h2>
        <p>Sichert die Daten des angemeldeten Benutzers als Archiv in den Ordner <code>backup/</code> des Datenordners
        (7-Zip, ohne 7-Zip als <code>.zip</code>). Die Datenbank wird dabei als konsistenter Snapshot gesichert.
        Vorhandene Sicherungen lassen sich unten herunterladen.</p>
        <form method="POST" action="/backup/create">
            <div class="rowWithObjects">
                <label><input type="radio" name="scope" value="db"> nur DB</label>
                <label><input type="radio" name="scope" value="all" checked> alle Daten</label>
                <button type="submit" class="coloredButton bg-blue">&#x1F4BE; Backup erstellen</button>
            </div>
        </form>
        '''
    # Vorhandene Sicherungen zum Herunterladen: Archive und Kopien vor einer Programm-Umstellung (Positivliste in
    # ``backup.downloadable_backups``). Jeder Wert aus dem Dateisystem wird maskiert; der Link trägt den Namen prozentkodiert.
    _entries = _downloadable_backups(_userctx.user_data_dir())
    if _entries:
        s += ('<h3>Vorhandene Sicherungen</h3><table class="backupTable"><tr><th>Datei</th><th>Art</th><th>Angelegt</th>'
              '<th class="num">Größe</th><th></th></tr>')
        for _entry in _entries:
            _kind = 'Sicherung' if _entry.kind == 'archive' else f'vor Umstellung auf {_entry.version} (nur Datenbank)'
            _link = _html.escape('/backup/download?file=' + _quote(_entry.name, safe=''), quote=True)
            s += (f'<tr><td>{_html.escape(_entry.name)}</td><td>{_html.escape(_kind)}</td>'
                  f'<td>{_html.escape(_date_label(_entry.mtime))}</td>'
                  f'<td class="num">{_html.escape(_size_label(_entry.size))}</td>'
                  f'<td><a href="{_link}">Herunterladen</a></td></tr>')
        s += '</table>'
        if any(_entry.kind == 'migration' for _entry in _entries):
            s += ('<p class="hint">Sicherungen vor einer Programm-Umstellung lassen sich nur herunterladen; '
                  'zurückspielen wie im README beschrieben.</p>')
    else:
        s += '<p class="muted">Noch keine Sicherungen vorhanden.</p>'
    s += '''
        <script>
        (function() {
            const p = new URLSearchParams(window.location.search);
            const status = p.get('backup');
            if (!status) return;
            let text, type;
            if (status === 'ok') {
                const kb = Math.round(parseInt(p.get('size') || '0') / 1024);
                text = 'Backup erstellt: ' + p.get('file') + ' (' + kb + ' KB)'; type = 'success';
            } else {
                text = 'Backup fehlgeschlagen: ' + miscMsg; type = 'error';
            }
            document.addEventListener('DOMContentLoaded', () => appMsg(text, type));
        })();
        </script>
        '''
    s += '\t</div>'

    # ── Wiederherstellung ─────────────────────────────────────────────────────
    s += '\t<div class="rectRounded">'
    s += '<h2>&#x267B;&#xFE0F; Wiederherstellung</h2>'
    if _backups:
        _opts = ''.join(f'<option value="{_html.escape(b)}">{_html.escape(b)}</option>' for b in _backups)
        s += f'''
        <p>Stellt ein Archiv aus <code>backup/</code> im Benutzerverzeichnis wieder her.
        Sicherungen mit „nur DB“ lassen sich nur ohne „vorher löschen“ zurückspielen.</p>
        <form method="POST" action="/backup/restore"
              onsubmit="return confirm('Backup wirklich wiederherstellen? Aktuelle Daten werden ersetzt!')">
            <div class="rowWithObjects">Archiv:&nbsp;<select name="archive">{_opts}</select></div>
            <div class="rowWithObjects">
                <label><input type="radio" name="mode" value="wipe"> vorhandene Daten vorher löschen</label>
                <label><input type="radio" name="mode" value="overwrite" checked> vorhandene Daten überschreiben</label>
            </div>
            <br>
            <button type="submit" class="coloredButton bg-red">&#x267B;&#xFE0F; Wiederherstellen</button>
        </form>
        '''
    else:
        s += '<p class="muted">Noch keine Backups vorhanden.</p>'
    s += '''
        <script>
        (function() {
            const p = new URLSearchParams(window.location.search);
            const status = p.get('restore');
            if (!status) return;
            let text, type;
            if (status === 'ok') {
                text = 'Backup wiederhergestellt: ' + p.get('file'); type = 'success';
            } else {
                text = 'Wiederherstellung fehlgeschlagen: ' + miscMsg; type = 'error';
            }
            document.addEventListener('DOMContentLoaded', () => appMsg(text, type));
        })();
        </script>
        '''
    s += '\t</div>'
    s += '</div>'

    s += Footer()
    return s
