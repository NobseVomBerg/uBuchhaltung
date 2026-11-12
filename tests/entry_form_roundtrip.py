# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Eine Maske öffnen, abschicken wie der Browser und vergleichen, was sich geändert hat.

Die Seite wird gerendert (``PageTransactions``), das Formular so ausgelesen, wie ein Browser es
abschickt (jedes benannte Feld, auch in ausgeblendeten Zeilen, ausgewähltes Element eines
Auswahlfelds), und an ``handle_add_transaction`` übergeben – derselbe Weg wie im Betrieb. Für
„vorhandene Buchung zuordnen“ wird das Skript der Seite (``splitAdopt``) nachgebildet.

Alle Namen und Werte sind erfunden.
"""
from contextlib import closing
from html.parser import HTMLParser

from server.handlers import handle_add_transaction
from server.pages_transactions import PageTransactions

ACTION = '/transactions/add'


class _Form(HTMLParser):
    """Felder des Formulars ``/transactions/add`` in Dokumentreihenfolge, plus die Optionen der
    Auswahl „vorhandene Buchung zuordnen“ (``#adoptSelect``)."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.fields = []              # [(Name, Wert)]
        self.adopt_options = []       # [dict(value, amount, docnr, text, coa, rate, tax)]
        self._in_form = False
        self._select = None           # [Name, [(Wert, ausgewählt)], ist_adopt]
        self._option = None           # [Wert, ausgewählt, Text]
        self._textarea = None         # [Name, [Text]]

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == 'form':
            self._in_form = a.get('action') == ACTION
        elif tag == 'select':
            self._select = [a.get('name'), [], a.get('id') == 'adoptSelect', 'disabled' in a]
        elif tag == 'option' and self._select is not None:
            self._option = [a.get('value'), 'selected' in a, '', a]
        elif tag == 'textarea' and self._in_form:
            self._textarea = [a.get('name'), [], 'disabled' in a]
        elif tag == 'input' and self._in_form:
            name, kind = a.get('name'), (a.get('type') or 'text').lower()
            if not name or 'disabled' in a:
                return
            if kind in ('submit', 'button', 'reset', 'image', 'file'):
                return
            if kind in ('checkbox', 'radio') and 'checked' not in a:
                return
            self.fields.append((name, a.get('value') if a.get('value') is not None
                                else ('on' if kind in ('checkbox', 'radio') else '')))

    def handle_data(self, data):
        if self._option is not None:
            self._option[2] += data
        elif self._textarea is not None:
            self._textarea[1].append(data)

    def handle_endtag(self, tag):
        if tag == 'form':
            self._in_form = False
        elif tag == 'option' and self._option is not None and self._select is not None:
            value, selected, text, attrs = self._option
            value = value if value is not None else ' '.join(text.split())
            self._select[1].append((value, selected))
            if self._select[2]:
                self.adopt_options.append({
                    'value': value, 'amount': attrs.get('data-amount', ''),
                    'docnr': attrs.get('data-docnr', ''), 'text': attrs.get('data-text', ''),
                    'coa': attrs.get('data-coa', ''), 'rate': attrs.get('data-rate', ''),
                    'tax': attrs.get('data-tax', '')})
            self._option = None
        elif tag == 'select' and self._select is not None:
            name, options, is_adopt, disabled = self._select
            self._select = None
            if self._in_form and name and not disabled and not is_adopt and options:
                chosen = [value for value, selected in options if selected] or [options[0][0]]
                self.fields.append((name, chosen[-1]))
        elif tag == 'textarea' and self._textarea is not None:
            name, parts, disabled = self._textarea
            self._textarea = None
            if name and not disabled:
                self.fields.append((name, ''.join(parts)))


def open_form(db, booking_id):
    """Maske eines Buchungssatzes öffnen: (abgeschickte Felder wie ``parse_qs``, Optionen der
    Auswahl „vorhandene Buchung zuordnen“)."""
    parser = _Form()
    parser.feed(PageTransactions(db, edit_transaction_id=booking_id))
    post = {}
    for name, value in parser.fields:
        post.setdefault(name, []).append(value)
    return post, parser.adopt_options


def submit(db, post):
    status, location = handle_add_transaction(db, post)
    assert status == 303, (status, location)


def add_row(post, **values):
    """Eine neue Zeile des Buchungssatz-Editors vor der (leeren) Vorlagenzeile, wie ``splitAdd`` und
    ``splitAdopt`` sie anlegen. ``values``: ``split_id``, ``split_amount``, ``split_coa``, ``split_text``,
    ``split_docnr``, ``split_tax_rate``, ``split_tax_amount``, ``split_counter_coa``; was fehlt, bleibt leer."""
    result = {name: list(items) for name, items in post.items()}
    template = {name: result[name].pop() for name in list(result) if name.startswith('split_')}
    row = dict(template, **{name: str(value) for name, value in values.items()})
    for name, value in row.items():
        result[name] += [value, template[name]]
    return result


def adopt_row(post, option, **row):
    """Das Skript der Seite (``splitAdopt``): eine Zeile aus der Vorlage mit den Angaben der
    gewählten Buchung – Betrag, Beleg-Nr., Text, Zweckkonto, Steuersatz und -betrag. ``row`` überschreibt
    Felder der Zeile (``split_amount=…``), wie wenn der Nutzer sie danach ändert."""
    values = dict(split_id=option['value'], split_amount=option['amount'],
                  split_docnr=option['docnr'], split_text=option['text'],
                  split_tax_rate=option.get('rate', ''), split_tax_amount=option.get('tax', ''))
    if option['coa']:
        values['split_coa'] = option['coa']
    values.update(row)
    return add_row(post, **values)


def drop_row(post, row_id):
    """Die Zeile mit dieser ``split_id`` aus dem Formular nehmen (✕ in der Zeile)."""
    result = {name: list(values) for name, values in post.items()}
    index = result['split_id'].index(str(row_id))
    for name in [n for n in result if n.startswith('split_')]:
        del result[name][index]
    return result


# ── Vergleich ─────────────────────────────────────────────────────────────────

def snapshot(db):
    """Alle Spalten aller Buchungen, {ID: {Spalte: Wert}}. Ein leerer Text gilt wie kein Text –
    ein Formular kann beides nicht unterscheiden."""
    with closing(db._get_connection()) as conn:
        columns = [row[1] for row in conn.execute('PRAGMA table_info(Bookings)')]
        rows = conn.execute('SELECT * FROM Bookings ORDER BY ID').fetchall()
    return {row[0]: {column: (None if value == '' else value)
                     for column, value in zip(columns, row)} for row in rows}


def differences(before, after, names=None, ignore=()):
    """Lesbare Liste der Unterschiede zweier Schnappschüsse; Kontonummern statt COA-IDs, wenn
    ``names`` ({COA-ID: Nummer}) mitgegeben ist. ``ignore``: Spalten, die nicht verglichen werden."""
    names = names or {}

    def show(column, value):
        if column in ('COA_ID', 'CounterCOA_ID') and value is not None:
            return names.get(value, value)
        return value

    found = []
    for booking_id in sorted(set(before) | set(after)):
        if booking_id not in after:
            found.append(f'Buchung {booking_id} ist weg')
        elif booking_id not in before:
            found.append(f'Buchung {booking_id} ist neu')
        else:
            for column, value in before[booking_id].items():
                if column not in ignore and after[booking_id][column] != value:
                    label = before[booking_id].get('DocumentNumber') or booking_id
                    found.append(f'{label} ({booking_id}): {column} {show(column, value)!r} -> '
                                 f'{show(column, after[booking_id][column])!r}')
    return found


# ── die Zeilen des Editors, wie das Skript der Seite sie rechnet ───────────────

class _Rows(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows = []
        self._row = None
        self._select = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == 'tr' and 'splitRow' in (a.get('class') or '').split():
            self._row = {'nobank': 'data-nobank' in a,
                         'sign': float(a['data-sign']) if 'data-sign' in a else 1.0,
                         'id': None, 'amount': 0.0, 'coa': None}
        elif self._row is not None and tag == 'input':
            if a.get('name') == 'split_id':
                self._row['id'] = a.get('value') or None
            elif a.get('name') == 'split_amount':
                self._row['amount'] = float(a.get('value') or 0)
        elif self._row is not None and tag == 'select' and a.get('name') == 'split_coa':
            self._select = True
        elif self._row is not None and self._select and tag == 'option' and 'selected' in a:
            self._row['coa'] = int(a['value']) if a.get('value') else None

    def handle_endtag(self, tag):
        if tag == 'select':
            self._select = None
        elif tag == 'tr' and self._row is not None:
            self.rows.append(self._row)
            self._row = None


def editor_rows(db, bank_id):
    """(Zeilen des Editors, Betrag der Bankbewegung) – ohne die Vorlagenzeile."""
    import re
    page = PageTransactions(db, edit_transaction_id=bank_id)
    parser = _Rows()
    parser.feed(page)
    amount = float(re.search(r'SPLIT_BANK_AMOUNT = (-?[\d.]+);', page).group(1))
    return parser.rows, amount


def editor_rest(db, bank_id):
    """Der Rest, den die Summenzeile des Editors zeigt (``splitRecalc``): Betrag der Bankbewegung minus die
    Summe der Zeilen ohne ``data-nobank``, jede mit ihrem ``data-sign``."""
    rows, amount = editor_rows(db, bank_id)
    total = sum(row['sign'] * row['amount'] for row in rows if not row['nobank'])
    return round(amount - total, 2)
