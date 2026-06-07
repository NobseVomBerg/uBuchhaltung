"""
Contacts management pages (Stammdaten/Kontakte)
Option C – normalized structure:
  Contacts         – base: ID, ContactType, EntityType, DisplayName, CustomerNumber, Email, Phone, Notes, Logo
  ContactAddresses – 1:n  (AddressType='main' for primary address)
  CompanyDetails   – 1:1  for EntityType='company'
  PersonDetails    – 1:1  for EntityType='person'
"""
import html as _html
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
    ('customer',         'Kunde'),
    ('supplier',         'Lieferant'),
    ('prospect',         'Interessent'),
    ('employee',         'Mitarbeiter'),
    ('freelancer',       'Freelancer'),
    ('partner',          'Partner'),
    ('service_provider', 'Dienstleister'),
    ('authority',        'Behörde'),
    ('bank',             'Bank'),
    ('other',            'Sonstiges'),
]

PERSON_ROLES = [
    ('invoice_recipient', 'Rechnungsempfänger'),
    ('orderer',           'Besteller'),
    ('technical',         'Techn. Ansprechpartner'),
    ('purchasing',        'Einkauf'),
    ('accounting',        'Buchhaltung'),
    ('data_protection',   'Datenschutzbeauftragter'),
    ('contract',          'Vertragsverantwortlicher'),
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

def _checkbox_grid(items, name, active_keys, wide=False):
    """Render a list of (key, label) tuples as a 3-column CSS-grid div.
    wide=True uses .check-grid-wide (200px columns) for longer labels.
    """
    css_class = 'check-grid-wide' if wide else 'check-grid'
    labels = []
    for key, label in items:
        chk = 'checked' if key in active_keys else ''
        labels.append(
            f'<label><input type="checkbox" name="{name}" value="{key}" {chk}> {label}</label>'
        )
    return f'<div class="{css_class}">{"".join(labels)}</div>'

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
                if (filePath.toLowerCase().includes('\\private\\')) {
                    displayPath = 'seed_data/private/' + filename;
                } else if (filePath.toLowerCase().includes('\\pybuch\\')) {
                    const idx = filePath.toLowerCase().indexOf('\\pybuch\\');
                    displayPath = filePath.substring(idx + 8).replace(/\\/g, '/');
                } else {
                    displayPath = 'seed_data/private/' + filename;
                }
            }
            document.getElementById(inputId).value = displayPath;
            const reader = new FileReader();
            reader.onload = function(e) {
                document.getElementById(previewId).innerHTML =
                    '<img src="' + e.target.result + '" class="logo-preview">';
            };
            reader.readAsDataURL(input.files[0]);
        }
