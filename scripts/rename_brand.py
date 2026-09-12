#!/usr/bin/env python3
"""Přejmenuje značku v celém projektu.

Jméno je v kódu na stovkách míst a většina z nich je zobrazovaný text ve
statickém HTML. Centralizovat to nejde zdarma — stránky se servírují přes
send_from_directory, ne přes šablony, takže by to znamenalo přestavět
38 cest na render_template a přijít o statické cachování. Místo toho je
tady nástroj, který změnu udělá celou, ve všech tvarech a jedním krokem.

Co NEpřejmenovává, schválně:

  * vnitřní JS jmenné prostory (`window.InkLinkI18N`, `InkLinkBookings`…).
    Uživatel je nevidí, hodnotu to nepřinese a rozbít se to dá snadno —
    stačí jeden zapomenutý výskyt a stránka tiše nefunguje.
  * bundle ID nativní aplikace. To je jednorázové rozhodnutí, které se po
    prvním vydání na App Storu UŽ NIKDY nezmění, takže si ho zaslouží
    člověk udělat vědomě, ne skript mimochodem. Skript ho jen ohlásí.
  * doménu a e-mailové adresy. Ty závisí na tom, co si koupíš a kde máš
    schránku; skript vypíše, kde jsou, ať se na ně nezapomene.

Použití:
    ./venv/bin/python scripts/rename_brand.py --to Tatera
    ./venv/bin/python scripts/rename_brand.py --to Tatera --apply

Bez `--apply` se jen vypíše, co by se stalo. Po `--apply` pusť testy.
"""
import argparse
import os
import re
import sys

KOREN = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Soubory, kde jméno bydlí. Rozšiřuj vědomě — .db, .p8 a obrázky sem nepatří.
PRIPONY = ('.py', '.html', '.js', '.json', '.css', '.md', '.svg', '.txt')
VYNECHAT = ('venv', 'node_modules', '.git', 'uploads', 'ios', 'android',
            '__pycache__', '.claude')

# Vnitřní jména, na která se nesahá. Chytá `InkLinkI18N` i `InkLinkBookings`:
# velké písmeno hned za značkou znamená složený identifikátor v kódu.
VNITRNI = re.compile(r'InkLink(?=[A-Z])')

# Doména a mail se mění jinde než v kódu, ale ať je vidět, kde jsou.
DOMENA = re.compile(r'[\w.@-]*inklink\.club')


def tvary(stare, nove):
    """Značka se v kódu píše třemi způsoby a každý má svůj protějšek."""
    return (
        (stare,          nove),           # InkLink   → Tatera
        (stare.lower(),  nove.lower()),   # inklink   → tatera
        (stare.upper(),  nove.upper()),   # INKLINK   → TATERA
    )


def soubory():
    for koren, dirs, jmena in os.walk(KOREN):
        dirs[:] = [d for d in dirs if d not in VYNECHAT and not d.startswith('.')]
        for j in jmena:
            if j.endswith(PRIPONY):
                yield os.path.join(koren, j)


def zpracuj(cesta, stare, nove, zapsat):
    try:
        with open(cesta, encoding='utf-8') as fh:
            text = fh.read()
    except (UnicodeDecodeError, OSError):
        return 0, 0, []

    # Vnitřní jména se schovají za značku, kterou v textu nikdo nemá, aby
    # je náhrada minula. Po náhradě se vrátí zpátky.
    STRAZ = '\x00INTERNI\x00'
    text_bez = VNITRNI.sub(STRAZ, text)

    zmen = 0
    novy = text_bez
    for a, b in tvary(stare, nove):
        zmen += novy.count(a)
        novy = novy.replace(a, b)

    novy = novy.replace(STRAZ, 'InkLink')
    vnitrnich = text_bez.count(STRAZ)
    domeny = sorted(set(DOMENA.findall(text)))

    if zmen and zapsat:
        with open(cesta, 'w', encoding='utf-8') as fh:
            fh.write(novy)
    return zmen, vnitrnich, domeny


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--to', required=True, help='nové jméno, např. Tatera')
    ap.add_argument('--from', dest='stare', default='InkLink')
    ap.add_argument('--apply', action='store_true', help='opravdu zapsat')
    args = ap.parse_args()

    nove = args.to.strip()
    if not re.fullmatch(r'[A-Za-z][A-Za-z0-9]{1,24}', nove):
        print('Jméno musí být jedno slovo z písmen a číslic, začínat písmenem.')
        return 1
    if nove.lower() == args.stare.lower():
        print('Nové jméno je stejné jako staré.')
        return 1

    print(f'{"ZKOUŠKA (nic se nezapíše)" if not args.apply else "ZÁPIS"} — '
          f'{args.stare} → {nove}\n')

    celkem = vnitrnich = 0
    domeny = set()
    for cesta in sorted(soubory()):
        n, v, d = zpracuj(cesta, args.stare, nove, args.apply)
        celkem += n
        vnitrnich += v
        domeny.update(d)
        if n:
            print(f'  {n:>4}×  {os.path.relpath(cesta, KOREN)}')

    print(f'\n  přejmenováno:            {celkem}')
    print(f'  vnitřní jména ponechána: {vnitrnich}  (window.InkLink*, uživatel je nevidí)')

    if domeny:
        print('\n  ── ručně, skript na ně nesahá ────────────────────────────')
        print('  Doména a maily:')
        for d in sorted(domeny):
            print(f'    {d}')
        print('  Změnit v Railway (APP_BASE_URL, odesílatel v Resendu), v DNS')
        print('  a ve schránce. Staré adresy nechej chvíli přeposílat.')

    print('\n  ── bundle ID ─────────────────────────────────────────────')
    print('  native/capacitor.config.json a project.pbxproj drží')
    print('  `club.inklink.app`. Změň ho RUČNĚ a PŘED prvním vydáním na')
    print('  App Store — po vydání se bundle ID už nikdy nemění a musel by')
    print('  ses publikovat jako nová aplikace.')

    if not args.apply:
        print('\nNic se nezapsalo. Pro zápis spusť znovu s --apply.')
    else:
        print('\nHotovo. Teď pusť testy:')
        print('  ./venv/bin/python -m unittest tests.test_e2e -q')
    return 0


if __name__ == '__main__':
    sys.exit(main())
