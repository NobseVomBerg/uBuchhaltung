# run_https.ps1 – PyBuch über HTTPS starten (self-signed Zertifikat)
#
# Erzeugt beim ersten Aufruf ein selbst-signiertes Zertifikat (cert.pem/key.pem)
# im Projektordner und startet den Server dann per HTTPS. OpenSSL muss installiert
# und im PATH sein. Der Browser zeigt beim ersten Besuch eine Zertifikatswarnung,
# die einmalig bestätigt wird.
#
# Aufruf (im Projektordner):  .\run_https.ps1
# Im LAN erreichbar machen:   .\run_https.ps1 -BindAll
# Anderer Hostname im Cert:   .\run_https.ps1 -CommonName 192.168.1.10

param(
    [switch]$BindAll,                # an 0.0.0.0 binden (Netzzugriff)
    [string]$CommonName = "localhost",
    [int]$Port = 8080
)

$ErrorActionPreference = "Stop"
$cert = Join-Path $PSScriptRoot "cert.pem"
$key  = Join-Path $PSScriptRoot "key.pem"

if (-not (Test-Path $cert) -or -not (Test-Path $key)) {
    Write-Host "Erzeuge selbst-signiertes Zertifikat (cert.pem / key.pem) für CN=$CommonName ..."
    if (-not (Get-Command openssl -ErrorAction SilentlyContinue)) {
        Write-Error "OpenSSL wurde nicht gefunden. Bitte OpenSSL installieren und im PATH bereitstellen."
        exit 1
    }
    & openssl req -x509 -newkey rsa:2048 -nodes -keyout $key -out $cert -days 825 -subj "/CN=$CommonName"
    if ($LASTEXITCODE -ne 0) {
        Write-Error "Zertifikatserzeugung fehlgeschlagen (OpenSSL Exit $LASTEXITCODE)."
        exit 1
    }
}

$env:PYBUCH_CERT = $cert
$env:PYBUCH_KEY  = $key
$env:PYBUCH_PORT = "$Port"
if ($BindAll) { $env:PYBUCH_HOST = "0.0.0.0" }

Write-Host "Starte PyBuch über HTTPS auf Port $Port ..."
python (Join-Path $PSScriptRoot "main.py")
