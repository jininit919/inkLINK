"""Generate docs/pitch_deck.pdf — 6-slide investor pitch for InkLink.

Run:
    python3 scripts/build_pitch_deck.py
Output:
    docs/pitch_deck.pdf  (A4 landscape, paper-mode design)

Why reportlab + Canvas (not platypus): full pixel-level control for
slide layouts. Each slide is rendered manually to match the InkLink
paper-mode aesthetic (#faf8f3 bg, #c62828 accent, ink crimson dots).
"""
import os
import sys
from pathlib import Path
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.units import mm
from reportlab.lib.colors import HexColor
from reportlab.pdfgen import canvas as rl_canvas
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
import reportlab as _rl

# Register a Czech-capable font and alias it to 'Helvetica' so the rest of the
# script can use the standard PostScript names. Search order:
#   1. /Library/Fonts/Arial Unicode.ttf       (macOS, full Czech)
#   2. /System/Library/Fonts/Helvetica.ttc    (macOS, subfont index 0/1)
#   3. reportlab bundled Vera                  (partial — fallback for CI)
# Bitstream Vera lacks ř/ů/Ě — pick a system font if available.
_RL_FONTS = os.path.join(os.path.dirname(_rl.__file__), 'fonts')
_CANDIDATES = [
    ('/Library/Fonts/Arial Unicode.ttf', '/Library/Fonts/Arial Unicode.ttf', None),
    ('/System/Library/Fonts/Helvetica.ttc', '/System/Library/Fonts/Helvetica.ttc', 0),
]
_VERA = (os.path.join(_RL_FONTS, 'Vera.ttf'), os.path.join(_RL_FONTS, 'VeraBd.ttf'), None)

_picked = None
for reg, bold, idx in _CANDIDATES:
    if os.path.exists(reg):
        _picked = (reg, bold, idx)
        break
if _picked is None:
    _picked = _VERA

_reg, _bold, _idx = _picked
try:
    if _idx is not None:
        pdfmetrics.registerFont(TTFont('Helvetica',      _reg,  subfontIndex=0))
        pdfmetrics.registerFont(TTFont('Helvetica-Bold', _bold, subfontIndex=1))
    else:
        pdfmetrics.registerFont(TTFont('Helvetica',      _reg))
        pdfmetrics.registerFont(TTFont('Helvetica-Bold', _bold))
except Exception as _e:
    print(f'[font] {_e} — falling back to Vera', file=sys.stderr)
    pdfmetrics.registerFont(TTFont('Helvetica',      _VERA[0]))
    pdfmetrics.registerFont(TTFont('Helvetica-Bold', _VERA[1]))

# ── Design tokens (mirror theme.css paper mode) ────────────────────────────
PAPER       = HexColor('#faf8f3')
INK         = HexColor('#0a0a0a')
INK_DARK    = HexColor('#1a1a1a')
INK_MUTED   = HexColor('#5a5a5a')
INK_FAINT   = HexColor('#9a9a9a')
RULE        = HexColor('#d4cfbf')
RULE_DARK   = HexColor('#a8a399')
ACCENT      = HexColor('#c62828')
ACCENT_DEEP = HexColor('#8b0000')

# Pitch content (keep this short and confident — incubator deck, not VC deck)
PROJECT_ROOT = Path(__file__).resolve().parent.parent
# Pre-processed transparent-bg PNG of the full "inklink" wordmark. Built by
# scripts/build_app_assets.py or the one-off snippet that crops the white
# margins and turns white pixels into alpha=0.
LOGO_HERO = PROJECT_ROOT / 'docs' / '_assets' / 'inklink-logo.png'
OUT_PATH = PROJECT_ROOT / 'docs' / 'pitch_deck.pdf'

# A4 landscape canvas size
PAGE = landscape(A4)
PAGE_W, PAGE_H = PAGE


def _draw_page_background(c):
    """Paint paper background + subtle slide number marker."""
    c.setFillColor(PAPER)
    c.rect(0, 0, PAGE_W, PAGE_H, fill=1, stroke=0)


