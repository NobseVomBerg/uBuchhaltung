# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""
Vorschau-Logik für den Kontoauszug-Import.

Reine, schreibfreie Funktionen: aus den geparsten Belegdaten wird je Datei
eine Vorschau berechnet (Konto-Zuordnung, Duplikat-Zähler, Parse-Hinweise),
die die Transaktionsseite inline anzeigt.
"""


def _norm_iban(iban):
    """IBAN für den Vergleich normalisieren (Leerzeichen weg, Großschreibung)."""
    return (iban or "").replace(" ", "").upper()


def match_account(accounts, iban):
    """Bankkonto zu einer IBAN finden.

    accounts: Rückgabe von db.fetch_accounts() (Number/IBAN an Index 3).
    Returns (account_id, account_name) oder (None, None).
    """
    target = _norm_iban(iban)
    if not target:
        return None, None
    for acc in accounts:
        if _norm_iban(acc[3]) == target:
            return acc[0], acc[1]
    return None, None


def _transaction_warnings(transaction, document_date=None):
    """Parse-Auffälligkeiten einer Buchung.

    Die Befunde stammen aus der Plausibilitätsprüfung der Importschicht und
    hängen bereits an der Transaktion. Nur für Import-Dateien, die vor der
    Abstraktionsschicht abgelegt wurden, wird nachträglich geprüft.
    """
    stored = transaction.get("warnings")
    if stored is not None:
        return list(stored)
    from importers.base import StatementTransaction
    from importers.plausibility import check_transaction
    return check_transaction(
        StatementTransaction.from_dict(transaction), document_date)


def build_import_preview(db, import_data):
    """Pro Beleg eine Vorschau berechnen.

    import_data: { "files": [ {filename, bank_code, iban, document_date,
                               transactions: [...]}, ... ] }

    Returns { "files": [ {file_index, filename, bank_code, iban, document_date,
                          account_id, account_name, total, new_count, dup_count,
                          dup_upload_count, status, problems, transactions} ] }.

    dup_count zählt alle möglichen Duplikate; dup_upload_count davon die, die
    nicht im Bestand stecken, sondern in einer anderen Datei desselben Uploads
    (bei Transaktionen: "dup_source" = "db" | "upload" | None).

    status: "error" = Konto nicht gefunden (Import blockiert),
            "warn"  = Duplikate oder Parse-Hinweise vorhanden,
            "ok"    = sauber.
    Schreibt nichts in die Datenbank.
    """
    accounts = db.fetch_accounts()
    files_out = []

    # Zählbasierter Duplikat-Zähler, je Datei (gleiche Logik wie der
    # Bestätigungs-Import, nur ohne zu schreiben): db_counts ist der Bestand je
    # Schlüssel (Datum/Betrag/Konto), planned die Zahl der Umsätze, die frühere
    # Dateien dieses Uploads je Schlüssel neu anlegen würden. Innerhalb einer
    # Datei gilt ein Umsatz als Duplikat, solange sie weniger davon enthält als
    # der Bestand (Quelle "db") bzw. Bestand + planned ("upload"). Über mehrere
    # Dateien gilt damit das Maximum, nicht die Summe.
    db_counts = {}
    planned = {}

    for idx, f in enumerate(import_data.get("files", [])):
        iban = f.get("iban")
        account_id, account_name = match_account(accounts, iban)
        txns = f.get("transactions", []) or []

        new_count = 0
        dup_count = 0
        dup_upload_count = 0
        has_warn = False
        problems = []
        txns_out = []
        seen = {}        # Schlüssel -> Umsätze dieser Datei bisher
        new_here = {}    # Schlüssel -> davon neu (landet nach der Datei in planned)

        for t in txns:
            date = t.get("date")
            amount = t.get("amount")
            recipient = t.get("recipient") or ""
            reference = t.get("reference") or ""
            foreign_iban = t.get("foreign_iban") or ""

            warns = _transaction_warnings(t, f.get("document_date"))
            if warns:
                has_warn = True

            dup_source = None
            if account_id is not None and date and amount is not None:
                dup_key = (date, round(float(amount), 2), account_id)
                if dup_key not in db_counts:
                    db_counts[dup_key] = db.check_booking_exists(
                        date, amount, account_id, foreign_iban, reference
                    )
                position = seen.get(dup_key, 0)
                seen[dup_key] = position + 1
                if position < db_counts[dup_key]:
                    dup_source = "db"
                elif position < db_counts[dup_key] + planned.get(dup_key, 0):
                    dup_source = "upload"
                else:
                    new_here[dup_key] = new_here.get(dup_key, 0) + 1
            is_dup = dup_source is not None
            if is_dup:
                dup_count += 1
                if dup_source == "upload":
                    dup_upload_count += 1
            else:
                new_count += 1

            row = {
                "date": date,
                "recipient": recipient,
                "reference": reference,
                "amount": amount,
                "foreign_iban": foreign_iban,
                "dup": is_dup,
                "dup_source": dup_source,
                "warn": warns,
            }
            txns_out.append(row)
            if is_dup or warns:
                problems.append(row)

        # new_here füllt sich nur bei Dateien mit Konto: eine ohne Konto wird nicht
        # importiert und zählt deshalb nicht zu dem, was folgende Dateien finden.
        for dup_key, n in new_here.items():
            planned[dup_key] = planned.get(dup_key, 0) + n

        if account_id is None:
            status = "error"
        elif dup_count > 0 or has_warn or f.get("warnings"):
            status = "warn"
        else:
            status = "ok"

        files_out.append({
            "file_index": idx,
            "filename": f.get("filename"),
            "bank_code": f.get("bank_code"),
            "iban": iban,
            "document_date": f.get("document_date"),
            "account_id": account_id,
            "account_name": account_name,
            "total": len(txns),
            "new_count": new_count,
            "dup_count": dup_count,
            "dup_upload_count": dup_upload_count,
            "status": status,
            "warnings": list(f.get("warnings") or []),
            "problems": problems,
            "transactions": txns_out,
        })

    return {"files": files_out}
