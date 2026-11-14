# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""
DATEV Buchungsstapel – Import/Export-Modul

Unterstützte DATEV-Version: EXTF 700, Kategorie 21 (Buchungsstapel)
Encoding: Windows-1252 (CP1252) – Standard für DATEV-Dateien
Zeilenumbruch: CRLF

Der Stapel ist dem nachgebildet, den WISO Mein Büro schreibt – gemessen an einem Datenbestand, keine Vorgabe von
DATEV und keine Zusicherung, dass die Kanzlei ihn so annimmt (siehe ``db/datev_keys.py``).

Spaltenbelegung (0-basiert, 124 Spalten gesamt):
  0  Umsatz (ohne S/H)        – Betrag als dt. Dezimalzahl, kein Vorzeichen
  1  Soll/Haben-Kennzeichen   – "S" oder "H"
  2  WKZ Umsatz               – Währungscode, z.B. "EUR"
  3  Kurs                     – Wechselkurs (leer = 1,00)
  4  Basis-Umsatz             – dt. Dezimalzahl
  5  WKZ Basis-Umsatz         – Währungscode Basis
  6  Konto                    – SKR-Kontonummer (4-stellig, ohne Anführungszeichen)
  7  Gegenkonto               – Gegenkonto-SKR-Nummer
  8  BU-Schlüssel             – der an der Buchung gespeicherte Schlüssel (``Bookings.DatevKey``), sonst nach der
                                Art des Zweckkontos abgeleitet, "0" ohne Schlüssel
  9  Belegdatum               – DDMM (4 Stellen, Jahr aus Kopfzeile)
 10  Belegfeld 1              – Belegnummer ohne Leerzeichen (höchstens 36 Zeichen)
 11  Belegfeld 2              – (leer)
 12  Skonto                   – dt. Dezimalzahl
 13  Buchungstext             – max. 60 Zeichen; Zeilenumbrüche, Tabulatoren und andere Steuerzeichen werden zu Leerzeichen
 14  Postensperre             – 0
 18  Zinssperre               – 0
 40  EU-Steuersatz            – der Steuersatz ("19,00") an einer Umsatzsteuer-Zeile, sonst leer
105  Skontosperre             – 0
113  Festschreibung           – 0
114  Leistungsdatum           – DDMMYYYY, nur an der Forderung einer Rechnung (Leistungsdatum der Rechnung, sonst
                                Buchungsdatum)
115  Datum Zuord. Steuerperiode – leer (WISO belegt sie nicht)
119  Land                     – ISO-Code, nur an der Rechnungskette

Soll/Haben (Spalte 1) bezieht sich auf das Feld „Konto“ und folgt der Form des
Buchungssatzes, die er beim Schreiben bekommen hat (``Bookings.EntryForm``, Spalte 22 von
``SELECT * FROM Bookings``; ``db.matching.soll_side``): Steht bei positivem Betrag das Feld
„Konto“ im Soll, ist das Kennzeichen S bei positivem Betrag, sonst H. Nur eine Zeile ohne Form
(per Roh-SQL angelegt) wird mit den Zahlungskonten ``liquid_coa_ids`` eingeordnet; ohne sie
gilt allein das Vorzeichen.

