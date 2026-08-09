# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Import direkt aus der WISO-Mein-Büro-Datenbank.

WISO speichert seine Daten in Firebird-Dateien (``DB1.FDB`` = Mandant 1,
``DB0.FDB`` = mitgelieferter Standard-Kontenrahmen). Sie sind unverschlüsselt
und werden hier ohne Firebird-Installation gelesen – siehe :mod:`.ods`.

Warum überhaupt, wo es doch Textexporte gibt? Weil die Exporte Wesentliches
verlieren:

* **``ACCOUNTINGID``** – die Klammer, die zusammengehörige Teilbuchungen
  verbindet. Ohne sie muss die Zugehörigkeit über Belegnummer und Datum
  geraten werden, und das geht bei gleichbetragigen Buchungen desselben Tages
  (Anlagenverkäufe!) regelmäßig daneben.
* **Anlagenverwaltung** – ``BAS_INVENTORY`` samt AfA-Plan und den
  Teilüberweisungen eines Anlagenkaufs; über die Oberfläche nicht exportierbar.
* **Vollständigkeit** – die Exporte umfassen nur ausgewählte Jahre.

Zwei Eigenheiten der Datenbank, die dieses Modul kapselt:

1. **Kontonummern sind SKR03.** ``BAS_FINACC_PLAN`` bildet sie in der Spalte
   ``SKR04`` auf den hier verwendeten Rahmen ab (1210 → 1810 Bank,
   1360 → 1460 Verrechnungskonto, 8405 → 4405 „Erlöse 19 % noch offen“).
2. **Steuerzeilen sind eigene Sätze.** WISO bucht DATEV-artig: eine Zeile
   trägt den Bruttobetrag, eine zweite die Umsatz- bzw. Vorsteuer. Sätze ohne
   ``AMOUNTGROSS`` sind genau diese Steuerzeilen; uBuchhaltung führt die Steuer
   am Buchungssatz selbst und lässt sie deshalb weg.
