# Offene Fachfragen

Fragen zu Steuern, Kontierung und DATEV, die bei der Entwicklung offen geblieben sind. Die App
funktioniert auch ohne die Antworten; sie verhält sich an diesen Stellen vorsichtig und sagt es.
Keine der Aussagen hier ist Steuerberatung.

**Du kennst eine Antwort?** Dann freuen wir uns über ein
[Issue](https://github.com/NobseVomBerg/uBuchhaltung/issues) – am besten mit einer Quelle (Gesetz,
Verwaltungsanweisung, Kontenrahmen, Steuerschlüsseltabelle) und dem Jahr, für das sie gilt. Eine
Antwort wird vor der Umsetzung geprüft. Bitte keine echten Mandanten- oder Belegdaten anhängen.

Angaben zum Kontenrahmen beziehen sich auf den SKR04, Stand 2026.

## DATEV-Buchungsstapel

Der Export bildet einen Stapel nach, wie ihn verbreitete Buchhaltungsprogramme schreiben. Ob eine
Kanzlei ihn so annimmt, ist nicht bestätigt.

1. **Steuerschlüssel:** Werden die dreistelligen Schlüssel (401/402 Vorsteuer, 101/102 Umsatzsteuer)
   angenommen, oder erwartet die Kanzlei die einstelligen (9/8, 3/2)?
2. **Automatikkonten:** 4400 (Erlöse 19 %), 4300 (Erlöse 7 %) und die Skonto-Konten 4736/4731 rechnen
   die Steuer selbst heraus. Darf an einer Zeile auf ein Automatikkonto zusätzlich ein Steuerschlüssel
   stehen, oder muss das Feld leer bleiben?
3. **Rechnungskette bei Ist-Versteuerung:** Die Forderung („Debitor an 4405“) und die Umbuchung bei
   Zahlung („4405 an 4400“) stehen ohne Schlüssel im Stapel, der Steuersatz nur in der Spalte für den
   Steuersatz. Ist das die erwartete Form?
4. **Erstattung auf ein Aufwandskonto:** Trägt sie den Vorsteuer-Schlüssel der ursprünglichen Ausgabe?
5. **Schlüssel 490, 100 und 121:** Wofür werden sie in der Praxis gebraucht (z. B. 121 bei der
   Privatnutzung „2100 an 4640“)?
6. **Belegfeld 1:** Welche Länge ist zulässig – 12 Zeichen oder mehr?
7. **Zahlung mit Skonto:** Sie steht als zwei Zeilen gegen die Bank im Stapel (Zahlung und Skonto),
   zusammen ergeben sie den Betrag des Kontoauszugs. Ist das annehmbar, oder soll das Skonto gegen
   den Debitor gebucht sein?
8. **Übertrag zwischen eigenen Konten über das Monats- oder Jahresende:** Erwartet die Kanzlei ein
   Geldtransitkonto?
9. **Steuersätze ohne Schlüssel** (z. B. 5,5 %): Die Zeile trägt keinen Schlüssel und den Satz in der
   Spalte für den Steuersatz. Welcher Schlüssel wäre richtig?

## Skonto

10. **Skonto bei 0 %, steuerfreien Umsätzen und Kleinunternehmern:** Die App bucht alle drei auf 4730
    „Gewährte Skonti“. Der Kontenrahmen führt daneben 4734 „Gewährte Skonti 0 % USt“, 4701–4705
    „Erlösschmälerungen für steuerfreie Umsätze …“ und – neu ab 2026 – 4707 „Erlösschmälerungen für
    steuerfreie Umsätze nach § 19 Abs. 1 UStG“. Welches Konto gehört zu welchem Fall? (Betrag, Steuer
    und Gewinn sind in allen Varianten gleich; es geht um die Zuordnung in Umsatzsteuererklärung und
    DATEV.)
11. **Skonto bei Sätzen ohne eigenes Skonto-Konto** (5,5 %, Durchschnittssätze nach § 24 UStG, 16 %
    und 5 % aus 2020): Auf welches Konto und mit welchem Schlüssel wird gebucht? Die App lehnt Skonto
    für diese Sätze bisher ab.
12. **Mehrere Steuersätze auf einer Rechnung:** Die App teilt das Skonto im Verhältnis der
    Bruttobeträge je Satz auf und rundet je Satz auf den Cent. Gibt es eine Vorgabe, wohin ein
    Rest-Cent gehört?

## Einnahmenüberschussrechnung

13. **Abgrenzung nach Datum:** Ein Buchungssatz zählt mit seinem eigenen Datum, auch wenn die
    zugehörige Bankbewegung in einem anderen Monat liegt. Maßgeblich für die EÜR ist der Zu- bzw.
    Abfluss – soll immer das Datum der Bankbewegung gelten?
14. **Umbuchungen ohne Geldfluss** zwischen Privat- und Erfolgskonten (z. B. Kfz-Privatnutzung
    „2100 an 4645“, privat bezahlte Betriebsausgabe „6815 an 2180“) erscheinen nicht in der EÜR der
    App. In welcher Form gehören sie hinein?
15. **Steuersätze 16 % und 5 % (zweites Halbjahr 2020):** Der Kontenrahmen kannte dafür eigene
    Steuerkonten. Die App führt sie auf den Konten für 19 % und 7 %. Betrifft nur importierte
    Altbestände.

## Anlagen

16. **Abgang einer Anlage:** Die AfA im Abgangsjahr wird voll statt zeitanteilig geplant, und der
    Restbuchwert wird nicht von selbst als Aufwand gebucht. Welche Buchungen erwartet die Praxis?
17. **Degressive AfA und geringwertige Wirtschaftsgüter:** Die App rechnet fest mit 25 % bzw. einer
    Grenze von 800 €. Beide Werte hängen vom Anschaffungsjahr ab – welche Tabelle der Jahre wäre zu
    hinterlegen?

## Umsatzsteuer-Voranmeldung

18. **Rundung der Bemessungsgrundlage:** Die Seite zeigt die Bemessungsgrundlage in vollen Euro und lässt
    die Cent weg (zur Null hin, auch bei negativen Beträgen). Ist das die richtige Richtung – und rechnet
    das Formular die Steuer selbst aus den vollen Euro? Die App zeigt die Steuer wie gebucht, auf den Cent.
19. **Vorsteuer über den Jahreswechsel:** Die App zählt die Vorsteuer mit dem Datum der Zahlung, nicht mit
    dem der Rechnung. Wie ist eine Rechnung vom Dezember zu behandeln, die im Januar bezahlt wird – und
    umgekehrt?
20. **Kennzahl für Skonti ohne Steuer, steuerfreie Umsätze und 0 % mit Steuerausweis:** Zu welcher
    Kennzahl gehören 4730, 4110 und Erlöse zu 0 % auf 4185? Die App ordnet sie keiner Kennzahl zu und
    nennt sie unter „Nicht zugeordnet“.
21. **Monatspflicht für Neugründer nach 2026:** Müssen Neugründer in den ersten Jahren wieder monatlich
    anmelden? Die App lässt Monat oder Quartal frei wählen und prüft nichts.
