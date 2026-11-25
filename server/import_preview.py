# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""
Vorschau-Logik für den Kontoauszug-Import.

Reine, schreibfreie Funktionen: aus den geparsten Belegdaten wird je Datei
eine Vorschau berechnet (Konto-Zuordnung, Duplikat-Zähler, Parse-Hinweise),
die die Transaktionsseite inline anzeigt.

Die Duplikatprüfung hat EINE Stelle, ``classify_duplicates``: Vorschau, Import
(``handlers.handle_confirm_import``) und die Prüfung von ``force`` rechnen mit ihr.
Duplikat = Konto + Buchungsdatum + Betrag, zählbasiert; die Bankreferenz ändert die
Zählung nie, sie macht nur ein verworfenes Duplikat verdächtig (``ref_differs``).
"""


def file_transactions(file_data):
    """Die Umsätze eines Belegs in der Reihenfolge, in der Vorschau UND Import (``handlers.handle_confirm_import``) sie
    durchlaufen. Die Position darin ist der ``index`` eines Umsatzes – damit nennt das Feld ``selection`` von
    ``/confirm_transactions`` genau die Umsätze, die die Vorschau gezeigt hat. Die EINE Stelle, die diese Liste liest."""
    return file_data.get("transactions") or []


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


def _text_key(value):
    """Verwendungszweck für den Vergleich: ohne jeden Leerraum, Groß-/Kleinschreibung egal,
    ohne ein führendes „Storno:“.

    Ohne Leerraum, weil ein Zeilenumbruch mitten im Wort stehen kann („Rechnungsnum“,
    Umbruch, „mer 4711“); „Storno:“ setzt die Bank bei Rückbuchungen davor (geprüft wird
    nach dem Entfernen des Leerraums, also auch „Storno : …“).
    """
    key = "".join((value or "").split()).casefold()
    return key[len("storno:"):] if key.startswith("storno:") else key


def _texts_agree(a, b):
    """Können zwei (mit ``_text_key`` aufbereitete) Verwendungszwecke derselbe Umsatz sein?

    Ja, wenn sie gleich sind, einer der Anfang des anderen ist (PDF und CAMT kürzen
    verschieden) oder einer leer ist – der leere Text ist der Anfang jedes Textes.
    """
    return a.startswith(b) or b.startswith(a)


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


def duplicate_rows(transactions, account_id, amounts):
    """Die Umsätze einer Datei, die die Duplikatprüfung betrifft, als ``[(index, key, ref)]`` in Dateireihenfolge: nur mit Konto (``account_id``) und mit Datum und Betrag.

    ``key`` = ``(Datum, Betrag auf zwei Stellen gerundet, Konto)``, ``ref`` = die Bankreferenz des Umsatzes (``''`` = keine; durch ``clean_bank_text``, wie im
    Bestand). ``amounts`` merkt je Schlüssel den ROHEN Betrag des ersten Umsatzes: mit ihm liest ``bank_stock_for`` den Bestand (wie ``check_booking_exists``, dieselbe Bedingung)."""
    from importers.base import clean_bank_text
    rows = []
    if account_id is None:
        return rows
    for index, t in enumerate(transactions):
        date, amount = t.get("date"), t.get("amount")
        if not date or amount is None:
            continue
        key = (date, round(float(amount), 2), account_id)
        amounts.setdefault(key, amount)
        rows.append((index, key, clean_bank_text(t.get("bank_ref"))))
    return rows


def bank_stock_for(db, amounts, inserted_ids=()):
    """``stock_for`` für ``classify_duplicates``: der Bestand der Datenbank zu einem Schlüssel (``Database.bank_stock``), je Schlüssel EINMAL gelesen – beim ersten Zugriff.

    ``[(Quelle, Referenz)]`` nach Buchungs-ID: Quelle ``upload`` für eine Buchung aus ``inserted_ids`` (der Lauf hat sie selbst angelegt), sonst ``db``."""
    cache = {}

    def stock_for(key):
        if key not in cache:
            date, _amount, account_id = key
            cache[key] = [("upload" if booking_id in inserted_ids else "db", ref)
                          for booking_id, ref in db.bank_stock(date, amounts[key], account_id)]
        return cache[key]

    return stock_for


def _claim(stock, used, wanted):
    """Belegt die erste freie Bestandszeile, deren Referenz ``wanted`` erfüllt (``used`` merkt die belegten): ``True``, wenn es sie gab."""
    for m, (_source, stock_ref) in enumerate(stock):
        if not used[m] and wanted(stock_ref):
            used[m] = True
            return True
    return False


def classify_duplicates(rows, stock_for):
    """Ordnet die Umsätze EINER Datei ein: ``{index: (dup_source, ref_differs)}``.

    ``rows`` = ``[(index, key, ref)]`` in Dateireihenfolge (``duplicate_rows``), ``stock_for(key)`` = der Bestand zum Schlüssel als ``[(Quelle, Referenz)]`` – Quelle
    ``db`` vor ``upload`` (``bank_stock_for``; die Vorschau hängt die neuen Umsätze früherer Dateien als ``upload`` an).

    ``dup_source`` ist die Zählung nach Konto, Datum und Betrag und hängt von keiner Referenz ab: Je Schlüssel sind die ersten ``min(Umsätze, Bestand)`` Umsätze Duplikate, ihre
    Quelle die der Bestandszeile an derselben Position (``db`` | ``upload``); die übrigen sind neu (``None``). Von selbst wird nie anders gezählt.

    ``ref_differs`` markiert nur: Ein Umsatz, den diese Zählung als Duplikat verwirft, trägt es, wenn er eine eigene Referenz hat und nach zwei Durchgängen keine Bestandszeile belegt.
    Durchgang 1: Jeder Umsatz mit Referenz belegt die erste freie Zeile mit genau dieser Referenz. Durchgang 2, die übrigen in Dateireihenfolge: Ein Umsatz mit Referenz nimmt die
    erste freie Zeile OHNE Referenz; einer ohne Referenz zuerst die erste freie MIT Referenz, sonst die erste ohne. Ohne Referenz auf einer Seite ist nie ein Umsatz verdächtig.
    Kein Abgleich über ein anderes Datum oder Konto."""
    by_key = {}
    for index, key, ref in rows:
        by_key.setdefault(key, []).append((index, ref))
    verdicts = {}
    for key, group in by_key.items():
        stock = list(stock_for(key))
        used = [False] * len(stock)
        no_ref = lambda stock_ref: not stock_ref
        matched = [bool(ref) and _claim(stock, used, lambda stock_ref: stock_ref == ref) for _index, ref in group]       # Durchgang 1
        for n, (_index, ref) in enumerate(group):                                                                       # Durchgang 2
            if not matched[n]:
                matched[n] = _claim(stock, used, no_ref) if ref else _claim(stock, used, bool) or _claim(stock, used, no_ref)
        for n, (index, ref) in enumerate(group):
            dup_source = stock[n][0] if n < len(stock) else None
            verdicts[index] = (dup_source, dup_source is not None and not matched[n] and bool(ref))
    return verdicts


class UploadClassifier:
    """Die Duplikatprüfung über die Dateien EINES Uploads, Datei für Datei in ihrer Reihenfolge – so rechnet die Vorschau, und so prüft der Import vorab, ob ``force`` nur nennt,
    was die Vorschau verdächtig nannte (Bestand der Datenbank, nur einmal je Schlüssel gelesen, dazu die neuen Umsätze der früheren Dateien als Quelle ``upload``; über mehrere Dateien gilt
    damit das Maximum, nicht die Summe). Schreibt nichts."""

    def __init__(self, db):
        self._amounts = {}                              # Schlüssel -> roher Betrag des ersten Umsatzes damit, über alle Dateien
        self._db_stock = bank_stock_for(db, self._amounts)
        self._planned = {}                              # Schlüssel -> [('upload', Referenz)]: neu in früheren Dateien

    def classify(self, transactions, account_id):
        """Die Umsätze der nächsten Datei einordnen: ``(rows, verdicts)`` – ``duplicate_rows`` und ``classify_duplicates`` dazu. Die neuen Umsätze zählen danach für die folgenden Dateien.
        Eine Datei ohne Konto (``account_id`` ``None``) hat keine Zeilen und bleibt ohne Wirkung."""
        rows = duplicate_rows(transactions, account_id, self._amounts)
        verdicts = classify_duplicates(rows, lambda key: self._db_stock(key) + self._planned.get(key, []))
        for index, key, ref in rows:
            if verdicts[index][0] is None:
                self._planned.setdefault(key, []).append(("upload", ref))
        return rows, verdicts


def build_import_preview(db, import_data):
    """Pro Beleg eine Vorschau berechnen.

    import_data: { "files": [ {filename, bank_code, iban, document_date,
                               currency, warnings, transactions: [...]}, ... ] }

    Returns { "files": [ {file_index, filename, bank_code, iban, document_date,
                          currency, account_id, account_name, selectable, total, new_count,
                          dup_count, dup_upload_count, dup_upload_differs_count, status,
                          warnings, problems, transactions} ] }.

    Jeder Umsatz trägt seinen ``index`` (Position in ``file_transactions``) und ``importable``
    (weder Duplikat noch unvollständig – nur ein solcher Umsatz lässt sich auswählen).
    ``selectable``: Das Konto ist gefunden und lässt eine Auswahl einzelner Umsätze zu
    (``Database.account_allows_selection``: gemischt genutztes Konto, keine Kasse).

    dup_count zählt alle möglichen Duplikate; dup_upload_count davon die, die
    nicht im Bestand stecken, sondern in einer anderen Datei desselben Uploads
    (bei Transaktionen: "dup_source" = "db" | "upload" | None), und
    dup_upload_differs_count davon die, deren Verwendungszweck zu keinem Umsatz
    der früheren Dateien passt ("text_differs"; nur ein Hinweis, der Import
    entscheidet allein nach Konto/Datum/Betrag).

    Ein Duplikat bleibt ein Duplikat, auch wenn Umsatz und Bestandszeile verschiedene
    Bankreferenzen tragen: Es ist dann nur verdächtig ("ref_differs" und "forceable"
    wahr, Befund "ref_differs"; ``classify_duplicates``) – der Nutzer kann es über das Feld
    ``force`` von ``/confirm_transactions`` doch anlegen lassen. ``importable`` bleibt falsch.

    Ein Umsatz ohne Datum oder Betrag legt der Import nicht an: Er zählt weder
    als neu noch als Duplikat und trägt den Befund "incomplete".

    status: "error" = Konto nicht gefunden (Import blockiert),
            "warn"  = Duplikate oder Parse-Hinweise vorhanden,
            "ok"    = sauber.
    Schreibt nichts in die Datenbank.
    """
    accounts = db.fetch_accounts()
    files_out = []

    # Die Duplikatzählung je Datei und über die Dateien (``UploadClassifier``): derselbe Bestand
    # wie beim Bestätigungs-Import, nur ohne zu schreiben. prior_texts hält je Schlüssel die
    # Verwendungszwecke der Umsätze früherer Dateien (nur für den Hinweis "text_differs").
    classifier = UploadClassifier(db)
    prior_texts = {}

    for idx, f in enumerate(import_data.get("files", [])):
        iban = f.get("iban")
        account_id, account_name = match_account(accounts, iban)
        txns = file_transactions(f)
        rows, verdicts = classifier.classify(txns, account_id)
        keys = {index: key for index, key, _ref in rows}

        new_count = 0
        dup_count = 0
        dup_upload_count = 0
        dup_upload_differs_count = 0
        has_warn = False
        problems = []
        txns_out = []
        file_texts = {}  # Schlüssel -> Verwendungszwecke dieser Datei (landen in prior_texts)

        for index, t in enumerate(txns):
            date = t.get("date")
            amount = t.get("amount")
            recipient = t.get("recipient") or ""
            reference = t.get("reference") or ""
            foreign_iban = t.get("foreign_iban") or ""

            warns = _transaction_warnings(t, f.get("document_date"))
            # Ohne Datum oder Betrag legt handle_confirm_import nichts an.
            incomplete = not date or amount is None
            if incomplete:
                warns.append("incomplete")
            dup_source, ref_differs = verdicts.get(index, (None, False))
            if ref_differs:
                warns.append("ref_differs")
            if warns:
                has_warn = True

            text_differs = False
            if index in keys:
                text = _text_key(reference)
                if dup_source == "upload":
                    text_differs = not any(
                        _texts_agree(text, other)
                        for other in prior_texts.get(keys[index], ()))
                file_texts.setdefault(keys[index], []).append(text)
            is_dup = dup_source is not None
            if is_dup:
                dup_count += 1
                if dup_source == "upload":
                    dup_upload_count += 1
                    if text_differs:
                        dup_upload_differs_count += 1
            elif not incomplete:
                new_count += 1

            row = {
                "index": index,
                "importable": not is_dup and not incomplete,
                "date": date,
                "recipient": recipient,
                "reference": reference,
                "amount": amount,
                "foreign_iban": foreign_iban,
                "dup": is_dup,
                "dup_source": dup_source,
                "text_differs": text_differs,
                "ref_differs": ref_differs,
                "forceable": ref_differs,
                "warn": warns,
            }
            txns_out.append(row)
            if is_dup or warns:
                problems.append(row)

        # file_texts füllt sich nur bei Dateien mit Konto: eine ohne Konto wird nicht
        # importiert und zählt deshalb nicht zu dem, was folgende Dateien finden.
        for dup_key, texts in file_texts.items():
            prior_texts.setdefault(dup_key, []).extend(texts)

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
            "currency": f.get("currency"),
            "account_id": account_id,
            "account_name": account_name,
            "selectable": account_id is not None and db.account_allows_selection(account_id),
            "total": len(txns),
            "new_count": new_count,
            "dup_count": dup_count,
            "dup_upload_count": dup_upload_count,
            "dup_upload_differs_count": dup_upload_differs_count,
            "status": status,
            "warnings": list(f.get("warnings") or []),
            "problems": problems,
            "transactions": txns_out,
        })

    return {"files": files_out}
