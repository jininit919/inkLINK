"""Generate docs/pricing_engine.pdf with the full pricing engine docs.

Run:
    python3 scripts/build_pricing_docs_pdf.py
Output:
    docs/pricing_engine.pdf

Why a script (not WeasyPrint or pandoc): reportlab is already in
requirements.txt for the monthly artist PDF reports. No new dep.
"""
import os
import sys
from datetime import datetime

# Ensure project root importable so we can read config values at build time.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import mm
from reportlab.lib.colors import HexColor, Color
from reportlab.lib.enums import TA_LEFT, TA_CENTER
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, PageBreak, Table, TableStyle,
    KeepTogether,
)

# Read live config values so PDF always reflects current rates
try:
    from pricing import (
        COMMISSION_TIERS, MIN_COMMISSION_CZK,
        SERVICE_FEE_RATE, SERVICE_FEE_CAP_CZK,
        FOUNDING_ARTIST_FREE_DAYS, FOUNDING_ARTIST_FLAT_DAYS,
        FOUNDING_ARTIST_FLAT_RATE, FOUNDING_CLIENT_MAX,
        STRIPE_FEES, DISCOUNT_MAX_PCT_OF_COMMISSION,
        WELCOME_DISCOUNT_CZK, REFERRAL_BONUS_CZK,
    )
except Exception:
    COMMISSION_TIERS = None  # fallback if module isn't importable


# ── Style ─────────────────────────────────────────────────────────────────
INK        = HexColor('#0a0a0a')
PAPER      = HexColor('#faf8f3')
INK_MUTED  = HexColor('#5a5a5a')
INK_FAINT  = HexColor('#9a9a9a')
RULE       = HexColor('#d4cfbf')
ACCENT     = HexColor('#9f1239')   # ink crimson for emphasis

styles = getSampleStyleSheet()

H1 = ParagraphStyle(
    'H1', parent=styles['Heading1'],
    fontName='Helvetica-Bold', fontSize=22, leading=26,
    textColor=INK, spaceBefore=10, spaceAfter=10,
)
H2 = ParagraphStyle(
    'H2', parent=styles['Heading2'],
    fontName='Helvetica-Bold', fontSize=14, leading=18,
    textColor=INK, spaceBefore=14, spaceAfter=6,
)
H3 = ParagraphStyle(
    'H3', parent=styles['Heading3'],
    fontName='Helvetica-Bold', fontSize=11, leading=14,
    textColor=INK, spaceBefore=10, spaceAfter=4,
)
Body = ParagraphStyle(
    'Body', parent=styles['BodyText'],
    fontName='Helvetica', fontSize=9.5, leading=13,
    textColor=INK, spaceAfter=5,
)
BodyMuted = ParagraphStyle(
    'BodyMuted', parent=Body, textColor=INK_MUTED,
)
Code = ParagraphStyle(
    'Code', parent=Body, fontName='Courier', fontSize=8.5, leading=11,
    leftIndent=10, textColor=HexColor('#0d2944'),
    backColor=HexColor('#f3eee1'),
    borderPadding=6, borderColor=RULE, borderWidth=0.5,
    spaceBefore=6, spaceAfter=6,
)
Eyebrow = ParagraphStyle(
    'Eyebrow', parent=Body, fontName='Helvetica-Bold', fontSize=8,
    leading=10, textColor=INK_FAINT, spaceAfter=4,
)


def hr():
    """Horizontal rule via thin table."""
    t = Table([['']], colWidths=[170 * mm], rowHeights=[0.4])
    t.setStyle(TableStyle([
        ('LINEABOVE', (0, 0), (-1, 0), 0.4, RULE),
    ]))
    return t


def header_footer(canvas, doc):
    """Brand header + page number footer."""
    canvas.saveState()
    # Top brand
    canvas.setFont('Helvetica-Bold', 9)
    canvas.setFillColor(INK)
    canvas.drawString(20 * mm, 285 * mm, 'inklink')
    canvas.setFont('Helvetica', 8)
    canvas.setFillColor(INK_FAINT)
    canvas.drawRightString(190 * mm, 285 * mm, 'Pricing & Discount Engine — Technical Documentation')
    # Bottom rule + page number
    canvas.setStrokeColor(RULE)
    canvas.setLineWidth(0.3)
    canvas.line(20 * mm, 18 * mm, 190 * mm, 18 * mm)
    canvas.setFont('Helvetica', 8)
    canvas.setFillColor(INK_FAINT)
    canvas.drawString(20 * mm, 12 * mm, f'Generated {datetime.now().strftime("%d. %m. %Y")}  ·  www.inklink.club')
    canvas.drawRightString(190 * mm, 12 * mm, f'Strana {doc.page}')
    canvas.restoreState()


