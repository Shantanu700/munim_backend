"""Mint an Ed25519 keypair for signing mandates.

    python manage.py mandate_keygen

Prints one line to paste into .env. The seed is a secret: it is never written to
a file, a compose config, or the ledger by this command.
"""
import base64

from django.conf import settings
from django.core.management.base import BaseCommand
from nacl.signing import SigningKey


class Command(BaseCommand):
    help = "Generate an Ed25519 mandate signing key and print the .env line."

    def add_arguments(self, parser):
        parser.add_argument("--kid", default="m1", help="Key id (default: m1)")
        parser.add_argument("--rotate", action="store_true",
                            help="Prepend to existing keys instead of replacing them")

    def handle(self, *args, **o):
        key = SigningKey.generate()
        seed = base64.b64encode(bytes(key)).decode()
        public = base64.b64encode(bytes(key.verify_key)).decode()

        entries = [f"{o['kid']}:{seed}"]
        if o["rotate"]:
            entries += [f"{kid}:{base64.b64encode(s).decode()}"
                        for kid, s in settings.MANDATE_KEYS.items() if kid != o["kid"]]

        self.stdout.write("")
        self.stdout.write(self.style.MIGRATE_HEADING("Paste into .env, then recreate the web container:"))
        self.stdout.write("")
        self.stdout.write(f"MUNIM_MANDATE_KEYS={','.join(entries)}")
        self.stdout.write("")
        self.stdout.write(f"public key (safe to publish, kid={o['kid']}): {public}")
        self.stdout.write(self.style.WARNING(
            "The seed above is a secret. Do not commit it and do not put it in a compose file."
        ))
