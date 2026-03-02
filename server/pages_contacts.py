"""
Contacts management pages (Stammdaten/Kontakte)
Option C – normalized structure:
  Contacts         – base: ID, ContactType, EntityType, DisplayName, CustomerNumber, Email, Phone, Notes, Logo
  ContactAddresses – 1:n  (AddressType='main' for primary address)
  CompanyDetails   – 1:1  for EntityType='company'
  PersonDetails    – 1:1  for EntityType='person'
"""
from db import Database


def Header1(active_page=None):
    from server.pages import Header1 as _H
    return _H(active_page)

def Header2(content=""):
    from server.pages import Header2 as _H
    return _H(content)

def Header3(content=""):
    from server.pages import Header3 as _H
    return _H(content)

def Footer():
    from server.pages import Footer as _F
    return _F()


# ── Constants ────────────────────────────────────────────────────────────────

CONTACT_TYPES = [
    ('customer',  'Kunde'),
    ('supplier',  'Lieferant'),
    ('own',       'Eigene Daten'),
    ('insurance', 'Versicherung'),
    ('other',     'Sonstiges'),
]

LEGAL_FORMS = [
    '', 'GmbH', 'AG', 'UG (haftungsbeschränkt)', 'GbR', 'OHG', 'KG', 'KGaA',
    'e.K.', 'e.V.', 'Freiberufler', 'Einzelunternehmen', 'Sonstiges',
]

COUNTRIES = [
    ('DE', 'Deutschland'), ('AT', 'Österreich'), ('CH', 'Schweiz'),
    ('FR', 'Frankreich'), ('NL', 'Niederlande'), ('BE', 'Belgien'),
    ('PL', 'Polen'), ('IT', 'Italien'), ('ES', 'Spanien'),
    ('GB', 'Großbritannien'), ('US', 'USA'),
]


# ── Helper: option builders ───────────────────────────────────────────────────

def _sel(val, current):
    return 'selected' if val == current else ''

def _type_opts(current):
    return ''.join(f'<option value="{v}" {_sel(v,current)}>{l}</option>' for v, l in CONTACT_TYPES)

def _country_opts(current):
    return ''.join(f'<option value="{c}" {_sel(c,current)}>{n} ({c})</option>' for c, n in COUNTRIES)

def _legal_form_opts(current):
    return ''.join(f'<option value="{lf}" {_sel(lf,current)}>{lf if lf else "– keine –"}</option>'
                   for lf in LEGAL_FORMS)

def _sal_opts(current):
    return ''.join(f'<option value="{v}" {_sel(v,current)}>{v if v else "–"}</option>'
                   for v in ['', 'Herr', 'Frau', 'Divers'])


# ── Helper: shared logo-picker JS ─────────────────────────────────────────────

_LOGO_JS = r'''
        function updateLogoPath(input, inputId, previewId) {
            if (!input.files || !input.files[0]) return;
            const filePath = input.value;
            let displayPath = filePath;
            if (filePath.includes('\\')) {
                const parts = filePath.split('\\');
                const filename = parts[parts.length - 1];
                if (filePath.toLowerCase().includes('\\static\\')) {
                    displayPath = 'static/' + filename;
                } else if (filePath.toLowerCase().includes('\\pybuch\\')) {
                    const idx = filePath.toLowerCase().indexOf('\\pybuch\\');
                    displayPath = filePath.substring(idx + 8).replace(/\\/g, '/');
                } else {
                    displayPath = 'static/' + filename;
                }
            }
            document.getElementById(inputId).value = displayPath;
            const reader = new FileReader();
            reader.onload = function(e) {
                document.getElementById(previewId).innerHTML =
                    '<img src="' + e.target.result + '" style="max-width:150px;max-height:80px;border:1px solid #ccc;">';
            };
            reader.readAsDataURL(input.files[0]);
        }
'''


# ── Helper: shared contact form ───────────────────────────────────────────────

