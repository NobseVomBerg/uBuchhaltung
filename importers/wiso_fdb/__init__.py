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

#: Personenkonten (Debitoren/Kreditoren) stehen nicht im Sachkontenrahmen und
#: tragen in SKR03 wie SKR04 dieselbe Nummer.
FIRST_PERSONAL_ACCOUNT = 10000

#: Liquide Konten ohne Rückfrage: Kasse und Verrechnungskonto (SKR04).
#: Die eigenen Bankkonten kommen aus der Anwendung dazu.
DEFAULT_LIQUID_ACCOUNTS = frozenset(range(1000, 1100)) | {1460}

#: WISO bricht Verwendungszwecke hart um; für die Anzeige wird daraus eine Zeile.
_WHITESPACE = re.compile(r'\s+')


def _clean(text: Optional[str]) -> str:
    return _WHITESPACE.sub(' ', (text or '').replace('\x00', '')).strip()


def _date(value: Optional[str]) -> Optional[str]:
    """``YYYY-MM-DD hh:mm:ss`` bzw. ``YYYY-MM-DD`` auf das Datum kürzen."""
    return value[:10] if value else None


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
    purchase_price: float = 0.0
    sale_date: Optional[str] = None
    useful_life_years: Optional[int] = None
    account: Optional[int] = None
    depreciation_account: Optional[int] = None
    source_id: Optional[int] = None
    depreciations: List[WisoDepreciation] = field(default_factory=list)
    payments: List[WisoPayment] = field(default_factory=list)


@dataclass
class WisoData:
    """Das Ergebnis eines Lesevorgangs."""

    bookings: List[WisoBooking] = field(default_factory=list)
    assets: List[WisoAsset] = field(default_factory=list)
    chart: Dict[int, WisoAccount] = field(default_factory=dict)
    #: SKR03-Nummer → Anzahl Verwendungen, für die es keine SKR04-Zuordnung gibt.
    unmapped_accounts: Dict[int, int] = field(default_factory=dict)
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
        self._chart = chart
        return chart

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
        for row in self._safe_rows(T_ASSET_PAYMENTS):
            by_asset_payment.setdefault(row.get('INVENTORYID'), []).append(
                WisoPayment(date=_date(row.get('BOOKINGDATE')),
                            amount=row.get('AMOUNTNET') or 0.0,
                            text=_clean(row.get('DESCRIPTION'))))

        out = []
        for row in self.catalog.rows(T_ASSETS):
            asset_id = row.get('ID')
            payments = sorted(by_asset_payment.get(asset_id, []),
                              key=lambda p: p.date or '')
            price = self._purchase_price(row.get('PURCHASEAMOUNTNET'), payments)
            out.append(WisoAsset(
                number=row.get('INVNO'), label=_clean(row.get('LABEL')),
                purchase_date=_date(row.get('PURCHASEDATE')),
                purchase_price=price,
                sale_date=_date(row.get('SALEDATE')),
                useful_life_years=row.get('SERVICELIFE'),
                account=self._to_skr04(row.get('FINACIALACCOUNT'), missing),
                depreciation_account=self._to_skr04(
                    row.get('FINACIALACCOUNT_AFA'), missing),
                source_id=asset_id, payments=payments,
                depreciations=self._depreciations(
                    by_asset_depreciation.get(asset_id, []), payments,
                    _date(row.get('SALEDATE')))))
        return sorted(out, key=lambda a: (a.purchase_date or '', a.number or 0))

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

    def _safe_rows(self, table):
        if table not in self.catalog.relation_id:
            return []
        return self.catalog.rows(table)

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
        memo = getattr(self, '_memo_skipped', 0)
        total = self.catalog.count(T_BOOKINGS)
        return WisoData(
            bookings=bookings, assets=assets, chart=self.chart(),
            unmapped_accounts=missing, memo_rows_skipped=memo,
            tax_rows_skipped=total - len(bookings) - memo)


def read_wiso_database(path, standard_chart_path=None,
                       liquid_accounts=None) -> WisoData:
    """Bequemer Einzeiler für den Regelfall."""
    with WisoDatabase(path, standard_chart_path) as wiso:
        return wiso.read(liquid_accounts)
