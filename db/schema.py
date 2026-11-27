# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Database-Mixin: schema."""
import datetime
import sqlite3
import os
import json
from decimal import Decimal
from money import to_minor, from_minor
from tax_groups import MIXED_TAX_RATES, pct_text
from .accounts import DEFAULT_CASH_ACCOUNT
from .matching import read_entry_form


class SchemaTooNewError(RuntimeError):
    """Die Datenbank stammt von einer neueren Programmversion.

    Sie wird nicht geöffnet und nicht verändert – ein älteres Programm würde sonst
    seine eigene (niedrigere) Schema-Zahl hineinschreiben, und beim nächsten Start
    der neueren Version liefen deren Migrationen erneut.
    """


class MigrationBackupError(RuntimeError):
    """Die Sicherung vor einer Migration ist gescheitert.

    Dann wird die Datenbank nicht geöffnet und nicht verändert: Ohne Sicherung gäbe es nach
    der Umstellung kein Zurück auf die vorherige Version.
    """


#: Ab dieser Schema-Zahl (Version 2.1.0) trägt jeder Buchungssatz seine Form
#: (``Bookings.EntryForm``). Gesichert wird vor jedem Migrationslauf (``initialize_database``).
ENTRY_FORM_SCHEMA = 2001

#: Ab dieser Schema-Zahl (Version 2.2.0) führt eine Rechnung den Versandzeitpunkt und den Zähler ihrer
#: Fassung (``Invoices.SentAt``, ``Invoices.DocumentVersion``), dazu die Spalten für den Stornobeleg
#: (``InvoiceKind``, ``CancelsInvoiceId``, ``ReplacesInvoiceId``); Fassungen und Protokoll haben eigene
#: Tabellen (``InvoiceDocumentVersions``, ``InvoiceCorrections``).
INVOICE_VERSIONS_SCHEMA = 2002

#: Die Spalten, die Schema 2002 an ``Invoices`` anhängt (in dieser Reihenfolge; Index 50 bis 54)
_INVOICE_VERSION_COLUMNS = (
    ('InvoiceKind', 'TEXT'),
    ('CancelsInvoiceId', 'INTEGER'),
    ('ReplacesInvoiceId', 'INTEGER'),
    ('SentAt', 'DATETIME'),
    ('DocumentVersion', 'INTEGER'),
)

#: Die Spalten, die Schema 2010 an ``Invoices`` anhängt (in dieser Reihenfolge; Index 56 bis 58)
_DETAILS_INVOICE_COLUMNS = (
    ('ServiceDateEnd', 'DATE'),
    ('PaymentMeans', 'TEXT'),
    ('QuoteStatusBefore', 'TEXT'),
)

#: Ab dieser Schema-Zahl (Version 2.3.0) trägt ein Buchungssatz den BU-Schlüssel, den seine Quelle mitgebracht
#: hat (``Bookings.DatevKey``, NULL = keiner, der Export leitet ihn ab), und die eigene Firma Berater- und
#: Mandantennummer für den Vorlauf des DATEV-Stapels (``CompanyDetails.DatevConsultantNo``/``DatevClientNo``).
#: ``TaxKeys`` ist berichtigt und ergänzt (``TAX_KEY_CORRECTIONS``).
DATEV_KEY_SCHEMA = 2003

#: Die Spalten, die Schema 2003 an ``CompanyDetails`` anhängt (in dieser Reihenfolge)
_DATEV_NUMBER_COLUMNS = ('DatevConsultantNo', 'DatevClientNo')

#: Die Zeilen von ``TaxKeys``, die Schema 2003 berichtigt oder ergänzt (Code, Bezeichnung, Satz, Art) – dieselben wie
#: in ``seed_data/tax_keys.json``, das eine frische Datenbank anlegt. Der 121 war als „steuerfrei“ geführt, ist aber die
#: Umsatzsteuer der Verwendung von Gegenständen; der 490 trägt keinen Satz.
TAX_KEY_CORRECTIONS = (
    ('100', 'Umsatzsteuer 0 %', 0.0, 'USt'),
    ('120', 'Verwendung von Gegenständen 0 %', 0.0, 'USt'),
    ('121', 'Verwendung von Gegenständen 19 %', 0.19, 'USt'),
    ('122', 'Verwendung von Gegenständen 7 %', 0.07, 'USt'),
    ('490', 'ohne Vorsteuerabzug', None, 'keine'),
)

#: Ab dieser Schema-Zahl (Version 2.4.0) trägt ein eigener Kontakt (Firma oder Person) die Besteuerungsart seiner
#: Umsatzsteuer (``Contacts.VatMethod``: ``'ist'`` oder ``'soll'``, NULL = nicht festgelegt). Die Angabe wird gespeichert und
#: angezeigt – keine Buchung liest sie; die Seite Umsatzsteuer-Voranmeldung zeigt Zahlen nur bei ``'ist'``.
VAT_METHOD_SCHEMA = 2004

#: Ab dieser Schema-Zahl (Version 2.5.0) trägt ein Bankkonto das Kennzeichen „reines Geschäftskonto“
#: (``Accounts.BusinessOnly``: 1 = reines Geschäftskonto, 0 = gemischt genutzt). Nur auf einem gemischt genutzten
#: Konto lassen sich beim Import eines Kontoauszugs einzelne Umsätze auswählen; auf einem reinen Geschäftskonto
#: wird der Auszug vollständig importiert. Das Kennzeichen ändert keine Buchung und keine Auswertung.
BUSINESS_ONLY_SCHEMA = 2005

#: Ab dieser Schema-Zahl (Version 2.6.0) gibt es die Lesung eines Belegs (``DocumentReadings``): die aus der Datei gelesenen
#: Felder, getrennt von dem, was der Nutzer am Beleg pflegt (``Documents``). Die Tabelle entsteht in
#: ``_ensure_document_readings_schema`` (in der Transaktion der Umstellung, ohne eigenen Schritt in ``_run_migrations``: es
#: ändert sich keine vorhandene Tabelle); der Bestand bekommt keine Zeilen – nichts wird umgedeutet.
DOCUMENT_READINGS_SCHEMA = 2006