def _contact_form(db: Database, form_action: str, entity_type: str = 'company',
                  c=None, extra_hidden: str = '') -> str:
    """Build the HTML form for creating or editing a contact.

    c  – sqlite3.Row from fetch_contacts (column order defined in db.py), or None.
    Columns used by index (backward-compat with invoice page):
      0=id, 1=contact_type, 2=customer_number, 3=display_name, 4=company_name,
      5=street, 6=postal_code, 7=city, 8=country, 9=email, 10=phone, 11=tax_id,
      12=notes, 13=logo, 14=buyer_route_id, 15=entity_type, 16=display_name_manual,
      17=legal_form, 18=salutation, 19=title, 20=first_name, 21=last_name,
      22=date_of_birth, 23=company_contact_id, 24=company_name_free, 25=address_line1
    """

    def g(idx, default=''):
        if c is None:
            return default
        try:
            v = c[idx]
            return v if v is not None else default
        except (IndexError, TypeError):
            return default

    contact_type     = g(1,  'customer')
    customer_number  = g(2,  '')
    display_name_val = g(16, '')   # manually set override (index 16)
    email            = g(9,  '')
    phone            = g(10, '')
    notes            = g(12, '')
    logo             = g(13, '')
    # address
    address_line1    = g(25, '')
    street           = g(5,  '')
    postal_code      = g(6,  '')
    city             = g(7,  '')
    country          = g(8,  'DE')
    # company
    company_name     = g(4,  '')
    legal_form       = g(17, '')
    tax_id           = g(11, '')
    buyer_route_id   = g(14, '')
    # person
    salutation       = g(18, '')
    title_val        = g(19, '')
    first_name       = g(20, '')
    last_name        = g(21, '')
    date_of_birth    = g(22, '')
    company_contact_id = g(23, '')
    company_name_free  = g(24, '')

    show_c = '' if entity_type == 'company' else 'none'
    show_p = '' if entity_type == 'person'  else 'none'
    entity_label = "🏢 Unternehmen" if entity_type == 'company' else "👤 Person"

    # Alt-entity switch (only for new contact forms)
    if not extra_hidden:
        alt       = 'person' if entity_type == 'company' else 'company'
        alt_label = '👤 Als Person anlegen' if entity_type == 'company' else '🏢 Als Unternehmen anlegen'
        alt_link  = f'/masterdata/contacts/new?entity={alt}'
        alt_switch = f'&nbsp; <a href="{alt_link}" style="font-size:0.85em;color:#666;">{alt_label}</a>'
    else:
        alt_switch = ''

    # Companies dropdown for person-company link
    companies    = db.fetch_contacts(entity_type='company')
    company_opts = '<option value="">– keine Firma verknüpft –</option>'
    for comp in companies:
        comp_id   = comp[0]
        comp_name = comp[3] or f'ID {comp[0]}'
        sel = 'selected' if str(comp_id) == str(company_contact_id) else ''
        company_opts += f'<option value="{comp_id}" {sel}>{comp_name}</option>'

    logo_preview = f'<img src="{logo}" style="max-width:150px;max-height:80px;border:1px solid #ccc;">' if logo else ''

    s  = f'<form method="POST" action="{form_action}">'
    s += extra_hidden
    s += f'<input type="hidden" name="entity_type" value="{entity_type}">'
    s += f'''
    <table style="width:100%; border-collapse:collapse; margin-bottom:20px;">

        <!-- ─── Allgemein ─── -->
        <tr style="background:#f0f0f0;">
            <td colspan="2" style="padding:8px;font-weight:bold;font-size:1.05em;">
                {entity_label} {alt_switch}
            </td>
        </tr>
        <tr>
            <td style="width:230px;padding:6px 12px;">Kontakttyp:</td>
            <td style="padding:6px;"><select name="contact_type">{_type_opts(contact_type)}</select></td>
        </tr>
        <tr>
            <td style="padding:6px 12px;">Anzeigename:</td>
            <td style="padding:6px;">
                <input type="text" name="display_name" id="field_display_name"
                       value="{display_name_val}" style="width:360px;"
                       placeholder="Wird automatisch aus den Feldern generiert">
                <small style="color:#888;display:block;">Leer lassen für automatische Generierung</small>
            </td>
        </tr>
        <tr>
            <td style="padding:6px 12px;">Kunden-/Kontaktnummer:</td>
            <td style="padding:6px;">
                <input type="text" name="customer_number" value="{customer_number}"
                       style="width:200px;" placeholder="z.B. K-12345">
            </td>
        </tr>

        <!-- ─── Unternehmens-Felder ─── -->
        <tr id="sec_company" style="background:#e8f4e8;display:{show_c};">
            <td colspan="2" style="padding:8px;font-weight:bold;">🏢 Unternehmen</td>
        </tr>
        <tr id="row_company_name" style="display:{show_c};">
            <td style="padding:6px 12px;">Firmenname: *</td>
            <td style="padding:6px;">
                <input type="text" name="company_name" id="field_company_name"
                       value="{company_name}" style="width:360px;">
            </td>
        </tr>
        <tr id="row_legal_form" style="display:{show_c};">
            <td style="padding:6px 12px;">Rechtsform:</td>
            <td style="padding:6px;">
                <select name="legal_form" style="width:220px;">{_legal_form_opts(legal_form)}</select>
            </td>
        </tr>
        <tr id="row_tax_id" style="display:{show_c};">
            <td style="padding:6px 12px;">USt-IdNr / Steuer-Nr:</td>
            <td style="padding:6px;">
                <input type="text" name="tax_id" value="{tax_id}"
                       placeholder="z.B. DE123456789" style="width:280px;">
            </td>
        </tr>
        <tr id="row_buyer_route_id" style="display:{show_c};">
            <td style="padding:6px 12px;">Leitweg-ID (B2G):</td>
            <td style="padding:6px;">
                <input type="text" name="buyer_route_id" value="{buyer_route_id}"
                       placeholder="z.B. 991-ABCDE-12" style="width:280px;">
                <small style="color:#666;display:block;">Nur für öffentliche Auftraggeber (XRechnung B2G)</small>
            </td>
        </tr>

        <!-- ─── Personen-Felder ─── -->
        <tr id="sec_person" style="background:#e8ecf4;display:{show_p};">
            <td colspan="2" style="padding:8px;font-weight:bold;">👤 Person</td>
        </tr>
        <tr id="row_salutation" style="display:{show_p};">
            <td style="padding:6px 12px;">Anrede:</td>
            <td style="padding:6px;"><select name="salutation">{_sal_opts(salutation)}</select></td>
        </tr>
        <tr id="row_title" style="display:{show_p};">
            <td style="padding:6px 12px;">Titel:</td>
            <td style="padding:6px;">
                <input type="text" name="title" value="{title_val}"
                       placeholder="z.B. Dr., Prof." style="width:150px;">
            </td>
        </tr>
        <tr id="row_first_name" style="display:{show_p};">
            <td style="padding:6px 12px;">Vorname: *</td>
            <td style="padding:6px;">
                <input type="text" name="first_name" id="field_first_name"
                       value="{first_name}" style="width:280px;">
            </td>
        </tr>
        <tr id="row_last_name" style="display:{show_p};">
            <td style="padding:6px 12px;">Nachname: *</td>
            <td style="padding:6px;">
                <input type="text" name="last_name" id="field_last_name"
                       value="{last_name}" style="width:280px;">
            </td>
        </tr>
        <tr id="row_dob" style="display:{show_p};">
            <td style="padding:6px 12px;">Geburtsdatum:</td>
            <td style="padding:6px;">
                <input type="date" name="date_of_birth" value="{date_of_birth}" style="width:180px;">
            </td>
        </tr>
        <tr id="row_company_link" style="display:{show_p};">
            <td style="padding:6px 12px;">Zugehörige Firma:</td>
            <td style="padding:6px;">
                <select name="company_contact_id" style="width:330px;">{company_opts}</select>
                <br>
                <small style="color:#666;">Oder Freitext, falls Firma nicht in der DB:</small><br>
                <input type="text" name="company_name_free" value="{company_name_free}"
                       placeholder="Firmenname (Freitext)" style="width:330px;margin-top:4px;">
            </td>
        </tr>

        <!-- ─── Adresse ─── -->
        <tr style="background:#f5f0e8;">
            <td colspan="2" style="padding:8px;font-weight:bold;">📍 Adresse</td>
        </tr>
        <tr>
            <td style="padding:6px 12px;">Zusatzzeile:</td>
            <td style="padding:6px;">
                <input type="text" name="address_line1" value="{address_line1}" style="width:420px;"
                       placeholder="z.Hd. Max Mustermann · Abt. Einkauf · c/o ...">
                <small style="color:#888;display:block;">Erscheint zwischen Firmenname und Straße</small>
            </td>
        </tr>
        <tr>
            <td style="padding:6px 12px;">Straße / Nr.:</td>
            <td style="padding:6px;">
                <input type="text" name="street" value="{street}" style="width:360px;">
            </td>
        </tr>
        <tr>
            <td style="padding:6px 12px;">PLZ:</td>
            <td style="padding:6px;">
                <input type="text" name="postal_code" value="{postal_code}" style="width:100px;">
            </td>
        </tr>
        <tr>
            <td style="padding:6px 12px;">Stadt:</td>
            <td style="padding:6px;">
                <input type="text" name="city" value="{city}" style="width:260px;">
            </td>
        </tr>
        <tr>
            <td style="padding:6px 12px;">Land:</td>
            <td style="padding:6px;">
                <select name="country" style="width:230px;">{_country_opts(country)}</select>
            </td>
        </tr>

        <!-- ─── Kontakt & Sonstiges ─── -->
        <tr style="background:#f0f0f0;">
            <td colspan="2" style="padding:8px;font-weight:bold;">📞 Kontakt & Sonstiges</td>
        </tr>
        <tr>
            <td style="padding:6px 12px;">E-Mail:</td>
            <td style="padding:6px;">
                <input type="email" name="email" value="{email}" style="width:300px;">
            </td>
        </tr>
        <tr>
            <td style="padding:6px 12px;">Telefon:</td>
            <td style="padding:6px;">
                <input type="tel" name="phone" value="{phone}" style="width:200px;">
            </td>
        </tr>
        <tr>
            <td style="padding:6px 12px;">Logo / Bild:</td>
            <td style="padding:6px;">
                <input type="text" name="logo" id="field_logo" value="{logo}"
                       placeholder="/static/logo.png oder URL" style="width:360px;">
                <button type="button"
                        onclick="document.getElementById('logo_file_input').click()">Datei wählen</button>
                <input type="file" id="logo_file_input" accept="image/*" style="display:none;"
                       onchange="updateLogoPath(this,'field_logo','logo_preview_div')">
                <div id="logo_preview_div" style="margin-top:5px;">{logo_preview}</div>
            </td>
        </tr>
        <tr>
            <td style="padding:6px 12px;">Notizen:</td>
            <td style="padding:6px;">
                <textarea name="notes" rows="3" style="width:420px;">{notes}</textarea>
            </td>
        </tr>
        <tr>
            <td></td>
            <td style="padding:12px;">
                <input type="submit" value="Speichern"
                       style="padding:8px 24px;background:#4CAF50;color:white;border:none;border-radius:4px;cursor:pointer;font-size:1em;">
                &nbsp; <a href="/masterdata/contacts">Abbrechen</a>
            </td>
        </tr>
    </table>
    </form>
    '''

    # Append JS (entity_type is a Python variable – no JS interpolation needed)
    s += f'''
    <script>
        {_LOGO_JS}

        // Auto-populate Anzeigename placeholder
        function refreshDisplayNameHint() {{
            const display = document.getElementById('field_display_name');
            if (!display || display.value.trim() !== '') return;
            let hint = '';
            const et = '{entity_type}';
            if (et === 'company') {{
                const cn = document.getElementById('field_company_name');
                if (cn) hint = cn.value.trim();
            }} else {{
                const title  = document.querySelector('input[name="title"]');
                const fn     = document.getElementById('field_first_name');
                const ln     = document.getElementById('field_last_name');
                const parts  = [];
                if (title && title.value.trim()) parts.push(title.value.trim());
                if (fn    && fn.value.trim())    parts.push(fn.value.trim());
                if (ln    && ln.value.trim())    parts.push(ln.value.trim());
                hint = parts.join(' ');
            }}
            display.placeholder = hint || '(wird automatisch gesetzt)';
        }}

        ['field_company_name','field_first_name','field_last_name'].forEach(id => {{
            const el = document.getElementById(id);
            if (el) el.addEventListener('input', refreshDisplayNameHint);
        }});
        const titleEl = document.querySelector('input[name="title"]');
        if (titleEl) titleEl.addEventListener('input', refreshDisplayNameHint);

        refreshDisplayNameHint();
    </script>
    '''
    return s


