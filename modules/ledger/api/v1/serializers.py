from rest_framework import serializers



class AuditVerifyResponseSerializer(serializers.Serializer):
    ok = serializers.BooleanField()
    merchants = serializers.DictField()


class LedgerEntrySerializer(serializers.Serializer):
    """One row of the audit table.

    Flat, and assembled by the view rather than read off the model: agent,
    buyer and amount live inside the entry's `detail` blob, and the screen
    should not have to know the shape of a JSON column to render a row.
    """

    seq = serializers.IntegerField()
    at = serializers.DateTimeField()
    action = serializers.CharField()
    decision = serializers.CharField()
    reason_code = serializers.CharField(allow_blank=True)
    reason_description = serializers.CharField(allow_blank=True)
    amount_paise = serializers.IntegerField(allow_null=True)
    agent = serializers.CharField(allow_blank=True)
    buyer_uuid = serializers.CharField(allow_blank=True)
    buyer_label = serializers.CharField(allow_blank=True)
    chain_hash = serializers.CharField()


class LedgerEntryDetailSerializer(LedgerEntrySerializer):
    """One entry opened: the row, its full detail blob, and where it sits in
    the chain. `prev_hash` is what makes the position checkable by hand."""

    prev_hash = serializers.CharField()
    detail = serializers.DictField()


class LedgerListResponseSerializer(serializers.Serializer):
    """Schema-only: the view builds the paginated envelope by hand."""

    count = serializers.IntegerField()
    next = serializers.CharField(allow_null=True)
    previous = serializers.CharField(allow_null=True)
    results = LedgerEntrySerializer(many=True)


class LedgerExportSerializer(serializers.Serializer):
    """The whole chain, in the form a third party can re-hash.

    Entries carry the raw stored columns rather than the table's flattened
    row: anyone recomputing the hashes needs exactly what was hashed.
    """

    merchant = serializers.CharField()
    exported_at = serializers.DateTimeField()
    verification = serializers.DictField()
    entries = serializers.ListField(child=serializers.DictField())
