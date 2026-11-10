// SPDX-License-Identifier: AGPL-3.0-or-later
// SPDX-FileCopyrightText: 2026 unsix IT Engineering
// Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
//
// Erzeugt die Screenshots für das README (docs/screenshots/*.png), jeweils hell
// und dunkel (*-dark.png, Dark Mode der App über prefers-color-scheme).
//
// Die App wird im Demo-Modus (UBUCHHALTUNG_DEMO=1) in einem temporären Ordner
// gestartet (dort entsteht ./data mit den Demodaten des Benutzers „demo“, als der
// jeder Besucher arbeitet), fotografiert und wieder beendet – die eigenen Daten
// werden nie berührt. Es erscheinen ausschließlich erfundene Beispieldaten.
//
// Lokal ausführen (im Projektordner):
//     npm i playwright            # einmalig (legt node_modules/ an)
//     npx playwright install chromium   # einmalig, falls noch kein Chromium da ist
//     node tools/screenshots.mjs
//
// Optional: UBUCHHALTUNG_SCREENSHOT_PORT (Default 8097),
//           PLAYWRIGHT_CHROMIUM_PATH (eigener Chromium/Chrome-Pfad).
// Voraussetzungen: Python mit den Abhängigkeiten aus requirements.txt.

import { spawn } from 'node:child_process';
import { mkdtempSync, mkdirSync, rmSync, symlinkSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { createRequire } from 'node:module';

// require statt import: findet playwright auch über NODE_PATH / globale Installation
const { chromium } = createRequire(import.meta.url)('playwright');

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const OUT = join(ROOT, 'docs', 'screenshots');
const PORT = process.env.UBUCHHALTUNG_SCREENSHOT_PORT || '8097';
const BASE = `http://127.0.0.1:${PORT}`;

// Seiten: Dateiname, Pfad. Ohne ?from=&to= zeigt die App das laufende Jahr
// (Demodaten sind relativ zum Startdatum datiert).
const SHOTS = [
  { file: 'dashboard',    path: '/dashboard' },
  { file: 'transactions', path: '/transactions' },
  { file: 'invoice-list', path: '/invoice' },
  { file: 'assets',       path: '/assets' },
  { file: 'trips',        path: '/trips' },
  { file: 'worktime',     path: '/worktime' },
];

async function waitForServer(url, tries = 60) {
  for (let i = 0; i < tries; i++) {
    try { if ((await fetch(url)).ok) return; } catch { /* noch nicht bereit */ }
    await new Promise(r => setTimeout(r, 500));
  }
  throw new Error('Server nicht erreichbar: ' + url);
}

const dataDir = mkdtempSync(join(tmpdir(), 'ubuch-screens-'));
// Die App liefert buch.css/favicon.ico relativ zum Arbeitsverzeichnis aus.
for (const f of ['buch.css', 'favicon.ico']) symlinkSync(join(ROOT, f), join(dataDir, f));
const server = spawn(process.env.PYTHON || 'python3', [join(ROOT, 'main.py')], {
  cwd: dataDir,                                  // ./data entsteht hier
  env: { ...process.env, UBUCHHALTUNG_DEMO: '1', UBUCHHALTUNG_PORT: PORT,
         UBUCHHALTUNG_HOST: '127.0.0.1', PYTHONPATH: ROOT },
  stdio: ['ignore', 'inherit', 'inherit'],
});

let browser;
try {
  await waitForServer(BASE + '/');
  mkdirSync(OUT, { recursive: true });
  browser = await chromium.launch(
    { args: ['--lang=de-DE'],   // Datumsfelder: TT.MM.JJJJ (braucht auch die Umgebung)
      env: { ...process.env, LANG: 'de_DE.UTF-8', LANGUAGE: 'de_DE:de', LC_ALL: 'de_DE.UTF-8' },
      ...(process.env.PLAYWRIGHT_CHROMIUM_PATH ? { executablePath: process.env.PLAYWRIGHT_CHROMIUM_PATH } : {}) });
  for (const [scheme, suffix] of [['light', ''], ['dark', '-dark']]) {
    const ctx = await browser.newContext({
      viewport: { width: 1440, height: 900 }, deviceScaleFactor: 2, colorScheme: scheme, locale: 'de-DE', timezoneId: 'Europe/Berlin',
      extraHTTPHeaders: { 'Accept-Language': 'de-DE,de' },
    });
    const page = await ctx.newPage();
    for (const s of SHOTS) {
      await page.goto(BASE + s.path, { waitUntil: 'networkidle' });
      const target = join(OUT, `${s.file}${suffix}.png`);
      await page.screenshot({ path: target });
      console.log('✓', target);
    }
    await ctx.close();
  }
  // Auf 256 Farben verkleinern (Pillow kommt mit pdfplumber aus requirements.txt)
  const shrink = spawn(process.env.PYTHON || 'python3', ['-c',
    'import glob,sys\nfrom PIL import Image\nfor f in glob.glob(sys.argv[1]+"/*.png"):\n' +
    '    Image.open(f).convert("RGB").quantize(colors=256, method=Image.Quantize.MEDIANCUT,' +
    ' dither=Image.Dither.NONE).save(f, optimize=True)', OUT], { stdio: 'inherit' });
  await new Promise((ok, fail) => shrink.on('exit', c => c === 0 ? ok() : fail(new Error('Verkleinern fehlgeschlagen'))));
} finally {
  if (browser) await browser.close();
  server.kill();
  rmSync(dataDir, { recursive: true, force: true });
}
