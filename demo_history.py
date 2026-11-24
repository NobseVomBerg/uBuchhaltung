# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Erzeuger der umfangreichen Demodaten (nur Demo-Modus): mehrere Jahre Buchhaltung aus Tabellen.

``build_history(today, profile)`` ist eine reine Funktion – ohne Datenbank, ohne Zufall, ohne Uhr. Sie liefert
Vorgaben im Schema des Szenarios (``seed_data/test/demo_scenario.json``: ``days_ago``, ``customer_number``,
``items``, ``amount`` …), dazu ein ``date`` (ISO) je Eintrag und neue Schlüssel für das, was das Szenario nicht
kennt: Angebote, Anlagen, Überträge, Arbeitszeiten, offene Bankbewegungen. ``demo_seed.DemoScenario`` hängt sie über
``extra`` an das von Hand gepflegte Szenario an und lädt sie über die App-eigenen Codepfade.

Die Texttabellen stehen in ``seed_data/test/demo_history.json`` (dort prüft sie der Anonymisierungs-Guard); Kunden und
Artikel sind die der Test-Seeddaten. Beträge sind ``Decimal`` in Euro, gerechnet wird über ``money.py`` in Minor Units.

Regeln, die den Aufbau tragen:

* **Auswahl aus Tabellen** über ``zlib.crc32(f"{jahr}-{monat}-{k}") % len(tabelle)`` (``_pick``): Ein Kalendermonat
  sieht unabhängig vom Stichtag immer gleich aus, nichts hängt an ``PYTHONHASHSEED`` oder Dict-Reihenfolgen.
* **Daten** liegen auf Werktagen (``_workday``: Samstag → Freitag, Sonntag → Montag; nie über den Stichtag hinaus und
  nie über einen Jahreswechsel, dann die andere Richtung). Was nach dem Stichtag läge, entfällt.
* **Das erste Jahr der Historie ist ein Kleinunternehmerjahr** (Rechnungen ohne Steuerausweis, Ausgaben ohne Vorsteuer,
  niedrigere Miete, eine Privateinlage); danach 19 % bzw. 7 %.
* **In den letzten 14 Tagen legt der Erzeuger keine Rechnung an** (auch keine Storno- oder Ersatzrechnung): Das
  E-Rechnungs-Beispiel (``tools/make_einvoice_samples.py``) bleibt die jüngste 19-%-Rechnung des Szenarios.
* Je Jahr zwei Stornos: eines unbezahlt, eines bezahlt, dann storniert, mit niedrigerer Ersatzrechnung (erst ab dem
  3. Quartal) und Erstattung des Rests. Abgeschlossene Jahre sind bezahlt bis auf EINE überfällige Rechnung des Vorjahres.
* **Genau eine Rechnung mit Steuersatz je Position** (7 % und 19 % auf einem Beleg, in zwei Raten bezahlt), **nur im Profil ``full``**
  (``profiles.full.mixed_example``, ``_mixed_invoice``): Das Profil ``test`` – der Bestand mit fest gepinnten Werten – und das handgepflegte Szenario
  bleiben ohne sie. Sie geht den Weg der Maske (``taxMode: mixed``) wie jede Rechnung des Erzeugers.
* **Ein zweites Beispiel mit Steuersätzen aus den Stammdaten** (seit 2.8.0: 19 % und steuerfrei auf einem Beleg, in zwei Raten bezahlt), ebenfalls **nur im
  Profil ``full``** (``profiles.full.tax_entry_example``, ``_tax_entry_invoice``): Dazu legt der Erzeuger zwei Einträge der Stammdaten an (``tax_entries``:
  steuerfrei mit erfundenem Grund und dem Erlöskonto 4100, 5,5 % mit dem Erlöskonto 4180 – zu 5,5 % gibt es keine Rechnung). ``demo_seed.DemoScenario`` schreibt
  sie vor den Rechnungen; die steuerfreie Position nennt ihren Eintrag (``tax_entry``) und schickt seine ID wie die Maske (``taxRateId``). Profil ``test`` und
  Szenario bleiben auch hier ohne sie – ihre Ausgabe ist dieselbe wie vorher.
* **Ein drittes Beispiel: eine Rechnung, die der Kunde mit Skonto bezahlt** (seit 2.9.0, 19 %, Skonto 2 % in 7 Tagen), ebenfalls **nur im Profil ``full``**
  (``profiles.full.skonto_example``, ``_skonto_invoice``): Die Zahlung kommt innerhalb der Skontofrist und ist die Rechnung minus dem Skonto, das sie vorsieht; ``demo_seed``
  ordnet sie wie der Klick „Mit Skonto zuordnen“ zu (``link_booking_to_invoice_capped(…, skonto=True)``, der Schlüssel ``skonto`` der Zahlung). Profil ``test`` und Szenario
  bleiben ohne sie – ihre gepinnten Werte (EÜR, Summen- und Saldenliste, Liquidität, DATEV) ändern sich nicht.
