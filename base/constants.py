from rest_framework import status


STATUS_MAPPING = {
    "get": status.HTTP_200_OK,
    "post": status.HTTP_201_CREATED,
    "put": status.HTTP_200_OK,
    "delete": status.HTTP_200_OK,
}


FIELDS_MAPPING = {
    "int": "IntegerField",
    "str": "CharField",
    "float": "FloatField",
    "bool": "BooleanField",
}
