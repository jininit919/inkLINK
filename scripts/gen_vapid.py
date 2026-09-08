#!/usr/bin/env python3
"""Vygeneruje pár klíčů pro web push (VAPID).

Formát není libovolný a spletl se tu už jednou: PUSH_PUBLIC jde rovnou do
prohlížeče jako applicationServerKey, takže musí být base64url nekomprimo-
vaného bodu bez zarovnání. PUSH_PRIVATE čeká pywebpush jako base64url
ŘETĚZEC 32bajtové hodnoty — ne dekódované bajty.

    python3 scripts/gen_vapid.py
"""
import base64

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec


def main():
    b64 = lambda b: base64.urlsafe_b64encode(b).decode().rstrip('=')
    priv = ec.generate_private_key(ec.SECP256R1())
    pub = priv.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    print('PUSH_PUBLIC=' + b64(pub))
    print('PUSH_PRIVATE=' + b64(
        priv.private_numbers().private_value.to_bytes(32, 'big')))
    print()
    print('Obojí do Railway → služba INKLINK → Variables.')
    print('PUSH_PRIVATE nikam neposílej a ulož si ho do správce hesel.')


if __name__ == '__main__':
    main()
