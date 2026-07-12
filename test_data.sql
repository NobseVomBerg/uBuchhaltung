-- Summierung von Amount für Privateentnahmen + Privateinlagen für einen Zeitraum
SELECT COALESCE(SUM(Amount), 0) AS total FROM Bookings WHERE (COA_ID=10 OR CounterCOA_ID=11) AND DateBooking BETWEEN '2024-01-01' AND '2024-01-31';

-- Summierung von Amount für einen Zeitraum für das angegebene SKR-Konto
SELECT COALESCE(SUM(b.Amount), 0) AS total FROM Bookings b
WHERE EXISTS (
  SELECT 1 FROM ChartOfAccounts c WHERE c.AccountNumber = 3806
    AND (c.ID = b.COA_ID OR c.ID = b.CounterCOA_ID)
)
AND b.DateBooking BETWEEN '2024-01-01' AND '2024-01-31';

-- Gesamte Datensätze für einen Zeitraum für das angegebene SKR-Gegenkonto
SELECT * FROM Bookings b
WHERE EXISTS (
  SELECT 1 FROM ChartOfAccounts c WHERE c.AccountNumber = 4400
    AND (c.ID = b.CounterCOA_ID)
)
AND b.DateBooking BETWEEN '2024-01-01' AND '2024-01-31';






-- Test Data for uBuchhaltung Database
-- Execute these statements to populate the database with sample data

INSERT INTO Articles (Name, Unit, UnitPrice, TaxRate, Description, Active) VALUES
('Fleisch','kg',10,7,'Frisch vom Viech',1),
('Käse','kg',9,7,'gelb und rund',1),
('Brot','Stk.',2.5,7,'körnig',1),
('Fisch','kg',11,7,'aus dem Wasser',1);

-- =============================================================================
-- 1. ChartOfAccounts (Standardkontenrahmen SKR 03/04)
-- =============================================================================

-- SKR 03 - Aktiva
INSERT INTO ChartOfAccounts (Framework, AccountNumber, Name, Description) VALUES
(3, 1000, 'Kasse', 'Bargeld'),
(3, 1200, 'Bank', 'Bankguthaben'),
(3, 1400, 'Forderungen aus Lieferungen und Leistungen', 'Debitorenkonto'),
(3, 1576, 'Abziehbare Vorsteuer 19%', 'Vorsteuer'),
(3, 1571, 'Abziehbare Vorsteuer 7%', 'Vorsteuer ermäßigt');

-- SKR 03 - Passiva & Aufwendungen
INSERT INTO ChartOfAccounts (Framework, AccountNumber, Name, Description) VALUES
(3, 1710, 'Umsatzsteuer 19%', 'Umsatzsteuer Regelsteuersatz'),
(3, 1776, 'Umsatzsteuer 7%', 'Umsatzsteuer ermäßigt'),
(3, 4400, 'Wareneingang', 'Wareneinkauf'),
(3, 4980, 'Bürobedarf', 'Büromaterial'),
(3, 6300, 'Löhne und Gehälter', 'Personalkosten'),
(3, 6815, 'Betriebsbedarf', 'Betriebsausgaben'),
(3, 6825, 'Werkzeuge und Kleingeräte', 'Werkzeug'),
(3, 6030, 'Zinsaufwendungen', 'Zinsen'),
(3, 4120, 'Erlöse 19% USt', 'Umsatzerlöse');

-- SKR 04 - Alternative
INSERT INTO ChartOfAccounts (Framework, AccountNumber, Name, Description) VALUES
(4, 1600, 'Verbindlichkeiten aus Lieferungen und Leistungen', 'Kreditorenkonto'),
(4, 3400, 'Wareneingang', 'Wareneinkauf SKR04'),
(4, 6815, 'Betriebsbedarf', 'Betriebsausgaben SKR04'),
(4, 8400, 'Erlöse 19% USt', 'Umsatzerlöse SKR04');

-- =============================================================================
-- 2. Categories (Buchungskategorien)
-- =============================================================================

-- Hauptkategorien
INSERT INTO Categories (Name, Text, Parent_ID) VALUES
('Einnahmen', 'Alle Einnahmen', NULL),
('Ausgaben', 'Alle Ausgaben', NULL),
('Privat', 'Private Ausgaben für Steuererklärung', NULL),
('Geschäftlich', 'Betriebsausgaben', NULL);

-- Unterkategorien Einnahmen
INSERT INTO Categories (Name, Text, Parent_ID) VALUES
('Produktverkauf', 'Verkauf von Waren', 1),
('Dienstleistungen', 'Erbrachte Dienstleistungen', 1),
('Zinserträge', 'Bankzinsen', 1);

