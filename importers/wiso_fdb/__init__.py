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

Drei Eigenheiten der Datenbank, die dieses Modul kapselt:

1. **Kontonummern sind SKR03.** ``BAS_FINACC_PLAN`` bildet sie in der Spalte
   ``SKR04`` auf den hier verwendeten Rahmen ab (1210 → 1810 Bank,
   1360 → 1460 Verrechnungskonto, 8405 → 4405 „Erlöse 19 % noch offen“).
   Später angelegte Konten fehlen dort; ihre SKR04-Nummer speichert WISO je Konto
   in ``FINT_EXTERNDEFS`` (siehe :meth:`WisoDatabase.chart`).
2. **Steuerzeilen sind eigene Sätze.** WISO bucht DATEV-artig: eine Zeile
   trägt den Bruttobetrag, eine zweite die Umsatz- bzw. Vorsteuer. Sätze ohne
   ``AMOUNTGROSS`` sind genau diese Steuerzeilen; uBuchhaltung führt die Steuer
   am Buchungssatz selbst und lässt sie deshalb weg.
3. **Der AfA-Plan steht sofort als Buchungssätze da.** WISO schreibt die AfA aller
   Jahre auf einmal in ``MOV_FINACC_ACCRECORDS`` (mit ``INVENTORYID`` und
   ``INVENTORYAMORTID``), auch die künftigen, ohne Kennzeichen „geplant“. Der Leser
   liefert sie und kennzeichnet sie: Welcher Satz ein AfA-Satz ist, zu welchem Planjahr er
   gehört und ob sein Datum nach dem Stichtag (``today``) liegt, steht an
   :class:`WisoBooking` (``is_depreciation``, ``depreciation_year``, ``future_depreciation``;
   erkannt am ``INVENTORYAMORTID`` des Satzes, an WISOs Buchungsart oder ersatzweise an der
   AfA-Tabelle, siehe :func:`_is_depreciation`). Ob ein künftiger Satz angelegt wird,
   entscheidet der Import: nie.
