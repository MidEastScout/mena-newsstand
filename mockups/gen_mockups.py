#!/usr/bin/env python3
"""Generate design-direction mockups for Middle East Intelligence Hub.

Builds each option as an SVG (pixel-accurate to the site's real color and
typography tokens) and rasterizes to PNG via cairosvg. No browser needed.
"""
import html
import cairosvg

W = 1240  # desktop canvas width

SERIF = "Liberation Serif, Georgia, serif"
SANS  = "Liberation Sans, Arial, sans-serif"

# Shared tokens (match index.html)
ACCENT = "#C0212A"
INK    = "#141414"
CREAM  = "#F4F2EC"
DARK   = "#16161A"
LINE   = "#E2DFD6"
MUTE   = "#6B6B6B"


def esc(s):
    return html.escape(s, quote=True)


def T(x, y, s, size=15, fill=INK, family=SANS, weight="400",
      anchor="start", spacing=None, style=""):
    sp = f' letter-spacing="{spacing}"' if spacing is not None else ""
    st = f' font-style="{style}"' if style else ""
    return (f'<text x="{x}" y="{y}" font-family="{family}" font-size="{size}" '
            f'fill="{fill}" font-weight="{weight}" text-anchor="{anchor}"{sp}{st}>'
            f'{esc(s)}</text>')


def R(x, y, w, h, fill, rx=0, stroke=None, sw=1, opacity=None):
    s = f' stroke="{stroke}" stroke-width="{sw}"' if stroke else ""
    o = f' opacity="{opacity}"' if opacity is not None else ""
    return f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{rx}" fill="{fill}"{s}{o}/>'


def line(x1, y1, x2, y2, stroke, sw=1):
    return (f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" '
            f'stroke="{stroke}" stroke-width="{sw}"/>')


def live_dot(x, y, on_dark=True):
    c = "#fff" if on_dark else INK
    return (f'<circle cx="{x}" cy="{y}" r="4" fill="{ACCENT}"/>'
            + T(x + 10, y + 4, "LIVE", 11, c, SANS, "700", spacing="1.5"))


def masthead(y0, h=120, region_color="rgba(255,255,255,.6)"):
    """Dark hero masthead band, returns svg + bottom y."""
    cx = W // 2
    out = [
        R(0, y0, W, h, DARK),
        R(0, y0, W, 5, ACCENT),  # crimson top stripe
        T(cx, y0 + 50, "MIDDLE EAST", 22, "#FFFFFF", SERIF, "400",
          "middle", spacing="9"),
        T(cx, y0 + 92, "Intelligence Hub", 46, "#FFFFFF", SERIF, "700", "middle"),
    ]
    # live pill top-right
    out.append(R(W - 110, y0 + 22, 84, 26, "#26262B", rx=13,
                 stroke="rgba(255,255,255,.15)"))
    out.append(f'<circle cx="{W-92}" cy="{y0+35}" r="4" fill="{ACCENT}"/>')
    out.append(T(W - 82, y0 + 39, "Live", 12, "rgba(255,255,255,.85)", SANS, "600"))
    out.append(T(36, y0 + 39, "JUN 22, 2026", 12, "rgba(255,255,255,.55)",
                 SANS, "600", spacing="1.5"))
    return "\n".join(out), y0 + h


def navbar(y0, bg="#FFFFFF", h=52, active=0, on_dark=False,
           items=("Front Pages", "Headlines", "World Briefing")):
    out = [R(0, y0, W, h, bg)]
    out.append(line(0, y0 + h, W, y0 + h, LINE if not on_dark else "#2C2C32"))
    x = 40
    base = "#E8E6DF" if on_dark else INK
    for i, it in enumerate(items):
        col = ACCENT if i == active else (base if not on_dark else "#B8B6B0")
        wgt = "700" if i == active else "500"
        out.append(T(x, y0 + 33, it, 15, col, SANS, wgt))
        if i == active:
            tw = len(it) * 8 + 8
            out.append(R(x - 2, y0 + h - 3, tw, 3, ACCENT))
        x += len(it) * 9 + 44
    return "\n".join(out), y0 + h


