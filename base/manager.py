from django.db.models import Manager
from django.contrib.auth.models import UserManager as OGUserManager

from base.enums import Status



class DeleteStatusManager(Manager):
    def get_queryset(self):
        return super().get_queryset().exclude(status=Status.DELETED)


class UserManager(DeleteStatusManager, OGUserManager):
    r"""User Manager to provide `create_user` functionality to our custom User"""

    def create_user(self, username, email=None, password=None, **extra_fields):
        user = super().create_user(username, email, password, **extra_fields)
        user.set_password(password)
        user.save()
        return user
