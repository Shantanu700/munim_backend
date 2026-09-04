"""Issue a signed mandate to an agent.

    python manage.py issue_mandate demo-store.test --cap 5000 --total 20000 \
        --ceiling 50000 --ttl-hours 48 --subject agent-1

Stands in for the buyer authorising their agent, and is now a thin wrapper over
services.issue_mandate -- the buyer's own surface at /buyer/mandates/ reaches the
same function. One issuance path matters more here than for the other writes in
this codebase: the engine reads the caps from the verified claims and the
mandate's identity from `jti`, so a second claim builder would not fail, it would
mint mandates the gate refuses or honours with the wrong limits.
"""
from django.core.management.base import BaseCommand, CommandError

from modules.core.models import Buyer, Merchant
from modules.policy import services


def rupees_to_paise(value: str) -> int:
    """Kept as a re-export. services.rupees_to_paise is the canonical converter,
    and unlike the version that used to live here it rounds half-up rather than
    truncating -- `--cap 50.999` no longer becomes Rs 50.99 of authority."""
    try:
        return services.rupees_to_paise(value)
    except ValueError as exc:
        raise CommandError(str(exc)) from exc


class Command(BaseCommand):
    help = "Issue an Ed25519-signed spending mandate for one agent on one store."

    def add_arguments(self, parser):
        parser.add_argument("merchant", help="Merchant domain")
        parser.add_argument("--subject", default="agent-1", help="Which agent this authorises")
        parser.add_argument("--cap", required=True, help="Per-transaction cap, in rupees")
        parser.add_argument("--total", required=True, help="Total budget, in rupees")
        parser.add_argument("--ceiling", required=True,
                            help="Rupees above which not even a human may approve")
        parser.add_argument("--ttl-hours", type=int, default=services.DEFAULT_TTL_HOURS)
        parser.add_argument("--categories", default="",
                            help="Comma separated; omit for any category")
        parser.add_argument("--buyer-email", default="",
                            help="Attach the mandate to this buyer, so later "
                                 "decisions can be attributed to them")

    def handle(self, *args, **o):
        try:
            tenant = Merchant.objects.get(domain=o["merchant"])
        except Merchant.DoesNotExist as exc:
            raise CommandError(f"No merchant {o['merchant']!r}") from exc

        buyer = None
        if o["buyer_email"]:
            buyer, _ = Buyer.objects.get_or_create(email=o["buyer_email"])

        categories = [c.strip() for c in o["categories"].split(",") if c.strip()]

        try:
            mandate = services.issue_mandate(
                tenant,
                subject=o["subject"],
                per_txn_cap_paise=rupees_to_paise(o["cap"]),
                total_cap_paise=rupees_to_paise(o["total"]),
                step_up_ceiling_paise=rupees_to_paise(o["ceiling"]),
                allowed_categories=categories,
                ttl_hours=o["ttl_hours"],
                buyer=buyer,
                by_user=None,
                via="cli",
            )
        except ValueError as exc:
            raise CommandError(str(exc)) from exc

        self.stdout.write("")
        self.stdout.write(f"  merchant   {tenant.domain}")
        self.stdout.write(f"  agent      {mandate.subject}")
        self.stdout.write(f"  per txn    Rs {mandate.per_txn_cap_paise / 100:,.2f}")
        self.stdout.write(f"  total      Rs {mandate.total_cap_paise / 100:,.2f}")
        self.stdout.write(f"  ceiling    Rs {mandate.step_up_ceiling_paise / 100:,.2f}")
        self.stdout.write(f"  categories {', '.join(mandate.allowed_categories) or 'any'}")
        if buyer is not None:
            self.stdout.write(f"  buyer      {buyer.email}")
        self.stdout.write(f"  expires    {mandate.expires_at:%Y-%m-%d %H:%M %Z}")
        self.stdout.write("")
        self.stdout.write(self.style.MIGRATE_HEADING("mandate token (hand this to the agent):"))
        self.stdout.write(mandate.token)
        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS(f"issued {mandate.jti}"))