"""
import datetime
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from db.matching import LEGACY_CASH_NUMBERS, import_entry_form
from money import format_de, from_minor, round_minor, to_minor

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
#: Zusatzwerte je Konto und Kontenplan. Die Art 82 trägt in ``EXTERNVALUE`` die SKR04-Nummer,
#: die WISO zum Konto speichert – auch für Unterkonten, die ``BAS_FINACC_PLAN`` nicht führt.
T_EXTERN = 'FINT_EXTERNDEFS'
EXTERN_KIND_SKR04 = 82
EXTERN_COLUMNS = ('ACCOUNTNO', 'FINTABLEID', 'EXTERNKIND', 'EXTERNVALUE')
#: Rechnungen, Kunden, eigene Firma. Der Positionstext steht nicht bei der
#: Rechnungsposition, sondern bei der zugehörigen Auftragsposition.
T_INVOICES = 'MOV_INVOICES'
T_INVOICE_POSITIONS = 'MOV_INVOICES_POSITIONS'
T_ORDER_POSITIONS = 'MOV_ORDERS_POSITIONS'
T_CUSTOMERS = 'BAS_CUSTOMERS'
T_COMPANY = 'BAS_COMPANY'
T_UNITS = 'SUP_ARTICLES_UNITS'

#: Buchungsart (``MOV_FINACC_ACCRECORDS.ACCOUNTINGKIND``) der AfA-Sätze. Gemessen an einer Datenbank
#: einer WISO-Version: Alle AfA-Sätze tragen 28 (und ``BOOKINGKIND`` 41), die Abgangsbuchungen 31
#: (``BOOKINGKIND`` 44/45). Eine andere Version kann anders zählen – die Spalte ist optional, und ohne
#: sie (oder mit einer anderen Zahl) erkennt der Leser den AfA-Satz wie zuvor über ``INVENTORYAMORTID``
#: bzw. die AfA-Tabelle.
ACCOUNTING_KIND_DEPRECIATION = 28

#: Personenkonten (Debitoren/Kreditoren) stehen nicht im Sachkontenrahmen und
#: tragen in SKR03 wie SKR04 dieselbe Nummer.
FIRST_PERSONAL_ACCOUNT = 10000

#: Liquide Konten ohne Rückfrage (SKR04-Nummern, nach der Umschlüsselung): 1460 ist das
#: Verrechnungskonto (Geldtransit). Die Kasse der App (SKR04 1600) und die Bankkonten zählen
#: über die eigenen Konten (``Accounts.SKRAccount``). Die Importe richten das Vorzeichen an
#: dieser Menge aus und schreiben die Form des Satzes (``Bookings.EntryForm``) mit; ebenso
#: nimmt `Database.get_payment_coa_ids` sie. Der SKR03-Kassenbereich 1000–1099 gehört seit 2.1.0
#: nicht mehr dazu – nach der Umschlüsselung sind es in SKR04 Vorräte. Was frühere Versionen
#: damit importiert haben, hält die Form des Bestands fest (Migration auf 2.1.0); ein neuer Import
#: meldet Buchungen auf diesen Konten (``stock_range_accounts``).
DEFAULT_LIQUID_ACCOUNTS = frozenset({1460})

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


def _has_columns(catalog, table, columns) -> bool:
    """Führt ``catalog`` die Tabelle mit allen diesen Spalten (im aktuellen Satzformat)?"""
    relation = catalog.relation_id.get(table)
    if relation is None:
        return False
    found = {column.name for column in catalog.columns(relation, catalog.relation_format[relation])}
    return set(columns) <= found


def _cutoff(today=None) -> str:
    """Stichtag als ISO-Datum: ``None`` ist heute (Ortsdatum), sonst ein ``date`` oder ein ISO-Text.

    Ein Datum **nach** dem Stichtag gilt als künftig."""
    if today is None:
        today = datetime.date.today()
    if isinstance(today, datetime.date):
        return today.isoformat()[:10]
    return datetime.date.fromisoformat(str(today)[:10]).isoformat()


def _positive_whole(value) -> Optional[int]:
    """Eine positive ganze Zahl aus einer Zahl oder einem Text aus Ziffern, sonst ``None``."""
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        text = value.strip()
        if not (text.isascii() and text.isdigit()):
            return None
        try:
            number = int(text)
        except ValueError:                      # absurd lange Ziffernfolge
            return None
        return number if number > 0 else None
    if isinstance(value, float):
        return int(value) if value.is_integer() and value > 0 else None
    if isinstance(value, int):
        return value if value > 0 else None
    return None


def _cents(value) -> Optional[int]:
    """Ein WISO-Betrag ohne Vorzeichen, auf den Cent gerundet, in Minor Units – zum Vergleich zweier
    Beträge (``None`` ohne Betrag)."""
    return None if value is None else round_minor(to_minor(abs(value)), 2)


def _is_depreciation(row: dict, inventory_id, date: Optional[str], gross, amort_amounts) -> bool:
    """Ist der Buchungssatz ``row`` ein AfA-Satz? Nur ein Satz mit ``INVENTORYID`` kann einer sein; dann
    entscheidet in dieser Reihenfolge:

    1. ``INVENTORYAMORTID`` gesetzt – der Verweis auf die AfA-Zeile: ja.
    2. WISOs Buchungsart: ``ACCOUNTINGKIND`` = :data:`ACCOUNTING_KIND_DEPRECIATION` – ja, auch bei leerem
       ``INVENTORYAMORTID`` (die Spalte ``ACCOUNTINGKIND`` ist optional).
    3. Der Satz hat die Spalte ``INVENTORYAMORTID``, aber keinen Wert: nein – eine Abgangsbuchung o. Ä. am
       Anlagegut, auch wenn sie am Tag einer AfA-Zeile steht.
    4. Nur wenn der Satz die Spalte nicht kennt (die Tabelle hat sie nicht, oder er steht in einem älteren
       Satzformat: ``catalog.rows`` liefert dann gar keinen Schlüssel statt ``None``), und nicht Art 28 ist:
       ersatzweise die AfA-Tabelle – eine Zeile derselben Anlage mit demselben Datum **und** demselben Betrag
       (|``AMOUNTGROSS``| = |``AMORT_AMOUNT``|, auf den Cent). ``amort_amounts`` ist
       :meth:`WisoDatabase._depreciation_index`.
    """
    if inventory_id is None:
        return False
    if row.get('INVENTORYAMORTID') is not None:
        return True
    if row.get('ACCOUNTINGKIND') == ACCOUNTING_KIND_DEPRECIATION:
        return True
    if 'INVENTORYAMORTID' in row:
        return False
    return _cents(gross) in amort_amounts.get((inventory_id, date), ())


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
    #: Form des Satzes (``db.matching.FORM_*``), wie der Import das Vorzeichen gesetzt hat.
    form: str = ''
    #: Betrag, den die Regel von 2.0.x (Zahlungskonten einschließlich 1000–1099) ergeben hätte –
    #: nur bei einem Satz, der ein Konto 1000–1099 berührt, sonst ``None``. Hat ein älterer Import
    #: den Satz geschrieben, steht er so in der Datenbank (``db.wiso_fdb_import``).
    legacy_amount: Optional[float] = None
    #: Ein AfA-Satz: Der Satz trägt eine ``INVENTORYID`` und ``INVENTORYAMORTID`` oder WISOs Buchungsart
    #: der AfA (``ACCOUNTINGKIND``, :data:`ACCOUNTING_KIND_DEPRECIATION`). Kennt er die Spalte
    #: ``INVENTORYAMORTID`` nicht (die Tabelle hat sie nicht, oder er steht in einem älteren Satzformat),
    #: erkennt der Leser ihn ersatzweise an der AfA-Tabelle (``MOV_INVENTORY_AMORTIZATIONS``): Zeile
    #: derselben Anlage mit demselben Datum und demselben Betrag. Ein Satz mit leerer ``INVENTORYAMORTID``
    #: und anderer Buchungsart – eine Abgangsbuchung am Anlagegut – ist keiner.
    is_depreciation: bool = False
    #: Ein AfA-Satz mit einem Datum **nach** dem Stichtag: WISO hat ihn mit dem AfA-Plan vorab geschrieben, gebucht
    #: wird im jeweiligen Jahr in der App. Der Leser liefert ihn, kennzeichnet ihn aber nur – er zählt in keinem
    #: Zähler des Lesers. Der Import legt ihn nie an; nur ein vorhandener Satz derselben Gruppe (Bestand aus einem
    #: Import bis 2.2.4) wird wiedererkannt.
    future_depreciation: bool = False
    #: Das Planjahr des AfA-Satzes: das Jahr des ``BOOKINGDATE`` der Zeile, auf die ``INVENTORYAMORTID``
    #: zeigt; sonst (Ersatzweg über die AfA-Tabelle, keine passende Zeile) das Jahr des Satzdatums. ``None``, wenn
    #: der Satz kein AfA-Satz ist oder kein Datum trägt.
    depreciation_year: Optional[int] = None
    #: Konto und Gegenkonto, wie ein Import bis 2.2.4 sie gelesen hat (ohne die von WISO gespeicherte
    #: SKR04-Nummer, nur über das Basiskonto) – nur gesetzt, wenn sie von ``account`` bzw.
    #: ``counter_account`` abweichen. Ein Satz, den ein solcher Import schon geschrieben hat, steht
    #: auf diesem Basiskonto (``db.wiso_fdb_import``).
    legacy_account: Optional[int] = None
    legacy_counter_account: Optional[int] = None


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
    #: Anschaffungswert **netto** – Summe der Teilzahlungen, plus ein echter eingetragener Wert
    #: (``PURCHASEAMOUNTNET``, siehe :meth:`WisoDatabase._purchase_price`).
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
    #: Unterkonten von Zahlungskonten, die in der App keine Zahlungskonten sind: je neuer Kontonummer
    #: (``nummer``) mit dem Konto, unter dem ein Import bis 2.2.4 sie gelesen hat (``basis``, ein
    #: Zahlungskonto), und der Zahl der Sätze (``anzahl``). Auf der Seite des Satzes ändert sich damit das
    #: Vorzeichen und die Form – er wird als Umbuchung gelesen. Nur gemeldet, nicht behandelt.
    liquid_subaccounts: List[dict] = field(default_factory=list)
    #: Sätze mit negativem Bruttobetrag und genau einer liquiden Seite. WISO schreibt den
    #: Bruttobetrag nicht negativ (Umkehrungen stehen als vertauschte Konten); der Zähler ist
    #: ein Wächter für Sätze außerhalb dieses Musters. Die Regel ist festgeschrieben: Bei genau
    #: einer liquiden Seite bestimmt die Geldbewegung das Vorzeichen.
    negative_gross: int = 0


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
        self._legacy_chart = None

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

        Die SKR04-Nummer eines Kontos kommt in dieser Reihenfolge:

        1. ``BAS_FINACC_PLAN`` führt jedes Konto je Buchungsjahr; genommen wird
           der jüngste Eintrag, weil die SKR04-Zuordnung über die Jahre stabil ist.
           Was dort eine SKR04-Nummer nennt, wird nie überschrieben.
        2. Für jedes Konto ohne Nummer die, die WISO zum Konto speichert
           (``FINT_EXTERNDEFS``, Art 82, siehe :meth:`_stored_skr04`) – so behalten
           später angelegte Unterkonten ihre eigene Nummer. Konten aus ``KNOWN_EQUIVALENTS``
           nehmen daran nicht teil.
        3. Nur für Konten ohne gespeicherte Nummer der Rückgriff auf das Basiskonto
           (:meth:`_resolve_subaccounts`).
        4. ``KNOWN_EQUIVALENTS``.

        Ohne ``FINT_EXTERNDEFS`` (Tabelle oder Spalten fehlen) entfällt Schritt 2 –
        das Ergebnis ist dann das der Versionen bis 2.2.4. Diese Zuordnung
        ohne Schritt 2 hält :meth:`_legacy_skr04` für den Abgleich mit älteren Importen bereit.
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
        bases, labels = self._subaccounts()
        legacy = dict(chart)                       # WisoAccount wird ersetzt, nie verändert
        self._resolve_subaccounts(legacy, bases, labels)
        self._apply_known_equivalents(legacy)
        self._apply_stored_skr04(chart, bases, labels)
        self._resolve_subaccounts(chart, bases, labels)
        self._apply_known_equivalents(chart)
        self._legacy_chart = legacy
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

    def _subaccounts(self):
        """Selbst angelegte Unterkonten aus ``FINT_ACCOUNTS``: ``(Basiskonten, Bezeichnungen)``,
        beide nach SKR03-Nummer des Kontos. Merkt sich die Bezeichnungen für :meth:`read`."""
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
        return bases, labels

    def _stored_skr04(self) -> Dict[int, int]:
        """SKR03-Nummer → die SKR04-Nummer, die WISO zum Konto speichert.

        ``FINT_EXTERNDEFS`` führt Zusatzwerte je Konto (``ACCOUNTNO``) und Kontenplan
        (``FINTABLEID``, die höhere Zahl ist der jüngere Plan); die Art 82 nennt in
        ``EXTERNVALUE`` die SKR04-Nummer. Je Konto gilt der Wert aus dem jüngsten Plan, der
        eine brauchbare Nummer nennt: Text oder Zahl, nur eine ganze Zahl von 1 bis 9999 zählt –
        ein leerer, nicht numerischer, negativer oder zu großer Wert wird übergangen, als stünde
        er nicht da. Eine gespeicherte Nummer gilt nur für Sachkonten: ``ACCOUNTNO`` unter
        ``FIRST_PERSONAL_ACCOUNT``; Personenkonten (Debitoren/Kreditoren) tragen in beiden
        Rahmen dieselbe Nummer und bekommen nie einen Eintrag daraus.
        Gelesen wird aus dem Mandanten; was er zu einem Konto nicht nennt, ergänzt der
        Standardrahmen (Mandant schlägt Standard, die Plan-Nummern beider Dateien sind nicht
        vergleichbar). Fehlt die Tabelle oder eine ihrer Spalten (``EXTERN_COLUMNS``), wird die
        Datei übergangen – der Leser verhält sich dann wie bis 2.2.4.
        """
        stored: Dict[int, int] = {}
        for source in (self._standard, self.catalog):
            if source is None or not _has_columns(source, T_EXTERN, EXTERN_COLUMNS):
                continue
            newest: Dict[int, tuple] = {}
            for row in source.rows(T_EXTERN):
                if row.get('EXTERNKIND') != EXTERN_KIND_SKR04:
                    continue
                account = row.get('ACCOUNTNO')
                number = _positive_whole(row.get('EXTERNVALUE'))
                if (number is None or number >= FIRST_PERSONAL_ACCOUNT
                        or not isinstance(account, (int, float)) or isinstance(account, bool)
                        or account >= FIRST_PERSONAL_ACCOUNT):
                    continue
                plan = row.get('FINTABLEID')
                plan = plan if isinstance(plan, (int, float)) and not isinstance(plan, bool) else 0
                if account not in newest or plan >= newest[account][0]:
                    newest[account] = (plan, number)
            stored.update({account: number for account, (_plan, number) in newest.items()})
        return stored

    def _apply_stored_skr04(self, chart, bases, labels):
        """Konten ohne SKR04-Nummer in ``BAS_FINACC_PLAN`` bekommen die gespeicherte Nummer –
        außer den Konten aus ``KNOWN_EQUIVALENTS``: Die behalten exakt die Zuordnung, die sie vor
        der gespeicherten Nummer hatten (Rückgriff auf das Basiskonto, sonst der geprüfte Nachtrag).

        Der Text: die Bezeichnung aus ``FINT_ACCOUNTS`` (wie bei Unterkonten), sonst der Text, den
        der Plan zum Konto führt, sonst der des Basiskontos, sonst leer. Fehlt das SKR04-Konto im
        Kontenrahmen der App, legt der Import es mit dieser Bezeichnung an.
        """
        for number, skr04 in self._stored_skr04().items():
            known = chart.get(number)
            if (known is not None and known.skr04) or number in KNOWN_EQUIVALENTS:
                continue
            text = (labels.get(number) or (known.text if known else '')
                    or self._base_text(chart, bases, number))
            chart[number] = WisoAccount(skr03=number, skr04=skr04, text=text,
                                        year=known.year if known else None)

    @staticmethod
    def _base_text(chart, bases, number):
        """Text des nächsten Basiskontos, das der Kontenrahmen mit Text führt – sonst leer."""
        seen = {number}
        base = bases.get(number)
        while base is not None and base not in seen:
            seen.add(base)
            account = chart.get(base)
            if account is not None and account.text:
                return account.text
            base = bases.get(base)
        return ''

    @staticmethod
    def _resolve_subaccounts(chart, bases, labels):
        """Unterkonten ohne SKR04-Nummer über ihr Basiskonto anschließen – der Rückgriff.

        WISO erlaubt Unterkonten je Fahrzeug oder Projekt („4532 laufende
        Kfz-Betriebskosten Tesla“ unter „4530“). Sie stehen nur in
        ``FINT_ACCOUNTS`` und nennen dort mit ``BASEACCOUNTNO`` ihr Basiskonto.
        Hat WISO zum Unterkonto keine eigene Nummer gespeichert (:meth:`_stored_skr04`) und
        nennt auch ``BAS_FINACC_PLAN`` keine, erbt es die SKR04-Nummer des Basiskontos – das
        ist nur eine Näherung: eigene Unterkonten fallen dabei auf das Basiskonto zusammen.
        Die Kette wird verfolgt, denn ein Basiskonto kann selbst ein Unterkonto sein.
        ``bases`` und ``labels`` liefert :meth:`_subaccounts`.
        """
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

    def _legacy_skr04(self, number, current):
        """Die SKR04-Nummer, die ein Import bis 2.2.4 zu ``number`` gelesen hat (Zuordnung ohne die
        von WISO gespeicherte Nummer, siehe :meth:`chart`) – nur, wenn sie von ``current`` abweicht,
        sonst ``None``."""
        if number is None or number >= FIRST_PERSONAL_ACCOUNT:
            return None
        self.chart()
        account = self._legacy_chart.get(number)
        old = account.skr04 if account is not None else None
        return old if old and old != current else None

    # ------------------------------------------------------------------
    # Buchungen
    # ------------------------------------------------------------------
    def bookings(self, liquid_accounts=None, missing=None,
                 disposed=None, today=None) -> List[WisoBooking]:
        """Buchungssätze lesen, umschlüsseln und vorzeichenrichtig stellen.

        ``liquid_accounts`` sind SKR04-Nummern der eigenen Zahlungskonten; sie
        entscheiden über die Richtung des Betrags (Zugang positiv, Abgang
        negativ) – dieselbe Regel wie beim CSV-Import.

        ``disposed`` bildet Anlagegut-Id auf Abgangsdatum ab. Damit fallen die
        AfA-Buchungen des Erinnerungswerts weg, die WISO für ein längst
        verkauftes Anlagegut weiterplant (``memo_rows_skipped``; die Regel hat
        Vorrang vor der folgenden). :meth:`read` füllt das selbst.

        ``today`` ist der Stichtag (``None``: heute, sonst ein ``date`` oder ein
        ISO-Datum). WISO schreibt den ganzen AfA-Plan sofort als Buchungssätze, auch
        für künftige Jahre. Welcher Satz ein **AfA-Satz** ist, regelt :func:`_is_depreciation`
        (``INVENTORYAMORTID``, WISOs Buchungsart, ersatzweise die AfA-Tabelle); eine
        Abgangsbuchung am Anlagegut ist keiner. Ein AfA-Satz mit einem Datum **nach** dem
        Stichtag wird geliefert und gekennzeichnet (``WisoBooking.future_depreciation``): Der
        Import legt ihn nie an (gebucht wird im jeweiligen Jahr in der App) und zählt ihn selbst.
        Der Leser zählt für solche Sätze nichts – weder fehlende Konten noch negative Beträge noch
        Unterkonten von Zahlungskonten –, und sie stehen nicht unter den übersprungenen Sätzen
        (``tax_rows_skipped``, ``memo_rows_skipped``). ``WisoBooking.depreciation_year`` nennt das
        Planjahr eines AfA-Satzes. Sätze ohne ``INVENTORYID`` sind nie betroffen.

        Unterkonten von Zahlungskonten, die in der App keine sind (``liquid_subaccounts``),
        zählt der Leser nur: Vorzeichen und Form folgen der neuen Nummer.
        """
        liquid = set(DEFAULT_LIQUID_ACCOUNTS) | set(liquid_accounts or ())
        legacy_liquid = liquid | set(LEGACY_CASH_NUMBERS)        # die Menge von 2.0.x
        missing = {} if missing is None else missing
        disposed = disposed or {}
        cutoff = _cutoff(today)
        amort_years, amort_amounts = self._depreciation_index()
        liquid_subaccounts: Dict[tuple, int] = {}
        self._memo_skipped = 0
        self._negative_gross = 0
        out = []
        for row in self.catalog.rows(T_BOOKINGS):
            gross = row.get('AMOUNTGROSS')
            if gross is None:
                continue                     # reine Steuerzeile, siehe Modulkopf
            date = _date(row.get('ACCOUNTING_DATE'))
            inventory_id = row.get('INVENTORYID')
            if _after_disposal_year(date, disposed.get(inventory_id)):
                self._memo_skipped += 1
                continue
            amort_id = row.get('INVENTORYAMORTID')
            is_depreciation = _is_depreciation(row, inventory_id, date, gross, amort_amounts)
            future = is_depreciation and bool(date and date > cutoff)
            depreciation_year = None
            if is_depreciation:
                depreciation_year = amort_years.get(amort_id)
                if depreciation_year is None and date:
                    depreciation_year = int(date[:4])
            source_account = row.get('ACCOUNTNO')
            source_counter = row.get('CONTRA_ACCOUNTNO')
            # Ein künftiger AfA-Satz kommt nicht in die Bücher: Er soll in keinem Zähler stehen
            counting = {} if future else missing
            account = self._to_skr04(source_account, counting)
            counter = self._to_skr04(source_counter, counting)
            unmapped = [number for number, mapped
                        in ((source_account, account), (source_counter, counter))
                        if number is not None and mapped is None]
            amount, form = self._signed(gross, account, counter, liquid)
            if not future and float(gross) < 0 and (account in liquid) != (counter in liquid):
                self._negative_gross += 1          # genau eine Seite liquide: sie bestimmt das Vorzeichen
            legacy_amount = None
            if account in LEGACY_CASH_NUMBERS or counter in LEGACY_CASH_NUMBERS:
                legacy_amount = self._signed_amount(
                    gross, account in legacy_liquid, counter in legacy_liquid)
            tax_rate = row.get('TAXRATE')
            tax_rate = tax_rate / 100.0 if tax_rate else None
            net = row.get('AMOUNTNET')
            tax_amount = None
            if tax_rate and net is not None:
                tax_amount = round(abs(gross) - abs(net), 2)
                if amount < 0:
                    tax_amount = -tax_amount
            legacy_account = self._legacy_skr04(source_account, account)
            legacy_counter = self._legacy_skr04(source_counter, counter)
            for legacy, current in ((legacy_account, account), (legacy_counter, counter)):
                if not future and legacy in liquid and current is not None and current not in liquid:
                    key = (current, legacy)         # ein Zahlungskonto, das jetzt keines mehr ist
                    liquid_subaccounts[key] = liquid_subaccounts.get(key, 0) + 1
            group = row.get('ACCOUNTINGID')
            out.append(WisoBooking(
                date=date,
                amount=amount, account=account, counter_account=counter,
                tax_rate=tax_rate, tax_amount=tax_amount,
                text=_clean(row.get('ACCOUNTING_TEXT')),
                document_number=_clean(row.get('REFERENCENO')),
                group=str(group) if group is not None else '',
                invoice_id=row.get('INVID'), payment_id=row.get('PAYMENTID'),
                inventory_id=inventory_id, source_id=row.get('ID'),
                unmapped=unmapped, form=form, legacy_amount=legacy_amount,
                is_depreciation=is_depreciation, depreciation_year=depreciation_year,
                future_depreciation=future,
                legacy_account=legacy_account, legacy_counter_account=legacy_counter,
            ))
        self._liquid_subaccounts = [
            {'nummer': number, 'basis': basis, 'anzahl': count}
            for (number, basis), count in sorted(liquid_subaccounts.items())]
        return out

    def _depreciation_index(self):
        """Die AfA-Tabelle für die Zuordnung der Buchungssätze zu ihrem Planjahr:
        ``({ID der AfA-Zeile: Jahr ihres BOOKINGDATE}, {(INVENTORYID, ISO-Datum): {Beträge}})``. Die
        Beträge (``AMORT_AMOUNT``) stehen ohne Vorzeichen auf den Cent gerundet in Minor Units
        (:func:`_cents`) – für den Ersatzweg von :func:`_is_depreciation`."""
        years, amounts = {}, {}
        for row in self._safe_rows(T_DEPRECIATIONS):
            date = _date(row.get('BOOKINGDATE'))
            if not date:
                continue
            cents = _cents(row.get('AMORT_AMOUNT'))
            if cents is not None:
                amounts.setdefault((row.get('INVENTORYID'), date), set()).add(cents)
            if row.get('ID') is not None:
                years[row['ID']] = int(date[:4])
        return years, amounts

    @staticmethod
    def _signed(gross, account, counter, liquid):
        """Vorzeichen aus Sicht des liquiden Kontos und die Form des Satzes.

        Gegenkonto liquide → Geld fließt ab (``purpose_first``, Betrag negativ); Konto liquide →
        Geld kommt an (``liquid_first``, Betrag positiv). Sind beide Seiten liquide, ist der Satz
        ein Spiegel: Der Rohbetrag bleibt, positiv heißt „Konto im Soll“ (``mirror_coa``).
        Berührt keine Seite ein Zahlungskonto, bleibt der Betrag, wie er ist, und WISO meint
        „Konto im Soll“ (``transfer``) – auch bei einer leeren oder unbekannten Seite. Bei genau
        einer liquiden Seite wird der Betrag zum Betrag der Geldbewegung. WISO schreibt den
        Bruttobetrag nicht negativ (Umkehrungen stehen als vertauschte Konten); ``bookings``
        zählt einen negativen Brutto trotzdem als ``negative_gross`` – ein Wächter für Sätze
        außerhalb dieses Musters. Die Regel ist festgeschrieben: Bei genau einer liquiden Seite
        bestimmt die Geldbewegung das Vorzeichen.

        Returns ``(Betrag, Form)``.
        """
        counter_liquid = counter in liquid
        account_liquid = account in liquid
        amount = WisoDatabase._signed_amount(gross, account_liquid, counter_liquid)
        return amount, import_entry_form(account, account_liquid, counter, counter_liquid)

    @staticmethod
    def _signed_amount(gross, account_liquid, counter_liquid):
        """Der Betrag zu den liquiden Seiten: bei genau einer die Richtung der Geldbewegung, sonst
        der Rohbetrag."""
        amount = float(gross)
        if counter_liquid and not account_liquid:
            amount = -abs(amount)
        elif account_liquid and not counter_liquid:
            amount = abs(amount)
        return amount

    # ------------------------------------------------------------------
    # Anlagen
    # ------------------------------------------------------------------
    def assets(self, missing=None) -> List[WisoAsset]:
        """Anlagegüter mit AfA-Plan und den Zahlungen des Kaufs. Der AfA-Plan bleibt vollständig – auch seine
        künftigen Jahre (der Stichtag gilt den Buchungssätzen, :meth:`bookings`)."""
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
            stated = row.get('PURCHASEAMOUNTNET')
            price = self._purchase_price(stated, payments)
            disposal = by_asset_disposal.get(asset_id)
            sale_date = _date(row.get('SALEDATE'))
            entered = self._entered_price(stated, payments)
            schedule = self._depreciations(
                by_asset_depreciation.get(asset_id, []), payments, sale_date, entered)
            residual, warnings = self._residual_value(schedule, disposal, price)
            if entered:
                warnings.insert(0, (
                    f'Der Anschaffungswert ({format_de(to_minor(price))} €) setzt sich aus dem in WISO '
                    f'eingetragenen Wert ({format_de(to_minor(entered))} €) und den Zahlungen '
                    f'({format_de(self._paid(payments))} €) zusammen – bitte prüfen, ob beides zur '
                    'Anschaffung gehört.'))
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
        AfA-Plan mit dem einfachen rechnet (:meth:`_purchase_price` zählt eine
        doppelt erfasste Anschaffung einmal). Eine Abweichung wird gemeldet, denn
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
    #: Gilt nur ohne Zahlungen (``_purchase_price``); mit Zahlungen gilt die Behelfsgrenze.
    PRICE_PLACEHOLDER = 0.01

    #: Behelfsgrenze für ``PURCHASEAMOUNTNET`` bei vorhandenen Zahlungen (1,00 €): Gemessen an echten
    #: Daten trägt der Nutzer bei der Erstanlage eines Anlageguts oft einen Behelfsbetrag von 1 € ein.
    #: Ein Wert bis zu dieser Grenze (einschließlich) zählt dann nie zum Anschaffungswert.
    PRICE_HELPER_LIMIT = 1.00

    @staticmethod
    def _paid(payments) -> int:
        """Summe der **positiven** Zahlungen in Minor Units. Negative Zeilen in
        ``MOV_INVENTORY_BOOKINGS`` sind der Abgang, nicht Teil der Anschaffung."""
        return sum(to_minor(p.amount) for p in payments if p.amount > 0)

    @staticmethod
    def _entered_price(stated, payments) -> float:
        """Der eingetragene Wert (``PURCHASEAMOUNTNET``), soweit er **zusätzlich** zu den Zahlungen
        zum Anschaffungswert zählt, sonst ``0.0``.

        Er zählt nur, wenn es Zahlungen gibt, und dann, wenn er **über** der Behelfsgrenze liegt
        (:attr:`PRICE_HELPER_LIMIT`) und **nicht** auf den Cent gleich der Summe der Zahlungen ist:

        * bis zur Grenze: Behelfsbetrag, zählt nicht;
        * gleich der Zahlungssumme: dieselbe Anschaffung zweimal erfasst (von Hand **und** über die
          Zahlungen), zählt einmal – :meth:`_residual_value` meldet die Folgen im Quellbestand;
        * sonst: zwei echte Beträge (z. B. ein Gerät von Hand eingetragen, die Montage als Zahlung).
          :meth:`assets` meldet das als Auffälligkeit der Anlage – ein Wert, der der Zahlungssumme
          nur um einen Cent nicht gleicht, wäre sonst still ein zweiter Betrag.
        """
        paid = WisoDatabase._paid(payments)
        if paid <= 0 or not stated or stated <= 0:
            return 0.0
        cents = _cents(stated)
        if cents <= to_minor(WisoDatabase.PRICE_HELPER_LIMIT) or cents == round_minor(paid, 2):
            return 0.0
        return stated

    @staticmethod
    def _purchase_price(stated, payments):
        """Anschaffungswert **netto** in Euro.

        Mit Zahlungen: die Summe der **positiven** Zahlungen **plus** der eingetragene Wert
        (``PURCHASEAMOUNTNET``), wenn er zählt (:meth:`_entered_price`: nicht als Behelf bis 1,00 €,
        nicht als Doppelung der Zahlungssumme). Gerechnet wird in Minor Units. Ohne Zahlungen gilt wie
        bisher ``PURCHASEAMOUNTNET``, wenn es über dem Platzhalter (:attr:`PRICE_PLACEHOLDER`) liegt.
        """
        paid = WisoDatabase._paid(payments)
        if paid > 0:
            entered = to_minor(WisoDatabase._entered_price(stated, payments))
            return float(from_minor(round_minor(paid + entered, 2)))
        return stated if stated and stated > WisoDatabase.PRICE_PLACEHOLDER else 0.0

    @staticmethod
    def _depreciations(rows, payments, sale_date=None, entered=0.0):
        """AfA-Zeilen mit jahresbezogenem Restbuchwert.

        Im **Abgangsjahr** steht noch die zeitanteilige AfA – die gehört dazu.
        Erst danach plant WISO mit dem Erinnerungswert weiter (0,01 € und
        Korrekturen darauf); diese Zeilen entfallen. Der Schnitt geht deshalb
        nach dem Abgangs*jahr*, nicht nach dem Abgangs*datum*.

        Der Anschaffungswert **wächst**: spätere Teilzahlungen sind
        nachträgliche Anschaffungskosten. Der Restwert eines Jahres ist deshalb
        die Summe der bis dahin geleisteten Zahlungen minus der kumulierten AfA
        – so und nur so trifft er WISOs eigene Werte. ``entered`` ist der
        eingetragene Wert, soweit er zum Anschaffungswert zählt
        (:meth:`_entered_price`): Er trägt kein Datum und gilt ab dem Kaufjahr,
        also in jedem Planjahr, zur Basis.

        Die Planjahre bleiben vollständig, auch die künftigen: Den Status eines Jahres im Plan der App
        bestimmt, ob sein AfA-Satz in den Büchern steht (``db.wiso_fdb_import``), nicht sein Datum.

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
            base = to_minor(entered) + sum(
                to_minor(p.amount) for p in payments
                if p.amount > 0 and (year is None or (p.date or '')[:4] <= str(year)))
            out.append(WisoDepreciation(
                year=year, date=date,
                amount=row.get('AMORT_AMOUNT') or 0.0, cumulated=cumulated,
                book_value=float(from_minor(max(0, round_minor(base - to_minor(cumulated), 2))))))
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
    def read(self, liquid_accounts=None, today=None) -> WisoData:
        """Alles auf einmal lesen – das, was der Import braucht.

        Reihenfolge mit Absicht: erst die Anlagen, denn ihre Abgangsdaten
        entscheiden, welche AfA-Buchungen noch echt sind.

        ``today`` ist der Stichtag für vorausgeplante AfA (``None``: heute, sonst ein
        ``date`` oder ein ISO-Datum): Ein AfA-Satz danach trägt ``future_depreciation``
        (der Import legt ihn nie an).
        """
        cutoff = _cutoff(today)
        missing: Dict[int, int] = {}
        assets = self.assets(missing)
        disposed = {a.source_id: a.sale_date for a in assets if a.sale_date}
        bookings = self.bookings(liquid_accounts, missing, disposed, cutoff)
        customers = self.customers()
        memo = getattr(self, '_memo_skipped', 0)
        total = self.catalog.count(T_BOOKINGS)
        labels = getattr(self, 'account_labels', {})
        return WisoData(
            bookings=bookings, assets=assets, chart=self.chart(),
            customers=customers, invoices=self.invoices(customers),
            company=self.company(),
            unmapped_accounts=missing, memo_rows_skipped=memo,
            liquid_subaccounts=getattr(self, '_liquid_subaccounts', []),
            negative_gross=getattr(self, '_negative_gross', 0),
            unmapped_labels={n: labels[n] for n in missing if n in labels},
            tax_rows_skipped=total - len(bookings) - memo)


def read_wiso_database(path, standard_chart_path=None,
                       liquid_accounts=None, today=None) -> WisoData:
    """Bequemer Einzeiler für den Regelfall."""
    with WisoDatabase(path, standard_chart_path) as wiso:
        return wiso.read(liquid_accounts, today)
