#!/usr/bin/env python3
"""Full-page render of the Ink Blue & Amber scheme."""
import html
import cairosvg

W = 1240
SERIF = "Liberation Serif, Georgia, serif"
SANS  = "Liberation Sans, Arial, sans-serif"

MAST   = "#15264F"   # ink blue masthead
ACCENT = "#E0851A"   # amber
BG     = "#EEF1F6"   # cool light gray-blue page
CARD   = "#FFFFFF"
INK    = "#16213E"   # deep blue-ink text
MUTE   = "#69707E"
LINE   = "#DBE0E8"


def esc(s): return html.escape(s, quote=True)


def T(x, y, s, size, fill, family=SANS, weight="400", anchor="start", spacing=None):
    sp = f' letter-spacing="{spacing}"' if spacing is not None else ""
    return (f'<text x="{x}" y="{y}" font-family="{family}" font-size="{size}" '
            f'fill="{fill}" font-weight="{weight}" text-anchor="{anchor}"{sp}>{esc(s)}</text>')


def R(x, y, w, h, fill, rx=0, stroke=None, sw=1):
    s = f' stroke="{stroke}" stroke-width="{sw}"' if stroke else ""
    return f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{rx}" fill="{fill}"{s}/>'


def L(x1, y1, x2, y2, stroke, sw=1):
    return f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="{stroke}" stroke-width="{sw}"/>'


GULF = [
    ("Gulf states press for revived nuclear framework as talks resume in Muscat", "AL JAZEERA · 41 MIN AGO"),
    ("Saudi Aramco second-quarter profit beats forecasts on higher output", "ARAB NEWS · 1 HR AGO"),
    ("UAE central bank holds rates, flags resilient non-oil growth", "THE NATIONAL · 2 HR AGO"),
]
LEVANT = [
    ("Lebanon ceasefire monitors report calm along southern border", "L'ORIENT TODAY · 32 MIN AGO"),
    ("Syria reconstruction conference opens in Damascus amid sanctions debate", "WAFA NEWS · 1 HR AGO"),
    ("Jordan and Iraq sign cross-border electricity grid agreement", "JORDAN TIMES · 3 HR AGO"),
]

p = []
# masthead
mh = 120
p.append(R(0, 0, W, mh, MAST))
p.append(R(0, 0, W, 5, ACCENT))
cx = W / 2
p.append(T(cx, 50, "MIDDLE EAST", 22, "#EAF0FA", SERIF, "400", "middle", "9"))
p.append(T(cx, 92, "Intelligence Hub", 46, "#FFFFFF", SERIF, "700", "middle"))
p.append(R(W - 110, 22, 84, 26, "#1E3463", rx=13, stroke="#ffffff22"))
p.append(f'<circle cx="{W-92}" cy="35" r="4" fill="{ACCENT}"/>')
p.append(T(W - 82, 39, "Live", 12, "#EAF0FA", SANS, "600"))
p.append(T(36, 39, "JUN 22, 2026", 12, "#9FB0CC", SANS, "600", "start", "1.5"))
# nav
ny = mh
p.append(R(0, ny, W, 52, CARD))
p.append(L(0, ny + 52, W, ny + 52, LINE))
nx = 40
for i, it in enumerate(("Front Pages", "Headlines", "World Briefing")):
    col = ACCENT if i == 0 else INK
    p.append(T(nx, ny + 33, it, 15, col, SANS, "700" if i == 0 else "500"))
    if i == 0:
        p.append(R(nx - 2, ny + 49, len(it) * 8 + 8, 3, ACCENT))
    nx += len(it) * 9 + 44
# hero briefing card
hy = ny + 52 + 28
hw = W - 80
hh = 176
p.append(R(40, hy, hw, hh, CARD, rx=4))
p.append(R(40, hy, hw, 4, ACCENT))
p.append(T(68, hy + 38, "WORLD BRIEFING", 13, ACCENT, SANS, "700", "start", "2"))
p.append(T(40 + hw - 28, hy + 38, "JUN 22 · 06:00 UTC", 11.5, MUTE, SANS, "600", "end"))
p.append(T(68, hy + 76, "Russia-Ukraine:  Overnight strikes on energy infrastructure drew fresh", 21, INK, SERIF, "500"))
p.append(T(68, hy + 104, "Western condemnation as European leaders weighed new air-defence support.", 21, INK, SERIF, "500"))
tx = 68
for i, lab in enumerate(("EN", "HE", "AR")):
    on = i == 0
    p.append(R(tx, hy + 128, 44, 26, ACCENT if on else CARD, rx=13, stroke=ACCENT if on else LINE))
    p.append(T(tx + 22, hy + 145, lab, 12, "#fff" if on else INK, SANS, "700", "middle"))
    tx += 52
# regions
gy = hy + hh + 44


def rows(x, y, w, items):
    cy = y
    out = []
    for title, meta in items:
        out.append(T(x, cy, title, 15, INK, SERIF, "500"))
        out.append(T(x, cy + 17, meta, 11.5, MUTE, SANS, "600", "start", "0.5"))
        cy += 44
        out.append(L(x, cy - 16, x + w, cy - 16, LINE))
    return "\n".join(out), cy


def head(x, y, label):
    return T(x, y, label, 26, INK, SERIF, "700") + R(x, y + 10, 64, 3, ACCENT)


p.append(head(40, gy, "GULF"))
hb, e1 = rows(40, gy + 44, 540, GULF)
p.append(hb)
p.append(head(W // 2 + 20, gy, "LEVANT"))
hb2, e2 = rows(W // 2 + 20, gy + 44, 540, LEVANT)
p.append(hb2)
H = int(max(e1, e2) + 36)

svg = f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}">{R(0,0,W,H,BG)}{"".join(p)}</svg>'
cairosvg.svg2png(bytestring=svg.encode("utf-8"),
                 write_to="/home/user/mena-newsstand/mockups/scheme_blue_amber_full.png",
                 output_width=W, output_height=H, background_color="white")
print(f"wrote scheme_blue_amber_full.png ({W}x{H})")