def _draw_footer(c, slide_no, total):
    """Bottom-left brand + bottom-right slide counter."""
    c.setFillColor(INK_FAINT)
    c.setFont('Helvetica', 8)
    c.drawString(18 * mm, 12 * mm, 'INKLINK · pitch · 2026')
    c.drawRightString(PAGE_W - 18 * mm, 12 * mm, f'{slide_no:02d} / {total:02d}')
    # accent dot bottom-left as ink-blot reference
    c.setFillColor(ACCENT)
    c.circle(13 * mm, 13 * mm, 1.4 * mm, fill=1, stroke=0)


def _h_text(c, text, x, y, size=42, color=INK, font='Helvetica-Bold'):
    c.setFillColor(color)
    c.setFont(font, size)
    c.drawString(x, y, text)


def _wrap_lines(text, max_chars=78):
    """Very simple word-wrap for body text (we're not running TextObject layout)."""
    out, cur = [], ''
    for word in text.split(' '):
        if len(cur) + len(word) + 1 > max_chars:
            out.append(cur.strip()); cur = word + ' '
        else:
            cur += word + ' '
    if cur.strip(): out.append(cur.strip())
    return out


def _body(c, lines, x, y, size=12, color=INK_DARK, leading=18, font='Helvetica'):
    c.setFillColor(color)
    c.setFont(font, size)
    for ln in lines:
        c.drawString(x, y, ln)
        y -= leading
    return y


# ── Slide 1: HOOK ──────────────────────────────────────────────────────────
def slide_1_hook(c):
    _draw_page_background(c)

    # Full "inklink" wordmark — transparent-bg PNG, hero on top half.
    # Aspect ~3.4:1 → at full width inside margins (W - 56mm), height ≈ width/3.4.
    if LOGO_HERO.exists():
        margin = 28 * mm
        logo_w = (PAGE_W - 2 * margin) * 0.62      # 62 % of usable width
        logo_h = logo_w / 3.4
        # Center horizontally (block-level)
        logo_x = (PAGE_W - logo_w) / 2
        logo_y = PAGE_H - 95 * mm                  # baseline of image
        c.drawImage(str(LOGO_HERO),
                    logo_x, logo_y,
                    width=logo_w, height=logo_h,
                    mask='auto', preserveAspectRatio=True)
        # Crimson accent dot — placed after the last "k" of inklink
        dot_x = logo_x + logo_w + 4 * mm
        dot_y = logo_y + logo_h * 0.32
        c.setFillColor(ACCENT)
        c.circle(dot_x, dot_y, 3.6 * mm, fill=1, stroke=0)

    # Subtitle (under the logo, centered)
    c.setFillColor(INK_MUTED)
    c.setFont('Helvetica', 13)
    sub = 'TATTOO BOOKING NETWORK · ČESKÁ REPUBLIKA'
    sub_w = c.stringWidth(sub, 'Helvetica', 13)
    c.drawString((PAGE_W - sub_w) / 2, PAGE_H - 115 * mm, sub)

    # Tagline — centered, big
    c.setFillColor(INK)
    c.setFont('Helvetica-Bold', 30)
    for i, line in enumerate(['Rezervuj tetování.', 'Bez DM ping-pongů.']):
        lw = c.stringWidth(line, 'Helvetica-Bold', 30)
        c.drawString((PAGE_W - lw) / 2, PAGE_H - (140 + i * 13) * mm, line)

    # Supporting line — centered
    c.setFillColor(INK_MUTED)
    c.setFont('Helvetica', 12)
    sup = 'Marketplace s férovými pravidly, Stripe escrow a transparentní cenou.'
    sw = c.stringWidth(sup, 'Helvetica', 12)
    c.drawString((PAGE_W - sw) / 2, PAGE_H - 180 * mm, sup)

    _draw_footer(c, 1, 6)


