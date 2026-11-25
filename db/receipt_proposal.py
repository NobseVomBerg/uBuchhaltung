# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Aus einem gelesenen Beleg eine Zeile für den Buchungssatz-Editor einer Bankbewegung vorschlagen – die reine Regel (seit 2.10.1).

Rein: kein Datenbankzugriff, keine Uhr. Die Eingaben liefern ``Database.receipt_proposal_inputs`` (Bankbewegung und Lesung),
``Database.recipient_history_groups`` und ``Database.vat_history_groups`` (die Historie), ``server.handlers.handle_receipt_proposal`` setzt
sie zusammen. Vorgeschlagen wird nur: Das Skript der Maske trägt die Werte in eine NEUE Zeile des Editors ein; gespeichert wird allein mit der Maske.

Aus dem Beleg kommen nur Steuersatz und Steuerbetrag – nie Absender, Rechnungsnummer, Dateiname, ``Text`` oder ``Raw`` (eine Lesung ist eine Lesung,
nicht der Inhalt des Belegs). Der Betrag ist der der Bankbewegung; er muss auf den Cent der Bruttobetrag der Lesung sein (``proposal_refusal``).
Jede Unsicherheit lässt das Feld leer und erzeugt einen festen Hinweissatz:

* Die Steuer (``proposal_tax``) nur bei genau einem Satz und beiden Proben (Netto + Steuer = Brutto, Netto × Satz = Steuer auf den Cent); sie bekommt
  das Vorzeichen des Betrags der Bank. Der Satz muss einer der Buchhaltung sein (``build_proposal``: Die erlaubten Sätze kommen als Menge von außen – die
  festen Sätze und die Einträge der Stammdaten –, 20 % oder 21 % gelten als falsch gelesen); Satz 0 heißt „ohne Steuer“.
* Das Konto (``steady_account``, ``choose_account``) nur, wenn die jüngsten Gruppen der Historie – eine Bankbewegung mit ihren Sätzen oder ein
  eigenständiger Satz – sich einig sind; die Historie kommt aus zwei Quellen (derselbe Empfänger der Bankbewegung, derselbe Absender des Belegs
  über die USt-IdNr.), die sich nicht widersprechen dürfen. Eine Suche über Schlagworte gibt es nicht.