'''


# ── Helper: shared contact form ───────────────────────────────────────────────

def _contact_form(db: Database, form_action: str, entity_type: str = 'company',
                  c=None, extra_hidden: str = '') -> str:
    """Build the HTML form for creating or editing a contact.

    c  – sqlite3.Row from fetch_contacts (column order defined in db.py), or None.
    Columns used by index:
      0=id, 1=contact_type, 2=customer_number, 3=display_name, 4=company_name,
      5=street, 6=postal_code, 7=city, 8=country, 9=email, 10=phone, 11=tax_id,
      12=notes, 13=logo, 14=buyer_route_id, 15=entity_type, 16=display_name_manual,
      17=legal_form, 18=salutation, 19=title, 20=first_name, 21=last_name,
      22=date_of_birth, 23=company_contact_id, 24=company_name_free, 25=address_line1,
      26=abbreviation, 27=type_keys, 28=job_title, 29=department,
      30=is_primary_contact, 31=role_keys
    """

    def g(idx, default=''):
        if c is None:
            return default
        try:
            v = c[idx]
            return v if v is not None else default
        except (IndexError, TypeError):
            return default

    contact_id_val      = g(0,  '')
    contact_type        = g(1,  'customer')
    customer_number     = g(2,  '')
    display_name_val    = g(16, '')   # manually set override (index 16)
    abbreviation        = g(26, '')
    type_keys_str       = g(27, '')
    job_title           = g(28, '')
    department          = g(29, '')
    is_primary_contact  = g(30, 0)
    role_keys_str       = g(31, '')

    # Parse comma-separated keys from DB
    active_type_keys = [t.strip() for t in type_keys_str.split(',') if t.strip()]
    if not active_type_keys and contact_type not in ('own',):
        active_type_keys = [contact_type]
    active_role_keys = [r.strip() for r in role_keys_str.split(',') if r.strip()]
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

    # XSS-Schutz: freie Texteingaben vor dem Einsetzen in HTML escapen.
    # Felder, die Auswahl-Helfer speisen (legal_form, country, salutation) oder
    # IDs/Flags sind, bleiben bewusst roh, damit Options-Matching/Logik stimmt.
    _esc = _html.escape
    customer_number   = _esc(str(customer_number))
    display_name_val  = _esc(str(display_name_val))
    abbreviation      = _esc(str(abbreviation))
    email             = _esc(str(email))
    phone             = _esc(str(phone))
    notes             = _esc(str(notes))
    address_line1     = _esc(str(address_line1))
    street            = _esc(str(street))
    postal_code       = _esc(str(postal_code))
    city              = _esc(str(city))
    company_name      = _esc(str(company_name))
    tax_id            = _esc(str(tax_id))
    buyer_route_id    = _esc(str(buyer_route_id))
    title_val         = _esc(str(title_val))
    first_name        = _esc(str(first_name))
    last_name         = _esc(str(last_name))
    job_title         = _esc(str(job_title))
    department        = _esc(str(department))
    company_name_free = _esc(str(company_name_free))

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
        comp_name = _html.escape(comp[3]) if comp[3] else f'ID {comp[0]}'
        sel = 'selected' if str(comp_id) == str(company_contact_id) else ''
        company_opts += f'<option value="{comp_id}" {sel}>{comp_name}</option>'

    logo_preview = f'<img src="{logo}" class="logo-preview">' if logo else ''

    # Fachliche Rollen (nur für Personen) – muss vor entity_section stehen (wird darin referenziert)
    if entity_type == 'person':
        roles_grid = _checkbox_grid(PERSON_ROLES, 'role_keys', active_role_keys, wide=True)
        roles_section = f'''
        <tr><th colspan="2">🎯 Fachliche Rollen</th></tr>
        <tr><td colspan="2" style="padding:6px 8px;">{roles_grid}</td></tr>'''
    else:
        roles_section = ''

    # Nur die zur Entität passende Sektion rendern (Handler verträgt fehlende Felder)
    if entity_type == 'company':
        entity_section = f'''
        <tr><th colspan="2">🏢 Unternehmen</th></tr>
        <tr><td>Firmenname: *</td>
            <td><input type="text" name="company_name" id="field_company_name" value="{company_name}"></td></tr>
        <tr><td>Rechtsform:</td>
            <td><select name="legal_form">{_legal_form_opts(legal_form)}</select></td></tr>
        <tr><td>USt-IdNr / Steuer-Nr:</td>
            <td><input type="text" name="tax_id" value="{tax_id}" placeholder="z.B. DE123456789"></td></tr>
        <tr><td>Leitweg-ID (B2G):</td>
            <td><input type="text" name="buyer_route_id" value="{buyer_route_id}" placeholder="z.B. 991-ABCDE-12">
                <small class="muted">Nur für öffentliche Auftraggeber (XRechnung B2G)</small></td></tr>
        '''
    else:
        entity_section = f'''
        <tr><th colspan="2">👤 Person</th></tr>
        <tr><td>Anrede:</td>
            <td><select name="salutation">{_sal_opts(salutation)}</select></td></tr>
        <tr><td>Titel:</td>
            <td><input type="text" name="title" value="{title_val}" placeholder="z.B. Dr., Prof."></td></tr>
        <tr><td>Vorname: *</td>
            <td><input type="text" name="first_name" id="field_first_name" value="{first_name}"></td></tr>
        <tr><td>Nachname: *</td>
            <td><input type="text" name="last_name" id="field_last_name" value="{last_name}"></td></tr>
        <tr><td>Geburtsdatum:</td>
            <td><input type="date" name="date_of_birth" value="{date_of_birth}"></td></tr>
        <tr><td>Position / Jobtitel:</td>
            <td><input type="text" name="job_title" value="{job_title}" placeholder="z.B. CEO, Leiter Einkauf"></td></tr>
        <tr><td>Abteilung:</td>
            <td><input type="text" name="department" value="{department}" placeholder="z.B. Buchhaltung, Vertrieb"></td></tr>
        <tr><td>Primärkontakt:</td>
            <td><label><input type="checkbox" name="is_primary_contact" value="1"
                {'checked' if is_primary_contact else ''}>
                Hauptansprechpartner dieser Firma</label></td></tr>
        <tr><td>Zugehörige Firma:</td>
            <td><select name="company_contact_id">{company_opts}</select>
                <small class="muted">Oder Freitext, falls Firma nicht in der DB:</small>
                <input type="text" name="company_name_free" value="{company_name_free}" placeholder="Firmenname (Freitext)"></td></tr>
        {roles_section}
        '''

    # Kontakttyp-Bereich: Checkboxen (außer bei 'own')
    if contact_type == 'own':
        type_section = '<span class="badge" style="background:#e8f0fe;color:#1a73e8;padding:3px 8px;border-radius:4px;">⭐ Eigene Daten</span>'
    else:
        type_section = _checkbox_grid(CONTACT_TYPES, 'type_keys', active_type_keys)

    s  = f'<form method="POST" action="{form_action}" id="contact_form">'
    s += extra_hidden
    s += f'<input type="hidden" name="entity_type" value="{entity_type}">'
    s += f'''
    <table class="form-table">

        <tr><th colspan="2">{entity_label} {alt_switch}</th></tr>
        <tr><td>Kontakttyp:</td>
            <td>{type_section}</td></tr>
        <tr><td>Anzeigename:</td>
            <td><input type="text" name="display_name" id="field_display_name" value="{display_name_val}"
                       placeholder="Wird automatisch aus den Feldern generiert">
                <small class="muted">Leer lassen für automatische Generierung</small></td></tr>
        <tr><td>Kunden-/Kontaktnummer:</td>
            <td><input type="text" name="customer_number" value="{customer_number}" placeholder="z.B. K-12345"></td></tr>
        <tr><td>Kürzel:</td>
            <td><input type="text" name="abbreviation" id="field_abbreviation" value="{abbreviation}"
                       maxlength="10" style="width:90px;text-transform:uppercase;" placeholder="z.B. MS">
                <span id="abbreviation_feedback" style="margin-left:6px;"></span>
                <small class="muted">2–3 Buchstaben, wird aus dem Namen vorgeschlagen</small></td></tr>

        {entity_section}

        <tr><th colspan="2">📍 Adresse</th></tr>
        <tr><td>Zusatzzeile:</td>
            <td><input type="text" name="address_line1" value="{address_line1}"
                       placeholder="z.Hd. Max Mustermann · Abt. Einkauf · c/o ...">
                <small class="muted">Erscheint zwischen Firmenname und Straße</small></td></tr>
        <tr><td>Straße / Nr.:</td>
            <td><input type="text" name="street" value="{street}"></td></tr>
        <tr><td>PLZ:</td>
            <td><input type="text" name="postal_code" value="{postal_code}"></td></tr>
        <tr><td>Stadt:</td>
            <td><input type="text" name="city" value="{city}"></td></tr>
        <tr><td>Land:</td>
            <td><select name="country">{_country_opts(country)}</select></td></tr>

        <tr><th colspan="2">📞 Kontakt &amp; Sonstiges</th></tr>
        <tr><td>E-Mail:</td>
            <td><input type="email" name="email" value="{email}"></td></tr>
        <tr><td>Telefon:</td>
            <td><input type="tel" name="phone" value="{phone}"></td></tr>
        <tr><td>Logo / Bild:</td>
            <td><input type="text" name="logo" id="field_logo" value="{logo}"
                       placeholder="seed_data/private/logo.png oder URL">
                <button type="button"
                        onclick="document.getElementById('logo_file_input').click()">Datei wählen</button>
                <input type="file" id="logo_file_input" accept="image/*" style="display:none"
                       onchange="updateLogoPath(this,'field_logo','logo_preview_div')">
                <div id="logo_preview_div">{logo_preview}</div></td></tr>
        <tr><td>Notizen:</td>
            <td><textarea name="notes" rows="3">{notes}</textarea></td></tr>
    </table>
    </form>
    '''

    # Append JS (entity_type and contact_id_val are Python variables)
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

        // ── Kürzel-Logik ─────────────────────────────────────────────────────

        function _buildAbbreviation() {{
            const et = '{entity_type}';
            if (et === 'person') {{
                const fn = (document.getElementById('field_first_name')?.value || '').trim();
                const ln = (document.getElementById('field_last_name')?.value  || '').trim();
                if (!fn && !ln) return '';
                const lnParts = ln.split(/[-\\s]+/).filter(p => p.length > 0);
                const fnParts = fn.split(/[-\\s]+/).filter(p => p.length > 0);
                let raw = '';
                if (lnParts.length > 1) {{
                    raw = (fnParts[0]?.[0] || '') + lnParts[0][0] + lnParts[1][0];
                }} else if (fnParts.length > 1) {{
                    raw = fnParts[0][0] + fnParts[1][0];
                }} else {{
                    raw = (fnParts[0]?.[0] || '') + (lnParts[0]?.[0] || '');
                }}
                return raw.toUpperCase();
            }} else {{
                const cn = (document.getElementById('field_company_name')?.value || '').trim();
                if (!cn) return '';
                const words = cn.split(/\\s+/).filter(w => w.length > 0);
                let raw = '';
                if (words.length >= 3) {{
                    raw = words[0][0] + words[1][0] + words[2][0];
                }} else if (words.length === 2) {{
                    raw = words[0].slice(0, 2) + words[1][0];
                }} else {{
                    raw = cn.slice(0, 3);
                }}
                return raw.toUpperCase();
            }}
        }}

        function checkAbbreviationUnique() {{
            const abbr     = document.getElementById('field_abbreviation');
            const feedback = document.getElementById('abbreviation_feedback');
            if (!abbr || !feedback) return;
            const value = abbr.value.trim();
            if (!value) {{ feedback.innerHTML = ''; abbr.style.borderColor = ''; return; }}
            const excludeId = '{contact_id_val}';
            const url = '/masterdata/contacts/check-abbreviation?value=' + encodeURIComponent(value)
                        + (excludeId ? '&exclude_id=' + excludeId : '');
            fetch(url)
                .then(r => r.json())
                .then(data => {{
                    if (data.exists) {{
                        feedback.innerHTML = '&#x26A0; Bereits vergeben &rarr; <a href="#" onclick="useAbbrevSuggestion(\'' + data.suggestion + '\');return false;">' + data.suggestion + '</a>';
                        feedback.style.color = '#c00';
                        abbr.style.borderColor = '#c00';
                    }} else {{
                        feedback.innerHTML = '&#x2713;';
                        feedback.style.color = 'green';
                        abbr.style.borderColor = '';
                    }}
                }})
                .catch(() => {{ feedback.innerHTML = ''; }});
        }}

        function useAbbrevSuggestion(s) {{
            const abbr = document.getElementById('field_abbreviation');
            if (abbr) {{
                abbr.value = s;
                abbr.dataset.userModified = 'true';
                checkAbbreviationUnique();
            }}
        }}

        function autoFillAbbreviation() {{
            const abbr = document.getElementById('field_abbreviation');
            if (!abbr || abbr.dataset.userModified === 'true') return;
            const generated = _buildAbbreviation();
            if (generated) {{
                abbr.value = generated;
                checkAbbreviationUnique();
            }}
        }}

        (function initAbbrevField() {{
            const abbr = document.getElementById('field_abbreviation');
            if (!abbr) return;
            if (abbr.value.trim()) {{
                abbr.dataset.userModified = 'true';
                checkAbbreviationUnique();
            }}
            abbr.addEventListener('input', function() {{
                this.value = this.value.toUpperCase();
                this.dataset.userModified = 'true';
                checkAbbreviationUnique();
            }});
        }})();

        ['field_company_name','field_first_name','field_last_name'].forEach(id => {{
            const el = document.getElementById(id);
            if (el) el.addEventListener('blur', autoFillAbbreviation);
        }});
    </script>
    '''
    return s


