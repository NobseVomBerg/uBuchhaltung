# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Öffnen und unverändert speichern: Eine Maske, die man ohne Änderung abschickt, ändert nichts.

Die Auswertungen (EÜR, DATEV, Dashboard) lesen die Form eines Buchungssatzes (``Bookings.EntryForm``);
die Masken zeigen sie und schreiben sie zurück. Hier wird jede Maske eines Bestands gerendert, mit
genau den angezeigten Werten abgeschickt (wie der Browser es tut), und danach sind **alle Spalten aller
Buchungen** wie vorher:

* jede Bankbewegung im Buchungssatz-Editor,
* jeder Buchungssatz in der Buchungsmaske,
* „vorhandene Buchung zuordnen“ (Konten, Betrag und Form der Buchung bleiben; jede andere Spalte –
  Steuer, Datum, Steuerdatum, Empfänger, Text, Beleg-Nr., Kategorie, Quellgruppe – ebenso, auch beim
  zweiten Speichern) und die Zeile wieder entfernen (eine importierte oder AfA-Buchung wird nur gelöst und ist
  danach wie vor dem Zuordnen, jede andere geht; sonst bleibt alles).

Der Bestand: ein migrierter (alle Schreibwege, Spiegel in beiden Konventionen, Konto 1000–1099, dazu
Sätze unter einer Bankbewegung, die das Konto der Bank mit 1460 und 1050 zusammenführen) und ein frisch
geschriebener mit denselben Zeilen, die ohne Form roh angelegt wurden. Jeweils mit der Kasse auf 1600,
auf 1460 und ohne Nummer – und nach geänderter Kontenliste: ein Bankkonto gelöscht, 1050 als eigenes
Bankkonto angelegt, die Kasse ohne SKR-Nummer.

