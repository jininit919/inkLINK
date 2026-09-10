#!/usr/bin/env python3
"""Ověří, že APNs klíč opravdu funguje — bez telefonu.

`/__health` řekne jen to, že proměnné existují. Neřekne, jestli je klíč
platný. Nejčastější chyba je, že se při vkládání do Railway slepí PEM do
jednoho řádku; klíč pak vypadá nastavený a push jen tiše nechodí.

Tenhle skript klíč načte a podepíše jím token, což je přesně to, co dělá
odesílání. Když projde, chyba už může být jen na straně zařízení.

Použití (v Railway konzoli služby INKLINK):
    cd /app && /opt/venv/bin/python scripts/check_apns.py

Nic neodesílá a nic nemění.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main():
    import server

    print('APNs — kontrola nastavení\n')

    chybi = []
    for name, val in (('APNS_KEY_ID', server.APNS_KEY_ID),
                      ('APNS_TEAM_ID', server.APNS_TEAM_ID),
                      ('APNS_BUNDLE_ID', server.APNS_BUNDLE_ID)):
        if val:
            print(f'  {name:<16} {val}')
        else:
            print(f'  {name:<16} CHYBÍ')
            chybi.append(name)

    pem = server.APNS_KEY_PEM
    path = server.APNS_KEY_PATH
    if not pem and not path:
        print('  APNS_KEY_PEM     CHYBÍ')
        chybi.append('APNS_KEY_PEM')
    elif pem:
        radku = pem.count('\n') + 1
        print(f'  APNS_KEY_PEM     {len(pem)} znaků, {radku} řádků')
        if '-----BEGIN PRIVATE KEY-----' not in pem:
            print('     ✗ chybí úvodní řádek -----BEGIN PRIVATE KEY-----')
            chybi.append('APNS_KEY_PEM (začátek)')
        if '-----END PRIVATE KEY-----' not in pem:
            print('     ✗ chybí závěrečný řádek -----END PRIVATE KEY-----')
            chybi.append('APNS_KEY_PEM (konec)')
        if radku < 3:
            print('     ✗ klíč je na jednom řádku — Railway ho při vkládání slepil')
            chybi.append('APNS_KEY_PEM (zalomení)')

    print(f'\n  prostředí        {"sandbox" if server.APNS_USE_SANDBOX else "produkce"}'
          f'  (APNS_USE_SANDBOX={"1" if server.APNS_USE_SANDBOX else "0"})')

    if chybi:
        print('\n✗ Nastavení není kompletní: ' + ', '.join(chybi))
        return 1

    # Podepsání tokenu je to jediné, co klíč opravdu prověří.
    print('\nPodepisuju token klíčem…')
    try:
        import tempfile
        from apns2.credentials import TokenCredentials
        key_path = path
        if not key_path:
            tmp = tempfile.NamedTemporaryFile(mode='w', suffix='.p8', delete=False)
            tmp.write(pem)
            tmp.close()
            key_path = tmp.name
        creds = TokenCredentials(auth_key_path=key_path,
                                 auth_key_id=server.APNS_KEY_ID,
                                 team_id=server.APNS_TEAM_ID)
        token = creds.get_authorization_header(server.APNS_BUNDLE_ID)
        if not key_path == path:
            os.unlink(key_path)
    except Exception as e:
        print(f'\n✗ Klíč se nepodařilo použít: {type(e).__name__}: {e}')
        print('  Nejčastěji to znamená poškozený PEM — zkus ho do Railway')
        print('  vložit znovu a zkontroluj, že zůstala zalomení řádků.')
        return 1

    if not token:
        print('\n✗ Podpis vrátil prázdno.')
        return 1

    print('✓ Klíč je platný a podepisuje.\n')
    print('Co to znamená: server umí APNs požádat o doručení. Jestli')
    print('notifikace nedorazí, chyba už bude na straně zařízení —')
    print('nepovolené notifikace, nezaregistrovaný token, nebo nesoulad')
    print('sandbox/produkce (buildy z Xcode = sandbox, TestFlight = produkce).')
    return 0


if __name__ == '__main__':
    sys.exit(main())
