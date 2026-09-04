from typing import Any

from drf_spectacular.utils import OpenApiExample, OpenApiResponse, extend_schema
from rest_framework.serializers import Serializer
from rest_framework.settings import api_settings

from base.constants import STATUS_MAPPING
from base.exceptions import CustomError, SerializerError
from base.permissions import APIAuthenticationPermission, IsMerchantPermission
from base.serializers import FileObjectSerializer
from base.types import (
    CustomErrorResponseType,
    SerializerErrorResponseType,
    UnAuthenticatedResponseType,
    UnAuthorizedResponseType,
)


def extend_response(type: Any | None = None):
    """
    Custom decorator to annotate BaseAPIView methods, in order to define the return type.
    Injects default status code with the return type.

    :param type: ReturnType of the method
    """

    def decorator(f):
        if callable(f):
            method = getattr(f, "__name__")
            default_response_code = STATUS_MAPPING.get(method)
            if default_response_code is None:
                return f

            return extend_schema(
                responses={
                    default_response_code: OpenApiResponse(
                        type, description="Indicates that operation was successful."
                    ),
                }
            )(f)
        else:
            return f

    if hasattr(type, "file_fields"):
        for field in getattr(type, "file_fields"):
            getattr(type, "_declared_fields").update(
                {field: FileObjectSerializer(required=False)}
            )

    return decorator


def extend_base_schema(cls, handler):
    """Extend the base schema, and add response types based on BaseAPIView's attributes.
    Its designed to respect all the responses dictionaries, i.e. extend_schema, which were called directly on the method.

    Args:
        handler: function that handles the request
    """
    method = getattr(handler, "__name__")

    extend_schema_params = {}

    responses = {
        CustomError.status_code: OpenApiResponse(
            CustomErrorResponseType,
            "Raised when there is a custom error, the message can be directly shown to the user.",
            [
                OpenApiExample(
                    "Example 1",
                    description="This is an example custom error, giving a custom error which has to be shown to user.",
                    value={"msg": "Cannot apply leave within this range!"},
                ),
            ],
        ),
        SerializerError.status_code: OpenApiResponse(
            SerializerErrorResponseType,
            "Raised when there is a serializer error during the validation of schemas",
            [
                OpenApiExample(
                    "Foreign Key doesn't exists",
                    description="This is an example serializer error, when the foreign key doesn't exists.",
                    value={"author": ['Invalid pk "999" - object does not exist.']},
                ),
            ],
        ),
    }

    authentication = getattr(cls, "authentication", True)

    if (
        authentication
        if isinstance(authentication, bool)
        else authentication.get(method, True)
    ):
        responses.update(
            {
                APIAuthenticationPermission.code: OpenApiResponse(
                    UnAuthenticatedResponseType,
                    "Raised when the user is not authenticated",
                ),
            }
        )
    else:
        extend_schema_params["auth"] = []

    if IsMerchantPermission in getattr(cls, "permission_classes", []):
        responses.update(
            {
                IsMerchantPermission.code: OpenApiResponse(
                    UnAuthorizedResponseType,
                    "Raised when the caller is authenticated but owns no store",
                ),
            }
        )

    BaseSchema = (
        getattr(handler, "schema", None)
        or getattr(handler, "kwargs", {}).get("schema", None)
        or api_settings.DEFAULT_SCHEMA_CLASS
    )

    class ExtendedSchema(BaseSchema): 
        def get_response_serializers(self):
            _responses = super().get_response_serializers() or {}
            _responses.update(responses)
            return _responses

    if not hasattr(handler, "kwargs"):
        handler.kwargs = {}
    handler.kwargs["schema"] = ExtendedSchema

    return extend_schema(**extend_schema_params)(handler)


def copy_serializer(name: str, Base: type[Serializer], **kwargs) -> Serializer:
    """
    A helper function to copy serializer with a new name.

    :param name: name of the
    :param Base: Base Serializer
    :param kwargs: optional kwargs for serializer initialization
    """
    serializer_class = type(name, (Base,), {})
    return serializer_class(**kwargs)