Alle Namen und Werte sind erfunden.
"""
import os
from contextlib import closing
from decimal import Decimal

import pytest

from db import Database, _CoreMixin
from db.matching import MIRROR_FORMS
from tests import entry_form_world as world
from tests.entry_form_roundtrip import (
    adopt_row, differences, drop_row, open_form, snapshot, submit,
)

KASSEN = {1600: 'kasse-1600', 1460: 'kasse-1460', None: 'kasse-ohne-skr'}


def _build(tmp_path, kind, cash_number):
    path = str(tmp_path / 'buch.db')
    db = world.build(path, cash_number)
    world.add_roundtrip_rows(db)
    if kind == 'migriert':
        world.to_state_2000(path)
        _CoreMixin._initialized_dbs.discard(os.path.abspath(path))
        db = Database(db_name=path)                   # öffnen = migrieren
    # Jeder Satz trägt einen mitgebrachten BU-Schlüssel (wie nach einem Import; beim Migrieren aus 2.2.x entsteht die
    # Spalte leer, deshalb erst jetzt): Er übersteht jedes Öffnen und unveränderte Speichern wie jede andere Spalte –
    # ``snapshot`` vergleicht alle Spalten aller Buchungen, auch ``DatevKey``
    with closing(db._get_connection()) as conn:
        conn.execute("UPDATE Bookings SET DatevKey = 401 + (ID % 5) WHERE BookingType = 'entry'")
        conn.commit()
    return db


@pytest.fixture(params=['migriert', 'frisch'])
def kind(request):
    return request.param


@pytest.fixture(params=list(KASSEN), ids=list(KASSEN.values()))
def kasse(request):
    return request.param


@pytest.fixture
def bestand(tmp_path, kind, kasse):
    return _build(tmp_path, kind, kasse)


@pytest.fixture
def neuer_bestand(tmp_path, kind, kasse):
    """Baut den Bestand immer wieder neu (``neuer_bestand(n)``: nach den ersten n Schritten der
    Kontenänderung) – wer Buchungen löscht, braucht für den nächsten Schritt einen frischen."""
    def build(upto):
        directory = tmp_path / f'schritt-{upto}'
        directory.mkdir()
        db = _build(directory, kind, kasse)
        for step in STEPS[1:upto + 1]:
            _change_accounts(db, step)
        return db
    return build


def _accounts(db):
    return {a[1]: a for a in db.fetch_accounts()}


def _change_accounts(db, step):
    """Die Kontenliste ändert sich nach dem Buchen (kumulativ)."""
    if step == 'bankkonto-geloescht':
        db.delete_account(_accounts(db)['Giro Beta'][0])
    elif step == 'konto-1050-eigenes-bankkonto':
        db.insert_account('Giro Gamma', 'Owner', 'DE00GAMMA', 'BIC', 'Bank', is_cash=0,
                          skr_account=1050)
    elif step == 'kasse-ohne-skr':
        cash = next(a for a in db.fetch_accounts() if a[6] == 1)
        db.update_account(cash[0], cash[1], cash[2], cash[3], cash[4], cash[5], skr_account=None)


STEPS = ['unveraendert', 'bankkonto-geloescht', 'konto-1050-eigenes-bankkonto', 'kasse-ohne-skr']


def _ids(db, booking_type):
    with closing(db._get_connection()) as conn:
        return [row[0] for row in conn.execute(
            'SELECT ID FROM Bookings WHERE BookingType = ? ORDER BY ID', (booking_type,))]


def _names(db):
    return {coa_id: number for coa_id, number in db.get_coa_id_to_number_map().items()}


def _label(db, booking_id):
    with closing(db._get_connection()) as conn:
        return conn.execute(
            'SELECT BookingType, DateBooking, COALESCE(DocumentNumber, Text, "") FROM Bookings '
            'WHERE ID = ?', (booking_id,)).fetchone()


# ── jede Bankbewegung im Editor, jeder Satz in der Buchungsmaske ──────────────

# INHERENT: Der Editor zählt einen Satz als „Zahlung über das Konto der Bewegung“, sobald eines seiner Konten
# ein eigenes Bankkonto ist (``is_bank_effective``, die Kontenliste von jetzt – so schon in 2.0.x), und
# richtet dessen Gegenkonto beim Speichern auf das Konto der Bewegung aus. ALT-14 (6850 an 1050) hängt an der
# Bewegung eines anderen Kontos; wird 1050 ein eigenes Bankkonto, gehört der Satz nach dieser Regel zu dessen
# Bewegungen und wird beim Speichern umgehängt.
def test_opening_and_saving_a_bank_movement_without_a_change_changes_nothing(bestand):
    db = bestand
    problems = []
    for step in STEPS:
        _change_accounts(db, step)
        for bank_id in _ids(db, 'bank'):
            before = snapshot(db)
            post, _options = open_form(db, bank_id)
            submit(db, post)
            for line in differences(before, snapshot(db), _names(db)):
                if step == 'konto-1050-eigenes-bankkonto' and 'ALT-14 (' in line:
                    continue                          # siehe INHERENT
                problems.append(f'[{step}] Bankbewegung {bank_id} {_label(db, bank_id)[1:]}: {line}')
        if problems:                                  # eine Abweichung verfälscht alles Weitere
            break
    assert not problems, '\n'.join(problems[:20])


# Die Maske trägt das vorgewählte Bankkonto in einen eigenständigen Satz ein (bekannt, ``docs/aufgaben.md``:
# „Ein eigenständiger Buchungssatz bekommt beim Speichern in der Maske das vorgewählte Bankkonto“) –
# das ist nicht die Form und bleibt hier außen vor.
MASK_SETS_ACCOUNT = ('Account_ID',)


def _restore_account(db, booking_id, account_id):
    """Den Nebeneffekt der Maske zurücknehmen, damit er die nächsten Schritte nicht stört (ein
    Bankkonto lässt sich nicht löschen, solange ein Satz es führt)."""
    with closing(db._get_connection()) as conn:
        conn.execute('UPDATE Bookings SET Account_ID = ? WHERE ID = ?', (account_id, booking_id))
        conn.commit()


def test_opening_and_saving_an_entry_in_the_mask_without_a_change_changes_nothing(bestand):
    db = bestand
    problems = []
    for step in STEPS:
        _change_accounts(db, step)
        for entry_id in _ids(db, 'entry'):
            before = snapshot(db)
            post, _options = open_form(db, entry_id)
            submit(db, post)
            for line in differences(before, snapshot(db), _names(db), ignore=MASK_SETS_ACCOUNT):
                problems.append(f'[{step}] Satz {entry_id}: {line}')
            _restore_account(db, entry_id, before[entry_id]['Account_ID'])
        if problems:
            break
    assert not problems, '\n'.join(problems[:20])


# ── „vorhandene Buchung zuordnen“ und wieder entfernen ────────────────────────

def _stored(db, booking_id):
    with closing(db._get_connection()) as conn:
        return conn.execute(
            'SELECT COA_ID, CounterCOA_ID, Amount, EntryForm, ParentBooking_ID FROM Bookings '
            'WHERE ID = ?', (booking_id,)).fetchone()


# Spalten, die das Zuordnen ändern darf: die Verknüpfung – und bei einer Buchung, der ein Konto oder das
# Gegenkonto fehlt, die Konten samt Form und Betrag (ein leeres Gegenkonto füllt das Konto der Bank). Wann genau,
# legen die Prüfungen im Test fest; jede andere Spalte bleibt, wie sie ist.
ACCOUNT_COLUMNS = ('COA_ID', 'CounterCOA_ID', 'Amount', 'EntryForm')
ADOPTION_CHANGES = ('ParentBooking_ID',) + ACCOUNT_COLUMNS


def _plan_of_depreciations(db):
    with closing(db._get_connection()) as conn:
        return conn.execute('SELECT * FROM AssetDepreciations ORDER BY ID').fetchall()


def _made_outside_the_editor(db, row):
    """Eine importierte Buchung (Quellklammer) oder eine AfA-Buchung (ein Jahr des AfA-Plans verweist auf sie):
    Entfernt man ihre Zeile im Editor, wird sie nur abgehängt."""
    with closing(db._get_connection()) as conn:
        return bool(row['SourceGroup']) or bool(conn.execute(
            'SELECT 1 FROM AssetDepreciations WHERE Booking_ID = ?', (row['ID'],)).fetchone())


def test_assigning_an_existing_booking_keeps_its_accounts_amount_and_form(neuer_bestand):
    """Eine Buchung mit Konten auf beiden Seiten – Geldkonto vorne oder hinten, gleich welches (Bank der Bewegung,
    andere Bank, Kasse, 1460, 1050) – behält Konten, Betrag und Form, wenn man sie einer Bankbewegung zuordnet;
    nur ein leeres Gegenkonto füllt das Konto der Bank. Jede Buchung behält alle übrigen Spalten (Steuer, Datum,
    Steuerdatum, Empfänger …) – beim Zuordnen und bei jedem weiteren Speichern. Wieder entfernt wird eine
    importierte oder eine AfA-Buchung nur gelöst und ist danach wie vor dem Zuordnen (das AfA-Jahr bleibt
    gebucht; außer einem gefüllten Gegenkonto, das das ✕ nicht zurücknehmen kann), jede andere wird gelöscht;
    sonst bleibt alles."""
    problems = []
    for index, step in enumerate(STEPS):
        db = neuer_bestand(index)
        alpha = _accounts(db)['Giro Alpha'][0]
        bank_coa = db.get_coa_id_by_account_number(1800)                    # das SKR-Konto von Giro Alpha
        names = _names(db)
        candidates = [row[0] for row in db.get_unlinked_entry_bookings(limit=500)]
        assert candidates
        for candidate in candidates:
            coa, counter, amount, form, _parent = _stored(db, candidate)
            label = f'[{step}] {_label(db, candidate)[2]}'
            bank_id = db.insert_booking('2026-08-01', Decimal('1.00'), account_id=alpha,
                                        booking_type='bank', text='Zuordnungsziel')
            post, options = open_form(db, bank_id)
            option = next(o for o in options if o['value'] == str(candidate))

            before = snapshot(db)
            submit(db, adopt_row(post, option))
            after = snapshot(db)
            now = _stored(db, candidate)
            complete = coa is not None and counter is not None
            if now[4] != bank_id:
                problems.append(f'{label}: nicht der Bankbewegung zugeordnet')
            if complete and now[:4] != (coa, counter, amount, form):
                problems.append(f'{label}: Konten/Betrag/Form {(names.get(coa), names.get(counter), amount, form)} '
                                f'-> {(names.get(now[0]), names.get(now[1]), now[2], now[3])}')
            if (now[0], now[1]) == (coa, counter) and (now[2], now[3]) != (amount, form):
                problems.append(f'{label}: Betrag/Form {(amount, form)} -> {(now[2], now[3])}')
            for booking_id, row in before.items():
                if booking_id != candidate and after[booking_id] != row:
                    problems.append(f'{label}: Buchung {booking_id} geändert')
            # Der mitgebrachte BU-Schlüssel (``DatevKey``) bleibt – außer die Konten ändern sich: Mit dem gefüllten
            # Gegenkonto passt er nicht mehr zum gespeicherten Stand und fällt weg (``_datev_key_stale``)
            for line in differences({candidate: before[candidate]}, {candidate: after[candidate]}, names,
                                    ignore=ADOPTION_CHANGES + (() if complete else ('DatevKey',))):
                problems.append(f'{label} zugeordnet: {line}')
            if not complete and after[candidate]['DatevKey'] is not None:
                problems.append(f'{label}: der Schlüssel blieb trotz gefülltem Konto')

            # zugeordnet erneut speichern, ohne etwas zu ändern: bleibt, wie es ist
            again, _ = open_form(db, bank_id)
            settled = snapshot(db)
            submit(db, again)
            for line in differences(settled, snapshot(db), names):
                problems.append(f'{label} erneut gespeichert: {line}')

            # wieder entfernen (✕ und speichern): Eine importierte oder AfA-Buchung wird nur gelöst, jede andere
            # Buchung geht; sonst ändert sich nichts
            third, _ = open_form(db, bank_id)
            if str(candidate) not in third['split_id']:
                own = {a[7] for a in db.fetch_accounts() if a[7]}
                if not (names.get(coa) in own and names.get(counter) in own):    # ein Spiegel fehlt
                    problems.append(f'{label}: steht nach dem Zuordnen nicht im Editor')
                continue
            plan = _plan_of_depreciations(db)
            submit(db, drop_row(third, candidate))
            gone = snapshot(db)
            if not _made_outside_the_editor(db, before[candidate]):
                if candidate in gone:
                    problems.append(f'{label} blieb nach dem Entfernen')
            elif candidate not in gone:
                problems.append(f'{label} ging beim Entfernen verloren')
            else:
                # Alle Spalten wie vor dem Zuordnen. Ausgenommen ein leeres Gegenkonto: Das Zuordnen füllt es mit
                # dem Konto der Bank, und ob es vorher leer war, steht nach dem Zuordnen nirgends mehr – ebenso der
                # BU-Schlüssel, der mit dem gefüllten Konto wegfiel.
                filled = coa is not None and counter is None
                for line in differences({candidate: before[candidate]}, {candidate: gone[candidate]}, names,
                                        ignore=('CounterCOA_ID', 'EntryForm', 'DatevKey') if filled else ()):
                    problems.append(f'{label} entfernt: {line}')
                if filled and gone[candidate]['CounterCOA_ID'] != bank_coa:
                    problems.append(f'{label}: Gegenkonto nicht mit dem Konto der Bank gefüllt')
            if _plan_of_depreciations(db) != plan:
                problems.append(f'{label}: der AfA-Plan änderte sich beim Entfernen')
            for booking_id, row in settled.items():
                if booking_id != candidate and gone.get(booking_id) != row:
                    problems.append(f'{label}: Buchung {booking_id} änderte sich beim Entfernen')
        if problems:
            break
    assert not problems, '\n'.join(problems[:20])


def test_the_roundtrip_covers_the_difficult_shapes(bestand):
    """Der Bestand enthält, worauf die Maske hereinfallen kann – sonst wäre die Probe wertlos:
    Sätze mit dem Konto der Bank vorne und 1460/1050 hinten unter einer Bankbewegung, Geldkonten
    vorne ohne Bankbewegung, Spiegel."""
    db = bestand
    names = _names(db)
    with closing(db._get_connection()) as conn:
        rows = conn.execute("SELECT COA_ID, CounterCOA_ID, ParentBooking_ID, EntryForm FROM Bookings "
                            "WHERE BookingType = 'entry'").fetchall()
    shapes = {(names.get(coa), names.get(counter), parent is not None, form)
              for coa, counter, parent, form in rows}
    pairs = {(coa, counter, linked) for coa, counter, linked, _form in shapes}
    assert (1800, 1460, True) in pairs and (1800, 1050, True) in pairs
    assert (1460, 1800, True) in pairs and (1050, 1800, True) in pairs
    assert (1810, 4400, False) in pairs and (1800, 4400, False) in pairs
    assert any(form in MIRROR_FORMS for *_rest, form in shapes)
