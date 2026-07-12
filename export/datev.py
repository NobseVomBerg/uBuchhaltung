# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""
DATEV Buchungsstapel – Import/Export-Modul

Unterstützte DATEV-Version: EXTF 700, Kategorie 21 (Buchungsstapel)
Encoding: Windows-1252 (CP1252) – Standard für DATEV-Dateien
Zeilenumbruch: CRLF

Spaltenbelegung (0-basiert, 124 Spalten gesamt):
  0  Umsatz (ohne S/H)        – Betrag als dt. Dezimalzahl, kein Vorzeichen
  1  Soll/Haben-Kennzeichen   – "S" oder "H"
  2  WKZ Umsatz               – Währungscode, z.B. "EUR"
  3  Kurs                     – Wechselkurs (leer = 1,00)
  4  Basis-Umsatz             – dt. Dezimalzahl
  5  WKZ Basis-Umsatz         – Währungscode Basis
  6  Konto                    – SKR-Kontonummer (4-stellig, ohne Anführungszeichen)
  7  Gegenkonto               – Gegenkonto-SKR-Nummer
  8  BU-Schlüssel             – Steuerschlüssel (z.B. "9" = 19%, "10" = 7%)
  9  Belegdatum               – DDMM (4 Stellen, Jahr aus Kopfzeile)
 10  Belegfeld 1              – Belegnummer (max. 12 Zeichen)
 11  Belegfeld 2              – (leer)
 12  Skonto                   – dt. Dezimalzahl
 13  Buchungstext             – max. 60 Zeichen
 14  Postensperre             – 0
 18  Zinssperre               – 0
105  Skontosperre             – 0
113  Festschreibung           – 0
114  Leistungsdatum           – DDMMYYYY (Buchungsdatum)
115  Datum Zuord. Steuerperiode – DDMMYYYY (= aktuelles Datum beim Export)
119  Land                     – "DE"
"""
import datetime

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
_C_LEISTUNG    = 114
_C_STEUERDATUM = 115
_C_STEUERSATZ  = 118
_C_LAND        = 119

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



def _taxrate_to_bu(tax_rate, sh: str = 'S') -> str:
    """TaxRate (float/None) + Soll/Haben-Kennzeichen → DATEV BU-Schlüssel.

    S (Ausgabe/Vorsteuer): 19% → '9', 7% → '8'
    H (Einnahme/Umsatzsteuer): 19% → '3', 7% → '2'
    """
    if tax_rate is None:
        return '0'
    r = round(float(tax_rate), 4)
    if r >= 0.19:
        return '3' if sh == 'H' else '9'
    if r >= 0.07:
        return '2' if sh == 'H' else '8'
    return '0'


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
def _make_meta_row(date_from: str, date_to: str) -> str:
    """DATEV-Metadatenzeile (erste Zeile der Datei)."""
    ts = datetime.datetime.now().strftime('%Y%m%d%H%M%S000')
    wj_start = date_from[:4] + '0101'         # Wirtschaftsjahresbeginn
    d_from = date_from.replace('-', '')        # YYYYMMDD
    d_to   = date_to.replace('-', '')
    parts = [
        '"EXTF"', '700', '21', '"Buchungsstapel"', '12',
        ts, '', '""', '"uBuchhaltung"', '""',
        '70000', '99999', wj_start, '4',
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
                    date_from: str, date_to: str) -> tuple:
    """
    Buchungen nach DATEV Buchungsstapel-Format exportieren.

    :param bookings:          Iterable von Bookings-Tupeln (SELECT * FROM Bookings):
                              0=ID, 1=DateBooking, 2=DateTax, 3=BookingGroup_ID,
                              4=Account_ID, 5=ForeignBankAccount, 6=RecipientClient,
                              7=Contact_ID, 8=COA_ID, 9=CounterCOA_ID, 10=Category_ID,
                              11=Amount, 12=Currency, 13=TaxRate, 14=TaxAmount,
                              15=Text, 16=DocumentNumber
    :param coa_id_to_number:  Dict {coa_id (int): account_number (int/str)}
    :param date_from:         'YYYY-MM-DD' – Anfang des Buchungszeitraums
    :param date_to:           'YYYY-MM-DD' – Ende des Buchungszeitraums
    :returns:                 (csv_bytes in CP1252, list_of_exported_booking_ids)
    """
    today_ddmmyyyy = _fmt_ddmmyyyy(datetime.date.today().isoformat())
    lines = [_make_meta_row(date_from, date_to), DATEV_COL_HEADER]
    exported_ids = []

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
        # Vorzeichen → S/H-Kennzeichen: S=Soll/Ausgabe, H=Haben/Einnahme
        sh = 'H' if amount_f >= 0 else 'S'
        konto      = str(coa_id_to_number.get(coa_id, '')) if coa_id else ''
        gegenkonto = str(coa_id_to_number.get(counter_coa_id, '')) if counter_coa_id else ''

        cols = [None] * TOTAL_COLS
        # Standardwerte setzen
        for k, v in _ROW_DEFAULTS.items():
            cols[k] = v

        cols[_C_AMOUNT]      = _fmt_amount(amount_f)
        cols[_C_SH]          = sh
        cols[_C_WKZ]         = currency
        cols[_C_KONTO]       = konto
        cols[_C_GEGENKONTO]  = gegenkonto
        cols[_C_BU]          = _taxrate_to_bu(tax_rate, sh)
        cols[_C_BELEGDATUM]  = _fmt_ddmm(date_booking)
        cols[_C_BELEGFELD1]  = doc_number[:12]
        cols[_C_TEXT]        = text[:60]
        cols[_C_LEISTUNG]    = _fmt_ddmmyyyy(date_booking)
        cols[_C_STEUERDATUM] = today_ddmmyyyy   # Datum Zuord. Steuerperiode = heute

        lines.append(_build_row(cols))
        exported_ids.append(booking_id)

    csv_text = '\r\n'.join(lines) + '\r\n'
    return csv_text.encode('cp1252', errors='replace'), exported_ids
