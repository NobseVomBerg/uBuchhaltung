"""
Master Data (Stammdaten) page generation functions
Contains: Articles, Contacts, Chart of Accounts (SKR)
"""
from db import Database

def Header1(active_page=None):
    from server.pages import Header1 as BaseHeader1
    return BaseHeader1(active_page)

def Header2(content=""):
    from server.pages import Header2 as BaseHeader2
    return BaseHeader2(content)

def Header3(content=""):
    from server.pages import Header3 as BaseHeader3
    return BaseHeader3(content)

def Footer():
    from server.pages import Footer as BaseFooter
    return BaseFooter()


def PageMasterData(db: Database):
    """Master Data overview page with navigation to subpages"""
    s = Header1('masterdata')
    s += Header2("<a href='/masterdata/articles'>📦 Artikel</a> | <a href='/masterdata/contacts'>👥 Kontakte</a> | <a href='/masterdata/skr'>📊 SKR</a> | <a href='/masterdata/bankaccounts'>🏦 Bankkonten</a> | <a href='/masterdata/numberranges'>🔢 Nummernkreise</a> | <a href='/asset_categories'>📂 AfA-Kategorien</a>")
    s += Header3()
    
    s += '''
        <div class="grid1RowPrefered gridMain">
            <div class="rectRounded">
                <h3>📦 Artikel</h3>
                <p>Verwaltung des Produkt- und Dienstleistungskatalogs</p>
                <p><a href="/masterdata/articles" style="font-weight: bold;">→ Zur Artikelverwaltung</a></p>
            </div>
            
            <div class="rectRounded">
                <h3>👥 Kontakte</h3>
                <p>Kunden, Lieferanten, eigene Firmendaten und sonstige Kontakte</p>
                <p><a href="/masterdata/contacts" style="font-weight: bold;">→ Zur Kontaktverwaltung</a></p>
            </div>
            
            <div class="rectRounded">
                <h3>📊 SKR (Kontenrahmen)</h3>
                <p>Standardkontenrahmen (SKR03/04/07) für die Buchhaltung</p>
                <p><a href="/masterdata/skr" style="font-weight: bold;">→ Zur SKR-Verwaltung</a></p>
            </div>
            
            <div class="rectRounded">
                <h3>🏦 Bankkonten</h3>
                <p>Verwaltung eigener Bank- und Kassenkonten</p>
                <p><a href="/masterdata/bankaccounts" style="font-weight: bold;">→ Zur Kontenverwaltung</a></p>
            </div>
            
            <div class="rectRounded">
                <h3>🔢 Nummernkreise</h3>
                <p>Nummerierung für Rechnungen und Belege verwalten</p>
                <p><a href="/masterdata/numberranges" style="font-weight: bold;">→ Zu den Nummernkreisen</a></p>
            </div>

            <div class="rectRounded">
                <h3>📂 AfA-Kategorien</h3>
                <p>Abschreibungskategorien für das Anlagenverzeichnis</p>
                <p><a href="/asset_categories" style="font-weight: bold;">→ Zu den AfA-Kategorien</a></p>
            </div>
        </div>
    '''
    
    s += Footer()
    return s


# ══════════════════════════════════════════════════════════════════════
# ARTICLES (Artikel)
# ══════════════════════════════════════════════════════════════════════