-- Unterkategorien Ausgaben
INSERT INTO Categories (Name, Text, Parent_ID) VALUES
('Wareneinkauf', 'Einkauf von Handelswaren', 2),
('Büromaterial', 'Bürobedarf und Verbrauchsmaterial', 2),
('Marketing', 'Werbung und Marketing', 2),
('Versicherungen', 'Geschäfts- und Privatversicherungen', 2),
('KFZ', 'Kraftfahrzeugkosten', 2);

-- Private Kategorien
INSERT INTO Categories (Name, Text, Parent_ID) VALUES
('Immobilien', 'Eigenheim - Anlage V', 3),
('Krankenversicherung', 'Gesundheitskosten - Sonderausgaben', 3),
('Altersvorsorge', 'Riester/Rürup - Sonderausgaben', 3),
('Spenden', 'Gemeinnützige Spenden - Sonderausgaben', 3);

-- =============================================================================
-- 3. Accounts (Bankkonten und Kasse)
-- =============================================================================

-- Kasse wird automatisch bei DB-Init erstellt, hier weitere Konten:
INSERT INTO Accounts (Name, Owner, Number, BIC, BankName, IsCash) VALUES
('Geschäftskonto Volksbank', 'Max Mustermann', 'DE89370400440532013000', 'COBADEFFXXX', 'Volksbank Rottweil', 0),
('Privatkonto Sparkasse', 'Max Mustermann', 'DE12500105170648489890', 'SPUEDE2UXXX', 'Sparkasse Schwarzwald-Baar', 0),
('PayPal Business', 'Max Mustermann', 'paypal@mustermann.de', '', 'PayPal', 0),
('Kreditkarte VISA', 'Max Mustermann', '4111111111111111', '', 'DKB Bank', 0);

-- =============================================================================
-- 4. Contacts (Kunden, Lieferanten, eigene Daten, Versicherungen)
-- =============================================================================

-- Eigene Daten (für Rechnungserstellung)
INSERT INTO Contacts (ContactType, CustomerNumber, Name, Company, Street, PostalCode, City, Country, Email, Phone, TaxID, Notes) VALUES
('own', '', 'Max Mustermann', 'Mustermann GmbH', 'Hauptstraße 123', '78628', 'Rottweil', 'Deutschland', 'info@mustermann.de', '+49 741 123456', 'DE123456789', 'Geschäftsführer und Inhaber');

-- Kunden
INSERT INTO Contacts (ContactType, CustomerNumber, Name, Company, Street, PostalCode, City, Country, Email, Phone, TaxID, Notes) VALUES
('customer', 'K-10001', 'Anna Schmidt', 'Schmidt & Partner GmbH', 'Bahnhofstraße 45', '78054', 'Villingen-Schwenningen', 'Deutschland', 'a.schmidt@schmidt-partner.de', '+49 7721 987654', 'DE987654321', 'Stammkunde seit 2020'),
('customer', 'K-10002', 'Thomas Weber', 'Weber IT Solutions', 'Technologiepark 8', '78048', 'VS-Schwenningen', 'Deutschland', 'thomas@weber-it.de', '+49 7720 556677', 'DE456789123', 'IT-Dienstleister'),
('customer', 'K-10003', 'Maria Schneider', '', 'Waldweg 12', '78647', 'Trossingen', 'Deutschland', 'm.schneider@email.de', '+49 7425 334455', '', 'Privatkunde'),
('customer', 'K-10004', 'Peter Müller', 'Müller Bau AG', 'Industriestraße 99', '78628', 'Rottweil', 'Deutschland', 'p.mueller@mueller-bau.de', '+49 741 223344', 'DE789123456', 'Bauprojekte');

-- Lieferanten
INSERT INTO Contacts (ContactType, CustomerNumber, Name, Company, Street, PostalCode, City, Country, Email, Phone, TaxID, Notes) VALUES
('supplier', 'L-20001', 'Hans Fischer', 'Fischer Großhandel GmbH', 'Logistikzentrum 1', '78050', 'VS-Schwenningen', 'Deutschland', 'einkauf@fischer-gh.de', '+49 7720 112233', 'DE321654987', 'Hauptlieferant Büromaterial'),
('supplier', 'L-20002', 'Claudia Bauer', 'Bauer Software AG', 'Softwarepark 5', '70173', 'Stuttgart', 'Deutschland', 'vertrieb@bauer-sw.de', '+49 711 445566', 'DE654987321', 'Softwarelizenzen'),
('supplier', 'L-20003', 'Wolfgang Klein', 'Klein Logistik', 'Am Hafen 23', '78462', 'Konstanz', 'Deutschland', 'w.klein@klein-logistik.de', '+49 7531 778899', 'DE147258369', 'Versanddienstleister'),
('supplier', 'L-20004', 'Sabine Koch', 'Koch Consulting', 'Beraterstraße 14', '78462', 'Konstanz', 'Deutschland', 's.koch@koch-consulting.de', '+49 7531 998877', 'DE963852741', 'Unternehmensberatung');

