from django.core import exceptions
from rest_framework import status
from rest_framework.exceptions import APIException
from rest_framework.response import Response
from rest_framework.views import set_rollback


def exception_handler(exc, context):
    if isinstance(exc, exceptions.ValidationError):
        data = {"msg": exc.error_list}
        return Response(data, status=status.HTTP_409_CONFLICT)

    if isinstance(exc, APIException):
        headers = {}
        if getattr(exc, "auth_header", None):
            headers["WWW-Authenticate"] = exc.auth_header 
        if getattr(exc, "wait", None):
            headers["Retry-After"] = "%d" % exc.wait 

        if isinstance(exc.detail, (list, dict)):
            data = exc.detail
        else:
            data = {"msg": exc.detail}

        set_rollback()
        return Response(data, status=exc.status_code, headers=headers)

    return None


class PermissionException(APIException):

    def __init__(self, detail=None, code=None):
        super().__init__(detail, code)
        self.status_code = code


class CustomError(APIException):
    "To be raised, when there is a custom error"
    status_code = status.HTTP_409_CONFLICT


class SerializerError(APIException):
    "To be raised, when there is a serializer error"
    status_code = status.HTTP_418_IM_A_TEAPOT
