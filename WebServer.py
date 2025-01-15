from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import parse_qs
from db import Database

class SimpleWebServer(BaseHTTPRequestHandler):
    def Header():
        s = "<!DOCTYPE html>"
        s+= "<html><head><title>Contabilidad simple</title></head><body>"
        s+= '<a href="/">Home</a> | <a href="/belege">Belege</a> | <a href="/skr">Skr</a> | <a href="/about">About</a>'
        return s
    
    def Footer():
        s = "</body></html>"
        return s

    def PageRoot():
        s = SimpleWebServer.Header()
        s+= "<h1>Welcome to the homepage!</h1>"
        s+= "<p>This is the homepage of our webserver.</p>"
        s+= SimpleWebServer.Footer()
        return s

    def PageAbout():
        s = SimpleWebServer.Header()
        s+= "<h1>About</h1>"
        s+= "<p>This is the about page of our webserver.</p>"
        s+= SimpleWebServer.Footer()
        return s

    # Belege
    def PageBelege(db):
        rows = db.fetch_belege()
        s = SimpleWebServer.Header()
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
                Nummer: <input type="text" name="nummer"><br>
                Datum: <input type="date" name="datum"><br>
                Dateiname: <input type="text" name="dateiname"><br>
                Pfad: <input type="text" name="pfad"><br>
                Info: <input type="text" name="info"><br>
                <input type="submit" value="Add Beleg">
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

        s = SimpleWebServer.Header
        s+= "<h1>Edit Beleg</h1>"
        s+= f'''
            <form method="POST" action="/update_beleg">
                Nummer: <input type="text" name="nummer" value="{beleg[0]}" readonly><br>
                Datum: <input type="date" name="datum" value="{beleg[1]}"><br>
                Dateiname: <input type="text" name="dateiname" value="{beleg[2]}"><br>
                Pfad: <input type="text" name="pfad" value="{beleg[3]}"><br>
                Info: <input type="text" name="info" value="{beleg[4]}"><br>
                <input type="submit" value="Update Beleg">
            </form>
        '''
        s+= SimpleWebServer.Footer()
        return s

    # Standardkontorahmen
    def PageSkr(db):
        rows = db.fetch_skr()
        s = SimpleWebServer.Header()
        s+= "<h1>Skr</h1>"
        s+= "<table border='1'>"
        s+= "<tr><th>ID</th><th>SKR Nr</th><th>Konto</th><th>Name</th><th>Gruppe</th><th>Actions</th></tr>"
        for row in rows:
            s+= f"<tr><td>{row[0]}</td><td>{row[1]}</td><td>{row[2]}</td><td>{row[3]}</td><td>{row[4]}</td>"
            s+= f"<td><a href='/edit_skr?id={row[0]}'>Edit</a></td></tr>"
        s+= "</table>"
        s+= '''
            <h2>Add New Skr</h2>
            <form method="POST" action="/add_skr">
                Kontorahmen: <input type="text" name="rid"><br>
                Konto: <input type="text" name="konto"><br>
                Name: <input type="text" name="name"><br>
                Gruppe: <input type="text" name="gruppe"><br>
                <input type="submit" value="Add Skr">
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

        s = SimpleWebServer.Header()
        s+= "<h1>Edit Skr</h1>"
        s+= f'''
            <form method="POST" action="/update_skr">
                ID: <input type="text" name="id" value="{skr[0]}" readonly><br>
                Kontorahmen: <input type="text" name="rid" value="{skr[1]}"><br>
                Konto: <input type="text" name="konto" value="{skr[2]}"><br>
                Name: <input type="text" name="name" value="{skr[3]}"><br>
                Gruppe: <input type="text" name="gruppe" value="{skr[4]}"><br>
                <input type="submit" value="Update Skr">
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
        elif self.path == "/belege":
            self.respond(200, SimpleWebServer.PageBelege(db))
        elif self.path == "/skr":
            self.respond(200, SimpleWebServer.PageSkr(db))
        elif self.path.startswith("/edit_beleg"):
            query_components = parse_qs(self.path.split('?')[1])
            nummer = query_components["nummer"][0]
            self.respond(200, SimpleWebServer.PageBelegEdit(db, nummer))
        elif self.path.startswith("/edit_skr"):
            query_components = parse_qs(self.path.split('?')[1])
            id = query_components["id"][0]
            self.respond(200, SimpleWebServer.PageSkrEdit(db, id))
        else:
            self.respond(404, "Page "+ self.path + " not found.")

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
        elif self.path == "/add_skr":
            rid = post_data["rid"][0]
            konto = post_data["konto"][0]
            name = post_data["name"][0]
            gruppe = post_data["gruppe"][0]
            db.insert_skr(rid, konto, name, gruppe)
            self.respond(303, "", headers={"Location": "/skr"})
        elif self.path == "/update_skr":
            id = post_data["id"][0]
            rid = post_data["rid"][0]
            konto = post_data["konto"][0]
            name = post_data["name"][0]
            gruppe = post_data["gruppe"][0]
            db.update_skr(id, rid, konto, name, gruppe)
            self.respond(303, "", headers={"Location": "/skr"})
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
