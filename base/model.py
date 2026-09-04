import uuid6
from django.db import models

from base.defaults import DEFAULT_ON_DELETE
from base.enums import Status
from base.manager import DeleteStatusManager


class ModelBase(models.Model):
    BASE_MODEL_FIELDS = (
        "id",
        "status",
        "created_at",
        "updated_at",
    )

    uuid = models.UUIDField(default=uuid6.uuid6)
    status = models.IntegerField(default=Status.CREATED)
    created_at = models.DateTimeField(auto_now=True)
    updated_at = models.DateTimeField(auto_now_add=True)

    objects = DeleteStatusManager()
    unfiltered_objects = models.Manager()
    class Meta:
        abstract = True


class DropdownBase(ModelBase):
    r"""Base Dropdown Model: Contains `label`, `parent`, `max_level`, `config`"""

    label = models.CharField(max_length=200)
    parent = models.ForeignKey(
        "self",
        DEFAULT_ON_DELETE,
        null=True,
        related_name="children",
    )
    max_level = models.IntegerField(default=0)
    config = models.SmallIntegerField(default=0)

    class Meta(ModelBase.Meta):
        abstract = True
