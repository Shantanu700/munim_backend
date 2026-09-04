from enum import IntEnum, StrEnum



class Status(IntEnum):
    CREATED = 0
    UPDATED = 1
    DELETED = 2

class RoleEnum(IntEnum):
    BUYER = 0
    MERCHANT = 1