def PageArticles(db: Database, edit_article_id=None):
    """Generate articles management page with inline edit form (grid2Cols layout)"""
    articles = db.fetch_articles()

    # Artikel zum Bearbeiten laden
    edit_article = None
    if edit_article_id:
        edit_article = db.get_article_by_id(edit_article_id)

    s = Header1('masterdata')
    submenu = '<a href="/masterdata">Stammdaten</a> -> <span id="ActivePage">📦 Artikel</span>'
    s += Header2(submenu)

    header3_content = '''
        <div class="rowWithObjects">
            <div>
                <label>🔍 Suche:</label>
                <input type="text" id="articleSearch" oninput="filterArticles()" placeholder="Bezeichnung / Beschreibung" style="width: 200px;">
            </div>
            <div>
                <label>Status:</label>
                <select id="activeFilter" onchange="filterArticles()">
                    <option value="">Alle</option>
                    <option value="1">Nur aktive</option>
                    <option value="0">Nur inaktive</option>
                </select>
            </div>
            <div>
                <label>MwSt:</label>
                <select id="taxFilter" onchange="filterArticles()">
                    <option value="">Alle</option>
                    <option value="0">0%</option>
                    <option value="7">7%</option>
                    <option value="19">19%</option>
                </select>
            </div>
            <div>
                <label>Min. Preis:</label>
                <input type="number" step="0.01" class="noButtons" id="minPrice" oninput="filterArticles()" style="width: 80px;">
                <label>Max. Preis:</label>
                <input type="number" step="0.01" class="noButtons" id="maxPrice" oninput="filterArticles()" style="width: 80px;">
            </div>
        </div>
    '''
    s += Header3(header3_content)

    # Formular-Modus
    form_title = "Artikel bearbeiten" if edit_article else "Neuer Artikel"

    ea_name       = edit_article[1] if edit_article else ''
    ea_unit       = edit_article[2] if edit_article else 'Stk.'
    ea_unit_price = edit_article[3] if edit_article else 0.0
    ea_tax_rate   = int(edit_article[4]) if edit_article else 19
    ea_desc       = edit_article[5] if edit_article else ''
    ea_active     = edit_article[6] if edit_article and len(edit_article) > 6 else 1

    unit_options = [
        ('Stk.', 'Stk. (Stück)'), ('Std.', 'Std. (Stunde)'),
        ('kg', 'kg (Kilogramm)'), ('g', 'g (Gramm)'),
        ('m', 'm (Meter)'), ('m²', 'm² (Quadratmeter)'),
        ('l', 'l (Liter)'), ('Psch.', 'Psch. (Pauschale)'),
    ]
    unit_select = ''.join(
        f'<option value="{u}" {"selected" if u == ea_unit else ""}>{label}</option>'
        for u, label in unit_options
    )
    tax_select = ''.join(
        f'<option value="{r}" {"selected" if r == ea_tax_rate else ""}>{label}</option>'
        for r, label in [(19, '19%'), (7, '7%'), (0, '0%')]
    )
    active_checked = 'checked' if ea_active else ''
    id_row = (f'<tr><td>ID:</td><td style="color:#666;">{edit_article[0]}'
              f'<input type="hidden" name="id" value="{edit_article[0]}"></td></tr>'
              if edit_article else '')
    if edit_article:
        action_buttons = (
            '<input type="submit" value="💾 Aktualisieren" class="coloredButton btn-sm bg-green">'
            '<input type="submit" value="💾 Als neu anlegen" formaction="/masterdata/articles/add" class="coloredButton btn-sm bg-blue">'
            '<button type="button" onclick="window.location.href=\'/masterdata/articles\'" class="coloredButton btn-sm bg-gray">← Abbrechen</button>'
        )
    else:
        action_buttons = '<input type="submit" value="💾 Artikel hinzufügen" formaction="/masterdata/articles/add" class="coloredButton btn-sm bg-green">'

    s += f'''
    <div class="grid2Cols gridMain">
    <div class="gridRightCol gridMiddle" style="order:2">
        <div class="rectRounded">
        <h2>{form_title}</h2>
        <form method="POST" action="/masterdata/articles/update">
            <div class="rowWithObjects">{action_buttons}</div>
        </div>
        <div class="rectRounded">
            <table class="form-table">
                {id_row}
                <tr><td>Bezeichnung:</td><td><input type="text" name="name" value="{ea_name}" required size="40"></td></tr>
                <tr><td>Einheit:</td><td><select name="unit">{unit_select}</select></td></tr>
                <tr><td>Einzelpreis (netto in €):</td><td><input type="number" step="0.01" name="unit_price" value="{ea_unit_price:.2f}"></td></tr>
                <tr><td>MwSt (%):</td><td><select name="tax_rate">{tax_select}</select></td></tr>
                <tr><td>Beschreibung:</td><td><textarea name="description" rows="3" cols="38">{ea_desc}</textarea></td></tr>
                <tr><td>Aktiv:</td><td><input type="checkbox" name="active" value="1" {active_checked}></td></tr>
            </table>
        </form>
        </div>
    </div>
    <div class="gridLeftCol" style="order:1">
        <table>
            <tr><th>Bezeichnung</th><th>Einheit</th><th>Preis (netto)</th><th>MwSt</th><th>Beschreibung</th><th>Aktionen</th></tr>
    '''

    for article in articles:
        article_id  = article[0]
        name        = article[1] or ''
        unit        = article[2] or 'Stk.'
        unit_price  = article[3] or 0
        tax_rate    = article[4] or 19
        description = article[5] or ''
        active      = article[6] if len(article) > 6 else 1

        active_badge = ("<span class='badge bg-green' title='Aktiv'>✓</span>"
                        if active else
                        "<span class='badge bg-orange' title='Inaktiv'>✗</span>")
        s += (f"<tr class='article-row' "
              f"data-active='{active}' "
              f"data-tax='{int(tax_rate)}' "
              f"data-price='{unit_price}'>")
        s += f"<td>{name}</td>"
        s += f"<td>{unit}</td>"
        s += f"<td style='text-align:right;'>{unit_price:.2f}&nbsp;€</td>"
        s += f"<td>{int(tax_rate)}%</td>"
        s += f"<td>{description[:50]}</td>"
        s += (f"<td>{active_badge}"
              f" <a href='javascript:void(0)' onclick='openEditForm(\"/masterdata/articles/edit?id={article_id}\")' class='action-icon' title='Bearbeiten'>&#9998;</a>"
              f" <a href='javascript:void(0);' class='action-icon delete-icon' title='Löschen'"
              f" onclick='appConfirmHref(\"/masterdata/articles/delete?id={article_id}\", \"Artikel wirklich löschen?\")'>&#128465;</a></td>")
        s += f"</tr>"

    s += "</table>"
    s += '</div><!-- Ende gridLeftCol --></div><!-- Ende grid2Cols -->'

    s += '''
    <script>
        function filterArticles() {
            const search    = document.getElementById('articleSearch').value.toLowerCase();
            const activeVal = document.getElementById('activeFilter').value;
            const taxVal    = document.getElementById('taxFilter').value;
            const minPrice  = parseFloat(document.getElementById('minPrice').value);
            const maxPrice  = parseFloat(document.getElementById('maxPrice').value);

            document.querySelectorAll('.article-row').forEach(row => {
                const rowActive = row.getAttribute('data-active');
                const rowTax    = row.getAttribute('data-tax');
                const rowPrice  = parseFloat(row.getAttribute('data-price'));
                const rowText   = row.textContent.toLowerCase();

                let show = true;
                if (search    && !rowText.includes(search))  show = false;
                if (activeVal && rowActive !== activeVal)     show = false;
                if (taxVal    && rowTax    !== taxVal)        show = false;
                if (!isNaN(minPrice) && rowPrice < minPrice) show = false;
                if (!isNaN(maxPrice) && rowPrice > maxPrice) show = false;

                row.style.display = show ? '' : 'none';
            });
        }

        function openEditForm(url) {
            fetch(url)
                .then(r => r.text())
                .then(html => {
                    const doc = new DOMParser().parseFromString(html, 'text/html');
                    const newForm = doc.querySelector('.gridRightCol');
                    const curForm = document.querySelector('.gridRightCol');
                    if (newForm && curForm) {
                        curForm.innerHTML = newForm.innerHTML;
                        curForm.querySelectorAll('script').forEach(s => {
                            const ns = document.createElement('script');
                            ns.textContent = s.textContent;
                            s.replaceWith(ns);
                        });
                        history.pushState({}, '', url);
                    }
                })
                .catch(() => { window.location.href = url; });
        }
    </script>
    '''

    s += Footer()
    return s