#: Ab dieser Schema-Zahl (Version 2.7.0) trägt ein Beleg das Kennzeichen „Steuersatz je Position“
#: (``Invoices.MixedTaxRates``: NULL = ein Satz für den ganzen Beleg – der ganze Bestand –, 1 = Satz je Position). Der Satz im
#: Kopf (``Invoices.TaxRate``) ist dann nur ein Platzhalter: der höchste Satz der Positionen. Der Bestand behält überall NULL
#: und wird weiter allein nach ``Invoices.TaxRate`` gelesen – nichts wird umgedeutet.
MIXED_TAX_SCHEMA = 2007

#: Ab dieser Schema-Zahl (Version 2.8.0) gibt es die Steuersätze der Stammdaten (``TaxRates``: Bezeichnung, Satz in Hundertstel Prozent,
#: Art ``standard``/``exempt``, Befreiungsgrund, Erlöskonto) – vorbelegt beim Anlegen der Tabelle mit den Sätzen aus
#: ``tax_groups.MIXED_TAX_RATES`` – und eine Position kann die Momentaufnahme eines solchen Eintrags tragen (``InvoiceItems.TaxRateBp``,
#: ``TaxExemptReason``, ``RevenueCoaId``, dazu die schon vorhandene ``TaxCategory``: ``'E'`` = steuerfrei). Der Bestand behält überall NULL
#: (bzw. ``'S'``) – der Satz steht dort allein in ``InvoiceItems.TaxRate``, nichts wird umgedeutet. Die Tabelle entsteht in
#: ``_ensure_tax_rates_schema`` (in der Transaktion der Umstellung), die Spalten fügt ``_run_migrations`` an.
TAX_RATES_SCHEMA = 2008

#: Ab dieser Schema-Zahl (Version 2.9.0) kann eine Zahlungszuordnung ein Skonto tragen (``InvoicePayments.SkontoAmount``: Minor Units, NULL = keines).
#: Die Spalte steht nur an der Zuordnung, die die Rechnung mit Skonto schließt; ``Amount`` bleibt das Geld, das geflossen ist. Der Bestand behält überall
#: NULL – nichts wird umgedeutet. Die Spalte fügt ``_run_migrations`` am Ende der Tabelle an, wie das CREATE TABLE der frischen Datenbank sie führt.
SKONTO_SCHEMA = 2009

#: Ab dieser Schema-Zahl (Version 2.10.0) trägt eine Rechnung das Ende ihres Leistungszeitraums (``Invoices.ServiceDateEnd``, NULL = nur das Leistungsdatum), ihre Zahlungsart
#: (``PaymentMeans``, Code nach UNTDID 4461, NULL = Überweisung) und den Status ihres Angebots vor der Umwandlung (``QuoteStatusBefore``, NULL = ``accepted``); eine Bankbewegung kann
#: die Zusatzfelder ihres CAMT-Auszugs führen (``BankTransactionDetails``), ein Buchungssatz das Merkmal „stand in einem DATEV-Stapel“ (``BookingExports``), und ``Bookings.SourceGroup``
#: hat einen Index. ``Bookings`` bleibt bei seinen 24 Spalten. Der Bestand behält in den neuen Spalten überall NULL und bekommt keine Zeile in den neuen Tabellen – nichts wird
#: umgedeutet. Die Spalten fügt ``_run_migrations`` am Ende von ``Invoices`` an, wie das CREATE TABLE der frischen Datenbank sie führt; die Tabellen und der Index entstehen in
#: ``_ensure_details_schema`` (in der Transaktion der Umstellung).
DETAILS_SCHEMA = 2010


