"""Ingest a merchant's catalog.

    python manage.py ingest demo-store.test --source http://host.docker.internal:8090
    python manage.py ingest demo-store.test --source ... --limit 5 --dry-run
"""
from django.core.management.base import BaseCommand, CommandError

from modules.core.models import Merchant
from modules.twin.ingest import discover, fetch, normalise_source, product_from_html
from modules.twin.models import Product


def _clip(text: str, width: int) -> str:
    """Truncate with an ellipsis so long titles cannot break column alignment."""
    text = text or ""
    return text if len(text) <= width else text[: width - 1] + "\u2026"


class Command(BaseCommand):
    help = "Ingest a merchant's catalog from their product feed or sitemap + JSON-LD."

    COLS = (("conf", 4), ("cur", 3), ("price", 12), ("sku", 12), ("title", 38))

    def add_arguments(self, parser):
        parser.add_argument("merchant", help="Merchant domain, e.g. demo-store.test")
        parser.add_argument("--source", required=True, help="Store base URL")
        parser.add_argument("--limit", type=int, default=0, help="Cap pages fetched (0 = all)")
        parser.add_argument("--match", default="",
                            help="Only ingest URLs whose path contains this, e.g. /product")
        parser.add_argument("--dry-run", action="store_true", help="Parse and report, write nothing")

    def handle(self, *args, **o):
        try:
            tenant = Merchant.objects.get(domain=o["merchant"])
        except Merchant.DoesNotExist as exc:
            raise CommandError(
                f"No merchant {o['merchant']!r}. Merchants sign up through the app; "
                "this command only ingests into an existing one."
            ) from exc

        try:
            source = normalise_source(o["source"])
        except ValueError as exc:
            raise CommandError(str(exc)) from exc

        self.stdout.write(f"source:   {source}")
        if o["match"]:
            self.stdout.write(f"match:    paths containing {o['match']!r}")
        urls, rp = discover(source, limit=o["limit"], match=o["match"])
        self.stdout.write(f"robots:   {'honoured' if rp else 'absent, proceeding'}")
        self.stdout.write(f"sitemap:  {len(urls)} candidate pages")
        if not urls:
            raise CommandError(
                "No URLs discovered. Check --source, --match, and that a sitemap exists."
            )

        header = "  " + "  ".join(
            f"{name:>{w}}" if name in ("conf", "price") else f"{name:<{w}}"
            for name, w in self.COLS
        ) + "  url"
        self.stdout.write("")
        self.stdout.write(self.style.MIGRATE_HEADING(header))
        self.stdout.write("  " + "  ".join("-" * w for _, w in self.COLS) + "  " + "-" * 40)

        created = updated = skipped = canonical_differs = 0
        for url in urls:
            html = fetch(url)
            if not html:
                skipped += 1
                continue
            item = product_from_html(html, url)
            if not item:
                skipped += 1
                continue

            if o["dry_run"]:
                created += 1
            else:
                _, was_created = Product.objects.update_or_create(
                    tenant=tenant,
                    url=item.url,
                    defaults={
                        "sku": item.sku,
                        "title": item.title,
                        "price_paise": item.price_paise,
                        "currency": item.currency,
                        "in_stock": bool(item.in_stock),
                        "image_url": item.image_url,
                        "raw": item.raw,
                        "confidence": item.confidence,
                        "source": item.source,
                    },
                )
                created += was_created
                updated += not was_created

            if item.url != url:
                canonical_differs += 1

            self.stdout.write(
                f"  {item.confidence:>4.2f}"
                f"  {item.currency:<3}"
                f"  {item.price_paise / 100:>12,.2f}"
                f"  {_clip(item.sku or '-', 12):<12}"
                f"  {_clip(item.title, 38):<38}"
                f"  {item.url}"
            )

        self.stdout.write("")
        verb = "would write" if o["dry_run"] else "wrote"
        self.stdout.write(self.style.SUCCESS(
            f"{verb}: {created} new, {updated} updated, {skipped} pages without Product JSON-LD"
        ))
        if canonical_differs:
            self.stdout.write(self.style.WARNING(
                f"note: {canonical_differs} row(s) carry a canonical url differing from the page "
                f"fetched; that canonical is what is stored and re-verified"
            ))
        if not o["dry_run"]:
            self.stdout.write(f"catalog now holds {Product.objects.filter(tenant=tenant).count()} rows")