Der BU-Schlüssel (Spalte 8) steht als Daten am Satz (``Bookings.DatevKey``, Spalte 23), wenn die Quelle der Buchung
ihn mitgebracht hat (Import); der Export gibt ihn unverändert aus. Sonst leitet ``db.datev_keys`` ihn ab: nach der
Art des Zweckkontos (Erlöskonto Umsatzsteuer, jedes andere Vorsteuer – auch bei einer Erstattung), nach dem exakten
Steuersatz (19, 7, 16, 5 %; in den befristeten Sätzen von Juli bis Dezember 2020 tauschen die Plätze) und nur mit
einem Satz. Die Rechnungskette (Forderung, „Wartekonto an Erlöskonto“, Gegenbuchung einer Stornorechnung) trägt
keinen Schlüssel, aber in Spalte 40 den Satz; ein Spiegel trägt keinen. Die Umbuchung „Aufwand an Anlagekonto“ (AfA) ist
von der Art Vorsteuer und trägt nur deshalb keinen Schlüssel, weil sie keinen Steuersatz hat.
"""
import datetime
import unicodedata

from db import datev_keys
from db.matching import ENTRY_FORMS, SIDE_COA, SIDE_COUNTER, read_entry_form, soll_side

# ─── Spaltenindizes ───────────────────────────────────────────────────────────
_C_AMOUNT      = 0
_C_SH          = 1
_C_WKZ         = 2
_C_KURS        = 3
_C_BASIS       = 4
_C_WKZ_BASIS   = 5
_C_KONTO       = 6
_C_GEGENKONTO  = 7
_C_BU          = 8
_C_BELEGDATUM  = 9
_C_BELEGFELD1  = 10
_C_BELEGFELD2  = 11
_C_SKONTO      = 12
_C_TEXT        = 13
_C_EU_RATE     = 40
_C_LEISTUNG    = 114
_C_STEUERDATUM = 115
_C_STEUERSATZ  = 118
_C_LAND        = 119

#: Spalten der Buchungszeile (``SELECT * FROM Bookings``), die der Export über die Pflichtspalten hinaus liest
_ROW_ENTRY_FORM = 22
_ROW_DATEV_KEY = 23

#: Berater- und Mandantennummer im Vorlauf, solange die eigene Firma keine hinterlegt hat
PLACEHOLDER_CONSULTANT_NO = '70000'
PLACEHOLDER_CLIENT_NO = '99999'

TOTAL_COLS = 124

# Spalten mit Text-Typ: gequotet wenn leer → ""; gequotet wenn gefüllt → "Wert"
# Alle übrigen Spalten: numerisch/roh – leer bleibt leer (kein Anführungszeichen)
_TEXT_COLS = frozenset([
    1, 2, 5, 8, 10, 11, 13,
    15, 16, 17, 19,
    *range(20, 36),   # Beleginfo Art/Inhalt 1-8
    36, 37,           # KOST1, KOST2
    39, 41, 42, 43, 46,
    *range(47, 87),   # Zusatzinformation Art/Inhalt 1-20
    94, 95, 97, 98,
    101, 102, 103, 104,
    106, 107, 108, 109, 110, 111,
    119, 120, 121, 122,
])

# Standardwerte (unverändert für alle exportierten Zeilen)
_ROW_DEFAULTS = {
    4:   '0,00',  # Basis-Umsatz
    12:  '0,00',  # Skonto
    14:  '0',     # Postensperre
    18:  '0',     # Zinssperre
    105: '0',     # Skontosperre
    113: '0',     # Festschreibung
}


def _fmt_amount(value) -> str:
    """float → deutsches Dezimalformat ohne Vorzeichen, z.B. '27,20'."""
    return f"{abs(float(value or 0)):.2f}".replace('.', ',')


def _fmt_ddmm(date_str: str) -> str:
    """'YYYY-MM-DD' → 'DDMM' (4-stellig, kein Trennzeichen)."""
    if not date_str:
        return ''
    try:
        d = datetime.date.fromisoformat(str(date_str)[:10])
        return d.strftime('%d%m')
    except (ValueError, TypeError):
        return ''


def _fmt_ddmmyyyy(date_str: str) -> str:
    """'YYYY-MM-DD' → 'DDMMYYYY' (8-stellig, kein Trennzeichen)."""
    if not date_str:
        return ''
    try:
        d = datetime.date.fromisoformat(str(date_str)[:10])
        return d.strftime('%d%m%Y')
    except (ValueError, TypeError):
        return ''


def _text_field(text) -> str:
    """Buchungstext für Spalte 13: Zeilenumbrüche, Tabulatoren und andere Steuerzeichen (auch die Zeilen- und Absatztrenner
    von Unicode) werden je Zeichen zu einem Leerzeichen – ein Umbruch im Text bräche sonst die Zeile des Stapels –, danach
    höchstens 60 Zeichen."""
    return ''.join(' ' if unicodedata.category(char) in ('Cc', 'Zl', 'Zp') else char
                   for char in str(text or ''))[:60]


def _fmt_percent(rate) -> str:
    """Steuersatz → Prozent mit zwei Nachkommastellen und Komma, z.B. '19,00' (Spalte 40)."""
    return f"{datev_keys.percent(rate):.2f}".replace('.', ',')


def _country_field(country) -> str:
    """Land der Rechnung für Spalte 119: zwei Buchstaben als ISO-Code in Großbuchstaben; leer, „D“ und
    „Deutschland“ sind „DE“; alles andere (längere oder unlesbare Angaben) bleibt leer – nie ein roher Wert im
    Stapel."""
    text = str(country or '').strip()
    if text.casefold() in ('', 'd', 'deutschland'):
        return 'DE'
    return text.upper() if len(text) == 2 and text.isascii() and text.isalpha() else ''


def _invoice_info(invoices, number):
    """``(Leistungsdatum, Land)`` der Rechnung mit dieser Beleg-Nr. aus ``invoices`` – oder ``None``."""
    info = invoices.get(number) if number else None
    return info if isinstance(info, (tuple, list)) and len(info) == 2 else None


def _build_row(cols: list) -> str:
    """124 Spaltenwerte → DATEV-Zeile (Semikolon-getrennt, korrekte Anführungszeichen)."""
    parts = []
    for i, v in enumerate(cols):
        if v is None or v == '':
            parts.append('""' if i in _TEXT_COLS else '')
        else:
            sv = str(v)
            if i in _TEXT_COLS:
                parts.append('"' + sv.replace('"', '""') + '"')
            else:
                parts.append(sv)
    return ';'.join(parts)


# ─── Kopfzeile (Zeile 1) ──────────────────────────────────────────────────────
def _make_meta_row(date_from: str, date_to: str, consultant_no=None, client_no=None) -> str:
    """DATEV-Metadatenzeile (erste Zeile der Datei).

    Berater- und Mandantennummer stammen aus den Stammdaten der eigenen Firma; sie gelten nur beide und nur als
    Ziffernfolgen (Berater 4–7, Mandant 1–5 Stellen), sonst stehen die Platzhalter da."""
    consultant, client = datev_keys.consultant_no(consultant_no), datev_keys.client_no(client_no)
    if not (consultant and client):
        consultant, client = PLACEHOLDER_CONSULTANT_NO, PLACEHOLDER_CLIENT_NO
    ts = datetime.datetime.now().strftime('%Y%m%d%H%M%S000')
    wj_start = date_from[:4] + '0101'         # Wirtschaftsjahresbeginn
    d_from = date_from.replace('-', '')        # YYYYMMDD
    d_to   = date_to.replace('-', '')
    parts = [
        '"EXTF"', '700', '21', '"Buchungsstapel"', '12',
        ts, '', '""', '"uBuchhaltung"', '""',
        consultant, client, wj_start, '4',
        d_from, d_to, '""', '""', '1', '0', '0', '"EUR"',
        '', '""', '', '', '""', '0', '', '', '""',
    ]
    return ';'.join(parts)


# ─── Spaltenüberschriften (Zeile 2) ───────────────────────────────────────────
DATEV_COL_HEADER = (
    '"Umsatz (ohne Soll/Haben-Kz)";"Soll/Haben-Kennzeichen";"WKZ Umsatz";"Kurs";'
    '"Basis-Umsatz";"WKZ Basis-Umsatz";"Konto";"Gegenkonto (ohne BU-Schlüssel)";'
    '"BU-Schlüssel";"Belegdatum";"Belegfeld 1";"Belegfeld 2";"Skonto";"Buchungstext";'
    '"Postensperre";"Diverse Adressnummer";"Geschäftspartnerbank";"Sachverhalt";"Zinssperre";'
    '"Beleglink";"Beleginfo - Art 1";"Beleginfo - Inhalt 1";"Beleginfo - Art 2";'
    '"Beleginfo - Inhalt 2";"Beleginfo - Art 3";"Beleginfo - Inhalt 3";"Beleginfo - Art 4";'
    '"Beleginfo - Inhalt 4";"Beleginfo - Art 5";"Beleginfo - Inhalt 5";"Beleginfo - Art 6";'
    '"Beleginfo - Inhalt 6";"Beleginfo - Art 7";"Beleginfo - Inhalt 7";"Beleginfo - Art 8";'
    '"Beleginfo - Inhalt 8";"KOST1 - Kostenstelle";"KOST2 - Kostenstelle";"Kost-Menge";'
    '"EU-Land u. UStID";"EU-Steuersatz";"Abw. Versteuerungsart";"Sachverhalt L+L";'
    '"Funktionsergänzung L+L";"BU 49 Hauptfunktionstyp";"BU 49 Hauptfunktionsnummer";'
    '"BU 49 Funktionsergänzung";"Zusatzinformation - Art 1";"Zusatzinformation- Inhalt 1";'
    '"Zusatzinformation - Art 2";"Zusatzinformation- Inhalt 2";"Zusatzinformation - Art 3";'
    '"Zusatzinformation- Inhalt 3";"Zusatzinformation - Art 4";"Zusatzinformation- Inhalt 4";'
    '"Zusatzinformation - Art 5";"Zusatzinformation- Inhalt 5";"Zusatzinformation - Art 6";'
    '"Zusatzinformation- Inhalt 6";"Zusatzinformation - Art 7";"Zusatzinformation- Inhalt 7";'
    '"Zusatzinformation - Art 8";"Zusatzinformation- Inhalt 8";"Zusatzinformation - Art 9";'
    '"Zusatzinformation- Inhalt 9";"Zusatzinformation - Art 10";"Zusatzinformation- Inhalt 10";'
    '"Zusatzinformation - Art 11";"Zusatzinformation- Inhalt 11";"Zusatzinformation - Art 12";'
    '"Zusatzinformation- Inhalt 12";"Zusatzinformation - Art 13";"Zusatzinformation- Inhalt 13";'
    '"Zusatzinformation - Art 14";"Zusatzinformation- Inhalt 14";"Zusatzinformation - Art 15";'
    '"Zusatzinformation- Inhalt 15";"Zusatzinformation - Art 16";"Zusatzinformation- Inhalt 16";'
    '"Zusatzinformation - Art 17";"Zusatzinformation- Inhalt 17";"Zusatzinformation - Art 18";'
    '"Zusatzinformation- Inhalt 18";"Zusatzinformation - Art 19";"Zusatzinformation- Inhalt 19";'
    '"Zusatzinformation - Art 20";"Zusatzinformation- Inhalt 20";"Stück";"Gewicht";'
    '"Zahlweise";"Forderungsart";"Veranlagungsjahr";"Zugeordnete Fälligkeit";"Skontotyp";'
    '"Auftragsnummer";"Buchungstyp";"Ust-Schlüssel (Anzahlungen)";"EU-Land (Anzahlungen)";'
    '"Sachverhalt L+L (Anzahlungen)";"EU-Steuersatz (Anzahlungen)";"Erlöskonto (Anzahlungen)";'
    '"Herkunft-Kz";"Leerfeld";"KOST-Datum";"Mandatsreferenz";"Skontosperre";'
    '"Gesellschaftername";"Beteiligtennummer";"Identifikationsnummer";"Zeichnernummer";'
    '"Postensperre bis";"Bezeichnung SoBil-Sachverhalt";"Kennzeichen SoBil-Buchung";'
    '"Festschreibung";"Leistungsdatum";"Datum Zuord. Steuerperiode";"Fälligkeit";'
    '"Generalumkehr";"Steuersatz";"Land";"Abrechnungsreferenz";'
    '"BVV-Position (Betriebsvermögensvergleich)";"EU-Mitgliedstaat u. UStID (Ursprung)";'
    '"EU-Steuersatz (Ursprung)"'
)


# ─── Export ───────────────────────────────────────────────────────────────────
def export_to_datev(bookings, coa_id_to_number: dict,
                    date_from: str, date_to: str,
                    liquid_coa_ids=None, *,
                    waiting_numbers=(), invoices=None,
                    consultant_no=None, client_no=None) -> tuple:
    """
    Buchungen nach DATEV Buchungsstapel-Format exportieren.

    :param bookings:          Iterable von Bookings-Tupeln (SELECT * FROM Bookings):
                              0=ID, 1=DateBooking, 2=DateTax, 3=BookingGroup_ID,
                              4=Account_ID, 5=ForeignBankAccount, 6=RecipientClient,
                              7=Contact_ID, 8=COA_ID, 9=CounterCOA_ID, 10=Category_ID,
                              11=Amount, 12=Currency, 13=TaxRate, 14=TaxAmount,
                              15=Text, 16=DocumentNumber, 22=EntryForm (optional),
                              23=DatevKey (optional)
    :param coa_id_to_number:  Dict {coa_id (int): account_number (int/str)}
    :param date_from:         'YYYY-MM-DD' – Anfang des Buchungszeitraums
    :param date_to:           'YYYY-MM-DD' – Ende des Buchungszeitraums
    :param liquid_coa_ids:    COA-IDs der Zahlungskonten (``db.get_payment_coa_ids()``);
                              nur für Zeilen ohne gespeicherte Form: daran erkennt der Export,
                              auf welcher Seite des Satzes das Geldkonto steht. Ohne sie (None)
                              gilt für solche Zeilen allein das Vorzeichen.
    :param waiting_numbers:   Kontonummern der Wartekonten (``db.get_waiting_account_numbers()``):
                              Daran erkennt der Export die Rechnungskette. Ohne sie gibt es keine.
    :param invoices:          ``{Beleg-Nr.: (Leistungsdatum, Land)}`` der Rechnungen
                              (``db.get_datev_invoice_data()``) für Spalte 114 und 119. Ohne Rechnung zur
                              Beleg-Nr. gilt für die Forderung das Buchungsdatum und für die Kette das Land „DE“.
    :param consultant_no:     Berater- und Mandantennummer der eigenen Firma (``db.get_datev_numbers()``),
    :param client_no:         beide gültig oder beide Platzhalter im Vorlauf.
    :returns:                 (csv_bytes in CP1252, list_of_exported_booking_ids)
    """
    lines = [_make_meta_row(date_from, date_to, consultant_no, client_no), DATEV_COL_HEADER]
    exported_ids = []
    waiting = tuple(waiting_numbers or ())
    invoices = invoices or {}

    for row in bookings:
        booking_id      = row[0]
        date_booking    = row[1]
        coa_id          = row[8]
        counter_coa_id  = row[9]   # CounterCOA_ID → Gegenkonto
        amount          = row[11]
        currency        = row[12] or 'EUR'
        tax_rate        = row[13]
        text            = row[15] or ''
        doc_number      = row[16] or ''

        if amount is None:
            continue

        amount_f = float(amount)
        is_inflow = amount_f >= 0
        stored_form = row[_ROW_ENTRY_FORM] if len(row) > _ROW_ENTRY_FORM else None
        if stored_form in ENTRY_FORMS or liquid_coa_ids is not None:
            form = read_entry_form(stored_form, coa_id, counter_coa_id, liquid_coa_ids)
            side = soll_side(form)
        else:
            form = None                          # keine Form, keine Geldkonten: allein das Vorzeichen
            side = SIDE_COUNTER
        # S/H-Kennzeichen gehört zum Feld „Konto“ und hängt daran, ob dieses Konto bei positivem
        # Betrag im Soll steht (Geldkonto vorne, Umbuchung, Spiegel mit Betrag auf dem Konto) oder
        # im Haben (Zweckkonto vorne: Einnahme = Konto im Haben, Ausgabe = Konto im Soll).
        sh = 'S' if is_inflow == (side == SIDE_COA) else 'H'
        konto_number    = coa_id_to_number.get(coa_id) if coa_id else None
        gegen_number    = coa_id_to_number.get(counter_coa_id) if counter_coa_id else None
        konto      = str(coa_id_to_number.get(coa_id, '')) if coa_id else ''
        gegenkonto = str(coa_id_to_number.get(counter_coa_id, '')) if counter_coa_id else ''

        # BU-Schlüssel: der an der Buchung gespeicherte (nur 1 bis 9999; alles andere zählt als keiner), sonst
        # nach der Art des Zweckkontos, dem Steuersatz und dem Buchungsdatum abgeleitet – nicht nach der
        # Richtung der Geldbewegung (db.datev_keys).
        stored_key = (datev_keys.parse_key(row[_ROW_DATEV_KEY]) if len(row) > _ROW_DATEV_KEY else None)
        kind = datev_keys.booking_kind(form, konto_number, gegen_number, is_inflow, waiting)
        key = stored_key if stored_key is not None else datev_keys.derive_key(kind, tax_rate, date_booking)
        invoice = _invoice_info(invoices, doc_number)

        cols = [None] * TOTAL_COLS
        # Standardwerte setzen
        for k, v in _ROW_DEFAULTS.items():
            cols[k] = v

        cols[_C_AMOUNT]      = _fmt_amount(amount_f)
        cols[_C_SH]          = sh
        cols[_C_WKZ]         = currency
        cols[_C_KONTO]       = konto
        cols[_C_GEGENKONTO]  = gegenkonto
        cols[_C_BU]          = str(key)
        cols[_C_BELEGDATUM]  = _fmt_ddmm(date_booking)
        cols[_C_BELEGFELD1]  = datev_keys.beleg_field(doc_number)
        cols[_C_TEXT]        = _text_field(text)
        # Spalte 40: der Satz an einer Umsatzsteuer-Zeile – auch ohne Schlüssel (Rechnungskette)
        if datev_keys.is_vat_row(key, kind, tax_rate):
            cols[_C_EU_RATE] = _fmt_percent(tax_rate)
        # Spalte 114: das Leistungsdatum nur an der Forderung (sonst leer)
        if datev_keys.is_receivable_row(form, konto_number, gegen_number, waiting):
            cols[_C_LEISTUNG] = (_fmt_ddmmyyyy(invoice[0]) if invoice else '') or _fmt_ddmmyyyy(date_booking)
        # Spalte 115 (Datum Zuord. Steuerperiode) bleibt leer, Spalte 119 trägt das Land nur in der Rechnungskette
        if kind == datev_keys.KIND_INVOICE_CHAIN:
            cols[_C_LAND] = _country_field(invoice[1]) if invoice else 'DE'

        lines.append(_build_row(cols))
        exported_ids.append(booking_id)

    csv_text = '\r\n'.join(lines) + '\r\n'
    return csv_text.encode('cp1252', errors='replace'), exported_ids