# ── Page: Contacts List ───────────────────────────────────────────────────────

def PageContacts(db: Database, contact_type_filter=None, entity_type_filter=None):
    """Contacts overview list with filter tabs."""
    contacts = db.fetch_contacts(contact_type=contact_type_filter, entity_type=entity_type_filter)

    s = Header1('masterdata')
    submenu = '<a href="/masterdata">Stammdaten</a> -> <span id="ActivePage">👥 Kontakte</span>'
    s += Header2(submenu)

    header3 = '''
        <strong>Entität:</strong>
        <a href="/masterdata/contacts">Alle</a>
        <a href="/masterdata/contacts?entity=company">🏢 Unternehmen</a>
        <a href="/masterdata/contacts?entity=person">👤 Personen</a>
        &nbsp;|&nbsp;
        <strong>Typ:</strong>
        <a href="/masterdata/contacts">Alle</a>
        <a href="/masterdata/contacts?type=customer">Kunden</a>
        <a href="/masterdata/contacts?type=supplier">Lieferanten</a>
        <a href="/masterdata/contacts?type=own">Eigene Daten</a>
        <a href="/masterdata/contacts?type=insurance">Versicherungen</a>
        <a href="/masterdata/contacts?type=other">Sonstige</a>
    '''
    s += Header3(header3)

    s += '''
        <div class="rectRounded">
            <a href="/masterdata/contacts/new?entity=company" class="coloredButton btn-green">
                + 🏢 Unternehmen anlegen
            </a>
            <a href="/masterdata/contacts/new?entity=person" class="coloredButton btn-indigo">
                + 👤 Person anlegen
            </a>
        </div>
    '''

    type_labels = dict(CONTACT_TYPES)

    s += "<table border='1' style='width:100%;border-collapse:collapse;'>"
    s += ("<tr><th>ID</th><th>Entität</th><th>Typ</th><th>Anzeigename</th>"
          "<th>Kd-Nr.</th><th>Firma / Zuordnung</th><th>E-Mail</th><th>Telefon</th><th>Aktionen</th></tr>")

    for c in contacts:
        cid          = c[0]
        c_type       = c[1] or 'customer'
        cust_nr      = c[2] or ''
        display_name = c[3] or '–'
        company_name = c[4] or ''
        email        = c[9] or ''
        phone        = c[10] or ''
        entity_type  = c[15] if len(c) > 15 else 'company'
        entity_icon  = "🏢" if entity_type == 'company' else "👤"
        type_label   = type_labels.get(c_type, c_type)

        s += f"<tr>"
        s += f"<td>{cid}</td>"
        s += f"<td style='text-align:center;font-size:1.2em;'>{entity_icon}</td>"
        s += f"<td>{type_label}</td>"
        s += f"<td><strong>{display_name}</strong></td>"
        s += f"<td>{cust_nr}</td>"
        s += f"<td><small>{company_name}</small></td>"
        s += f"<td>{email}</td>"
        s += f"<td>{phone}</td>"
        s += f"<td>"
        s += f"<a href='/masterdata/contacts/edit?id={cid}'>Bearbeiten</a> | "
        s += f"<a href='/masterdata/contacts/delete?id={cid}' "
        s += f"onclick='return confirm(\"Kontakt wirklich löschen?\")'>Löschen</a>"
        s += f"</td></tr>"

    if not contacts:
        s += "<tr><td colspan='9' style='text-align:center;padding:20px;'><em>Keine Kontakte vorhanden.</em></td></tr>"

    s += "</table>"
    s += Footer()
    return s