-- Versicherungen
INSERT INTO Contacts (ContactType, CustomerNumber, Name, Company, Street, PostalCode, City, Country, Email, Phone, TaxID, Notes) VALUES
('insurance', 'V-30001', 'Allianz Versicherung', 'Allianz SE', 'Königinstraße 28', '80802', 'München', 'Deutschland', 'service@allianz.de', '+49 89 38000', '', 'Betriebshaftpflicht - Vertrag BH-12345'),
('insurance', 'V-30002', 'HUK-COBURG', 'HUK-COBURG Versicherung', 'Bahnhofsplatz', '96450', 'Coburg', 'Deutschland', 'info@huk.de', '+49 9561 960', '', 'KFZ-Versicherung - Kennzeichen RW-MM 123'),
('insurance', 'V-30003', 'AOK Baden-Württemberg', 'AOK', 'Presselstraße 19', '70191', 'Stuttgart', 'Deutschland', 'service@bw.aok.de', '+49 711 25930', '', 'Krankenversicherung - Mitgl.-Nr. 123456789'),
('insurance', 'V-30004', 'Helvetia Versicherung', 'Helvetia Schweizerische Versicherung', 'Berliner Straße 56', '60311', 'Frankfurt', 'Deutschland', 'info@helvetia.de', '+49 69 13330', '', 'Rechtsschutz - Police RS-987654');

-- Sonstige Kontakte
INSERT INTO Contacts (ContactType, CustomerNumber, Name, Company, Street, PostalCode, City, Country, Email, Phone, TaxID, Notes) VALUES
('other', '1', 'Finanzamt Rottweil', 'Finanzamt Rottweil', 'Hauptstraße 60', '78628', 'Rottweil', 'Deutschland', 'poststelle@fa-rottweil.de', '+49 741 2420', '', 'Steuernummer: 12345/67890'),
('other', '2', 'Stadtwerke Rottweil', 'Stadtwerke Rottweil GmbH', 'Königstraße 51', '78628', 'Rottweil', 'Deutschland', 'info@stadtwerke-rottweil.de', '+49 741 4920', '', 'Strom, Wasser, Gas - Kundennr. SW-456789'),
('other', '3', 'IHK Schwarzwald-Baar-Heuberg', 'Industrie- und Handelskammer', 'Erzbergerstraße 42', '78628', 'Rottweil', 'Deutschland', 'info@rottweil.ihk.de', '+49 741 94090', '', 'Mitgliedsnummer: IHK-2024-123');

-- =============================================================================
-- 5. Documents (Belege)
-- =============================================================================

INSERT INTO Documents (Number, Date, Filename, Path, Info) VALUES
('2025-001', '2025-01-15', 'rechnung_schmidt_2025_001.pdf', './data/Belege/2025/Rechnungen/', 'Rechnung an Schmidt & Partner GmbH'),
('2025-002', '2025-01-20', 'rechnung_weber_2025_002.pdf', './data/Belege/2025/Rechnungen/', 'Rechnung an Weber IT Solutions'),
('2025-003', '2025-02-05', 'eingangsrechnung_fischer_A1234.pdf', './data/Belege/2025/Eingangsrechnungen/', 'Büromaterial von Fischer Großhandel'),
('2025-004', '2025-02-10', 'quittung_tankstelle_20250210.pdf', './data/Belege/2025/Quittungen/', 'Tankquittung Dienstwagen'),
('2025-005', '2025-03-01', 'versicherung_allianz_q1_2025.pdf', './data/Belege/2025/Versicherungen/', 'Quartalsrechnung Betriebshaftpflicht'),
('2025-006', '2025-03-15', 'kontoauszug_vb_maerz_2025.pdf', './data/Belege/2025/Konten/VBR/', 'Kontoauszug Volksbank März 2025'),
('2026-001', '2026-01-05', 'rechnung_mueller_2026_001.pdf', './data/Belege/2026/Rechnungen/', 'Rechnung an Müller Bau AG'),
('2026-002', '2026-01-20', 'softwarelizenz_bauer_2026.pdf', './data/Belege/2026/Eingangsrechnungen/', 'Jahres-Softwarelizenz von Bauer Software'),
('2026-003', '2026-02-01', 'krankenkasse_aok_2026_01.pdf', './data/Belege/2026/Versicherungen/', 'AOK Krankenversicherung Januar 2026');

