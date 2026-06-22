#!/usr/bin/env python3
"""Background-color options to replace the cream --bg (#F4F2EC).

Renders the real page area (white cards + crimson + ink serif headlines) on
each candidate background so they can be compared side by side. Outputs one
comparison sheet plus full-layout previews of the top picks.
"""
import html
import cairosvg

SERIF = "Liberation Serif, Georgia, serif"
SANS  = "Liberation Sans, Arial, sans-serif"
ACCENT = "#C0212A"
INK    = "#141414"
DARK   = "#16161A"
MUTE   = "#6B6B6B"


def esc(s):
    return html.escape(s, quote=True)


def T(x, y, s, size=15, fill=INK, family=SANS, weight="400", anchor="start",
      spacing=None):
    sp = f' letter-spacing="{spacing}"' if spacing is not None else ""
    return (f'<text x="{x}" y="{y}" font-family="{family}" font-size="{size}" '
            f'fill="{fill}" font-weight="{weight}" text-anchor="{anchor}"{sp}>'
            f'{esc(s)}</text>')


def R(x, y, w, h, fill, rx=0, stroke=None, sw=1):
    s = f' stroke="{stroke}" stroke-width="{sw}"' if stroke else ""
    return f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{rx}" fill="{fill}"{s}/>'


def line(x1, y1, x2, y2, stroke, sw=1):
    return (f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" '
            f'stroke="{stroke}" stroke-width="{sw}"/>')


# Candidate backgrounds (name, hex, short note, line color)
OPTIONS = [
    ("Cream (current)", "#F4F2EC", "warm ivory — what's live now", "#E2DFD6"),
    ("Porcelain",       "#F7F7F5", "near-white, clean & bright",    "#E6E6E2"),
    ("Newsprint Gray",  "#ECECEA", "neutral light gray, classic",   "#DCDCD9"),
    ("Cool Mist",       "#EBEEF1", "faint cool blue-gray",          "#D8DDE2"),
    ("Warm Stone",      "#EDE9E3", "soft taupe, less yellow",       "#DCD6CC"),
    ("Steel Blue-Gray", "#E5EAEE", "muted slate, editorial cool",   "#D2DAE0"),
    ("Greige",          "#E9E7E2", "balanced gray-beige",           "#D8D5CE"),
    ("Pale Sage",       "#E9EDE7", "barely-there green, calm",      "#D6DDD3"),
]

ROW_H = 132
LABEL_W = 290
CARD_X = LABEL_W + 30
W = 1180


def row(opt, y0):
    name, hexv, note, ln = opt
    out = [R(0, y0, W, ROW_H, hexv)]  # the candidate background fills the row
    # left: swatch chip + name + hex + note
    out.append(R(28, y0 + 30, 70, 70, hexv, rx=6, stroke="#00000022"))
    out.append(T(118, y0 + 52, name, 19, INK, SERIF, "700"))
    out.append(T(118, y0 + 74, hexv.upper(), 13, MUTE, SANS, "700", spacing="1"))
    out.append(T(118, y0 + 94, note, 12.5, MUTE, SANS, "400"))
    # divider
    out.append(line(LABEL_W, y0 + 16, LABEL_W, y0 + ROW_H - 16, "#00000018"))
    # right: a real white card on this bg with crimson top + section head + 2 lines
    cx, cy, cw, ch = CARD_X, y0 + 20, W - CARD_X - 28, ROW_H - 40
    out.append(R(cx, cy, cw, ch, "#FFFFFF", rx=4))
    out.append(R(cx, cy, cw, 3, ACCENT))
    out.append(T(cx + 22, cy + 32, "GULF", 18, INK, SERIF, "700"))
    out.append(R(cx + 22, cy + 40, 48, 3, ACCENT))
    out.append(T(cx + 22, cy + 66, "Gulf states press for revived nuclear framework as talks resume",
                 14.5, INK, SERIF, "500"))
    out.append(T(cx + 22, cy + 82, "AL JAZEERA · 41 MIN AGO", 10.5, MUTE, SANS,
                 "600", spacing="0.5"))
    return "\n".join(out)


def comparison_sheet():
    H = 70 + ROW_H * len(OPTIONS) + 24
    body = [R(0, 0, W, H, "#FFFFFF")]
    body.append(R(0, 0, W, 56, DARK))
    body.append(R(0, 0, W, 4, ACCENT))
    body.append(T(28, 36, "BACKGROUND OPTIONS  —  replacing the cream page color",
                  18, "#fff", SERIF, "700"))
    y = 70
    for opt in OPTIONS:
        body.append(row(opt, y))
        y += ROW_H
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" '
           f'viewBox="0 0 {W} {H}">{"".join(body)}</svg>')
    cairosvg.svg2png(bytestring=svg.encode("utf-8"),
                     write_to="/home/user/mena-newsstand/mockups/bg_options.png",
                     output_width=W, output_height=H, background_color="white")
    print(f"wrote bg_options.png ({W}x{H})")


comparison_sheet()
