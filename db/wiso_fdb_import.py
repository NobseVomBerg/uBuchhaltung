# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Database-Mixin: Import aus der WISO-Mein-Büro-Datenbank.

Gegenstück zu :mod:`db.wiso_import` (CSV/Text), aber aus der Firebird-Datei
selbst. Der Unterschied ist nicht die Menge, sondern die Genauigkeit: die
Datenbank nennt mit ``ACCOUNTINGID`` ausdrücklich, welche Teilbuchungen
zusammengehören. Sie landet in ``Bookings.SourceGroup`` und ersetzt dort das
Raten über Beleg-Nr. und Datum.

Das Lesen der Datei erledigt :mod:`importers.wiso_fdb`; hier steht nur die
Abbildung auf ``Bookings``, ``Assets`` und ``AssetDepreciations``.
"""
import datetime

from db.core import coa_id
from money import to_minor

#: uBuchhaltung rechnet in SKR04.
SKR04 = 4


class WisoFdbImportMixin:
    """Buchungssätze und Anlagen aus einer ``.FDB``-Datei übernehmen."""

    def import_wiso_fdb(self, path, standard_chart_path=None,
                        with_assets=True, with_invoices=True) -> dict:
        """WISO-Mandantendatenbank importieren.

        Args:
            path: ``DB1.FDB`` des Mandanten.
            standard_chart_path: ``DB0.FDB`` – liefert Konten, die im
                Mandantenrahmen fehlen. Optional.
            with_assets: Anlagenverzeichnis samt AfA-Plan mitnehmen.
            with_invoices: Kunden und Ausgangsrechnungen samt Positionen
                mitnehmen. Die Kundennummern bleiben die aus WISO.

        Returns:
            dict mit ``imported``, ``skipped``, ``assets``, ``depreciations``,
            ``tax_rows_skipped``, ``memo_rows_skipped``, ``missing_coa`` und
            ``errors``.
        """
        from importers.wiso_fdb import WisoDatabase

        result = {'imported': 0, 'skipped': 0, 'assets': 0,
                  'depreciations': 0, 'tax_rows_skipped': 0,
                  'memo_rows_skipped': 0, 'missing_coa': {}, 'created_coa': {},
                  'unresolved_accounts': 0, 'blocker': [], 'hints': [],
                  'asset_warnings': [], 'customers': 0, 'invoices': 0,
                  'invoice_items': 0, 'invoices_skipped': 0,
                  'invoice_warnings': [], 'payments_linked': 0,
                  'payment_warnings': [], 'errors': []}
        try:
            wiso = WisoDatabase(path, standard_chart_path)
        except Exception as exc:                       # defekte/fremde Datei
            result['errors'].append(f'{path}: {exc}')
            return result

        with wiso:
            blocker, hints = wiso.check()
            result['hints'] = hints
            if blocker:
                result['blocker'] = blocker
                result['errors'] += blocker
                return result
            data = wiso.read(self._liquid_skr_accounts())
            result['tax_rows_skipped'] = data.tax_rows_skipped
            result['memo_rows_skipped'] = data.memo_rows_skipped
            result['missing_coa'] = {
                number: {'anzahl': count,
                         'bezeichnung': data.unmapped_labels.get(number, '')}
                for number, count in sorted(data.unmapped_accounts.items(),
                                            key=lambda kv: -kv[1])}
            result['created_coa'] = self._create_missing_coa(data)
            self._insert_wiso_bookings(data.bookings, result)
            if with_assets:
                self._insert_wiso_assets(data.assets, result)
            if with_invoices:
                contacts = self._insert_wiso_customers(data.customers, result)
                self._insert_wiso_invoices(data, contacts, result)
        return result

    def link_wiso_invoice_payments(self, path, standard_chart_path=None) -> dict:
        """Zahlungen den Rechnungen zuordnen – **nach** dem Auto-Abgleich.

        Eigener Schritt und nicht Teil von :meth:`import_wiso_fdb`, weil die
        Reihenfolge zählt: verknüpft werden soll die Bankbewegung, und die
        hängt erst nach ``link_bank_to_entries`` an den Buchungssätzen. Vorher
        aufgerufen, landet die Zahlung am Buchungssatz – nicht falsch, aber
        weniger nützlich, und ein zweiter Lauf korrigiert es nicht mehr.
        """
        from importers.wiso_fdb import WisoDatabase

        result = {'payments_linked': 0, 'payment_warnings': [], 'errors': []}
        try:
            wiso = WisoDatabase(path, standard_chart_path)
        except Exception as exc:
            result['errors'].append(f'{path}: {exc}')
            return result
        with wiso:
            self._link_wiso_invoice_payments(wiso.read(self._liquid_skr_accounts()),
                                             result)
        return result

    # ------------------------------------------------------------------
    def _liquid_skr_accounts(self):
        """SKR-Nummern der eigenen Zahlungskonten – sie bestimmen das Vorzeichen."""
        conn = self._get_connection()
        try:
            rows = conn.execute(
                'SELECT SKRAccount FROM Accounts WHERE SKRAccount IS NOT NULL'
            ).fetchall()
        finally:
            conn.close()
        return {row[0] for row in rows}

    def _coa_map(self, cursor):
        cursor.execute('SELECT AccountNumber, ID FROM ChartOfAccounts')
        return {row[0]: row[1] for row in cursor.fetchall()}

    def _create_missing_coa(self, data):
        """SKR04-Konten anlegen, die der eigene Kontenrahmen noch nicht kennt.

        Hier wird nichts geraten: Nummer **und** Bezeichnung stammen aus WISOs
        eigener Umschlüsselungstabelle. Ohne diesen Schritt verlören Buchungen
        auf Konten, die uBuchhaltung nicht mitbringt, ihre Kontierung und
        stünden in der Übersicht als offen.
        """
        wanted = {}
        for account in data.chart.values():
            if account.skr04:
                wanted.setdefault(account.skr04, account.text)
        used = {number for booking in data.bookings
                for number in (booking.account, booking.counter_account)
                if number is not None}
        used |= {number for asset in data.assets
                 for number in (asset.account, asset.depreciation_account)
                 if number is not None}

        conn = self._get_connection()
        cursor = conn.cursor()
        known = set(self._coa_map(cursor))
        created = {}
        for number in sorted(used - known):
            name = wanted.get(number) or f'SKR04 {number}'
            cursor.execute('''
                INSERT OR IGNORE INTO ChartOfAccounts
                    (ID, Framework, AccountNumber, Name, Description,
                     IsStandard, PrivateSharePercent, ShowInMenu)
                VALUES (?,?,?,?,?,0,0,1)
            ''', (coa_id(SKR04, number), SKR04, number, name[:120],
                  'aus der WISO-Datenbank übernommen'))
            created[number] = name
        conn.commit()
        conn.close()
        return created

    def _insert_wiso_bookings(self, bookings, result):
        """Buchungssätze schreiben; ``SourceGroup`` trägt die Split-Klammer.

        Duplikate erkennt die Quell-Id (``SourceGroup`` + Betrag + Konto reicht
        nicht, weil ein Vorgang gleichbetragige Zeilen enthalten darf): schon
        vorhandene ``SourceGroup``-Werte werden übersprungen, damit ein zweiter
        Lauf nichts verdoppelt.
        """
        conn = self._get_connection()
        cursor = conn.cursor()
        coa_map = self._coa_map(cursor)
        cursor.execute("SELECT DISTINCT SourceGroup FROM Bookings "
                       "WHERE SourceGroup IS NOT NULL AND SourceGroup <> ''")
        known_groups = {row[0] for row in cursor.fetchall()}

        for booking in bookings:
            if booking.group and booking.group in known_groups:
                result['skipped'] += 1
                continue
            coa = coa_map.get(booking.account)
            counter_coa = coa_map.get(booking.counter_account)
            if booking.account is not None and coa is None:
                result['unresolved_accounts'] += 1
            if booking.counter_account is not None and counter_coa is None:
                result['unresolved_accounts'] += 1
            cursor.execute('''
                INSERT INTO Bookings
                    (DateBooking, COA_ID, CounterCOA_ID, Amount, TaxRate,
                     TaxAmount, Text, DocumentNumber, BookingType, SourceGroup)
                VALUES (?,?,?,?,?,?,?,?,'entry',?)
            ''', (booking.date, coa, counter_coa,
                  to_minor(booking.amount or 0), booking.tax_rate,
                  self._minor_opt(booking.tax_amount), booking.text,
                  booking.document_number or None, booking.group or None))
            result['imported'] += 1

        conn.commit()
        conn.close()

    def _insert_wiso_customers(self, customers, result):
        """Kunden übernehmen; die Kundennummer bleibt die aus WISO.

        Returns die Zuordnung Kundennummer → Contacts.ID, die die Rechnungen
        brauchen. Vorhandene Kundennummern werden nicht angetastet – wer den
        Kontakt in uBuchhaltung schon gepflegt hat, soll ihn behalten.
        """
        conn = self._get_connection()
        try:
            known = {row[0]: row[1] for row in conn.execute(
                'SELECT CustomerNumber, ID FROM Contacts '
                'WHERE CustomerNumber IS NOT NULL')}
        finally:
            conn.close()

        for customer in customers:
            number = str(customer.number) if customer.number is not None else None
            if number is None or number in known:
                continue
            try:
                contact_id = self.insert_contact(
                    contact_type='customer', entity_type=customer.entity_type,
                    display_name=customer.display_name or None,
                    customer_number=number, email=customer.email,
                    phone=customer.phone, notes=customer.notes,
                    address_line1=customer.address_line1, street=customer.street,
                    postal_code=customer.postal_code, city=customer.city,
                    country=customer.country,
                    company_name=customer.company_name, tax_id=customer.vat_id,
                    first_name=customer.first_name, last_name=customer.last_name)
            except Exception as exc:                # z. B. doppeltes Kürzel
                result['errors'].append(
                    f'Kunde {number} ({customer.display_name}): {exc}')
                continue
            if contact_id:
                known[number] = contact_id
                result['customers'] += 1
        return known

    def _insert_wiso_invoices(self, data, contacts, result):
        """Rechnungen samt Positionen übernehmen.

        Der Absender steht in WISO nur einmal (Firmenstammdaten) und wird als
        Momentaufnahme in jede Rechnung geschrieben – so hält es auch die
        Rechnungsmaske von uBuchhaltung.

        Die Rechnungsnummer ist der Wiedererkennungsschlüssel: was es schon
        gibt, bleibt unverändert.
        """
        conn = self._get_connection()
        try:
            known = {row[0] for row in conn.execute(
                'SELECT InvoiceNumber FROM Invoices')}
            # Die eigene Firma steht als Kontakt vom Typ 'own' im Stamm; die
            # Rechnung verweist darauf und führt den Absender zusätzlich als
            # Momentaufnahme, damit spätere Änderungen alte Belege nicht ändern.
            own = conn.execute(
                "SELECT ID FROM Contacts WHERE ContactType = 'own' "
                'ORDER BY ID LIMIT 1').fetchone()
            # Bankverbindung für den Zahlungshinweis: WISO führt sie nicht an
            # der Rechnung, sondern im Briefbogen. Genommen wird das eigene
            # Geschäftskonto – ein Zahlungskonto mit IBAN, das keine Kasse ist.
            bank = conn.execute(
                'SELECT ID, BankName, Number, BIC FROM Accounts '
                "WHERE COALESCE(IsCash, 0) = 0 AND COALESCE(Number, '') != '' "
                'ORDER BY ID LIMIT 1').fetchone()
        finally:
            conn.close()
        own_company_id = own[0] if own else None
        if bank is None:
            result['invoice_warnings'].append(
                'Kein Geschäftskonto mit IBAN in der Kontenverwaltung – die '
                'Rechnungen bleiben ohne Bankverbindung')

        company = data.company
        for invoice in data.invoices:
            if invoice.number in known:
                result['invoices_skipped'] += 1
                continue
            due = None
            if invoice.date and invoice.payment_days:
                due = (datetime.date.fromisoformat(invoice.date)
                       + datetime.timedelta(days=invoice.payment_days)).isoformat()
            invoice_id = self.insert_invoice({
                'invoice_number': invoice.number,
                'invoice_date': invoice.date,
                'own_company_id': own_company_id,
                'seller_name': (company.name if company else '') or 'unbekannt',
                'seller_company': (company.company if company else '') or 'unbekannt',
                'seller_street': company.street if company else '',
                'seller_postal_code': company.postal_code if company else '',
                'seller_city': company.city if company else '',
                'seller_country': company.country if company else 'DE',
                'seller_vat_id': company.vat_id if company else '',
                'seller_email': company.email if company else '',
                'seller_phone': company.phone if company else '',
                'customer_id': contacts.get(str(invoice.customer_number)),
                'buyer_name': invoice.buyer_name or 'unbekannt',
                'buyer_company': invoice.buyer_company or '',
                'buyer_street': invoice.buyer_street,
                'buyer_postal_code': invoice.buyer_postal_code,
                'buyer_city': invoice.buyer_city,
                'buyer_country': invoice.buyer_country,
                'delivery_date': invoice.delivery_date,
                'payment_due_date': due,
                'bank_account_id': bank[0] if bank else None,
                'bank_name': bank[1] if bank else None,
                'bank_iban': bank[2] if bank else None,
                'bank_bic': bank[3] if bank else None,
                'tax_rate': invoice.tax_rate,
                'sum_net': invoice.sum_net, 'tax_amount': invoice.tax_amount,
                'sum_gross': invoice.sum_gross,
                # Bezahlt heißt: nichts mehr offen.
                'amount_due': 0 if invoice.status == 'paid' else invoice.sum_gross,
                'status': invoice.status,
                'intro_text': invoice.intro_text or None,
                'closing_text': invoice.closing_text or None,
            })
            known.add(invoice.number)
            result['invoices'] += 1
            for item in invoice.items:
                self.insert_invoice_item({
                    'invoice_id': invoice_id, 'position': item.position,
                    'description': item.description or '(ohne Bezeichnung)',
                    'quantity': item.quantity, 'unit': item.unit,
                    'price_per_unit': item.price_per_unit,
                    'total_net': item.total_net,
                    'tax_rate': item.tax_rate if item.tax_rate is not None
                    else invoice.tax_rate,
                })
                result['invoice_items'] += 1
            for warning in invoice.warnings:
                result['invoice_warnings'].append(
                    f'Rechnung {invoice.number}: {warning}')

    def _link_wiso_invoice_payments(self, data, result):
        """Zahlungen ihren Rechnungen zuordnen – über WISOs ``INVID``.

        WISO hängt an jede Buchung eines Rechnungsvorgangs die Rechnungs-Id.
        Zahlung ist davon aber nur die Zeile, die ein **Zahlungskonto**
        berührt; die Forderungsbuchung (Debitor an Erlöse) trägt dieselbe Id
        und darf nicht mitgezählt werden.

        Verknüpft wird bevorzugt die **Bankbewegung** des Vorgangs – sie ist
        das, was der Kontoauszug zeigt. Fehlt sie (für alte Jahre gibt es
        keine Auszüge), tritt der Buchungssatz selbst an ihre Stelle.

        Der Betrag behält sein **Vorzeichen**. Das ist keine Kosmetik: eine
        stornierte und neu gebuchte Zahlung erscheint dreimal (hin, zurück,
        hin) und hebt sich nur vorzeichenrichtig auf; Gutschriften sind
        durchweg negativ. Mit Betragsbeträgen käme das Dreifache heraus.
        """
        from importers.wiso_fdb import DEFAULT_LIQUID_ACCOUNTS

        liquid = set(DEFAULT_LIQUID_ACCOUNTS) | self._liquid_skr_accounts()
        numbers = {invoice.source_id: invoice.number for invoice in data.invoices}
        if not numbers:
            return

        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT InvoiceNumber, ID FROM Invoices')
        invoice_ids = {row[0]: row[1] for row in cursor.fetchall()}
        cursor.execute("SELECT SourceGroup, ID, ParentBooking_ID, Amount "
                       "FROM Bookings WHERE SourceGroup IS NOT NULL "
                       "AND SourceGroup != '' AND BookingType = 'entry'")
        by_group = {}
        for group, booking_id, parent_id, amount in cursor.fetchall():
            by_group.setdefault(group, []).append((booking_id, parent_id, amount))
        cursor.execute('SELECT InvoiceID, BookingID FROM InvoicePayments')
        already = {tuple(row) for row in cursor.fetchall()}
        conn.close()

        for booking in data.bookings:
            number = numbers.get(booking.invoice_id)
            invoice_id = invoice_ids.get(number) if number else None
            if invoice_id is None:
                continue
            if not ({booking.account, booking.counter_account} & liquid):
                continue                       # Forderungs-/Umbuchungszeile
            target = self._payment_booking(by_group.get(booking.group, []),
                                           booking.amount)
            if target is None or (invoice_id, target) in already:
                continue
            try:
                self.link_invoice_to_transaction(invoice_id, target,
                                                 booking.amount)
            except Exception as exc:           # bereits verknüpft o. Ä.
                result['payment_warnings'].append(f'Rechnung {number}: {exc}')
                continue
            already.add((invoice_id, target))
            result['payments_linked'] += 1

    @staticmethod
    def _payment_booking(candidates, amount):
        """Aus den Buchungen einer Quellgruppe die Zahlung heraussuchen.

        Die Bankbewegung ist die richtige Verknüpfung – sie steht im
        Kontoauszug. Nur wenn keine da ist, tritt der betragsgleiche
        Buchungssatz an ihre Stelle. Verglichen wird **mit Vorzeichen**, sonst
        ließe sich eine Zahlung nicht von ihrer Stornierung unterscheiden.
        """
        minor = to_minor(amount or 0)
        passend = [c for c in candidates if abs((c[2] or 0) - minor) < 50]
        if not passend:
            return None
        for booking_id, parent_id, _amount in passend:
            if parent_id:
                return parent_id
        return passend[0][0]

    def _insert_wiso_assets(self, assets, result):
        """Anlagegüter samt AfA-Plan übernehmen.

        Die Inventarnummer ist der Schlüssel: ein zweiter Lauf aktualisiert
        das vorhandene Anlagegut, statt es zu verdoppeln.
        """
        conn = self._get_connection()
        cursor = conn.cursor()
        coa_map = self._coa_map(cursor)

        for asset in assets:
            number = str(asset.number) if asset.number is not None else None
            cursor.execute('SELECT ID FROM Assets WHERE InventoryNumber = ?',
                           (number,))
            row = cursor.fetchone()
            # Anschaffung und Verkauf stehen beide **netto** in den Büchern –
            # nur so ist der Vergleich mit dem Restbuchwert aussagekräftig.
            values = (asset.label or 'Anlagegut', coa_map.get(asset.account),
                      asset.purchase_date, to_minor(asset.purchase_price or 0),
                      asset.useful_life_years or 1, asset.sale_date,
                      self._minor_opt(asset.sale_price),
                      'sold' if asset.sale_date else 'active')
            if row:
                asset_id = row[0]
                # COALESCE beim Erlös: laesst sich der Verkaufspreis nicht aus
                # WISO ableiten, darf ein zweiter Lauf einen von Hand
                # gepflegten Wert nicht wieder ausloeschen.
                cursor.execute('''
                    UPDATE Assets SET Name=?, COA_ID=?, PurchaseDate=?,
                        PurchasePrice=?, UsefulLifeYears=?, SaleDate=?,
                        SalePrice=COALESCE(?, SalePrice), Status=?
                    WHERE ID=?
                ''', values + (asset_id,))
            else:
                cursor.execute('''
                    INSERT INTO Assets
                        (Name, COA_ID, PurchaseDate, PurchasePrice,
                         UsefulLifeYears, SaleDate, SalePrice, Status,
                         InventoryNumber, DepreciationMethod)
                    VALUES (?,?,?,?,?,?,?,?,?,'linear')
                ''', values + (number,))
                asset_id = cursor.lastrowid
            result['assets'] += 1
            for warning in asset.warnings:
                result['asset_warnings'].append(
                    f'{asset.label or number}: {warning}')

            for entry in asset.depreciations:
                if entry.year is None:
                    continue
                cursor.execute('''
                    INSERT INTO AssetDepreciations
                        (Asset_ID, Year, DepreciationAmount, BookValue, Status)
                    VALUES (?,?,?,?,'booked')
                    ON CONFLICT(Asset_ID, Year) DO UPDATE SET
                        DepreciationAmount=excluded.DepreciationAmount,
                        BookValue=excluded.BookValue
                ''', (asset_id, entry.year, to_minor(entry.amount or 0),
                      to_minor(entry.book_value or 0)))
                result['depreciations'] += 1

        conn.commit()
        conn.close()