class SchemaMixin:
    def _column_exists(self, cursor, table, column):
        cols = [r[1] for r in cursor.execute(f"PRAGMA table_info({table})").fetchall()]
        return column in cols

    def _table_exists(self, cursor, table):
        return cursor.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table,)).fetchone() is not None

    def _add_column_if_missing(self, cursor, table, column, coldef):
        """ALTER TABLE … ADD COLUMN nur, wenn die Spalte fehlt (idempotent)."""
        if not self._column_exists(cursor, table, column):
            cursor.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coldef}")

    def _run_migrations(self, cursor, from_version):
        """Bestehende DBs schrittweise an SCHEMA_VERSION angleichen.

        ``from_version`` ist die Zahl aus ``PRAGMA user_version``: 0 bis 6 für
        Datenbanken aus Version 1.x, ab 2000 ``MAJOR × 1000 + SCHEMA_STEP``
        (siehe version.py). Neue Schritte prüfen gegen die neue Zahl, z. B.
        ``if from_version < 2001:``.

        Neue Tabellen deckt ``CREATE TABLE IF NOT EXISTS`` bereits ab; hier nur
        Spalten-Erweiterungen für DBs, die vor der jeweiligen Version angelegt
        wurden. Die Schritte sind defensiv (add-if-missing), damit sie auch bei
        gemischten Ständen sicher sind. FK-Constraints lassen sich per ALTER in
        SQLite nicht nachrüsten – migrierte Spalten bleiben einfache Spalten.
        """
        if from_version < 2:
            # v2: Fahrtenbuch um Tachostände und Beleg-Bezug erweitert
            self._add_column_if_missing(cursor, 'Trips', 'StartKm', 'INTEGER')
            self._add_column_if_missing(cursor, 'Trips', 'EndKm', 'INTEGER')
            self._add_column_if_missing(cursor, 'Trips', 'DocumentID', 'INTEGER')
        if from_version < 3:
            # v3: Buchungssätze, die uBuchhaltung selbst als Spiegel einer
            # Bank-Bewegung angelegt hat, werden markiert. Nur sie werden beim
            # Bearbeiten der Bank-Zeile mitgezogen – eigenständig erfasste,
            # bloß verknüpfte Buchungen behalten ihre eigene Kontierung.
            self._add_column_if_missing(cursor, 'Bookings', 'AutoMirror',
                                        'INTEGER DEFAULT 0')
        if from_version < 5:
            # v5: Buchungsgruppen abgelöst. Die Klammer eines mehrzeiligen
            # Belegs ist jetzt die Beleg-Nr. (+ Datum), die Verbindung zur
            # Bankbewegung läuft über ParentBooking_ID.
            #
            # Die Spalte BookingGroup_ID bleibt bestehen (die Anwendung liest
            # Bookings positionsbasiert per SELECT *) und wird geleert. Die
            # Tabelle wird NICHT gelöscht, sondern nur geleert: In vor v5
            # angelegten Datenbanken verweist der Fremdschlüssel der
            # Bookings-Tabelle weiterhin auf sie, und SQLite lehnt jedes
            # INSERT mit "no such table" ab, sobald das Ziel fehlt – ein
            # Entfernen erforderte einen kompletten Tabellen-Neuaufbau.
            # Neu angelegte Datenbanken bekommen weder Tabelle noch
            # Fremdschlüssel.
            cursor.execute('UPDATE Bookings SET BookingGroup_ID = NULL '
                           'WHERE BookingGroup_ID IS NOT NULL')
            if self._table_exists(cursor, 'BookingGroups'):
                cursor.execute('DELETE FROM BookingGroups')
        if from_version < 6:
            # v6: Klammer aus dem Quellsystem. Der Import aus der
            # WISO-Datenbank kennt die Zusammengehörigkeit von Teilbuchungen
            # (ACCOUNTINGID) und muss sie nicht mehr aus Beleg-Nr. und Datum
            # ableiten. Altbestand bleibt NULL und wird weiter über die
            # Beleg-Nr. gruppiert.
            self._add_column_if_missing(cursor, 'Bookings', 'SourceGroup', 'TEXT')
        if from_version < 4:
            # v4: Bestehende Spiegel nachträglich markieren, damit auch vor
            # der Einführung von AutoMirror kontierte Bank-Buchungen weiter
            # bearbeitbar bleiben. Bewusst sehr streng – markiert wird nur ein
            # EINZIGES Kind, das in allen kopierten Feldern exakt der
            # Bank-Zeile entspricht und dessen Gegenkonto das SKR-Konto der
            # Bank ist. Eigenständig erfasste Buchungen (WISO/Handerfassung)
            # weichen mindestens in Empfänger, Text oder Steuerdatum ab.
            self._mark_existing_auto_mirrors(cursor)
        if from_version < ENTRY_FORM_SCHEMA:
            # 2.1.0: Die Form eines Buchungssatzes (Zweckkonto/Geldkonto vorne, Umbuchung,
            # Spiegel) steht als Daten am Satz, statt bei jedem Lesen aus der Kontenliste von
            # heute geraten zu werden. Der Bestand bekommt die Form, mit der 2.0.x ihn las
            # (_fill_entry_forms) – danach liest jede Auswertung jede Zeile wie vorher.
            self._add_column_if_missing(cursor, 'Bookings', 'EntryForm', 'TEXT')
            self._fill_entry_forms(cursor)
        if from_version < INVOICE_VERSIONS_SCHEMA:
            # 2.2.0: Fassungen und Protokoll einer Rechnung. Die Spalten kommen in der Reihenfolge ans Ende,
            # in der sie auch das CREATE TABLE der frischen Datenbank führt; alle Werte des Bestands
            # bleiben NULL (``DocumentVersion`` NULL = Fassung 1). Die Tabellen und der Index entstehen
            # in ``_ensure_invoice_version_schema``.
            for column, definition in _INVOICE_VERSION_COLUMNS:
                self._add_column_if_missing(cursor, 'Invoices', column, definition)
        if from_version < DATEV_KEY_SCHEMA:
            # 2.3.0: Der BU-Schlüssel, den die Quelle einer Buchung mitgebracht hat, steht als Daten am Satz;
            # Berater- und Mandantennummer stehen bei der eigenen Firma. Die Spalten kommen in der Reihenfolge ans
            # Ende, in der sie auch das CREATE TABLE der frischen Datenbank führt; der Bestand behält überall NULL
            # (NULL = kein Schlüssel mitgebracht: Der Export leitet ihn ab) – nichts wird umgedeutet.
            self._add_column_if_missing(cursor, 'Bookings', 'DatevKey', 'INTEGER')
            for column in _DATEV_NUMBER_COLUMNS:
                self._add_column_if_missing(cursor, 'CompanyDetails', column, 'TEXT')
            self._correct_tax_keys(cursor)
        if from_version < VAT_METHOD_SCHEMA:
            # 2.4.0: Besteuerungsart der eigenen Firma (oder Person). Die Spalte kommt ans Ende, wie im CREATE TABLE
            # der frischen Datenbank; der Bestand behält NULL (= nicht festgelegt) – nichts wird umgedeutet.
            self._add_column_if_missing(cursor, 'Contacts', 'VatMethod', 'TEXT')
        if from_version < BUSINESS_ONLY_SCHEMA:
            # 2.5.0: Kennzeichen „reines Geschäftskonto“ am Bankkonto. Die Spalte kommt ans Ende, wie im CREATE TABLE der
            # frischen Datenbank; der Bestand bekommt überall 1 (= reines Geschäftskonto: die Importe laufen wie bisher) –
            # nichts wird umgedeutet.
            self._add_column_if_missing(cursor, 'Accounts', 'BusinessOnly', 'INTEGER NOT NULL DEFAULT 1')
        if from_version < MIXED_TAX_SCHEMA:
            # 2.7.0: Kennzeichen „Steuersatz je Position“ am Beleg. Die Spalte kommt ans Ende, wie im CREATE TABLE der frischen
            # Datenbank; der Bestand behält überall NULL (= ein Satz für den ganzen Beleg, ``Invoices.TaxRate`` gilt wie bisher) –
            # nichts wird umgedeutet.
            self._add_column_if_missing(cursor, 'Invoices', 'MixedTaxRates', 'INTEGER')
        if from_version < TAX_RATES_SCHEMA:
            # 2.8.0: Momentaufnahme eines Steuersatzes aus den Stammdaten an der Position. Die Spalten kommen in der Reihenfolge ans
            # Ende (Index 11 bis 13), in der sie auch das CREATE TABLE der frischen Datenbank führt; der Bestand behält überall NULL
            # (= keine Momentaufnahme: der Satz steht allein in ``TaxRate``) – nichts wird umgedeutet. Die Tabelle ``TaxRates`` und der
            # Index entstehen in ``_ensure_tax_rates_schema``.
            self._add_column_if_missing(cursor, 'InvoiceItems', 'TaxRateBp', 'INTEGER')
            self._add_column_if_missing(cursor, 'InvoiceItems', 'TaxExemptReason', 'TEXT')
            self._add_column_if_missing(cursor, 'InvoiceItems', 'RevenueCoaId', 'INTEGER')
        if from_version < SKONTO_SCHEMA:
            # 2.9.0: Skonto an der Zahlungszuordnung. Die Spalte kommt ans Ende, wie im CREATE TABLE der frischen Datenbank; der Bestand behält überall
            # NULL (= kein Skonto: ``Amount`` ist die ganze Zahlung) – nichts wird umgedeutet.
            self._add_column_if_missing(cursor, 'InvoicePayments', 'SkontoAmount', 'INTEGER')
        if from_version < DETAILS_SCHEMA:
            # 2.10.0: Ende des Leistungszeitraums, Zahlungsart und Status des Angebots vor der Umwandlung. Die Spalten kommen in der Reihenfolge ans Ende (Index 56 bis 58), in der
            # sie auch das CREATE TABLE der frischen Datenbank führt; der Bestand behält überall NULL (kein Zeitraum, Überweisung, Angebot ``accepted``) – nichts wird umgedeutet. Die
            # Tabellen und der Index entstehen in ``_ensure_details_schema``.
            for column, definition in _DETAILS_INVOICE_COLUMNS:
                self._add_column_if_missing(cursor, 'Invoices', column, definition)

    def _correct_tax_keys(self, cursor):
        """Die Zeilen von ``TAX_KEY_CORRECTIONS`` in ``TaxKeys`` berichtigen (eine abweichende Zeile wird überschrieben,
        eine fehlende angelegt); eine schon richtige Zeile und alles andere bleibt unberührt (auch ihre Reihenfolge in
        der Datei: kein ``INSERT OR REPLACE``, das die Zeile neu schriebe). Eine leere Tabelle bleibt leer:
        ``_seed_tax_keys`` legt sie dann vollständig an – mit denselben Zeilen aus ``seed_data/tax_keys.json`` –, was
        bei fünf vorab geschriebenen Zeilen unterbliebe."""
        if cursor.execute('SELECT 1 FROM TaxKeys LIMIT 1').fetchone() is None:
            return
        for code, description, rate, tax_type in TAX_KEY_CORRECTIONS:
            row = cursor.execute('SELECT Description, TaxRate, TaxType FROM TaxKeys WHERE Code = ?',
                                 (code,)).fetchone()
            if row is None:
                cursor.execute('INSERT INTO TaxKeys (Code, Description, TaxRate, TaxType) VALUES (?, ?, ?, ?)',
                               (code, description, rate, tax_type))
            elif tuple(row) != (description, rate, tax_type):
                cursor.execute('UPDATE TaxKeys SET Description = ?, TaxRate = ?, TaxType = ? WHERE Code = ?',
                               (description, rate, tax_type, code))

    def _fill_entry_forms(self, cursor):
        """Jedem Buchungssatz (``BookingType='entry'``) ohne Form die Form geben, die
        ``classify_entry`` mit den Zahlungskonten von 2.0.x (``_get_legacy_payment_coa_ids``,
        einschließlich des Bereichs 1000–1099) für ihn ergibt. Ein Spiegel bekommt
        ``mirror_legacy``: Welche Konvention sein Betrag hat, weiß nur der, der ihn geschrieben
        hat. Bankzeilen bleiben ohne Form. Zeilen, die schon eine Form tragen, bleiben unberührt –
        der Schritt ist wiederholbar.

        Eingeordnet wird je Kontopaar einmal (in Python), geschrieben wird gebündelt über die IDs:
        Ein Bestand hat viele Sätze, aber nur wenige verschiedene Paare."""
        money_ids = self._get_legacy_payment_coa_ids(cursor)
        form_of_pair = {}
        ids_by_form = {}
        rows = cursor.execute(
            "SELECT ID, COA_ID, CounterCOA_ID FROM Bookings "
            "WHERE BookingType = 'entry' AND EntryForm IS NULL").fetchall()
        for booking_id, coa_id, counter_coa_id in rows:
            pair = (coa_id, counter_coa_id)
            if pair not in form_of_pair:
                form_of_pair[pair] = read_entry_form(None, coa_id, counter_coa_id, money_ids)
            ids_by_form.setdefault(form_of_pair[pair], []).append(booking_id)
        for form, ids in ids_by_form.items():
            for start in range(0, len(ids), 500):
                chunk = ids[start:start + 500]
                cursor.execute(
                    f"UPDATE Bookings SET EntryForm = ? WHERE ID IN ({','.join('?' * len(chunk))})",
                    [form, *chunk])

    def _backup_before_migration(self, label):
        """Kopie der Datenbank nach ``<Ordner der Datenbank>/backup/JJJJMMTT_vor_<label>_<Datei>``
        (``label``: die Programmversion, die die Datenbank umstellt), gezogen über die
        Backup-API von SQLite (konsistent, auch bei offenen Verbindungen) – bevor die Migration
        irgendetwas ändert. ``backup/`` wird von der Datensicherung nie erfasst oder gelöscht.

        Die Kopie entsteht unter ``<Name>.part`` und bekommt ihren Namen erst, wenn sie vollständig
        und geprüft ist (``os.replace``): eine abgebrochene Sicherung hinterlässt nie eine Datei, die
        wie eine gültige aussieht. Ein vorhandener Name wird nie überschrieben (laufende Nummer).
        Scheitert die Kopie, ``MigrationBackupError``; ein Rest wird entfernt. Returns den Pfad."""
        source_path = os.path.abspath(self.db_name)
        directory = os.path.join(os.path.dirname(source_path), 'backup')
        stamp = datetime.date.today().strftime('%Y%m%d')
        name = os.path.basename(source_path)
        target = os.path.join(directory, f'{stamp}_vor_{label}_{name}')
        part = target + '.part'
        try:
            os.makedirs(directory, exist_ok=True)
            number = 1
            while os.path.exists(target):
                number += 1
                target = os.path.join(directory, f'{stamp}_vor_{label}_{number}_{name}')
            part = target + '.part'
            if os.path.exists(part):
                os.remove(part)                       # Rest eines abgebrochenen Versuchs
            source = sqlite3.connect(source_path)
            try:
                destination = sqlite3.connect(part)
                try:
                    source.backup(destination)
                    if (destination.execute('PRAGMA user_version').fetchone()[0]
                            != source.execute('PRAGMA user_version').fetchone()[0]):
                        raise sqlite3.DatabaseError('Die Kopie trägt einen anderen Stand als die Datenbank')
                finally:
                    destination.close()
            finally:
                source.close()
            os.replace(part, target)
        except Exception as exc:
            try:
                os.remove(part)
            except OSError:
                pass
            raise MigrationBackupError(
                f"Die Datenbank '{self.db_name}' muss auf Version {label} umgestellt werden. Vorher "
                f"legt das Programm eine Sicherung an, und die ist gescheitert ({exc}). Die "
                "Datenbank wurde nicht geöffnet und nicht verändert – bitte Schreibrechte und "
                "freien Speicherplatz im Ordner der Datenbank prüfen.") from exc
        return target

    def _mark_existing_auto_mirrors(self, cursor):
        """Altbestand: von uBuchhaltung erzeugte Buchungssätze markieren."""
        cursor.execute('''
            UPDATE Bookings SET AutoMirror = 1
            WHERE ID IN (
                SELECT c.ID
                FROM Bookings c
                JOIN Bookings p  ON p.ID = c.ParentBooking_ID
                JOIN Accounts a  ON a.ID = p.Account_ID
                JOIN ChartOfAccounts coa ON coa.AccountNumber = a.SKRAccount
                WHERE p.BookingType = 'bank'
                  AND c.COA_ID IS NOT NULL
                  AND c.CounterCOA_ID = coa.ID
                  AND c.COA_ID <> c.CounterCOA_ID
                  AND c.BookingGroup_ID IS NULL
                  AND c.Amount = p.Amount
                  AND c.DateBooking = p.DateBooking
                  AND COALESCE(c.DateTax, '')          = COALESCE(p.DateTax, '')
                  AND COALESCE(c.RecipientClient, '')  = COALESCE(p.RecipientClient, '')
                  AND COALESCE(c.Text, '')             = COALESCE(p.Text, '')
                  AND (SELECT COUNT(*) FROM Bookings x
                       WHERE x.ParentBooking_ID = p.ID) = 1
            )
        ''')

    def initialize_database(self):
        conn = self._get_connection()
        cursor = conn.cursor()

        # Vor dem (idempotenten) Anlegen festhalten, ob es sich um eine bereits
        # bestehende DB handelt und welche Schema-Version sie trägt. Wird unten in
        # _create_extended_schema für die Migration ausgewertet (eigene Connection).
        self._migrate_from_version = cursor.execute("PRAGMA user_version").fetchone()[0]
        from version import APP_VERSION, SCHEMA_VERSION, describe_schema_version
        if self._migrate_from_version > SCHEMA_VERSION:
            conn.close()
            raise SchemaTooNewError(
                f"Die Datenbank '{self.db_name}' hat den Stand "
                f"{describe_schema_version(self._migrate_from_version)}, dieses Programm kennt nur "
                f"{describe_schema_version(SCHEMA_VERSION)}. Sie wurde nicht geöffnet und nicht "
                "verändert – bitte eine neuere Programmversion verwenden.")
        self._migrate_had_tables = cursor.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='Invoices'"
        ).fetchone() is not None
        if self._migrate_had_tables and self._migrate_from_version < SCHEMA_VERSION:
            # Vor jedem Migrationslauf einer bestehenden Datenbank: ohne Sicherung kein Weg zurück
            # auf die vorherige Version (die Zahl in der Datenbank wird nie zurückgestempelt, die
            # ältere Version öffnet die umgestellte Datenbank nicht)
            try:
                self._backup_before_migration(APP_VERSION)
            except MigrationBackupError:
                conn.close()
                raise

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
                -- Reines Geschäftskonto (2.5.0, Schema 2005; Reihenfolge wie das ALTER TABLE der Migration): 1 = reines
                -- Geschäftskonto (Kontoauszüge werden vollständig importiert), 0 = gemischt genutzt (beim Import lassen
                -- sich einzelne Umsätze auswählen)
                BusinessOnly INTEGER NOT NULL DEFAULT 1,
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
                -- Besteuerungsart der Umsatzsteuer der eigenen Firma/Person (2.4.0, Schema 2004; Reihenfolge wie das
                -- ALTER TABLE der Migration): 'ist' oder 'soll'; NULL = nicht festgelegt
                VatMethod      TEXT,
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
                -- DATEV-Vorlauf der eigenen Firma (Reihenfolge wie das ALTER TABLE der Migration):
                -- Beraternummer 1001–9999999, Mandantennummer 1–99999, ohne führende Null; NULL = nicht eingetragen
                DatevConsultantNo TEXT,
                DatevClientNo     TEXT,
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

        # Trips: Fahrtenbuch (Zeiten-Bereich). Startpunkt leer ⇒ eigene Adresse.
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS Trips (
                ID          INTEGER PRIMARY KEY AUTOINCREMENT,
                DriverID    INTEGER NOT NULL,            -- FK Contacts(ID) – Fahrer
                StartDate   DATE    NOT NULL,
                StartTime   TEXT,                        -- optional 'HH:MM'
                EndDate     DATE,                        -- optional
                EndTime     TEXT,                        -- optional 'HH:MM'
                StartPoint  TEXT,                        -- leer ⇒ eigene Adresse/Firmensitz
                Destination TEXT    NOT NULL,            -- Ziel
                Vehicle     TEXT,                        -- Freitext
                Reason      TEXT,                        -- Grund
                DistanceKm  INTEGER,                     -- gefahrene km (manuell/berechnet)
                StartKm     INTEGER,                     -- Tachostand Start (optional)
                EndKm       INTEGER,                     -- Tachostand Ende (optional)
                DocumentID  INTEGER,                     -- optional verknüpfter Beleg
                CreatedAt   DATETIME DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (DriverID)   REFERENCES Contacts(ID)  ON DELETE CASCADE,
                FOREIGN KEY (DocumentID) REFERENCES Documents(ID) ON DELETE SET NULL
            )
        ''')
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_trips_driver_date
            ON Trips(DriverID, StartDate)
        ''')

        # Bookings (replaces Zahlung with enhanced structure)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS Bookings (
                ID INTEGER PRIMARY KEY AUTOINCREMENT,
                DateBooking DATE NOT NULL,
                DateTax DATE,
                BookingGroup_ID INTEGER,   -- stillgelegt (v5): Splits laufen über ParentBooking_ID
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
                AutoMirror INTEGER DEFAULT 0,
                -- Klammer aus dem Quellsystem (v6): WISO liefert mit
                -- ACCOUNTINGID die Zusammengehörigkeit von Teilbuchungen
                -- ausdrücklich mit. Wo sie gesetzt ist, muss sie nicht über
                -- Beleg-Nr. + Datum geraten werden.
                SourceGroup TEXT,
                -- Form des Buchungssatzes (2.1.0): purpose_first | liquid_first |
                -- transfer | mirror_coa | mirror_counter | mirror_legacy, siehe
                -- db/matching.py. Bankzeilen haben keine (NULL). ANS ENDE anhängen:
                -- die Leser lesen Bookings positionsbasiert (SELECT *).
                EntryForm TEXT,
                -- BU-Schlüssel, den die Quelle der Buchung mitgebracht hat (2.3.0, Index 23): eine ganze Zahl
                -- von 1 bis 9999. NULL = keiner (0 wird nie gespeichert), der DATEV-Export leitet ihn ab; er
                -- fällt (NULL), wenn sich Konto, Gegenkonto oder Steuersatz ändern.
                DatevKey INTEGER,
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
        # Type: 'invoice' (Ausgangsrechnungen), 'quote' (Angebote), 'receipt_company' (Belege Firma), 'receipt_category' (Belege Kategorien)
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

                -- Fassungen, Versand, Stornobeleg (Schema 2002, Index 50-54; Reihenfolge wie die
                -- ALTER TABLE der Migration in _run_migrations)
                InvoiceKind TEXT,                              -- NULL = Rechnung, 'storno' = Stornorechnung (seit 2.2.1)
                CancelsInvoiceId INTEGER,                      -- Stornorechnung: die stornierte Rechnung (seit 2.2.1)
                ReplacesInvoiceId INTEGER,                     -- neue Rechnung: die ersetzte, stornierte (seit 2.2.2)
                SentAt DATETIME,                               -- erstmals versendet (UTC); Import und Altbestand: NULL
                DocumentVersion INTEGER,                       -- Nummer der aktuellen Fassung; NULL = Fassung 1

                -- Mehrere Steuersätze je Beleg (Schema 2007, Index 55; Reihenfolge wie die ALTER TABLE der Migration)
                MixedTaxRates INTEGER,                         -- NULL = ein Satz für den ganzen Beleg (Bestand), 1 = Satz je Position (seit 2.7.0)

                -- Bündel 2.10.0 (Schema 2010, Index 56-58; Reihenfolge wie die ALTER TABLE der Migration)
                ServiceDateEnd DATE,                           -- Ende des Leistungszeitraums; NULL = nur das Leistungsdatum (Bestand)
                PaymentMeans TEXT,                             -- Zahlungsart, Code nach UNTDID 4461 ('10', '48', '1'); NULL = Überweisung (58, Bestand)
                QuoteStatusBefore TEXT,                        -- Rechnung aus einem Angebot: dessen Status vor der Umwandlung; NULL = 'accepted' (Bestand)

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

                -- Momentaufnahme eines Steuersatzes aus den Stammdaten (Schema 2008, Index 11-13; Reihenfolge wie die ALTER TABLE
                -- der Migration): NULL = keine Momentaufnahme, der Satz steht allein in TaxRate (der ganze Bestand); TaxCategory
                -- bleibt wie bisher ('S') und heißt mit der Momentaufnahme eines steuerfreien Eintrags 'E'
                TaxRateBp INTEGER,                             -- Satz in Hundertstel Prozent (1900 = 19 %, 550 = 5,5 %)
                TaxExemptReason TEXT,                          -- Grund der Steuerbefreiung (nur bei TaxCategory 'E')
                RevenueCoaId INTEGER,                          -- Erlöskonto des Eintrags; ohne Fremdschlüssel (frisch = migriert), das Löschen eines Kontos prüft coa_is_referenced

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

        Eine frische Datenbank bekommt hier alle Spalten in ihrer
        CREATE-TABLE-Definition; ein bestehender Bestand bekommt neue Spalten
        über ``_run_migrations`` (gleiche Spaltenreihenfolge wie hier).
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
                -- Skonto (2.9.0, Schema 2009; Reihenfolge wie das ALTER TABLE der Migration): der Rest der Rechnung, den diese Zahlung als
                -- Skonto ausbucht, in Minor Units; NULL = keines. Nur an der Zuordnung, die die Rechnung schließt; Amount bleibt das Geld
                SkontoAmount INTEGER,
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

        # Bestehende, ältere DBs migrieren (neue Spalten nachziehen). Frische DBs
        # sind durch die CREATE-Statements oben bereits aktuell.
        from version import SCHEMA_VERSION
        migrating = (getattr(self, '_migrate_had_tables', False)
                     and self._migrate_from_version < SCHEMA_VERSION)
        if migrating:
            # Die Umstellung läuft in EINER Transaktion – ALTER TABLE eingeschlossen: Bricht sie ab,
            # ist die Datenbank danach so wie vorher (keine leere Spalte, keine halbe Befüllung),
            # und der nächste Start versucht es noch einmal.
            if conn.in_transaction:
                conn.commit()
            cursor.execute('BEGIN')
        try:
            if migrating:
                self._run_migrations(cursor, self._migrate_from_version)

            self._ensure_invoice_version_schema(cursor)
            self._ensure_invoice_payment_index(cursor)
            self._ensure_document_readings_schema(cursor)
            self._ensure_tax_rates_schema(cursor)
            self._ensure_details_schema(cursor)

            # Schema-Version in der DB hinterlegen (MAJOR × 1000 + SCHEMA_STEP, siehe
            # version.py). Bei jeder Schema-Änderung SCHEMA_STEP dort erhöhen.
            cursor.execute(f"PRAGMA user_version = {int(SCHEMA_VERSION)}")

            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

        # Seed-Daten aus seed_data/ laden
        self._seed_chart_of_accounts()
        self._seed_asset_categories()
        self._seed_tax_keys()
    def _ensure_invoice_version_schema(self, cursor):
        """Die Tabellen für Protokoll und Fassungen einer Rechnung und der eindeutige Index auf dem
        Stornobeleg. Läuft bei der Umstellung in deren Transaktion (nach dem Anfügen der Spalten), bei einer
        frischen Datenbank gleich beim Anlegen.

        Beide Tabellen haben bewusst keinen Fremdschlüssel auf ``Invoices``: Protokoll und abgelegte
        Fassungen überleben das Löschen eines Entwurfs."""
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS InvoiceCorrections (
                ID INTEGER PRIMARY KEY AUTOINCREMENT,
                InvoiceID INTEGER NOT NULL,
                InvoiceNumber TEXT,                            -- Momentaufnahme: Nummer zur Zeit des Eintrags
                Version INTEGER NOT NULL,                      -- die Fassung, die durch den Eintrag entstand bzw. gilt
                ChangedAt DATETIME DEFAULT CURRENT_TIMESTAMP,  -- UTC
                UserName TEXT,
                Action TEXT NOT NULL,                          -- 'correct' (Berichtigung) | 'status' (Statuswechsel)
                Field TEXT,                                    -- Spaltenname bzw. 'Status'
                OldValue TEXT,
                NewValue TEXT
            )
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_invoicecorrections_invoice "
                       "ON InvoiceCorrections(InvoiceID)")
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS InvoiceDocumentVersions (
                ID INTEGER PRIMARY KEY AUTOINCREMENT,
                InvoiceID INTEGER NOT NULL,
                Version INTEGER NOT NULL,                      -- die abgelegte Fassung
                Kind TEXT NOT NULL,                            -- 'pdf' | 'xml'
                RelPath TEXT NOT NULL,                         -- relativ zum Ordner invoices/ des Benutzers
                Sha256 TEXT,
                ArchivedAt DATETIME DEFAULT CURRENT_TIMESTAMP, -- UTC
                Reason TEXT NOT NULL,                          -- 'corrected' | 'reopened'
                UNIQUE(InvoiceID, Version, Kind)
            )
        """)
        cursor.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_invoices_cancels "
                       "ON Invoices(CancelsInvoiceId) WHERE CancelsInvoiceId IS NOT NULL")

    def _ensure_invoice_payment_index(self, cursor):
        """Ein Paar (Rechnung, Bankbewegung) gibt es höchstens einmal in ``InvoicePayments``.

        Neue Datenbanken bekommen den Index gleich, bestehende beim Öffnen – aber nur, wenn
        sie keine Dubletten enthalten: Der Index ließe sich sonst nicht anlegen. Dann wird er
        übersprungen (kein Fehler, kein Abbruch); die Prüfliste „Zahlungszuordnungen“ auf
        der Seite *Daten* meldet die Dubletten, und beim nächsten Start ohne Dubletten entsteht
        er."""
        if cursor.execute(
                "SELECT 1 FROM InvoicePayments GROUP BY InvoiceID, BookingID "
                "HAVING COUNT(*) > 1 LIMIT 1").fetchone():
            print("Hinweis: Der eindeutige Index auf den Zahlungszuordnungen wurde nicht angelegt, "
                  "weil Zahlungen doppelt zugeordnet sind (Prüfliste „Zahlungszuordnungen“ auf "
                  "der Seite Daten).")
            return
        cursor.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS ux_invoicepayments_invoice_booking "
            "ON InvoicePayments(InvoiceID, BookingID)")

    def _ensure_document_readings_schema(self, cursor):
        """Die Lesung eines Belegs (2.6.0, Schema 2006): ``DocumentReadings`` mit einer Zeile je Beleg, dazu ein Index auf dem
        Bruttobetrag. Läuft bei der Umstellung in deren Transaktion (nach den Schritten davor), bei einer frischen Datenbank
        gleich beim Anlegen.

        Die Lesung sind abgeleitete Daten, getrennt von dem, was der Nutzer am Beleg pflegt: Sie hängt mit
        ``ON DELETE CASCADE`` am Beleg und geht mit ihm. Es gibt keine CHECK-Regeln – was ein Leser nicht erkennt, bleibt NULL."""
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS DocumentReadings (
                ID INTEGER PRIMARY KEY AUTOINCREMENT,
                Document_ID INTEGER NOT NULL UNIQUE REFERENCES Documents(ID) ON DELETE CASCADE,
                Status TEXT NOT NULL,
                Source TEXT,
                ReaderVersion INTEGER NOT NULL,                -- Stand des Lesers: eine ältere Lesung gilt als ungelesen
                ReadAt DATETIME NOT NULL,
                DocDate DATE,
                GrossMinor INTEGER,                            -- Beträge in den Minor Units der App (money.SCALE)
                NetMinor INTEGER,
                TaxMinor INTEGER,
                TaxRate REAL,                                  -- als Anteil wie in Bookings (0.19)
                Currency TEXT,
                SenderName TEXT,
                SenderVatId TEXT,
                InvoiceNumber TEXT,
                Kind TEXT,
                Raw TEXT,                                      -- JSON mit der Herkunft je Feld
                Text TEXT                                      -- der aus der Datei gelesene Text (gekappt)
            )
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_documentreadings_gross ON DocumentReadings(GrossMinor)")

    def _ensure_tax_rates_schema(self, cursor):
        """Die Steuersätze der Stammdaten (2.8.0, Schema 2008): ``TaxRates`` und ein Index auf den Positionen eines Belegs. Läuft bei der
        Umstellung in deren Transaktion (nach dem Anfügen der Spalten an ``InvoiceItems``), bei einer frischen Datenbank gleich beim Anlegen.

        **Vorbelegt wird genau einmal: beim Anlegen der Tabelle** (oder beim nächsten Versuch, wenn der erste nach dem Anlegen abbrach) – je Satz aus ``tax_groups.MIXED_TAX_RATES`` ein Eintrag der Art
        ``standard`` ohne Erlöskonto (19 % und 7 % haben ihr Standardkonto). Wer später alle Einträge löscht, bekommt beim nächsten Öffnen
        keinen zurück (anders als die Seeds der Kategorien und Steuerschlüssel, die eine leere Tabelle wieder füllen). Die Sätze nennt nur
        ``MIXED_TAX_RATES``; ``RateBp`` ist der Satz in Hundertstel Prozent (1900 = 19 %), ``Label`` der Text der Auswahl (``19 %``). Es gibt
        keine CHECK-Regeln im Schema (wie sonst im Repo): Was ein Eintrag tragen darf, prüft die Anwendung, nicht die Datenbank.

        Der Index auf ``InvoiceItems.InvoiceId`` (einen gab es bisher nicht) beschleunigt, was die Positionen eines Belegs liest und – über
        ``ON DELETE CASCADE`` – mit dem Beleg löscht; er ändert keine Daten."""
        created = not self._table_exists(cursor, 'TaxRates')
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS TaxRates (
                ID INTEGER PRIMARY KEY AUTOINCREMENT,
                Label TEXT NOT NULL,                           -- Bezeichnung in Auswahl und Liste
                RateBp INTEGER NOT NULL,                       -- Satz in Hundertstel Prozent: 1900 = 19 %, 550 = 5,5 %
                Kind TEXT NOT NULL DEFAULT 'standard',         -- 'standard' | 'exempt' (steuerfrei)
                ExemptReason TEXT,                             -- Grund der Steuerbefreiung (nur bei 'exempt')
                RevenueCoaId INTEGER REFERENCES ChartOfAccounts(ID),   -- Erlöskonto; NULL = das Standardkonto des Satzes (19 %, 7 %)
                VatCoaId INTEGER REFERENCES ChartOfAccounts(ID),       -- Umsatzsteuerkonto des Satzes (freiwillig; für Auswertungen)
                InputTaxCoaId INTEGER REFERENCES ChartOfAccounts(ID),  -- Vorsteuerkonto des Satzes (freiwillig; für Auswertungen)
                Active INTEGER NOT NULL DEFAULT 1,             -- 1 = wählbar, 0 = inaktiv
                SortOrder INTEGER NOT NULL DEFAULT 0,          -- Reihenfolge in der Auswahl (dann ID)
                CreatedAt DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # Bricht der Prozess nach dem CREATE und vor dem Abschluss ab, steht die Tabelle leer da, die Datenbank aber noch auf dem alten Stand:
        # Dann wird beim nächsten Versuch vorbelegt. Eine Datenbank im Stand 2008 mit geleerter Tabelle bleibt leer.
        unfinished = (not created and getattr(self, '_migrate_from_version', TAX_RATES_SCHEMA) < TAX_RATES_SCHEMA
                      and cursor.execute('SELECT 1 FROM TaxRates LIMIT 1').fetchone() is None)
        if created or unfinished:
            for number, rate in enumerate(MIXED_TAX_RATES, 1):
                cursor.execute("INSERT INTO TaxRates (Label, RateBp, Kind, Active, SortOrder) VALUES (?, ?, 'standard', 1, ?)",
                               (f'{pct_text(rate)} %', int(rate * 100), number * 10))
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_invoiceitems_invoice ON InvoiceItems(InvoiceId)")

    def _ensure_details_schema(self, cursor):
        """Die Zusatzfelder einer Bankbewegung, das Merkmal „exportiert“ und der Index auf ``Bookings.SourceGroup`` (2.10.0, Schema 2010). Läuft bei der Umstellung in deren
        Transaktion (nach dem Anfügen der Spalten, auch von ``SourceGroup`` bei einer Datenbank aus 1.x), bei einer frischen Datenbank gleich beim Anlegen.

        Beide Tabellen hängen mit ``ON DELETE CASCADE`` an ``Bookings`` (``PRAGMA foreign_keys = ON``, auch in der SQL-Konsole): Wird die Buchung gelöscht, bleibt nichts verwaist.
        ``Bookings`` selbst bekommt keine Spalte – die Leser lesen es positionsbasiert (``SELECT *``), und die neuen Angaben gibt es nur für wenige Zeilen (die CAMT-Bankzeilen,
        die exportierten Sätze). Der Bestand bekommt keine Zeile. Es gibt keine CHECK-Regeln: Was die Anwendung nicht lesen kann, liest sie als leer (``get_bank_details``).

        Der Index steht hier und nicht bei den Indizes auf ``Bookings`` in ``_create_extended_schema``, damit er bei der Umstellung in deren Transaktion entsteht."""
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS BankTransactionDetails (
                Booking_ID INTEGER PRIMARY KEY,                -- die Bankbewegung (Bookings.ID, BookingType 'bank')
                ValueDate DATE,                                -- Valuta (CAMT Ntry/ValDt), ISO
                EndToEndId TEXT,                               -- TxDtls/Refs/EndToEndId
                MandateRef TEXT,                               -- TxDtls/Refs/MndtId
                CreditorId TEXT,                               -- Gläubiger-ID (Cdtr …/PrvtId/Othr/Id mit SchmeNm/Prtry 'SEPA')
                BankRef TEXT,                                  -- Ntry/AcctSvcrRef
                FOREIGN KEY (Booking_ID) REFERENCES Bookings(ID) ON DELETE CASCADE
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS BookingExports (
                Booking_ID INTEGER PRIMARY KEY,                -- ein Buchungssatz, der in einem DATEV-Stapel stand
                ExportedAt DATETIME NOT NULL,                  -- erster Export (UTC, CURRENT_TIMESTAMP)
                FOREIGN KEY (Booking_ID) REFERENCES Bookings(ID) ON DELETE CASCADE
            )
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_bookings_sourcegroup ON Bookings(SourceGroup)")

    def ensure_kasse_exists(self):
        """Ensure the default 'Kasse' account exists and cannot be deleted.

        Eine neu angelegte Kasse bekommt das Kassenkonto 1600 (SKR04); fehlt es im
        Kontenrahmen, wird es nachgelegt. Eine vorhandene Kasse bleibt unverändert,
        mit oder ohne SKR-Nummer."""
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT COUNT(*) FROM Accounts WHERE IsCash = 1')
        count = cursor.fetchone()[0]
        conn.close()
        if count == 0:
            self.ensure_cash_coa()
            conn = self._get_connection()
            conn.execute('''
                INSERT INTO Accounts (Name, Owner, Number, BIC, BankName, IsCash, SKRAccount)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            ''', ("Kasse", "", "", "", "", 1, DEFAULT_CASH_ACCOUNT))
            conn.commit()
            conn.close()
