# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Erzeugt ``export/icc/sRGB-v2.icc`` – ein minimales ICC-v2-Matrixprofil für sRGB.

Warum selbst erzeugen: PDF/A verlangt ein OutputIntent-Profil; fertige sRGB-
Profile (HP/Microsoft ``sRGB IEC61966-2.1``) haben eigene Lizenzbedingungen.
Dieses Profil enthält nur die in IEC 61966-2-1 festgelegten, Fakten-Werte
(Primärvalenzen D50-adaptiert, Weißpunkt D50, sRGB-Kennlinie) – kein fremdes
Urheberrecht. Aufruf: ``python tools/make_srgb_icc.py`` (deterministisch).
"""
import os
import struct

# Primärvalenzen und Weißpunkt (D50-adaptiert, wie in der ICC-sRGB-Referenz)
R_XYZ = (0.43607, 0.22249, 0.01392)
G_XYZ = (0.38515, 0.71687, 0.09708)
B_XYZ = (0.14307, 0.06061, 0.71410)
D50 = (0.96420, 1.00000, 0.82491)


def s15(v):
    return struct.pack('>i', int(round(v * 65536)))


def xyz_tag(xyz):
    return b'XYZ \0\0\0\0' + b''.join(s15(v) for v in xyz)


def curve_tag(n=1024):
    def srgb(x):
        return x / 12.92 if x <= 0.04045 else ((x + 0.055) / 1.055) ** 2.4
    vals = [min(65535, max(0, int(round(srgb(i / (n - 1)) * 65535)))) for i in range(n)]
    return b'curv\0\0\0\0' + struct.pack('>I', n) + struct.pack(f'>{n}H', *vals)


def text_tag(text):
    return b'text\0\0\0\0' + text.encode('ascii') + b'\0'


def desc_tag(text):
    ascii_ = text.encode('ascii') + b'\0'
    return (b'desc\0\0\0\0' + struct.pack('>I', len(ascii_)) + ascii_
            + struct.pack('>II', 0, 0) + struct.pack('>HB', 0, 0) + b'\0' * 67)


def build():
    curve = curve_tag()
    tags = [
        (b'desc', desc_tag('sRGB (uBuchhaltung, IEC 61966-2-1 Werte)')),
        (b'cprt', text_tag('Public domain - enthaelt nur Normwerte (IEC 61966-2-1).')),
        (b'wtpt', xyz_tag(D50)),
        (b'rXYZ', xyz_tag(R_XYZ)), (b'gXYZ', xyz_tag(G_XYZ)), (b'bXYZ', xyz_tag(B_XYZ)),
        (b'rTRC', curve), (b'gTRC', curve), (b'bTRC', curve),
    ]
    table_size = 4 + 12 * len(tags)
    offset = 128 + table_size
    table, body, shared = b'', b'', {}
    for sig, data in tags:
        data_padded = data + b'\0' * (-len(data) % 4)
        if data_padded in shared:                       # gleiche Kennlinie nur einmal
            off = shared[data_padded]
        else:
            off = offset + len(body)
            shared[data_padded] = off
            body += data_padded
        table += sig + struct.pack('>II', off, len(data))
    size = 128 + table_size + len(body)
    header = struct.pack('>I4sI4s4s4s', size, b'\0' * 4, 0x02100000, b'mntr', b'RGB ', b'XYZ ')
    header += struct.pack('>6H', 2026, 1, 1, 0, 0, 0)    # fester Zeitstempel (deterministisch)
    header += b'acsp'
    header += b'\0' * 4 * 4                              # Plattform, Flags, Hersteller, Modell
    header += b'\0' * 8                                  # Attribute
    header += struct.pack('>I', 0)                       # Rendering Intent: wahrnehmungsgemäß
    header += s15(D50[0]) + s15(D50[1]) + s15(D50[2])    # Illuminant
    header += b'\0' * 4 + b'\0' * 16 + b'\0' * 28        # Creator, Profil-ID, reserviert
    assert len(header) == 128, len(header)
    return header + struct.pack('>I', len(tags)) + table + body


if __name__ == '__main__':
    out = os.path.join(os.path.dirname(__file__), '..', 'export', 'icc', 'sRGB-v2.icc')
    data = build()
    with open(out, 'wb') as f:
        f.write(data)
    print(f'{out}: {len(data)} Bytes')
