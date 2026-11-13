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

CSV-Import und Datenbank-Import lassen sich nacheinander benutzen. Grundsatz: Was
der CSV-Import schon angelegt hat (``entry`` ohne ``SourceGroup``), übernimmt dieser
Import nur, wenn es nach Schlüssel (Beleg-Nr., Datum, Konto, Betrag) **und Text**
derselbe Satz ist und das Gegenkonto nicht widerspricht – die Buchung bekommt die
``SourceGroup`` und wird nicht ein zweites Mal angelegt. Im Zweifel legt er neu an
und meldet ähnliche vorhandene Buchungen als möglicherweise doppelt (Regeln:
:meth:`WisoFdbImportMixin._insert_wiso_bookings`).
"""
import datetime
from collections import namedtuple

from db.core import coa_id
from db.matching import (
    FORM_MIRROR_COA, FORM_MIRROR_LEGACY, LEGACY_CASH_NUMBERS, LEGACY_CASH_RANGE, MIRROR_FORMS,
)
from money import FACTOR, format_de, from_minor, to_minor

#: uBuchhaltung rechnet in SKR04.
SKR04 = 4

#: So viele übernommene und so viele möglicherweise doppelte Buchungen nennt das
#: Ergebnis einzeln; die Zähler zählen alle.
MAX_ADOPTED_ROWS = 500

#: So viele Fälle „AfA schon in der App gebucht“ nennt das Ergebnis einzeln
#: (``depreciation_conflict_rows``); ``depreciation_conflicts`` zählt alle.
MAX_CONFLICT_ROWS = 100

#: So viele Unterkonten von Zahlungskonten nennt das Ergebnis (``liquid_subaccounts``).
MAX_LIQUID_SUBACCOUNTS = 50

#: Die AfA eines Planjahrs ist in der App gebucht: Es gibt die Buchung, an die
#: ``AssetDepreciations.Booking_ID`` zeigt (``Database.book_depreciation``). Der Import
#: schreibt kein ``Booking_ID``. Festes SQL-Stück, kein Wert.
_APP_BOOKED_SQL = ('EXISTS (SELECT 1 FROM Bookings b '
                   'WHERE b.ID = AssetDepreciations.Booking_ID)')

#: Ein Planjahr, dessen AfA-Satz in den Büchern steht, ist gebucht – ein Jahr, das die App selbst
#: gebucht hat, bleibt unberührt. Festes SQL-Stück, kein Wert.
_PLAN_BOOKED_SQL = ("UPDATE AssetDepreciations SET Status = 'booked' WHERE Asset_ID = ? AND Year = ? "
                    f"AND Status = 'planned' AND NOT {_APP_BOOKED_SQL}")

#: Eine WISO-Anlage und ihre Anlage in der App (über die Inventarnummer): ``asset_id`` (``None``: es gibt
#: keine), ``name`` (Bezeichnung, sonst Inventarnummer) und ``years`` – ihre Planjahre als
#: ``{Jahr: (Status, von der App gebucht)}``.
_AssetLink = namedtuple('_AssetLink', 'name asset_id years')
#: Zu einer WISO-Anlage gibt es keine Anlage in der App (oder der Satz nennt eine Anlage, die BAS_INVENTORY nicht führt).
_NO_ASSET = _AssetLink('', None, {})

#: Eine Buchung des CSV-Imports als Kandidat; Beleg-Nr. und Text sind für den
#: Vergleich schon aufbereitet (Leerraum zusammengefasst, Text ohne Groß-/Kleinschreibung),
#: ``shown`` ist der gespeicherte Text, ``form`` die gespeicherte Form (``Bookings.EntryForm``).
_CsvRow = namedtuple('_CsvRow', 'id doc counter text shown form')

#: SKR04-Konten 1000–1099 (Vorräte): Ältere Versionen haben sie als Kasse gelesen.
STOCK_RANGE = LEGACY_CASH_NUMBERS


def _asset_name(asset):
    """Bezeichnung einer WISO-Anlage für Meldungen: ihr Text, sonst die Inventarnummer."""
    if asset.label:
        return asset.label
    return str(asset.number) if asset.number is not None else f'WISO-Anlage {asset.source_id}'


def _squash(value):
    """Leerraum zusammenfassen: „RE  12“ und „RE 12“ gelten als dieselbe Beleg-Nr."""
    return ' '.join((value or '').split())


def _comparable(value):
    """Text für den Vergleich: Leerraum zusammengefasst, Groß-/Kleinschreibung egal.

    Das Auslassungszeichen „…“ zählt als Leerraum: In der WISO-Datenbank ist es das Byte
    0x85, das als latin-1 gelesen ein Zeilentrenner (U+0085) ist und schon beim Lesen der
    Datenbank wie Leerraum behandelt wird – der Text kommt von dort ohne „…“ an."""
    value = (value or '').replace('\u2026', ' ').replace('\x85', ' ')
    return _squash(value).casefold()


def _as_csv_text(value):
    """Text eines Datenbank-Satzes in der Lesart der CSV – nur für den Vergleich.

    Die CSV wird als cp1252 gelesen, die WISO-Datenbank als latin-1: „€“, „–“, „…“ und
    andere Zeichen aus 0x80–0x9F kämen von dort als Steuerzeichen an und glichen nie dem
    Text der CSV-Zeile. Vor dem Zusammenfassen des Leerraums anwenden (U+0085 gälte
    sonst als Leerraum). Am gespeicherten Text ändert sich nichts; was sich nicht nach
    latin-1 kodieren lässt, bleibt, wie es ist.
    """
    value = value or ''
    try:
        return value.encode('latin-1').decode('cp1252', 'replace')
    except UnicodeEncodeError:
        return value


def _matching_row(rows, counter_coa, text, locked=False, fallback=False):
    """Die Zeile des CSV-Imports, die zu einem WISO-Satz passt – oder ``None``.

    ``rows`` haben schon dieselbe Beleg-Nr. und sind nach ID sortiert; ``text`` ist
    die Vergleichsform des WISO-Satzes. Der Text muss immer gleich und nicht leer
    sein – zwei leere Texte beweisen nichts. Dazu das Gegenkonto: gleich (beide
    bekannt) oder die Zeile hat keines. Ein anderes Gegenkonto passt nie. Gleiches
    Gegenkonto geht vor, dann die kleinste ID.

    ``locked`` (mehrdeutiger Schlüssel) und ``fallback`` (Ersatzsuche ohne Konto)
    lassen nur das gleiche Gegenkonto zu.
    """
    if not text:
        return None
    open_row = None
    for row in rows:
        if row.text != text:
            continue
        if row.counter is not None:
            if row.counter == counter_coa:
                return row
        elif open_row is None and not (locked or fallback):
            open_row = row
    return open_row


def _row_on_base_account(pool, booking, coa_map, coa, counter_coa, amount, text):
    """Die Zeile einer schon importierten Gruppe, die ein Import bis 2.2.4 für diesen Satz
    geschrieben hat – oder ``None``.

    Dem Unterkonto eines Satzes ordnet der Leser heute die SKR04-Nummer zu, die WISO zum Konto
    speichert (z. B. 6532); ein Import bis 2.2.4 hat die des Basiskontos genommen (6530).
    ``booking.legacy_account``/``legacy_counter_account`` nennen diese alte Nummer, wo sie abweicht.
    Gesucht wird die Zeile mit dem alten Konto bzw. Gegenkonto (die andere Seite gleich), gleichem
    Datum, gleichem |Betrag| und gleichem Text (``text`` in der Vergleichsform). Ein Basiskonto, das
    der Kontenrahmen nicht kennt, passt nie.
    """
    old_coa = coa if booking.legacy_account is None else coa_map.get(booking.legacy_account)
    old_counter = (counter_coa if booking.legacy_counter_account is None
                   else coa_map.get(booking.legacy_counter_account))
    if ((booking.legacy_account is not None and old_coa is None)
            or (booking.legacy_counter_account is not None and old_counter is None)):
        return None
    return next((row for row in pool
                 if row[1] == booking.date and row[2] == old_coa and row[3] == old_counter
                 and abs(row[4]) == abs(amount) and _comparable(_as_csv_text(row[6])) == text),
                None)


class WisoFdbImportMixin:
    """Buchungssätze und Anlagen aus einer ``.FDB``-Datei übernehmen."""

    def import_wiso_fdb(self, path, standard_chart_path=None,
                        with_assets=True, with_invoices=True, today=None) -> dict:
        """WISO-Mandantendatenbank importieren.

        Args:
            path: ``DB1.FDB`` des Mandanten.
            standard_chart_path: ``DB0.FDB`` – liefert Konten, die im
                Mandantenrahmen fehlen. Optional.
            with_assets: Anlagenverzeichnis samt AfA-Plan mitnehmen.
            with_invoices: Kunden und Ausgangsrechnungen samt Positionen
                mitnehmen. Die Kundennummern bleiben die aus WISO.
            today: Stichtag für vorausgeplante AfA – ein ISO-Datum oder ein ``date``,
                ohne Angabe der Tag des Imports (Ortsdatum). Ein AfA-Satz mit einem Datum
                **nach** dem Stichtag wird nie neu angelegt (siehe unten).

        Reihenfolge, damit jede Prüfung den Bestand vor diesem Lauf sieht: erst die
        Datenbank prüfen und lesen (``check``, ``read``), dann fehlende SKR04-Konten
        anlegen (nicht für künftige AfA-Sätze), dann die **Buchungen** schreiben
        (``_insert_wiso_bookings``): in einer schreibenden Transaktion (``BEGIN IMMEDIATE``), die
        zuerst die Anlagen der App und ihre Planjahre liest (``_link_assets``: welche Jahre die App
        selbst gebucht hat, welche geplant sind) – so schiebt sich kein ``book_depreciation`` einer
        anderen Anfrage dazwischen – und den Status der vorhandenen Planjahre mitführt. Erst danach
        die **Anlagen** samt AfA-Plan (``_insert_wiso_assets``, mit den Jahren, deren AfA-Satz in
        den Büchern steht), zuletzt Kunden und Rechnungen. Der Abgleich „AfA schon in der App
        gebucht“ steht deshalb vor dem Schreiben der Planzeilen dieses Laufs.

        Returns:
            dict mit ``imported``, ``skipped``, ``adopted`` (Buchungen aus dem
            CSV-Import, die wiedererkannt statt neu angelegt wurden),
            ``adopted_rows`` (davon die ersten 500 mit ``id``, ``date``,
            ``amount`` in Euro und dem ``text`` des WISO-Satzes),
            ``possible_duplicates_count`` (vorhandene Buchungen, die einem neu
            angelegten Satz in Datum, Konto und Betrag gleichen, aber nicht
            übernommen wurden), ``possible_duplicates`` (davon die ersten 500
            mit ``id``, ``date``, ``amount`` in Euro und ``text`` der vorhandenen
            Buchung), ``assets``, ``depreciations``, ``tax_rows_skipped``,
            ``memo_rows_skipped``, ``missing_coa`` und ``errors``. Dazu
            ``negative_gross`` (Sätze mit negativem Bruttobetrag und genau einer liquiden Seite:
            WISO schreibt den Bruttobetrag nicht negativ – Umkehrungen stehen als vertauschte
            Konten; der Zähler ist ein Wächter für Sätze außerhalb dieses Musters. Die Regel ist
            festgeschrieben: Bei genau einer liquiden Seite bestimmt die Geldbewegung das
            Vorzeichen),
            ``stock_range_accounts`` (Konten 1000–1099 in den Sätzen: ältere Versionen haben sie
            als Kasse gelesen; je Konto ``nummer``, ``bezeichnung``, ``anzahl`` der Sätze),
            ``legacy_mirrors`` (Spiegel der Datenbank, die als schon vorhandene Spiegel des
            Bestands wiedererkannt wurden: Sie stehen noch in der alten Lesart, ihre Form und
            ihr Betrag bleiben unverändert), ``legacy_stock_rows`` (Sätze auf Konten 1000–1099,
            die eine ältere Version aus dem CSV-Import mit ihrer Lesart – diese Konten als Kasse –
            gespeichert hat und die der Import übernimmt statt sie neu anzulegen) und
            ``form_conflicts_count``/``form_conflicts``
            (schon importierte Sätze, deren gespeicherte Form von der des frischen Imports
            abweicht: nur gemeldet, die ersten 500 mit ``id``, ``date``, ``amount``, ``text``,
            ``stored`` und ``fresh``).

            Unterkonten: ``subaccount_rows`` zählt Sätze, die ein Import bis 2.2.4 schon auf das
            **Basiskonto** geschrieben hat (z. B. 6530), während der Leser dem Unterkonto heute seine
            eigene, von WISO gespeicherte SKR04-Nummer gibt (z. B. 6532). Der Re-Import erkennt sie
            an der Quellgruppe wieder, legt keinen zweiten Satz an und ändert Konto, Betrag und Form
            nicht. ``liquid_subaccounts`` nennt Unterkonten eines Bank-, Kassen- oder
            Verrechnungskontos, die in der App kein Zahlungskonto sind (höchstens 50 mit ``nummer``,
            ``basis`` – das Zahlungskonto, unter dem ein Import bis 2.2.4 sie gelesen hat – und
            ``anzahl`` der Sätze): Deren Sätze werden als Umbuchung gelesen. Nur gemeldet.

            AfA: WISO schreibt den ganzen AfA-Plan sofort als Buchungssätze, auch für künftige Jahre.
            Der Leser liefert die Sätze nach dem Stichtag gekennzeichnet; der Import legt sie **nie**
            neu an, übernimmt sie nie aus einer CSV-Zeile und legt keine Konten für sie an
            (``future_depreciations_skipped`` zählt sie). Steht ein solcher Satz schon in den Büchern
            (Bestand aus einem Import bis 2.2.4: seine Quellgruppe ist bekannt, und eine Zeile der Gruppe
            gleicht ihm genau oder liegt, vom Nutzer geändert, im Planjahr), bleibt er unverändert und sein
            Jahr gilt als gebucht (``future_depreciations_kept`` zählt diese Sätze).

            **Der Status eines Planjahrs folgt der Buchung, nicht dem Datum:** ``booked``, wenn der
            AfA-Satz der Anlage für dieses Jahr in den Büchern steht (in diesem Lauf angelegt, aus dem
            CSV-Import übernommen oder als vorhandene Zeile seiner Gruppe wiedererkannt – auch ein
            künftiger), sonst ``planned`` – dann bucht die App das Jahr selbst (``depreciations`` zählt
            alle Planjahre dieses Laufs, ``depreciations_planned`` die, die danach ``planned`` sind).
            Ein geplantes Jahr, dessen Satz jetzt in den Büchern steht, wird ``booked`` (auch bei
            ``with_assets=False``). **Geänderter Satz:** Gleicht einem AfA-Satz einer bekannten Gruppe keine
            Zeile genau, hat die Gruppe aber eine freie Zeile mit einem Datum im Planjahr (gleich welcher
            Betrag, Tag oder welche Konten), hat der Nutzer ihn geändert: Er steht in den Büchern, das Jahr gilt
            als gebucht, es wird nichts angelegt (jede Zeile dient höchstens einem Satz; vorher bekommt jeder
            Satz seine genaue Zeile). **Nachtrag:** Hat die Gruppe für das Jahr keine Zeile und liegt der Satz
            nicht mehr in der Zukunft, wird er nur dann nachträglich angelegt, wenn die Anlage in der App
            eindeutig zugeordnet ist und ihre Planzeile dieses Jahres existiert und ``planned`` ist; sonst
            nie – ein Satz einer bekannten Gruppe wird nicht neu angelegt (der Nutzer kann ihn gelöscht
            haben). Hat die App ein Jahr schon selbst gebucht
            (``AssetDepreciations.Booking_ID``), bleibt die Planzeile ganz unverändert, und der AfA-Satz
            der Datenbank für dieses Jahr wird nicht angelegt: ``depreciation_conflicts`` zählt solche
            Anlage-Jahr-Fälle, ``depreciation_conflict_rows`` nennt die ersten 100 mit ``anlage``
            (Bezeichnung oder Inventarnummer) und ``jahr``. Die Zuordnung zur Anlage der App läuft über
            die Inventarnummer, nur wenn sie in der WISO-Datenbank eindeutig ist (sonst steht eine
            Warnung in ``asset_warnings``). ``depreciation_plan_mismatches`` zählt Anlagen, deren
            geplante Jahre die App anders plant als WISO (Warnung in ``asset_warnings``): Beim Buchen
            gilt der Plan der App. Bestand aus einem Import bis 2.2.4 (künftige AfA-Buchungen mit
            Quellgruppe, Jahre ``booked``) bleibt unberührt.
        """
        from importers.wiso_fdb import WisoDatabase

        result = {'imported': 0, 'skipped': 0, 'adopted': 0, 'adopted_rows': [],
                  'possible_duplicates_count': 0, 'possible_duplicates': [],
                  'negative_gross': 0, 'stock_range_accounts': [], 'legacy_mirrors': 0,
                  'legacy_stock_rows': 0, 'subaccount_rows': 0,
                  'form_conflicts_count': 0, 'form_conflicts': [],
                  'liquid_subaccounts': [],
                  'assets': 0, 'depreciations': 0, 'depreciations_planned': 0,
                  'future_depreciations_skipped': 0, 'future_depreciations_kept': 0,
                  'depreciation_conflicts': 0, 'depreciation_conflict_rows': [],
                  'depreciation_plan_mismatches': 0,
                  'tax_rows_skipped': 0,
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
            data = wiso.read(self._liquid_skr_accounts(), today=today)
            result['tax_rows_skipped'] = data.tax_rows_skipped
            result['memo_rows_skipped'] = data.memo_rows_skipped
            result['missing_coa'] = {
                number: {'anzahl': count,
                         'bezeichnung': data.unmapped_labels.get(number, '')}
                for number, count in sorted(data.unmapped_accounts.items(),
                                            key=lambda kv: -kv[1])}
            result['created_coa'] = self._create_missing_coa(data)
            result['negative_gross'] = data.negative_gross
            result['liquid_subaccounts'] = data.liquid_subaccounts[:MAX_LIQUID_SUBACCOUNTS]
            result['stock_range_accounts'] = self._stock_range_accounts(data.bookings)
            booked_years = self._insert_wiso_bookings(
                data.bookings, result, self._recipients_by_invoice(data), data.assets)
            if with_assets:
                self._insert_wiso_assets(data.assets, result, booked_years)
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

    def _stock_range_accounts(self, bookings):
        """Konten 1000–1099 (SKR04: Vorräte) in den Sätzen: ``[{'nummer', 'bezeichnung',
        'anzahl'}]`` nach Nummer. Ältere Versionen haben sie als Kasse gelesen – ein Satz
        darauf kann eine Kasse meinen. Die Bezeichnung steht im Kontenrahmen (nach
        ``_create_missing_coa``)."""
        counts = {}
        for booking in bookings:
            if booking.future_depreciation:
                continue                     # kommt nicht in die Bücher
            for number in {booking.account, booking.counter_account}:
                if number in STOCK_RANGE:
                    counts[number] = counts.get(number, 0) + 1
        if not counts:
            return []
        conn = self._get_connection()
        try:
            names = dict(conn.execute(
                'SELECT AccountNumber, Name FROM ChartOfAccounts WHERE AccountNumber BETWEEN ? AND ?',
                (LEGACY_CASH_RANGE[0], LEGACY_CASH_RANGE[1])).fetchall())
        finally:
            conn.close()
        return [{'nummer': number, 'bezeichnung': names.get(number, ''), 'anzahl': count}
                for number, count in sorted(counts.items())]

    def _create_missing_coa(self, data):
        """SKR04-Konten anlegen, die der eigene Kontenrahmen noch nicht kennt.

        Hier wird nichts geraten: Nummer **und** Bezeichnung stammen aus WISOs
        eigener Umschlüsselungstabelle. Ohne diesen Schritt verlören Buchungen
        auf Konten, die uBuchhaltung nicht mitbringt, ihre Kontierung und
        stünden in der Übersicht als offen. Künftige AfA-Sätze kommen nicht in die Bücher (der
        Import legt sie nie an): Konten, die nur sie brauchen, entstehen nicht.
        """
        wanted = {}
        for account in data.chart.values():
            if account.skr04:
                wanted.setdefault(account.skr04, account.text)
        used = {number for booking in data.bookings if not booking.future_depreciation
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

    @staticmethod
    def _recipients_by_invoice(data):
        """Rechnungs-Id → Kundenname.

        ``MOV_FINACC_ACCRECORDS`` führt keinen Empfänger – den kennt sonst nur
        der Kontoauszug. Für rechnungsbezogene Buchungen lässt er sich aber
        ableiten: die Buchung nennt ihre Rechnung, die Rechnung den Kunden.
        """
        return {invoice.source_id: invoice.buyer_name
                for invoice in data.invoices if invoice.buyer_name}

    def _link_assets(self, assets, result, cursor):
        """Die WISO-Anlagen den Anlagen der App zuordnen – samt der Planjahre der App.

        Returns ``(links, excluded)``: ``links`` bildet ``INVENTORYID`` (``WisoAsset.source_id``)
        auf einen :data:`_AssetLink` ab – die Anlage der App mit ihren Planjahren (Status, von der
        App gebucht) oder ohne Anlage (``asset_id`` ``None``) –, ``excluded`` sind die
        ``INVENTORYID`` der Anlagen, die sich nicht zuordnen lassen: ohne Inventarnummer oder mit
        einer, die in der WISO-Datenbank mehrfach vorkommt. Sie nehmen am Abgleich der AfA mit dem
        Plan nicht teil (für ihre AfA-Sätze gilt der Stichtag und sonst die Gruppenlogik ohne
        Anlagenbezug); der Import meldet jede in ``asset_warnings``.

        Zugeordnet wird über die Inventarnummer (``Assets.InventoryNumber`` ist die ``INVNO`` aus
        WISO, ``WisoAsset.number``; ``INVENTORYID`` der Buchungen ist ``WisoAsset.source_id``).
        Ein Planjahr ist von der App gebucht, wenn die Buchung existiert, an die es zeigt
        (``_APP_BOOKED_SQL``) – so, wie ``book_depreciation`` es schreibt. Gelesen wird der Bestand
        **vor** den Schreibvorgängen dieses Laufs und mit ``cursor`` der Transaktion, die
        :meth:`_insert_wiso_bookings` als schreibende (``BEGIN IMMEDIATE``) eröffnet hat: Zwischen
        Lesen und Schreiben kann keine andere Anfrage ein Jahr buchen.
        """
        counts = {}
        for asset in assets:
            if asset.number is not None:
                counts[str(asset.number)] = counts.get(str(asset.number), 0) + 1
        excluded = set()
        for asset in assets:
            if asset.number is None:
                reason = 'hat keine Inventarnummer'
            elif counts[str(asset.number)] > 1:
                reason = f'Inventarnummer {asset.number} mehrfach vergeben'
            else:
                continue
            excluded.add(asset.source_id)
            result['asset_warnings'].append(
                f'{_asset_name(asset)}: {reason} – die AfA lässt sich nicht mit dem Plan abgleichen')

        app_assets = {number: asset_id for number, asset_id in cursor.execute(
            'SELECT InventoryNumber, ID FROM Assets WHERE InventoryNumber IS NOT NULL').fetchall()}
        plans = {}
        for asset_id, year, status, booked in cursor.execute(
                f'SELECT Asset_ID, Year, Status, {_APP_BOOKED_SQL} FROM AssetDepreciations').fetchall():
            plans.setdefault(asset_id, {})[year] = (status, bool(booked))
        links = {}
        for asset in assets:
            if asset.source_id is None or asset.source_id in excluded:
                continue
            app_id = app_assets.get(str(asset.number))
            links[asset.source_id] = _AssetLink(_asset_name(asset), app_id,
                                                plans.get(app_id, {}) if app_id is not None else {})
        return links, excluded

    def _insert_wiso_bookings(self, bookings, result, recipients=None, assets=()):
        """Buchungssätze schreiben; ``SourceGroup`` trägt die Split-Klammer.

        Returns die Menge der Planjahre, deren AfA-Satz danach in den Büchern steht:
        ``{(INVENTORYID, Jahr)}`` – für den Status der Planzeilen in :meth:`_insert_wiso_assets`.

        Alles läuft in EINER schreibenden Transaktion (``BEGIN IMMEDIATE``). Sie liest als Erstes die
        Anlagen der App und ihre Planjahre (:meth:`_link_assets`, ``assets`` sind die WISO-Anlagen):
        Zwischen diesem Lesen und dem Schreiben kann keine andere Anfrage ein Jahr buchen
        (``book_depreciation`` braucht dieselbe Sperre) – die Prüfung „die App hat das Jahr schon selbst
        gebucht“ gilt bis zum Commit. Die Planzeilen dieses Laufs schreibt erst :meth:`_insert_wiso_assets`.

        Duplikate erkennt die Quell-Id (``SourceGroup`` + Betrag + Konto reicht
        nicht, weil ein Vorgang gleichbetragige Zeilen enthalten darf): schon
        vorhandene ``SourceGroup``-Werte werden übersprungen, damit ein zweiter
        Lauf nichts verdoppelt.

        Der CSV-Import schreibt keine ``SourceGroup``. Seine Buchungen übernimmt
        dieser Import statt sie neu anzulegen (``result['adopted']``, die ersten
        500 mit Datum, Betrag und Text in ``result['adopted_rows']``) – aber nur,
        was nach Schlüssel **und Text** derselbe Satz ist. Im Zweifel wird neu
        angelegt und gemeldet: lieber eine sichtbare zweite Buchung als eine still
        falsch übernommene.

        * Kandidat ist eine ``entry`` ohne ``SourceGroup``, ohne ``AutoMirror`` und
          ohne ``Account_ID`` mit gleichem Datum, Betrag und Konto sowie gleicher
          Beleg-Nr. (Leerraum zusammengefasst). Eigene Einfügungen dieses Laufs
          sind nie Kandidaten.
        * Der Text muss immer gleich sein (Leerraum zusammengefasst, Groß-/Klein-
          schreibung egal; der Text des WISO-Satzes wird dafür von latin-1 nach
          cp1252 umgesetzt, gespeichert wird er unverändert) und darf nicht leer
          sein: Zwei leere Texte beweisen nichts, solche Sätze werden neu angelegt.
        * Das Gegenkonto muss gleich sein (beide bekannt) oder dem Kandidaten
          fehlen; ein anderes Gegenkonto passt nie. Gewählt wird zuerst der
          Kandidat mit gleichem Gegenkonto, dann der ohne, bei Gleichstand die
          kleinste ID. Jeder Kandidat wird höchstens einmal verbraucht – zwei
          gleich hohe Teilbuchungen übernehmen zwei CSV-Zeilen.
        * Mehrdeutigkeitssperre: Tragen Sätze dieses Imports mit gleichem Datum,
          Betrag, Konto, Beleg-Nr. und Text verschiedene Gegenkonten, übernehmen
          sie keinen Kandidaten ohne Gegenkonto – welcher Satz zu welcher Zeile
          gehört, ließe sich nur raten. Kandidaten mit gleichem Gegenkonto bleiben
          erlaubt.
        * Fehlte der CSV-Zeile das Konto (im Kontenrahmen unbekannt), wird
          ersatzweise ohne Konto gesucht: nur wenn es zum Schlüssel mit Konto beim
          Laden keinen Kandidaten gab (nicht, wenn sie bloß aufgebraucht sind), und
          nur bei gleichem bekanntem Gegenkonto und gleichem, nicht leerem Text.
        * Übernommen wird mit einem UPDATE (im Replay-Log): ``SourceGroup`` setzen,
          leere Konten und einen leeren Empfänger füllen, Steuersatz und
          Steuerbetrag ergänzen, wenn beide fehlen. Alles andere – Text,
          Verknüpfungen, Änderungen des Nutzers – bleibt.
        * Die Form (``Bookings.EntryForm``) schreibt der Import mit, wie er das Vorzeichen
          gesetzt hat (``WisoBooking.form``) – für Sätze, die er neu anlegt. Die Form einer
          vorhandenen Zeile ändert kein Import: Ein übernommener Spiegel des Bestands
          (``mirror_legacy``) bleibt einer, nur seine Quellgruppe wird gesetzt, und der Import
          zählt ihn (``legacy_mirrors``). Füllt die Übernahme ein fehlendes Konto, ändern sich
          die Konten des Satzes – dann gilt für ihn die Form des Imports.
        * Ein Spiegel des Bestands mit umgekehrtem Vorzeichen (``mirror_legacy``, Betrag
          −gross: Richtung auf dem Gegenkonto) wird übernommen, wenn Konto, Gegenkonto, Beleg-Nr.
          und (nicht leerer) Text gleich sind und nur das Vorzeichen abweicht, und die Datei
          nicht selbst eine Zeile mit dem Betrag dieses Spiegels enthält (dann wäre es deren
          Duplikat). Form und Betrag bleiben. Mehrdeutigkeit und „im Zweifel neu anlegen“ gelten
          wie oben.
        * Ein Satz, der ein Konto 1000–1099 berührt, wird zusätzlich mit dem Betrag gesucht, den die
          Regel von 2.0.x (diese Konten als Kasse) ergeben hätte: Eine ältere Version hat die Zeile des
          CSV-Imports so gespeichert. Es gelten dieselben Schutzregeln (Text gleich und nicht leer,
          Beleg-Nr., Konto, Gegenkonto – hier immer beide bekannt und gleich) und die Datei darf keine
          Zeile mit diesem Betrag selbst beanspruchen. Übernommen wird mit der Quellgruppe, Form und
          Betrag bleiben (``legacy_stock_rows``); Steuer und Konten füllt die Übernahme nicht.
        * Bekannte ``SourceGroup`` (Re-Import): Statt zu überspringen, wird der Satz mit der
          Zeile der Gruppe verglichen (Datum, Konto, Gegenkonto, |Betrag|). Ein
          ``mirror_legacy``-Spiegel wird gezählt (``legacy_mirrors``); weicht eine andere
          gespeicherte Form von der des frischen Imports ab, wird das nur gemeldet
          (``form_conflicts``) – nichts wird umgedeutet. **Wiedererkannt wird ein Satz an seiner
          Quellgruppe, nicht am Konto**: Ein Satz einer bekannten Gruppe wird nie neu angelegt, auch
          wenn sein Konto nicht mehr zur Zeile passt – außer dem fehlenden AfA-Satz (siehe unten).
          Erst bekommt jeder Satz aller bekannten Gruppen seine genaue Zeile (jede Zeile gehört höchstens
          einem Satz), erst danach dienen die übrigen Zeilen einem AfA-Satz als Nachweis (siehe unten).
        * Unterkonten: Ein Import bis 2.2.4 hat einen Satz auf einem Unterkonto auf das Basiskonto
          geschrieben (6530); der Leser nennt heute die gespeicherte SKR04-Nummer des Unterkontos
          (6532) und liefert die alte Nummer als ``legacy_account``/``legacy_counter_account``. Passt
          zu einem Satz einer bekannten Gruppe keine Zeile mit den heutigen Konten, wohl aber eine
          mit den alten (Datum, |Betrag| und Text gleich, die andere Seite gleich), zählt der Import
          sie (``subaccount_rows``). Die Zeile bleibt, wie sie ist: Konto, Betrag und Form ändern
          sich nicht, und es entsteht kein zweiter Satz.
        * AfA-Sätze (``WisoBooking.is_depreciation``, Planjahr ``depreciation_year``) und die Planjahre der
          App (aus :meth:`_link_assets`; Anlagen ohne eindeutige Inventarnummer nehmen an alldem nicht teil
          – für ihre Sätze gilt die Gruppenlogik ohne Anlagenbezug):

          - Hat die App das Jahr selbst gebucht, wird der Satz nicht angelegt – und auch keine
            CSV-Zeile übernommen –, damit das Jahr nicht doppelt zählt. Gezählt wird je Anlage und
            Jahr einmal (``depreciation_conflicts``), die ersten 100 stehen mit Bezeichnung und Jahr
            in ``depreciation_conflict_rows``.
          - Steht der Satz danach in den Büchern (neu angelegt, aus dem CSV-Import übernommen oder
            als Zeile seiner bekannten Gruppe wiedererkannt), merkt sich der Lauf ``(INVENTORYID,
            Jahr)`` als gebucht, und ein geplantes Jahr der App wird sofort ``booked`` – in derselben
            Transaktion, auch ohne dass Anlagen importiert werden.
          - **Geänderter Satz:** Gleicht einem AfA-Satz einer bekannten Gruppe keine Zeile genau, hat die Gruppe
            aber noch eine freie Zeile mit einem Datum im Planjahr (``depreciation_year``; gleich welcher
            Betrag, welcher Tag, welche Konten), hat der Nutzer den Satz geändert: Er steht in den Büchern, es
            wird nichts angelegt, und das Jahr gilt als gebucht (``in_books``: ein geplantes Jahr der App
            wird ``booked``, eine neue Planzeile ebenso; ein von der App gebuchtes Jahr ist vorher als
            Konflikt abgehandelt). Jede Zeile dient höchstens einem AfA-Satz als Nachweis, und die genauen
            Treffer aller Sätze sind vorher vergeben – ein Satz, dessen Zeile der Nutzer gelöscht hat, nimmt
            so nie die Zeile eines anderen Satzes. Grenzen: Der Nachweis gehört keinem bestimmten Satz (bei
            zwei Sätzen eines Jahres und nur einer verbliebenen, geänderten Zeile nimmt sie der erste der
            Datei), die Zeile eines nicht erkannten Satzes derselben Gruppe im selben Jahr zählt ebenso,
            und ein in ein anderes Jahr verschobener Satz ist nicht zu erkennen.
          - **Nachtrag:** Hat die Gruppe für das Planjahr keine Zeile (ein früherer Lauf hat den Satz als
            künftig übersprungen, die Gruppe trägt mehrere Jahre), wird er nur dann angelegt wie ein neuer,
            wenn die Anlage in der App eindeutig zugeordnet ist **und** ihre Planzeile dieses Jahres
            existiert, ``planned`` ist und nicht von der App gebucht. In jedem anderen Fall – keine Anlage in
            der App, keine Planzeile, Jahr ``booked``, Anlage ohne eindeutige Inventarnummer – gilt: Ein Satz
            einer bekannten Gruppe wird nie neu angelegt (der Nutzer kann ihn gelöscht haben).
          - **Künftige Sätze** (``WisoBooking.future_depreciation``, Datum nach dem Stichtag) werden nie
            angelegt, nie aus einer CSV-Zeile übernommen und lösen weder Nachtrag noch Konflikt aus; die
            Konten, die nur sie brauchen, hat :meth:`_create_missing_coa` nicht angelegt. Steht ein solcher
            Satz schon in den Büchern – seine Quellgruppe ist bekannt, und eine Zeile der Gruppe gleicht ihm
            genau oder liegt, vom Nutzer geändert, im Planjahr (Bestand aus einem Import bis 2.2.4) –, bleibt
            er unverändert und sein Jahr gilt als gebucht (``future_depreciations_kept``, auch in
            ``skipped``); ein geplantes Jahr der App wird ``booked``, ein von der App gebuchtes bleibt, wie es
            ist. Sonst zählt er in ``future_depreciations_skipped``.
        * Möglicherweise doppelt: Hat dieser Lauf zu Datum, Betrag und Konto (bzw.
          der Ersatzsuche) einen Satz neu angelegt, werden die dazu geladenen,
          nicht übernommenen Kandidaten gemeldet (``possible_duplicates_count``,
          die ersten 500 in ``possible_duplicates``) – ohne Rücksicht auf Text und
          Beleg-Nr.
        """
        recipients = recipients or {}
        booked_years = set()             # (INVENTORYID, Jahr) der AfA-Sätze, die in den Büchern stehen
        depreciation_cases = {}          # (INVENTORYID, Jahr) -> Eintrag für die Ergebnisliste
        adopt_sql = '''
            UPDATE Bookings SET
                SourceGroup     = COALESCE(?, SourceGroup),
                COA_ID          = COALESCE(COA_ID, ?),
                CounterCOA_ID   = COALESCE(CounterCOA_ID, ?),
                TaxRate         = CASE WHEN TaxRate IS NULL AND TaxAmount IS NULL
                                       THEN ? ELSE TaxRate END,
                TaxAmount       = CASE WHEN TaxRate IS NULL AND TaxAmount IS NULL
                                       THEN ? ELSE TaxAmount END,
                RecipientClient = COALESCE(NULLIF(RecipientClient, ''), ?, RecipientClient),
                EntryForm       = COALESCE(?, EntryForm)
            WHERE ID = ?
        '''
        with self.transaction() as tx:
            tx.conn.execute('BEGIN IMMEDIATE')
            cursor = tx.conn.cursor()
            links, excluded = self._link_assets(assets, result, cursor)
            coa_map = self._coa_map(cursor)
            cursor.execute("SELECT DISTINCT SourceGroup FROM Bookings "
                           "WHERE SourceGroup IS NOT NULL AND SourceGroup <> ''")
            known_groups = {row[0] for row in cursor.fetchall()}
            pools = {}
            created_for = set()

            def csv_rows(key):
                """Zeilen des CSV-Imports zu (Datum, Betrag, Konto) als Paar
                (Beleg-Nrn. beim Laden, noch freie Zeilen); je Schlüssel einmal
                geladen – vor den eigenen Einfügungen dieses Laufs, die so nie
                Kandidaten sind – und von den Aufrufern nacheinander verbraucht.
                Die Beleg-Nr. wird in Python verglichen, damit Leerraum nicht zählt."""
                if key not in pools:
                    cursor.execute(
                        "SELECT ID, DocumentNumber, CounterCOA_ID, Text, EntryForm FROM Bookings "
                        "WHERE BookingType = 'entry' AND COALESCE(SourceGroup, '') = '' "
                        "AND COALESCE(AutoMirror, 0) = 0 AND Account_ID IS NULL "
                        "AND DateBooking = ? AND Amount = ? AND COA_ID IS ? "
                        "ORDER BY ID", key)
                    rows = [_CsvRow(row[0], _squash(row[1]), row[2],
                                    _comparable(row[3]), row[3] or '', row[4])
                            for row in cursor.fetchall()]
                    pools[key] = ({row.doc for row in rows}, rows)
                return pools[key]

            # Einmal über alle Sätze: Schlüssel mit verschiedenen Gegenkonten sind
            # mehrdeutig (auch Sätze, die schon eine SourceGroup haben, zählen mit; künftige
            # AfA-Sätze nicht – sie kommen nie in die Bücher und nehmen keinem Satz eine Übernahme).
            prepared = []
            counters = {}
            legacy_counters = {}
            accounts = {}      # dasselbe für die Ersatzsuche: gleicher Satz, verschiedene Konten
            for booking in bookings:
                coa = coa_map.get(booking.account)
                counter_coa = coa_map.get(booking.counter_account)
                amount = to_minor(booking.amount or 0)
                number = _squash(booking.document_number)
                text = _comparable(_as_csv_text(booking.text))
                key = (booking.date, amount, coa, number, text)
                if not booking.future_depreciation:
                    counters.setdefault(key, set()).add(counter_coa)
                    if booking.legacy_amount is not None:
                        legacy_counters.setdefault(
                            (booking.date, to_minor(booking.legacy_amount), coa, number, text),
                            set()).add(counter_coa)
                    accounts.setdefault((booking.date, amount, number, text, counter_coa),
                                        set()).add(coa)
                prepared.append((booking, coa, counter_coa, amount, number, text, key))
            ambiguous = {key for key, seen in counters.items() if len(seen) > 1}
            # dasselbe für die Beträge der Regel von 2.0.x (Konten 1000–1099): Sätze, die sich nur im
            # Gegenkonto unterscheiden, lassen sich keiner Zeile sicher zuordnen
            legacy_ambiguous = {key for key, seen in legacy_counters.items() if len(seen) > 1}
            # Die Ersatzsuche kennt das Konto der CSV-Zeile nicht: Unterscheiden sich
            # zwei Sätze nur im Konto (z. B. zwei gleich hohe Löhne auf zwei
            # Personenkonten), ließe sich nicht sagen, welche Zeile zu welchem gehört.
            ambiguous_accounts = {key for key, seen in accounts.items() if len(seen) > 1}

            group_pools = {}

            def group_rows(group):
                """Zeilen einer schon importierten Gruppe (Re-Import), je Gruppe einmal geladen und
                von den Aufrufern nacheinander verbraucht."""
                if group not in group_pools:
                    cursor.execute(
                        "SELECT ID, DateBooking, COA_ID, CounterCOA_ID, Amount, EntryForm, Text "
                        "FROM Bookings WHERE BookingType = 'entry' AND SourceGroup = ? "
                        "ORDER BY ID", (group,))
                    group_pools[group] = [list(row) for row in cursor.fetchall()]
                return group_pools[group]

            def note_conflict(row_id, date, minor, text, stored, fresh):
                result['form_conflicts_count'] += 1
                if len(result['form_conflicts']) < MAX_ADOPTED_ROWS:
                    result['form_conflicts'].append(
                        {'id': row_id, 'date': date, 'amount': float(from_minor(minor)),
                         'text': text or '', 'stored': stored, 'fresh': fresh})

            def deviates(stored, fresh):
                """Weicht die gespeicherte Form von der des frischen Imports ab? Zwei Spiegel
                sind sich einig – ob ihr Betrag auf dem Konto oder dem Gegenkonto steht, sagt die
                Form selbst."""
                return stored != fresh and not (stored in MIRROR_FORMS and fresh in MIRROR_FORMS)

            def in_books(booking, year, link):
                """Der AfA-Satz zum Planjahr ``year`` steht in den Büchern: Der Lauf merkt es sich (für
                die neuen Planzeilen), und ein geplantes Jahr der App wird sofort ``booked`` – eines, das
                die App selbst gebucht hat, bleibt. Kein AfA-Satz (``year`` leer): nichts."""
                if year is None:
                    return
                booked_years.add((booking.inventory_id, year))
                if link is not None and link.asset_id is not None:
                    cursor.execute(_PLAN_BOOKED_SQL, (link.asset_id, year))
                    if cursor.rowcount > 0:
                        tx.log.append((_PLAN_BOOKED_SQL, (link.asset_id, year),
                                       'WISO-Datenbank-Import: AfA-Planjahr als gebucht gesetzt'))

            def recognize(booking, coa, counter_coa, amount, text):
                """Gleicht eine Zeile der bekannten Gruppe dem Satz genau (Datum, Konto, Gegenkonto, |Betrag|) –
                oder, bei einem Unterkonto, dem Satz, den ein Import bis 2.2.4 auf das Basiskonto geschrieben hat?
                Die Zeile bleibt, wie sie ist, und wird verbraucht (jede Zeile gehört höchstens einem Satz); ein
                Spiegel des Bestands wird gezählt, eine abweichende Form nur gemeldet."""
                pool = group_rows(booking.group)
                same = [row for row in pool
                        if row[1] == booking.date and row[2] == coa and row[3] == counter_coa
                        and abs(row[4]) == abs(amount)]
                row = next((r for r in same if r[4] == amount), same[0] if same else None)
                if row is not None:
                    pool.remove(row)
                    row_id, _date, _coa, _counter, row_amount, stored, row_text = row
                    if stored == FORM_MIRROR_LEGACY and booking.form == FORM_MIRROR_COA:
                        result['legacy_mirrors'] += 1
                    elif stored and deviates(stored, booking.form):
                        note_conflict(row_id, booking.date, row_amount, row_text, stored, booking.form)
                    return True
                if booking.legacy_account is not None or booking.legacy_counter_account is not None:
                    # Ein Import bis 2.2.4 hat den Satz auf das Basiskonto geschrieben statt auf das
                    # Unterkonto: Die Zeile bleibt, wie sie ist, und wird nur gezählt.
                    old = _row_on_base_account(pool, booking, coa_map, coa, counter_coa, amount, text)
                    if old is not None:
                        pool.remove(old)
                        result['subaccount_rows'] += 1
                        return True
                return False

            def year_row(booking, year):
                """Nachweis für einen AfA-Satz, dem keine Zeile seiner bekannten Gruppe genau gleicht: Hat die
                Gruppe noch eine freie Zeile mit einem Datum im Planjahr ``year`` (gleich welcher Betrag, welcher
                Tag, welche Konten), hat der Nutzer den Satz geändert, und das Jahr steht in den Büchern. Die Zeile
                wird verbraucht – sie dient höchstens einem AfA-Satz als Nachweis. Ohne Planjahr (kein AfA-Satz)
                gibt es keinen."""
                if year is None:
                    return False
                pool = group_rows(booking.group)
                row = next((r for r in pool if str(r[1] or '')[:4] == str(year)), None)
                if row is None:
                    return False
                pool.remove(row)
                return True

            # Re-Import: Erst werden die genauen Treffer ALLER Sätze aller bekannten Gruppen vergeben, erst danach
            # dienen die übrigen Zeilen als Nachweis für einen geänderten AfA-Satz (``year_row``). Sonst nähme der
            # Satz, dessen Zeile der Nutzer gelöscht hat, die Zeile eines anderen Satzes derselben Gruppe und
            # desselben Jahres – und dieser stünde dann ohne Zeile da.
            exact = {}
            for index, (booking, coa, counter_coa, amount, _number, text, _key) in enumerate(prepared):
                if booking.group and booking.group in known_groups:
                    exact[index] = recognize(booking, coa, counter_coa, amount, text)

            for index, (booking, coa, counter_coa, amount, number, text, key) in enumerate(prepared):
                # AfA-Satz: sein Planjahr und – wenn die Anlage am Abgleich teilnimmt – die Anlage der App
                # und ihre Planzeile des Jahres (Status, von der App gebucht)
                year = booking.depreciation_year if booking.is_depreciation else None
                participates = year is not None and booking.inventory_id not in excluded
                link = (links.get(booking.inventory_id) or _NO_ASSET) if participates else None
                plan = link.years.get(year) if participates else None
                known = index in exact
                if known and exact[index]:
                    # Re-Import: Die Zeile des Satzes steht schon, wie sie ist – neu angelegt wird nichts.
                    in_books(booking, year, link)
                    result['skipped'] += 1
                    if booking.future_depreciation:
                        result['future_depreciations_kept'] += 1
                    continue
                if booking.future_depreciation:
                    # Ein künftiger AfA-Satz kommt nie in die Bücher (gebucht wird im Jahr selbst in der App):
                    # weder neu noch aus einer CSV-Zeile, kein Nachtrag, kein Konflikt mit der App. Was ein
                    # früherer Import schon geschrieben hat, bleibt – auch geändert (eine Zeile der Gruppe im
                    # Planjahr genügt).
                    if known and year_row(booking, year):
                        in_books(booking, year, link)
                        result['skipped'] += 1
                        result['future_depreciations_kept'] += 1
                    else:
                        result['future_depreciations_skipped'] += 1
                    continue
                if participates and plan is not None and plan[1]:
                    # Die App hat die AfA dieses Jahres selbst gebucht: Der Satz der Datenbank käme
                    # als zweiter dazu.
                    depreciation_cases.setdefault((booking.inventory_id, year),
                                                  {'anlage': link.name, 'jahr': year})
                    continue
                if known:
                    # Keine Zeile der Gruppe passt genau. Ein AfA-Satz mit einer Zeile der Gruppe im Planjahr ist
                    # vom Nutzer geändert: Das Jahr steht in den Büchern, nichts wird angelegt.
                    if year_row(booking, year):
                        in_books(booking, year, link)
                        result['skipped'] += 1
                        continue
                    # Sonst bleibt der Satz weg (der Nutzer kann die Buchung gelöscht haben). Nachgetragen wird
                    # nur der AfA-Satz eines geplanten Jahres, dessen Planzeile es in der App gibt – ein früherer
                    # Lauf hat ihn als künftig übersprungen, die Gruppe trägt mehrere Jahre. Ein von der App
                    # gebuchtes Jahr ist oben schon abgehandelt.
                    if not (participates and plan is not None and plan[0] == 'planned'):
                        result['skipped'] += 1
                        continue
                if booking.account is not None and coa is None:
                    result['unresolved_accounts'] += 1
                if booking.counter_account is not None and counter_coa is None:
                    result['unresolved_accounts'] += 1

                searched = [(booking.date, amount, coa)]
                loaded, free = csv_rows(searched[0])
                match = _matching_row([row for row in free if row.doc == number],
                                      counter_coa, text, locked=key in ambiguous)
                found_without_account = False
                if (match is None and coa is not None and number not in loaded
                        and (booking.date, amount, number, text, counter_coa)
                        not in ambiguous_accounts):
                    searched.append((booking.date, amount, None))
                    free = csv_rows(searched[1])[1]
                    match = _matching_row([row for row in free if row.doc == number],
                                          counter_coa, text, fallback=True)
                    found_without_account = match is not None
                flipped = False
                if (match is None and booking.form == FORM_MIRROR_COA and coa is not None
                        and counter_coa is not None
                        and (booking.date, -amount, coa, number, text) not in counters):
                    # Ein Spiegel des Bestands mit umgekehrtem Vorzeichen: gleiche Buchung, der
                    # Betrag steht auf dem Gegenkonto. Das Gegenkonto muss gleich sein (locked).
                    free = csv_rows((booking.date, -amount, coa))[1]
                    match = _matching_row(
                        [row for row in free
                         if row.doc == number and row.form == FORM_MIRROR_LEGACY],
                        counter_coa, text, locked=True)
                    flipped = match is not None
                legacy_stock = False
                if (match is None and booking.legacy_amount is not None
                        and coa is not None and counter_coa is not None):
                    # Berührt der Satz ein Konto 1000–1099, hat eine ältere Version die CSV-Zeile mit
                    # dem Betrag ihrer Regel gespeichert. Sonst dieselben Schutzregeln wie oben; eine
                    # Zeile, die ein anderer Satz der Datenbank mit diesem Betrag beansprucht, ist keine.
                    legacy_minor = to_minor(booking.legacy_amount)
                    legacy_key = (booking.date, legacy_minor, coa, number, text)
                    if legacy_minor != amount and legacy_key not in counters:
                        free = csv_rows(legacy_key[:3])[1]
                        match = _matching_row([row for row in free if row.doc == number],
                                              counter_coa, text, locked=legacy_key in legacy_ambiguous)
                        legacy_stock = match is not None
                if match is not None:
                    free.remove(match)
                    stored = match.form
                    form_value = None
                    params_fill = (coa, counter_coa, booking.tax_rate,
                                   self._minor_opt(booking.tax_amount))
                    if legacy_stock:
                        # Betrag und Form der älteren Version bleiben, und die Übernahme füllt nichts
                        # (Konten, Steuer): der Steuerbetrag der Datenbank trüge das Vorzeichen des
                        # neuen Betrags, und neue Konten verlangten eine Form, die zu ihm passt.
                        result['legacy_stock_rows'] += 1
                        params_fill = (None, None, None, None)
                    elif not stored or found_without_account or (
                            match.counter is None and counter_coa is not None):
                        # Zeile ohne Form, oder die Übernahme füllt ein fehlendes Konto: Die Konten
                        # des Satzes ändern sich, also gilt die Form des Imports für sie
                        form_value = booking.form
                    elif stored == FORM_MIRROR_LEGACY and booking.form == FORM_MIRROR_COA:
                        result['legacy_mirrors'] += 1        # bleibt, wie sie ist
                    elif deviates(stored, booking.form):
                        note_conflict(match.id, booking.date, -amount if flipped else amount,
                                      match.shown, stored, booking.form)
                    params = (booking.group or None, *params_fill,
                              recipients.get(booking.invoice_id) or None, form_value, match.id)
                    cursor.execute(adopt_sql, params)
                    tx.log.append((adopt_sql, params, 'WISO-Datenbank-Import: '
                                                      'Buchung aus CSV-Import übernommen'))
                    result['adopted'] += 1
                    if len(result['adopted_rows']) < MAX_ADOPTED_ROWS:
                        result['adopted_rows'].append(
                            {'id': match.id, 'date': booking.date,
                             'amount': booking.amount or 0.0, 'text': booking.text})
                    in_books(booking, year, link)
                    continue

                cursor.execute('''
                    INSERT INTO Bookings
                        (DateBooking, COA_ID, CounterCOA_ID, Amount, TaxRate,
                         TaxAmount, Text, DocumentNumber, BookingType, SourceGroup,
                         RecipientClient, EntryForm)
                    VALUES (?,?,?,?,?,?,?,?,'entry',?,?,?)
                ''', (booking.date, coa, counter_coa, amount, booking.tax_rate,
                      self._minor_opt(booking.tax_amount), booking.text,
                      booking.document_number or None, booking.group or None,
                      recipients.get(booking.invoice_id) or None, booking.form or None))
                result['imported'] += 1
                created_for.update(searched)
                in_books(booking, year, link)

            # Nicht übernommene Kandidaten zu Schlüsseln, für die dieser Lauf einen
            # Satz neu angelegt hat: dieselbe Buchung zweimal? Das entscheidet der Nutzer.
            leftovers = {row.id: (key, row) for key in created_for for row in pools[key][1]}
            result['possible_duplicates_count'] = len(leftovers)
            for row_id in sorted(leftovers)[:MAX_ADOPTED_ROWS]:
                (date, amount, _coa), row = leftovers[row_id]
                result['possible_duplicates'].append(
                    {'id': row_id, 'date': date,
                     'amount': float(from_minor(amount)), 'text': row.shown})

            result['depreciation_conflicts'] = len(depreciation_cases)
            result['depreciation_conflict_rows'] = sorted(
                depreciation_cases.values(),
                key=lambda case: (case['anlage'], case['jahr']))[:MAX_CONFLICT_ROWS]
        return booked_years

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

        Der Betrag ist der des Buchungssatzes, wie der Import ihn gespeichert hat: Bei genau
        einer liquiden Seite trägt er die Richtung der Geldbewegung (``WisoDatabase._signed``).
        WISO schreibt eine Rückzahlung an den Kunden mit vertauschten Konten (Debitor an Geld);
        nach dem Import trägt sie das umgekehrte Vorzeichen der Zahlung.
        ``link_invoice_to_transaction`` ordnet jedes Paar Rechnung–Bewegung höchstens einmal zu
        und meldet Doppeltes in ``payment_warnings``. Ob eine Gutschrift so vorzeichenrichtig
        zugeordnet wird, ist an echten Daten nicht geprüft.
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

    def _insert_wiso_assets(self, assets, result, booked_years=frozenset()):
        """Anlagegüter samt AfA-Plan übernehmen.

        Die Inventarnummer ist der Schlüssel: ein zweiter Lauf aktualisiert
        das vorhandene Anlagegut, statt es zu verdoppeln. Dabei behält eine vorhandene Anlage ihr
        Anlagekonto (``COA_ID``): Ein Re-Import ändert es nicht, auch wenn der Leser inzwischen eine
        andere SKR04-Nummer nennt.

        Der AfA-Plan: WISO plant alle Jahre sofort und schreibt die AfA-Sätze gleich mit; Sätze nach dem
        Stichtag legt der Import nie an (:meth:`import_wiso_fdb`). **Der Status eines Planjahrs folgt der
        Buchung, nicht dem Datum:** ``booked``, wenn der AfA-Satz der Anlage für dieses Jahr in den
        Büchern steht (``booked_years``, aus :meth:`_insert_wiso_bookings`: ``{(INVENTORYID,
        Jahr)}`` – auch ein künftiger Satz aus einem früheren Import zählt), sonst ``planned`` – dann
        bucht die App das Jahr selbst (``book_depreciation``).
        Gibt es das Jahr schon, werden Betrag und Buchwert aktualisiert, und der Status geht nur von
        ``planned`` auf ``booked``, wenn der Satz jetzt in den Büchern steht; sonst bleibt er, wie er
        ist (auch ein ``booked`` aus einem Import bis 2.2.4). **Hat die App das Jahr selbst gebucht**
        (``AssetDepreciations.Booking_ID`` zeigt auf eine vorhandene Buchung), bleibt die Zeile ganz
        unverändert. ``result['depreciations']`` zählt alle Planjahre dieses Laufs,
        ``depreciations_planned`` die, die danach ``planned`` sind (und nicht von der App gebucht).

        Danach prüft :meth:`_check_depreciation_plans`, ob die App die geplanten Jahre so plant wie WISO.
        """
        conn = self._get_connection()
        cursor = conn.cursor()
        coa_map = self._coa_map(cursor)
        checks = []

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
                # gepflegten Wert nicht wieder ausloeschen. Das Anlagekonto bleibt ebenso.
                cursor.execute('''
                    UPDATE Assets SET Name=?, COA_ID=COALESCE(COA_ID, ?), PurchaseDate=?,
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

            planned = []
            for entry in asset.depreciations:
                if entry.year is None:
                    continue
                amounts = (to_minor(entry.amount or 0), to_minor(entry.book_value or 0))
                status = 'booked' if (asset.source_id, entry.year) in booked_years else 'planned'
                cursor.execute(f'''
                    INSERT INTO AssetDepreciations
                        (Asset_ID, Year, DepreciationAmount, BookValue, Status)
                    VALUES (?,?,?,?,?)
                    ON CONFLICT(Asset_ID, Year) DO UPDATE SET
                        DepreciationAmount=excluded.DepreciationAmount,
                        BookValue=excluded.BookValue,
                        Status=CASE WHEN excluded.Status = 'booked' AND Status = 'planned'
                                    THEN 'booked' ELSE Status END
                    WHERE NOT {_APP_BOOKED_SQL}
                ''', (asset_id, entry.year) + amounts + (status,))
                result['depreciations'] += 1
                state = cursor.execute(
                    f'SELECT Status, {_APP_BOOKED_SQL} FROM AssetDepreciations '
                    'WHERE Asset_ID = ? AND Year = ?', (asset_id, entry.year)).fetchone()
                if state[0] == 'planned' and not state[1]:
                    result['depreciations_planned'] += 1
                    planned.append((entry.year, amounts[0]))
            if planned:
                checks.append((asset_id, asset, planned))

        conn.commit()
        conn.close()
        self._check_depreciation_plans(checks, result)

    def _check_depreciation_plans(self, checks, result):
        """Warnt, wenn die App die geplanten Jahre einer Anlage anders plant als WISO.

        ``book_depreciation`` rechnet mit dem eigenen Plan (``calculate_depreciation_plan``: linear,
        degressiv, GWG) und überschreibt den Betrag der Planzeile – ein WISO-Plan, der anders rechnet
        (anderer Satz, andere Aufteilung), liefe beim Buchen auseinander. Je Anlage mit einem
        geplanten Jahr (``checks``: Anlage der App, WISO-Anlage und ihre geplanten Jahre mit dem
        WISO-Betrag) wird der Plan der App so berechnet wie beim Buchen und verglichen. Weicht ein Jahr
        um mehr als einen Cent ab oder plant die App dafür gar keine AfA, kommt **eine** Warnung in
        ``asset_warnings`` (das erste abweichende Jahr) und zählt in
        ``depreciation_plan_mismatches``. Gebucht wird nichts.
        """
        for asset_id, asset, planned in checks:
            row = self.get_asset_by_id(asset_id)
            plan = self.calculate_depreciation_plan(row[7], row[6], row[8], row[9])
            for year, wiso_minor in planned:
                entry = next((e for e in plan if e['year'] == year), None)
                app_minor = to_minor(entry['depreciation']) if entry else None
                if app_minor is not None and abs(app_minor - wiso_minor) <= FACTOR // 100:
                    continue
                app = 'keine AfA' if app_minor is None else f'{format_de(app_minor)} €'
                result['asset_warnings'].append(
                    f'{_asset_name(asset)}: Die App plant die künftige AfA anders als WISO '
                    f'({year}: App {app}, WISO {format_de(wiso_minor)} €). '
                    'Beim Buchen in der App gilt der Plan der App – bitte prüfen.')
                result['depreciation_plan_mismatches'] += 1
                break