def sec_head(x, y, label, on_dark=False):
    col = "#fff" if on_dark else INK
    out = [
        T(x, y, label, 26, col, SERIF, "700"),
        R(x, y + 10, 64, 3, ACCENT),
    ]
    return "\n".join(out)


def headline_rows(x, y, w, items, gap=30, on_dark=False, size=15):
    col = "#E8E6DF" if on_dark else INK
    out = []
    cy = y
    for title, meta in items:
        out.append(T(x, cy, title, size, col, SERIF, "500"))
        out.append(T(x, cy + 17, meta, 11.5, MUTE if not on_dark else "#8A8A90",
                     SANS, "600", spacing="0.5"))
        cy += gap + 14
        out.append(line(x, cy - 16, x + w, cy - 16,
                        LINE if not on_dark else "#2C2C32"))
    return "\n".join(out), cy


def wrap_svg(height, bg, body):
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" '
            f'height="{height}" viewBox="0 0 {W} {height}">'
            f'{R(0,0,W,height,bg)}{body}</svg>')


# ---- sample content -------------------------------------------------------
GULF = [
    ("Gulf states press for revived nuclear framework as talks resume in Muscat",
     "AL JAZEERA · 41 MIN AGO"),
    ("Saudi Aramco second-quarter profit beats forecasts on higher output",
     "ARAB NEWS · 1 HR AGO"),
    ("UAE central bank holds rates, flags resilient non-oil growth",
     "THE NATIONAL · 2 HR AGO"),
]
LEVANT = [
    ("Lebanon ceasefire monitors report calm along southern border",
     "L'ORIENT TODAY · 32 MIN AGO"),
    ("Syria reconstruction conference opens in Damascus amid sanctions debate",
     "WAFA NEWS · 1 HR AGO"),
    ("Jordan and Iraq sign cross-border electricity grid agreement",
     "JORDAN TIMES · 3 HR AGO"),
]
BRIEF_LEAD = ("Russia-Ukraine:  Overnight strikes on energy infrastructure "
              "drew fresh Western condemnation as European leaders weighed a "
              "new tranche of air-defence support.")
BRIEF_PARAS = [
    "US-China:  Washington and Beijing traded warnings over tech-export curbs "
    "ahead of next week's ministerial.",
    "Global economy:  Markets steadied after the latest inflation print, with "
    "oil holding above $80 a barrel.",
    "Middle East:  Diplomats signalled cautious progress on a regional security "
    "track as Gulf mediators stepped in.",
]


