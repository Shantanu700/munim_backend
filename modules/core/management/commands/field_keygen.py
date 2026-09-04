"""Mint a Fernet key for the encrypted model fields.

    python manage.py field_keygen
    python manage.py field_keygen --rotate    # keep the old keys as readers

Prints one line to paste into .env. The key is a secret: it is never written to a
file, a compose config, or the ledger by this command.
"""
from cryptography.fernet import Fernet
from django.conf import settings
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Generate a Fernet field-encryption key and print the .env line."

    def add_arguments(self, parser):
        parser.add_argument(
            "--rotate", action="store_true",
            help="Prepend to the existing keys instead of replacing them. The new key "
                 "encrypts; the old ones stay able to decrypt rows not yet rewritten.",
        )

    def handle(self, *args, **o):
        entries = [Fernet.generate_key().decode()]
        if o["rotate"]:
            entries += list(getattr(settings, "FIELD_ENCRYPTION_KEYS", []))

        self.stdout.write("")
        self.stdout.write(self.style.MIGRATE_HEADING(
            "Paste into .env, then recreate the web container:"))
        self.stdout.write("")
        self.stdout.write(f"MUNIM_FIELD_ENCRYPTION_KEYS={','.join(entries)}")
        self.stdout.write("")
        if o["rotate"]:
            self.stdout.write(self.style.WARNING(
                "Rotation is not finished. Nothing is re-encrypted until every Merchant "
                "row is re-saved; drop the old key only after that."
            ))
        self.stdout.write(self.style.WARNING(
            "This key is a secret, and it is the only way to read the encrypted columns. "
            "Do not commit it and do not put it in a compose file. Lose it and every "
            "merchant re-enters their Razorpay credentials."
        ))
