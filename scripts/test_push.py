#!/usr/bin/env python3
"""Pošle zkušební notifikaci na zařízení konkrétního uživatele.

Poslední článek, který se jinak ověřit nedá: že notifikace opravdu
dorazí. `check_apns.py` potvrdí, že klíč podepisuje; tenhle skript
zkusí doručení naostro.

Použití (v Railway konzoli služby INKLINK):
    cd /app && /opt/venv/bin/python scripts/test_push.py --user mgart
    cd /app && /opt/venv/bin/python scripts/test_push.py --user mgart --send

Bez `--send` jen vypíše, na kolik zařízení by to šlo a jakých.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--user', required=True, help='uživatelské jméno')
    ap.add_argument('--send', action='store_true', help='opravdu odeslat')
    ap.add_argument('--title', default='InkLink')
    ap.add_argument('--body', default='Zkušební notifikace — všechno funguje.')
    args = ap.parse_args()

    import server

    conn = server.get_db()
    u = conn.execute('SELECT id, username FROM users WHERE username=?',
                     (args.user,)).fetchone()
    if not u:
        print(f'Uživatel @{args.user} neexistuje.')
        conn.close()
        return 1

    subs = conn.execute(
        "SELECT id, endpoint, COALESCE(provider,'web') AS provider, platform "
        "FROM push_subscriptions WHERE user_id=?", (u['id'],)).fetchall()
    conn.close()

    if not subs:
        print(f'@{u["username"]} nemá zaregistrované žádné zařízení.')
        print('\nV aplikaci se musí zapnout notifikace — zvoneček v horní')
        print('liště, „Zapnout push notifikace". Teprve tím se token uloží.')
        return 1

    print(f'@{u["username"]} má {len(subs)} zařízení:\n')
    for s in subs:
        tail = (s['endpoint'] or '')[-10:]
        print(f'  {s["provider"]:<5} {s["platform"] or "—":<8} …{tail}')

    apns = [s for s in subs if s['provider'] == 'apns']
    print(f'\n  z toho iOS: {len(apns)}')
    print(f'  prostředí serveru: '
          f'{"sandbox" if server.APNS_USE_SANDBOX else "produkce"}')
    if apns and not server.APNS_USE_SANDBOX:
        print('  ⚠ Build z Xcode registruje token v sandboxu. Když je server')
        print('    v produkci, Apple ho odmítne jako neplatný a token se smaže.')

    if not args.send:
        print('\nNic se neodeslalo. Pro odeslání spusť znovu s --send.')
        return 0

    print('\nOdesílám…')
    server.send_push(u['id'], args.title, args.body, '/')
    print('Odesláno. Notifikace by měla dorazit během několika vteřin.')
    print('\nKdyž nedorazí, podívej se do logů služby na řádky [APNS] —')
    print('Apple v odpovědi říká důvod (BadDeviceToken, TopicDisallowed…).')
    return 0


if __name__ == '__main__':
    sys.exit(main())
