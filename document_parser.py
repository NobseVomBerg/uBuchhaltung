# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""
Document Parser for receipts and bank statements
Supports PDF parsing with text extraction and OCR fallback
"""
import os
import re
import json
import hashlib
import shutil
import threading
from datetime import datetime
from typing import Dict, List, Optional, Tuple
try:
    import pdfplumber
    PDFPLUMBER_AVAILABLE = True
except ImportError:
    pdfplumber = None
    PDFPLUMBER_AVAILABLE = False
from pathlib import Path

from importers import pdftext

# ── SQL-Log-Rotation ─────────────────────────────────────────────────────────
# Audit-Logs (sql_operations.log/.sql) werden ab dieser Größe rotiert und
# komprimiert archiviert (7-Zip falls installiert, sonst gzip). Archive werden
# nie automatisch gelöscht (Audit-Trail).
SQL_LOG_MAX_BYTES = 5 * 1024 * 1024

_rotate_lock = threading.Lock()
_sevenzip_path = ''   # '' = noch nicht gesucht, None = nicht vorhanden


def _find_sevenzip():
    """7-Zip-Binary suchen (PATH, dann Windows-Standardpfade). Ergebnis gecacht."""
    global _sevenzip_path
    if _sevenzip_path == '':
        found = None
        for name in ('7z', '7zz', '7za', '7zr'):   # 7zz = offizielles Linux-Binary (Debian-Paket "7zip")
            p = shutil.which(name)
            if p:
                found = p
                break
        if found is None and os.name == 'nt':
            for p in (r'C:\Program Files\7-Zip\7z.exe',
                      r'C:\Program Files (x86)\7-Zip\7z.exe'):
                if os.path.isfile(p):
                    found = p
                    break
        _sevenzip_path = found
    return _sevenzip_path


def compress_rotated_log(path):
    """Rotierte Log-Datei komprimieren: 7-Zip falls verfügbar, sonst gzip.

    Löscht das Original nur nach erfolgreicher Kompression; schlägt sie fehl,
    bleibt die unkomprimierte Datei liegen (kein Datenverlust im Audit-Trail).
    """
    try:
        exe = _find_sevenzip()
        if exe:
            import subprocess
            result = subprocess.run(
                [exe, 'a', '-bd', '-y', path + '.7z', path],
                capture_output=True, timeout=300)
            if result.returncode == 0 and os.path.isfile(path + '.7z'):
                os.remove(path)
                return path + '.7z'
        import gzip
        with open(path, 'rb') as src, gzip.open(path + '.gz', 'wb') as dst:
            shutil.copyfileobj(src, dst)
        os.remove(path)
        return path + '.gz'
    except Exception:
        return None  # Original bleibt unkomprimiert erhalten


def _rotate_if_needed(path):
    """Log-Datei ab SQL_LOG_MAX_BYTES wegrotieren (Zeitstempel-Name) und im
    Hintergrund komprimieren. Thread-sicher; no-op wenn Datei fehlt/klein ist."""
    try:
        if os.path.getsize(path) < SQL_LOG_MAX_BYTES:
            return
    except OSError:
        return
    with _rotate_lock:
        try:
            if os.path.getsize(path) < SQL_LOG_MAX_BYTES:
                return  # anderer Thread hat bereits rotiert
        except OSError:
            return
        dirname, fname = os.path.split(path)
        stamp = datetime.now().strftime('%Y%m%d-%H%M%S')
        rotated = os.path.join(dirname, f"{stamp}_{fname}")
        n = 1
        while os.path.exists(rotated) or os.path.exists(rotated + '.7z') or os.path.exists(rotated + '.gz'):
            rotated = os.path.join(dirname, f"{stamp}-{n}_{fname}")
            n += 1
        os.replace(path, rotated)
    threading.Thread(target=compress_rotated_log, args=(rotated,), daemon=True).start()


class DocumentParser:
    def __init__(self, data_dir=None, log_dir=None):
        # Ohne explizite Pfade richtet sich die Ablage nach dem aktuellen Nutzer
        # (Mehrbenutzer-Betrieb): Einzelmodus ⇒ ./data, mit Login ⇒
        # data/users/<user>/. Explizit übergebene Pfade gewinnen (z. B. Tests).
        if log_dir is None or data_dir is None:
            import userctx
            base = userctx.user_data_dir()
            if log_dir is None:
                log_dir = base
            if data_dir is None:
                data_dir = os.path.join(base, "Belege")
        self.data_dir = data_dir
        self.log_dir = log_dir
        os.makedirs(log_dir, exist_ok=True)
    
    def log_sql(self, sql_statement: str, parameters: tuple, description: str = ""):
        """Log SQL statements to file for audit trail"""
        # Detailed log with timestamps and descriptions
        log_file = os.path.join(self.log_dir, "sql_operations.log")
        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        _rotate_if_needed(log_file)

        with open(log_file, 'a', encoding='utf-8') as f:
            f.write(f"\n{'='*80}\n")
            f.write(f"Timestamp: {timestamp}\n")
            if description:
                f.write(f"Description: {description}\n")
            f.write(f"SQL: {sql_statement}\n")
            f.write(f"Parameters: {parameters}\n")
            f.write(f"{'='*80}\n")
        
        # Compact SQL-only log (one statement per line)
        sql_only_file = os.path.join(self.log_dir, "sql_operations.sql")
        _rotate_if_needed(sql_only_file)
        # Clean up SQL statement: remove leading/trailing whitespace and normalize to single line
        clean_sql = ' '.join(sql_statement.split())
        with open(sql_only_file, 'a', encoding='utf-8') as f:
            f.write(f"{clean_sql};\n")
    
    def save_parsed_data(self, filename: str, parsed_data: Dict) -> str:
        """Save parsed data to temporary JSON file for review"""
        temp_dir = os.path.join(self.log_dir, "pending_imports")
        os.makedirs(temp_dir, exist_ok=True)
        
        # Generate unique ID for this import
        import_id = datetime.now().strftime('%Y%m%d_%H%M%S')
        json_file = os.path.join(temp_dir, f"{import_id}_{filename}.json")
        
        # Add metadata
        parsed_data['import_id'] = import_id
        parsed_data['original_filename'] = filename
        parsed_data['parsed_at'] = datetime.now().isoformat()
        
        with open(json_file, 'w', encoding='utf-8') as f:
            json.dump(parsed_data, f, indent=2, default=str)
        
        return import_id
        
    def organize_file(self, filepath: str, document_date: datetime, doc_type: str = "general", subdir: str = "") -> str:
        """
        Organize file into year/type/subdir structure
        Example: ./data/Belege/2026/Konten/VBR/filename.pdf
        """
        year = document_date.year
        filename = os.path.basename(filepath)
        
        # Build target directory
        if doc_type == "bank_statement":
            target_dir = os.path.join(self.data_dir, str(year), "Konten", subdir)
        else:
            target_dir = os.path.join(self.data_dir, str(year), doc_type)
        
        # Create directory if not exists
        os.makedirs(target_dir, exist_ok=True)
        
        # Build target path
        target_path = os.path.join(target_dir, filename)
        
        # If target already exists and source is different location
        if os.path.exists(target_path) and os.path.abspath(filepath) != os.path.abspath(target_path):
            # Compare file contents using hash
            source_hash = self._calculate_file_hash(filepath)
            target_hash = self._calculate_file_hash(target_path)
            
            if source_hash == target_hash:
                # Files are identical - remove source and use existing target
                os.remove(filepath)
                return target_path
            else:
                # Files have same name but different content - ERROR
                raise FileExistsError(
                    f"Datei '{filename}' existiert bereits mit unterschiedlichem Inhalt. "
                    f"Bitte umbenennen oder vorhandene Datei prüfen."
                )
        
        # Move file if source and target are different locations
        if os.path.abspath(filepath) != os.path.abspath(target_path):
            os.rename(filepath, target_path)
        
        return target_path
    
    def _calculate_file_hash(self, filepath: str) -> str:
        """Calculate SHA256 hash of file for comparison"""
        sha256_hash = hashlib.sha256()
        with open(filepath, "rb") as f:
            for byte_block in iter(lambda: f.read(4096), b""):
                sha256_hash.update(byte_block)
        return sha256_hash.hexdigest()
    
    # Extraktions-Helfer liegen in importers/pdftext.py – sie werden von allen
    # Importmodulen gebraucht und haben mit der Ablage-Logik hier nichts zu tun.
    def extract_text_from_pdf(self, filepath: str) -> str:
        """Gesamten Text eines PDFs lesen (siehe importers.pdftext)."""
        return pdftext.extract_text(filepath)

    def extract_iban(self, text: str) -> Optional[str]:
        """IBAN des Kontoinhabers aus dem Belegkopf (siehe importers.pdftext)."""
        return pdftext.extract_iban(text)

    def extract_date_from_text(self, text: str,
                               pattern: str = pdftext.DOCUMENT_DATE_PATTERN) -> Optional[datetime]:
        """Datum per Muster aus dem Text (siehe importers.pdftext)."""
        return pdftext.extract_date(text, pattern)

    def parse_bank_statement_vbr(self, filepath: str) -> Dict:
        """Volksbank-Rottweil-Auszug lesen (Abstraktionsschicht, todo #2).

        Die Bankeigenheiten stecken in importers/vbr.py; hier bleibt nur der
        alte Aufrufweg erhalten, damit bestehende Aufrufer nichts merken.
        """
        from importers.vbr import VbrImporter
        statement = VbrImporter().parse(filepath)
        result = statement.as_dict()
        result.pop('warnings', None)
        for row in result['transactions']:
            row.pop('warnings', None)
        return result

    def _parse_vbr_text(self, text: str, year: int = None) -> List[Dict]:
        """Delegiert an importers.vbr.parse_text (siehe dort)."""
        from importers.vbr import parse_text
        return parse_text(text, year)

    # ── DKB (Deutsche Kreditbank) parser ────────────────────────────

    def parse_bank_statement_dkb(self, filepath: str) -> Dict:
        """Parse DKB bank statement PDF.

        DKB statements have a two-column amount layout (Belastung /
        Gutschrift) without S/H suffixes, so we must use the x-position
        of each amount word to determine debit vs credit.

        Returns the same dict shape as ``parse_bank_statement_vbr``.
        """
        result: Dict = {
            'iban': None,
            'document_date': None,
            'transactions': [],
            'bank_code': 'DKB',
        }

        try:
            with pdfplumber.open(filepath) as pdf:
                full_text = ''
                for page in pdf.pages:
                    full_text += (page.extract_text() or '') + '\n'

                # ── IBAN from header ──────────────────────────────
                # "Kontonummer 1007556036 / IBAN DE04 1203 0000 1007 5560 36"
                m = re.search(
                    r'IBAN\s+([A-Z]{2}[\s\d]{15,})',
                    full_text[:600])
                if m:
                    result['iban'] = re.sub(r'\s', '', m.group(1))

                # ── Date range from header ────────────────────────
                # "Kontoauszug Nummer 002 / 2016 vom 05.01.2016 bis 04.02.2016"
                m = re.search(
                    r'vom\s+\d{2}\.\d{2}\.\d{4}\s+bis\s+(\d{2}\.\d{2}\.\d{4})',
                    full_text[:600])
                if m:
                    try:
                        result['document_date'] = datetime.strptime(
                            m.group(1), '%d.%m.%Y')
                    except ValueError:
                        pass

                # Derive year range for DD.MM. -> full date conversion
                year_from, year_to = self._dkb_year_range(full_text[:600])

                # ── Parse each page ───────────────────────────────
                for page in pdf.pages:
                    txns, continuation = self._parse_dkb_page(
                        page, year_from, year_to)
                    # Merge continuation from page-spanning transaction
                    if continuation and result['transactions']:
                        last_tx = result['transactions'][-1]
                        cont_text = '\n'.join(continuation)
                        # Clean continuation (same SEPA markers)
                        for pat in (
                            r'I\s*B\s*A\s*N\s*:?.*',
                            r'B\s*I\s*C\s*:?.*',
                            r'M\s*R\s*E\s*F\s*[+:].*',
                            r'E\s*R\s*E\s*F\s*[+:].*',
                            r'C\s*R\s*E\s*D\s*[+:].*',
                            r'K\s*R\s*E\s*F\s*[+:].*',
                            r'S\s*V\s*W\s*Z\s*\+.*',
                            r'A\s*B\s*W[AE]\s*\+.*',
                        ):
                            cont_text = re.sub(
                                pat, '', cont_text,
                                flags=re.I | re.DOTALL)
                        cont_clean = '\n'.join(
                            ln.strip() for ln in cont_text.split('\n')
                            if ln.strip())
                        if cont_clean:
                            if last_tx['reference']:
                                last_tx['reference'] += '\n' + cont_clean
                            else:
                                last_tx['reference'] = cont_clean
                        # Try to extract foreign IBAN from continuation
                        if not last_tx.get('foreign_iban'):
                            full_cont = ' '.join(continuation)
                            iban_pat = (r'I\s*B\s*A\s*N\s*:?\s*'
                                        r'([A-Z]{2}\s*\d{2}[A-Z0-9\s]{15,}?)'
                                        r'(?:\s*B\s*I\s*C\s*:|\s|$)')
                            iban_m = re.search(
                                iban_pat, full_cont, re.IGNORECASE)
                            if iban_m:
                                last_tx['foreign_iban'] = re.sub(
                                    r'\s+', '', iban_m.group(1).upper())
                    result['transactions'].extend(txns)

        except Exception as e:
            print(f"Error parsing DKB statement {filepath}: {e}")
            import traceback
            traceback.print_exc()

        return result

    # ----------------------------------------------------------------
    @staticmethod
    def _dkb_year_range(header_text: str):
        """Return (year_from, year_to) from the DKB header range line.

        Example header:
          ``Kontoauszug Nummer 004 / 2016 vom 07.03.2016 bis 01.04.2016``
        """
        m = re.search(
            r'vom\s+\d{2}\.\d{2}\.(\d{4})\s+bis\s+\d{2}\.\d{2}\.(\d{4})',
            header_text)
        if m:
            return int(m.group(1)), int(m.group(2))
        return datetime.now().year, datetime.now().year

    # ----------------------------------------------------------------
    def _parse_dkb_page(self, page, year_from: int, year_to: int) -> tuple:
        """Parse a single DKB PDF page into transaction dicts.

        Returns (transactions, continuation_lines).
        continuation_lines: raw text lines overflowing from the
            previous page's last transaction (appears at the top of
            the page before the first real transaction).

        Strategy:
        1. Build a *credit set* from word-level x-positions: for every
           amount word whose x0 >= 500 we record its approximate y
           position as "credit".
        2. Walk ``extract_text()`` lines with the same DD.MM. DD.MM.
           regex used for VBR.  When we find a transaction header we
           look up whether the amount's y-row is in the credit set.
        """
        text = page.extract_text() or ''
        words = page.extract_words()

        # ── 1. Build credit-y set ─────────────────────────────────
        #   Amount words: right-aligned numbers with comma (e.g. 14.305,28)
        #   Threshold x0 >= 500 → Gutschrift column
        credit_tops: set = set()
        for w in words:
            if w['top'] < 160:
                continue  # skip header area
            if re.match(r'^[\d.]+,\d{2}$', w['text']) and w['x0'] >= 500:
                credit_tops.add(round(w['top']))

        # Also build a top→word map for ALL amount-like words so we can
        # fall back to matching by y-position when needed.
        amount_by_top: dict = {}
        for w in words:
            if w['top'] < 160:
                continue
            if re.match(r'^[\d.]+,\d{2}$', w['text']) and w['x0'] > 350:
                amount_by_top[round(w['top'])] = w

        # ── 2. Map bu-tag date words to y-positions ───────────────
        bu_tag_tops: list = []
        for w in words:
            if re.match(r'^\d{2}\.\d{2}\.$', w['text']) and w['x0'] < 60:
                bu_tag_tops.append(round(w['top']))

        # ── 3. Strip footer / boilerplate ─────────────────────────
        text = re.sub(
            r'DEUTSCHE KREDITBANK AG\s+IBAN:.*',
            '', text, flags=re.DOTALL)
        # Remove ALTER/NEUER KONTOSTAND and everything after
        text = re.sub(
            r'ALTER KONTOSTAND.*',
            '', text, flags=re.DOTALL)

        lines = text.split('\n')

        # ── 4. Iterate lines ──────────────────────────────────────
        transactions: List[Dict] = []
        continuation_lines: list = []  # overflow from prev page
        first_real_tx_found = False
        i = 0
        bu_tag_idx = 0  # index into bu_tag_tops

        while i < len(lines):
            line = lines[i].strip()

            # Skip page header lines
            if (not line
                    or 'Kontoauszug Nummer' in line
                    or 'Kontonummer' in line
                    or line.startswith('Bu.Tag')
                    or 'Wir haben' in line):
                i += 1
                continue

            # ── Transaction header: DD.MM. DD.MM. type amount ─────
            match = re.match(
                r'^(\d{2}\.\d{2}\.) \d{2}\.\d{2}\. (.+)', line)

            if not match:
                i += 1
                continue

            bu_tag = match.group(1)            # e.g. "04.01."
            rest_of_line = match.group(2)

            # Extract trailing amount – strict format: 1.234,56
            amount_match = re.search(
                r'(\d{1,3}(?:\.\d{3})*,\d{2})\s*$', rest_of_line)

            if not amount_match:
                # No valid amount → continuation from previous page
                if not first_real_tx_found:
                    continuation_lines.append(rest_of_line)
                    # Advance bu_tag_idx for this date entry
                    if bu_tag_idx < len(bu_tag_tops):
                        bu_tag_idx += 1
                    # Collect subsequent non-date detail lines
                    j = i + 1
                    while j < len(lines):
                        nxt = lines[j].strip()
                        if (not nxt
                                or re.match(
                                    r'^\d{2}\.\d{2}\. \d{2}\.\d{2}\.',
                                    nxt)
                                or 'Kontoauszug Nummer' in nxt
                                or 'Kontonummer' in nxt
                                or nxt.startswith('Bu.Tag')
                                or 'ALTER KONTOSTAND' in nxt
                                or 'DEUTSCHE KREDITBANK' in nxt):
                            break
                        continuation_lines.append(nxt)
                        j += 1
                    i = j
                    continue
                i += 1
                continue

            amount_str = amount_match.group(1)

            first_real_tx_found = True

            amount_val = float(
                amount_str.replace('.', '').replace(',', '.'))

            # Transaction type (between second date and amount)
            trans_type = rest_of_line[:amount_match.start()].strip()

            # ── Determine debit/credit via y-position ─────────────
            # Find the bu-tag y that corresponds to this line
            y_top = None
            if bu_tag_idx < len(bu_tag_tops):
                y_top = bu_tag_tops[bu_tag_idx]
                bu_tag_idx += 1

            if y_top is not None and y_top in credit_tops:
                amount = amount_val       # credit → positive
            elif y_top is not None:
                # Check +-1 tolerance
                if (y_top - 1) in credit_tops or (y_top + 1) in credit_tops:
                    amount = amount_val
                else:
                    amount = -amount_val  # debit → negative
            else:
                # Fallback: guess from transaction type
                if trans_type.lower() in ('zahlungseingang',):
                    amount = amount_val
                else:
                    amount = -amount_val

            # ── Collect detail lines ──────────────────────────────
            recipient = ''
            reference_lines: list = []
            foreign_iban = ''
            all_detail_lines: list = []

            j = i + 1
            while j < len(lines):
                next_line = lines[j].strip()

                # Stop at next transaction
                if re.match(r'^\d{2}\.\d{2}\. \d{2}\.\d{2}\.', next_line):
                    break

                # Skip empty, header, footer, KONTOSTAND
                if (not next_line
                        or 'Kontoauszug Nummer' in next_line
                        or 'Kontonummer' in next_line
                        or next_line.startswith('Bu.Tag')
                        or 'ALTER KONTOSTAND' in next_line
                        or 'NEUER KONTOSTAND' in next_line
                        or 'DEUTSCHE KREDITBANK' in next_line
                        or next_line.startswith('Guthaben sind als')
                        or next_line.startswith('Seite ')):
                    j += 1
                    continue

                all_detail_lines.append(next_line)
                j += 1

            # ── Extract IBAN ──────────────────────────────────────
            full_text_sl = ' '.join(all_detail_lines)
            iban_pat = (r'I\s*B\s*A\s*N\s*:?\s*'
                        r'([A-Z]{2}\s*\d{2}[A-Z0-9\s]{15,}?)'
                        r'(?:\s*B\s*I\s*C\s*:|\s|$)')
            iban_m = re.search(iban_pat, full_text_sl, re.IGNORECASE)
            if iban_m:
                foreign_iban = re.sub(r'\s+', '', iban_m.group(1).upper())

            # ── Clean detail text (remove IBAN/BIC/REF fields) ────
            cleaned = '\n'.join(all_detail_lines)
            cleaned = re.sub(
                r'I\s*B\s*A\s*N\s*:?.*', '', cleaned,
                flags=re.I | re.DOTALL)
            cleaned = re.sub(
                r'B\s*I\s*C\s*:?.*', '', cleaned,
                flags=re.I | re.DOTALL)
            cleaned = re.sub(
                r'M\s*R\s*E\s*F\s*[+:].*', '', cleaned,
                flags=re.I | re.DOTALL)
            cleaned = re.sub(
                r'E\s*R\s*E\s*F\s*[+:].*', '', cleaned,
                flags=re.I | re.DOTALL)
            cleaned = re.sub(
                r'C\s*R\s*E\s*D\s*[+:].*', '', cleaned,
                flags=re.I | re.DOTALL)
            cleaned = re.sub(
                r'K\s*R\s*E\s*F\s*[+:].*', '', cleaned,
                flags=re.I | re.DOTALL)
            cleaned = re.sub(
                r'R\s*E\s*F\s*[+:].*', '', cleaned,
                flags=re.I | re.DOTALL)
            cleaned = re.sub(
                r'A\s*B\s*W[AE]\s*\+.*', '', cleaned,
                flags=re.I | re.DOTALL)
            cleaned = re.sub(
                r'S\s*V\s*W\s*Z\s*\+.*', '', cleaned,
                flags=re.I | re.DOTALL)

            cleaned_lines = [
                ln.strip() for ln in cleaned.split('\n')
                if ln.strip()]

            # ── Derive recipient / reference ──────────────────────
            if trans_type.lower().startswith('abrechnung'):
                recipient = 'DKB'
                reference_lines = [trans_type]
            elif cleaned_lines:
                recipient = cleaned_lines[0]
                reference_lines = cleaned_lines[1:]
            else:
                recipient = trans_type

            # ── Build date ────────────────────────────────────────
            day_month = bu_tag  # "04.01."
            month_int = int(day_month[3:5])
            # Use year_to for months that belong to the "bis" year,
            # year_from otherwise (handles Dec→Jan spanning)
            if year_from != year_to and month_int >= 10:
                year = year_from
            else:
                year = year_to
            try:
                tx_date = datetime.strptime(
                    f'{day_month}{year}', '%d.%m.%Y')
            except ValueError:
                tx_date = datetime.now()

            reference = ('\n'.join(reference_lines)
                         if reference_lines else trans_type)

            transactions.append({
                'date': tx_date.strftime('%Y-%m-%d'),
                'recipient': recipient if recipient else trans_type,
                'reference': reference,
                'amount': amount,
                'foreign_iban': foreign_iban,
            })

            i = j  # skip processed lines
            continue

        return transactions, continuation_lines

    def parse_document(self, filepath: str) -> Optional[Dict]:
        """Beleg einlesen: zuständiges Importmodul wählen und auswerten.

        Die Bankerkennung liegt in der Abstraktionsschicht (importers): jedes
        Modul erkennt seine eigenen Belege. Eine neue Bank anzubinden heißt
        deshalb, ein Modul zu schreiben – hier ändert sich nichts.

        Kontoauszüge liefern ``iban``/``document_date``/``transactions``
        (plus ``warnings`` je Bewegung aus der Plausibilitätsprüfung), alles
        andere das bisherige generische Dict.
        """
        import importers

        text = pdftext.extract_text(filepath)
        statement = importers.parse_statement(
            filepath, os.path.basename(filepath), text)
        if statement is not None:
            return statement.as_dict()

        return {
            'type': 'generic',
            'text': text,
            'iban': pdftext.extract_iban(text),
            'date': pdftext.extract_date(text)
        }
    
    def process_and_organize(self, filepath: str) -> Tuple[str, Dict]:
        """
        Parse document and organize into correct directory structure
        Returns: (new_filepath, parsed_data)
        """
        parsed = self.parse_document(filepath)
        
        if not parsed:
            return filepath, {}
        
        # Organize file based on parsed data
        if parsed.get('document_date'):
            doc_type = "bank_statement" if 'transactions' in parsed else "general"
            subdir = parsed.get('bank_code', '')
            
            new_path = self.organize_file(
                filepath,
                parsed['document_date'],
                doc_type,
                subdir
            )
            return new_path, parsed
        
        return filepath, parsed
