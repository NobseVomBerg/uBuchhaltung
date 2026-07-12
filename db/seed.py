# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Database-Mixin: seed."""
import sqlite3
import os
import json
from decimal import Decimal
from money import to_minor, from_minor
from .core import coa_id


class SeedMixin:
    def _load_seed_json(self, filename):
        """Lädt eine JSON-Datei aus dem seed_data/-Verzeichnis."""
        seed_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'seed_data')
        filepath = os.path.join(seed_dir, filename)
        with open(filepath, 'r', encoding='utf-8') as f:
            return json.load(f)
    def _seed_tax_keys(self):
        """Seed der DATEV-Steuerschlüssel (BU-Schlüssel) aus seed_data/tax_keys.json.

        Quelle: https://help-center.apps.datev.de/documents/1008613
        """
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT COUNT(*) FROM TaxKeys')
        if cursor.fetchone()[0] > 0:
            conn.close()
            return

        data = self._load_seed_json('tax_keys.json')
        rows = [(e['code'], e['description'], e['tax_rate'], e['tax_type']) for e in data]
        cursor.executemany(
            'INSERT OR IGNORE INTO TaxKeys (Code, Description, TaxRate, TaxType) VALUES (?,?,?,?)',
            rows)
        conn.commit()
        conn.close()
    def get_tax_rate_for_bu(self, bu_code: str):
        """Steuersatz für einen BU-Schlüssel aus DB holen.

        Returns:
            float | None: Steuersatz als Dezimalzahl (0.19) oder None.
        """
        if not bu_code:
            return None
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT TaxRate FROM TaxKeys WHERE Code=?', (bu_code,))
        row = cursor.fetchone()
        conn.close()
        return row[0] if row else None
    def _get_bank_coa_ids(self, cursor):
        """Ermittelt COA-IDs die echten Bankkonten entsprechen.

        Basiert auf der Accounts-Tabelle (SKRAccount-Nr.), nicht auf
        einem hart codierten Bereich.  Dadurch wird z.B. 1460
        (Verrechnungskonto) korrekt NICHT als Bankkonto eingestuft.

        Returns:
            set[int]: COA-IDs die Bankkonten sind.
        """
        cursor.execute('''
            SELECT c.ID
            FROM ChartOfAccounts c
            JOIN Accounts a ON c.AccountNumber = a.SKRAccount
            WHERE a.SKRAccount IS NOT NULL
        ''')
        return {row[0] for row in cursor.fetchall()}
    def _seed_asset_categories(self):
        """Seed der BMF-AfA-Kategorien aus seed_data/asset_categories.json."""
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT COUNT(*) FROM AssetCategories')
        if cursor.fetchone()[0] > 0:
            conn.close()
            return

        data = self._load_seed_json('asset_categories.json')
        rows = [(e['name'], e['useful_life_years'], e['depreciation_method'],
                 e['coa_id'], e['notes']) for e in data]
        cursor.executemany('''
            INSERT INTO AssetCategories (Name, UsefulLifeYears, DepreciationMethod, COA_ID, Notes)
            VALUES (?, ?, ?, ?, ?)
        ''', rows)
        conn.commit()
        conn.close()
    def _seed_chart_of_accounts(self):
        """Seed der Standard-SKR-Konten aus seed_data/chart_of_accounts_skr04.json.

        Falls seed_data/private/chart_of_accounts_custom.json existiert,
        werden zusätzlich benutzerspezifische Konten geladen.
        Ein optionaler ``overrides``-Block in der Custom-Datei erlaubt es,
        Felder vorhandener Standard-Konten nachträglich zu ändern
        (z. B. ``PrivateSharePercent``).
        """
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT COUNT(*) FROM ChartOfAccounts')
        if cursor.fetchone()[0] > 0:
            conn.close()
            return

        # Standard-Kontenrahmen
        data = self._load_seed_json('chart_of_accounts_skr04.json')
        framework = data['framework']
        rows = [(coa_id(framework, e['account_number']), framework, e['account_number'],
                 e['name'], e['description'],
                 1 if e['is_standard'] else 0,
                 e.get('private_share_percent', 0))
                for e in data['accounts']]

        # Private Ergänzungen (optional)
        seed_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'seed_data', 'private')
        private_file = os.path.join(seed_dir, 'chart_of_accounts_custom.json')
        overrides = []
        if os.path.exists(private_file):
            with open(private_file, 'r', encoding='utf-8') as f:
                pdata = json.load(f)
            pfw = pdata.get('framework', framework)
            rows += [(coa_id(pfw, e['account_number']), pfw, e['account_number'],
                      e['name'], e['description'],
                      1 if e.get('is_standard') else 0,
                      e.get('private_share_percent', 0))
                     for e in pdata.get('accounts', [])]
            overrides = pdata.get('overrides', [])

        cursor.executemany('''
            INSERT OR IGNORE INTO ChartOfAccounts
                (ID, Framework, AccountNumber, Name, Description, IsStandard, PrivateSharePercent)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        ''', rows)

        # Overrides anwenden (z. B. Privatanteil für Standard-Konten setzen)
        for ov in overrides:
            acct_nr = ov.get('account_number')
            if acct_nr is None:
                continue
            psp = ov.get('private_share_percent')
            if psp is not None:
                cursor.execute('''
                    UPDATE ChartOfAccounts SET PrivateSharePercent = ?
                    WHERE AccountNumber = ? AND Framework = ?
                ''', (psp, acct_nr, ov.get('framework', framework)))

        conn.commit()
        conn.close()
    def is_first_run(self) -> bool:
        """Prüft ob die App noch nicht eingerichtet wurde.

        Gilt als erster Start wenn:
        - kein Kontakt mit ContactType='own' existiert, UND
        - kein echtes Bankkonto (IsCash=0) angelegt ist.
        """
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM Contacts WHERE ContactType='own'")
        has_own = cursor.fetchone()[0] > 0
        cursor.execute("SELECT COUNT(*) FROM Accounts WHERE IsCash=0")
        has_bank = cursor.fetchone()[0] > 0
        conn.close()
        return not has_own and not has_bank
    def load_test_seed_data(self):
        """Lädt Testdaten aus seed_data/test/ (Kontakte, Bankkonten, Belege, Buchungen).

        Idempotent: bereits vorhandene Einträge werden übersprungen.
        Dient sowohl dem Setup-Wizard ('Mit Testdaten starten') als auch
        dem pytest-Fixture db_with_coa.
        """
        seed_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'seed_data', 'test')

        # ── Kontakte ──────────────────────────────────────────────────────────
        contacts_file = os.path.join(seed_dir, 'test_contacts.json')
        if os.path.exists(contacts_file):
            with open(contacts_file, 'r', encoding='utf-8') as f:
                contacts = json.load(f)
            conn = self._get_connection()
            cursor = conn.cursor()
            for c in contacts:
                # Eigene-Daten-Kontakt nur einmalig anlegen
                if c.get('contact_type') == 'own':
                    cursor.execute("SELECT COUNT(*) FROM Contacts WHERE ContactType='own'")
                    if cursor.fetchone()[0] > 0:
                        continue
                conn.close()
                try:
                    self.insert_contact(
                        contact_type=c.get('contact_type', 'customer'),
                        entity_type=c.get('entity_type', 'company'),
                        display_name=c.get('display_name'),
                        customer_number=c.get('customer_number'),
                        abbreviation=c.get('abbreviation', ''),
                        email=c.get('email', ''),
                        phone=c.get('phone', ''),
                        notes=c.get('notes', ''),
                        street=c.get('street', ''),
                        postal_code=c.get('postal_code', ''),
                        city=c.get('city', ''),
                        country=c.get('country', 'DE'),
                        company_name=c.get('company_name', ''),
                        legal_form=c.get('legal_form', ''),
                        tax_id=c.get('tax_id', ''),
                        salutation=c.get('salutation', ''),
                        first_name=c.get('first_name', ''),
                        last_name=c.get('last_name', ''),
                        job_title=c.get('job_title', ''),
                        department=c.get('department', ''),
                        is_primary_contact=c.get('is_primary_contact', 0),
                        type_keys=c.get('type_keys', []),
                        role_keys=c.get('role_keys', []),
                    )
                except Exception:
                    pass  # Duplikat oder Constraint-Fehler – überspringen
                conn = self._get_connection()
                cursor = conn.cursor()
            conn.close()

        # ── Bankkonten ────────────────────────────────────────────────────────
        accounts_file = os.path.join(seed_dir, 'test_accounts.json')
        if os.path.exists(accounts_file):
            with open(accounts_file, 'r', encoding='utf-8') as f:
                accounts = json.load(f)
            for a in accounts:
                self.insert_account(
                    name=a['name'],
                    holder=a.get('holder', ''),
                    number=a.get('number', ''),
                    bic=a.get('bic', ''),
                    bank_name=a.get('bank_name', ''),
                    is_cash=a.get('is_cash', 0),
                    skr_account=a.get('skr_account'),
                )

        # ── Dokumente (Belege) ────────────────────────────────────────────────
        documents_file = os.path.join(seed_dir, 'test_documents.json')
        if os.path.exists(documents_file):
            with open(documents_file, 'r', encoding='utf-8') as f:
                documents = json.load(f)
            for d in documents:
                conn = self._get_connection()
                cursor = conn.cursor()
                cursor.execute('SELECT COUNT(*) FROM Documents WHERE Number=?', (d['number'],))
                exists = cursor.fetchone()[0] > 0
                conn.close()
                if not exists:
                    self.insert_receipt(
                        number=d['number'],
                        date=d['date'],
                        filename=d.get('filename', ''),
                        path=d.get('path', ''),
                        info=d.get('info', ''),
                    )

        # ── Buchungen ─────────────────────────────────────────────────────────
        bookings_file = os.path.join(seed_dir, 'test_bookings.json')
        if os.path.exists(bookings_file):
            with open(bookings_file, 'r', encoding='utf-8') as f:
                bookings_data = json.load(f)

            # Lookup-Maps einmalig aufbauen
            conn = self._get_connection()
            cursor = conn.cursor()
            cursor.execute('SELECT Name, ID FROM Accounts')
            acct_map = {row[0]: row[1] for row in cursor.fetchall()}
            cursor.execute('SELECT AccountNumber, ID FROM ChartOfAccounts')
            coa_map = {row[0]: row[1] for row in cursor.fetchall()}
            conn.close()

            for b in bookings_data:
                doc_nr = b.get('document_number')
                # Duplikat-Prüfung über DocumentNumber + BookingType
                if doc_nr:
                    conn = self._get_connection()
                    cursor = conn.cursor()
                    cursor.execute(
                        "SELECT COUNT(*) FROM Bookings WHERE DocumentNumber=? AND BookingType=?",
                        (doc_nr, b.get('booking_type', 'bank')),
                    )
                    if cursor.fetchone()[0] > 0:
                        conn.close()
                        continue
                    conn.close()

                account_id = acct_map.get(b.get('account_name'))

                # Optionale COA-/Steuerfelder für standalone Einzel-Buchungen
                parent_coa_id = coa_map.get(b.get('coa_number'))
                parent_counter_coa_id = coa_map.get(b.get('counter_coa_number'))
                parent_tax_rate = b.get('tax_rate')
                parent_tax_amount = None
                if parent_tax_rate and b['amount']:
                    parent_tax_amount = round(
                        abs(b['amount']) - abs(b['amount']) / (1 + parent_tax_rate), 2
                    )
                    if b['amount'] < 0:
                        parent_tax_amount = -parent_tax_amount

                parent_id = self.insert_booking(
                    date_booking=b['date'],
                    amount=b['amount'],
                    account_id=account_id,
                    coa_id=parent_coa_id,
                    counter_coa_id=parent_counter_coa_id,
                    tax_rate=parent_tax_rate,
                    tax_amount=parent_tax_amount,
                    recipient_client=b.get('recipient', ''),
                    booking_type=b.get('booking_type', 'bank'),
                    text=b.get('text', ''),
                    document_number=doc_nr,
                )

                for child in b.get('children', []):
                    coa_id = coa_map.get(child.get('coa_number'))
                    counter_coa_id = coa_map.get(child.get('counter_coa_number'))
                    tax_rate = child.get('tax_rate')
                    child_amount = child['amount']
                    tax_amount = None
                    if tax_rate and child_amount:
                        tax_amount = round(
                            abs(child_amount) - abs(child_amount) / (1 + tax_rate), 2
                        )
                        if child_amount < 0:
                            tax_amount = -tax_amount
                    self.insert_booking(
                        date_booking=child['date'],
                        amount=child_amount,
                        coa_id=coa_id,
                        counter_coa_id=counter_coa_id,
                        tax_rate=tax_rate,
                        tax_amount=tax_amount,
                        booking_type='entry',
                        text=child.get('text', ''),
                        document_number=child.get('document_number'),
                        parent_booking_id=parent_id,
                    )

        # ── Artikel ───────────────────────────────────────────────────────────
        articles_file = os.path.join(seed_dir, 'test_articles.json')
        if os.path.exists(articles_file):
            with open(articles_file, 'r', encoding='utf-8') as f:
                articles_data = json.load(f)
            conn = self._get_connection()
            cursor = conn.cursor()
            cursor.execute('SELECT Name FROM Articles')
            existing_article_names = {row[0] for row in cursor.fetchall()}
            conn.close()
            for a in articles_data:
                if a['name'] not in existing_article_names:
                    try:
                        self.insert_article(
                            name=a['name'],
                            unit=a.get('unit', 'Stk.'),
                            unit_price=a.get('unit_price', 0),
                            tax_rate=a.get('tax_rate', 19),
                            description=a.get('description', ''),
                            active=a.get('active', 1),
                        )
                    except Exception:
                        pass

        # ── Anlagegüter ───────────────────────────────────────────────────────
        assets_file = os.path.join(seed_dir, 'test_assets.json')
        if os.path.exists(assets_file):
            with open(assets_file, 'r', encoding='utf-8') as f:
                assets_data = json.load(f)
            conn = self._get_connection()
            cursor = conn.cursor()
            cursor.execute('SELECT Name, PurchaseDate FROM Assets')
            existing_assets = {(row[0], row[1]) for row in cursor.fetchall()}
            cursor.execute('SELECT Name, ID FROM AssetCategories')
            cat_map = {row[0]: row[1] for row in cursor.fetchall()}
            conn.close()
            for a in assets_data:
                key = (a['name'], a['purchase_date'])
                if key not in existing_assets:
                    try:
                        self.insert_asset(
                            name=a['name'],
                            purchase_date=a['purchase_date'],
                            purchase_price=a['purchase_price'],
                            useful_life_years=a['useful_life_years'],
                            description=a.get('description', ''),
                            asset_category_id=cat_map.get(a.get('category_name')),
                            depreciation_method=a.get('depreciation_method', 'linear'),
                            serial_number=a.get('serial_number', ''),
                            location=a.get('location', ''),
                            notes=a.get('notes', ''),
                        )
                    except Exception:
                        pass

        # ── Rechnungen ────────────────────────────────────────────────────────
        invoices_file = os.path.join(seed_dir, 'test_invoices.json')
        if os.path.exists(invoices_file):
            with open(invoices_file, 'r', encoding='utf-8') as f:
                invoices_data = json.load(f)

            # Lookup-Maps aufbauen
            own_rows = self.fetch_contacts(contact_type='own')
            own = own_rows[0] if own_rows else None
            all_customers = self.fetch_contacts(contact_type='customer')
            customers_by_nr = {c[2]: c for c in all_customers}  # c[2] = CustomerNumber
            conn = self._get_connection()
            cursor = conn.cursor()
            cursor.execute('SELECT Name, ID FROM Articles')
            articles_map = {row[0]: row[1] for row in cursor.fetchall()}
            cursor.execute(
                'SELECT ID, Number, BIC, BankName FROM Accounts WHERE IsCash=0 ORDER BY Name ASC LIMIT 1'
            )
            bank_row = cursor.fetchone()
            cursor.execute('SELECT InvoiceNumber FROM Invoices')
            existing_inv = {row[0] for row in cursor.fetchall()}
            conn.close()

            for inv in invoices_data:
                inv_number = inv['invoice_number']
                if inv_number in existing_inv:
                    continue

                # Verkäufer-Snapshot aus eigenem Kontakt
                own_id        = own[0]  if own else None
                seller_name   = own[3]  if own else ''  # display_name
                seller_company= (own[4] or '') if own else ''  # company_name
                seller_street = own[5]  if own else ''  # Street
                seller_plz    = own[6]  if own else ''  # PostalCode
                seller_city   = own[7]  if own else ''  # City
                seller_country= own[8]  if own else 'DE'  # country
                seller_vat_id = own[11] if own else ''  # tax_id
                seller_email  = own[9]  if own else ''  # Email
                seller_phone  = own[10] if own else ''  # Phone

                # Käufer-Snapshot aus Kundenkontakt
                buyer = customers_by_nr.get(inv.get('customer_number'))
                customer_id    = buyer[0]  if buyer else None
                buyer_name     = buyer[3]  if buyer else ''
                buyer_company  = (buyer[4] or '') if buyer else ''
                buyer_street   = buyer[5]  if buyer else ''
                buyer_plz      = buyer[6]  if buyer else ''
                buyer_city     = buyer[7]  if buyer else ''
                buyer_country  = buyer[8]  if buyer else 'DE'
                buyer_vat_id   = buyer[11] if buyer else ''

                # Bankkonto-Snapshot
                bank_account_id = bank_row[0] if bank_row else None
                bank_iban       = bank_row[1] if bank_row else ''
                bank_bic        = bank_row[2] if bank_row else ''
                bank_name       = bank_row[3] if bank_row else ''

                # Positionen aufsummieren
                items = inv.get('items', [])
                sum_net = 0.0
                tax_amount = 0.0
                dominant_tax_rate = 19.0
                for item in items:
                    qty   = item.get('quantity', 1)
                    price = item.get('price_per_unit', 0)
                    rate  = item.get('tax_rate', 19)
                    item_net = round(qty * price, 2)
                    sum_net    += item_net
                    tax_amount += round(item_net * rate / 100, 2)
                    dominant_tax_rate = rate
                sum_net    = round(sum_net, 2)
                tax_amount = round(tax_amount, 2)
                sum_gross  = round(sum_net + tax_amount, 2)

                invoice_data = {
                    'invoice_number':    inv_number,
                    'invoice_date':      inv['invoice_date'],
                    'own_company_id':    own_id,
                    'seller_name':       seller_name,
                    'seller_company':    seller_company,
                    'seller_street':     seller_street,
                    'seller_postal_code':seller_plz,
                    'seller_city':       seller_city,
                    'seller_country':    seller_country,
                    'seller_vat_id':     seller_vat_id,
                    'seller_email':      seller_email,
                    'seller_phone':      seller_phone,
                    'customer_id':       customer_id,
                    'buyer_name':        buyer_name,
                    'buyer_company':     buyer_company,
                    'buyer_street':      buyer_street,
                    'buyer_postal_code': buyer_plz,
                    'buyer_city':        buyer_city,
                    'buyer_country':     buyer_country,
                    'buyer_vat_id':      buyer_vat_id,
                    'currency':          'EUR',
                    'delivery_date':     inv.get('delivery_date', inv['invoice_date']),
                    'payment_terms':     inv.get('payment_terms', 'Zahlungsziel 30 Tage netto'),
                    'payment_due_date':  inv.get('payment_due_date'),
                    'bank_account_id':   bank_account_id,
                    'bank_name':         bank_name,
                    'bank_iban':         bank_iban,
                    'bank_bic':          bank_bic,
                    'tax_category':      'S',
                    'tax_rate':          dominant_tax_rate,
                    'sum_net':           sum_net,
                    'tax_amount':        tax_amount,
                    'sum_gross':         sum_gross,
                    'amount_due':        sum_gross,
                    'status':            inv.get('status', 'finalized'),
                }
                try:
                    invoice_id = self.insert_invoice(invoice_data)
                    for pos, item in enumerate(items, 1):
                        art_name = item.get('article_name', '')
                        art_id   = articles_map.get(art_name)
                        qty      = item.get('quantity', 1)
                        price    = item.get('price_per_unit', 0)
                        rate     = item.get('tax_rate', 19)
                        self.insert_invoice_item({
                            'invoice_id':    invoice_id,
                            'position':      pos,
                            'article_id':    art_id,
                            'description':   art_name or item.get('description', ''),
                            'quantity':      qty,
                            'unit':          'C62',
                            'price_per_unit':price,
                            'total_net':     round(qty * price, 2),
                            'tax_category':  'S',
                            'tax_rate':      rate,
                        })
                except Exception:
                    pass  # Duplikat oder Fehler – überspringen

        # ── Angebote (Quotes) ─────────────────────────────────────────────────
        quotes_file = os.path.join(seed_dir, 'test_quotes.json')
        if os.path.exists(quotes_file):
            with open(quotes_file, 'r', encoding='utf-8') as f:
                quotes_data = json.load(f)

            own_rows = self.fetch_contacts(contact_type='own')
            own = own_rows[0] if own_rows else None
            customers_by_nr = {c[2]: c for c in self.fetch_contacts(contact_type='customer')}
            conn = self._get_connection()
            cursor = conn.cursor()
            cursor.execute('SELECT Name, ID FROM Articles')
            articles_map = {row[0]: row[1] for row in cursor.fetchall()}
            cursor.execute('SELECT InvoiceNumber FROM Invoices')
            existing_docs = {row[0] for row in cursor.fetchall()}
            conn.close()

            for q in quotes_data:
                q_number = q['quote_number']
                if q_number in existing_docs:
                    continue
                buyer = customers_by_nr.get(q.get('customer_number'))
                items = q.get('items', [])
                sum_net = 0.0
                tax_amount = 0.0
                dominant_tax_rate = 19.0
                for item in items:
                    item_net = round(item.get('quantity', 1) * item.get('price_per_unit', 0), 2)
                    sum_net += item_net
                    tax_amount += round(item_net * item.get('tax_rate', 19) / 100, 2)
                    dominant_tax_rate = item.get('tax_rate', 19)
                sum_net = round(sum_net, 2)
                tax_amount = round(tax_amount, 2)
                sum_gross = round(sum_net + tax_amount, 2)

                quote_data = {
                    'invoice_number':    q_number,
                    'invoice_date':      q['quote_date'],
                    'own_company_id':    own[0] if own else None,
                    'seller_name':       own[3] if own else '',
                    'seller_company':    (own[4] or '') if own else '',
                    'seller_street':     own[5] if own else '',
                    'seller_postal_code':own[6] if own else '',
                    'seller_city':       own[7] if own else '',
                    'seller_country':    own[8] if own else 'DE',
                    'seller_vat_id':     own[11] if own else '',
                    'seller_email':      own[9] if own else '',
                    'seller_phone':      own[10] if own else '',
                    'customer_id':       buyer[0] if buyer else None,
                    'buyer_name':        buyer[3] if buyer else '',
                    'buyer_company':     (buyer[4] or '') if buyer else '',
                    'buyer_street':      buyer[5] if buyer else '',
                    'buyer_postal_code': buyer[6] if buyer else '',
                    'buyer_city':        buyer[7] if buyer else '',
                    'buyer_country':     buyer[8] if buyer else 'DE',
                    'buyer_vat_id':      buyer[11] if buyer else '',
                    'currency':          'EUR',
                    'tax_category':      'S',
                    'tax_rate':          dominant_tax_rate,
                    'sum_net':           sum_net,
                    'tax_amount':        tax_amount,
                    'sum_gross':         sum_gross,
                    'amount_due':        sum_gross,
                    'status':            q.get('status', 'draft'),
                    'document_type':     'quote',
                    'valid_until':       q.get('valid_until'),
                    'intro_text':        q.get('intro_text') or None,
                    'closing_text':      q.get('closing_text') or None,
                }
                try:
                    quote_id = self.insert_invoice(quote_data)
                    for pos, item in enumerate(items, 1):
                        art_name = item.get('article_name', '')
                        self.insert_invoice_item({
                            'invoice_id':    quote_id,
                            'position':      pos,
                            'article_id':    articles_map.get(art_name),
                            'description':   art_name or item.get('description', ''),
                            'quantity':      item.get('quantity', 1),
                            'unit':          'C62',
                            'price_per_unit':item.get('price_per_unit', 0),
                            'total_net':     round(item.get('quantity', 1) * item.get('price_per_unit', 0), 2),
                            'tax_category':  'S',
                            'tax_rate':      item.get('tax_rate', 19),
                        })
                except Exception:
                    pass

        # ── Arbeitszeiten ─────────────────────────────────────────────────────
        worktimes_file = os.path.join(seed_dir, 'test_worktimes.json')
        if os.path.exists(worktimes_file):
            with open(worktimes_file, 'r', encoding='utf-8') as f:
                worktimes_data = json.load(f)

            # Lookup: Personen (Mitarbeiter/own) und Kunden über CustomerNumber
            persons = list(self.fetch_contacts(contact_type='employee'))
            persons += list(self.fetch_contacts(contact_type='own'))
            person_by_nr = {p[2]: p[0] for p in persons if p[2]}
            default_person = persons[0][0] if persons else None
            customers = {c[2]: c for c in self.fetch_contacts(contact_type='customer') if c[2]}

            for w in worktimes_data:
                person_id = person_by_nr.get(w.get('person_number'), default_person)
                if not person_id:
                    continue
                cust = customers.get(w.get('customer_number'))
                customer_id = cust[0] if cust else None
                mode = w.get('location_mode', 'customer')
                if mode == 'own':
                    own_rows = list(self.fetch_contacts(contact_type='own'))
                    city = (own_rows[0][7] if own_rows else '') or ''
                elif mode == 'customer':
                    city = (cust[7] if cust else '') or ''
                else:
                    city = w.get('location_city', '')
                try:
                    self.insert_worktime(
                        person_id=person_id,
                        date=w['date'],
                        kind=w.get('kind', 'work'),
                        customer_id=customer_id,
                        start_time=w.get('start_time', ''),
                        end_time=w.get('end_time', ''),
                        pause_minutes=w.get('pause_minutes', 0),
                        location_mode=mode,
                        location_city=city,
                        note=w.get('note', ''),
                    )
                except Exception:
                    pass
