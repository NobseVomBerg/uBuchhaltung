"""Database-Mixin: schema."""
import sqlite3
import os
import json
from decimal import Decimal
from money import to_minor, from_minor


class SchemaMixin:
    def initialize_database(self):
        conn = self._get_connection()
        cursor = conn.cursor()

        # Chart of Accounts = Standard Konto Rahmen, 03/04 in Germany or 07 for Austria
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS ChartOfAccounts (
                ID INTEGER PRIMARY KEY,
                Framework INTEGER,
                AccountNumber INTEGER,
                Name TEXT,
                Description TEXT,
                IsStandard INTEGER DEFAULT 0,
                PrivateSharePercent INTEGER DEFAULT 0,
                ShowInMenu INTEGER DEFAULT 1,
                UNIQUE(Framework, AccountNumber)
            )
        ''')

        # Categories for private Things (Immobilien, Krankenbelege, Versicherungen, ...)
        # Text: zusätzliche Infos, z.B. für welchen Teil der Steuererklärung
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS Categories (
                ID INTEGER PRIMARY KEY AUTOINCREMENT,
                Name TEXT,
                Text TEXT,
                Parent_ID INTEGER
            )
        ''')

        # Accounts table for bank accounts and cash accounts
        # Number: IBAN or CreditCardNr
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS Accounts (
                ID INTEGER PRIMARY KEY AUTOINCREMENT,
                Name TEXT NOT NULL,
                Owner TEXT,
                Number TEXT,
                BIC TEXT,
                BankName TEXT,
                IsCash INTEGER DEFAULT 0,
                SKRAccount INTEGER,
                UNIQUE(Name)
            )
        ''')

        # ── Contacts: normalized 4-table structure (Option C) ─────────────────────
        # Base table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS Contacts (
                ID             INTEGER PRIMARY KEY AUTOINCREMENT,
                ContactType    TEXT NOT NULL DEFAULT 'customer',
                EntityType     TEXT NOT NULL DEFAULT 'company',
                DisplayName    TEXT,
                CustomerNumber TEXT,
                Abbreviation   TEXT,
                Email          TEXT,
                Phone          TEXT,
                Notes          TEXT,
                Logo           TEXT,
                UNIQUE(CustomerNumber),
                UNIQUE(Abbreviation)
            )
        ''')

        # Addresses (1:n – AddressType='main' is the primary address)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS ContactAddresses (
                ID           INTEGER PRIMARY KEY AUTOINCREMENT,
                ContactID    INTEGER NOT NULL,
                AddressType  TEXT NOT NULL DEFAULT 'main',
                AddressLine1 TEXT,
                Street       TEXT,
                PostalCode   TEXT,
                City         TEXT,
                Country      TEXT DEFAULT 'DE',
                UNIQUE(ContactID, AddressType),
                FOREIGN KEY (ContactID) REFERENCES Contacts(ID) ON DELETE CASCADE
            )
        ''')

        # Person-specific fields (1:1)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS PersonDetails (
                ContactID          INTEGER PRIMARY KEY,
                Salutation         TEXT,
                Title              TEXT,
                FirstName          TEXT,
                LastName           TEXT,
                DateOfBirth        TEXT,
                CompanyContactID   INTEGER,
                CompanyName_Free   TEXT,
                JobTitle           TEXT,
                Department         TEXT,
                IsPrimaryContact   INTEGER DEFAULT 0,
                FOREIGN KEY (ContactID) REFERENCES Contacts(ID) ON DELETE CASCADE,
                FOREIGN KEY (CompanyContactID) REFERENCES Contacts(ID)
            )
        ''')

        # Company-specific fields (1:1)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS CompanyDetails (
                ContactID    INTEGER PRIMARY KEY,
                CompanyName  TEXT,
                LegalForm    TEXT,
                TaxID        TEXT,
                BuyerRouteID TEXT,
                FOREIGN KEY (ContactID) REFERENCES Contacts(ID) ON DELETE CASCADE
            )
        ''')

        # Contact ↔ Type (n:m) – Mehrfach-Kontakttypen (außer 'own')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS ContactTypeLinks (
                ContactID INTEGER NOT NULL,
                TypeKey   TEXT    NOT NULL,
                PRIMARY KEY (ContactID, TypeKey),
                FOREIGN KEY (ContactID) REFERENCES Contacts(ID) ON DELETE CASCADE
            )
        ''')

        # Person ↔ Fachliche Rollen (n:m)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS PersonRoles (
                ContactID INTEGER NOT NULL,
                RoleKey   TEXT    NOT NULL,
                PRIMARY KEY (ContactID, RoleKey),
                FOREIGN KEY (ContactID) REFERENCES Contacts(ID) ON DELETE CASCADE
            )
        ''')

        # Documents (Belege)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS Documents (
                ID INTEGER PRIMARY KEY AUTOINCREMENT,
                Number TEXT,
                Date DATE,
                Filename TEXT,
                Path TEXT,
                Info TEXT
            )
        ''')

        # WorkTimes (Arbeitszeiten je Person)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS WorkTimes (
                ID            INTEGER PRIMARY KEY AUTOINCREMENT,
                PersonID      INTEGER NOT NULL,            -- FK Contacts(ID) (employee/own)
                Date          DATE    NOT NULL,
                Kind          TEXT    NOT NULL DEFAULT 'work',   -- 'work'|'vacation'|'sick'|'holiday'
                CustomerID    INTEGER,                     -- FK Contacts(ID), nullable
                StartTime     TEXT,                        -- 'HH:MM'
                EndTime       TEXT,                        -- 'HH:MM'
                PauseMinutes  INTEGER DEFAULT 0,
                LocationMode  TEXT    DEFAULT 'customer',  -- 'own'|'customer'|'other'
                LocationCity  TEXT,                        -- aufgelöster Ort (Snapshot)
                Note          TEXT,
                PauseText     TEXT,                        -- Pausenzeiten als Text, z.B. '12:00-12:30'
                FOREIGN KEY (PersonID)   REFERENCES Contacts(ID) ON DELETE CASCADE,
                FOREIGN KEY (CustomerID) REFERENCES Contacts(ID) ON DELETE SET NULL
            )
        ''')
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_worktimes_person_date
            ON WorkTimes(PersonID, Date)
        ''')

        # BookingGroups (Helper for linking Documents and Bookings with m:n together)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS BookingGroups (
                ID INTEGER PRIMARY KEY AUTOINCREMENT,
                Description TEXT,
                CreatedDate DATE,
                TotalAmount INTEGER
            )
        ''')

        # Bookings (replaces Zahlung with enhanced structure)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS Bookings (
                ID INTEGER PRIMARY KEY AUTOINCREMENT,
                DateBooking DATE NOT NULL,
                DateTax DATE,
                BookingGroup_ID INTEGER,
                Account_ID INTEGER,
                ForeignBankAccount TEXT,
                RecipientClient TEXT,
                Contact_ID INTEGER,
                COA_ID INTEGER,
                CounterCOA_ID INTEGER,
                Category_ID INTEGER,
                Amount INTEGER NOT NULL,
                Currency TEXT DEFAULT 'EUR',
                TaxRate REAL,
                TaxAmount INTEGER,
                Text TEXT,
                DocumentNumber TEXT,
                BookingType TEXT DEFAULT 'entry',
                ParentBooking_ID INTEGER,
                Status TEXT,
                FOREIGN KEY (BookingGroup_ID) REFERENCES BookingGroups(ID),
                FOREIGN KEY (Account_ID) REFERENCES Accounts(ID),
                FOREIGN KEY (Contact_ID) REFERENCES Contacts(ID),
                FOREIGN KEY (COA_ID) REFERENCES ChartOfAccounts(ID),
                FOREIGN KEY (CounterCOA_ID) REFERENCES ChartOfAccounts(ID),
                FOREIGN KEY (Category_ID) REFERENCES Categories(ID),
                FOREIGN KEY (ParentBooking_ID) REFERENCES Bookings(ID)
            )
        ''')

        # BookingDocuments (Junction table for many-to-many relationship)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS BookingDocuments (
                ID INTEGER PRIMARY KEY AUTOINCREMENT,
                Booking_ID INTEGER NOT NULL,
                Document_ID INTEGER NOT NULL,
                RelationType TEXT,
                FOREIGN KEY (Booking_ID) REFERENCES Bookings(ID),
                FOREIGN KEY (Document_ID) REFERENCES Documents(ID),
                UNIQUE(Booking_ID, Document_ID)
            )
        ''')

        # Articles table for product/service catalog
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS Articles (
                ID INTEGER PRIMARY KEY AUTOINCREMENT,
                Name TEXT NOT NULL,
                Unit TEXT DEFAULT 'Stk.',
                UnitPrice INTEGER DEFAULT 0,
                TaxRate REAL DEFAULT 19,
                Description TEXT,
                Active INTEGER DEFAULT 1
            )
        ''')

        # NumberRanges table for invoice/receipt numbering
        # Type: 'invoice' (Ausgangsrechnungen), 'receipt_company' (Belege Firma), 'receipt_category' (Belege Kategorien)
        # Format: e.g., 'R' for Rechnungen, 'B' for Belege, etc.
        # Suffix: optional suffix for subdivision (e.g., '_A', '_B') – appended after the number
        # NumberFormat: format template, e.g. '{yy}{l}{nnn}{s}' → '26F001' or '26F001_A'
        # CurrentNumber: the last used number in this range
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS NumberRanges (
                ID INTEGER PRIMARY KEY AUTOINCREMENT,
                Type TEXT NOT NULL,
                Year INTEGER NOT NULL,
                Letter TEXT NOT NULL,
                Prefix TEXT DEFAULT '',
                CurrentNumber INTEGER DEFAULT 0,
                Description TEXT,
                NumberFormat TEXT DEFAULT '{yy}{l}{nnn}{s}',
                UNIQUE(Type, Year, Letter, Prefix)
            )
        ''')

        # Invoices table for storing issued invoices (XRechnung-compliant)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS Invoices (
                ID INTEGER PRIMARY KEY AUTOINCREMENT,
                InvoiceNumber TEXT UNIQUE NOT NULL,
                InvoiceDate DATE NOT NULL,
                
                -- Seller (own company) - Snapshot
                OwnCompanyId INTEGER,
                SellerName TEXT NOT NULL,
                SellerCompany TEXT NOT NULL,
                SellerStreet TEXT,
                SellerPostalCode TEXT,
                SellerCity TEXT,
                SellerCountry TEXT DEFAULT 'DE',
                SellerVATID TEXT,
                SellerEmail TEXT,
                SellerPhone TEXT,
                
                -- Buyer (customer) - Snapshot
                CustomerId INTEGER,
                BuyerName TEXT NOT NULL,
                BuyerCompany TEXT NOT NULL,
                BuyerStreet TEXT,
                BuyerPostalCode TEXT,
                BuyerCity TEXT,
                BuyerCountry TEXT DEFAULT 'DE',
                BuyerVATID TEXT,
                BuyerReference TEXT,
                BuyerRouteID TEXT,
                
                -- Order reference
                OrderNumber TEXT,
                
                -- XRechnung specific
                Currency TEXT DEFAULT 'EUR',
                DeliveryDate DATE,
                
                -- Payment terms
                PaymentTerms TEXT,
                PaymentDueDate DATE,
                SkontoDays INTEGER,
                SkontoPercent REAL,
                
                -- Bank details - Snapshot
                BankAccountId INTEGER,
                BankName TEXT,
                BankIBAN TEXT,
                BankBIC TEXT,
                
                -- Totals
                TaxCategory TEXT DEFAULT 'S',
                TaxRate REAL NOT NULL,
                SumNet INTEGER NOT NULL,
                TaxAmount INTEGER NOT NULL,
                SumGross INTEGER NOT NULL,
                AmountDue INTEGER NOT NULL,
                
                -- Management
                Status TEXT DEFAULT 'draft',
                PDFPath TEXT,
                XMLPath TEXT,
                CreatedAt DATETIME DEFAULT CURRENT_TIMESTAMP,
                UpdatedAt DATETIME,

                -- Angebots-/Dokument-Erweiterung (ANS ENDE anhängen: der Rechnungscode
                -- liest Spalten positionsbasiert – neue Felder ab Index 45, niemals
                -- in die Mitte einfügen!)
                DocumentType TEXT NOT NULL DEFAULT 'invoice',  -- 'invoice' | 'quote'
                ValidUntil DATE,                               -- Angebot gültig bis
                IntroText TEXT,                                -- Fließtext vor Positionen
                ClosingText TEXT,                              -- Fließtext nach Positionen
                SourceQuoteId INTEGER,                         -- Rechnung: Quell-Angebot

                FOREIGN KEY (OwnCompanyId) REFERENCES Contacts(ID),
                FOREIGN KEY (CustomerId) REFERENCES Contacts(ID),
                FOREIGN KEY (BankAccountId) REFERENCES Accounts(ID),
                FOREIGN KEY (SourceQuoteId) REFERENCES Invoices(ID) ON DELETE SET NULL
            )
        ''')

        # InvoiceItems table for invoice line items
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS InvoiceItems (
                ID INTEGER PRIMARY KEY AUTOINCREMENT,
                InvoiceId INTEGER NOT NULL,
                Position INTEGER NOT NULL,
                ArticleId INTEGER,
                Description TEXT NOT NULL,
                Quantity REAL NOT NULL,
                Unit TEXT DEFAULT 'C62',
                PricePerUnit INTEGER NOT NULL,
                TotalNet INTEGER NOT NULL,
                TaxCategory TEXT DEFAULT 'S',
                TaxRate REAL NOT NULL,
                FOREIGN KEY (InvoiceId) REFERENCES Invoices(ID) ON DELETE CASCADE,
                FOREIGN KEY (ArticleId) REFERENCES Articles(ID)
            )
        ''')

        conn.commit()
        conn.close()
        
        # Restliches Schema (Anlagen, Zahlungen, Steuerschlüssel, Indizes, Seeds)
        self._create_extended_schema()

        # Ensure the default "Kasse" account exists
        self.ensure_kasse_exists()
    def _create_extended_schema(self):
        """Legt die übrigen Tabellen, Indizes und Seed-Daten an.

        Keine Migrationen: Bei Schema-Änderungen wird die DB gelöscht und neu
        aufgesetzt (kein produktiver Bestand). Alle Spalten gehören direkt in
        die jeweilige CREATE-TABLE-Definition.
        """
        conn = self._get_connection()
        cursor = conn.cursor()

        # AssetCategories table (AfA-Tabellen nach BMF)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS AssetCategories (
                ID INTEGER PRIMARY KEY AUTOINCREMENT,
                Name TEXT NOT NULL,
                UsefulLifeYears INTEGER NOT NULL,
                DepreciationMethod TEXT DEFAULT 'linear',
                COA_ID INTEGER,
                Notes TEXT,
                FOREIGN KEY (COA_ID) REFERENCES ChartOfAccounts(ID)
            )
        ''')

        # Assets table (Anlagenverzeichnis)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS Assets (
                ID INTEGER PRIMARY KEY AUTOINCREMENT,
                InventoryNumber TEXT UNIQUE,
                Name TEXT NOT NULL,
                Description TEXT,
                AssetCategory_ID INTEGER,
                COA_ID INTEGER,
                PurchaseDate DATE NOT NULL,
                PurchasePrice INTEGER NOT NULL,
                UsefulLifeYears INTEGER NOT NULL,
                DepreciationMethod TEXT DEFAULT 'linear',
                SerialNumber TEXT,
                Location TEXT,
                Supplier_ID INTEGER,
                Document_ID INTEGER,
                Booking_ID INTEGER,
                SaleDate DATE,
                SalePrice INTEGER,
                Status TEXT DEFAULT 'active',
                Notes TEXT,
                Parent_ID INTEGER,
                CreatedAt DATETIME DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (AssetCategory_ID) REFERENCES AssetCategories(ID),
                FOREIGN KEY (COA_ID) REFERENCES ChartOfAccounts(ID),
                FOREIGN KEY (Supplier_ID) REFERENCES Contacts(ID),
                FOREIGN KEY (Document_ID) REFERENCES Documents(ID),
                FOREIGN KEY (Booking_ID) REFERENCES Bookings(ID),
                FOREIGN KEY (Parent_ID) REFERENCES Assets(ID)
            )
        ''')

        # AssetDepreciations table (AfA-Plan)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS AssetDepreciations (
                ID INTEGER PRIMARY KEY AUTOINCREMENT,
                Asset_ID INTEGER NOT NULL,
                Year INTEGER NOT NULL,
                DepreciationAmount INTEGER NOT NULL,
                BookValue INTEGER NOT NULL,
                Booking_ID INTEGER,
                Status TEXT DEFAULT 'planned',
                BookedAt DATETIME,
                FOREIGN KEY (Asset_ID) REFERENCES Assets(ID) ON DELETE CASCADE,
                FOREIGN KEY (Booking_ID) REFERENCES Bookings(ID),
                UNIQUE(Asset_ID, Year)
            )
        ''')

        # InvoicePayments: Zahlungsverknüpfungen Rechnung ↔ Buchung
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS InvoicePayments (
                ID INTEGER PRIMARY KEY AUTOINCREMENT,
                InvoiceID INTEGER NOT NULL,
                BookingID INTEGER NOT NULL,
                Amount INTEGER NOT NULL,
                PaymentDate DATE NOT NULL,
                Notes TEXT,
                CreatedAt DATETIME DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (InvoiceID) REFERENCES Invoices(ID) ON DELETE CASCADE,
                FOREIGN KEY (BookingID) REFERENCES Bookings(ID)
            )
        ''')

        # DATEV Steuerschlüssel (BU-Schlüssel) → Steuersatz-Zuordnung
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS TaxKeys (
                Code TEXT PRIMARY KEY,
                Description TEXT NOT NULL,
                TaxRate REAL,
                TaxType TEXT
            )
        ''')

        # Performance-Indizes für häufige Queries auf Bookings
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_bookings_type   ON Bookings(BookingType)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_bookings_parent ON Bookings(ParentBooking_ID)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_bookings_group  ON Bookings(BookingGroup_ID)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_bookings_date   ON Bookings(DateBooking)")

        conn.commit()
        conn.close()

        # Seed-Daten aus seed_data/ laden
        self._seed_chart_of_accounts()
        self._seed_asset_categories()
        self._seed_tax_keys()
    def ensure_kasse_exists(self):
        """Ensure the default 'Kasse' account exists and cannot be deleted"""
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT COUNT(*) FROM Accounts WHERE IsCash = 1')
        count = cursor.fetchone()[0]
        if count == 0:
            cursor.execute('''
                INSERT INTO Accounts (Name, Owner, Number, BIC, BankName, IsCash)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', ("Kasse", "", "", "", "", 1))
            conn.commit()
        conn.close()