def PageArticleEdit(db: Database, article_id):
    """Delegiert an PageArticles mit gesetzter Edit-ID."""
    return PageArticles(db, edit_article_id=article_id)



# ══════════════════════════════════════════════════════════════════════
# CONTACTS (Kontakte)
# ══════════════════════════════════════════════════════════════════════

# Contacts pages are implemented in pages_contacts.py
from .pages_contacts import PageContacts, PageContactNew, PageContactEdit  # noqa: F401


# ══════════════════════════════════════════════════════════════════════
# SKR (Chart of Accounts)
# ══════════════════════════════════════════════════════════════════════

def PageSkr(db: Database, edit_id=None, copy_from_id=None, msg=None, msg_type='info'):
    """SKR-Kontenrahmen (Stammdaten): Übersicht links, Inline-Formular rechts.

    Modi: Bearbeiten (edit_id) lädt ein vorhandenes Konto – Rahmen/Nummer sind
    dann schreibgeschützt (die ID = Rahmen·100000+Nummer bleibt fix). Kopieren
    (copy_from_id) öffnet ein leeres Anlegen-Formular, vorbefüllt mit den Werten
    der Quelle und der nächsten freien Nummer. Ohne beides: leeres Neu-Formular.
    Standard-Konten sind bis auf Privatanteil und Menü-Sichtbarkeit schreibgeschützt.
    """
    rows = db.fetch_chart_of_accounts()

    # ChartOfAccounts (SELECT *): [0]ID [1]Framework-Nr [2]Konto [3]Name
    #               [4]Gruppe [5]IsStandard [6]PrivateSharePercent [7]ShowInMenu
    edit_skr = next((row for row in rows if row[0] == edit_id), None) if edit_id is not None else None
    src      = next((row for row in rows if row[0] == copy_from_id), None) if copy_from_id is not None else None

    is_existing = edit_skr is not None
    is_std = (edit_skr[5] if is_existing and len(edit_skr) > 5 else 0) or 0

    # Formular-Werte je nach Modus
    if is_existing:
        form_title  = "SKR-Konto bearbeiten"
        es_framework, es_account = edit_skr[1], edit_skr[2]
        es_name, es_group        = edit_skr[3], edit_skr[4]
        es_psp  = edit_skr[6] if len(edit_skr) > 6 and edit_skr[6] else 0
        es_show = edit_skr[7] if len(edit_skr) > 7 else 1
    elif src is not None:
        suggested = db.next_free_account_number(src[1], int(src[2]) + 1)
        form_title  = f"Neues SKR-Konto (Kopie von {src[2]})"
        es_framework, es_account = src[1], suggested
        es_name, es_group        = src[3], src[4]
        es_psp  = src[6] if len(src) > 6 and src[6] else 0
        es_show = src[7] if len(src) > 7 else 1
    else:
        form_title  = "Neues SKR-Konto"
        es_framework = es_account = es_name = es_group = ''
        es_psp, es_show = 0, 1

    # Readonly: Rahmen/Nummer fix bei vorhandenem Konto; Name/Gruppe nur bei Standard
    fixed_attr = ' readonly' if is_existing else ''
    name_attr  = ' readonly' if is_std else ''
    readonly_note = ('<p class="muted">Standard-Konto: Rahmen, Nummer und Name sind fix; '
                     'nur Privatanteil und Menü-Sichtbarkeit sind änderbar.</p>' if is_std else '')
    show_checked = 'checked' if es_show else ''

    s = Header1('masterdata')
    submenu = '<a href="/masterdata">Stammdaten</a> → <span id="ActivePage">📊 SKR (Kontenrahmen)</span>'
    s += Header2(submenu)
    s += Header3()

    # Flash (z. B. Lösch-Hinweis) – erst nach DOMContentLoaded (appMsg kommt aus Footer)
    if msg:
        safe = msg.replace('\\', '\\\\').replace("'", "\\'").replace('\n', ' ')
        safe_type = (msg_type or 'info').replace("'", '')
        s += (f"<script>document.addEventListener('DOMContentLoaded',function(){{"
              f"appMsg('{safe}','{safe_type}');}});</script>")

    id_row = (f'<input type="hidden" name="id" value="{edit_skr[0]}">' if is_existing else '')

    if is_existing:
        action_buttons = (
            '<input type="submit" value="Aktualisieren" class="coloredButton btn-sm bg-green">'
            f'<button type="button" onclick="window.location.href=\'/masterdata/skr/copy?id={edit_skr[0]}\'" class="coloredButton btn-sm bg-blue">Kopieren</button>'
            '<button type="button" onclick="window.location.href=\'/masterdata/skr\'" class="coloredButton btn-sm bg-gray">← Abbrechen</button>'
        )
    else:
        action_buttons = (
            '<input type="submit" value="SKR-Konto hinzufügen" formaction="/masterdata/skr/add" class="coloredButton btn-sm bg-green">'
            + ('<button type="button" onclick="window.location.href=\'/masterdata/skr\'" class="coloredButton btn-sm bg-gray">← Abbrechen</button>'
               if src is not None else '')
        )

    s += f'''
    <div class="grid2Cols gridMain">
    <div class="gridRightCol gridMiddle" style="order:2">
        <div class="rectRounded">
        <h2>{form_title}</h2>
        {readonly_note}
        <form method="POST" action="/masterdata/skr/update">
            {id_row}
            <div class="rowWithObjects">{action_buttons}</div>
        </div>
        <div class="rectRounded">
            <table class="form-table">
                <tr><td>Rahmen-Nr.:</td><td><input type="number" name="framework_nr" value="{es_framework}"{fixed_attr}></td></tr>
                <tr><td>Konto:</td><td><input type="number" name="account" value="{es_account}"{fixed_attr}></td></tr>
                <tr><td>Name:</td><td><input type="text" name="name" value="{es_name}"{name_attr}></td></tr>
                <tr><td>Gruppe:</td><td><input type="text" name="group" value="{es_group}"{name_attr}></td></tr>
                <tr><td>Privatanteil %:</td><td><input type="number" name="private_share_percent" value="{es_psp}" min="0" max="100"></td></tr>
                <tr><td>Im Auswahlmenü:</td><td><input type="checkbox" name="show_in_menu" value="1" {show_checked}></td></tr>
            </table>
        </form>
        </div>
    </div>
    <div class="gridLeftCol" style="order:1">
        <table>
            <tr><th>ID</th><th>SKR-Nr.</th><th>Konto</th><th>Name</th><th>Gruppe</th><th>Privatanteil %</th><th>Standard</th><th>Aktionen</th></tr>
    '''
    for row in rows:
        is_standard = row[5] if len(row) > 5 else 0
        standard_text = "\u2713" if is_standard else ""
        psp = row[6] if len(row) > 6 and row[6] else 0
        psp_display = f"{psp}\u2009%" if psp else ""
        show_in_menu = row[7] if len(row) > 7 else 1
        eye_glyph = '&#128065;' if show_in_menu else '&#128683;'  # \ud83d\udc41 sichtbar / \ud83d\udeab ausgeblendet
        eye_title = 'Im Men\u00fc sichtbar \u2013 ausblenden' if show_in_menu else 'Ausgeblendet \u2013 einblenden'
        eye_icon = (f"<a href='javascript:void(0)' onclick='toggleSkrMenu({row[0]})' class='action-icon' "
                    f"title='{eye_title}'>{eye_glyph}</a>")
        del_icon = ''
        if not is_standard:
            del_icon = (f" <a href='javascript:void(0);' class='action-icon delete-icon' title='L\u00f6schen'"
                        f" onclick='appConfirmHref(\"/masterdata/skr/delete?id={row[0]}\", \"Konto wirklich l\u00f6schen?\")'>&#128465;</a>")
        s += (f"<tr><td>{row[0]}</td><td>{row[1]}</td><td>{row[2]}</td><td>{row[3]}</td><td>{row[4]}</td>"
              f"<td>{psp_display}</td><td>{standard_text}</td>"
              f"<td><a href='javascript:void(0)' onclick='openEditForm(\"/masterdata/skr/edit?id={row[0]}\")' class='action-icon' title='Bearbeiten'>&#9998;</a>"
              f" {eye_icon}{del_icon}</td></tr>")
    s += "</table>"
    s += '</div><!-- Ende gridLeftCol --></div><!-- Ende grid2Cols -->'
    s += '''
    <script>
        function openEditForm(url) {
            fetch(url)
                .then(r => r.text())
                .then(html => {
                    const doc = new DOMParser().parseFromString(html, 'text/html');
                    const newForm = doc.querySelector('.gridRightCol');
                    const curForm = document.querySelector('.gridRightCol');
                    if (newForm && curForm) {
                        curForm.innerHTML = newForm.innerHTML;
                        curForm.querySelectorAll('script').forEach(s => {
                            const ns = document.createElement('script');
                            ns.textContent = s.textContent;
                            s.replaceWith(ns);
                        });
                        history.pushState({}, '', url);
                    }
                })
                .catch(() => { window.location.href = url; });
        }

        function toggleSkrMenu(id) {
            fetch('/masterdata/skr/togglemenu?id=' + id)
                .then(r => r.text())
                .then(html => {
                    const doc = new DOMParser().parseFromString(html, 'text/html');
                    const newLeft = doc.querySelector('.gridLeftCol');
                    const curLeft = document.querySelector('.gridLeftCol');
                    if (newLeft && curLeft) curLeft.innerHTML = newLeft.innerHTML;
                })
                .catch(() => { window.location.href = '/masterdata/skr'; });
        }
    </script>
    '''
    s += Footer()
    return s


