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
            '<input type="submit" value="Aktualisieren" class="coloredButton btn-sm bg-green">'
            '<input type="submit" value="Als neu anlegen" formaction="/masterdata/articles/add" class="coloredButton btn-sm bg-blue">'
            '<a href="/masterdata/articles" class="coloredButton btn-sm bg-gray">Abbrechen</a>'
        )
    else:
        action_buttons = '<input type="submit" value="Artikel hinzufügen" formaction="/masterdata/articles/add" class="coloredButton btn-sm bg-green">'

    s += f'''
    <div class="grid2Cols gridMain">
    <div class="gridRightCol" style="order:2">
        <div class="rectRounded">
        <h2>{form_title}</h2>
        <form method="POST" action="/masterdata/articles/update">
            <table class="form-table">
                {id_row}
                <tr><td>Bezeichnung:</td><td><input type="text" name="name" value="{ea_name}" required size="40"></td></tr>
                <tr><td>Einheit:</td><td><select name="unit">{unit_select}</select></td></tr>
                <tr><td>Einzelpreis (netto):</td><td><input type="number" step="0.01" name="unit_price" value="{ea_unit_price:.2f}"> €</td></tr>
                <tr><td>MwSt (%):</td><td><select name="tax_rate">{tax_select}</select></td></tr>
                <tr><td>Beschreibung:</td><td><textarea name="description" rows="3" cols="38">{ea_desc}</textarea></td></tr>
                <tr><td>Aktiv:</td><td><input type="checkbox" name="active" value="1" {active_checked}></td></tr>
                <tr><td></td><td>
                    {action_buttons}
                </td></tr>
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
              f" <a href='/masterdata/articles/edit?id={article_id}' class='action-icon' title='Bearbeiten'>&#9998;</a>"
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

def PageSkr(db: Database):
    """Generate SKR (chart of accounts) page"""
    rows = db.fetch_chart_of_accounts()
    s = Header1('masterdata')
    submenu = '<a href="/masterdata">Stammdaten</a> -> <span id="ActivePage">📊 SKR (Kontenrahmen)</span>'
    s += Header2(submenu)
    s += Header3()
    s += '''
        <h2>Neues SKR-Konto anlegen</h2>
        <form method="POST" action="/masterdata/skr/add">
            <table class="form-table">
                <tr><td>Rahmen-Nr.:</td><td><input type="text" name="framework_nr"></td></tr>
                <tr><td>Konto:</td><td><input type="text" name="account"></td></tr>
                <tr><td>Name:</td><td><input type="text" name="name"></td></tr>
                <tr><td>Gruppe:</td><td><input type="text" name="group"></td></tr>
                <tr><td>Privatanteil %:</td><td><input type="number" name="private_share_percent" value="0" min="0" max="100" style="width:80px;"></td></tr>
                <tr><td></td><td><input type="submit" value="SKR-Konto hinzufügen"></td></tr>
            </table>
        </form>
    '''
    s += "<h2>Standardkontorahmen, definierte Konten</h2>"
    s += "<table>"
    s += "<tr><th>ID</th><th>SKR-Nr.</th><th>Konto</th><th>Name</th><th>Gruppe</th><th>Privatanteil %</th><th>Standard</th><th>Aktionen</th></tr>"
    for row in rows:
        is_standard = row[5] if len(row) > 5 else 0
        standard_text = "\u2713" if is_standard else ""
        psp = row[6] if len(row) > 6 and row[6] else 0
        psp_display = f"{psp}\u2009%" if psp else ""
        edit_link = f"<a href='/masterdata/skr/edit?id={row[0]}'>Bearbeiten</a>"
        s += f"<tr><td>{row[0]}</td><td>{row[1]}</td><td>{row[2]}</td><td>{row[3]}</td><td>{row[4]}</td><td>{psp_display}</td><td>{standard_text}</td>"
        s += f"<td>{edit_link}</td></tr>"
    s += "</table>"
    s += Footer()
    return s


def PageSkrEdit(db: Database, id):
    """Generate SKR edit page"""
    rows = db.fetch_chart_of_accounts()
    skr = None
    for row in rows:
        if row[0] == id:
            skr = row
            break
    if not skr:
        return "SKR-Konto nicht gefunden."

    s = Header1('masterdata')
    submenu = '<a href="/masterdata">Stammdaten</a> -> <a href="/masterdata/skr">📊 SKR</a> -> <span id="ActivePage">Bearbeiten</span>'
    s += Header2(submenu)
    s += Header3()

    is_standard = skr[5] if len(skr) > 5 else 0
    psp = skr[6] if len(skr) > 6 and skr[6] else 0
    readonly_attr = ' readonly' if is_standard else ''
    readonly_note = "<p style='color:#666;'>Standard-Konto: Nur der Privatanteil kann geändert werden.</p>" if is_standard else ""

    s += "<h1>SKR-Konto bearbeiten</h1>"
    s += readonly_note
    s += f'''
        <form method="POST" action="/masterdata/skr/update">
            <table class="form-table">
                <tr><td>ID:</td><td><input type="text" name="id" value="{skr[0]}" readonly></td></tr>
                <tr><td>Rahmen-Nr.:</td><td><input type="text" name="framework_nr" value="{skr[1]}"{readonly_attr}></td></tr>
                <tr><td>Konto:</td><td><input type="text" name="account" value="{skr[2]}"{readonly_attr}></td></tr>
                <tr><td>Name:</td><td><input type="text" name="name" value="{skr[3]}"{readonly_attr}></td></tr>
                <tr><td>Gruppe:</td><td><input type="text" name="group" value="{skr[4]}"{readonly_attr}></td></tr>
                <tr><td>Privatanteil %:</td><td><input type="number" name="private_share_percent" value="{psp}" min="0" max="100" style="width:80px;"></td></tr>
                <tr><td></td><td><input type="submit" value="SKR-Konto aktualisieren"></td></tr>
            </table>
        </form>
        <p><a href="/masterdata/skr">Zurück zur SKR-Übersicht</a></p>
    '''
    s += Footer()
    return s


# ══════════════════════════════════════════════════════════════════════
# BANK ACCOUNTS (Bankkonten)
# ══════════════════════════════════════════════════════════════════════

def PageBankAccounts(db: Database):
    """Generate bank accounts management page"""
    rows = db.fetch_accounts()
    s = Header1('masterdata')
    submenu = '<a href="/masterdata">Stammdaten</a> -> <span id="ActivePage">🏦 Bankkonten</span>'
    s += Header2(submenu)
    s += Header3()
    s += '''
        <h2>Neues Bankkonto anlegen</h2>
        <form method="POST" action="/masterdata/bankaccounts/add">
            <table class="form-table">
                <tr><td>Bezeichnung:</td><td><input type="text" name="name" required></td></tr>
                <tr><td>Inhaber:</td><td><input type="text" name="holder"></td></tr>
                <tr><td>IBAN:</td><td><input type="text" name="iban"></td></tr>
                <tr><td>BIC:</td><td><input type="text" name="bic"></td></tr>
                <tr><td>Bank:</td><td><input type="text" name="bank_name"></td></tr>
                <tr><td>SKR-Gegenkonto:</td><td><input type="number" name="skr_account" placeholder="z.B. 1810" style="width:120px;"></td></tr>
                <tr><td></td><td><input type="submit" value="Konto hinzufügen"></td></tr>
            </table>
        </form>
    '''
    s += "<h2>Vorhandene Konten</h2>"
    s += "<table>"
    s += "<tr><th>ID</th><th>Bezeichnung</th><th>Inhaber</th><th>IBAN</th><th>BIC</th><th>Bank</th><th>Typ</th><th>SKR-Konto</th><th>Aktionen</th></tr>"
    for row in rows:
        account_type = "Kasse" if row[6] == 1 else "Bank"
        skr_display = row[7] if len(row) > 7 and row[7] else "–"
        s += f"<tr><td>{row[0]}</td><td>{row[1]}</td><td>{row[2]}</td><td>{row[3]}</td><td>{row[4]}</td><td>{row[5]}</td><td>{account_type}</td><td>{skr_display}</td>"
        if row[6] == 1:  # is_cash
            s += f"<td><a href='/masterdata/bankaccounts/edit?id={row[0]}'>SKR zuweisen</a></td></tr>"
        else:
            s += f"<td><a href='/masterdata/bankaccounts/edit?id={row[0]}'>Bearbeiten</a> | <a href='/masterdata/bankaccounts/delete?id={row[0]}'>Löschen</a></td></tr>"
    s += "</table>"
    s += Footer()
    return s


def PageBankAccountEdit(db: Database, account_id):
    """Generate bank account edit page"""
    account = db.get_account_by_id(account_id)
    if not account:
        return "Konto nicht gefunden."

    s = Header1('masterdata')
    submenu = '<a href="/masterdata">Stammdaten</a> -> <a href="/masterdata/bankaccounts">🏦 Bankkonten</a> -> <span id="ActivePage">Bearbeiten</span>'
    s += Header2(submenu)
    s += Header3()
    s += "<h1>Bankkonto bearbeiten</h1>"

    skr_val = account[7] if len(account) > 7 and account[7] is not None else ''
    if account[6] == 1:  # is_cash – nur SKR-Konto kann geändert werden
        s += "<p style='color:#666;'>Kasse-Konto: Name und Typ können nicht geändert werden.</p>"
        s += f'''
            <form method="POST" action="/masterdata/bankaccounts/update">
                <table class="form-table">
                    <input type="hidden" name="id" value="{account[0]}">
                    <input type="hidden" name="name" value="{account[1]}">
                    <input type="hidden" name="holder" value="{account[2] or ''}"> 
                    <input type="hidden" name="iban" value="{account[3] or ''}">
                    <input type="hidden" name="bic" value="{account[4] or ''}">
                    <input type="hidden" name="bank_name" value="{account[5] or ''}">
                    <tr><td>Bezeichnung:</td><td>{account[1]}</td></tr>
                    <tr><td>Typ:</td><td>Kasse</td></tr>
                    <tr><td>SKR-Gegenkonto:</td><td><input type="number" name="skr_account" value="{skr_val}" placeholder="z.B. 1460" style="width:120px;"></td></tr>
                    <tr><td></td><td><input type="submit" value="SKR-Konto speichern"></td></tr>
                </table>
            </form>
            <p><a href="/masterdata/bankaccounts">Zurück zur Kontenübersicht</a></p>
        '''
    else:
        s += f'''
            <form method="POST" action="/masterdata/bankaccounts/update">
                <table class="form-table">
                    <tr><td>ID:</td><td><input type="text" name="id" value="{account[0]}" readonly></td></tr>
                    <tr><td>Bezeichnung:</td><td><input type="text" name="name" value="{account[1]}" required></td></tr>
                    <tr><td>Inhaber:</td><td><input type="text" name="holder" value="{account[2]}"></td></tr>
                    <tr><td>IBAN:</td><td><input type="text" name="iban" value="{account[3]}"></td></tr>
                    <tr><td>BIC:</td><td><input type="text" name="bic" value="{account[4]}"></td></tr>
                    <tr><td>Bank:</td><td><input type="text" name="bank_name" value="{account[5]}"></td></tr>
                    <tr><td>SKR-Gegenkonto:</td><td><input type="number" name="skr_account" value="{skr_val}" placeholder="z.B. 1810" style="width:120px;"></td></tr>
                    <tr><td></td><td><input type="submit" value="Konto aktualisieren"></td></tr>
                </table>
            </form>
            <p><a href="/masterdata/bankaccounts">Zurück zur Kontenübersicht</a></p>
        '''
    s += Footer()
    return s


# ══════════════════════════════════════════════════════════════════════════════
# NUMBER RANGES (Nummernkreise)
# ══════════════════════════════════════════════════════════════════════════════

def PageNumberRanges(db: Database):
    """Generate number ranges management page"""
    import datetime
    current_year = datetime.datetime.now().year

    ranges = db.fetch_number_ranges()
    s = Header1('masterdata')
    submenu = '<a href="/masterdata">Stammdaten</a> -> <span id="ActivePage">🔢 Nummernkreise</span>'
    s += Header2(submenu)
    s += Header3()

    s += '''
        <h2>Nummernkreise</h2>
        <p>Nummernkreise definieren die Nummerierung für Rechnungen und Belege.</p>
        <p><strong>Standard-Format:</strong> <code>{yy}{l}{nnn}{s}</code> &rarr; z.B. <strong>26F001</strong> oder <strong>26F002_A</strong></p>
        <ul>
            <li><strong>{yy}</strong> = Jahr (2-stellig, z.B. 26)</li>
            <li><strong>{yyyy}</strong> = Jahr (4-stellig, z.B. 2026)</li>
            <li><strong>{l}</strong> = Buchstabe (z.B. F=Faktura, V=Verbindlichkeit)</li>
            <li><strong>{nnn}</strong> = Laufende Nummer (3-stellig, z.B. 001)</li>
            <li><strong>{s}</strong> = Suffix (optional, z.B. _A, _B &ndash; wird ans Ende angefügt)</li>
        </ul>

        <h3>Neuen Nummernkreis anlegen</h3>
        <form method="POST" action="/masterdata/numberranges/add">
            <table class="form-table">
                <tr><td>Typ:</td><td>
                    <select name="type" required>
                        <option value="invoice">Ausgangsrechnungen</option>
                        <option value="receipt_company">Belegnummern Firma</option>
                        <option value="receipt_category">Belegnummern Kategorien</option>
                    </select>
                </td></tr>
    '''
    s += f'<tr><td>Jahr:</td><td><input type="number" name="year" value="{current_year}" min="2000" max="2099" required></td></tr>'
    s += '''
                <tr><td>Buchstabe:</td><td><input type="text" name="letter" maxlength="1" pattern="[A-Z]" required placeholder="z.B. F" style="width: 50px; text-transform: uppercase;"> (A-Z)</td></tr>
                <tr><td>Suffix (optional):</td><td><input type="text" name="prefix" maxlength="10" placeholder="z.B. _A" style="width: 80px;"> (wird ans Ende der Nummer angehängt)</td></tr>
                <tr><td>Format:</td><td><input type="text" name="number_format" value="{yy}{l}{nnn}{s}" size="20"> Platzhalter: {yy} {yyyy} {l} {nnn} {s}</td></tr>
                <tr><td>Aktuelle Nummer:</td><td><input type="number" name="current_number" value="0" min="0"> (letzte vergebene Nummer)</td></tr>
                <tr><td>Beschreibung:</td><td><input type="text" name="description" size="40"></td></tr>
                <tr><td></td><td><input type="submit" value="Nummernkreis hinzufügen"></td></tr>
            </table>
        </form>

        <h3>Bestehende Nummernkreise</h3>
    '''

    type_names = {
        'invoice': 'Ausgangsrechnungen',
        'receipt_company': 'Belegnummern Firma',
        'receipt_category': 'Belegnummern Kategorien'
    }

    for range_type, type_name in type_names.items():
        type_ranges = [r for r in ranges if r[1] == range_type]
        s += f"<h4>{type_name}</h4>"

        if type_ranges:
            s += "<table>"
            s += "<tr><th>ID</th><th>Jahr</th><th>Buchstabe</th><th>Suffix</th><th>Format</th><th>Aktuelle Nr.</th><th>Nächste Nr.</th><th>Beschreibung</th><th>Aktionen</th></tr>"

            for r in type_ranges:
                # r: ID=0, Type=1, Year=2, Letter=3, Prefix=4, CurrentNumber=5, Description=6, NumberFormat=7
                range_id = r[0]
                year = r[2]
                letter = r[3]
                suffix = r[4] or ''
                current_num = r[5] or 0
                description = r[6] or ''
                number_format = r[7] if len(r) > 7 and r[7] else '{yy}{l}{nnn}{s}'

                next_num = current_num + 1
                next_formatted = db._apply_number_format(number_format, year, letter, next_num, suffix)

                s += f"<tr>"
                s += f"<td>{range_id}</td>"
                s += f"<td>{year}</td>"
                s += f"<td>{letter}</td>"
                s += f"<td>{suffix}</td>"
                s += f"<td><code>{number_format}</code></td>"
                s += f"<td style='text-align: right;'>{current_num}</td>"
                s += f"<td><strong>{next_formatted}</strong></td>"
                s += f"<td>{description}</td>"
                s += f"<td><a href='/masterdata/numberranges/edit?id={range_id}'>Bearbeiten</a> | "
                s += f"<a href='javascript:void(0);' onclick='appConfirmHref(\"/masterdata/numberranges/delete?id={range_id}\", \"Nummernkreis wirklich löschen?\")'>Löschen</a></td>"
                s += f"</tr>"

            s += "</table>"
        else:
            s += "<p><em>Keine Nummernkreise definiert.</em></p>"

    s += Footer()
    return s


def PageNumberRangesEdit(db: Database, range_id):
    """Generate number range edit page"""
    nr = db.get_number_range_by_id(range_id)
    if not nr:
        return "Nummernkreis nicht gefunden."

    # nr: ID=0, Type=1, Year=2, Letter=3, Prefix=4, CurrentNumber=5, Description=6, NumberFormat=7
    range_type = nr[1]
    year = nr[2]
    letter = nr[3]
    suffix = nr[4] or ''
    current_num = nr[5] or 0
    description = nr[6] or ''
    number_format = nr[7] if len(nr) > 7 and nr[7] else '{yy}{l}{nnn}{s}'

    type_names = {
        'invoice': 'Ausgangsrechnungen',
        'receipt_company': 'Belegnummern Firma',
        'receipt_category': 'Belegnummern Kategorien'
    }
    type_name = type_names.get(range_type, range_type)

    next_num = current_num + 1
    next_formatted = db._apply_number_format(number_format, year, letter, next_num, suffix)

    s = Header1('masterdata')
    submenu = '<a href="/masterdata">Stammdaten</a> -> <a href="/masterdata/numberranges">Nummernkreise</a> -> <span id="ActivePage">Bearbeiten</span>'
    s += Header2(submenu)
    s += Header3()

    s += f'''
        <h2>Nummernkreis bearbeiten</h2>
        <p>Typ: <strong>{type_name}</strong></p>
        <p>Nächste Nummer wird sein: <strong>{next_formatted}</strong></p>

        <form method="POST" action="/masterdata/numberranges/update">
            <input type="hidden" name="id" value="{range_id}">
            <input type="hidden" name="type" value="{range_type}">
            <table class="form-table">
                <tr><td>Jahr:</td><td><input type="number" name="year" value="{year}" min="2000" max="2099" required></td></tr>
                <tr><td>Buchstabe:</td><td><input type="text" name="letter" value="{letter}" maxlength="1" pattern="[A-Z]" required style="width: 50px; text-transform: uppercase;"></td></tr>
                <tr><td>Suffix (optional):</td><td><input type="text" name="prefix" value="{suffix}" maxlength="10" style="width: 80px;"> (wird ans Ende der Nummer angehängt, z.B. _A)</td></tr>
                <tr><td>Format:</td><td><input type="text" name="number_format" value="{number_format}" size="20"> Platzhalter: {{yy}} {{yyyy}} {{l}} {{nnn}} {{s}}</td></tr>
                <tr><td>Aktuelle Nummer:</td><td><input type="number" name="current_number" value="{current_num}" min="0"> (letzte vergebene Nummer)</td></tr>
                <tr><td>Beschreibung:</td><td><input type="text" name="description" value="{description}" size="40"></td></tr>
                <tr><td></td><td><input type="submit" value="Nummernkreis aktualisieren"></td></tr>
            </table>
        </form>
        <p><a href="/masterdata/numberranges">Zurück zur Übersicht</a></p>
    '''
    s += Footer()
    return s