-- =============================================================================
-- 6. BookingGroups (Split-Buchungen)
-- =============================================================================

INSERT INTO BookingGroups (Description, CreatedDate, TotalAmount) VALUES
('Rechnungszahlung Schmidt mit Skonto', '2025-01-25', 5000.00),
('Wareneinkauf Fischer mit Versandkosten', '2025-02-08', 1500.00),
('Gehaltszahlung Januar 2025', '2025-01-31', 0.00);

-- =============================================================================
-- 7. Bookings (Buchungstransaktionen)
-- =============================================================================

-- Achtung: Account_ID, Customer_ID, COA_ID, Category_ID müssen auf existierende IDs verweisen
-- Kasse hat normalerweise ID=1, Geschäftskonto ID=2, Privatkonto ID=3
-- Kontakte beginnen bei ID=1 (eigene Daten), Kunden ab ID=2, Lieferanten ab ID=6

-- Einnahmen - Rechnungszahlungen von Kunden
INSERT INTO Bookings (DateBooking, DateTax, BookingGroup_ID, Account_ID, ForeignBankAccount, RecipientClient, Contact_ID, COA_ID, Category_ID, Amount, Currency, TaxRate, TaxAmount, Text, DocumentNumber) VALUES
('2025-01-25', '2025-01-15', 1, 2, 'DE89370400440532013001', 'Schmidt & Partner GmbH', 2, 14, 5, 4750.00, 'EUR', 0.19, 761.34, 'Zahlung Rechnung 2025-001 mit 5% Skonto', '2025-001'),
('2025-02-01', '2025-01-20', NULL, 2, 'DE12345678901234567890', 'Weber IT Solutions', 3, 14, 6, 8925.00, 'EUR', 0.19, 1425.00, 'Zahlung Rechnung 2025-002 - IT-Beratung', '2025-002'),
('2026-01-15', '2026-01-05', NULL, 2, 'DE98765432109876543210', 'Müller Bau AG', 5, 14, 5, 15470.00, 'EUR', 0.19, 2470.00, 'Zahlung Rechnung 2026-001 - Bauprojekt', '2026-001');

-- Ausgaben - Wareneinkauf
INSERT INTO Bookings (DateBooking, DateTax, BookingGroup_ID, Account_ID, ForeignBankAccount, RecipientClient, Contact_ID, COA_ID, Category_ID, Amount, Currency, TaxRate, TaxAmount, Text, DocumentNumber) VALUES
('2025-02-08', '2025-02-05', 2, 2, 'DE11111111111111111111', 'Fischer Großhandel GmbH', 6, 8, 8, -1428.00, 'EUR', 0.19, -228.00, 'Büromaterial Sammelbestellung', '2025-003'),
('2025-02-08', '2025-02-05', 2, 2, 'DE11111111111111111111', 'Fischer Großhandel GmbH', 6, 8, 8, -71.40, 'EUR', 0.19, -11.40, 'Versandkosten Büromaterial', '2025-003');

-- Ausgaben - Betriebskosten
INSERT INTO Bookings (DateBooking, DateTax, BookingGroup_ID, Account_ID, ForeignBankAccount, RecipientClient, Contact_ID, COA_ID, Category_ID, Amount, Currency, TaxRate, TaxAmount, Text, DocumentNumber) VALUES
('2025-02-10', '2025-02-10', NULL, 1, '', 'Tankstelle Aral', NULL, 11, 12, -89.50, 'EUR', 0.19, -14.29, 'Tanken Dienstwagen', '2025-004'),
('2025-03-05', '2025-03-01', NULL, 2, 'DE22222222222222222222', 'Allianz SE', 10, 11, 11, -487.50, 'EUR', 0.00, 0.00, 'Betriebshaftpflicht Q1/2025', '2025-005'),
('2026-01-25', '2026-01-20', NULL, 2, 'DE33333333333333333333', 'Bauer Software AG', 7, 11, 9, -1190.00, 'EUR', 0.19, -190.00, 'Softwarelizenz Jahresgebühr 2026', '2026-002');