# =========================================================================
# OPTION A — Magazine Hero
# =========================================================================
def option_a():
    b, y = masthead(0)
    parts = [b]
    # HERO briefing card (cream/crimson) BETWEEN masthead and nav
    hx, hy, hw, hh = 40, y + 28, W - 80, 188
    parts.append(R(hx, hy, hw, hh, "#FFFFFF", rx=4))
    parts.append(R(hx, hy, hw, 4, ACCENT))  # crimson top border
    parts.append(T(hx + 28, hy + 40, "WORLD BRIEFING", 13, ACCENT, SANS, "700",
                   spacing="2"))
    parts.append(T(hx + hw - 28, hy + 40, "JUN 22 · 06:00 UTC", 11.5, MUTE,
                   SANS, "600", anchor="end"))
    # large lead paragraph (two lines)
    parts.append(T(hx + 28, hy + 80, "Russia-Ukraine:  Overnight strikes on energy infrastructure drew fresh",
                   21, INK, SERIF, "500"))
    parts.append(T(hx + 28, hy + 108, "Western condemnation as European leaders weighed new air-defence support.",
                   21, INK, SERIF, "500"))
    # language toggle pills
    tx = hx + 28
    for i, lab in enumerate(("EN", "HE", "AR")):
        on = i == 0
        parts.append(R(tx, hy + 132, 44, 26,
                       ACCENT if on else "#FFFFFF", rx=13,
                       stroke=ACCENT if on else LINE))
        parts.append(T(tx + 22, hy + 149, lab, 12,
                       "#fff" if on else INK, SANS, "700", "middle"))
        tx += 52
    nav_y = hy + hh + 20
    nb, y2 = navbar(nav_y)
    parts.append(nb)
    # regions
    gy = y2 + 46
    parts.append(sec_head(40, gy, "GULF"))
    hb, _ = headline_rows(40, gy + 44, 540, GULF)
    parts.append(hb)
    parts.append(sec_head(W // 2 + 20, gy, "LEVANT"))
    hb2, endy = headline_rows(W // 2 + 20, gy + 44, 540, LEVANT)
    parts.append(hb2)
    H = int(endy + 40)
    return wrap_svg(H, CREAM, "\n".join(parts)), H


# =========================================================================
# OPTION B — Two-Column Editorial (sticky sidebar)
# =========================================================================
def option_b():
    b, y = masthead(0)
    nb, y2 = navbar(y)
    parts = [b, nb]
    top = y2 + 44
    mainx = 40
    mainw = 740
    sidex = mainx + mainw + 36
    sidew = W - sidex - 40
    # main column
    parts.append(sec_head(mainx, top, "GULF"))
    hb, cy = headline_rows(mainx, top + 44, mainw, GULF)
    parts.append(hb)
    parts.append(sec_head(mainx, cy + 18, "LEVANT"))
    hb2, cy2 = headline_rows(mainx, cy + 62, mainw, LEVANT)
    parts.append(hb2)
    # sticky sidebar card
    sy = top
    sh = cy2 - top - 6
    parts.append(R(sidex, sy, sidew, sh, "#FFFFFF", rx=4))
    parts.append(R(sidex, sy, sidew, 4, ACCENT))
    parts.append(T(sidex + 22, sy + 38, "WORLD BRIEFING", 13, ACCENT, SANS,
                   "700", spacing="1.5"))
    parts.append(line(sidex + 22, sy + 52, sidex + sidew - 22, sy + 52, LINE))
    # briefing paras (wrapped short)
    by = sy + 80
    blocks = [
        ("Russia-Ukraine", "Overnight strikes drew fresh Western", "condemnation; allies weigh air defence."),
        ("US-China", "Trade warnings traded ahead of", "next week's ministerial talks."),
        ("Global economy", "Markets steadied after the latest", "inflation print; oil holds above $80."),
        ("Middle East", "Cautious progress on a regional", "security track via Gulf mediators."),
    ]
    for lead, l1, l2 in blocks:
        parts.append(T(sidex + 22, by, lead + ":", 14, INK, SERIF, "700"))
        parts.append(T(sidex + 22, by + 20, l1, 13.5, "#333", SERIF, "400"))
        parts.append(T(sidex + 22, by + 38, l2, 13.5, "#333", SERIF, "400"))
        by += 66
    # toggle + edition footer
    tx = sidex + 22
    for i, lab in enumerate(("EN", "HE", "AR")):
        on = i == 0
        parts.append(R(tx, by, 42, 24, ACCENT if on else "#FFFFFF", rx=12,
                       stroke=ACCENT if on else LINE))
        parts.append(T(tx + 21, by + 16, lab, 11.5, "#fff" if on else INK,
                       SANS, "700", "middle"))
        tx += 50
    parts.append(T(sidex + 22, by + 54, "EDITION · JUN 22 · 07:30 UTC", 11, MUTE,
                   SANS, "600", spacing="1"))
    H = int(cy2 + 40)
    return wrap_svg(H, CREAM, "\n".join(parts)), H


# =========================================================================
# OPTION C — Ink & Paper (high-contrast print)
# =========================================================================
def option_c():
    b, y = masthead(0)
    parts = [b]
    # plain text nav, no background, thin
    ny = y
    parts.append(R(0, ny, W, 46, "#FFFFFF"))
    parts.append(line(0, ny + 46, W, ny + 46, INK, 2))
    x = 40
    for i, it in enumerate(("Front Pages", "Headlines", "World Briefing")):
        col = ACCENT if i == 0 else INK
        parts.append(T(x, ny + 30, it.upper(), 13, col, SANS, "700", spacing="1"))
        x += len(it) * 9 + 40
    top = ny + 46 + 40
    # dense single-column with heavy rules, NO cards
    def block(x0, w0, label, items, y0):
        out = [T(x0, y0, label, 13, INK, SANS, "800", spacing="3")]
        out.append(line(x0, y0 + 12, x0 + w0, y0 + 12, INK, 1))
        cy = y0 + 42
        for title, meta in items:
            out.append(T(x0, cy, title, 16, INK, SERIF, "500"))
            out.append(T(x0, cy + 18, meta, 11, MUTE, SANS, "600", spacing="0.5"))
            cy += 50
            out.append(line(x0, cy - 14, x0 + w0, cy - 14, "#D8D5CC", 1))
        return "\n".join(out), cy
    colw = (W - 80 - 40) // 2
    g1, e1 = block(40, colw, "GULF", GULF, top)
    g2, e2 = block(40 + colw + 40, colw, "LEVANT", LEVANT, top)
    parts.append(g1)
    parts.append(g2)
    # vertical divider between columns
    parts.append(line(40 + colw + 20, top - 10, 40 + colw + 20, max(e1, e2) - 14,
                      "#D8D5CC", 1))
    H = int(max(e1, e2) + 30)
    return wrap_svg(H, "#FFFFFF", "\n".join(parts)), H


# =========================================================================
# OPTION D — Dark Mode Full Site
# =========================================================================
def option_d():
    PAGE = "#1A1A1E"
    CARD = "#26262B"
    b, y = masthead(0)
    nb, y2 = navbar(y, bg=CARD, on_dark=True)
    parts = [b, nb]
    top = y2 + 46
    parts.append(sec_head(40, top, "GULF", on_dark=True))

    def dcards(x0, w0, items, y0):
        out = []
        cy = y0
        for title, meta in items:
            ch = 70
            out.append(R(x0, cy, w0, ch, CARD, rx=4))
            out.append(R(x0, cy, 4, ch, ACCENT))  # crimson left border
            out.append(T(x0 + 22, cy + 30, title, 15.5, "#EDEBE4", SERIF, "500"))
            out.append(T(x0 + 22, cy + 52, meta, 11, "#8A8A90", SANS, "600",
                         spacing="0.5"))
            cy += ch + 14
        return "\n".join(out), cy
    c1, e1 = dcards(40, 540, GULF, top + 30)
    parts.append(c1)
    parts.append(sec_head(W // 2 + 20, top, "LEVANT", on_dark=True))
    c2, e2 = dcards(W // 2 + 20, 540, LEVANT, top + 30)
    parts.append(c2)
    H = int(max(e1, e2) + 36)
    return wrap_svg(H, PAGE, "\n".join(parts)), H


OPTIONS = {
    "option_A_magazine_hero": option_a,
    "option_B_two_column": option_b,
    "option_C_ink_and_paper": option_c,
    "option_D_dark_mode": option_d,
}

for name, fn in OPTIONS.items():
    svg, h = fn()
    out_png = f"/home/user/mena-newsstand/mockups/{name}.png"
    cairosvg.svg2png(bytestring=svg.encode("utf-8"), write_to=out_png,
                     output_width=W, output_height=h, background_color="white")
    print(f"wrote {out_png} ({W}x{h})")
