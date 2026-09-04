from typing import List, Union
from drf_spectacular.drainage import set_override
from rest_framework import serializers

from base.model import ModelBase
from base.constants import FIELDS_MAPPING
from base.functions import getattr_recursive
from base.types import CascaderType, DynamicKeysType



class BaseSerializer(serializers.ModelSerializer):

    file_fields: List[Union[str, CascaderType]] = []
    dynamic_keys: List[Union[str, DynamicKeysType]] = []

    def __init__(self, *args, **kwargs):
        fields = kwargs.pop("fields", None)
        exclude = kwargs.pop("exclude", None)
        component_name = kwargs.pop("component_name", None)
        component_description = kwargs.pop("component_description", None)

        if fields is not None:
            allowed = set(fields).union(
                set(fields).intersection(set(ModelBase.BASE_MODEL_FIELDS))
            )
            existing = set(self.fields.keys())

            difference = existing.difference(allowed)

            for each in difference:
                self.fields.pop(each)
        elif exclude is not None:
            trash = set(exclude).union(set(ModelBase.BASE_MODEL_FIELDS))
            for each in trash:
                self.fields.pop(each, None)
        else:
            pass

        super(BaseSerializer, self).__init__(*args, **kwargs)

        if component_name:
            set_override(self, "component_name", component_name)
        if component_description:
            set_override(self, "description", component_description)

        for field in self.dynamic_keys:
            isDict = isinstance(field, dict)
            field_type = (
                (
                    "int"
                    if field.get("source").split(".")[-1] == "id"
                    else field.get("type", "str")
                )
                if isDict
                else "str"
            )

            self.fields.update(
                {
                    field.get("name") if isDict else field.replace(".", "_"): (
                        getattr(
                            serializers, FIELDS_MAPPING.get(field_type, "CharField")
                        )
                    )(
                        source=field.get("source") if isDict else field,
                        read_only=True,
                    )
                }
            )

    def to_representation(self, instance):
        fields = super().to_representation(instance)
        for field in self.file_fields:
            isStr = isinstance(field, str)
            field_name = field if isStr else field.get("name")
            if field_name in fields.keys():
                fields[field_name] = (
                    {
                        "url": getattr(
                            getattr(
                                getattr_recursive(
                                    instance, field if isStr else field.get("field")
                                ),
                                "file",
                            ),
                            "url",
                        ),
                        "uuid": getattr(
                            getattr_recursive(
                                instance, field if isStr else field.get("field")
                            ),
                            "uuid",
                        ),
                    }
                    if getattr_recursive(
                        instance, field if isStr else field.get("field")
                    )
                    else None
                )
        return fields


class FileObjectSerializer(serializers.Serializer):
    url = serializers.CharField()
    uuid = serializers.CharField()
