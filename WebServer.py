from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import parse_qs
from db import Database

class SimpleWebServer(BaseHTTPRequestHandler):
    def Header1():
        s = "<!DOCTYPE html>\n"
        s+= "<html>\n <head>\n  <title>Contabilidad simple</title>\n"
        s+= "  <link rel='stylesheet' href='/buch.css'>\n"
        s+= "  <link rel='icon' sizes='32x32' href='favicon.ico'>\n"
        s+= " </head>\n <body>"
        s+= '<a href="/">Dashboard</a> | <a href="/belege">Belege</a> | <a href="/accounts">Accounts</a> | <a href="/skr">SKR</a> | <a href="/settings">Settings</a> | <a href="/about">About</a>'
        return s
    
    def Header2(content=""):
        s = "<div class='header2'>"
        if content:
            s += content
        else:
            s += "&nbsp;"
        s += "</div>"
        return s
    
    def Footer():
        s = "</body></html>"
        return s

    def PageRoot():
        s = SimpleWebServer.Header1()
        s+= SimpleWebServer.Header2()
        s+= "<h1>Dashboard</h1>"
        s+= "<p>Hier fehlen noch ein paar Dinge.</p>"
        s+= '''
        <form method="POST" action="/init_content">
            <input type="submit" value="Initialize DB Content">
        </form>
        '''
        s+= SimpleWebServer.Footer()
        return s

    def PageAbout():
        s = SimpleWebServer.Header1()
        s+= SimpleWebServer.Header2()
        s+= "<h1>About</h1>"
        s+= "<p>This is the about page of our webserver.</p>"
        s+= SimpleWebServer.Footer()
        return s

    def PageSettings():
        s = SimpleWebServer.Header1()
        submenu = '<a href="/settings/bankaccounts">Bank Accounts</a>'
        s+= SimpleWebServer.Header2(submenu)
        s+= "<h1>Settings</h1>"
        s+= "<p>Hier können Sie verschiedene Einstellungen vornehmen.</p>"
        s+= "<h2>Datenbankeinstellungen</h2>"
        s+= "<p>Datenbank: buch.db</p>"
        s+= "<h2>Systemeinstellungen</h2>"
        s+= "<p>Weitere Einstellungen folgen hier...</p>"
        s+= SimpleWebServer.Footer()
        return s

    # Belege
    def PageBelege(db):
        rows = db.fetch_belege()
        s = SimpleWebServer.Header1()
        s+= SimpleWebServer.Header2()
        s+= "<h1>Belege</h1>"
        s+= "<table border='1'>"
        s+= "<tr><th>Nummer</th><th>Datum</th><th>Dateiname</th><th>Pfad</th><th>Info</th><th>Actions</th></tr>"
        for row in rows:
            s+= f"<tr><td>{row[0]}</td><td>{row[1]}</td><td>{row[2]}</td><td>{row[3]}</td><td>{row[4]}</td>"
            s+= f"<td><a href='/edit_beleg?nummer={row[0]}'>Edit</a></td></tr>"
        s+= "</table>"
        s+= '''
            <h2>Add New Beleg</h2>
            <form method="POST" action="/add_beleg">
                <table>
                    <tr><td>Nummer:</td><td><input type="text" name="nummer"></td></tr>
                    <tr><td>Datum:</td><td><input type="date" name="datum"></td></tr>
                    <tr><td>Dateiname:</td><td><input type="text" name="dateiname"></td></tr>
                    <tr><td>Pfad:</td><td><input type="text" name="pfad"></td></tr>
                    <tr><td>Info:</td><td><input type="text" name="info"></td></tr>
                    <tr><td></td><td><input type="submit" value="Add Beleg"></td></tr>
                </table>
            </form>
        '''
        s+= SimpleWebServer.Footer()
        return s

    def PageBelegEdit(db, nummer):
        rows = db.fetch_belege()
        beleg = None
        for row in rows:
            if row[0] == nummer:
                beleg = row
                break
        if not beleg:
            return "Beleg not found."

        s = SimpleWebServer.Header1()
        s+= SimpleWebServer.Header2()
        s+= "<h1>Edit Beleg</h1>"
        s+= f'''
            <form method="POST" action="/update_beleg">
                <table>
                    <tr><td>Nummer:</td><td><input type="text" name="nummer" value="{beleg[0]}" readonly></td></tr>
                    <tr><td>Datum:</td><td><input type="date" name="datum" value="{beleg[1]}"></td></tr>
                    <tr><td>Dateiname:</td><td><input type="text" name="dateiname" value="{beleg[2]}"></td></tr>
                    <tr><td>Pfad:</td><td><input type="text" name="pfad" value="{beleg[3]}"></td></tr>
                    <tr><td>Info:</td><td><input type="text" name="info" value="{beleg[4]}"></td></tr>
                    <tr><td></td><td><input type="submit" value="Update Beleg"></td></tr>
                </table>
            </form>
        '''
        s+= SimpleWebServer.Footer()
        return s

    # Accounts (Kontobewegungen)
    def PageAccounts(db):
        # Generiere Header2 mit Konten-Checkboxen
        konten = db.fetch_konten()
        header2_content = ""
        for konto in konten:
            konto_id = konto[0]
            konto_name = konto[1]
            header2_content += f'<input type="checkbox" id="konto_{konto_id}" name="konto_{konto_id}" checked> '
            header2_content += f'<label for="konto_{konto_id}">{konto_name}</label> &nbsp; '
        
        s = SimpleWebServer.Header1()
        s+= SimpleWebServer.Header2(header2_content)
        s+= "<h1>Accounts</h1>"
        s+= "<table border='1'>"
        s+= "<tr><th>Date</th><th>Recipient/Principal</th><th>Reference</th><th>Amount</th><th>BankAccount</th><th>SkrKonto</th><th>RefNr</th><th>Actions</th></tr>"
        
        # Hier werden später die Kontobewegungen aus der Datenbank geladen
        # Momentan als Platzhalter leer
        
        s+= "</table>"
        s+= '''
            <h2>Add New Transaction</h2>
            <form method="POST" action="/accounts/add">
                <table>
                    <tr><td>Date:</td><td><input type="date" name="date" required></td></tr>
                    <tr><td>Recipient/Principal:</td><td><input type="text" name="recipient"></td></tr>
                    <tr><td>Reference:</td><td><input type="text" name="reference"></td></tr>
                    <tr><td>Amount:</td><td><input type="number" step="0.01" name="amount"></td></tr>
                    <tr><td>BankAccount:</td><td><select name="bankaccount">
        '''
        for konto in konten:
            s+= f'<option value="{konto[0]}">{konto[1]}</option>'
        s+= '''
                    </select></td></tr>
                    <tr><td>SkrKonto:</td><td><input type="text" name="skrkonto"></td></tr>
                    <tr><td>RefNr:</td><td><input type="text" name="refnr"></td></tr>
                    <tr><td></td><td><input type="submit" value="Add Transaction"></td></tr>
                </table>
            </form>
        '''
        s+= SimpleWebServer.Footer()
        return s

    # Settings - Bank Accounts
    def PageSettingsBankAccount(db):
        rows = db.fetch_konten()
        s = SimpleWebServer.Header1()
        submenu = '<a href="/settings">Settings</a> | <a href="/settings/bankaccounts">Bank Accounts</a>'
        s+= SimpleWebServer.Header2(submenu)
        s+= "<h1>Bank Accounts</h1>"
        s+= "<table border='1'>"
        s+= "<tr><th>ID</th><th>Bezeichnung</th><th>Inhaber</th><th>IBAN</th><th>BIC</th><th>BankName</th><th>Typ</th><th>Actions</th></tr>"
        for row in rows:
            typ = "Kasse" if row[6] == 1 else "Bank"
            s+= f"<tr><td>{row[0]}</td><td>{row[1]}</td><td>{row[2]}</td><td>{row[3]}</td><td>{row[4]}</td><td>{row[5]}</td><td>{typ}</td>"
            if row[6] == 1:  # IstKasse
                s+= f"<td><span style='color:gray;'>Kann nicht gelöscht werden</span></td></tr>"
            else:
                s+= f"<td><a href='/settings/bankaccounts/edit?id={row[0]}'>Edit</a> | <a href='/settings/bankaccounts/delete?id={row[0]}'>Delete</a></td></tr>"
        s+= "</table>"
        s+= '''
            <h2>Add New Bank Account</h2>
            <form method="POST" action="/settings/bankaccounts/add">
                <table>
                    <tr><td>Bezeichnung:</td><td><input type="text" name="bezeichnung" required></td></tr>
                    <tr><td>Inhaber:</td><td><input type="text" name="inhaber"></td></tr>
                    <tr><td>IBAN:</td><td><input type="text" name="iban"></td></tr>
                    <tr><td>BIC:</td><td><input type="text" name="bic"></td></tr>
                    <tr><td>BankName:</td><td><input type="text" name="bankname"></td></tr>
                    <tr><td></td><td><input type="submit" value="Add Bank Account"></td></tr>
                </table>
            </form>
        '''
        s+= SimpleWebServer.Footer()
        return s

    def PageSettingsBankAccountEdit(db, konto_id):
        konto = db.get_konto_by_id(konto_id)
        if not konto:
            return "Konto not found."

        s = SimpleWebServer.Header1()
        submenu = '<a href="/settings">Settings</a> | <a href="/settings/bankaccounts">Bank Accounts</a>'
        s+= SimpleWebServer.Header2(submenu)
        s+= "<h1>Edit Bank Account</h1>"
        
        if konto[6] == 1:  # IstKasse
            s+= "<p style='color:red;'>Hinweis: Das Kasse-Konto kann nicht bearbeitet werden.</p>"
            s+= "<a href='/settings/bankaccounts'>Zurück zur Kontenübersicht</a>"
        else:
            s+= f'''
                <form method="POST" action="/settings/bankaccounts/update">
                    <table>
                        <tr><td>ID:</td><td><input type="text" name="id" value="{konto[0]}" readonly></td></tr>
                        <tr><td>Bezeichnung:</td><td><input type="text" name="bezeichnung" value="{konto[1]}" required></td></tr>
                        <tr><td>Inhaber:</td><td><input type="text" name="inhaber" value="{konto[2]}"></td></tr>
                        <tr><td>IBAN:</td><td><input type="text" name="iban" value="{konto[3]}"></td></tr>
                        <tr><td>BIC:</td><td><input type="text" name="bic" value="{konto[4]}"></td></tr>
                        <tr><td>BankName:</td><td><input type="text" name="bankname" value="{konto[5]}"></td></tr>
                        <tr><td></td><td><input type="submit" value="Update Bank Account"></td></tr>
                    </table>
                </form>
            '''
        s+= SimpleWebServer.Footer()
        return s

    # Standardkontorahmen
    def PageSkr(db):
        rows = db.fetch_skr()
        s = SimpleWebServer.Header1()
        s+= SimpleWebServer.Header2()
        s+= "<h1>SKR</h1>"
        s+= "<table border='1'>"
        s+= "<tr><th>ID</th><th>SKR Nr</th><th>Konto</th><th>Name</th><th>Gruppe</th><th>Actions</th></tr>"
        for row in rows:
            s+= f"<tr><td>{row[0]}</td><td>{row[1]}</td><td>{row[2]}</td><td>{row[3]}</td><td>{row[4]}</td>"
            s+= f"<td><a href='/edit_skr?id={row[0]}'>Edit</a></td></tr>"
        s+= "</table>"
        s+= '''
            <h2>Add new SKR Konto</h2>
            <form method="POST" action="/add_skr">
                <table>
                    <tr><td>RahmenNr:</td><td><input type="text" name="RahmenNr"></td></tr>
                    <tr><td>Konto:</td><td><input type="text" name="konto"></td></tr>
                    <tr><td>Name:</td><td><input type="text" name="name"></td></tr>
                    <tr><td>Gruppe:</td><td><input type="text" name="gruppe"></td></tr>
                    <tr><td></td><td><input type="submit" value="Add SKR Konto"></td></tr>
                </table>
            </form>
        '''
        s+= SimpleWebServer.Footer()
        return s
    
    def PageSkrEdit(db, id):
        rows = db.fetch_skr()
        skr = None
        for row in rows:
            if row[0] == id:
                skr = row
                break
        if not skr:
            return "Skr not found."

        s = SimpleWebServer.Header1()
        s+= SimpleWebServer.Header2()
        s+= "<h1>Edit Skr</h1>"
        s+= f'''
            <form method="POST" action="/update_skr">
                <table>
                    <tr><td>ID:</td><td><input type="text" name="id" value="{skr[0]}" readonly></td></tr>
                    <tr><td>RahmenNr:</td><td><input type="text" name="RahmenNr" value="{skr[1]}"></td></tr>
                    <tr><td>Konto:</td><td><input type="text" name="konto" value="{skr[2]}"></td></tr>
                    <tr><td>Name:</td><td><input type="text" name="name" value="{skr[3]}"></td></tr>
                    <tr><td>Gruppe:</td><td><input type="text" name="gruppe" value="{skr[4]}"></td></tr>
                    <tr><td></td><td><input type="submit" value="Update Skr"></td></tr>
                </table>
            </form>
        '''
        s+= SimpleWebServer.Footer()
        return s

    def do_GET(self):
        db = Database()
        # URL-Routing
        if self.path == "/":
            self.respond(200, SimpleWebServer.PageRoot())
        elif self.path == "/about":
            self.respond(200, SimpleWebServer.PageAbout())
        elif self.path == "/settings":
            self.respond(200, SimpleWebServer.PageSettings())
        elif self.path == "/settings/bankaccounts":
            self.respond(200, SimpleWebServer.PageSettingsBankAccount(db))
        elif self.path == "/belege":
            self.respond(200, SimpleWebServer.PageBelege(db))
        elif self.path == "/accounts":
            self.respond(200, SimpleWebServer.PageAccounts(db))
        elif self.path == "/skr":
            self.respond(200, SimpleWebServer.PageSkr(db))
        elif self.path.startswith("/edit_beleg"):
            query_components = parse_qs(self.path.split('?')[1])
            nummer = query_components["nummer"][0]
            self.respond(200, SimpleWebServer.PageBelegEdit(db, nummer))
        elif self.path.startswith("/settings/bankaccounts/edit"):
            query_components = parse_qs(self.path.split('?')[1])
            konto_id = int(query_components["id"][0])
            self.respond(200, SimpleWebServer.PageSettingsBankAccountEdit(db, konto_id))
        elif self.path.startswith("/settings/bankaccounts/delete"):
            query_components = parse_qs(self.path.split('?')[1])
            konto_id = int(query_components["id"][0])
            db.delete_konto(konto_id)
            self.respond(303, "", headers={"Location": "/settings/bankaccounts"})
        elif self.path.startswith("/edit_skr"):
            query_components = parse_qs(self.path.split('?')[1])
            id = query_components["id"][0]
            self.respond(200, SimpleWebServer.PageSkrEdit(db, id))
        elif self.path == "/buch.css":
            self.serve_static_file("buch.css", "text/css")
        elif self.path == "/favicon.ico":
            self.serve_static_file("favicon.ico", "image/x-icon")
        else:
            self.respond(404, "Page not found.")

    def serve_static_file(self, filename, content_type):
        try:
            with open(filename, 'rb') as file:
                self.send_response(200)
                self.send_header("Content-type", content_type)
                self.end_headers()
                self.wfile.write(file.read())
        except FileNotFoundError:
            self.respond(404, "File not found.")

    def do_POST(self):
        db = Database()
        content_length = int(self.headers['Content-Length'])
        post_data = self.rfile.read(content_length).decode('utf-8')
        post_data = parse_qs(post_data)

        if self.path == "/add_beleg":
            nummer = post_data["nummer"][0]
            datum = post_data["datum"][0]
            dateiname = post_data["dateiname"][0]
            pfad = post_data["pfad"][0]
            info = post_data["info"][0]
            db.insert_beleg(nummer, datum, dateiname, pfad, info)
            self.respond(303, "", headers={"Location": "/belege"})
        elif self.path == "/update_beleg":
            nummer = post_data["nummer"][0]
            datum = post_data["datum"][0]
            dateiname = post_data["dateiname"][0]
            pfad = post_data["pfad"][0]
            info = post_data["info"][0]
            db.update_beleg(nummer, datum, dateiname, pfad, info)
            self.respond(303, "", headers={"Location": "/belege"})
        elif self.path == "/accounts/add":
            # Platzhalter für das Hinzufügen von Kontobewegungen
            # Später wird hier die eigentliche Logik implementiert
            self.respond(303, "", headers={"Location": "/accounts"})
        elif self.path == "/settings/bankaccounts/add":
            bezeichnung = post_data["bezeichnung"][0]
            inhaber = post_data.get("inhaber", [""])[0]
            iban = post_data.get("iban", [""])[0]
            bic = post_data.get("bic", [""])[0]
            bankname = post_data.get("bankname", [""])[0]
            db.insert_konto(bezeichnung, inhaber, iban, bic, bankname)
            self.respond(303, "", headers={"Location": "/settings/bankaccounts"})
        elif self.path == "/settings/bankaccounts/update":
            konto_id = int(post_data["id"][0])
            bezeichnung = post_data["bezeichnung"][0]
            inhaber = post_data.get("inhaber", [""])[0]
            iban = post_data.get("iban", [""])[0]
            bic = post_data.get("bic", [""])[0]
            bankname = post_data.get("bankname", [""])[0]
            db.update_konto(konto_id, bezeichnung, inhaber, iban, bic, bankname)
            self.respond(303, "", headers={"Location": "/settings/bankaccounts"})
        elif self.path == "/add_skr":
            rid = post_data["RahmenNr"][0]
            konto = post_data["konto"][0]
            name = post_data["name"][0]
            gruppe = post_data["gruppe"][0]
            db.insert_skr(rid, konto, name, gruppe)
            self.respond(303, "", headers={"Location": "/skr"})
        elif self.path == "/update_skr":
            id = post_data["id"][0]
            rid = post_data["RahmenNr"][0]
            konto = post_data["konto"][0]
            name = post_data["name"][0]
            gruppe = post_data["gruppe"][0]
            db.update_skr(id, rid, konto, name, gruppe)
            self.respond(303, "", headers={"Location": "/skr"})
        if self.path == "/init_content":
            db.init_content()
            self.respond(303, "", headers={"Location": "/"})
        else:
            self.respond(404, "Page not found.")

    def respond(self, status_code, content, headers=None):
        # Sende den HTTP-Status und Header
        self.send_response(status_code)
        if headers:
            for key, value in headers.items():
                self.send_header(key, value)
        self.send_header("Content-type", "text/html")
        self.end_headers()

        # Schreibe den Inhalt in die Antwort
        if content:
            self.wfile.write(content.encode("utf-8"))

# Webserver starten
def run_server(host="localhost", port=8080):
    db = Database()
    server_address = (host, port)
    httpd = HTTPServer(server_address, SimpleWebServer)
    print(f"Starting server on {host}:{port}...")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down server.")
        httpd.server_close()

if __name__ == "__main__":
    run_server()
