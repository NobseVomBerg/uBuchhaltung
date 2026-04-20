"""
Document Parser for receipts and bank statements
Supports PDF parsing with text extraction and OCR fallback
"""
import os
import re
import json
import hashlib
from datetime import datetime
from typing import Dict, List, Optional, Tuple
import pdfplumber
from pathlib import Path

class DocumentParser:
    def __init__(self, data_dir="./data/Belege", log_dir="./data"):
        self.data_dir = data_dir
        self.log_dir = log_dir
        os.makedirs(log_dir, exist_ok=True)
    
    def log_sql(self, sql_statement: str, parameters: tuple, description: str = ""):
        """Log SQL statements to file for audit trail"""
        # Detailed log with timestamps and descriptions
        log_file = os.path.join(self.log_dir, "sql_operations.log")
        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        
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
    
    def extract_text_from_pdf(self, filepath: str) -> str:
        """Extract text from PDF using pdfplumber"""
        text = ""
        try:
            with pdfplumber.open(filepath) as pdf:
                for page in pdf.pages:
                    text += page.extract_text() or ""
        except Exception as e:
            print(f"Error extracting text from {filepath}: {e}")
        return text
    
    def extract_iban(self, text: str) -> Optional[str]:
        """Extract IBAN from text - specifically the account holder's IBAN"""
        # First try: Look for IBAN near the beginning of document (account holder's IBAN)
        # VBR format: "IBAN: DE65 6429 0120 0027 3540 24 BIC: GENODES1VRW"
        lines = text.split('\n')
        for i, line in enumerate(lines[:20]):  # Check first 20 lines
            if 'IBAN:' in line and 'BIC:' in line:
                # Extract IBAN between "IBAN:" and "BIC:"
                iban_match = re.search(r'IBAN:\s*([A-Z]{2}[\s\d]+)(?:\s*BIC:)', line)
                if iban_match:
                    iban = iban_match.group(1).replace(' ', '')
                    if len(iban) >= 15:
                        return iban
        
        # Fallback: Look for any IBAN-like pattern in first part of document
        first_part = '\n'.join(lines[:30])
        iban_pattern = r'\b([A-Z]{2}\d{20,22})\b'
        matches = re.findall(iban_pattern, first_part.replace(" ", ""))
        if matches:
            return matches[0]
        
        return None
    
    def extract_date_from_text(self, text: str, pattern: str = r'erstellt am (\d{2}\.\d{2}\.\d{4})') -> Optional[datetime]:
        """Extract date from text using regex pattern"""
        match = re.search(pattern, text)
        if match:
            date_str = match.group(1)
            try:
                return datetime.strptime(date_str, '%d.%m.%Y')
            except ValueError:
                pass
        return None
    
    def parse_bank_statement_vbr(self, filepath: str) -> Dict:
        """
        Parse Volksbank Rottweil (VBR) bank statement
        Returns: {
            'iban': str,
            'document_date': datetime,
            'transactions': List[Dict]
        }
        """
        result = {
            'iban': None,
            'document_date': None,
            'transactions': [],
            'bank_code': 'VBR'
        }
        
        text = self.extract_text_from_pdf(filepath)
        
        # Extract IBAN
        result['iban'] = self.extract_iban(text)
        
        # Extract document date
        result['document_date'] = self.extract_date_from_text(text)
        
        # ── Extract statement year from header ─────────────────
        # Pattern: "Kontoauszug Nr. 1/2024" or "erstellt am 31.01.2024"
        stmt_year = None
        m = re.search(r'Kontoauszug\s+Nr\.\s*\d+/(\d{4})', text)
        if m:
            stmt_year = int(m.group(1))
        else:
            m = re.search(r'erstellt\s+am\s+\d{2}\.\d{2}\.(\d{4})', text)
            if m:
                stmt_year = int(m.group(1))
        if stmt_year is None:
            stmt_year = datetime.now().year

        # Extract transactions from text (not tables, as VBR uses text-based format)
        try:
            with pdfplumber.open(filepath) as pdf:
                for page in pdf.pages:
                    page_text = page.extract_text() or ""
                    transactions = self._parse_vbr_text(page_text, stmt_year)
                    result['transactions'].extend(transactions)
        except Exception as e:
            print(f"Error parsing transactions from {filepath}: {e}")
            import traceback
            traceback.print_exc()
        
        return result
    
    def _parse_vbr_text(self, text: str, year: int = None) -> List[Dict]:
        """Parse VBR transactions from plain text.

        *year* is the statement year extracted from the header
        (e.g. 2024 from ``Kontoauszug Nr. 1/2024``).
        """
        if year is None:
            year = datetime.now().year
        transactions = []
        
        # Strip page footer that VBR bank statements add at the bottom
        # of every page. The footer pattern looks like:
        #   0128
        #   000
        #   K00009283          (or similar K-number)
        #   5M                 (optional)
        #   Bitte beachten Sie die Hinweise ...
        text = re.sub(
            r'\n\d{4}\n\d{3}\n'
            r'K\d{5,}\n'
            r'(?:[A-Z0-9]{1,4}\n)?'
            r'Bitte beachten Sie die Hinweise[^\n]*',
            '', text)

        # Split text into lines
        lines = text.split('\n')
        
        i = 0
        while i < len(lines):
            line = lines[i].strip()
            
            # Look for transaction pattern: DD.MM. DD.MM. ... amount S/H
            # Example: "01.12. 01.12. Lastschrift PN:931 1.142,18 S"
            match = re.match(r'^(\d{2}\.\d{2}\.) \d{2}\.\d{2}\. (.+)', line)
            
            if match:
                # Found a transaction
                bu_tag = match.group(1)  # e.g., "01.12."
                rest_of_line = match.group(2)  # Everything after the second date
                
                # Extract amount (last number with comma and S or H)
                amount_match = re.search(r'([\d.,]+)\s+([SH])\s*$', rest_of_line)
                
                if amount_match:
                    amount_str = amount_match.group(1).replace('.', '').replace(',', '.')
                    debit_credit = amount_match.group(2)
                    
                    # Convert amount (S = negative, H = positive)
                    amount = float(amount_str)
                    if debit_credit == 'S':
                        amount = -amount
                    
                    # Extract transaction type (between second date and amount)
                    trans_type = rest_of_line[:amount_match.start()].strip()
                    
                    # Collect subsequent lines (recipient, reference, IBAN)
                    recipient = ""
                    reference_lines = []
                    foreign_iban = ""
                    all_detail_lines = []  # Collect all lines first for better IBAN detection
                    
                    j = i + 1
                    while j < len(lines):
                        next_line = lines[j].strip()
                        
                        # Stop if we hit the next transaction
                        if re.match(r'^\d{2}\.\d{2}\. \d{2}\.\d{2}\.', next_line):
                            break
                        
                        # Stop if empty or looks like footer/header
                        if (not next_line
                                or 'Kontoauszug' in next_line
                                or 'Blatt' in next_line
                                or re.match(r'^\d{3,4}$', next_line)
                                or re.match(r'^K\d{5,}$', next_line)
                                or next_line.startswith('Bitte beachten Sie')):
                            j += 1
                            continue
                        
                        all_detail_lines.append(next_line)
                        j += 1
                    
                    # Join all lines to handle IBAN/BIC split across lines
                    full_text = '\n'.join(all_detail_lines)
                    
                    # Extract IBAN first - handle line breaks within "IBAN" (e.g., "IB AN:")
                    # Look for patterns like: IBAN, IB AN, I BAN, etc.
                    full_text_single_line = ' '.join(all_detail_lines)
                    iban_pattern = r'I\s*B\s*A\s*N\s*:?\s*([A-Z]{2}\s*\d{2}[A-Z0-9\s]{15,}?)(?:\s*B\s*I\s*C\s*:|\s|$)'
                    iban_match = re.search(iban_pattern, full_text_single_line, re.IGNORECASE)
                    if iban_match:
                        foreign_iban = re.sub(r'\s+', '', iban_match.group(1).upper())
                    
                    # Remove IBAN, BIC and reference fields from the full text
                    # Important: Remove from the keyword onwards (including all following content)
                    cleaned_text = full_text
                    
                    # Remove IBAN and everything after it (with flexible spacing, case-insensitive)
                    cleaned_text = re.sub(r'I\s*B\s*A\s*N\s*:?.*', '', cleaned_text, flags=re.IGNORECASE | re.DOTALL)
                    # Remove BIC and everything after it (with flexible spacing)
                    cleaned_text = re.sub(r'B\s*I\s*C\s*:?.*', '', cleaned_text, flags=re.IGNORECASE | re.DOTALL)
                    # Remove REF fields and everything after them (specific ones first!)
                    cleaned_text = re.sub(r'M\s*R\s*E\s*F\s*:.*', '', cleaned_text, flags=re.IGNORECASE | re.DOTALL)
                    cleaned_text = re.sub(r'E\s*R\s*E\s*F\s*:.*', '', cleaned_text, flags=re.IGNORECASE | re.DOTALL)
                    cleaned_text = re.sub(r'C\s*R\s*E\s*D\s*:.*', '', cleaned_text, flags=re.IGNORECASE | re.DOTALL)
                    cleaned_text = re.sub(r'R\s*E\s*F\s*:.*', '', cleaned_text, flags=re.IGNORECASE | re.DOTALL)  # REF last!
                    
                    # Split back into lines and keep only non-empty ones
                    cleaned_lines = [line.strip() for line in cleaned_text.split('\n') if line.strip()]
                    
                    # Check if this is an "Abschluss" transaction (bank statement closing)
                    # Look for a line starting with "Abschluss" in cleaned_lines
                    abschluss_line = None
                    for line in cleaned_lines:
                        if re.match(r'^Abschluss\s', line, re.IGNORECASE):
                            abschluss_line = line
                            break
                    
                    if abschluss_line:
                        # For "Abschluss" transactions, recipient is VBR and reference is the Abschluss line
                        recipient = "VBR"
                        reference_lines = [abschluss_line]
                    else:
                        # Normal transaction: first line is recipient, rest is reference
                        if cleaned_lines:
                            recipient = cleaned_lines[0]
                            reference_lines = cleaned_lines[1:]
                    
                    # Parse date – use statement year from header
                    try:
                        date_str = bu_tag + str(year)
                        transaction_date = datetime.strptime(date_str, '%d.%m.%Y')
                    except Exception:
                        transaction_date = datetime.now()
                    
                    reference = '\n'.join(reference_lines) if reference_lines else trans_type
                    
                    transactions.append({
                        'date': transaction_date.strftime('%Y-%m-%d'),
                        'recipient': recipient if recipient else trans_type,
                        'reference': reference,
                        'amount': amount,
                        'foreign_iban': foreign_iban
                    })
                    
                    i = j - 1  # Skip processed lines
            
            i += 1
        
        return transactions
    
    def _parse_vbr_table(self, table: List[List[str]]) -> List[Dict]:
        """Parse VBR bank statement table (legacy, kept for compatibility)"""
        transactions = []
        
        # Find header row
        header_idx = None
        for idx, row in enumerate(table):
            if row and any('Bu-Tag' in str(cell) for cell in row if cell):
                header_idx = idx
                break
        
        if header_idx is None:
            return transactions
        
        # Parse data rows
        i = header_idx + 1
        while i < len(table):
            row = table[i]
            if not row or not any(row):
                i += 1
                continue
            
            # Check if this is a transaction row (has date in first column)
            if row[0] and re.match(r'\d{2}\.\d{2}\.\d{4}', str(row[0])):
                transaction = self._parse_vbr_transaction(table, i)
                if transaction:
                    transactions.append(transaction)
            
            i += 1
        
        return transactions
    
    def _parse_vbr_transaction(self, table: List[List[str]], start_idx: int) -> Optional[Dict]:
        """Parse a single VBR transaction (may span multiple rows)"""
        try:
            row = table[start_idx]
            
            # Bu-Tag (Buchungstag)
            date_str = row[0].strip() if row[0] else None
            if not date_str:
                return None
            
            transaction_date = datetime.strptime(date_str, '%d.%m.%Y')
            
            # Vorgang column - parse multi-line
            vorgang_lines = []
            vorgang_idx = 1  # Assuming Vorgang is second column
            
            # Collect all lines of Vorgang
            current_idx = start_idx
            while current_idx < len(table):
                current_row = table[current_idx]
                if current_idx > start_idx and current_row[0]:  # New transaction starts
                    break
                if len(current_row) > vorgang_idx and current_row[vorgang_idx]:
                    vorgang_lines.append(current_row[vorgang_idx].strip())
                current_idx += 1
            
            # Parse Vorgang content
            # Line 0: Transaction type (skip)
            # Line 1: Recipient/Payer
            # Line 2+: Reference/Purpose
            # Last line(s): Bank details (IBAN)
            
            recipient = vorgang_lines[1] if len(vorgang_lines) > 1 else ""
            
            # Extract reference (middle lines)
            reference_lines = []
            foreign_iban = None
            
            for line in vorgang_lines[2:]:
                # Check if line contains IBAN
                if re.match(r'[A-Z]{2}\d{2}', line.replace(" ", "")):
                    foreign_iban = self.extract_iban(line)
                else:
                    reference_lines.append(line)
            
            reference = " ".join(reference_lines)
            
            # Amount (last column, format: "123,45 S" or "123,45 H")
            amount_str = row[-1].strip() if row[-1] else "0,00 S"
            amount_match = re.match(r'([\d.,]+)\s*([SH])', amount_str)
            
            if amount_match:
                amount_value = float(amount_match.group(1).replace('.', '').replace(',', '.'))
                amount_type = amount_match.group(2)
                
                # S = Soll (debit), H = Haben (credit)
                amount = -amount_value if amount_type == 'S' else amount_value
            else:
                amount = 0.0
            
            return {
                'date': transaction_date,
                'recipient': recipient,
                'reference': reference,
                'amount': amount,
                'foreign_iban': foreign_iban
            }
            
        except Exception as e:
            print(f"Error parsing transaction at row {start_idx}: {e}")
            return None
    
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
        """
        Main entry point: Detect document type and parse accordingly
        """
        filename = os.path.basename(filepath).lower()
        
        # Try to detect bank statement
        text = self.extract_text_from_pdf(filepath)
        text_lower = text.lower()
        
        if 'volksbank' in text_lower or 'vbr' in filename:
            return self.parse_bank_statement_vbr(filepath)
        
        if 'deutsche kreditbank' in text_lower or 'dkb' in text_lower or 'kontoauszug_' in filename:
            return self.parse_bank_statement_dkb(filepath)
        
        # Add more parsers for other document types here
        # elif 'sparkasse' in text_lower:
        #     return self.parse_bank_statement_sparkasse(filepath)
        
        # Generic document
        return {
            'type': 'generic',
            'text': text,
            'iban': self.extract_iban(text),
            'date': self.extract_date_from_text(text)
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
