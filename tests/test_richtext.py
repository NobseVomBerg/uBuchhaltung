# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Richtext-Filter ``richtext.sanitize_richtext`` (2.0.7).

Einleitungs- und Schlusstext der Angebote kommen aus einem ``contenteditable``-Feld (auch mit aus Webseiten
eingefügtem Text) und liegen als Altbestand in der Datenbank. Sie laufen nur über die Positivliste
(b, strong, i, em, u, p, br, ul, ol, li, div, span, ohne Attribute, höchstens ``MAX_DEPTH`` Ebenen tief):
beim Speichern UND bei jeder Ausgabe – Formular und PDF-Satz –, auch für Altbestand.

Der Filter baut seine Ausgabe aus den Tokens von ``html.parser`` neu auf. Für eindeutige Fälle steht hier
die genaue Ausgabe; bei absichtlich verdrehtem Markup (das ein Parser-Update anders lesen dürfte) zählen
die Eigenschaften: nur erlaubte Tags, kein Attribut, kein ``<`` oder ``>`` im Text, wohlgeformt, idempotent.
"""
import json
import random
import re
import time

import pytest

import richtext
from export.pdf_document import _richtext_paragraphs
from export.pdf_invoice import generate_invoice_pdf
from richtext import ALLOWED_TAGS, MAX_DEPTH, sanitize_richtext
from server import handlers
from server.pages_quote import PageQuote
from tests.document_helpers import add_contacts, add_document, as_user, row, set_column
from tests.dom_helpers import Dom
from tests.test_quote import _all_content_bytes

TAG = re.compile(r'<(/?)([a-z0-9]+)>')


def check_well_formed(out):
    """Eigenschaften jeder Ausgabe: nur erlaubte Tags ohne Attribut, sonst kein ``<``/``>``, richtig
    verschachtelt, ``<br>`` ohne Ende-Tag."""
    assert '<' not in TAG.sub('', out) and '>' not in TAG.sub('', out), out
    stack = []
    for match in TAG.finditer(out):
        closing, name = match.group(1), match.group(2)
        assert name in ALLOWED_TAGS, out
        if name == 'br':
            assert not closing, out
        elif closing:
            assert stack and stack.pop() == name, out
        else:
            stack.append(name)
    assert not stack, out                                 # alles geschlossen


# ── eindeutige Fälle: genaue Ausgabe ─────────────────────────────────────────
UNCHANGED = [
    '<b>fett</b> <i>kursiv</i> <u>unter</u>',
    '<strong>stark</strong> <em>betont</em>',
    '<p>Absatz</p><br><ul><li>eins</li><li>zwei</li></ul>',
    '<ol><li>x</li></ol><div><span>a</span></div>',
    'Nur Text ohne jedes Markup.',
    'Umlaute äöüß € und „Anführungszeichen“',
    '1 &lt; 2 &amp;&amp; 3 &gt; 2',
    'a &amp; b &lt;script&gt;alert(1)&lt;/script&gt;',     # bereits maskiert: bleibt Text
]
REWRITTEN = [
    ('<B>gross</B>', '<b>gross</b>'),
    ('<BR>', '<br>'), ('<br/>', '<br>'), ('x<br />y', 'x<br>y'),
    ('<p onclick="alert(1)">Hallo</p>', '<p>Hallo</p>'),
    ('<b style="color:red">fett</b>', '<b>fett</b>'),
    ('<span class="a" id="b" data-x=1>t</span>', '<span>t</span>'),
    ('<b/onclick=alert(1)>x</b>', '<b>x</b>'),
    ('<li title="a>b" onclick=x>t</li>', '<li>t</li>'),
    ('<a href=javascript:alert(1)>klick</a>', 'klick'),
    ('<a href="javascript:alert(1)">klick</a>', 'klick'),
    ('<iframe src="http://x"></iframe>Text', 'Text'),
    ('<iframe src="http://x">inner</iframe>Text', 'Text'),
    ('<svg onload=alert(1)>a</svg>b', 'b'),
    ('<math><mi>x</mi></math>t', 't'),
    ('<object data=x>o</object>t', 't'),
    ('<style>body{display:none}</style>x', 'x'),
    ('<script>alert(1)</script>Text', 'Text'),
    ('<b>unbalanciert', '<b>unbalanciert</b>'),
    ('<p>a<b>b', '<p>a<b>b</b></p>'),
    ('</p>überzählig</b>', 'überzählig'),
    ('<b><i>x</b>y</i>', '<b><i>x</i></b>y'),
    ('<!-- Kommentar --><b>x</b>', '<b>x</b>'),
    ('<b>a<!-- c -->b</b>', '<b>ab</b>'),
    ('<!DOCTYPE html><b>x</b>', '<b>x</b>'),
    ('1 < 2 && 3 > 2', '1 &lt; 2 &amp;&amp; 3 &gt; 2'),
    ('Er sagte "Hallo" & \'ging\'', 'Er sagte "Hallo" &amp; \'ging\''),
    ('x\xa0y', 'x&nbsp;y'),
    ('<p>a</p>\n  ', '<p>a</p>'),
]
EMPTY = [None, '', '   ', '\n\t', '<script>alert(1)</script>', '<img src=x onerror=alert(1)>',
         '<!-- nur ein Kommentar -->', '<svg onload=alert(1)>', '<style>p{}</style>', '<?php echo 1 ?>']


@pytest.mark.parametrize('text', UNCHANGED)
def test_allowed_markup_and_plain_text_stay_unchanged(text):
    assert sanitize_richtext(text) == text
    check_well_formed(text)


@pytest.mark.parametrize('text, expected', REWRITTEN)
def test_attributes_and_everything_outside_the_list_are_removed(text, expected):
    assert sanitize_richtext(text) == expected
    check_well_formed(expected)


@pytest.mark.parametrize('text', EMPTY, ids=repr)
def test_nothing_left_is_none(text):
    assert sanitize_richtext(text) is None


def test_not_a_string_is_read_as_text():
    assert sanitize_richtext(123) == '123'


@pytest.mark.parametrize('text', UNCHANGED + [t for t, _ in REWRITTEN])
def test_a_second_pass_changes_nothing(text):
    once = sanitize_richtext(text)
    assert sanitize_richtext(once) == once


@pytest.mark.parametrize('text', [
    '<scr<script>x</script>ipt>alert(5)</script>',       # Tag in einem Tag
    '<b <script>alert(1)</script>>x</b>',
    '<<b>b</b>', '<b>b</b >', '< b>x</ b>',
    '<scr\x00ipt>alert(1)</scr\x00ipt>',
    '<noscript><p title="</noscript><img src=x onerror=alert(1)>">',
    '<textarea><b>x</b></textarea><title><b>y</b></title>',
    '<plaintext><b>x</b>',
    '<svg><script>alert(1)</script></svg><b>x</b>',
    '<![CDATA[x]]><b>y</b>',
    '"><img src=x onerror=alert(1)>',
    "'><svg/onload=alert(1)>",
    '<math><mtext></p><img src=x onerror=alert(1)></mtext></math>',
])
def test_twisted_markup_yields_only_inert_output(text):
    out = sanitize_richtext(text)
    if out is not None:
        check_well_formed(out)                            # nur erlaubte Tags, kein Attribut, kein loses < oder >
        assert sanitize_richtext(out) == out


def test_a_script_between_allowed_elements_is_dropped_and_its_neighbours_are_kept():
    assert sanitize_richtext('<b>vor</b><script>alert(1)</script><i>nach</i>') == '<b>vor</b><i>nach</i>'


def test_text_after_a_void_embed_element_is_kept():
    # <embed> ist ein leeres Element ohne Ende-Tag: nur das Tag fällt weg, der Text danach bleibt
    # (früher verwarf der Filter alles bis zu einem </embed>, das nie kommt).
    assert sanitize_richtext('<b>vor</b><embed src=x>Rest<i>mehr</i>') == '<b>vor</b>Rest<i>mehr</i>'


def test_server_module_reexports_the_filter():
    import server.richtext
    assert server.richtext.sanitize_richtext is richtext.sanitize_richtext
    assert server.richtext.ALLOWED_TAGS is richtext.ALLOWED_TAGS and server.richtext.MAX_DEPTH == MAX_DEPTH


def test_the_allowed_tags_are_the_documented_ones():
    assert ALLOWED_TAGS == {'b', 'strong', 'i', 'em', 'u', 'p', 'br', 'ul', 'ol', 'li', 'div', 'span'}


# ── Zufallstest ──────────────────────────────────────────────────────────────
FRAGMENTS = [
    '<b>', '</b>', '<i>', '</i>', '<u>', '</u>', '<p>', '</p>', '<br>', '<br/>', '</br>', '<ul>', '</ul>',
    '<ol>', '</ol>', '<li>', '</li>', '<div>', '</div>', '<span>', '</span>', '<strong>', '</strong>', '<em>',
    '</em>', '<a href=javascript:alert(1)>', '</a>', '<script>', '</script>', '<style>', '</style>',
    '<iframe src=x>', '</iframe>', '<img src=x onerror=alert(1)>', '<svg onload=alert(1)>', '</svg>', '<math>',
    '</math>', '<object>', '</object>', '<scr<script>x</script>ipt>', '<p onclick="alert(1)">',
    '<b style="x:y">', '<b/onclick=alert(1)>', '<b ', '<', '>', '"', "'", '=', '&', '&lt;', '&amp;',
    '&#60;script&#62;', '&nbsp;', '<!--', '-->', '<!-- c -->', '<![CDATA[', ']]>', '<?', '?>',
    '<!DOCTYPE html>', 'abc', 'Text', ' ', '\n', '\t', '\x00', '\xa0', '<noscript>', '</noscript>',
    '<textarea>', '</textarea>', '<title>', '</title>', '<plaintext>', '<template>', '</template>',
    '<form action=x>', '<input value=">">', '<B>', '</B>', '<Script>', '</SCRIPT >', '< b>', '</ b>', 'ä', '€',
    '<a', '"onclick="', '<svg><script>', '<svg><b>',
]


def test_random_input_always_yields_a_well_formed_idempotent_result():
    rnd = random.Random(20260711)                         # fester Startwert: derselbe Lauf jedes Mal
    for _ in range(2000):
        text = ''.join(rnd.choice(FRAGMENTS) for _ in range(rnd.randint(1, 14)))
        out = sanitize_richtext(text)
        if out is None:
            continue
        check_well_formed(out)                            # nur erlaubte Tags, kein `=` oder `<` im Tag
        assert sanitize_richtext(out) == out, text


# ── Laufzeit und Tiefe ───────────────────────────────────────────────────────
def test_unclosed_elements_with_stray_end_tags_take_linear_time():
    # Früher quadratisch (~12 s): zu jedem Ende-Tag wurde die Liste der offenen Elemente durchsucht
    start = time.perf_counter()
    out = sanitize_richtext('<b>' * 40000 + '</i>' * 40000)
    elapsed = time.perf_counter() - start
    assert elapsed < 2, f'{elapsed:.1f} s'
    check_well_formed(out)


@pytest.mark.parametrize('text', [
    '<b>' * 40000 + '</b>' * 40000,
    '<i>' * 40000 + '<b>' * 40000,
    '</b>' * 80000,
    ('<b>x</i>' * 20000),
    '<p><b><i>' * 15000,
])
def test_large_inputs_stay_fast(text):
    start = time.perf_counter()
    out = sanitize_richtext(text)
    assert time.perf_counter() - start < 2
    check_well_formed(out) if out else None


def test_nesting_is_limited_to_max_depth_and_the_text_remains():
    assert MAX_DEPTH == 50
    out = sanitize_richtext('<b>' * 60 + 'Text' + '</b>' * 60)
    assert out == '<b>' * 50 + 'Text' + '</b>' * 50       # genau 50 Ebenen, der Text bleibt
    check_well_formed(out)
    assert sanitize_richtext(out) == out                  # idempotent


def test_nesting_over_the_limit_keeps_text_of_the_dropped_levels():
    out = sanitize_richtext('<div>' * 60 + 'innen' + '</div>' * 60 + 'außen')
    assert out.count('<div>') == 50 and out.count('</div>') == 50
    assert out.endswith('</div>außen') and 'innen' in out
    check_well_formed(out)


def test_the_limit_counts_open_elements_not_how_many_there_are():
    flat = '<b>x</b>' * 1000 + '<br>' * 200
    assert sanitize_richtext(flat) == flat                # nacheinander: nichts fällt weg
    mixed = '<ul>' * 49 + '<li>tief</li>' + '</ul>' * 49
    assert sanitize_richtext(mixed) == mixed              # 50 Ebenen sind erlaubt
    too_deep = '<ul>' * 50 + '<li>tief</li>' + '</ul>' * 50
    assert sanitize_richtext(too_deep) == '<ul>' * 50 + 'tief' + '</ul>' * 50     # das <li> auf Ebene 51 fällt weg


# ── Speichern und Ausgabe ────────────────────────────────────────────────────
HOSTILE_TEXTS = [
    'Hallo<script>alert(1)</script> <b>Welt</b>',
    '<p onclick="alert(1)">Absatz</p><img src=x onerror=alert(1)>',
    '<a href=javascript:alert(1)>Link</a> und <iframe src=x></iframe>Rest',
    '<scr<script>x</script>ipt>alert(5)</script>',
    '<svg onload=alert(1)>',
]


def _quote_payload(tmp_db, **over):
    own, customer = add_contacts(tmp_db)
    payload = {'quoteNumber': '26A100', 'quoteDate': '2026-06-09', 'customerId': customer, 'ownCompanyId': own,
               'taxRate': 0.19, 'status': 'draft',
               'items': [{'position': 1, 'quantity': 1, 'description': 'X', 'unitPrice': 10.0, 'taxRate': 19}]}
    payload.update(over)
    return payload


@pytest.mark.parametrize('text', HOSTILE_TEXTS)
def test_saving_a_quote_stores_the_filtered_text(tmp_db, monkeypatch, text):
    monkeypatch.setattr(handlers, 'Database', lambda *a, **k: tmp_db)
    payload = _quote_payload(tmp_db, introText=text, closingText=text)
    saved = json.loads(handlers.handle_quote_save(json.dumps(payload).encode()))
    stored = tmp_db.get_invoice_by_id(saved['quote_id'])
    assert stored[47] == sanitize_richtext(text) and stored[48] == sanitize_richtext(text)
    assert '<script' not in (stored[47] or '') and 'onerror' not in (stored[47] or '')
    # auch beim Aktualisieren
    payload.update(quoteId=saved['quote_id'], introText='<b onclick=x>neu</b>' + text)
    json.loads(handlers.handle_quote_save(json.dumps(payload).encode()))
    assert tmp_db.get_invoice_by_id(saved['quote_id'])[47] == sanitize_richtext('<b onclick=x>neu</b>' + text)


def test_saving_a_quote_with_only_a_script_stores_no_text(tmp_db, monkeypatch):
    monkeypatch.setattr(handlers, 'Database', lambda *a, **k: tmp_db)
    payload = _quote_payload(tmp_db, introText='<script>alert(1)</script>', closingText='   ')
    saved = json.loads(handlers.handle_quote_save(json.dumps(payload).encode()))
    stored = tmp_db.get_invoice_by_id(saved['quote_id'])
    assert stored[47] is None and stored[48] is None


@pytest.mark.parametrize('text', HOSTILE_TEXTS)
def test_legacy_quote_text_stored_raw_is_filtered_when_the_form_shows_it(tmp_db, text):
    # Altbestand: roh in der Datenbank, am Filter beim Speichern vorbei (Import, SQL-Konsole, ältere Version)
    own, customer = add_contacts(tmp_db)
    quote = add_document(tmp_db, '26A001', 'quote', own, customer, intro_text=text, closing_text='<b>Gruß</b>' + text)
    assert row(tmp_db, quote)[47] == text                 # roh gespeichert
    with as_user():
        html = PageQuote(tmp_db, {}, quote)
    intro = f'id="intro_text_editor" contenteditable="true">{sanitize_richtext(text) or ""}</div>'
    closing = f'id="closing_text_editor" contenteditable="true">{sanitize_richtext("<b>Gruß</b>" + text)}</div>'
    assert intro in html and closing in html
    for payload in ('onerror=alert', 'javascript:alert', '<script>alert', 'onclick="alert', '<iframe', '<svg'):
        assert payload not in html, payload
    assert len(Dom(html).all('img')) == 1                 # nur das Firmenlogo der Vorlage, kein eingeschleustes Bild
    # Dieselbe Seite wie bei einem Text, der schon beim Speichern bereinigt wurde
    set_column(tmp_db, quote, 'IntroText', sanitize_richtext(text))
    set_column(tmp_db, quote, 'ClosingText', sanitize_richtext('<b>Gruß</b>' + text))
    with as_user():
        assert PageQuote(tmp_db, {}, quote) == html


# ── PDF-Satz ─────────────────────────────────────────────────────────────────
def test_pdf_paragraphs_keep_a_plain_less_than_sign():
    # „<14 Tagen“ ist Text, kein Tag: früher strich die Tag-Entfernung alles bis zum nächsten „>“
    assert _richtext_paragraphs('Zahlbar in <14 Tagen und >2 % Skonto') == [
        [('Zahlbar in <14 Tagen und >2 % Skonto', False, False)]]
    assert _richtext_paragraphs('<b>a < b</b>') == [[('a < b', True, False)]]
    assert _richtext_paragraphs('x &amp; y &lt;z&gt;') == [[('x & y <z>', False, False)]]


def test_pdf_paragraphs_drop_scripts_and_attributes():
    assert _richtext_paragraphs('<script>alert(1)</script>Text') == [[('Text', False, False)]]
    assert _richtext_paragraphs('<a href="javascript:alert(1)">klick</a>') == [[('klick', False, False)]]
    assert _richtext_paragraphs('<p onclick=x>Hallo</p>')[0] == [('Hallo', False, False)]
    assert _richtext_paragraphs(None) == [] and _richtext_paragraphs('') == []


def test_pdf_paragraphs_keep_bold_italic_lists_and_breaks():
    assert _richtext_paragraphs('<b>fett</b> <i>kursiv</i> normal') == [
        [('fett', True, False), (' ', False, False), ('kursiv', False, True), (' normal', False, False)]]
    assert _richtext_paragraphs('Zeile 1<br>Zeile 2') == [[('Zeile 1', False, False)], [('Zeile 2', False, False)]]
    assert [r for r in _richtext_paragraphs('<ul><li>eins</li><li>zwei</li></ul>') if r and r[0][0]] == [
        [('• eins', False, False)], [('• zwei', False, False)]]


def test_invoice_pdf_sets_the_payment_terms_with_a_less_than_sign(tmp_db):
    own, customer = add_contacts(tmp_db)
    invoice = add_document(tmp_db, '26R001', 'invoice', own, customer, payment_terms='Zahlbar in <14 Tagen netto')
    pdf, _ = generate_invoice_pdf(tmp_db, invoice)
    content = _all_content_bytes(pdf)
    assert b'(<14 ) Tj' in content and b'(Tagen ) Tj' in content