"""
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .catalog import Catalog
from .ods import OdsError, OdsFile
from .rtf import rtf_to_text

__all__ = [
    'Catalog', 'OdsError', 'OdsFile', 'WisoAccount', 'WisoAsset',
    'WisoBooking', 'WisoData', 'WisoDatabase', 'WisoDepreciation',
    'WisoPayment', 'DEFAULT_LIQUID_ACCOUNTS', 'read_wiso_database',
]

#: Buchungen, Kontenrahmen und Anlagen – die Tabellen, die dieses Modul liest.
T_BOOKINGS = 'MOV_FINACC_ACCRECORDS'
T_CHART = 'BAS_FINACC_PLAN'
T_ASSETS = 'BAS_INVENTORY'
T_DEPRECIATIONS = 'MOV_INVENTORY_AMORTIZATIONS'
T_ASSET_PAYMENTS = 'MOV_INVENTORY_BOOKINGS'
#: Selbst angelegte Unterkonten (z. B. „Kfz-Versicherung <Fahrzeug>“). Sie
#: stehen nicht im Kontenrahmen, nennen aber ihr Basiskonto.
T_SUBACCOUNTS = 'FINT_ACCOUNTS'
#: Rechnungen, Kunden, eigene Firma. Der Positionstext steht nicht bei der
#: Rechnungsposition, sondern bei der zugehörigen Auftragsposition.
T_INVOICES = 'MOV_INVOICES'
T_INVOICE_POSITIONS = 'MOV_INVOICES_POSITIONS'
T_ORDER_POSITIONS = 'MOV_ORDERS_POSITIONS'
T_CUSTOMERS = 'BAS_CUSTOMERS'
T_COMPANY = 'BAS_COMPANY'
T_UNITS = 'SUP_ARTICLES_UNITS'

#: Personenkonten (Debitoren/Kreditoren) stehen nicht im Sachkontenrahmen und
#: tragen in SKR03 wie SKR04 dieselbe Nummer.
FIRST_PERSONAL_ACCOUNT = 10000

#: Liquide Konten ohne Rückfrage: Kasse und Verrechnungskonto (SKR04).
#: Die eigenen Bankkonten kommen aus der Anwendung dazu.
DEFAULT_LIQUID_ACCOUNTS = frozenset(range(1000, 1100)) | {1460}

#: SKR03 → (SKR04, Bezeichnung) für Konten, die ``BAS_FINACC_PLAN`` nicht führt.
#:
#: WISOs Umschlüsselungstabelle deckt nicht jedes Konto ab – vor allem
#: Bilanzkonten fehlen dort. Diese Entsprechungen sind **fachlich geprüft**
#: nachgetragen, nicht abgeleitet. Wer hier ergänzt, prüft die Entsprechung
#: vorher: eine falsche SKR-Nummer verschiebt Beträge lautlos in die falsche
#: Zeile der Auswertung.
KNOWN_EQUIVALENTS = {
    640: (3160, 'Verbindlichkeiten gegenüber Kreditinstituten '
                '(Restlaufzeit 1 bis 5 Jahre)'),
    986: (1940, 'Damnum/Disagio (aktiver Rechnungsabgrenzungsposten)'),
    2150: (6880, 'Aufwendungen aus der Währungsumrechnung'),
}

#: WISO bricht Verwendungszwecke hart um; für die Anzeige wird daraus eine Zeile.
_WHITESPACE = re.compile(r'\s+')

#: Kennt WISO einen Kunden nur als Sammelposten („(alle)“, „(diverse Kunden)“),
#: trägt er eine nicht-positive Id. Solche Einträge sind keine Kunden.
FIRST_REAL_CUSTOMER_ID = 1

#: Steht eines dieser Kürzel im Namen, ist der Kunde eine Firma – auch wenn
#: daneben ein Vorname erfasst ist (Ansprechpartner).
LEGAL_FORMS = (' GmbH', ' AG', ' KG', ' UG', ' mbH', ' OHG', ' GbR', ' SE',
               ' e.K', ' e.V', ' Ltd', ' Inc', ' & Co', ' PartG', ' gGmbH')

#: ``PAYSTATE`` aus ``SYS_PAYSTATES`` auf den Status von uBuchhaltung.
PAY_STATES = {10: 'sent', 20: 'partial_payment', 30: 'paid', 40: 'sent'}

#: Zwei Buchstaben statt WISOs Kfz-Kennzeichen-Kürzeln.
COUNTRIES = {'D': 'DE', 'A': 'AT', 'CH': 'CH', 'F': 'FR', 'I': 'IT',
             'NL': 'NL', 'B': 'BE', 'L': 'LU', 'E': 'ES', 'PL': 'PL'}

#: Fällt die Einheit nicht zuzuordnen, gilt „Stück“ (UN/ECE C62).
DEFAULT_UNIT = 'C62'


def _clean(text) -> str:
    """Auf eine Zeile bringen. Nicht aufgelöste Blobs gelten als leer."""
    if not isinstance(text, str):
        return ''
    return _WHITESPACE.sub(' ', text.replace('\x00', '')).strip()


def _date(value: Optional[str]) -> Optional[str]:
    """``YYYY-MM-DD hh:mm:ss`` bzw. ``YYYY-MM-DD`` auf das Datum kürzen."""
    return value[:10] if value else None


def _country(value: Optional[str]) -> str:
    """WISOs Länderkürzel (Kfz-Kennzeichen) auf ISO 3166-1 alpha-2."""
    code = _clean(value).upper()
    return COUNTRIES.get(code, code if len(code) == 2 else 'DE')


def _after_disposal_year(date: Optional[str], sale_date: Optional[str]) -> bool:
    """Liegt ``date`` nach dem Jahr, in dem das Anlagegut abgegangen ist?

    Im Abgangsjahr selbst steht die zeitanteilige AfA – die ist echt. Was WISO
    danach noch plant, ist der Erinnerungswert (0,01 €) und gehört nicht in die
    Bücher.
    """
    return bool(date and sale_date and date[:4] > sale_date[:4])


@dataclass
class WisoAccount:
    """Ein Konto des WISO-Kontenrahmens."""

    skr03: int
    skr04: Optional[int] = None
    text: str = ''
    year: Optional[int] = None


@dataclass
class WisoBooking:
    """Ein Buchungssatz, bereits auf SKR04 umgeschlüsselt."""

    date: Optional[str] = None
    amount: float = 0.0
    account: Optional[int] = None
    counter_account: Optional[int] = None
    tax_rate: Optional[float] = None
    tax_amount: Optional[float] = None
    text: str = ''
    document_number: str = ''
    #: ``ACCOUNTINGID`` – die Split-Klammer; gleiche Gruppe = ein Vorgang.
    group: str = ''
    invoice_id: Optional[int] = None
    payment_id: Optional[int] = None
    inventory_id: Optional[int] = None
    source_id: Optional[int] = None
    #: SKR03-Nummern, die sich nicht umschlüsseln ließen.
    unmapped: List[int] = field(default_factory=list)


@dataclass
class WisoDepreciation:
    """Eine AfA-Zeile eines Anlageguts."""

    year: Optional[int] = None
    date: Optional[str] = None
    amount: float = 0.0
    cumulated: float = 0.0
    #: Restbuchwert, gerechnet als Anschaffungswert minus kumulierte AfA.
    book_value: Optional[float] = None


@dataclass
class WisoPayment:
    """Zahlung auf ein Anlagegut.

    Ein Kauf kann sich über mehrere Teilüberweisungen und mehrere Tage
    ziehen; der Abgang steht als **negative** Zeile in derselben Tabelle.
    """

    date: Optional[str] = None
    amount: float = 0.0
    text: str = ''


@dataclass
class WisoAsset:
    """Ein Anlagegut samt AfA-Plan und Kaufzahlungen."""

    number: Optional[int] = None
    label: str = ''
    purchase_date: Optional[str] = None
    #: Anschaffungswert **netto** – Summe der Teilzahlungen.
    purchase_price: float = 0.0
    sale_date: Optional[str] = None
    #: Verkaufserlös **netto**; None, wenn er sich nicht zuordnen ließ.
    sale_price: Optional[float] = None
    #: Restbuchwert im Zeitpunkt des Abgangs (das, was ausgebucht wurde).
    residual_value: Optional[float] = None
    useful_life_years: Optional[int] = None
    account: Optional[int] = None
    depreciation_account: Optional[int] = None
    source_id: Optional[int] = None
    depreciations: List[WisoDepreciation] = field(default_factory=list)
    payments: List[WisoPayment] = field(default_factory=list)
    #: Auffälligkeiten im Quellbestand – Klartext, zum Nachsehen in WISO.
    warnings: List[str] = field(default_factory=list)


@dataclass
class WisoCompany:
    """Die eigene Firma – der Absender auf jeder Rechnung."""

    name: str = ''
    company: str = ''
    street: str = ''
    postal_code: str = ''
    city: str = ''
    country: str = 'DE'
    vat_id: str = ''
    email: str = ''
    phone: str = ''


@dataclass
class WisoCustomer:
    """Ein Kunde mit seiner WISO-Kundennummer."""

    number: Optional[int] = None
    entity_type: str = 'company'
    display_name: str = ''
    company_name: str = ''
    first_name: str = ''
    last_name: str = ''
    address_line1: str = ''
    street: str = ''
    postal_code: str = ''
    city: str = ''
    country: str = 'DE'
    email: str = ''
    phone: str = ''
    vat_id: str = ''
    notes: str = ''
    source_id: Optional[int] = None


@dataclass
class WisoInvoiceItem:
    """Eine Rechnungsposition.

    Menge und Summe stammen von der Rechnungsposition, Text und Einzelpreis
    von der zugehörigen Auftragsposition – dort steht der Text, den der
    Nutzer für diesen Beleg angepasst hat.
    """

    position: int = 1
    description: str = ''
    quantity: float = 0.0
    unit: str = DEFAULT_UNIT
    price_per_unit: float = 0.0
    total_net: float = 0.0
    tax_rate: Optional[float] = None
    article_number: str = ''


@dataclass
class WisoInvoice:
    """Eine Ausgangsrechnung samt Positionen."""

    number: str = ''
    date: Optional[str] = None
    #: Kundennummer – aufgelöst über die interne Id, die die Rechnung nennt.
    customer_number: Optional[int] = None
    customer_source_id: Optional[int] = None
    buyer_name: str = ''
    buyer_company: str = ''
    buyer_street: str = ''
    buyer_postal_code: str = ''
    buyer_city: str = ''
    buyer_country: str = 'DE'
    delivery_date: Optional[str] = None
    payment_days: Optional[int] = None
    tax_rate: float = 0.0
    sum_net: float = 0.0
    tax_amount: float = 0.0
    sum_gross: float = 0.0
    status: str = 'paid'
    intro_text: str = ''
    closing_text: str = ''
    items: List[WisoInvoiceItem] = field(default_factory=list)
    source_id: Optional[int] = None
    warnings: List[str] = field(default_factory=list)


@dataclass
class WisoData:
    """Das Ergebnis eines Lesevorgangs."""

    bookings: List[WisoBooking] = field(default_factory=list)
    assets: List[WisoAsset] = field(default_factory=list)
    customers: List[WisoCustomer] = field(default_factory=list)
    invoices: List[WisoInvoice] = field(default_factory=list)
    company: Optional[WisoCompany] = None
    chart: Dict[int, WisoAccount] = field(default_factory=dict)
    #: SKR03-Nummer → Anzahl Verwendungen, für die es keine SKR04-Zuordnung gibt.
    unmapped_accounts: Dict[int, int] = field(default_factory=dict)
    #: Dazu die WISO-Bezeichnung, damit das Konto von Hand anlegbar ist.
    unmapped_labels: Dict[int, str] = field(default_factory=dict)
    #: Übersprungene Steuerzeilen (WISOs DATEV-Darstellung).
    tax_rows_skipped: int = 0
    #: Übersprungene Erinnerungswert-Buchungen abgegangener Anlagen.
    memo_rows_skipped: int = 0


class WisoDatabase:
    """Fachlicher Zugriff auf eine WISO-Mandantendatenbank.

    >>> with WisoDatabase('DB1.FDB', 'DB0.FDB') as wiso:   # doctest: +SKIP
    ...     data = wiso.read()
    """

    def __init__(self, path, standard_chart_path=None):
        self.catalog = Catalog(path)
        self._standard = None
        if standard_chart_path:
            try:
                self._standard = Catalog(standard_chart_path)
            except (OdsError, OSError):
                self._standard = None       # ohne Standardrahmen geht es auch
        self._chart = None

    def close(self):
        self.catalog.close()
        if self._standard:
            self._standard.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    @property
    def tables(self):
        return self.catalog.table_names()

    #: Ohne diese Tabellen und Spalten ist kein Import möglich.
    REQUIRED = {
        T_BOOKINGS: ('ACCOUNTINGID', 'ACCOUNTING_DATE', 'AMOUNTGROSS',
                     'ACCOUNTNO', 'CONTRA_ACCOUNTNO', 'ACCOUNTING_TEXT'),
        T_CHART: ('ID', 'SKR04'),
    }
    #: Schön, wenn vorhanden – ihr Fehlen kostet nur einzelne Angaben.
    OPTIONAL = {
        T_ASSETS: ('ID', 'INVNO', 'LABEL', 'PURCHASEDATE', 'SERVICELIFE'),
        T_DEPRECIATIONS: ('INVENTORYID', 'BOOKINGDATE', 'AMORT_AMOUNT'),
        T_ASSET_PAYMENTS: ('INVENTORYID', 'BOOKINGDATE', 'AMOUNTNET'),
        T_SUBACCOUNTS: ('ACCOUNTNO', 'BASEACCOUNTNO'),
        T_INVOICES: ('ID', 'INVNO', 'INVDATE', 'CUSTID', 'TOTALNET',
                     'TOTALGROSS', 'VAT1PERC', 'PAYSTATE'),
        T_INVOICE_POSITIONS: ('INVID', 'ORDPOSID', 'AMOUNT', 'TOTAL', 'POSID'),
        T_ORDER_POSITIONS: ('ID', 'ARTDESCR', 'PRICENET', 'UNITCODE'),
        T_CUSTOMERS: ('ID', 'CUSTNO', 'NAME1'),
        T_COMPANY: ('NAME1',),
    }

    def columns_of(self, table):
        """Spaltennamen einer Tabelle; leer, wenn es sie nicht gibt."""
        relation = self.catalog.relation_id.get(table)
        if relation is None:
            return set()
        return {column.name for column in
                self.catalog.columns(relation, self.catalog.relation_format[relation])}

    def check(self):
        """Passt diese WISO-Datenbank zum Import?

        Liefert ``(blocker, hinweise)`` – beides Klartext. Der Import ist auf
        den Stand abgestimmt, den WISO Mein Büro heute schreibt; eine ältere
        oder neuere Version kann Tabellen anders benennen. Genau das findet
        diese Prüfung, bevor irgendetwas geschrieben wird.
        """
        blocker, hints = [], []
        for table, columns in self.REQUIRED.items():
            found = self.columns_of(table)
            if not found:
                blocker.append(f'Tabelle {table} fehlt')
                continue
            missing = [c for c in columns if c not in found]
            if missing:
                blocker.append(f'{table}: Spalte(n) {", ".join(missing)} fehlen')
        for table, columns in self.OPTIONAL.items():
            found = self.columns_of(table)
            if not found:
                hints.append(f'Tabelle {table} fehlt – der zugehörige Teil '
                             f'wird übersprungen')
                continue
            missing = [c for c in columns if c not in found]
            if missing:
                hints.append(f'{table}: Spalte(n) {", ".join(missing)} fehlen')
        return blocker, hints

    # ------------------------------------------------------------------
    # Kontenrahmen
    # ------------------------------------------------------------------
    def chart(self) -> Dict[int, WisoAccount]:
        """SKR03-Nummer → Konto. Mandantenrahmen schlägt Standardrahmen.

        ``BAS_FINACC_PLAN`` führt jedes Konto je Buchungsjahr; genommen wird
        der jüngste Eintrag, weil die SKR04-Zuordnung über die Jahre stabil ist.
        """
        if self._chart is not None:
            return self._chart
        chart: Dict[int, WisoAccount] = {}
        for source in (self._standard, self.catalog):
            if source is None or T_CHART not in source.relation_id:
                continue
            for row in source.rows(T_CHART):
                number = row.get('ID')
                if number is None:
                    continue
                year = row.get('BOOKINGYEAR') or 0
                known = chart.get(number)
                if known is not None and (known.year or 0) > year:
                    continue
                chart[number] = WisoAccount(
                    skr03=number, skr04=row.get('SKR04'),
                    text=_clean(row.get('ACCOUNTTEXT')), year=year or None)
        self._resolve_subaccounts(chart)
        self._apply_known_equivalents(chart)
        self._chart = chart
        return chart

    @staticmethod
    def _apply_known_equivalents(chart):
        """Nachgetragene Entsprechungen einsetzen – siehe KNOWN_EQUIVALENTS.

        Nur dort, wo WISO selbst keine SKR04-Nummer nennt; ein gepflegter
        Eintrag im Mandantenrahmen behält immer Vorrang.
        """
        for skr03, (skr04, text) in KNOWN_EQUIVALENTS.items():
            known = chart.get(skr03)
            if known is not None and known.skr04:
                continue
            chart[skr03] = WisoAccount(
                skr03=skr03, skr04=skr04,
                text=(known.text if known and known.text else text))

    def _resolve_subaccounts(self, chart):
        """Selbst angelegte Unterkonten über ihr Basiskonto anschließen.

        WISO erlaubt Unterkonten je Fahrzeug oder Projekt („4532 laufende
        Kfz-Betriebskosten Tesla“ unter „4530“). Sie stehen nur in
        ``FINT_ACCOUNTS`` und nennen dort mit ``BASEACCOUNTNO`` ihr Basiskonto –
        über das sie dieselbe SKR04-Nummer erben. Die Kette wird verfolgt, denn
        ein Basiskonto kann selbst ein Unterkonto sein.
        """
        bases, labels = {}, {}
        for row in self._safe_rows(T_SUBACCOUNTS):
            number = row.get('ACCOUNTNO')
            if number is None:
                continue
            label = _clean(row.get('ACCOUNTLABEL') or row.get('ACCOUNTTEXT'))
            if label:
                labels.setdefault(number, label)
            base = row.get('BASEACCOUNTNO')
            if base:
                bases.setdefault(number, base)
        self.account_labels = labels

        def known(number):
            account = chart.get(number)
            return account is not None and account.skr04

        for number, base in bases.items():
            if known(number):
                continue
            seen = {number}
            while base is not None and base not in seen and not known(base):
                seen.add(base)
                base = bases.get(base)
            if base is not None and known(base):
                chart[number] = WisoAccount(
                    skr03=number, skr04=chart[base].skr04,
                    text=labels.get(number) or chart[base].text)

    def _to_skr04(self, number, missing):
        """SKR03 auf SKR04 abbilden; Personenkonten bleiben, wie sie sind."""
        if number is None:
            return None
        if number >= FIRST_PERSONAL_ACCOUNT:
            return number
        account = self.chart().get(number)
        if account is not None and account.skr04:
            return account.skr04
        missing[number] = missing.get(number, 0) + 1
        return None

    # ------------------------------------------------------------------
    # Buchungen
    # ------------------------------------------------------------------
    def bookings(self, liquid_accounts=None, missing=None,
                 disposed=None) -> List[WisoBooking]:
        """Buchungssätze lesen, umschlüsseln und vorzeichenrichtig stellen.

        ``liquid_accounts`` sind SKR04-Nummern der eigenen Zahlungskonten; sie
        entscheiden über die Richtung des Betrags (Zugang positiv, Abgang
        negativ) – dieselbe Regel wie beim CSV-Import.

        ``disposed`` bildet Anlagegut-Id auf Abgangsdatum ab. Damit fallen die
        AfA-Buchungen des Erinnerungswerts weg, die WISO für ein längst
        verkauftes Anlagegut weiterplant. :meth:`read` füllt das selbst.
        """
        liquid = set(DEFAULT_LIQUID_ACCOUNTS) | set(liquid_accounts or ())
        missing = {} if missing is None else missing
        disposed = disposed or {}
        self._memo_skipped = 0
        out = []
        for row in self.catalog.rows(T_BOOKINGS):
            gross = row.get('AMOUNTGROSS')
            if gross is None:
                continue                     # reine Steuerzeile, siehe Modulkopf
            if _after_disposal_year(_date(row.get('ACCOUNTING_DATE')),
                                    disposed.get(row.get('INVENTORYID'))):
                self._memo_skipped += 1
                continue
            source_account = row.get('ACCOUNTNO')
            source_counter = row.get('CONTRA_ACCOUNTNO')
            account = self._to_skr04(source_account, missing)
            counter = self._to_skr04(source_counter, missing)
            unmapped = [number for number, mapped
                        in ((source_account, account), (source_counter, counter))
                        if number is not None and mapped is None]
            amount = self._signed(gross, account, counter, liquid)
            tax_rate = row.get('TAXRATE')
            tax_rate = tax_rate / 100.0 if tax_rate else None
            net = row.get('AMOUNTNET')
            tax_amount = None
            if tax_rate and net is not None:
                tax_amount = round(abs(gross) - abs(net), 2)
                if amount < 0:
                    tax_amount = -tax_amount
            group = row.get('ACCOUNTINGID')
            out.append(WisoBooking(
                date=_date(row.get('ACCOUNTING_DATE')),
                amount=amount, account=account, counter_account=counter,
                tax_rate=tax_rate, tax_amount=tax_amount,
                text=_clean(row.get('ACCOUNTING_TEXT')),
                document_number=_clean(row.get('REFERENCENO')),
                group=str(group) if group is not None else '',
                invoice_id=row.get('INVID'), payment_id=row.get('PAYMENTID'),
                inventory_id=row.get('INVENTORYID'), source_id=row.get('ID'),
                unmapped=unmapped,
            ))
        return out

    @staticmethod
    def _signed(gross, account, counter, liquid):
        """Vorzeichen aus Sicht des liquiden Kontos.

        Gegenkonto liquide → Geld fließt ab; Konto liquide → Geld kommt an.
        Berührt keine Seite ein Zahlungskonto, bleibt der Betrag, wie er ist.
        """
        amount = float(gross)
        counter_liquid = counter in liquid
        account_liquid = account in liquid
        if counter_liquid and not account_liquid:
            return -abs(amount)
        if account_liquid and not counter_liquid:
            return abs(amount)
        return amount

    # ------------------------------------------------------------------
    # Anlagen
    # ------------------------------------------------------------------
    def assets(self, missing=None) -> List[WisoAsset]:
        """Anlagegüter mit AfA-Plan und den Zahlungen des Kaufs."""
        missing = {} if missing is None else missing
        if T_ASSETS not in self.catalog.relation_id:
            return []
        by_asset_depreciation: Dict[int, List[dict]] = {}
        for row in self._safe_rows(T_DEPRECIATIONS):
            by_asset_depreciation.setdefault(row.get('INVENTORYID'), []).append(row)
        by_asset_payment: Dict[int, List[WisoPayment]] = {}
        by_asset_disposal: Dict[int, dict] = {}
        for row in self._safe_rows(T_ASSET_PAYMENTS):
            payment = WisoPayment(date=_date(row.get('BOOKINGDATE')),
                                  amount=row.get('AMOUNTNET') or 0.0,
                                  text=_clean(row.get('DESCRIPTION')))
            by_asset_payment.setdefault(row.get('INVENTORYID'), []).append(payment)
            # REMOVEACCOUNT ist WISOs eigenes Kennzeichen für die Abgangszeile.
            if row.get('REMOVEACCOUNT'):
                by_asset_disposal[row.get('INVENTORYID')] = {
                    'date': payment.date, 'amount': payment.amount,
                    'text': payment.text}

        proceeds = self._disposal_proceeds(by_asset_disposal.values())
        out = []
        for row in self.catalog.rows(T_ASSETS):
            asset_id = row.get('ID')
            payments = sorted(by_asset_payment.get(asset_id, []),
                              key=lambda p: p.date or '')
            price = self._purchase_price(row.get('PURCHASEAMOUNTNET'), payments)
            disposal = by_asset_disposal.get(asset_id)
            sale_date = _date(row.get('SALEDATE'))
            schedule = self._depreciations(
                by_asset_depreciation.get(asset_id, []), payments, sale_date)
            residual, warnings = self._residual_value(schedule, disposal, price)
            out.append(WisoAsset(
                number=row.get('INVNO'), label=_clean(row.get('LABEL')),
                purchase_date=_date(row.get('PURCHASEDATE')),
                purchase_price=price,
                sale_date=sale_date,
                sale_price=proceeds.get((disposal or {}).get('text'))
                if disposal else None,
                residual_value=residual, warnings=warnings,
                useful_life_years=row.get('SERVICELIFE'),
                account=self._to_skr04(row.get('FINACIALACCOUNT'), missing),
                depreciation_account=self._to_skr04(
                    row.get('FINACIALACCOUNT_AFA'), missing),
                source_id=asset_id, payments=payments,
                depreciations=schedule))
        return sorted(out, key=lambda a: (a.purchase_date or '', a.number or 0))

    @staticmethod
    def _residual_value(schedule, disposal, price):
        """Restbuchwert beim Abgang – gerechnet, nicht abgeschrieben.

        Maßgeblich ist der AfA-Plan (Anschaffung minus kumulierte AfA). WISOs
        Abgangszeile wird nur als Gegenprobe genommen: sie kann daneben liegen,
        wenn das Anlagegut in WISO doppelt erfasst wurde – von Hand **und**
        über die Zahlungen. Dann bucht WISO den doppelten Wert aus, während der
        AfA-Plan mit dem einfachen rechnet. Eine Abweichung wird gemeldet, denn
        sie zeigt einen Fehler im Quellbestand an, den nur der Nutzer klären kann.
        """
        warnings = []
        gerechnet = schedule[-1].book_value if schedule else None
        gebucht = abs(disposal['amount']) if disposal else None
        if gerechnet is None:
            return gebucht, warnings
        if gebucht is not None and abs(gebucht - gerechnet) > 0.01:
            warnings.append(
                f'WISO bucht beim Abgang {gebucht:.2f} € aus, der AfA-Plan '
                f'ergibt {gerechnet:.2f} € (Anschaffung {price:.2f} € minus '
                f'kumulierte AfA). Übernommen wurde der gerechnete Wert.')
        return gerechnet, warnings

    #: Ein Abgangstext muss aussagekräftig sein, um als Schlüssel zu taugen.
    #: „Abschaffung“ allein (so bei Altbeständen) ist es nicht.
    MIN_DISPOSAL_TEXT = 12

    def _disposal_proceeds(self, disposals):
        """Verkaufserlöse (netto) zu den Abgängen suchen.

        WISO verknüpft den Erlös **nicht** mit dem Anlagegut: die Buchungen
        „Umbuchung / Abschaffung Anlagegut …“ tragen keine ``INVENTORYID``.
        Gemeinsam ist ihnen nur der Text, den WISO für den Abgang erzeugt –
        er steht wortgleich in ``MOV_INVENTORY_BOOKINGS.DESCRIPTION``.

        Der Erlös wird deshalb nur übernommen, wenn er eindeutig ist: der Text
        muss aussagekräftig sein, die Buchung auf den Abgangstag fallen und
        alle Treffer denselben Betrag nennen. Sonst bleibt das Feld leer –
        ein falscher Verkaufserlös wäre schlimmer als gar keiner.
        """
        gesucht = {d['text']: d for d in disposals
                   if d.get('text') and len(d['text']) >= self.MIN_DISPOSAL_TEXT}
        if not gesucht:
            return {}
        treffer: Dict[str, List[float]] = {}
        for row in self.catalog.rows(T_BOOKINGS):
            text = _clean(row.get('ACCOUNTING_TEXT'))
            disposal = gesucht.get(text)
            if disposal is None or row.get('AMOUNTGROSS') is None:
                continue
            if _date(row.get('ACCOUNTING_DATE')) != disposal['date']:
                continue
            netto = row.get('AMOUNTNET')
            treffer.setdefault(text, []).append(
                float(netto if netto is not None else row['AMOUNTGROSS']))
        return {text: round(werte[0], 2) for text, werte in treffer.items()
                if max(werte) - min(werte) < 0.01}

    #: WISO trägt ``PURCHASEAMOUNTNET`` nur ein, wenn der Wert von Hand kam;
    #: wird das Anlagegut aus Zahlungen aufgebaut, steht dort dieser Platzhalter.
    PRICE_PLACEHOLDER = 0.01

    @staticmethod
    def _purchase_price(stated, payments):
        """Anschaffungswert = Summe der **positiven** Zahlungen.

        Negative Zeilen in ``MOV_INVENTORY_BOOKINGS`` sind der Abgang, nicht
        Teil der Anschaffung. Für das einzige Anlagegut mit einem echten
        ``PURCHASEAMOUNTNET`` liefern beide Wege denselben Betrag – die Summe
        ist deshalb auch dort maßgeblich und dient als Gegenprobe.
        """
        paid = round(sum(p.amount for p in payments if p.amount > 0), 2)
        if paid > 0:
            return paid
        return stated if stated and stated > WisoDatabase.PRICE_PLACEHOLDER else 0.0

    @staticmethod
    def _depreciations(rows, payments, sale_date=None):
        """AfA-Zeilen mit jahresbezogenem Restbuchwert.

        Im **Abgangsjahr** steht noch die zeitanteilige AfA – die gehört dazu.
        Erst danach plant WISO mit dem Erinnerungswert weiter (0,01 € und
        Korrekturen darauf); diese Zeilen entfallen. Der Schnitt geht deshalb
        nach dem Abgangs*jahr*, nicht nach dem Abgangs*datum*.

        Der Anschaffungswert **wächst**: spätere Teilzahlungen sind
        nachträgliche Anschaffungskosten. Der Restwert eines Jahres ist deshalb
        die Summe der bis dahin geleisteten Zahlungen minus der kumulierten AfA
        – so und nur so trifft er WISOs eigene Werte.

        ``RESIDUALVALUE_AMOUNT`` bleibt ungenutzt: das Feld ist in den Daten
        nicht durchgängig gepflegt.
        """
        out = []
        for row in sorted(rows, key=lambda r: r.get('BOOKINGDATE') or ''):
            date = _date(row.get('BOOKINGDATE'))
            if _after_disposal_year(date, sale_date):
                continue
            year = int(date[:4]) if date else None
            cumulated = row.get('AMORT_CUMULATED_AMOUNT') or 0.0
            base = round(sum(p.amount for p in payments
                             if p.amount > 0
                             and (year is None or (p.date or '')[:4] <= str(year))), 2)
            out.append(WisoDepreciation(
                year=year, date=date,
                amount=row.get('AMORT_AMOUNT') or 0.0, cumulated=cumulated,
                book_value=max(0.0, round(base - cumulated, 2))))
        return out

    def _safe_rows(self, table, blobs=False):
        if table not in self.catalog.relation_id:
            return []
        return self.catalog.rows(table, blobs=blobs)

    # ------------------------------------------------------------------
    # Firma, Kunden, Rechnungen
    # ------------------------------------------------------------------
    def company(self) -> Optional[WisoCompany]:
        """Die eigene Firma. Der Absender steht in WISO nur an einer Stelle."""
        for row in self._safe_rows(T_COMPANY):
            person = ' '.join(x for x in (_clean(row.get('EMPLNAME2')),
                                          _clean(row.get('EMPLNAME1'))) if x)
            return WisoCompany(
                name=person or _clean(row.get('NAME1')),
                company=_clean(row.get('NAME1')),
                street=_clean(row.get('STREET')),
                postal_code=_clean(row.get('ZIPCODE')),
                city=_clean(row.get('CITY')),
                country=_country(row.get('COUNTRY')),
                vat_id=_clean(row.get('VATID')),
                email=_clean(row.get('EMAIL')),
                phone=_clean(row.get('PHONE1')))
        return None

    def customers(self) -> List[WisoCustomer]:
        """Kunden mit ihrer WISO-Kundennummer.

        Sammelposten („(alle)“, „(diverse Kunden)“) tragen eine nicht-positive
        Id und sind keine Kunden – sie entfallen.

        Firma oder Person: WISO unterscheidet das nicht sauber (``GENDER``
        ist ungenutzt, ``CUSTKIND`` meint die Kundenart). Als Person gilt
        deshalb, wer einen Vornamen in ``NAME2`` hat und dessen Name keine
        Rechtsform nennt – sonst ist ``NAME2`` der Ansprechpartner einer Firma.
        """
        out = []
        for row in self._safe_rows(T_CUSTOMERS, blobs=True):
            if (row.get('ID') or 0) < FIRST_REAL_CUSTOMER_ID:
                continue
            name1 = _clean(row.get('NAME1'))
            name2 = _clean(row.get('NAME2'))
            name3 = _clean(row.get('NAME3'))
            is_company = not name2 or any(f.lower() in f' {name1}'.lower()
                                          for f in LEGAL_FORMS)
            out.append(WisoCustomer(
                number=row.get('CUSTNO'), source_id=row.get('ID'),
                entity_type='company' if is_company else 'person',
                display_name=name1 if is_company else f'{name2} {name1}'.strip(),
                company_name=name1 if is_company else '',
                first_name='' if is_company else name2,
                last_name='' if is_company else name1,
                address_line1=(name2 if is_company else '') or name3,
                street=_clean(row.get('STREET')),
                postal_code=_clean(row.get('ZIPCODE')),
                city=_clean(row.get('CITY')),
                country=_country(row.get('COUNTRY')),
                email=_clean(row.get('EMAIL')),
                phone=_clean(row.get('PHONE1')) or _clean(row.get('MOBILE')),
                vat_id=_clean(row.get('VATID')),
                notes=_clean(row.get('NOTES'))))
        return sorted(out, key=lambda c: c.number or 0)

    def _units(self):
        """Einheiten-Id auf den UN/ECE-Code, den XRechnung erwartet."""
        codes = {}
        for row in self._safe_rows(T_UNITS):
            code = _clean(row.get('OPENTRANSCODE'))
            if code:
                codes[row.get('ID') or 0] = code
        return codes

    def _invoice_items(self):
        """Positionen je Rechnung, Text aus der Auftragsposition."""
        units = self._units()
        # blobs=True ist Pflicht: ARTDESCR – der angepasste Positionstext –
        # ist ein Blob und käme sonst als unaufgelöste Kennung zurück.
        orders = {row.get('ID'): row
                  for row in self._safe_rows(T_ORDER_POSITIONS, blobs=True)}
        items: Dict[int, List[WisoInvoiceItem]] = {}
        for row in self._safe_rows(T_INVOICE_POSITIONS):
            order = orders.get(row.get('ORDPOSID')) or {}
            quantity = float(row.get('AMOUNT') or 0)
            total = float(row.get('TOTAL') or 0)
            price = order.get('PRICENET')
            if price is None:
                price = total / quantity if quantity else 0.0
            items.setdefault(row.get('INVID'), []).append(WisoInvoiceItem(
                position=row.get('POSID') or 1,
                description=_clean(order.get('ARTDESCR')) or _clean(
                    order.get('SHORTDESCRIPTION')),
                quantity=quantity, total_net=round(total, 2),
                price_per_unit=round(float(price), 4),
                unit=units.get(order.get('UNITCODE') or 0, DEFAULT_UNIT),
                article_number=_clean(order.get('ARTNO'))))
        for positions in items.values():
            positions.sort(key=lambda p: p.position)
        return items

    def invoices(self, customers=None) -> List[WisoInvoice]:
        """Ausgangsrechnungen samt Positionen.

        Die Rechnung nennt den Kunden über seine interne Id; ``customers``
        liefert die Kundennummer dazu. Ohne sie bleibt das Feld leer – die
        Anschrift steht ohnehin als Momentaufnahme in der Rechnung selbst.
        """
        if T_INVOICES not in self.catalog.relation_id:
            return []
        numbers = {c.source_id: c.number
                   for c in (customers if customers is not None
                             else self.customers())}
        items = self._invoice_items()
        out = []
        for row in self.catalog.rows(T_INVOICES, blobs=True):
            number = _clean(str(row.get('INVNO') or ''))
            if not number:
                continue
            rate = row.get('VAT1PERC')
            positions = items.get(row.get('ID'), [])
            for position in positions:
                position.tax_rate = rate / 100.0 if rate else 0.0
            invoice = WisoInvoice(
                number=number, date=_date(row.get('INVDATE')),
                customer_number=numbers.get(row.get('CUSTID')),
                source_id=row.get('ID'),
                buyer_name=_clean(row.get('NAME1')),
                buyer_company=_clean(row.get('NAME1')),
                buyer_street=_clean(row.get('STREET')),
                buyer_postal_code=_clean(row.get('ZIPCODE')),
                buyer_city=_clean(row.get('CITY')),
                buyer_country=_country(row.get('COUNTRY')),
                delivery_date=_date(row.get('SERVICEDATE'))
                or _date(row.get('DELDATE')),
                payment_days=row.get('PAYDAYS') or None,
                tax_rate=(rate / 100.0) if rate else 0.0,
                sum_net=round(float(row.get('TOTALNET') or 0), 2),
                tax_amount=round(float(row.get('VAT1') or 0), 2),
                sum_gross=round(float(row.get('TOTALGROSS') or 0), 2),
                status=PAY_STATES.get(row.get('PAYSTATE'), 'sent'),
                intro_text=rtf_to_text(row.get('TEXT1')),
                closing_text=rtf_to_text(row.get('TEXT2')),
                items=positions, customer_source_id=row.get('CUSTID'))
            if not positions:
                invoice.warnings.append('ohne Positionen')
            if row.get('CUSTID') and invoice.customer_number is None:
                invoice.warnings.append(
                    'Kunde nicht mehr im Kundenstamm – die Anschrift steht '
                    'aber in der Rechnung selbst')
            summe = round(sum(p.total_net for p in positions), 2)
            if positions and abs(summe - invoice.sum_net) > 0.02:
                invoice.warnings.append(
                    f'Summe der Positionen {summe:.2f} € weicht vom '
                    f'Rechnungsnetto {invoice.sum_net:.2f} € ab')
            out.append(invoice)
        return sorted(out, key=lambda i: (i.date or '', i.number))

    # ------------------------------------------------------------------
    def read(self, liquid_accounts=None) -> WisoData:
        """Alles auf einmal lesen – das, was der Import braucht.

        Reihenfolge mit Absicht: erst die Anlagen, denn ihre Abgangsdaten
        entscheiden, welche AfA-Buchungen noch echt sind.
        """
        missing: Dict[int, int] = {}
        assets = self.assets(missing)
        disposed = {a.source_id: a.sale_date for a in assets if a.sale_date}
        bookings = self.bookings(liquid_accounts, missing, disposed)
        customers = self.customers()
        memo = getattr(self, '_memo_skipped', 0)
        total = self.catalog.count(T_BOOKINGS)
        labels = getattr(self, 'account_labels', {})
        return WisoData(
            bookings=bookings, assets=assets, chart=self.chart(),
            customers=customers, invoices=self.invoices(customers),
            company=self.company(),
            unmapped_accounts=missing, memo_rows_skipped=memo,
            unmapped_labels={n: labels[n] for n in missing if n in labels},
            tax_rows_skipped=total - len(bookings) - memo)


def read_wiso_database(path, standard_chart_path=None,
                       liquid_accounts=None) -> WisoData:
    """Bequemer Einzeiler für den Regelfall."""
    with WisoDatabase(path, standard_chart_path) as wiso:
        return wiso.read(liquid_accounts)