def PageSkrEdit(db: Database, id):
    """Thin-Wrapper – Inline-Bearbeiten in der kombinierten SKR-Seite."""
    return PageSkr(db, edit_id=id)


# ══════════════════════════════════════════════════════════════════════
# BANK ACCOUNTS (Bankkonten)
# ══════════════════════════════════════════════════════════════════════

def PageBankAccounts(db: Database, edit_id=None):
    """Bankkonten (Stammdaten): Übersicht links, Inline-Formular rechts.

    Kasse-Konten sind bis auf das SKR-Gegenkonto schreibgeschützt (Name/Typ
    fix); reine Bankkonten sind voll editierbar und löschbar.
    """
    rows = db.fetch_accounts()
    # Accounts (SELECT *): [0]ID [1]Name [2]Inhaber [3]IBAN [4]BIC [5]Bank
    #                      [6]IsCash [7]SKR-Gegenkonto
    edit_acc = db.get_account_by_id(edit_id) if edit_id is not None else None
    is_cash  = bool(edit_acc and edit_acc[6] == 1)

    s = Header1('masterdata')
    submenu = '<a href="/masterdata">Stammdaten</a> → <span id="ActivePage">🏦 Bankkonten</span>'
    s += Header2(submenu)
    s += Header3()

    # Formular-Werte (Bearbeiten oder Neu)
    ea_name   = edit_acc[1] if edit_acc else ''
    ea_holder = (edit_acc[2] or '') if edit_acc else ''
    ea_iban   = (edit_acc[3] or '') if edit_acc else ''
    ea_bic    = (edit_acc[4] or '') if edit_acc else ''
    ea_bank   = (edit_acc[5] or '') if edit_acc else ''
    ea_skr    = (edit_acc[7] if edit_acc and len(edit_acc) > 7 and edit_acc[7] is not None else '') if edit_acc else ''

    if edit_acc:
        form_title = "Kasse-Konto bearbeiten" if is_cash else "Bankkonto bearbeiten"
    else:
        form_title = "Neues Bankkonto"
    id_row = (f'<input type="hidden" name="id" value="{edit_acc[0]}">' if edit_acc else '')

    if edit_acc:
        action_buttons = ('<input type="submit" value="Aktualisieren" class="coloredButton btn-sm bg-green">'
                          '<button type="button" onclick="window.location.href=\'/masterdata/bankaccounts\'" class="coloredButton btn-sm bg-gray">← Abbrechen</button>')
    else:
        action_buttons = ('<input type="submit" value="Konto hinzufügen" '
                          'formaction="/masterdata/bankaccounts/add" class="coloredButton btn-sm bg-green">')

    if is_cash:
        note = '<p class="muted">Kasse-Konto: Name und Typ können nicht geändert werden.</p>'
        fields = (
            f'<input type="hidden" name="name" value="{ea_name}">'
            f'<input type="hidden" name="holder" value="{ea_holder}">'
            f'<input type="hidden" name="iban" value="{ea_iban}">'
            f'<input type="hidden" name="bic" value="{ea_bic}">'
            f'<input type="hidden" name="bank_name" value="{ea_bank}">'
            f'<tr><td>Bezeichnung:</td><td>{ea_name}</td></tr>'
            f'<tr><td>Typ:</td><td>Kasse</td></tr>'
            f'<tr><td>SKR-Gegenkonto:</td><td><input type="number" name="skr_account" value="{ea_skr}" placeholder="z.B. 1460"></td></tr>'
        )
    else:
        note = ''
        fields = (
            f'<tr><td>Bezeichnung:</td><td><input type="text" name="name" value="{ea_name}" required></td></tr>'
            f'<tr><td>Inhaber:</td><td><input type="text" name="holder" value="{ea_holder}"></td></tr>'
            f'<tr><td>IBAN:</td><td><input type="text" name="iban" value="{ea_iban}"></td></tr>'
            f'<tr><td>BIC:</td><td><input type="text" name="bic" value="{ea_bic}"></td></tr>'
            f'<tr><td>Bank:</td><td><input type="text" name="bank_name" value="{ea_bank}"></td></tr>'
            f'<tr><td>SKR-Gegenkonto:</td><td><input type="number" name="skr_account" value="{ea_skr}" placeholder="z.B. 1810"></td></tr>'
        )

    s += f'''
    <div class="grid2Cols gridMain">
    <div class="gridRightCol" style="order:2">
        <div class="rectRounded">
        <h2>{form_title}</h2>
        {note}
        <form method="POST" action="/masterdata/bankaccounts/update">
            {id_row}
            <table class="form-table">
                {fields}
                <tr><td></td><td>{action_buttons}</td></tr>
            </table>
        </form>
        </div>
    </div>
    <div class="gridLeftCol" style="order:1">
        <table>
            <tr><th>ID</th><th>Bezeichnung</th><th>Inhaber</th><th>IBAN</th><th>BIC</th><th>Bank</th><th>Typ</th><th>SKR-Konto</th><th>Aktionen</th></tr>
    '''
    for row in rows:
        account_type = "Kasse" if row[6] == 1 else "Bank"
        skr_display = row[7] if len(row) > 7 and row[7] else "–"
        s += (f"<tr><td>{row[0]}</td><td>{row[1]}</td><td>{row[2] or ''}</td><td>{row[3] or ''}</td>"
              f"<td>{row[4] or ''}</td><td>{row[5] or ''}</td><td>{account_type}</td><td>{skr_display}</td>")
        edit_title = "SKR zuweisen" if row[6] == 1 else "Bearbeiten"
        actions = f"<a href='javascript:void(0)' onclick='openEditForm(\"/masterdata/bankaccounts/edit?id={row[0]}\")' class='action-icon' title='{edit_title}'>&#9998;</a>"
        if row[6] != 1:  # nur reine Bankkonten löschbar (Kasse nicht)
            actions += (" <a href='javascript:void(0);' class='action-icon delete-icon' title='Löschen' "
                        f"onclick=\"appConfirmHref('/masterdata/bankaccounts/delete?id={row[0]}', 'Konto wirklich löschen?')\">&#128465;</a>")
        s += f"<td>{actions}</td></tr>"
    s += "</table>"
    s += '</div><!-- Ende gridLeftCol --></div><!-- Ende grid2Cols -->'
    s += '''
    <script>
        function openEditForm(url) {
            fetch(url)
                .then(r => r.text())
                .then(html => {
                    const doc = new DOMParser().parseFromString(html, 'text/html');
                    const newForm = doc.querySelector('.gridRightCol');
                    const curForm = document.querySelector('.gridRightCol');
                    if (newForm && curForm) {
                        curForm.innerHTML = newForm.innerHTML;
                        curForm.querySelectorAll('script').forEach(s => {
                            const ns = document.createElement('script');
                            ns.textContent = s.textContent;
                            s.replaceWith(ns);
                        });
                        history.pushState({}, '', url);
                    }
                })
                .catch(() => { window.location.href = url; });
        }
    </script>
    '''
    s += Footer()
    return s