-- Private Ausgaben (Status: draft - noch nicht gebucht)
INSERT INTO Bookings (DateBooking, DateTax, BookingGroup_ID, Account_ID, ForeignBankAccount, RecipientClient, Contact_ID, COA_ID, Category_ID, Amount, Currency, TaxRate, TaxAmount, Text, DocumentNumber) VALUES
('2026-02-01', '2026-02-01', NULL, 3, 'DE44444444444444444444', 'AOK Baden-Württemberg', 12, NULL, 14, -389.50, 'EUR', 0.00, 0.00, 'Krankenversicherung Januar 2026', '2026-003');

-- Bargeschäfte
INSERT INTO Bookings (DateBooking, DateTax, BookingGroup_ID, Account_ID, ForeignBankAccount, RecipientClient, Contact_ID, COA_ID, Category_ID, Amount, Currency, TaxRate, TaxAmount, Text, DocumentNumber) VALUES
('2025-03-10', '2025-03-10', NULL, 1, '', 'Bäckerei Schmidt', NULL, 11, 8, -12.50, 'EUR', 0.07, -0.82, 'Bewirtung Geschäftsmeeting', ''),
('2025-03-12', '2025-03-12', NULL, 1, '', 'Maria Schneider', 4, 14, 6, 150.00, 'EUR', 0.19, 23.95, 'Barzahlung Dienstleistung', '');

-- Stornierte Buchung (Beispiel)
INSERT INTO Bookings (DateBooking, DateTax, BookingGroup_ID, Account_ID, ForeignBankAccount, RecipientClient, Contact_ID, COA_ID, Category_ID, Amount, Currency, TaxRate, TaxAmount, Text, DocumentNumber) VALUES
('2025-02-15', '2025-02-15', NULL, 2, 'DE55555555555555555555', 'Fehlbuchung GmbH', NULL, NULL, NULL, -500.00, 'EUR', 0.19, -79.83, 'Versehentliche Doppelbuchung - STORNIERT', '');

-- Multi-Währung Beispiel
INSERT INTO Bookings (DateBooking, DateTax, BookingGroup_ID, Account_ID, ForeignBankAccount, RecipientClient, Contact_ID, COA_ID, Category_ID, Amount, Currency, TaxRate, TaxAmount, Text, DocumentNumber) VALUES
('2025-04-01', '2025-04-01', NULL, 4, 'US12345678901234567890', 'American Software Inc.', NULL, 11, 9, -1250.00, 'USD', 0.00, 0.00, 'Cloud Services April 2025', ''),
('2025-05-15', '2025-05-15', NULL, 3, 'GB98765432109876543210', 'British Consulting Ltd.', NULL, 14, 6, 2500.00, 'GBP', 0.20, 416.67, 'Consulting Project Payment', '');

-- =============================================================================
-- 8. BookingDocuments (Verknüpfungen Buchungen <-> Belege)
-- =============================================================================

-- Verknüpfe Buchungen mit passenden Dokumenten
-- Achtung: Booking_ID und Document_ID müssen existieren
INSERT INTO BookingDocuments (Booking_ID, Document_ID, RelationType) VALUES
(1, 1, 'invoice'),      -- Rechnung Schmidt mit Zahlung
(2, 2, 'invoice'),      -- Rechnung Weber mit Zahlung
(3, 7, 'invoice'),      -- Rechnung Müller mit Zahlung
(4, 3, 'receipt'),      -- Wareneinkauf Fischer
(5, 3, 'receipt'),      -- Versandkosten Fischer (gleicher Beleg)
(6, 4, 'receipt'),      -- Tankquittung
(7, 5, 'contract'),     -- Versicherung Allianz
(8, 8, 'invoice'),      -- Softwarelizenz Bauer
(9, 9, 'contract');     -- Krankenversicherung AOK

-- =============================================================================
-- ENDE DER TESTDATEN
-- =============================================================================

-- Hinweise:
-- 1. Diese SQL-Statements müssen in der richtigen Reihenfolge ausgeführt werden
-- 2. Foreign Key Constraints beachten (IDs müssen existieren)
-- 3. Bei Bedarf IDs in den Bookings und BookingDocuments anpassen
-- 4. Datum-Format: 'YYYY-MM-DD'
-- 5. Beträge: Positiv = Einnahme, Negativ = Ausgabe
-- 6. TaxRate als Dezimalzahl (0.19 = 19%, 0.07 = 7%)
-- 7. Status: 'draft', 'posted', 'cancelled'
-- 8. ContactType: 'customer', 'supplier', 'insurance', 'own', 'other'
