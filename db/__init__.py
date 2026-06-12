"""Datenbankschicht (zuvor monolithisches db.py).

Die Database-Klasse wird aus Domaenen-Mixins komponiert; das oeffentliche
Interface (from db import Database / coa_id) bleibt unveraendert.
"""
from .core import _CoreMixin, coa_id
from .schema import SchemaMixin
from .seed import SeedMixin
from .receipts import ReceiptsMixin
from .worktimes import WorkTimesMixin
from .trips import TripsMixin
from .bookings import BookingsMixin
from .matching import MatchingMixin
from .accounts import AccountsMixin
from .assets import AssetsMixin
from .wiso_import import WisoImportMixin
from .contacts import ContactsMixin
from .articles import ArticlesMixin
from .numbering import NumberRangeMixin
from .invoices import InvoicesMixin
from .reporting import ReportingMixin


class Database(SchemaMixin, SeedMixin, ReceiptsMixin, WorkTimesMixin, TripsMixin, BookingsMixin, MatchingMixin, AccountsMixin, AssetsMixin, WisoImportMixin, ContactsMixin, ArticlesMixin, NumberRangeMixin, InvoicesMixin, ReportingMixin, _CoreMixin):
    """Komposition aller Domaenen-Mixins. Siehe db/<domaene>.py."""
    pass


__all__ = ["Database", "coa_id"]