def PageBankAccountEdit(db: Database, account_id):
    """Thin-Wrapper – Inline-Bearbeiten in der kombinierten Bankkonten-Seite."""
    return PageBankAccounts(db, edit_id=account_id)


# ══════════════════════════════════════════════════════════════════════════════
# NUMBER RANGES (Nummernkreise)
# ══════════════════════════════════════════════════════════════════════════════

def PageNumberRanges(db: Database, edit_id=None):
    """Nummernkreise (Stammdaten): gruppierte Übersicht links, Inline-Formular rechts.

    Bearbeiten lädt den Nummernkreis ins rechte Formular; der Typ ist dabei fix.
    """
    import datetime
    current_year = datetime.datetime.now().year
    ranges = db.fetch_number_ranges()

    # NumberRange: [0]ID [1]Type [2]Year [3]Letter [4]Prefix [5]CurrentNumber
    #              [6]Description [7]NumberFormat
    edit_nr = db.get_number_range_by_id(edit_id) if edit_id is not None else None

    type_names = {
        'invoice': 'Ausgangsrechnungen',
        'receipt_company': 'Belegnummern Firma',
        'receipt_category': 'Belegnummern Kategorien',
    }

    s = Header1('masterdata')
    submenu = '<a href="/masterdata">Stammdaten</a> → <span id="ActivePage">🔢 Nummernkreise</span>'
    s += Header2(submenu)
    s += Header3()

    legend = ('<p class="muted">Format-Platzhalter: <code>{yy}</code> Jahr 2-stellig, '
              '<code>{yyyy}</code> Jahr 4-stellig, <code>{l}</code> Buchstabe, '
              '<code>{nnn}</code> lfd. Nr. 3-stellig, <code>{s}</code> Suffix. '
              'Beispiel <code>{yy}{l}{nnn}{s}</code> → <strong>26F001</strong>.</p>')

    # Formular-Werte (Bearbeiten oder Neu)
    if edit_nr:
        form_title = "Nummernkreis bearbeiten"
        nr_type    = edit_nr[1]
        nr_year    = edit_nr[2]
        nr_letter  = edit_nr[3]
        nr_prefix  = edit_nr[4] or ''
        nr_curnum  = edit_nr[5] or 0
        nr_desc    = edit_nr[6] or ''
        nr_format  = edit_nr[7] if len(edit_nr) > 7 and edit_nr[7] else '{yy}{l}{nnn}{s}'
        next_formatted = db._apply_number_format(nr_format, nr_year, nr_letter, nr_curnum + 1, nr_prefix)
        type_field = (f'<input type="hidden" name="type" value="{nr_type}">'
                      f'<tr><td>Typ:</td><td><strong>{type_names.get(nr_type, nr_type)}</strong></td></tr>')
        preview = f'<p class="muted">Nächste Nummer wird sein: <strong>{next_formatted}</strong></p>'
        id_row = f'<input type="hidden" name="id" value="{edit_nr[0]}">'
        action_buttons = ('<input type="submit" value="Aktualisieren" class="coloredButton btn-sm bg-green">'
                          '<button type="button" onclick="window.location.href=\'/masterdata/numberranges\'" class="coloredButton btn-sm bg-gray">← Abbrechen</button>')
    else:
        form_title = "Neuer Nummernkreis"
        nr_year    = current_year
        nr_letter  = ''
        nr_prefix  = ''
        nr_curnum  = 0
        nr_desc    = ''
        nr_format  = '{yy}{l}{nnn}{s}'
        type_opts  = ''.join(f'<option value="{v}">{l}</option>' for v, l in type_names.items())
        type_field = f'<tr><td>Typ:</td><td><select name="type" required>{type_opts}</select></td></tr>'
        preview = ''
        id_row = ''
        action_buttons = ('<input type="submit" value="Nummernkreis hinzufügen" '
                          'formaction="/masterdata/numberranges/add" class="coloredButton btn-sm bg-green">')

    s += f'''
    <div class="grid2Cols gridMain">
    <div class="gridRightCol" style="order:2">
        <div class="rectRounded">
        <h2>{form_title}</h2>
        {legend}
        {preview}
        <form method="POST" action="/masterdata/numberranges/update">
            {id_row}
            <table class="form-table">
                {type_field}
                <tr><td>Jahr:</td><td><input type="number" name="year" value="{nr_year}" min="2000" max="2099" required></td></tr>
                <tr><td>Buchstabe:</td><td><input type="text" name="letter" value="{nr_letter}" maxlength="1" pattern="[A-Z]" required placeholder="z.B. F" style="text-transform:uppercase;"></td></tr>
                <tr><td>Suffix (optional):</td><td><input type="text" name="prefix" value="{nr_prefix}" maxlength="10" placeholder="z.B. _A"></td></tr>
                <tr><td>Format:</td><td><input type="text" name="number_format" value="{nr_format}"></td></tr>
                <tr><td>Aktuelle Nummer:</td><td><input type="number" name="current_number" value="{nr_curnum}" min="0"></td></tr>
                <tr><td>Beschreibung:</td><td><input type="text" name="description" value="{nr_desc}"></td></tr>
                <tr><td></td><td>{action_buttons}</td></tr>
            </table>
        </form>
        </div>
    </div>
    <div class="gridLeftCol" style="order:1">
    '''

    for range_type, type_name in type_names.items():
        type_ranges = [r for r in ranges if r[1] == range_type]
        s += f"<h4>{type_name}</h4>"
        if not type_ranges:
            s += "<p><em>Keine Nummernkreise definiert.</em></p>"
            continue
        s += "<table>"
        s += ("<tr><th>ID</th><th>Jahr</th><th>Buchstabe</th><th>Suffix</th><th>Format</th>"
              "<th>Aktuelle Nr.</th><th>Nächste Nr.</th><th>Beschreibung</th><th>Aktionen</th></tr>")
        for r in type_ranges:
            range_id = r[0]
            year = r[2]
            letter = r[3]
            suffix = r[4] or ''
            current_num = r[5] or 0
            description = r[6] or ''
            number_format = r[7] if len(r) > 7 and r[7] else '{yy}{l}{nnn}{s}'
            next_formatted = db._apply_number_format(number_format, year, letter, current_num + 1, suffix)
            s += (f"<tr><td>{range_id}</td><td>{year}</td><td>{letter}</td><td>{suffix}</td>"
                  f"<td><code>{number_format}</code></td><td style='text-align:right;'>{current_num}</td>"
                  f"<td><strong>{next_formatted}</strong></td><td>{description}</td>"
                  f"<td><a href='javascript:void(0)' onclick='openEditForm(\"/masterdata/numberranges/edit?id={range_id}\")' class='action-icon' title='Bearbeiten'>&#9998;</a>"
                  f" <a href='javascript:void(0);' class='action-icon delete-icon' title='Löschen'"
                  f" onclick=\"appConfirmHref('/masterdata/numberranges/delete?id={range_id}', 'Nummernkreis wirklich löschen?')\">&#128465;</a></td></tr>")
        s += "</table>"

    s += '</div><!-- Ende gridLeftCol --></div><!-- Ende grid2Cols -->'
    s += '''
    <script>
        function openEditForm(url) {
            fetch(url)
                .then(r => r.text())
                .then(html => {
                    const doc = new DOMParser().parseFromString(html, 'text/html');
                    const newForm = doc.querySelector('.gridRightCol');
                    const curForm = document.querySelector('.gridRightCol');
                    if (newForm && curForm) {
                        curForm.innerHTML = newForm.innerHTML;
                        curForm.querySelectorAll('script').forEach(s => {
                            const ns = document.createElement('script');
                            ns.textContent = s.textContent;
                            s.replaceWith(ns);
                        });
                        history.pushState({}, '', url);
                    }
                })
                .catch(() => { window.location.href = url; });
        }
    </script>
    '''
    s += Footer()
    return s


def PageNumberRangesEdit(db: Database, range_id):
    """Thin-Wrapper – Inline-Bearbeiten in der kombinierten Nummernkreis-Seite."""
    return PageNumberRanges(db, edit_id=range_id)