# ── Slide 2: PROBLEM + SOLUTION ────────────────────────────────────────────
def slide_2_problem_solution(c):
    _draw_page_background(c)

    # Top label
    c.setFillColor(ACCENT)
    c.setFont('Helvetica-Bold', 10)
    c.drawString(28 * mm, PAGE_H - 26 * mm, 'PROBLÉM · ŘEŠENÍ')

    # Hairline
    c.setStrokeColor(RULE)
    c.setLineWidth(0.5)
    c.line(28 * mm, PAGE_H - 30 * mm, PAGE_W - 28 * mm, PAGE_H - 30 * mm)

    # Two columns
    col_w = (PAGE_W - 56 * mm - 14 * mm) / 2
    left_x = 28 * mm
    right_x = left_x + col_w + 14 * mm
    y0 = PAGE_H - 50 * mm

    # LEFT — problém
    _h_text(c, 'PROBLÉM', left_x, y0, size=24, color=INK)
    _body(c, [
        'Klient hledá tatéra přes Instagram DM —',
        'odpověď přijde za 3 dny, ceník nikde,',
        'záloha přes účet, kterému nikdo nedůvěřuje.',
    ], left_x, y0 - 22, size=12, color=INK_DARK, leading=18)

    _body(c, [
        '· 60–80 % no-show při bezzálohových termínech',
        '· Žádná transparentní cena → klient odejde',
        '· Tatér promešká hodiny v DMs',
        '· Žádný systém recenzí → reputace = jen IG',
    ], left_x, y0 - 80, size=11, color=INK_MUTED, leading=16)

    # RIGHT — řešení
    _h_text(c, 'ŘEŠENÍ', right_x, y0, size=24, color=ACCENT)
    _body(c, [
        'Curated marketplace s online rezervací,',
        'Stripe escrow pro zálohy a transparentními',
        'pravidly storna (96/48 h refund tier).',
    ], right_x, y0 - 22, size=12, color=INK_DARK, leading=18)

    _body(c, [
        '· Stripe Connect → záloha 30 %, peníze tatérovi',
        '· Pravidla storna v ToS, refund automatický',
        '· Recenze 5★ až po dokončené práci',
        '· Founding programy: 0 % provize / first 500 free',
    ], right_x, y0 - 80, size=11, color=INK_MUTED, leading=16)

    _draw_footer(c, 2, 6)


# ── Slide 3: MARKET ────────────────────────────────────────────────────────
def slide_3_market(c):
    _draw_page_background(c)
    c.setFillColor(ACCENT)
    c.setFont('Helvetica-Bold', 10)
    c.drawString(28 * mm, PAGE_H - 26 * mm, 'TRH')
    c.setStrokeColor(RULE)
    c.setLineWidth(0.5)
    c.line(28 * mm, PAGE_H - 30 * mm, PAGE_W - 28 * mm, PAGE_H - 30 * mm)

    _h_text(c, 'Český tetovací trh', 28 * mm, PAGE_H - 55 * mm, size=30, color=INK)

    # Three big number cards
    cards = [
        ('~2 500',  'aktivních tatérů v ČR',          'studio + freelance dohromady'),
        ('~150 K',  'tetování ročně',                 'odhad podle průměrného tatéra'),
        ('1.2 mld', 'CZK roční obrat odvětví',        'průměr 8 000 Kč na tetování'),
    ]
    card_w = (PAGE_W - 56 * mm - 28 * mm) / 3
    cy = PAGE_H - 130 * mm
    for i, (big, mid, small) in enumerate(cards):
        x = 28 * mm + i * (card_w + 14 * mm)
        c.setStrokeColor(RULE_DARK)
        c.setLineWidth(0.7)
        c.line(x, cy + 28 * mm, x + card_w, cy + 28 * mm)
        c.setFillColor(ACCENT)
        c.setFont('Helvetica-Bold', 36)
        c.drawString(x, cy + 6 * mm, big)
        c.setFillColor(INK_DARK)
        c.setFont('Helvetica-Bold', 12)
        c.drawString(x, cy - 4 * mm, mid)
        c.setFillColor(INK_MUTED)
        c.setFont('Helvetica', 10)
        c.drawString(x, cy - 12 * mm, small)

    # Bottom takeaway
    c.setFillColor(INK_DARK)
    c.setFont('Helvetica', 13)
    c.drawString(28 * mm, 38 * mm,
                 'Při 5 % marketshare a 8 % provizi = 4.8 mio CZK ARR jen z ČR.')
    c.setFillColor(INK_MUTED)
    c.setFont('Helvetica', 11)
    c.drawString(28 * mm, 30 * mm,
                 'Expandovatelné na SK / PL / DE — totožný legal stack přes Stripe Connect Europe.')

    _draw_footer(c, 3, 6)