def build(story):
    # ── COVER ──
    story.append(Spacer(1, 60 * mm))
    story.append(Paragraph('Pricing & Discount Engine', H1))
    story.append(Paragraph(
        '<font color="#5a5a5a">Tiered commission · service fees · founding programs · '
        'discount validation · Stripe integration · admin operations</font>', H3))
    story.append(Spacer(1, 18 * mm))
    story.append(hr())
    story.append(Spacer(1, 6 * mm))
    story.append(Paragraph(
        'Tato dokumentace popisuje pricing engine InkLink — co dělá, jak peníze tečou, '
        'a jak ho operuješ. Verze odpovídá kódu v <b>pricing/</b> module a feature flagu '
        '<b>USE_NEW_PRICING_ENGINE</b>. Vygenerováno z aktuálního config (pricing/config.py).', Body))
    story.append(PageBreak())

    # ── 1. BIG PICTURE ──
    story.append(Paragraph('1. Big picture — co dělá co', H2))
    story.append(Paragraph(
        'Pricing engine je pure-function modul mimo <b>server.py</b>, který za jeden volání '
        'vypočítá kompletní ekonomiku bookingu: kolik klient zaplatí, kolik dostane tatér, '
        'kolik nám zůstane, kolik si vezme Stripe, jaká sleva se aplikuje.', Body))
    story.append(Spacer(1, 4))
    story.append(Paragraph('Booking flow s engine zapnutým (USE_NEW_PRICING_ENGINE=1):', H3))
    story.append(Paragraph(
        '1. Klient otevře sketch a klikne Rezervovat<br/>'
        '2. Frontend volitelně volá <b>POST /api/discounts/preview</b> aby zobrazil náhled slevy<br/>'
        '3. Frontend volá <b>POST /api/bookings</b> s <b>discount_code</b><br/>'
        '4. Backend volá <b>pricing.calculate_booking_economics()</b> — pure function:<br/>'
        '&nbsp;&nbsp;&nbsp;• tier_for_price(gross) → 12 % / 8 % / 5 % podle ceny<br/>'
        '&nbsp;&nbsp;&nbsp;• commission_for_artist(gross, founding_status)<br/>'
        '&nbsp;&nbsp;&nbsp;• service_fee_for(gross, client_founding)<br/>'
        '&nbsp;&nbsp;&nbsp;• stripe_fee_for(client_pays_total)<br/>'
        '5. Backend uloží snapshot do <b>economics_snapshots</b> (immutable ledger)<br/>'
        '6. Backend zapíše do <b>discount_redemptions</b> (audit)<br/>'
        '7. Emit telemetry event <b>booking.economics_calculated</b><br/>'
        '8. Klient zaplatí přes Stripe → webhook potvrdí → status confirmed<br/>'
        '9. Po tetování → status completed → emit <b>booking.completed</b><br/>'
        '10. Refund/dispute → další snapshot s kind=&apos;refund&apos; nebo &apos;adjust&apos;', Body))
    story.append(PageBreak())

    # ── 2. ARCHITEKTURA ──
    story.append(Paragraph('2. Architektura kódu', H2))
    story.append(Paragraph(
        'Business rules žijí v <b>pricing/</b> module, ne v server.py. Změna rate '
        'znamená editaci jednoho souboru (pricing/config.py); server.py se nemění.', Body))
    arch = [
        ['Soubor', 'Co dělá'],
        ['pricing/config.py',     'Single source of truth pro rates (tiers, floor, fees, windows).'],
        ['pricing/economics.py',  'calculate_booking_economics() pure function. Decimal math.'],
        ['pricing/discounts.py',  'validate_discount() s error codes (DISCOUNT_TOO_LARGE…).'],
        ['pricing/telemetry.py',  'emit_event(name, payload, conn) → telemetry_events table.'],
        ['pricing/admin.py',      'admin_kpis() — agreguje economics_snapshots.'],
        ['tests/test_pricing.py', '41 unit testů. Tier boundaries, founding days, discount cap.'],
        ['server.py',             'Integration: volá engine, ukládá snapshots, webhooks, endpoints.'],
        ['public/admin.html',     'UI: KPI dashboard, promo CRUD, telemetry feed.'],
    ]
    t = Table(arch, colWidths=[52 * mm, 118 * mm])
    t.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), HexColor('#ede8db')),
        ('FONTNAME',   (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('FONTSIZE',   (0, 0), (-1, -1), 8.5),
        ('TEXTCOLOR',  (0, 0), (-1, 0), INK),
        ('TEXTCOLOR',  (0, 1), (-1, -1), INK_MUTED),
        ('LINEBELOW',  (0, 0), (-1, 0), 0.5, RULE),
        ('VALIGN',     (0, 0), (-1, -1), 'TOP'),
        ('LEFTPADDING',  (0, 0), (-1, -1), 8),
        ('RIGHTPADDING', (0, 0), (-1, -1), 8),
        ('TOPPADDING',   (0, 0), (-1, -1), 5),
        ('BOTTOMPADDING',(0, 0), (-1, -1), 5),
    ]))
    story.append(t)
    story.append(Spacer(1, 8))
    story.append(Paragraph(
        '<b>Klíčový princip:</b> pricing/ je dependency-free (jen Decimal/datetime z stdlib). '
        'Pure functions znamená že same input → same output, žádný side effect, ideální '
        'k unit testování. Idempotency: zavoláš dvakrát → stejný výsledek.', BodyMuted))
    story.append(PageBreak())

    # ── 3. MONEY FLOW ──
    story.append(Paragraph('3. Money flow — kdo komu kolik', H2))
    story.append(Paragraph(
        'Příklad: tetování za <b>5 000 Kč</b> od founding artista (day 35) pro non-founding klienta.', Body))
    flow = [
        ['Krok', 'Kč', 'Komu / kam'],
        ['Klient platí (gross + service fee)', '5 150', '→ Stripe'],
        ['Stripe fee (1.5 % + 6 Kč)',          '−76',   'cost'],
        ['Funds na platform balance',          '5 074', '↓'],
        ['Artist commission (5 % flat — founding flat window)', '−250', 'InkLink keeps'],
        ['Artist payout (gross − commission)', '4 750', '→ tatér (transfer)'],
        ['InkLink net (service_fee + commission − stripe_fee)', '324', 'naše net revenue'],
    ]
    t = Table(flow, colWidths=[80 * mm, 25 * mm, 65 * mm])
    t.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), HexColor('#ede8db')),
        ('FONTNAME',   (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('FONTSIZE',   (0, 0), (-1, -1), 8.5),
        ('TEXTCOLOR',  (0, 1), (-1, -1), INK),
        ('TEXTCOLOR',  (2, 1), (2, -1), INK_MUTED),
        ('ALIGN',      (1, 0), (1, -1), 'RIGHT'),
        ('FONTNAME',   (1, 1), (1, -1), 'Helvetica-Bold'),
        ('LINEBELOW',  (0, 0), (-1, 0), 0.5, RULE),
        ('LINEBELOW',  (0, 1), (-1, -2), 0.2, HexColor('#ece7d8')),
        ('VALIGN',     (0, 0), (-1, -1), 'MIDDLE'),
        ('LEFTPADDING',  (0, 0), (-1, -1), 8),
        ('RIGHTPADDING', (0, 0), (-1, -1), 8),
        ('TOPPADDING',   (0, 0), (-1, -1), 6),
        ('BOTTOMPADDING',(0, 0), (-1, -1), 6),
    ]))
    story.append(t)
    story.append(Spacer(1, 10))
    story.append(Paragraph('Inviolable rules (vynutí engine):', H3))
    story.append(Paragraph(
        '<b>1.</b> Artist payout = gross − commission. Discount <b>NIKDY</b> nezasáhne artistu.<br/>'
        '<b>2.</b> Discount ≤ 60 % naší komise.<br/>'
        '<b>3.</b> Net revenue nikdy záporné (kromě founding artist days 1–30 — explicit choice).', Body))
    story.append(PageBreak())

    # ── 4. RATES TABLE ──
    story.append(Paragraph('4. Aktuální rates (pricing/config.py)', H2))
    story.append(Paragraph('Tiered commission:', H3))
    tiers = [['Gross cena', 'Commission rate']]
    if COMMISSION_TIERS:
        for i, (thr, rate) in enumerate(COMMISSION_TIERS):
            next_thr = COMMISSION_TIERS[i + 1][0] if i + 1 < len(COMMISSION_TIERS) else None
            if next_thr:
                tiers.append([f'{int(thr)}–{int(next_thr)-1} Kč', f'{float(rate)*100:.0f} %'])
            else:
                tiers.append([f'{int(thr)}+ Kč', f'{float(rate)*100:.0f} %'])
    t = Table(tiers, colWidths=[90 * mm, 80 * mm])
    t.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), HexColor('#ede8db')),
        ('FONTNAME',   (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('FONTSIZE',   (0, 0), (-1, -1), 9),
        ('ALIGN',      (1, 0), (1, -1), 'CENTER'),
        ('LINEBELOW',  (0, 0), (-1, 0), 0.5, RULE),
        ('LEFTPADDING',  (0, 0), (-1, -1), 8),
        ('RIGHTPADDING', (0, 0), (-1, -1), 8),
        ('TOPPADDING',   (0, 0), (-1, -1), 6),
        ('BOTTOMPADDING',(0, 0), (-1, -1), 6),
    ]))
    story.append(t)
    story.append(Spacer(1, 6))
    story.append(Paragraph(
        f'<b>Minimum commission floor:</b> {int(MIN_COMMISSION_CZK)} Kč. Pokud tiered % '
        'dá méně, vezmeme floor. Founding artist days 1–30 floor neaplikuje (intentional).', Body))
    story.append(Spacer(1, 8))

    story.append(Paragraph('Service fee (od klienta):', H3))
    story.append(Paragraph(
        f'• Sazba: <b>{float(SERVICE_FEE_RATE)*100:.0f} %</b> z gross<br/>'
        f'• Cap: <b>{int(SERVICE_FEE_CAP_CZK)} Kč</b><br/>'
        f'• Founding client (prvních {FOUNDING_CLIENT_MAX} signups): <b>waived navždy</b>', Body))
    story.append(Spacer(1, 8))

    story.append(Paragraph('Founding artist program:', H3))
    story.append(Paragraph(
        f'• Day 1–{FOUNDING_ARTIST_FREE_DAYS}: <b>0 %</b> commission (i pod floor)<br/>'
        f'• Day {FOUNDING_ARTIST_FREE_DAYS+1}–{FOUNDING_ARTIST_FLAT_DAYS}: '
        f'<b>flat {float(FOUNDING_ARTIST_FLAT_RATE)*100:.0f} %</b> bez floor<br/>'
        f'• Day {FOUNDING_ARTIST_FLAT_DAYS+1}+: standard tiered<br/>'
        '• Clock start: první completed booking nastaví founding_artist_started_at', Body))
    story.append(Spacer(1, 8))

    story.append(Paragraph('Stripe fees:', H3))
    if STRIPE_FEES:
        sf = [['Card type', '%', 'Fix Kč']]
        for k in ('card_eea', 'card_non_eea'):
            v = STRIPE_FEES[k]
            sf.append([k, f'{float(v["percentage"])*100:.1f} %', f'{int(v["fixed_haler"])/100:.0f}'])
        t = Table(sf, colWidths=[60 * mm, 55 * mm, 55 * mm])
        t.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), HexColor('#ede8db')),
            ('FONTNAME',   (0, 0), (-1, 0), 'Helvetica-Bold'),
            ('FONTSIZE',   (0, 0), (-1, -1), 9),
            ('ALIGN',      (1, 0), (-1, -1), 'CENTER'),
            ('LINEBELOW',  (0, 0), (-1, 0), 0.5, RULE),
            ('LEFTPADDING', (0, 0), (-1, -1), 8),
            ('TOPPADDING', (0, 0), (-1, -1), 5),
            ('BOTTOMPADDING',(0, 0), (-1, -1), 5),
        ]))
        story.append(t)
        story.append(Spacer(1, 4))
        story.append(Paragraph(
            f'+ <b>{float(STRIPE_FEES["currency_conv"])*100:.0f} %</b> surcharge pokud karta není CZK.', BodyMuted))
    story.append(PageBreak())

    # ── 5. DISCOUNTS ──
    story.append(Paragraph('5. Discount engine', H2))
    story.append(Paragraph('Tři typy slev:', H3))
    dt = [
        ['Typ', 'Amount', 'Eligibility', 'Kde se konfiguruje'],
        ['WELCOME',      f'{int(WELCOME_DISCOUNT_CZK)} Kč',  'First booking only',     'Code path v engine'],
        ['REFERRAL',     f'{int(REFERRAL_BONUS_CZK)} Kč',    'Po referral linku',      'Code path + referrals table'],
        ['MANUAL_PROMO', 'Variabilní',                       'Admin issuance',         'discount_codes table'],
    ]
    t = Table(dt, colWidths=[35 * mm, 30 * mm, 50 * mm, 55 * mm])
    t.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), HexColor('#ede8db')),
        ('FONTNAME',   (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('FONTSIZE',   (0, 0), (-1, -1), 8.5),
        ('LINEBELOW',  (0, 0), (-1, 0), 0.5, RULE),
        ('LEFTPADDING',  (0, 0), (-1, -1), 6),
        ('RIGHTPADDING', (0, 0), (-1, -1), 6),
        ('TOPPADDING',   (0, 0), (-1, -1), 5),
        ('BOTTOMPADDING',(0, 0), (-1, -1), 5),
        ('VALIGN',     (0, 0), (-1, -1), 'TOP'),
    ]))
    story.append(t)
    story.append(Spacer(1, 8))

    story.append(Paragraph('Cap rule', H3))
    story.append(Paragraph(
        f'Discount ≤ <b>{float(DISCOUNT_MAX_PCT_OF_COMMISSION)*100:.0f} %</b> komise. Příklady:', Body))
    story.append(Paragraph(
        '• Booking 1 500 Kč → commission = 200 (floor). Max sleva = 120. <b>WELCOME 200 Kč REJECT.</b><br/>'
        '• Booking 5 000 Kč → commission = 400. Max sleva = 240. <b>WELCOME 200 Kč OK.</b>', Body))
    story.append(Spacer(1, 8))

    story.append(Paragraph('Error codes (stable, surface in admin alerts)', H3))
    story.append(Paragraph(
        '<b>DISCOUNT_TOO_LARGE</b> · <b>DISCOUNT_NOT_ELIGIBLE</b> · <b>DISCOUNT_ALREADY_USED</b> · '
        '<b>DISCOUNT_STACKING</b> · <b>NEGATIVE_PAYOUT</b> · <b>NEGATIVE_NET</b>', BodyMuted))
    story.append(PageBreak())

    # ── 6. STRIPE & WEBHOOKS ──
    story.append(Paragraph('6. Stripe integration', H2))
    story.append(Paragraph(
        '<b>Architektura:</b> destination charges (současné, nemigrovali jsme). Funds tečou '
        'rovnou tatérovi na jeho Connect account; platform si vezme application_fee_amount.', Body))
    story.append(Paragraph(
        'PaymentIntent.create() ukládá metadata.inklink_booking_id aby webhook našel booking.', Body))
    story.append(Spacer(1, 6))

    story.append(Paragraph('Webhook handlers (idempotent):', H3))
    wh = [
        ['Event', 'Akce'],
        ['payment_intent.succeeded', 'booking → confirmed, emit booking.payment_succeeded'],
        ['payment_intent.payment_failed', 'booking → payment_failed'],
        ['charge.refunded', 'new snapshot kind=refund, booking → refunded'],
        ['charge.dispute.created', 'booking → disputed (admin řeší ručně)'],
        ['account.updated', 'sync stripe_charges_enabled / payouts_enabled artisty'],
    ]
    t = Table(wh, colWidths=[60 * mm, 110 * mm])
    t.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), HexColor('#ede8db')),
        ('FONTNAME',   (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('FONTSIZE',   (0, 0), (-1, -1), 8.5),
        ('LINEBELOW',  (0, 0), (-1, 0), 0.5, RULE),
        ('LEFTPADDING',  (0, 0), (-1, -1), 8),
        ('RIGHTPADDING', (0, 0), (-1, -1), 8),
        ('TOPPADDING',   (0, 0), (-1, -1), 5),
        ('BOTTOMPADDING',(0, 0), (-1, -1), 5),
        ('VALIGN',     (0, 0), (-1, -1), 'TOP'),
    ]))
    story.append(t)
    story.append(Spacer(1, 8))

    story.append(Paragraph('Idempotency', H3))
    story.append(Paragraph(
        'Stripe retries webhooks aggressively. Před processing INSERT event_id do '
        '<b>processed_stripe_events</b>. Pokud UNIQUE fails → 200 OK + skip. Žádný '
        'duplicate accounting možný.', Body))
    story.append(PageBreak())

    # ── 7. TELEMETRY ──
    story.append(Paragraph('7. Telemetry & audit', H2))
    story.append(Paragraph(
        'Každá business událost se zapíše do tabulky <b>telemetry_events</b> jako JSON. '
        'Admin vidí v dashboardu (/admin → Telemetry events sekce).', Body))
    events = [
        ['Event name', 'Kdy se emituje'],
        ['booking.economics_calculated', 'Při PaymentIntent creation s full snapshotem'],
        ['booking.payment_succeeded',    'Webhook payment_intent.succeeded'],
        ['booking.payment_failed',       'Webhook payment_intent.payment_failed'],
        ['booking.completed',            'Po oboustranném potvrzení dokončení'],
        ['booking.refunded',             'Webhook charge.refunded'],
        ['booking.disputed',             'Webhook charge.dispute.created'],
        ['discount.applied',             'Sleva úspěšně aplikována na booking'],
        ['discount.rejected',            'Sleva neprošla validací (s error_code)'],
        ['founding_artist.clock_started','První completed booking → start 30/90 day window'],
        ['reconciliation.completed',     'Daily diff Stripe vs internal (>10 Kč = warning)'],
    ]
    t = Table(events, colWidths=[70 * mm, 100 * mm])
    t.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), HexColor('#ede8db')),
        ('FONTNAME',   (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('FONTSIZE',   (0, 0), (-1, -1), 8.5),
        ('LINEBELOW',  (0, 0), (-1, 0), 0.5, RULE),
        ('LEFTPADDING',  (0, 0), (-1, -1), 6),
        ('RIGHTPADDING', (0, 0), (-1, -1), 6),
        ('TOPPADDING',   (0, 0), (-1, -1), 4),
        ('BOTTOMPADDING',(0, 0), (-1, -1), 4),
        ('VALIGN',     (0, 0), (-1, -1), 'TOP'),
        ('FONTNAME',   (0, 1), (0, -1), 'Courier'),
        ('FONTSIZE',   (0, 1), (0, -1), 8),
    ]))
    story.append(t)
    story.append(PageBreak())

    # ── 8. ADMIN OPS ──
    story.append(Paragraph('8. Admin operations — co děláš ručně', H2))

    story.append(Paragraph('Zapnutí engine v produkci', H3))
    story.append(Paragraph('V Railway dashboardu nastav env proměnnou:', Body))
    story.append(Paragraph('USE_NEW_PRICING_ENGINE=1', Code))
    story.append(Paragraph(
        'Flask čte env per-request, žádný restart není potřeba. Existing bookingy '
        'beze změny — flag ovlivňuje jen nové.', BodyMuted))
    story.append(Spacer(1, 6))

    story.append(Paragraph('Vytvoření MANUAL_PROMO kódu', H3))
    story.append(Paragraph(
        'V /admin → sekce "Discount kódy" vyplň code (např. SPRING25), amount, '
        'volitelně max_uses a expirace. Klikni "+ Vytvořit". Klient pak při bookingu '
        'zadá kód, engine validuje cap a aplikuje.', Body))
    story.append(Spacer(1, 6))

    story.append(Paragraph('Enroll founding artist', H3))
    story.append(Paragraph('Najdi user id (např. v admin sekce) a v DB:', Body))
    story.append(Paragraph(
        'UPDATE users SET founding_artist=1 WHERE id IN (1,2,3,...);', Code))
    story.append(Paragraph(
        'founding_artist_started_at se nastaví automaticky při jejich prvním completed '
        'booking — žádný manuál nutný.', BodyMuted))
    story.append(Spacer(1, 6))

    story.append(Paragraph('Daily reconciliation cron', H3))
    story.append(Paragraph(
        'V Railway nastav env <b>RECONCILE_TOKEN=&lt;random&gt;</b> a cron schedule:', Body))
    story.append(Paragraph(
        '0 6 * * *  curl "https://www.inklink.club/api/cron/reconcile?token=$RECONCILE_TOKEN"', Code))
    story.append(Paragraph(
        'Porovná internal total vs Stripe balance. Diff > 10 Kč zaloguje warning v '
        'telemetry events. Pomáhá najít bugs DŘÍV než se nakumulují.', BodyMuted))
    story.append(Spacer(1, 6))

    story.append(Paragraph('KPI dashboard', H3))
    story.append(Paragraph(
        '/admin → sekce "Pricing engine — KPIs". Date range + tier filter. Default '
        'posledních 30 dní. Vidíš GMV, net revenue, take rate, discount cost, refund loss, '
        'Stripe cost, avg booking.', Body))
    story.append(PageBreak())

    # ── 9. ROLLBACK ──
    story.append(Paragraph('9. Co se NESTALO (záměrně) + rollback', H2))
    story.append(Paragraph('Záměrné non-goals (founder rozhodnutí):', H3))
    story.append(Paragraph(
        '• <b>Žádná migrace na separated charges</b> — funds dál tečou rovnou tatérovi přes '
        'destination charges. Real escrow s delayed transfers = budoucí migrace.<br/>'
        '• <b>Žádný 24h cooldown</b> před payout — destination charges to nemají.<br/>'
        '• <b>Žádný native push pro disputes</b> — admin emaily zatím chybí.', Body))
    story.append(Spacer(1, 6))

    story.append(Paragraph('Rollback v produkci', H3))
    story.append(Paragraph('Pokud něco bouchne, okamžitě:', Body))
    story.append(Paragraph('USE_NEW_PRICING_ENGINE=0', Code))
    story.append(Paragraph(
        'Engine se přestane volat, existing booking flow běží na flat 8 %. DB schema '
        '(snapshots, founding flags, discount tables) zůstává — neškodí.', BodyMuted))
    story.append(Spacer(1, 6))

    story.append(Paragraph('Fail-safe v kódu', H3))
    story.append(Paragraph(
        'POST /api/bookings má try/except kolem volání engine. Pokud engine throws, '
        'log warning <b>[pricing-engine] error, falling back to legacy</b> a booking se '
        'vytvoří se starým flat 8 %. Klient nic nepozná. Idempotency table chrání před '
        'duplicate processing při Stripe retry.', Body))
    story.append(Spacer(1, 6))

    story.append(Paragraph('Validation status', H3))
    story.append(Paragraph(
        '• <b>41 unit testů</b> všechny passují (tests/test_pricing.py)<br/>'
        '• Pure function determinism + idempotency verified<br/>'
        '• Tier boundaries, founding day transitions, discount cap edge cases covered', Body))

    # ── FIN ──
    story.append(Spacer(1, 40))
    story.append(hr())
    story.append(Spacer(1, 6))
    story.append(Paragraph(
        '<font color="#9a9a9a">© InkLink · '
        f'Generated from pricing/config.py on {datetime.now().strftime("%d. %m. %Y %H:%M")}'
        '</font>', BodyMuted))


def main():
    # Output path
    out_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'docs')
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, 'pricing_engine.pdf')

    doc = SimpleDocTemplate(
        out_path,
        pagesize=A4,
        leftMargin=20 * mm, rightMargin=20 * mm,
        topMargin=24 * mm,  bottomMargin=22 * mm,
        title='InkLink Pricing & Discount Engine',
        author='InkLink',
    )
    story = []
    build(story)
    doc.build(story, onFirstPage=header_footer, onLaterPages=header_footer)
    size = os.path.getsize(out_path) / 1024
    print(f'✓ Generated {out_path} ({size:.1f} KB)')


if __name__ == '__main__':
    main()