# ── Page: Contacts (kombiniert grid2Cols: Tabelle + Formular) ─────────────────

def PageContacts(db: Database, contact_type_filter=None, entity_type_filter=None,
                 edit_contact_id=None, new_entity_type=None, error_msg=None):
    """Kombinierte Kontakt-Seite (grid2Cols): links Übersichtstabelle, rechts Formular.

    Beim Bearbeiten wird der gewählte Kontakt ins rechte Formular geladen (wie
    auf der Artikel-Seite); ohne edit_contact_id ist es ein leeres Neu-Formular.
    """
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

    if error_msg:
        s += f'<div class="msg-error" style="margin:8px 16px;">{error_msg}</div>'

    # ── Formular bestimmen (rechte Spalte): Bearbeiten oder Neu ──────────────
    edit_contact = db.get_contact_by_id(edit_contact_id) if edit_contact_id else None
    if edit_contact:
        entity_type = 'company'
        try:
            entity_type = edit_contact[15] or 'company'
        except (IndexError, TypeError):
            pass
        display_name_shown = edit_contact[3] or '–'
        form_title = f'Kontakt bearbeiten: <em>{display_name_shown}</em>'
        form_html = _contact_form(
            db, form_action='/masterdata/contacts/update', entity_type=entity_type,
            c=edit_contact,
            extra_hidden=f'<input type="hidden" name="contact_id" value="{edit_contact_id}">',
        )
    else:
        entity_type  = new_entity_type or 'company'
        entity_label = "<span style=\"font-size: 0.8em;\">🏢</span> Unternehmen</span>" if entity_type == 'company' else "<span style=\"font-size: 0.8em;\">👤 Person</span>"
        form_title   = f'Neuer Kontakt: {entity_label}'
        form_html    = _contact_form(db, form_action='/masterdata/contacts/add',
                                     entity_type=entity_type)

    # ── grid2Cols: rechts Formular, links Übersichtstabelle ──────────────────
    s += '<div class="grid2Cols gridMain">'
    # Rechte Spalte: oben Box mit Überschrift + Buttons (Aufbau wie Invoice-Seite),
    # darunter das Eingabeformular. Die Buttons liegen außerhalb der <form> und
    # senden sie über das form-Attribut (form="contact_form").
    s += '<div class="gridRightCol gridMiddle" style="order:2">'
    s += '<div class="rectRounded">'
    s += f'<h2>{form_title}</h2>'
    s += '<div class="rowWithObjects">'
    s += '<button type="submit" form="contact_form" class="coloredButton btn-sm bg-green">💾 Speichern</button>'
    s += '<button type="button" onclick="window.location.href=\'/masterdata/contacts\'" class="coloredButton btn-sm bg-gray">← Abbrechen</button>'
    s += '</div>'
    s += '</div>'
    s += f'<div class="rectRounded">{form_html}</div>'
    s += '</div><!-- Ende gridRightCol -->'

    s += '<div class="gridLeftCol" style="order:1">'
    type_labels = dict(CONTACT_TYPES)

    s += "<table>"
    s += ("<tr><th>ID</th><th>Entität</th><th>Typ</th><th>Anzeigename</th>"
          "<th>Kd-Nr.</th><th>Kürzel</th><th>Position / Firma</th><th>E-Mail</th><th>Telefon</th><th>Aktionen</th></tr>")

    for c in contacts:
        cid              = c[0]
        c_type           = c[1] or 'customer'
        cust_nr          = c[2] or ''
        display_name     = c[3] or '–'
        company_name     = c[4] or ''
        email            = c[9] or ''
        phone            = c[10] or ''
        entity_type_row  = c[15] if len(c) > 15 else 'company'
        abbreviation_val = c[26] if len(c) > 26 else ''
        type_keys_raw    = c[27] if len(c) > 27 else ''
        job_title_val    = c[28] if len(c) > 28 else ''
        entity_icon      = "🏢" if entity_type_row == 'company' else "👤"

        # Typ-Tags (aus ContactTypeLinks; für 'own' Fallback)
        if c_type == 'own':
            #type_tags = '<span style="background:#e8f0fe;color:#1a73e8;padding:1px 6px;border-radius:3px;font-size:.85em;">Eigene Daten</span>'
            type_tags = '<small>Eigene Daten</small>'
        elif type_keys_raw:
            type_tags = ' '.join(
                #f'<span style="background:#f1f3f4;padding:1px 6px;border-radius:3px;font-size:.85em;">{type_labels.get(tk.strip(), tk.strip())}</span>'
                f'<small>{type_labels.get(tk.strip(), tk.strip())}</small>'
                for tk in type_keys_raw.split(',') if tk.strip()
            )
        else:
            #type_tags = f'<span style="color:#999;font-size:.85em;">{type_labels.get(c_type, c_type)}</span>'
            type_tags = f'<small>{type_labels.get(c_type, c_type)}</small>'

        # Positions/Firma-Spalte
        if entity_type_row == 'person':
            pos_parts = [p for p in [job_title_val, company_name] if p]
            position_cell = '<small>' + (' · '.join(pos_parts) if pos_parts else '–') + '</small>'
        else:
            position_cell = f'<small>{company_name}</small>'

        s += f"<tr>"
        s += f"<td>{cid}</td>"
        #s += f"<td style='text-align:center;font-size:1.2em;'>{entity_icon}</td>"
        s += f"<td>{entity_icon}</td>"
        s += f"<td>{type_tags}</td>"
        s += f"<td><strong>{display_name}</strong></td>"
        s += f"<td>{cust_nr}</td>"
        s += f"<td><code>{abbreviation_val or ''}</code></td>"
        s += f"<td>{position_cell}</td>"
        s += f"<td>{email}</td>"
        s += f"<td>{phone}</td>"
        s += f"<td>"
        s += f"<a href='javascript:void(0)' onclick='openEditForm(\"/masterdata/contacts/edit?id={cid}\")' class='action-icon' title='Bearbeiten'>&#9998;</a>"
        s += f" <a href='javascript:void(0);' class='action-icon delete-icon' title='Löschen' "
        s += f"onclick='appConfirmHref(\"/masterdata/contacts/delete?id={cid}\", \"Kontakt wirklich löschen?\")'>&#128465;</a>"
        s += f"</td></tr>"

    if not contacts:
        s += "<tr><td colspan='10' style='text-align:center;padding:20px;'><em>Keine Kontakte vorhanden.</em></td></tr>"

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


# ── New / Edit: Delegation an die kombinierte grid2Cols-Seite ─────────────────

def PageContactNew(db: Database, entity_type: str = 'company', error_msg=None):
    """Neuer Kontakt: kombinierte Seite mit leerem Formular (Firma/Person) rechts."""
    return PageContacts(db, new_entity_type=entity_type, error_msg=error_msg)


def PageContactEdit(db: Database, contact_id, error_msg=None):
    """Kontakt bearbeiten: kombinierte Seite mit geladenem Kontakt im rechten Formular."""
    if not db.get_contact_by_id(contact_id):
        return "Kontakt nicht gefunden."
    return PageContacts(db, edit_contact_id=contact_id, error_msg=error_msg)
