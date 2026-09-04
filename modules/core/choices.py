from django.db import models


class UserRoleChoices(models.IntegerChoices):
    ADMIN = 1, "Admin"
    MERCHANT = 2, "Merchant"
    BUYER = 3, "Buyer"