# ── Page: New Contact ─────────────────────────────────────────────────────────

def PageContactNew(db: Database, entity_type: str = 'company'):
    """New contact creation page."""
    entity_label = "🏢 Unternehmen" if entity_type == 'company' else "👤 Person"
    s = Header1('masterdata')
    submenu = (f'<a href="/masterdata">Stammdaten</a> -> '
               f'<a href="/masterdata/contacts">Kontakte</a> -> '
               f'<span id="ActivePage">Neu: {entity_label}</span>')
    s += Header2(submenu)
    s += Header3()
    s += f'<h2>Neuen Kontakt anlegen: {entity_label}</h2>'
    s += _contact_form(db, form_action='/masterdata/contacts/add', entity_type=entity_type)
    s += Footer()
    return s


# ── Page: Edit Contact ────────────────────────────────────────────────────────

def PageContactEdit(db: Database, contact_id):
    """Contact edit page."""
    contact = db.get_contact_by_id(contact_id)
    if not contact:
        return "Kontakt nicht gefunden."

    # EntityType at index 15 in the new query
    entity_type = 'company'
    try:
        entity_type = contact[15] or 'company'
    except (IndexError, TypeError):
        pass

    display_name_shown = contact[3] or '–'
    entity_label = "🏢 Unternehmen" if entity_type == 'company' else "👤 Person"

    s = Header1('masterdata')
    submenu = (f'<a href="/masterdata">Stammdaten</a> -> '
               f'<a href="/masterdata/contacts">Kontakte</a> -> '
               f'<span id="ActivePage">{entity_label} bearbeiten</span>')
    s += Header2(submenu)
    s += Header3()
    s += f'<h2>Kontakt bearbeiten: <em>{display_name_shown}</em></h2>'
    s += _contact_form(
        db,
        form_action='/masterdata/contacts/update',
        entity_type=entity_type,
        c=contact,
        extra_hidden=f'<input type="hidden" name="contact_id" value="{contact_id}">',
    )
    s += Footer()
    return s