# ── Slide 4: TRACTION / PRODUCT ────────────────────────────────────────────
def slide_4_traction(c):
    _draw_page_background(c)
    c.setFillColor(ACCENT)
    c.setFont('Helvetica-Bold', 10)
    c.drawString(28 * mm, PAGE_H - 26 * mm, 'PRODUKT · STAV')
    c.setStrokeColor(RULE)
    c.setLineWidth(0.5)
    c.line(28 * mm, PAGE_H - 30 * mm, PAGE_W - 28 * mm, PAGE_H - 30 * mm)

    _h_text(c, 'Co máme hotové', 28 * mm, PAGE_H - 55 * mm, size=30, color=INK)

    # Two columns of bullets
    col_w = (PAGE_W - 56 * mm - 14 * mm) / 2
    left_x = 28 * mm
    right_x = left_x + col_w + 14 * mm
    y0 = PAGE_H - 80 * mm

    c.setFillColor(INK_DARK)
    c.setFont('Helvetica-Bold', 13)
    c.drawString(left_x, y0, 'TECHNOLOGIE')
    _body(c, [
        '· Web + iOS/Android (Capacitor) v jednom kódu',
        '· Stripe Connect Express — KYC, escrow, payouts',
        '· APNs push + welcome email sekvence',
        '· iCal feed (Google/Apple Calendar)',
        '· Reconciliation cron, Sentry, GDPR export',
        '· 78 automatizovaných testů, 0 regression',
    ], left_x, y0 - 22, size=11, color=INK_MUTED, leading=16)

    c.setFillColor(INK_DARK)
    c.setFont('Helvetica-Bold', 13)
    c.drawString(right_x, y0, 'GO-TO-MARKET CONNECTOR')
    _body(c, [
        '· Pricing engine s tiered komisí (12 / 8 / 5 %)',
        '· Founding artist program: 0 % první měsíc',
        '· Founding client: prvních 500 free service fee',
        '· Referral 300 Kč za první rezervaci',
        '· Auto refund tier (96 h 100 %, 48 h 50 %)',
        '· Cookie consent, Soft account deletion 30 d',
    ], right_x, y0 - 22, size=11, color=INK_MUTED, leading=16)

    # Bottom: status badge
    c.setStrokeColor(ACCENT)
    c.setLineWidth(1.2)
    c.line(28 * mm, 38 * mm, 28 * mm + 50 * mm, 38 * mm)
    c.setFillColor(INK_DARK)
    c.setFont('Helvetica-Bold', 12)
    c.drawString(28 * mm, 30 * mm, 'STATUS: production deploy na Railway · ready pro launch.')

    _draw_footer(c, 4, 6)


# ── Slide 5: TEAM ──────────────────────────────────────────────────────────
def slide_5_team(c):
    _draw_page_background(c)
    c.setFillColor(ACCENT)
    c.setFont('Helvetica-Bold', 10)
    c.drawString(28 * mm, PAGE_H - 26 * mm, 'TÝM')
    c.setStrokeColor(RULE)
    c.setLineWidth(0.5)
    c.line(28 * mm, PAGE_H - 30 * mm, PAGE_W - 28 * mm, PAGE_H - 30 * mm)

    _h_text(c, 'Tým', 28 * mm, PAGE_H - 55 * mm, size=30, color=INK)

    # Founder card
    x0, y0 = 28 * mm, PAGE_H - 100 * mm
    c.setStrokeColor(RULE_DARK)
    c.setLineWidth(0.7)
    c.line(x0, y0 + 28 * mm, x0 + 110 * mm, y0 + 28 * mm)

    c.setFillColor(INK)
    c.setFont('Helvetica-Bold', 22)
    c.drawString(x0, y0 + 14 * mm, 'Matěj Gajdoš')
    c.setFillColor(ACCENT)
    c.setFont('Helvetica-Bold', 11)
    c.drawString(x0, y0 + 6 * mm, 'FOUNDER · PRODUCT · ENGINEERING')
    c.setFillColor(INK_MUTED)
    c.setFont('Helvetica', 11)
    _body(c, [
        'IČO 29532744 · Česká republika',
        'Solo founder — staví celý stack: backend (Flask/Postgres/Stripe),',
        'frontend (paper-mode design system), mobile (Capacitor), ops.',
    ], x0, y0 - 2 * mm, size=11, color=INK_MUTED, leading=15)

    # What we're looking for
    rx = 160 * mm
    c.setFillColor(INK_DARK)
    c.setFont('Helvetica-Bold', 13)
    c.drawString(rx, y0 + 14 * mm, 'KOHO HLEDÁME')
    _body(c, [
        '· Co-founder pro growth / sales',
        '  (network v tetovacích studiích, IG/TT)',
        '· Mentor pro marketplace dynamiku',
        '· Part-time community manažer',
    ], rx, y0 + 6 * mm, size=11, color=INK_MUTED, leading=16)

    _draw_footer(c, 5, 6)


