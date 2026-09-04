from typing import Any, cast
from pydantic import BaseModel
from rest_framework import status
from rest_framework.views import APIView
from rest_framework.request import Request
from rest_framework.response import Response


from base.constants import STATUS_MAPPING
from base.decorators import extend_base_schema
from base.exceptions import CustomError, PermissionException
from base.permissions import APIAuthenticationPermission
from base.types import AuthenticationMethodTypes




class BaseAV(APIView):

    permission_classes = [APIAuthenticationPermission,]
    authentication: bool | AuthenticationMethodTypes = True

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        for method in cls.http_method_names:
            handler = getattr(cls, method, None)
            if handler is not None and callable(handler):
                handler = extend_base_schema(cls, handler)
                setattr(cls, method, handler)

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)

    def permission_denied(self, request, message=None, code=None):
        """
        If request is not permitted, determine what kind of exception to raise.
        Overriding default behavior because it was not customizable, always raising
        `rest_framework.exceptions.NotAuthenticated`, not respecting `message` or `code` (in case of `401`, `UNAUTHORIZED`).
        """
        raise PermissionException(detail=message, code=code)

    def finalize_response(self, request: Request, response, *args, **kwargs):
        if isinstance(response, (list, tuple, dict, str, BaseModel)):
            if isinstance(response, BaseModel):
                response = response.model_dump()
            default_response_code = (
                STATUS_MAPPING.get(cast(str, request.method).lower())
                or status.HTTP_200_OK
            )
            response = Response(response, status=default_response_code)
        return super().finalize_response(request, response, *args, **kwargs)

    def fail(self, message: str):
        "Raises a CustomError"
        raise CustomError(message)

    def success(self, message: str):
        "Return a successful response"
        return {"msg": message}

