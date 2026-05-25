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


def _transaction_warnings(date, amount, recipient, reference):
    """Parse-Auffälligkeiten einer Buchung sammeln."""
    warns = []
    if amount is None or amount == 0:
        warns.append("amount")
    if not date:
        warns.append("date")
    if not recipient and not reference:
        warns.append("empty")
    return warns


def build_import_preview(db, import_data):
    """Pro Beleg eine Vorschau berechnen.

    import_data: { "files": [ {filename, bank_code, iban, document_date,
                               transactions: [...]}, ... ] }

    Returns { "files": [ {file_index, filename, bank_code, iban, document_date,
                          account_id, account_name, total, new_count, dup_count,
                          status, problems, transactions} ] }.

    status: "error" = Konto nicht gefunden (Import blockiert),
            "warn"  = Duplikate oder Parse-Hinweise vorhanden,
            "ok"    = sauber.
    Schreibt nichts in die Datenbank.
    """
    accounts = db.fetch_accounts()
    files_out = []

    for idx, f in enumerate(import_data.get("files", [])):
        iban = f.get("iban")
        account_id, account_name = match_account(accounts, iban)
        txns = f.get("transactions", []) or []

        new_count = 0
        dup_count = 0
        has_warn = False
        problems = []
        txns_out = []

        for t in txns:
            date = t.get("date")
            amount = t.get("amount")
            recipient = t.get("recipient") or ""
            reference = t.get("reference") or ""
            foreign_iban = t.get("foreign_iban") or ""

            warns = _transaction_warnings(date, amount, recipient, reference)
            if warns:
                has_warn = True

            is_dup = False
            if account_id is not None and date and amount is not None:
                is_dup = db.check_booking_exists(
                    date, amount, account_id, foreign_iban, reference
                )
            if is_dup:
                dup_count += 1
            else:
                new_count += 1

            row = {
                "date": date,
                "recipient": recipient,
                "reference": reference,
                "amount": amount,
                "foreign_iban": foreign_iban,
                "dup": is_dup,
                "warn": warns,
            }
            txns_out.append(row)
            if is_dup or warns:
                problems.append(row)

        if account_id is None:
            status = "error"
        elif dup_count > 0 or has_warn:
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
            "status": status,
            "problems": problems,
            "transactions": txns_out,
        })

    return {"files": files_out}
