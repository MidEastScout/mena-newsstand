#!/usr/bin/env python3
"""Full color-scheme options — accent, masthead, and background all change.

Each scheme is rendered as a compact live page (masthead + nav + section +
cards + briefing chip) so the whole palette can be judged at once. Tiled into
one contact sheet.
"""
import html
import cairosvg

SERIF = "Liberation Serif, Georgia, serif"
SANS  = "Liberation Sans, Arial, sans-serif"


def esc(s):
    return html.escape(s, quote=True)


def T(x, y, s, size, fill, family=SANS, weight="400", anchor="start", spacing=None):
    sp = f' letter-spacing="{spacing}"' if spacing is not None else ""
    return (f'<text x="{x}" y="{y}" font-family="{family}" font-size="{size}" '
            f'fill="{fill}" font-weight="{weight}" text-anchor="{anchor}"{sp}>'
            f'{esc(s)}</text>')


def R(x, y, w, h, fill, rx=0, stroke=None, sw=1):
    s = f' stroke="{stroke}" stroke-width="{sw}"' if stroke else ""
    return f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{rx}" fill="{fill}"{s}/>'


# name, masthead, mast_text, accent, page bg, card, ink, muted, line, on-accent
SCHEMES = [
    ("Crimson & Cream (current)",
     "#16161A", "#FFFFFF", "#C0212A", "#F4F2EC", "#FFFFFF", "#141414",
     "#6B6B6B", "#E2DFD6", "#FFFFFF"),
    ("Midnight Navy & Gold",
     "#10203B", "#F3EFE2", "#C9A227", "#F3F1EA", "#FFFFFF", "#15203A",
     "#6E7480", "#E3E0D6", "#1A1A1A"),
    ("Forest & Brass",
     "#13302A", "#EFE9DB", "#B07A2E", "#F1EFE8", "#FFFFFF", "#16241F",
     "#6B7066", "#DEDACF", "#FFFFFF"),
    ("Charcoal & Teal",
     "#1E2424", "#EAF1F0", "#0E8C8C", "#EDF1F1", "#FFFFFF", "#15201F",
     "#677272", "#D8E0DF", "#FFFFFF"),
    ("Ink Blue & Amber",
     "#15264F", "#EAF0FA", "#E0851A", "#EEF1F6", "#FFFFFF", "#16213E",
     "#69707E", "#DBE0E8", "#1A1A1A"),
    ("Oxblood & Sand",
     "#3A1416", "#F1E6D6", "#8C2A2E", "#EFE7DA", "#FFFFFF", "#241011",
     "#7A6F60", "#E0D6C5", "#FFFFFF"),
    ("Slate & Coral",
     "#26303A", "#EDF1F4", "#E2563D", "#EEF0F2", "#FFFFFF", "#1B232B",
     "#6B7480", "#DCE0E4", "#FFFFFF"),
    ("Graphite & Cyan (dark)",
     "#101216", "#E8EAEC", "#2FB9BE", "#16181C", "#21252B", "#E8EAEC",
     "#8A9097", "#2C3138", "#0B0D0F"),
]

PW, PH = 590, 372
GAP = 24
COLS = 2
MARGIN = 24
HEADER = 64


def panel(s, x, y):
    (name, mast, mtext, accent, bg, card, ink, muted, line, on_acc) = s
    o = [R(x, y, PW, PH, bg, rx=6, stroke="#00000018")]
    # masthead
    mh = 78
    o.append(R(x, y, PW, mh, mast))
    o.append(R(x, y, PW, 4, accent))
    cx = x + PW / 2
    o.append(T(cx, y + 34, "MIDDLE EAST", 12, mtext, SERIF, "400", "middle", "6"))
    o.append(T(cx, y + 62, "Intelligence Hub", 26, mtext, SERIF, "700", "middle"))
    o.append(f'<circle cx="{x+PW-58}" cy="{y+26}" r="3.5" fill="{accent}"/>')
    o.append(T(x + PW - 48, y + 30, "Live", 11, mtext, SANS, "600"))
    # nav
    ny = y + mh
    o.append(R(x, ny, PW, 34, card))
    o.append(R(x, ny + 33, PW, 1, line))
    nx = x + 22
    for i, it in enumerate(("Front Pages", "Headlines", "Briefing")):
        col = accent if i == 0 else ink
        o.append(T(nx, ny + 22, it, 12, col, SANS, "700" if i == 0 else "500"))
        if i == 0:
            o.append(R(nx - 2, ny + 30, len(it) * 7 + 6, 3, accent))
        nx += len(it) * 7.5 + 30
    # body: section head + two cards
    byo = ny + 34 + 28
    o.append(T(x + 22, byo, "GULF", 19, ink, SERIF, "700"))
    o.append(R(x + 22, byo + 9, 52, 3, accent))
    cards = [
        ("Gulf states press for revived nuclear framework", "AL JAZEERA · 41 MIN"),
        ("Saudi Aramco second-quarter profit beats forecast", "ARAB NEWS · 1 HR"),
    ]
    cy = byo + 26
    cw = PW - 44
    for title, meta in cards:
        ch = 60
        o.append(R(x + 22, cy, cw, ch, card, rx=4, stroke=line))
        o.append(R(x + 22, cy, cw, 3, accent))
        o.append(T(x + 38, cy + 30, title, 14.5, ink, SERIF, "500"))
        o.append(T(x + 38, cy + 49, meta, 10, muted, SANS, "600", spacing="0.5"))
        cy += ch + 12
    # briefing chip (filled accent) + language pills
    o.append(R(x + 22, cy, 150, 26, accent, rx=13))
    o.append(T(x + 22 + 75, cy + 17, "WORLD BRIEFING", 11, on_acc, SANS, "700",
              "middle", "0.5"))
    px = x + 184
    for i, lab in enumerate(("EN", "HE", "AR")):
        on = i == 0
        o.append(R(px, cy, 36, 26, accent if on else card, rx=13,
                   stroke=accent if on else line))
        o.append(T(px + 18, cy + 17, lab, 11, on_acc if on else ink, SANS,
                   "700", "middle"))
        px += 42
    # label strip
    ly = y + PH - 26
    o.append(T(x + 22, ly, name, 14, ink, SERIF, "700"))
    o.append(T(x + PW - 22, ly, f"{accent.upper()}  /  {mast.upper()}  /  {bg.upper()}",
              9.5, muted, SANS, "600", anchor="end", spacing="0.5"))
    return "\n".join(o)


rows = (len(SCHEMES) + COLS - 1) // COLS
W = MARGIN * 2 + PW * COLS + GAP * (COLS - 1)
H = HEADER + MARGIN + (PH + GAP) * rows
body = [R(0, 0, W, H, "#FFFFFF"), R(0, 0, W, HEADER, "#16161A"),
        R(0, 0, W, 4, "#C0212A"),
        T(MARGIN, 40, "FULL COLOR SCHEMES  —  new accent + masthead + background systems",
          18, "#fff", SERIF, "700")]
for i, s in enumerate(SCHEMES):
    r, c = divmod(i, COLS)
    x = MARGIN + c * (PW + GAP)
    y = HEADER + MARGIN + r * (PH + GAP)
    body.append(panel(s, x, y))

svg = (f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" '
       f'viewBox="0 0 {W} {H}">{"".join(body)}</svg>')
cairosvg.svg2png(bytestring=svg.encode("utf-8"),
                 write_to="/home/user/mena-newsstand/mockups/palette_schemes.png",
                 output_width=W, output_height=H, background_color="white")
print(f"wrote palette_schemes.png ({W}x{H})")
