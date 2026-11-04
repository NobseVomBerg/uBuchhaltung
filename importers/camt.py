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
* Beträge werden als ``Decimal`` aus dem XML-String gelesen, nie über float.

Befund-Schlüssel auf Beleg-Ebene (``BankStatement.warnings``):

==================  =========================================================
``balance``         Anfangssaldo + Umsätze ≠ Schlusssaldo
``split_mismatch``  Einzelbeträge einer Sammelbuchung ≠ Gesamtbetrag
``skipped_status``  nicht gebuchte Umsätze (nicht ``BOOK``) wurden übergangen
``bad_entry``       ein Umsatz war nicht lesbar und wurde übergangen
==================  =========================================================
"""
import io
import zipfile
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import List, Optional, Tuple
from xml.etree import ElementTree as ET
from xml.parsers import expat

from money import to_minor

from .base import BankStatement, StatementTransaction

BANK_CODE = 'CAMT'

#: Grenzen gegen Zip-Bombs und überlange Eingaben
MAX_XML_BYTES = 20 * 1024 * 1024          # je XML (entpackt)
MAX_ZIP_ENTRIES = 50                      # XML-Einträge je ZIP
MAX_ZIP_TOTAL_BYTES = 100 * 1024 * 1024   # alle XML eines ZIP (entpackt)
MAX_ZIP_RATIO = 200                       # entpackt : gepackt


class CamtError(ValueError):
    """Datei ist kein (zulässiges) CAMT.053 – Text ist für Nutzer gedacht."""


# ── XML sicher lesen ─────────────────────────────────────────────────────────

def _forbid(*_args):
    raise CamtError('XML mit DTD/Entity-Deklaration wird aus Sicherheitsgründen '
                    'nicht gelesen (CAMT enthält keine).')


def _parse_xml(data: bytes) -> ET.Element:
    if len(data) > MAX_XML_BYTES:
        raise CamtError('XML-Datei ist zu groß.')
    # Erst ein Expat-Durchlauf, der jede DTD/Entity-Deklaration abbricht (egal
    # in welcher Kodierung); erst danach baut ElementTree den Baum.
    guard = expat.ParserCreate()
    guard.StartDoctypeDeclHandler = _forbid
    guard.EntityDeclHandler = _forbid
    try:
        guard.Parse(data, True)
        return ET.fromstring(data)
    except CamtError:
        raise
    except (ET.ParseError, expat.ExpatError) as e:
        raise CamtError(f'Kein gültiges XML: {e}')


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
    if not value.is_finite():
        raise CamtError(f'Betrag nicht lesbar: {raw!r}')
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


# ── Einzelne Umsätze ─────────────────────────────────────────────────────────

def _tx_amount(tx):
    """Eigener Betrag einer ``TxDtls`` (v.02/.04: AmtDtls, sonst Amt)."""
    return (_path(tx, 'AmtDtls', 'TxAmt', 'Amt')
            or _path(tx, 'AmtDtls', 'InstdAmt', 'Amt')
            or _path(tx, 'Amt'))


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
            if node is None:
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

def _balance(stmt, codes) -> Optional[Decimal]:
    for bal in _children(stmt, 'Bal'):
        code = _text(bal, 'Tp', 'CdOrPrtry', 'Cd')
        if code in codes:
            return _signed(_parse_amount(_child(bal, 'Amt')),
                           _text(bal, 'CdtDbtInd'))
    return None


def _statement_from(stmt) -> BankStatement:
    iban = (_text(stmt, 'Acct', 'Id', 'IBAN') or _text(stmt, 'Acct', 'Id', 'Othr', 'Id'))
    result = BankStatement(bank_code=BANK_CODE,
                           iban=iban.replace(' ', '').upper() or None)
    warnings: list = []
    total = Decimal(0)
    dates = []
    skipped_status = False

    for entry in _children(stmt, 'Ntry'):
        status = _text(entry, 'Sts') or _text(entry, 'Sts', 'Cd')
        if status and status != 'BOOK':
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

    raw = (_text(stmt, 'FrToDt', 'ToDtTm') or _text(stmt, 'CreDtTm')
           or (max(dates) if dates else ''))
    try:
        result.document_date = datetime.strptime(raw[:10], '%Y-%m-%d')
    except ValueError:
        result.document_date = None
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