# ── Slide 6: ASK ───────────────────────────────────────────────────────────
def slide_6_ask(c):
    _draw_page_background(c)
    c.setFillColor(ACCENT)
    c.setFont('Helvetica-Bold', 10)
    c.drawString(28 * mm, PAGE_H - 26 * mm, 'CO HLEDÁME')
    c.setStrokeColor(RULE)
    c.setLineWidth(0.5)
    c.line(28 * mm, PAGE_H - 30 * mm, PAGE_W - 28 * mm, PAGE_H - 30 * mm)

    _h_text(c, 'Místo v inkubátoru.', 28 * mm, PAGE_H - 60 * mm, size=30, color=INK)
    c.setFillColor(INK_MUTED)
    c.setFont('Helvetica', 14)
    c.drawString(28 * mm, PAGE_H - 75 * mm,
                 'Produkt máme. Hledáme mentoring, network a první uživatele.')

    # Three columns of asks
    cards = [
        ('MENTORING', 'Marketplace dynamika · cold start · acquisition.'),
        ('NETWORK',   'Přístup k tatérským komunitám, tisku, prvním studiím.'),
        ('KAPITÁL',   'Pre-seed 0.5–1 M CZK na launch, ads a community manager.'),
    ]
    card_w = (PAGE_W - 56 * mm - 28 * mm) / 3
    cy = PAGE_H - 130 * mm
    for i, (lbl, body) in enumerate(cards):
        x = 28 * mm + i * (card_w + 14 * mm)
        c.setStrokeColor(ACCENT)
        c.setLineWidth(1.2)
        c.line(x, cy + 32 * mm, x + 40 * mm, cy + 32 * mm)
        c.setFillColor(ACCENT)
        c.setFont('Helvetica-Bold', 13)
        c.drawString(x, cy + 22 * mm, lbl)
        c.setFillColor(INK_DARK)
        c.setFont('Helvetica', 12)
        for j, ln in enumerate(_wrap_lines(body, max_chars=38)):
            c.drawString(x, cy + 12 * mm - j * 16, ln)

    # Contact bar
    c.setFillColor(INK)
    c.setFont('Helvetica-Bold', 13)
    c.drawString(28 * mm, 42 * mm, 'KONTAKT')
    c.setFillColor(INK_DARK)
    c.setFont('Helvetica', 12)
    c.drawString(28 * mm, 32 * mm, 'matej@inklink.cz · inklink.club · IČO 29532744')

    _draw_footer(c, 6, 6)


# ── Build ──────────────────────────────────────────────────────────────────
def main() -> int:
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    c = rl_canvas.Canvas(str(OUT_PATH), pagesize=PAGE)
    c.setTitle('InkLink — Pitch')
    c.setAuthor('InkLink (Matěj Gajdoš)')
    c.setSubject('Pitch deck pro startup inkubátor')

    slides = [
        slide_1_hook,
        slide_2_problem_solution,
        slide_3_market,
        slide_4_traction,
        slide_5_team,
        slide_6_ask,
    ]
    for fn in slides:
        fn(c)
        c.showPage()
    c.save()
    print(f'OK  →  {OUT_PATH.relative_to(PROJECT_ROOT)}  ({OUT_PATH.stat().st_size // 1024} KB)')
    return 0


if __name__ == '__main__':
    sys.exit(main())