* Das Konto muss ein Aufwands- oder Erlöskonto sein und im Editor wählbar (``check_account``, EINE Stelle: Kontonummer 4000–7999 ohne Wartekonten und
  Skonto-Konten, kein Zahlungskonto, nicht ausgeblendet): Ein Privat-, Anlage-, Steuer- oder Verrechnungskonto aus der Historie gäbe mit der Steuer des Belegs
  eine Buchung, die die EÜR anders liest."""
from dataclasses import dataclass, replace
from decimal import Decimal

from money import SCALE, tax_from_net

from .matching import PL_ACCOUNT_RANGE
from .receipt_matching import direction_fits, is_int, usable_gross

HISTORY_DEPTH = 3               # so viele (nicht leere) Gruppen der Historie betrachtet ``steady_account`` höchstens: die jüngsten
HISTORY_AGREE_MIN = 2           # … und so viele müssen es mindestens sein, alle mit demselben Konto
HISTORY_SCAN = 50               # so viele Treffer liest eine Historienabfrage höchstens, bevor sie zu Gruppen zusammenfasst
CENT = 10 ** (SCALE - 2)        # ein Cent in Minor Units

REFUSE_HAS_ROWS = 'Die Bankbewegung hat schon Buchungssätze – der Vorschlag füllt nur einen leeren Editor.'
REFUSE_CURRENCY = 'Die Bankbewegung ist nicht in Euro – kein Vorschlag.'
REFUSE_BANK_AMOUNT = 'Der Betrag der Bankbewegung ist nicht lesbar – kein Vorschlag.'
REFUSE_NO_READING = 'Zu diesem Beleg gibt es keine brauchbare Lesung (gelesen, in Euro, mit Bruttobetrag) – kein Vorschlag.'
REFUSE_OUTDATED = 'Die Lesung stammt von älteren Regeln – bitte den Beleg neu lesen.'
REFUSE_AMOUNT = 'Der gelesene Bruttobetrag weicht vom Betrag der Bankbewegung ab (Skonto, Teilzahlung?) – kein Vorschlag.'
REFUSE_DIRECTION = 'Die Zahlungsrichtung passt nicht zur Belegart (Rechnung oder Gutschrift) – kein Vorschlag.'
REASON_AMOUNT = 'Betrag aus der Bankbewegung'
REASON_TAX = 'Steuer aus der Lesung ({rate} %)'
REASON_TAX_NONE = 'laut Lesung ohne Steuer'
REASON_ACCOUNT_RECIPIENT = 'Konto aus früheren Buchungen desselben Empfängers'
REASON_ACCOUNT_SENDER = 'Konto aus früheren Buchungen zu Belegen mit derselben USt-IdNr.'
HINT_TAX = 'Steuer nicht übernommen (kein eindeutiger Satz oder die Proben stimmen nicht) – bitte prüfen.'
HINT_TAX_RATE = 'Steuer nicht übernommen: Der gelesene Satz ({rate} %) ist kein Steuersatz dieser Buchhaltung – bitte prüfen.'
HINT_NO_ACCOUNT = 'Kein Konto vorgeschlagen: Frühere Buchungen geben keine eindeutige Grundlage.'
HINT_ACCOUNT_CONFLICT = 'Kein Konto vorgeschlagen: Frühere Buchungen nennen verschiedene Konten.'
HINT_ACCOUNT_HIDDEN = 'Das Konto früherer Buchungen ist ausgeblendet – bitte ein Konto wählen.'
HINT_ACCOUNT_KIND = 'Kein Konto vorgeschlagen: Das Konto früherer Buchungen ist kein Aufwands- oder Erlöskonto – bitte wählen.'
DONE_TEXT = 'Vorschlag aus dem Beleg eingetragen – nichts gespeichert. Bitte prüfen und mit „Aktualisieren“ speichern.'

#: Die Begründungen eines Kontos aus der Historie: Sie fallen weg, wenn das Konto verworfen wird (``check_account``)
ACCOUNT_REASONS = (REASON_ACCOUNT_RECIPIENT, REASON_ACCOUNT_SENDER)


@dataclass(frozen=True)
class Proposal:
    """Das Ergebnis: ``ok`` und ``message`` (bei ``ok`` ``DONE_TEXT``, sonst der Satz der Ablehnung), der Betrag der Bankbewegung in Minor Units, der Steuersatz
    als Anteil (``0.19``) und der Steuerbetrag mit dem Vorzeichen des Betrags (beides ``None``: ohne Steuer oder nicht sicher), das Konto (``None``: keines),
    die Begründungen und die Hinweise (feste Sätze). Eine Ablehnung trägt nur ``message``."""
    ok: bool
    message: str
    amount_minor: int | None
    tax_rate: Decimal | None
    tax_minor: int | None
    coa_id: int | None
    reasons: tuple[str, ...]
    hints: tuple[str, ...]


def rate_percent_text(rate, mark='.') -> str:
    """Ein Steuersatz als Anteil (``0.19``) in Prozent ohne überflüssige Stellen: ``19``, ``5.5`` (mit ``mark=','``: ``5,5``); ohne Satz ``''``."""
    if rate is None:
        return ''
    return format((Decimal(str(rate)) * 100).normalize(), 'f').replace('.', mark)


def rate_bp(rate) -> int:
    """Ein Steuersatz als Anteil (``Decimal('0.19')``) in Hundertstel Prozent (1900): die ganze Zahl, in der die Stammdaten „Steuersätze“ ihn führen (``TaxEntry.bp``).
    ``proposal_tax`` liefert nur Sätze mit höchstens zwei Nachkommastellen in Prozent – die Zahl ist exakt."""
    return int(Decimal(str(rate)) * 10000)


def proposal_refusal(reading, reader_version, bank_amount, bank_currency, has_children) -> str | None:
    """Der feste Satz, warum es keinen Vorschlag gibt – oder ``None``. Geprüft wird in dieser Reihenfolge: Die Bankbewegung hat schon Sätze
    (``has_children``: auch ein gesperrter Zahlungssatz ist einer); ihre Währung ist nicht Euro (leer gilt als Euro); ihr Betrag ist keine ganze Zahl
    ungleich 0; die Lesung ist nicht brauchbar (``usable_gross``: Status ``ok``, Währung leer oder ``EUR``, Brutto ungleich 0); sie stammt von älteren
    Regeln als ``reader_version``; ihr Brutto ist nicht der Betrag der Bank auf den Cent (Skonto, Teilzahlung); die Zahlungsrichtung passt nicht zur Art
    (``direction_fits``: Rechnung erwartet einen Ausgang, Gutschrift einen Eingang)."""
    if has_children:
        return REFUSE_HAS_ROWS
    if ('' if bank_currency is None else str(bank_currency).strip().upper()) not in ('', 'EUR'):
        return REFUSE_CURRENCY
    if not is_int(bank_amount) or bank_amount == 0:
        return REFUSE_BANK_AMOUNT
    gross = usable_gross(reading)
    if gross is None:
        return REFUSE_NO_READING
    version = reading.get('ReaderVersion')
    if not is_int(version) or version < reader_version:
        return REFUSE_OUTDATED
    if abs(gross) != abs(bank_amount):
        return REFUSE_AMOUNT
    if not direction_fits(reading.get('Kind'), bank_amount):
        return REFUSE_DIRECTION
    return None


def proposal_tax(reading) -> tuple[Decimal, int] | None:
    """Satz (als Anteil) und Steuerbetrag (Minor Units, OHNE Vorzeichen) aus der Lesung – oder ``None``, wenn sie sich nicht sicher übernehmen lassen.
    Liest nur ``TaxRate``, ``NetMinor``, ``TaxMinor`` und ``GrossMinor``. Übernommen wird nur, wenn alles gilt: ``TaxRate`` ist eine Zahl (kein ``bool``)
    von 0 bis 1 – die Leser setzen sie nur, wenn der Beleg genau einen Satz nennt – mit höchstens zwei Nachkommastellen in Prozent; Brutto, Netto und Steuer
    sind ganze Zahlen (``gross > 0``, ``net >= 0``, ``tax >= 0``); Netto + Steuer = Brutto; Netto × Satz weicht von der Steuer um höchstens einen Cent ab
    (dieselbe Regel wie die Satzprobe des Textlesers). Bei Satz 0 muss die Steuer 0 sein; das Ergebnis ist dann ``(Decimal(0), 0)``: ohne Steuer.
    Wirft nie – jeder schiefe Wert ergibt ``None``."""
    if not isinstance(reading, dict):
        return None
    rate = reading.get('TaxRate')
    gross, net, tax = reading.get('GrossMinor'), reading.get('NetMinor'), reading.get('TaxMinor')
    if isinstance(rate, bool) or not isinstance(rate, (int, float, Decimal)):
        return None
    if not (is_int(gross) and is_int(net) and is_int(tax)) or gross <= 0 or net < 0 or tax < 0 or net + tax != gross:
        return None
    try:
        fraction = Decimal(str(rate))
        if not fraction.is_finite() or not 0 <= fraction <= 1:
            return None
        percent = fraction * 100
        if percent != percent.quantize(Decimal('0.01')):
            return None
        if percent == 0:
            return (Decimal(0), 0) if tax == 0 else None
        return (fraction, tax) if abs(tax_from_net(net, percent) - tax) <= CENT else None
    except ArithmeticError:
        return None


def _steady_row(group) -> int | None:
    """Das Konto der Gruppe, wenn sie genau einen Satz hat, der Geld bewegt (keine Umbuchung), kein Zahlungssatz ist und ein ganzzahliges Konto trägt."""
    if not isinstance(group, list) or len(group) != 1 or not isinstance(group[0], dict):
        return None
    only = group[0]
    coa_id = only.get('coa_id')
    if only.get('nobank') or only.get('payment') or not is_int(coa_id):
        return None
    return coa_id


def steady_account(groups) -> int | None:
    """Das Konto, auf das sich die Historie verlassen darf – oder ``None``. ``groups``: eine Liste von Gruppen (je eine Liste von Zeilen-``dict`` mit
    ``coa_id``, ``nobank``, ``payment``), die jüngste zuerst. Leere Gruppen werden übersprungen; betrachtet werden die jüngsten bis zu ``HISTORY_DEPTH``
    nicht leeren, und es müssen mindestens ``HISTORY_AGREE_MIN`` sein. Jede hat genau einen Satz, der Geld bewegt, kein Zahlungssatz ist und ein
    ganzzahliges Konto trägt (``_steady_row``); alle nennen dasselbe. Sonst ``None``."""
    considered = [group for group in groups if group][:HISTORY_DEPTH]
    if len(considered) < HISTORY_AGREE_MIN:
        return None
    accounts = {_steady_row(group) for group in considered}
    return accounts.pop() if len(accounts) == 1 and None not in accounts else None


def choose_account(recipient_groups, sender_groups) -> tuple[int | None, tuple[str, ...], tuple[str, ...]]:
    """Das Konto aus beiden Quellen der Historie (derselbe Empfänger der Bankbewegung, derselbe Absender des Belegs über die USt-IdNr.) als
    ``(Konto, Begründungen, Hinweise)``. Sind beide belastbar (``steady_account``) und verschieden: kein Konto, ``HINT_ACCOUNT_CONFLICT``. Sonst gilt die
    Quelle des Empfängers (bei gleichem Konto der Quelle des Absenders mit beiden Begründungen), sonst die des Absenders; sonst kein Konto, ``HINT_NO_ACCOUNT``."""
    recipient, sender = steady_account(recipient_groups), steady_account(sender_groups)
    if recipient is not None and sender is not None and recipient != sender:
        return None, (), (HINT_ACCOUNT_CONFLICT,)
    if recipient is not None:
        return recipient, (REASON_ACCOUNT_RECIPIENT, REASON_ACCOUNT_SENDER) if sender is not None else (REASON_ACCOUNT_RECIPIENT,), ()
    if sender is not None:
        return sender, (REASON_ACCOUNT_SENDER,), ()
    return None, (), (HINT_NO_ACCOUNT,)


def is_result_account(account_number, coa_id, payment_coa_ids, excluded_numbers) -> bool:
    """Ist das ein Aufwands- oder Erlöskonto? Die Kontonummer ist eine ganze Zahl (kein ``bool``) in ``PL_ACCOUNT_RANGE`` (4000–7999), nicht in ``excluded_numbers`` (die
    Wartekonten der offenen Posten und die Skonto-Konten: Sie liegen darin, sind aber keine Aufwands- oder Erlöskonten), und ihre ID ist kein Zahlungskonto
    (``payment_coa_ids``: die eigenen Bank- und Kassenkonten und das Verrechnungskonto). Rein: Nummer und Mengen kommen vom Aufrufer."""
    if type(account_number) is not int:
        return False
    low, high = PL_ACCOUNT_RANGE
    return low <= account_number <= high and account_number not in excluded_numbers and coa_id not in payment_coa_ids


def check_account(proposal, accounts, payment_coa_ids, excluded_numbers) -> Proposal:
    """Das Konto eines Vorschlags prüfen, ehe er die Maske erreicht – EINE Stelle für die Art und für „ausgeblendet“. ``accounts``: ``{COA-ID: (Kontonummer, im Editor wählbar)}``
    aus dem Kontenrahmen. Ein Konto, das kein Aufwands- oder Erlöskonto ist (``is_result_account``), wird verworfen: kein Konto, ``HINT_ACCOUNT_KIND``; ein ausgeblendetes
    oder unbekanntes ebenso mit ``HINT_ACCOUNT_HIDDEN`` (die Art zuerst: genau EIN Hinweis). Die Begründungen des Kontos fallen weg, der Hinweis kommt hinter die vorhandenen;
    alles andere bleibt. Ohne Konto und bei einer Ablehnung kommt derselbe Vorschlag zurück."""
    if proposal.coa_id is None:
        return proposal
    known = accounts.get(proposal.coa_id)
    if known is None:
        hint = HINT_ACCOUNT_HIDDEN
    elif not is_result_account(known[0], proposal.coa_id, payment_coa_ids, excluded_numbers):
        hint = HINT_ACCOUNT_KIND
    elif not known[1]:
        hint = HINT_ACCOUNT_HIDDEN
    else:
        return proposal
    return replace(proposal, coa_id=None, reasons=tuple(reason for reason in proposal.reasons if reason not in ACCOUNT_REASONS), hints=proposal.hints + (hint,))


def build_proposal(reading, reader_version, bank_amount, bank_currency, has_children, recipient_groups, sender_groups, allowed_rate_bps) -> Proposal:
    """Der Vorschlag: Ablehnung (``proposal_refusal``) oder die Zeile mit dem Betrag der Bank, der Steuer der Lesung (Vorzeichen wie der Betrag) und dem
    Konto der Historie. Begründungen: Betrag, Steuer, Konto; Hinweise in der Reihenfolge Steuer, Konto – alles feste Sätze, nie ein Wert aus dem Beleg.

    ``allowed_rate_bps``: die Sätze der Buchhaltung in Hundertstel Prozent (``rate_bp``) – die festen Sätze mit Steuerkonten und die Einträge der Stammdaten, vom Aufrufer
    gelesen. Ein Satz, der nicht dabei ist (20 %, 21 %, ein falsch gelesener), wird nicht übernommen: die Zeile ohne Steuer, mit ``HINT_TAX_RATE``. Satz 0 („ohne Steuer“)
    braucht keinen Eintrag. Das Konto prüft ``check_account`` danach."""
    refusal = proposal_refusal(reading, reader_version, bank_amount, bank_currency, has_children)
    if refusal is not None:
        return Proposal(False, refusal, None, None, None, None, (), ())
    sign = -1 if bank_amount < 0 else 1
    reasons, hints = [REASON_AMOUNT], []
    rate = tax_minor = None
    found = proposal_tax(reading)
    if found is None:
        hints.append(HINT_TAX)
    elif found[0] == 0:
        reasons.append(REASON_TAX_NONE)
    elif rate_bp(found[0]) not in allowed_rate_bps:
        hints.append(HINT_TAX_RATE.format(rate=rate_percent_text(found[0], ',')))
    else:
        rate, tax_minor = found[0], sign * found[1]
        reasons.append(REASON_TAX.format(rate=rate_percent_text(rate, ',')))
    coa_id, account_reasons, account_hints = choose_account(recipient_groups, sender_groups)
    return Proposal(True, DONE_TEXT, bank_amount, rate, tax_minor, coa_id, tuple(reasons) + account_reasons, tuple(hints) + account_hints)
