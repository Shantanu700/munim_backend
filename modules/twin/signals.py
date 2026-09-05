from django.db.models.signals import post_save
from django.dispatch import receiver

from modules.twin.models import IngestJob, Product
from modules.twin.pubsub import publish_job_status, publish_tenant_event


@receiver(post_save, sender=Product)
def _on_product_saved(sender, instance: Product, created, **kwargs):
    if not created:
        return
    publish_tenant_event(instance.tenant_id, {
        "type": "product",
        "uuid": str(instance.uuid),
        "sku": instance.sku,
        "title": instance.title,
        "price_paise": instance.price_paise,
        "currency": instance.currency,
        "image_url": instance.image_url,
        "in_stock": instance.in_stock,
    })


PROGRESS_FIELDS = frozenset({"job_status", "total_urls", "processed"})


@receiver(post_save, sender=IngestJob)
def _on_ingest_job_saved(sender, instance: IngestJob, created, update_fields, **kwargs):
    if not update_fields or PROGRESS_FIELDS.isdisjoint(update_fields):
        return
    publish_job_status(instance)
