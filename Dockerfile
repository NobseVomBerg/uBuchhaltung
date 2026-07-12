# uBuchhaltung als Container.
#   Fertiges Image:  ghcr.io/nobsevomberg/ubuchhaltung   (Build via GitHub Actions)
#   Selbst bauen:    docker compose up -d --build        (siehe compose.yaml)
FROM python:3.13-slim

# 7-Zip für Datensicherung (.7zip) und Log-Kompression; ohne 7-Zip fiele die
# App auf .zip/.gz zurück. Das Debian-Paket "7zip" liefert das Binary 7zz.
RUN apt-get update \
    && apt-get install -y --no-install-recommends 7zip \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Abhängigkeiten zuerst kopieren: eigener Layer, bleibt bei Code-Änderungen im Cache
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Anwendungscode (Ausschlüsse in .dockerignore – u.a. data/ und seed_data/private/)
COPY . .

# Standardport 2824 = "BUCH" auf der Telefontastatur; im Container auf allen
# Interfaces lauschen
ENV UBUCHHALTUNG_HOST=0.0.0.0 \
    UBUCHHALTUNG_PORT=2824 \
    PYTHONUNBUFFERED=1

# Unprivilegierter App-Benutzer. Der Entrypoint startet als root, übereignet
# das Daten-Volume und wechselt dann zu diesem Benutzer – so funktionieren
# auch root-eigene Bind-Mounts (Unraid/Proxmox) ohne Handarbeit.
# sed entfernt evtl. Windows-Zeilenenden aus dem Entrypoint-Skript.
RUN useradd --create-home --uid 1000 ubuch \
    && mkdir -p /app/data \
    && chown -R ubuch:ubuch /app \
    && sed -i 's/\r$//' docker-entrypoint.sh \
    && chmod +x docker-entrypoint.sh

VOLUME /app/data
EXPOSE 2824

# Healthy, sobald die Startseite antwortet (HTTPS wird berücksichtigt,
# falls UBUCHHALTUNG_CERT gesetzt ist)
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import os,ssl,urllib.request as u; https=bool(os.environ.get('UBUCHHALTUNG_CERT')); ctx=ssl._create_unverified_context() if https else None; u.urlopen(('https' if https else 'http')+'://127.0.0.1:'+os.environ.get('UBUCHHALTUNG_PORT','2824')+'/', timeout=4, context=ctx)"

ENTRYPOINT ["/app/docker-entrypoint.sh"]