"""
import datetime
import functools
import json
import os
import zlib
from decimal import Decimal

import money
from demo_seed import _clip_day, _month_start
from tax_groups import parse_rate_bp

_ROOT = os.path.dirname(os.path.abspath(__file__))
HISTORY_FILE = os.path.join(_ROOT, "seed_data", "test", "demo_history.json")
SCENARIO_FILE = os.path.join(_ROOT, "seed_data", "test", "demo_scenario.json")
ARTICLES_FILE = os.path.join(_ROOT, "seed_data", "test", "test_articles.json")

PROFILES = ("full", "test")
STANDARD_RATE = 19            # Prozent
REDUCED_RATE = 7


@functools.lru_cache(maxsize=None)
def _load(path):
    """Eine JSON-Datei mit Dezimalzahlen als ``Decimal`` – gelesen wird nur, nie verändert."""
    with open(path, encoding="utf-8") as f:
        return json.load(f, parse_float=Decimal)


def _tables():
    return _load(HISTORY_FILE)


def _scenario():
    return _load(SCENARIO_FILE)


def _reduced_articles():
    """Die Artikel der Test-Seeddaten mit 7 % Steuer: ``[(Name, Einzelpreis)]``."""
    return [(a["name"], Decimal(str(a["unit_price"]))) for a in _load(ARTICLES_FILE) if a.get("tax_rate") == REDUCED_RATE]


def _crc(*parts):
    return zlib.crc32("-".join(str(part) for part in parts).encode("utf-8"))


def _pick(table, year, month, k):
    """Der Eintrag der Tabelle für diesen Kalendermonat und diese Stelle ``k`` – unabhängig vom Stichtag."""
    return table[_crc(year, month, k) % len(table)]


def _jitter(year, month, k, spread):
    """Eine kleine Verschiebung von ``-(spread // 2)`` bis ``spread - 1 - spread // 2`` Tagen."""
    return _crc(year, month, k) % spread - spread // 2


def _workday(day, bound):
    """``day`` als Werktag: Samstag → Freitag, Sonntag → Montag; nie nach ``bound`` und nie in einem anderen Jahr (dann die
    andere Richtung: Samstag → Montag, Sonntag → Freitag). ``None``, wenn nichts passt – der Eintrag entfällt."""
    steps = {5: (-1, 2), 6: (1, -2)}.get(day.weekday(), (0,))
    for step in steps:
        candidate = day + datetime.timedelta(days=step)
        if candidate.year == day.year and candidate <= bound:
            return candidate
    return None


def _clip(year, month, day):
    return _clip_day(datetime.date(year, month, 1), min(max(day, 1), 28))


def _stamp(today, day):
    return {"date": day.isoformat(), "days_ago": (today - day).days}


def _euro(minor):
    return money.from_minor(minor)


def _tax_from_gross(amount, rate):
    """Die im Brutto enthaltene Steuer (Euro, mit dem Vorzeichen des Betrags) – ``None`` ohne Steuersatz."""
    if not rate:
        return None
    minor = money.tax_from_gross(abs(money.to_minor(amount)), Decimal(str(rate)) * 100)
    return _euro(-minor if amount < 0 else minor)


class _Ctx:
    def __init__(self, today, profile):
        if profile not in PROFILES:
            raise ValueError(f"Unbekanntes Profil der Demodaten: {profile!r}")
        self.t = _tables()
        self.sc = _scenario()
        self.today = today
        self.first_year = today.year - self.t["profiles"][profile]["years_back"]
        # die eine Beispielrechnung mit Steuersatz je Position – nur Profile, die sie in der Tabelle nennen (``full``), sonst ``None``
        self.mixed_example = self.t["profiles"][profile].get("mixed_example")
        # die Einträge der Stammdaten und die eine Beispielrechnung mit steuerfreier Position – ebenso nur ``full``
        self.tax_entry_example = self.t["profiles"][profile].get("tax_entry_example")
        # die eine Beispielrechnung, die mit Skonto bezahlt wird – ebenso nur ``full``
        self.skonto_example = self.t["profiles"][profile].get("skonto_example")
        # jüngster erlaubter Tag einer Rechnung: Der Erzeuger legt in den letzten Tagen keine an
        self.recent_limit = today - datetime.timedelta(days=self.t["recent_days"])

    def months(self):
        for year in range(self.first_year, self.today.year + 1):
            for month in range(1, 13):
                if (year, month) <= (self.today.year, self.today.month):
                    yield year, month

    def is_first_year(self, year):
        return year == self.first_year

    def month_name(self, month):
        return self.t["months"][month - 1]


# ── Rechnungen ───────────────────────────────────────────────────────────────

def _item(description, quantity, unit, price, tax_rate, tax_entry=None):
    """Eine Position der Vorgaben; ``tax_entry`` nennt den Eintrag der Stammdaten (``tax_entries``), den sie wählt – ohne ihn fehlt der Schlüssel."""
    item = {"description": description, "quantity": quantity, "unit": unit,
            "price_per_unit": Decimal(price), "tax_rate": tax_rate}
    if tax_entry is not None:
        item["tax_entry"] = tax_entry
    return item


def _consulting_items(ctx, year, month, index, small, travel=True):
    t = ctx.t["consulting"]
    rate_pct = 0 if small else STANDARD_RATE
    hours = _pick(t["first_year_hours"] if small else t["hours"], year, month, f"h{index}")
    price = _pick(t["first_year_rates"] if small else t["rates"], year, month, f"r{index}")
    description = _pick(t["descriptions"], year, month, f"t{index}").format(month=ctx.month_name(month), year=year)
    items = [_item(description, hours, "HUR", price, rate_pct)]
    if travel and not small and _crc(year, month, f"tr{index}") % t["travel_every"] == 0:
        items.append(_item(t["travel_text"], 1, "C62", _pick(t["travel"], year, month, f"tv{index}"), rate_pct))
    return items


def _flat_items(ctx, year, month, index, small):
    t = ctx.t["flat"]
    description = _pick(t["descriptions"], year, month, f"t{index}").format(month=ctx.month_name(month), year=year)
    price = _pick(t["first_year_prices"] if small else t["prices"], year, month, f"p{index}")
    return [_item(description, 1, "C62", price, 0 if small else STANDARD_RATE)]


def _private_items(ctx, year, month, index, small):
    t = ctx.t["private"]
    description = _pick(t["descriptions"], year, month, f"t{index}")
    hours = _pick(t["first_year_hours"] if small else t["hours"], year, month, f"h{index}")
    price = _pick(t["first_year_rates"] if small else t["rates"], year, month, f"r{index}")
    return [_item(description, hours, "HUR", price, 0 if small else STANDARD_RATE)]


def _goods_items(ctx, year, month, index):
    t = ctx.t["goods"]
    articles = _reduced_articles()
    lines = _pick(t["lines"], year, month, f"l{index}")
    chosen = []
    position = _crc(year, month, f"g{index}") % len(articles)
    while len(chosen) < lines:
        if position not in chosen:
            chosen.append(position)
        position = (position + 1) % len(articles)
    items = []
    for line, article in enumerate(chosen):
        name, price = articles[article]
        items.append(_item(name, _pick(t["quantities"], year, month, f"q{index}-{line}"), "C62", price, REDUCED_RATE))
    return items


def _payments(ctx, day, due_days, year, month, tag):
    """Die Zahlungen einer Rechnung: Versatz zur Fälligkeit aus der Tabelle, rund 8 % in zwei Raten. Was nach dem Stichtag
    läge, entfällt – eine Rechnung mit nur der ersten Rate bleibt teilbezahlt (nur in den letzten Wochen möglich)."""
    t = ctx.t["payment"]
    offset = _pick(t["offsets"], year, month, f"o{tag}")
    first = _workday(day + datetime.timedelta(days=due_days + offset), ctx.today)
    if first is None:
        return []
    after = (first - day).days
    if _crc(year, month, f"two{tag}") % 100 < t["two_rates_per_hundred"]:
        percent = _pick(t["first_percent"], year, month, f"fp{tag}")
        payments = [{"after_days": after, "percent": percent}]
        second = _workday(first + datetime.timedelta(days=_pick(t["second_after_days"], year, month, f"sa{tag}")), ctx.today)
        if second is not None:
            payments.append({"after_days": (second - day).days, "percent": 100 - percent, "closing": True})
        return payments
    return [{"after_days": after, "percent": 100, "closing": True}]


def _invoice(ctx, key, day, customer, due_days, items, small, payments, skonto=False, **more):
    terms = ctx.t["payment"]["texts"]["skonto" if skonto else "plain"].format(days=due_days)
    spec = {"key": key, "via": "form", **_stamp(ctx.today, day), "customer_number": customer, "due_days": due_days,
            "payment_terms": terms, "show_tax": not small, "items": items, "payments": payments}
    if skonto:
        spec["skonto"] = {"days": ctx.t["payment"]["skonto"]["days"], "percent": float(ctx.t["payment"]["skonto"]["percent"])}
    spec.update(more)
    return spec


def _regular_invoices(ctx):
    t = ctx.t
    specs = []
    for year, month in ctx.months():
        small = ctx.is_first_year(year)
        skonto_given = False
        for index, slot in enumerate(t["slots"]["first_year" if small else "regular"]):
            day = _workday(_clip(year, month, slot["day"] + _jitter(year, month, f"sd{index}", 3)), ctx.recent_limit)
            if day is None:
                continue
            kind = slot["kind"]
            customer = _pick(t["customers"][kind], year, month, f"c{index}")
            if kind == "consulting":
                items = _consulting_items(ctx, year, month, index, small)
            elif kind == "flat":
                items = _flat_items(ctx, year, month, index, small)
            elif kind == "private":
                items = _private_items(ctx, year, month, index, small)
            else:
                items = _goods_items(ctx, year, month, index)
            due_days = _pick(t["payment"]["due_days"], year, month, f"d{index}")
            skonto = kind == "flat" and not skonto_given
            skonto_given = skonto_given or skonto
            if skonto:
                due_days = max(due_days, 14)
            specs.append(_invoice(ctx, f"{year}-{month:02d}-{index}", day, customer, due_days, items, small,
                                  _payments(ctx, day, due_days, year, month, index), skonto=skonto, slot=index))
    return specs


def _chain_invoices(ctx, year):
    """Die zwei Stornos des Jahres: eines unbezahlt, eines bezahlt – mit Ersatzrechnung (niedriger, erst ab dem
    3. Quartal) und Erstattung des Rests."""
    c = ctx.t["chains"]
    small = ctx.is_first_year(year)
    rate_pct = 0 if small else STANDARD_RATE
    specs = []

    unpaid = c["unpaid"]
    day = _workday(_clip(year, unpaid["month"], unpaid["day"]), ctx.recent_limit)
    if day is not None:
        spec = _invoice(ctx, f"{year}-storno-unpaid", day, unpaid["customer_number"], unpaid["due_days"],
                        [_item(unpaid["description"], unpaid["hours"], "HUR", unpaid["rate"], rate_pct)], small, [])
        cancel = _workday(day + datetime.timedelta(days=unpaid["cancel_after_days"]), ctx.recent_limit)
        if cancel is not None:
            spec["cancel_after_days"] = (cancel - day).days
        specs.append(spec)

    refund = c["refund"]
    day = _workday(_clip(year, refund["month"], refund["day"]), ctx.recent_limit)
    if day is not None:
        hours = refund["first_year_hours"] if small else refund["hours"]
        lower = refund["first_year_lower_hours"] if small else refund["lower_hours"]
        price = refund["first_year_rate"] if small else refund["rate"]
        make = lambda quantity: [_item(refund["description"], quantity, "HUR", price, rate_pct)]
        paid = _workday(day + datetime.timedelta(days=refund["due_days"] + refund["pay_offset"]), ctx.today)
        payments = [] if paid is None else [{"after_days": (paid - day).days, "percent": 100, "closing": True}]
        spec = _invoice(ctx, f"{year}-storno-refund", day, refund["customer_number"], refund["due_days"], make(hours),
                        small, payments)
        cancel = _workday(day + datetime.timedelta(days=refund["cancel_after_days"]), ctx.recent_limit)
        if paid is not None and cancel is not None and cancel >= paid:
            spec["cancel_after_days"] = (cancel - day).days
            earliest = datetime.date(year, *refund["earliest_replacement"])
            replace = _workday(max(cancel + datetime.timedelta(days=refund["replace_after_days"]), earliest),
                               ctx.recent_limit)
            if replace is not None:
                after = _workday(replace + datetime.timedelta(days=refund["refund_after_days"]), ctx.today)
                spec["replacement"] = {"after_days": (replace - cancel).days, "items": make(lower)}
                if after is not None:
                    spec["replacement"]["refund_after_days"] = (after - replace).days
        specs.append(spec)
    return specs


def _example_day(ctx, example):
    """Der Tag einer Beispielrechnung: ``days_ago`` Tage vor dem Stichtag, auf einem Werktag, nie in den letzten 14 Tagen und nie im ersten Jahr des Profils (dem
    Kleinunternehmerjahr) – ``None``, wenn keiner passt."""
    day = _workday(ctx.today - datetime.timedelta(days=example["days_ago"]), ctx.recent_limit)
    return None if day is None or ctx.is_first_year(day.year) else day


def _example_payments(ctx, day, example):
    """Die Raten einer Beispielrechnung: Sie zählen vom Rechnungstag an (die erste nach der Zahlungsfrist ``due_days``, die zweite – sie schließt – ``second_after_days``
    Tage danach); was nach dem Stichtag läge, entfällt wie bei jeder Rechnung des Erzeugers."""
    payments = []
    first = _workday(day + datetime.timedelta(days=example["due_days"]), ctx.today)
    if first is not None:
        percent = example["first_percent"]
        payments.append({"after_days": (first - day).days, "percent": percent})
        second = _workday(first + datetime.timedelta(days=example["second_after_days"]), ctx.today)
        if second is not None:
            payments.append({"after_days": (second - day).days, "percent": 100 - percent, "closing": True})
    return payments


def _mixed_invoice(ctx):
    """Die eine Beispielrechnung mit Steuersatz je Position (``taxMode: mixed`` im Formular: 7 % und 19 % auf einem Beleg), bezahlt in zwei Raten –
    nur das Profil ``full`` hat sie (``profiles.full.mixed_example`` der Tabelle), das Profil ``test`` und das Szenario nie: Auf diesem Bestand
    stehen fest gepinnte Werte. Der Tag und die Raten: ``_example_day``, ``_example_payments``. ``None``, wenn das Profil keine Beispielrechnung hat oder
    kein Tag passt."""
    example = ctx.mixed_example
    if example is None:
        return None
    day = _example_day(ctx, example)
    if day is None:
        return None
    items = [_item(item["description"], item["quantity"], item["unit"], item["price"], item["tax_rate"]) for item in example["items"]]
    return _invoice(ctx, f"{day.year}-mixed", day, example["customer_number"], example["due_days"], items, False,
                    _example_payments(ctx, day, example), tax_mode="mixed")


def _tax_entry_invoice(ctx):
    """Die eine Beispielrechnung mit Steuersätzen aus den Stammdaten (seit 2.8.0: 19 % und steuerfrei auf einem Beleg, ``taxMode: mixed`` im Formular, die steuerfreie
    Position mit ihrer ``taxRateId``), bezahlt in zwei Raten – nur das Profil ``full`` hat sie (``profiles.full.tax_entry_example``), ein anderer Tag als der des
    Beispiels mit 7 % und 19 %. Ihre steuerfreie Position nennt den Eintrag, den ``_tax_entries`` anlegt (``tax_entry``). Zu 5,5 % gibt es keine Rechnung.
    ``None``, wenn das Profil keine Beispielrechnung hat oder kein Tag passt."""
    example = ctx.tax_entry_example
    if example is None:
        return None
    day = _example_day(ctx, example)
    if day is None:
        return None
    items = [_item(item["description"], item["quantity"], item["unit"], item["price"], item["tax_rate"], item.get("tax_entry"))
             for item in example["items"]]
    return _invoice(ctx, f"{day.year}-tax-entry", day, example["customer_number"], example["due_days"], items, False,
                    _example_payments(ctx, day, example), tax_mode="mixed")


def _skonto_invoice(ctx):
    """Die eine Beispielrechnung, die der Kunde mit Skonto bezahlt (19 %, Skonto-Bedingungen wie bei den Pauschalrechnungen: 2 % in 7 Tagen) – nur das Profil ``full`` hat
    sie (``profiles.full.skonto_example``), das Profil ``test`` und das Szenario nie. Der Tag: ``_example_day``. Die EINE Zahlung (``pay_after_days`` nach dem Rechnungstag,
    innerhalb der Skontofrist) schließt die Rechnung und trägt ``skonto``: ``demo_seed`` zahlt Brutto minus das Skonto der Rechnung und ordnet sie mit Skonto zu. Läge sie nach dem
    Stichtag, entfällt sie wie bei jeder Rechnung des Erzeugers (dann bleibt die Rechnung offen). ``None``, wenn das Profil keine Beispielrechnung hat oder kein Tag passt."""
    example = ctx.skonto_example
    if example is None:
        return None
    day = _example_day(ctx, example)
    if day is None:
        return None
    items = [_item(item["description"], item["quantity"], item["unit"], item["price"], item["tax_rate"]) for item in example["items"]]
    paid = _workday(day + datetime.timedelta(days=example["pay_after_days"]), ctx.today)
    payments = [] if paid is None else [{"after_days": (paid - day).days, "percent": 100, "closing": True, "skonto": True}]
    return _invoice(ctx, f"{day.year}-skonto", day, example["customer_number"], example["due_days"], items, False, payments, skonto=True)


def _tax_entries(ctx):
    """Die Einträge der Steuersätze, die das Profil ``full`` in den Stammdaten anlegt (``profiles.full.tax_entry_example.entries``): ``[{key, label, kind, bp,
    reason, revenue_account}]``, ``bp`` der Satz in Hundertstel Prozent, ``revenue_account`` die Nummer eines Erlöskontos des Standard-Kontenrahmens. Leer ohne
    das Beispiel (Profil ``test``). Den Satz liest ``tax_groups.parse_rate_bp`` – die eine Stelle dafür; einen Text, den sie nicht liest, ist ein Fehler der Tabelle
    und bricht ab."""
    example = ctx.tax_entry_example
    if example is None:
        return []
    entries = []
    for entry in example["entries"]:
        bp = parse_rate_bp(entry["rate"])
        if bp is None:
            raise ValueError(f"Demodaten: Der Steuersatz {entry['rate']!r} des Eintrags {entry['key']!r} ist keiner")
        entries.append({"key": entry["key"], "label": entry["label"], "kind": entry["kind"], "bp": bp,
                        "reason": entry.get("reason"), "revenue_account": entry["revenue_account"]})
    return entries


def _invoices(ctx):
    specs = _regular_invoices(ctx)
    overdue = ctx.t["chains"]["overdue"]
    previous = ctx.today.year - 1
    for spec in specs:
        year, month = int(spec["key"][:4]), int(spec["key"][5:7])
        if (year, month) == (previous, overdue["month"]) and spec["slot"] == overdue["slot"]:
            spec["payments"] = []          # die eine überfällige Rechnung des Vorjahres
    for year in range(ctx.first_year, ctx.today.year + 1):
        specs.extend(_chain_invoices(ctx, year))
    for example in (_mixed_invoice(ctx), _tax_entry_invoice(ctx), _skonto_invoice(ctx)):
        if example is not None:
            specs.append(example)

    f = ctx.t["finalized"]
    day = _workday(ctx.today - datetime.timedelta(days=f["days_ago"]), ctx.recent_limit)
    if day is not None:
        small = ctx.is_first_year(day.year)
        specs.append(_invoice(ctx, f"{day.year}-final", day, f["customer_number"], f["due_days"],
                              [_item(f["description"], f["hours"], "HUR", f["rate"], 0 if small else STANDARD_RATE)],
                              small, [], final_status="finalized"))
    specs.sort(key=lambda s: (s["date"], s["key"]))
    return specs


# ── Angebote ─────────────────────────────────────────────────────────────────

def _quotes(ctx):
    q = ctx.t["quotes"]
    quotes = []
    for year, month in ctx.months():
        day = _workday(_clip(year, month, q["day"]), ctx.today)
        if day is None:
            continue
        small = ctx.is_first_year(year)
        age = (ctx.today - day).days
        if age > q["old_days"]:
            status = "accepted" if _crc(year, month, "qs") % 100 < q["accepted_per_hundred"] else "rejected"
        elif age >= q["recent_days"]:
            status = "sent"
        else:
            status = "draft" if _crc(year, month, "qd") % 2 == 0 else "sent"
        items = _consulting_items(ctx, year, month, "q", small, travel=False)
        if _crc(year, month, "q2") % 2 == 0:
            items += _flat_items(ctx, year, month, "q", small)
        quotes.append({
            "number": f"AG-{year}-{month:03d}", **_stamp(ctx.today, day),
            "valid_until": (day + datetime.timedelta(days=q["valid_days"])).isoformat(),
            "customer_number": _pick(ctx.t["customers"]["consulting"], year, month, "qc"),
            "status": status, "show_tax": not small, "items": items,
            "intro": _pick(q["intro"], year, month, "qi"), "closing": _pick(q["closing"], year, month, "qz"),
        })
    # Jeder Status kommt vor: Aus den angenommenen älteren Angeboten wird das jüngste zum Rechnungsentwurf (an einem Tag, der
    # nicht in den letzten Tagen liegt), das jüngste Angebot ist ein Entwurf
    convert_day = _workday(ctx.today - datetime.timedelta(days=q["convert_days_ago"]), ctx.recent_limit)
    accepted = [quote for quote in quotes if quote["status"] == "accepted"]
    if convert_day is not None and accepted and accepted[-1]["date"] <= convert_day.isoformat():
        accepted[-1]["status"] = "converted"
        accepted[-1]["convert"] = _stamp(ctx.today, convert_day)
    if quotes and quotes[-1]["days_ago"] < q["recent_days"]:
        quotes[-1]["status"] = "draft"
    return quotes


# ── Anlagen ──────────────────────────────────────────────────────────────────

def _asset_day(ctx, entry):
    """Der Kauftag einer Anlage der Tabelle – ``None``, wenn ihr Jahr nicht zum Profil gehört oder der Tag nach dem
    Stichtag läge."""
    if entry["year_index"] > ctx.today.year - ctx.first_year:
        return None
    year = ctx.first_year + entry["year_index"]
    return _workday(_clip(year, entry["month"], entry["day"]), ctx.today)


def _car_day(ctx):
    for entry in ctx.t["assets"]:
        if entry.get("car"):
            return _asset_day(ctx, entry)
    return None


def _assets(ctx):
    assets = []
    for entry in ctx.t["assets"]:
        day = _asset_day(ctx, entry)
        if day is None:
            continue
        small = ctx.is_first_year(day.year)
        price = money.to_minor(entry["price"])
        tax = 0 if small else money.tax_from_net(price, STANDARD_RATE)
        spec = {
            **_stamp(ctx.today, day), "name": entry["name"], "category": entry["category"], "price": entry["price"],
            "years": entry["years"], "coa": entry["coa"], "afa_coa": entry["afa_coa"],
            "supplier_number": entry["supplier_number"], "recipient": entry["recipient"], "location": entry["location"],
            "serial_number": entry["serial_number"], "car": bool(entry.get("car")),
            "gross": _euro(price + tax), "tax_rate": None if small else STANDARD_RATE / 100,
            "tax_amount": None if small else _euro(tax), "text": f"Anschaffung {entry['name']}",
        }
        sale = entry.get("sale")
        if sale:
            sold = _workday(_clip(ctx.today.year, sale["month"], sale["day"]), ctx.today)
            if sold is not None and sold > day:
                net = money.to_minor(sale["price"])
                sale_tax = money.tax_from_net(net, STANDARD_RATE)
                spec["sale"] = {**_stamp(ctx.today, sold), "price": sale["price"], "gross": _euro(net + sale_tax),
                                "tax_rate": STANDARD_RATE / 100, "tax_amount": _euro(sale_tax), "coa": sale["coa"],
                                "buyer_number": sale["buyer_number"], "text": sale["text"]}
        assets.append(spec)
    return assets


# ── Ausgaben, Kasse, Überträge, offene Bankbewegungen ─────────────────────────

def _scenario_expense(ctx, entry, day):
    """Ein Posten des handgepflegten Szenarios für einen früheren Monat (im ersten Jahr ohne Vorsteuer, die Miete niedriger)."""
    small = ctx.is_first_year(day.year)
    amount = entry["amount"]
    if small and entry["coa"] == 6310:
        amount = ctx.t["first_year"]["rent"]
    rate = None if small or not entry.get("tax_rate") else float(entry["tax_rate"])
    return {**_stamp(ctx.today, day), "amount": amount, "coa": entry["coa"], "tax_rate": rate,
            "tax_amount": _tax_from_gross(amount, rate), "recipient": entry["recipient"], "text": entry["text"]}


def _extended_scenario_items(ctx):
    """Die Monats- und Quartalsposten des Szenarios rückwärts bis zum Beginn der Historie – der Raster setzt den des
    Szenarios fort (``months_back``), das Szenario selbst bleibt unverändert."""
    begin = datetime.date(ctx.first_year, 1, 1)
    items = []
    for entry in ctx.sc.get("monthly", []):
        back = entry["months_back"] + 1
        while _month_start(ctx.today, back) >= begin:
            items.append(_scenario_expense(ctx, entry, _clip_day(_month_start(ctx.today, back), entry["day"])))
            back += 1
    for entry in ctx.sc.get("quarterly", []):
        back = (entry["months_back"] // 3 + 1) * 3
        while _month_start(ctx.today, back) >= begin:
            items.append(_scenario_expense(ctx, entry, _clip_day(_month_start(ctx.today, back), entry["day"])))
            back += 3
    return items


def _expense(ctx, day, amount, coa, rate, recipient, text, **more):
    small = ctx.is_first_year(day.year)
    rate = None if small or rate is None else float(rate)
    return {**_stamp(ctx.today, day), "amount": amount, "coa": coa, "tax_rate": rate,
            "tax_amount": _tax_from_gross(amount, rate), "recipient": recipient, "text": text, **more}


def _one_offs(ctx):
    car = _car_day(ctx)
    year_ago = ctx.today - datetime.timedelta(days=365)
    items = []
    for rule in ctx.t["one_offs"]:
        for year, month in ctx.months():
            if month not in rule["months"]:
                continue
            day = _workday(_clip(year, month, rule["day"] + _jitter(year, month, f"{rule['id']}d", 5)), ctx.today)
            if day is None or (rule.get("requires") == "car" and (car is None or day <= car)):
                continue
            amount = -_pick(rule["amounts"], year, month, rule["id"])
            items.append(_expense(ctx, day, amount, rule["coa"], rule["rate"], rule["recipient"], rule["text"],
                                  **({"receipt": True} if rule.get("receipt") and day >= year_ago else {})))
    deposit = ctx.t["first_year"]["deposit"]
    day = _workday(_clip(ctx.first_year, deposit["month"], deposit["day"]), ctx.today)
    if day is not None:
        items.append(_expense(ctx, day, deposit["amount"], deposit["coa"], None, deposit["recipient"], deposit["text"]))
    return items


def _cash_items(ctx):
    t = ctx.t["cash"]
    items = []
    for year, month in ctx.months():
        for index, base in enumerate(t["days"]):
            day = _workday(_clip(year, month, base + _jitter(year, month, f"cd{index}", 5)), ctx.today)
            if day is None:
                continue
            kind = _pick(t["items"], year, month, f"ci{index}")
            amount = -_pick(kind["amounts"], year, month, f"ca{index}")
            items.append(_expense(ctx, day, amount, kind["coa"], kind["rate"], kind["recipient"], kind["text"], cash=True))
    return items


def _splits(ctx):
    splits = []
    for year in range(ctx.first_year, ctx.today.year + 1):
        small = ctx.is_first_year(year)
        for rule in ctx.t["splits"]:
            day = _workday(_clip(year, rule["month"], rule["day"]), ctx.today)
            if day is None:
                continue
            total = money.to_minor(_pick(rule["totals"], year, rule["month"], "st"))
            rows, remaining = [], total
            for index, row in enumerate(rule["rows"]):
                part = remaining if index == len(rule["rows"]) - 1 else \
                    money.round_minor(money.prorate(total, row["percent"], 100), 2)
                remaining -= part
                amount = -_euro(part)
                rate = None if small or row["rate"] is None else float(row["rate"])
                rows.append({"amount": amount, "coa": row["coa"], "tax_rate": rate,
                             "tax_amount": _tax_from_gross(amount, rate), "text": row["text"]})
            splits.append({**_stamp(ctx.today, day), "amount": -_euro(total), "recipient": rule["recipient"],
                           "text": rule["text"], "rows": rows})
    return splits


def _transfers(ctx):
    transfers = []
    for kind, rule in ctx.t["transfers"].items():
        for year, month in ctx.months():
            if month not in rule["months"]:
                continue
            day = _workday(_clip(year, month, rule["day"]), ctx.today)
            if day is None:
                continue
            amount = rule["amounts"][(year + rule["months"].index(month)) % len(rule["amounts"])]
            transfers.append({"kind": kind, **_stamp(ctx.today, day), "amount": Decimal(amount), "text": rule["text"],
                              "recipient": rule["recipient"]})
    transfers.sort(key=lambda x: (x["date"], x["kind"]))
    return transfers


def _open_bank(ctx):
    """Drei Bankbewegungen ohne Buchungssatz im laufenden Monat (heute, gestern, vorgestern; nie vor dem Monatsersten)."""
    first = datetime.date(ctx.today.year, ctx.today.month, 1)
    items = []
    for entry in ctx.t["open_bank"]:
        day = ctx.today - datetime.timedelta(days=entry["offset"])
        if day >= first:
            items.append({**_stamp(ctx.today, day), "amount": entry["amount"], "recipient": entry["recipient"],
                          "text": entry["text"]})
    return items


# ── Fahrten und Arbeitszeiten ────────────────────────────────────────────────

def _trips(ctx):
    """Fahrten ab dem Kauf des PKW bis vor die erste Fahrt des Szenarios. Die Tachostände rechnet ``DemoScenario`` rückwärts
    vom Startstand des Szenarios (``start_km``), sodass die Fahrten lückenlos anschließen."""
    car = _car_day(ctx)
    scenario_trips = ctx.sc.get("trips", [])
    if car is None or not scenario_trips:
        return []
    first_scenario = ctx.today - datetime.timedelta(days=max(t["days_ago"] for t in scenario_trips))
    t = ctx.t["trips"]
    trips = []
    for year, month in ctx.months():
        for index, base in enumerate(t["days"]):
            day = _workday(_clip(year, month, base + _jitter(year, month, f"td{index}", 5)), ctx.today)
            if day is None or day <= car or day >= first_scenario:
                continue
            route = _pick(t["routes"], year, month, f"tr{index}")
            trips.append({**_stamp(ctx.today, day), "start_point": route["start"], "destination": route["destination"],
                          "reason": route["reason"], "distance_km": route["km"]})
    return trips


def _worktimes(ctx):
    """Arbeitszeiten der letzten 12 Monate für die Person des Szenarios: 3–4 Tage je Woche nach der Wochentagstabelle,
    dazu Urlaub, Krankheit und feste Feiertage."""
    w = ctx.t["worktimes"]
    entries = []
    day = ctx.today - datetime.timedelta(days=364)
    while day <= ctx.today:
        iso_year, week, weekday = day.isocalendar()
        entry = None
        if weekday <= 5:
            if any((day.month, day.day) == (m, d) for m, d, _name in w["holidays"]):
                name = next(n for m, d, n in w["holidays"] if (m, d) == (day.month, day.day))
                entry = _absence("holiday", name)
            elif any((m1, d1) <= (day.month, day.day) <= (m2, d2) for m1, d1, m2, d2 in w["vacation"]):
                entry = _absence("vacation", "Urlaub")
            elif [day.month, day.day] in w["sick"]:
                entry = _absence("sick", "Krank")
            else:
                pattern = w["week_patterns"][week % len(w["week_patterns"])]
                if day.weekday() in pattern:
                    entry = _work_entry(ctx, w, iso_year, week, day, pattern)
        if entry is not None:
            entries.append({"date": day.isoformat(), **entry})
        day += datetime.timedelta(days=1)
    return entries


def _absence(kind, note):
    """Urlaub, Krankheit oder Feiertag: ganztägig, ohne Zeiten und Kunden."""
    return {"kind": kind, "customer_number": None, "start_time": "", "end_time": "", "pause_minutes": 0,
            "location_mode": "own", "note": note}


def _work_entry(ctx, w, iso_year, week, day, pattern):
    start, end = _pick(w["starts"], iso_year, week, f"s{day.weekday()}")
    office = day.weekday() == pattern[-1] and _crc(iso_year, week, "office") % 3 == 0
    note = _pick(w["office_notes"] if office else w["notes"], iso_year, week, f"n{day.weekday()}")
    return {"kind": "work", "customer_number": None if office else _pick(ctx.t["customers"]["worktime"], iso_year, week, "wc"),
            "start_time": start, "end_time": end, "pause_minutes": _pick(w["pauses"], iso_year, week, f"p{day.weekday()}"),
            "location_mode": "own" if office else "customer", "note": note}


# ── Zusammenbau ──────────────────────────────────────────────────────────────

def build_history(today, profile="full"):
    """Die Vorgaben für ``DemoScenario(extra=…)``: ``full`` = drei volle Vorjahre und das laufende Jahr bis ``today``, ``test`` =
    ein volles Vorjahr und das laufende Jahr. Rein: dieselben Eingaben ergeben denselben Rückgabewert; die Datenbank
    wird nie angefasst."""
    ctx = _Ctx(today, profile)
    one_off = _extended_scenario_items(ctx) + _one_offs(ctx) + _cash_items(ctx)
    one_off.sort(key=lambda item: (item["date"], item["text"]))
    history = {
        "profile": profile, "first_year": ctx.first_year, "accounts": dict(ctx.t["accounts"]),
        "invoices": _invoices(ctx), "quotes": _quotes(ctx), "one_off": one_off, "splits": _splits(ctx),
        "trips": _trips(ctx), "assets": _assets(ctx), "transfers": _transfers(ctx), "worktimes": _worktimes(ctx),
        "open_bank": _open_bank(ctx),
    }
    entries = _tax_entries(ctx)
    if entries:                       # nur das Profil ``full``: Die Ausgabe des Profils ``test`` bleibt, wie sie war
        history["tax_entries"] = entries
    return history
