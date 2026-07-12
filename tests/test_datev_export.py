# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""
Tests für den DATEV-Export, speziell die Ableitung des BU-/Steuerschlüssels
aus Steuersatz + Soll/Haben (datev._taxrate_to_bu) sowie ein End-to-End-Export
über mehrere Buchungen. Der Steuerschlüssel wird NICHT gespeichert, sondern beim
Export aus dem Steuersatz und der Buchungsrichtung abgeleitet.

Inhalte (Beträge/Belegnummern) sind zufällig/anonymisiert.
"""
import csv as _csv
import io
import random
import uuid

from export import datev
from export.datev import _taxrate_to_bu

_C_BU = 8  # Spaltenindex BU-Schlüssel im DATEV-Buchungsstapel


def test_taxrate_to_bu_mapping():
    # Ausgabe/Vorsteuer (Soll)
    assert _taxrate_to_bu(0.19, 'S') == '9'
    assert _taxrate_to_bu(0.07, 'S') == '8'
    # Einnahme/Umsatzsteuer (Haben)
    assert _taxrate_to_bu(0.19, 'H') == '3'
    assert _taxrate_to_bu(0.07, 'H') == '2'
    # 0 % bzw. kein Schlüssel → '0' (z.B. WISO-Schlüssel 490)
    assert _taxrate_to_bu(0.0, 'S') == '0'
    assert _taxrate_to_bu(0.0, 'H') == '0'
    assert _taxrate_to_bu(None, 'S') == '0'


def _bu_values(csv_bytes):
    """BU-Schlüssel aller Datenzeilen aus dem DATEV-CSV extrahieren."""
    text = csv_bytes.decode('cp1252')
    lines = [ln for ln in text.split('\r\n') if ln]
    # Zeile 0 = EXTF-Kopf, Zeile 1 = Spaltenüberschriften, ab Zeile 2 = Daten
    out = []
    for ln in lines[2:]:
        fields = next(_csv.reader([ln], delimiter=';', quotechar='"'))
        if len(fields) > _C_BU:
            out.append(fields[_C_BU])
    return out


def test_export_derives_bu_per_booking(db_with_coa):
    db = db_with_coa
    coa = {r[2]: r[0] for r in db.fetch_chart_of_accounts()}
    doc1 = f"D{random.randint(1000, 9999)}"
    doc2 = f"D{random.randint(1000, 9999)}"
    gross19 = round(random.uniform(50, 500), 2)
    net0 = round(random.uniform(50, 500), 2)

    # Ausgabe 19 % (negativ → Soll → BU '9')
    db.insert_booking(date_booking='2025-07-03', amount=-gross19,
                      coa_id=coa.get(6815), counter_coa_id=coa.get(1460),
                      tax_rate=0.19, document_number=doc1, booking_type='entry')
    # Ausgabe ohne Steuer (0 % → BU '0')
    db.insert_booking(date_booking='2025-07-03', amount=-net0,
                      coa_id=coa.get(6815), counter_coa_id=coa.get(1460),
                      tax_rate=0.0, document_number=doc2, booking_type='entry')

    bookings = db.fetch_bookings_range('2025-07-01', '2025-07-31')
    coa_map = db.get_coa_id_to_number_map()
    csv_bytes, exported_ids = datev.export_to_datev(
        bookings, coa_map, '2025-07-01', '2025-07-31')

    assert len(exported_ids) == 2
    bu = _bu_values(csv_bytes)
    assert '9' in bu  # 19 % Ausgabe
    assert '0' in bu  # 0 %
