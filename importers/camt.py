# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Kontoauszug-Import im Format CAMT.053 (ISO 20022, XML).

Unterstützt camt.053.001.02/.04/.08 – gelesen wird über den *lokalen*
Elementnamen, der Namespace spielt keine Rolle. Eine Datei darf mehrere
``<Stmt>`` enthalten, ein ZIP mehrere XML-Dateien. Jedes ``<Stmt>`` wird zu
einem :class:`~importers.base.BankStatement` (``bank_code`` ``CAMT``).

Dateien sind Nutzereingaben und werden entsprechend behandelt:

* XML mit DTD bzw. Entity-Deklaration wird abgelehnt (CAMT enthält nie eine),
* ZIP wird nur im Speicher gelesen, nie entpackt; Anzahl und Größe der
  Einträge sind begrenzt (Zip-Bomb), nur ``.xml``-Einträge zählen,
* Beträge werden als ``Decimal`` aus dem XML-String gelesen, nie über float,
* ein Auszug, der irgendwo eine andere Währung als Euro nennt, wird nicht gelesen
  (die Bewegungen hätten keine Euro-Beträge).

Befund-Schlüssel auf Beleg-Ebene (``BankStatement.warnings``):

====================  =======================================================
``balance``           Anfangssaldo + Umsätze ≠ Schlusssaldo
``no_balance``        Anfangs- oder Schlusssaldo fehlt: nicht geprüft
``split_mismatch``    Einzelbeträge einer Sammelbuchung ≠ Gesamtbetrag
``skipped_status``    nicht gebuchte Umsätze (nicht ``BOOK``) wurden übergangen
``bad_entry``         ein Umsatz war nicht lesbar und wurde übergangen
``foreign_currency``  Auszug nicht in Euro: keine Umsätze übernommen
====================  =======================================================
"""
import io
import re
import zipfile
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import List, Optional, Tuple
from xml.etree import ElementTree as ET

from money import to_minor

from .base import BankStatement, StatementTransaction
from .safe_xml import UnsafeXml, parse_untrusted_xml

BANK_CODE = 'CAMT'

#: Grenzen gegen Zip-Bombs und überlange Eingaben
MAX_XML_BYTES = 20 * 1024 * 1024          # je XML (entpackt)
MAX_ZIP_ENTRIES = 50                      # XML-Einträge je ZIP
MAX_ZIP_TOTAL_BYTES = 100 * 1024 * 1024   # alle XML eines ZIP (entpackt)
MAX_AMOUNT = Decimal('1e12')              # Beträge darüber sind Fehler/Angriff
MAX_ZIP_RATIO = 200                       # entpackt : gepackt


class CamtError(ValueError):
    """Datei ist kein (zulässiges) CAMT.053 – Text ist für Nutzer gedacht."""


# ── XML sicher lesen ─────────────────────────────────────────────────────────

def _parse_xml(data: bytes) -> ET.Element:
    # Der Wächter (Größe, keine DTD/Entity-Deklaration, wohlgeformt) liegt in importers/safe_xml.py; hier bleiben die
    # Meldungen für den Nutzer. Die Grenze wird bei jedem Aufruf gelesen (die Tests verkleinern sie).
    try:
        return parse_untrusted_xml(data, MAX_XML_BYTES)
    except UnsafeXml as e:
        if e.reason == 'too_large':
            raise CamtError('XML-Datei ist zu groß.') from None
        if e.reason == 'dtd':
            raise CamtError('XML mit DTD/Entity-Deklaration wird aus Sicherheitsgründen '
                            'nicht gelesen (CAMT enthält keine).') from None
        raise CamtError(f'Kein gültiges XML: {e.detail}') from None


def _local(tag) -> str:
    return tag.rsplit('}', 1)[-1] if isinstance(tag, str) else ''


def _children(el, name):
    return [c for c in el if _local(c.tag) == name]


def _child(el, name):
    for c in el:
        if _local(c.tag) == name:
            return c
    return None


def _path(el, *names):
    """Erstes Element entlang eines Pfades lokaler Namen (oder None)."""
    for name in names:
        if el is None:
            return None
        el = _child(el, name)
    return el


def _text(el, *names) -> str:
    node = _path(el, *names) if names else el
    return (node.text or '').strip() if node is not None else ''


def _parse_amount(node) -> Decimal:
    raw = (node.text or '').strip() if node is not None else ''
    try:
        value = Decimal(raw)
    except InvalidOperation:
        raise CamtError(f'Betrag nicht lesbar: {raw!r}')
    # Exponenten wie 1E+100 sind endlich, sprengen aber quantize/to_minor
    if not value.is_finite() or abs(value) >= MAX_AMOUNT:
        raise CamtError(f'Betrag nicht lesbar oder unrealistisch groß: {raw!r}')
    return value


def _signed(amount: Decimal, indicator: str) -> Decimal:
    if indicator == 'DBIT':
        return -amount
    if indicator == 'CRDT':
        return amount
    raise CamtError(f'Unbekannte Soll/Haben-Kennung: {indicator!r}')


def _iso_date(el) -> Optional[str]:
    """``Dt`` oder ``DtTm`` eines Datumscontainers als ``YYYY-MM-DD``."""
    if el is None:
        return None
    raw = _text(el, 'Dt') or _text(el, 'DtTm')
    try:
        return datetime.strptime(raw[:10], '%Y-%m-%d').strftime('%Y-%m-%d')
    except ValueError:
        return None


# ── Währung ──────────────────────────────────────────────────────────────────

_CURRENCY_CODE = re.compile(r'[A-Z]{3}')


def _currency(raw: Optional[str]) -> Optional[str]:
    """Die Währung, die eine Angabe nennt: ``None`` = keine Angabe, ``''`` = eine Angabe, die kein
    dreibuchstabiger Code ist, sonst der Code (getrimmt, groß geschrieben)."""
    if raw is None:
        return None
    code = raw.strip().upper()
    return code if _CURRENCY_CODE.fullmatch(code) else ''


def _amount_currency(node) -> Optional[str]:
    """Die Währung am Attribut ``Ccy`` eines ``Amt``-Elements (siehe ``_currency``)."""
    return _currency(node.get('Ccy')) if node is not None else None


def _not_euro(currency: Optional[str]) -> bool:
    """Nennt die Angabe eine Währung, und ist es nicht Euro? Eine unlesbare Angabe (``''``) zählt dazu."""
    return currency is not None and currency != 'EUR'


def _is_booked(entry) -> bool:
    status = _text(entry, 'Sts') or _text(entry, 'Sts', 'Cd')
    return not status or status == 'BOOK'


def _statement_currency(stmt) -> Tuple[bool, Optional[str]]:
    """Lautet der Auszug auf Euro? Liefert (nicht Euro, Währungscode).

    Gelesen werden die Währung des Kontos (``Acct/Ccy``), die der gebuchten Umsätze und die der verwendeten
    Salden (``Amt/@Ccy``). Eine fehlende Angabe nennt keine Währung; nennt irgendeine Stelle eine andere als
    Euro, ist es kein Euro-Auszug – der Code ist dann der erste abweichende (``None``, wenn dieser kein Code ist).
    Sonst ``'EUR'``, wenn irgendwo Euro steht, und ``None``, wenn nirgends eine Währung steht."""
    account = _path(stmt, 'Acct', 'Ccy')
    mentions = [_currency(account.text or '') if account is not None else None]
    mentions += [_amount_currency(_child(entry, 'Amt'))
                 for entry in _children(stmt, 'Ntry') if _is_booked(entry)]
    for codes in (('OPBD', 'PRCD'), ('CLBD',)):
        bal = _balance_node(stmt, codes)
        mentions.append(_amount_currency(_child(bal, 'Amt')) if bal is not None else None)
    for mention in mentions:
        if _not_euro(mention):
            return True, mention or None
    return False, 'EUR' if 'EUR' in mentions else None


# ── Einzelne Umsätze ─────────────────────────────────────────────────────────

def _tx_amount(tx):
    """Eigener Betrag einer ``TxDtls``: ihr ``Amt``.

    Die Beträge unter ``AmtDtls`` (``TxAmt``, ``InstdAmt``) teilen eine Sammelbuchung bewusst NICHT auf: So hat der
    Import solche Einträge immer gelesen – als EINEN Umsatz mit dem Gesamtbetrag –, und die Duplikaterkennung läuft
    über Datum und Betrag. Würden sie jetzt aufgeteilt, fände der erneute Import eines schon eingelesenen Auszugs
    seine Summenzeile nicht wieder und legte die Einzelbeträge zusätzlich an."""
    return _path(tx, 'Amt')


def _reference(entry, tx) -> str:
    lines = []
    if tx is not None:
        rmt = _path(tx, 'RmtInf')
        if rmt is not None:
            lines = [(u.text or '').strip() for u in _children(rmt, 'Ustrd')]
        text = ' '.join(l for l in lines if l)
        if text:
            return text
        text = _text(tx, 'AddtlTxInf')
        if text:
            return text
    return _text(entry, 'AddtlNtryInf')


def _counterparty(tx, indicator: str) -> Tuple[str, str]:
    """(Name, IBAN) der Gegenseite: Dbtr bei Gutschrift, Cdtr bei Lastschrift."""
    if tx is None:
        return '', ''
    rel = _path(tx, 'RltdPties')
    if rel is None:
        return '', ''
    party, acct = ('Dbtr', 'DbtrAcct') if indicator == 'CRDT' else ('Cdtr', 'CdtrAcct')
    name = _text(rel, party, 'Nm') or _text(rel, party, 'Pty', 'Nm')
    iban = _text(rel, acct, 'Id', 'IBAN').replace(' ', '').upper()
    return name, iban


def _entry_transactions(entry, amount: Decimal, indicator: str, date: str,
                        warnings: list) -> List[StatementTransaction]:
    """Ein ``Ntry`` in eine oder – bei Sammelbuchung – mehrere Bewegungen."""
    storno = _text(entry, 'RvslInd').lower() == 'true'
    prefix = 'Storno: ' if storno else ''
    txs = [t for d in _children(entry, 'NtryDtls') for t in _children(d, 'TxDtls')]

    if len(txs) > 1:
        parts = []
        for tx in txs:
            node = _tx_amount(tx)
            # Ein Einzelbetrag in anderer Währung (etwa der beauftragte Betrag in Originalwährung) ist kein
            # eigener Euro-Betrag: Dann bleibt es ein Umsatz mit dem Gesamtbetrag.
            if node is None or _not_euro(_amount_currency(node)):
                parts = None
                break
            tx_ind = _text(tx, 'CdtDbtInd') or indicator
            parts.append((tx, _signed(_parse_amount(node), tx_ind), tx_ind))
        if parts is not None:
            if sum(p[1] for p in parts) == _signed(amount, indicator):
                return [
                    _build(entry, tx, value, ind, date, prefix)
                    for tx, value, ind in parts
                ]
            warnings.append('split_mismatch')
        # Ohne eigene Einzelbeträge: als ein Umsatz mit dem Gesamtbetrag

    tx = txs[0] if len(txs) == 1 else None
    return [_build(entry, tx, _signed(amount, indicator), indicator, date, prefix)]


def _build(entry, tx, value: Decimal, indicator: str, date: str,
           prefix: str) -> StatementTransaction:
    name, iban = _counterparty(tx, 'CRDT' if value > 0 else 'DBIT')
    reference = _reference(entry, tx)
    return StatementTransaction(
        date=date,
        amount=float(value.quantize(Decimal('0.01'))),
        recipient=name,
        reference=(prefix + reference).strip(),
        foreign_iban=iban,
    )


# ── Statements ───────────────────────────────────────────────────────────────

def _balance_node(stmt, codes):
    """Das erste ``Bal`` mit einem dieser Saldo-Codes (oder None) – das, was die Prüfung verwendet."""
    for bal in _children(stmt, 'Bal'):
        if _text(bal, 'Tp', 'CdOrPrtry', 'Cd') in codes:
            return bal
    return None


def _balance(stmt, codes) -> Optional[Decimal]:
    bal = _balance_node(stmt, codes)
    if bal is None:
        return None
    return _signed(_parse_amount(_child(bal, 'Amt')), _text(bal, 'CdtDbtInd'))


def _statement_from(stmt) -> BankStatement:
    iban = (_text(stmt, 'Acct', 'Id', 'IBAN') or _text(stmt, 'Acct', 'Id', 'Othr', 'Id'))
    result = BankStatement(bank_code=BANK_CODE,
                           iban=iban.replace(' ', '').upper() or None)
    foreign, result.currency = _statement_currency(stmt)
    warnings: list = []
    total = Decimal(0)
    dates = []
    skipped_status = False

    for entry in _children(stmt, 'Ntry'):
        if not _is_booked(entry):
            skipped_status = True
            continue
        try:
            amount = _parse_amount(_child(entry, 'Amt'))
            indicator = _text(entry, 'CdtDbtInd')
            total += _signed(amount, indicator)
            date = _iso_date(_child(entry, 'BookgDt')) or _iso_date(_child(entry, 'ValDt'))
            result.transactions.extend(
                _entry_transactions(entry, amount, indicator, date, warnings))
            if date:
                dates.append(date)
        except CamtError:
            warnings.append('bad_entry')

    if skipped_status:
        warnings.append('skipped_status')

    # Plausibilität: Anfangssaldo + Umsätze = Schlusssaldo (Cent-genau)
    try:
        opening = _balance(stmt, ('OPBD', 'PRCD'))
        closing = _balance(stmt, ('CLBD',))
    except CamtError:
        opening = closing = None
        warnings.append('balance')
    if opening is not None and closing is not None:
        if to_minor(opening + total) != to_minor(closing):
            warnings.append('balance')
    elif 'balance' not in warnings:
        warnings.append('no_balance')       # nichts zum Vergleichen: die Vollständigkeit ist ungeprüft

    raw = (_text(stmt, 'FrToDt', 'ToDtTm') or _text(stmt, 'CreDtTm')
           or (max(dates) if dates else ''))
    try:
        result.document_date = datetime.strptime(raw[:10], '%Y-%m-%d')
    except ValueError:
        result.document_date = None
    if foreign:
        # Beträge in Fremdwährung wären als Euro gebucht worden: nichts übernehmen, nur der eine Befund
        result.transactions = []
        result.warnings = ['foreign_currency']
        return result
    # eindeutige Befunde, Reihenfolge erhalten
    result.warnings = list(dict.fromkeys(warnings))
    return result


def parse_xml(data: bytes) -> List[BankStatement]:
    """Eine CAMT.053-Datei (Bytes) in Kontoauszüge zerlegen."""
    root = _parse_xml(data)
    if _local(root.tag) != 'Document':
        raise CamtError('Kein CAMT.053-Dokument (Wurzelelement fehlt).')
    container = _child(root, 'BkToCstmrStmt')
    if container is None:
        raise CamtError('Kein CAMT.053-Kontoauszug (BkToCstmrStmt fehlt).')
    stmts = _children(container, 'Stmt')
    if not stmts:
        raise CamtError('Die Datei enthält keinen Kontoauszug.')
    return [_statement_from(s) for s in stmts]


def _read_zip(data: bytes) -> List[Tuple[str, bytes]]:
    """XML-Einträge eines ZIP – nur im Speicher, begrenzt."""
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise CamtError('Kein gültiges ZIP-Archiv.')
    files = []
    total = 0
    with zf:
        infos = [i for i in zf.infolist()
                 if not i.is_dir() and i.filename.lower().endswith('.xml')
                 and not i.filename.startswith('__MACOSX/')]
        if len(infos) > MAX_ZIP_ENTRIES:
            raise CamtError(f'ZIP enthält mehr als {MAX_ZIP_ENTRIES} XML-Dateien.')
        for info in infos:
            if info.flag_bits & 0x1:
                raise CamtError('Verschlüsselte ZIP-Archive werden nicht gelesen.')
            if info.file_size > MAX_XML_BYTES:
                raise CamtError(f'{info.filename}: Datei ist zu groß.')
            if info.compress_size and info.file_size / info.compress_size > MAX_ZIP_RATIO:
                raise CamtError(f'{info.filename}: unplausibles Kompressionsverhältnis.')
            try:
                with zf.open(info) as fh:
                    content = fh.read(MAX_XML_BYTES + 1)   # Größenangabe nicht trauen
            except (zipfile.BadZipFile, NotImplementedError, RuntimeError) as e:
                raise CamtError(f'{info.filename}: nicht lesbar ({e}).')
            if len(content) > MAX_XML_BYTES:
                raise CamtError(f'{info.filename}: Datei ist zu groß.')
            total += len(content)
            if total > MAX_ZIP_TOTAL_BYTES:
                raise CamtError('ZIP ist entpackt zu groß.')
            files.append((info.filename, content))
    if not files:
        raise CamtError('Das ZIP enthält keine XML-Dateien.')
    return files


def parse_upload(filename: str, data: bytes) -> List[Tuple[str, BankStatement]]:
    """Upload (``.xml`` oder ``.zip``) lesen.

    Returns Liste von (Anzeigename, Kontoauszug). Der Anzeigename ergänzt bei
    ZIP den Namen der XML-Datei und bei mehreren Auszügen deren Nummer.
    Raises :class:`CamtError`, wenn nichts Lesbares enthalten ist.
    """
    if filename.lower().endswith('.zip'):
        sources = [(f'{filename}/{n}', c) for n, c in _read_zip(data)]
    else:
        sources = [(filename, data)]
    out: List[Tuple[str, BankStatement]] = []
    for name, content in sources:
        statements = parse_xml(content)
        for idx, st in enumerate(statements, 1):
            label = name if len(statements) == 1 else f'{name} (Auszug {idx})'
            out.append((label, st))
    return out
