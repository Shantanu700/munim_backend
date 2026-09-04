import hashlib
import json

from django.db import models, transaction

from modules.core.models import Merchant, TenantScoped

GENESIS = "0" * 64


def canonical(payload: dict) -> bytes:
    """Byte-stable rendering of a payload. Any drift here breaks every later hash."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()


class LedgerEntry(TenantScoped):
    """Append-only, hash-chained record of every money decision.

    Denials are entries too -- a refusal you cannot show a judge is not a
    control. Each entry commits to its predecessor, so editing any historical
    row invalidates every hash after it (see /audit/verify).
    """
    seq = models.BigIntegerField()
    action = models.CharField(max_length=64)
    decision = models.CharField(max_length=16)
    reason_code = models.CharField(max_length=64, blank=True)
    detail = models.JSONField(default=dict)
    prev_hash = models.CharField(max_length=64)
    chain_hash = models.CharField(max_length=64)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["tenant", "seq"], name="uniq_tenant_ledger_seq"),
        ]
        ordering = ["tenant_id", "seq"]
        indexes = [
            models.Index(fields=["tenant", "action", "created_at"],
                         name="idx_ledger_tenant_action_at"),
        ]

    def compute_hash(self) -> str:
        return hashlib.sha256(self.prev_hash.encode() + canonical({
            "seq": self.seq,
            "action": self.action,
            "decision": self.decision,
            "reason_code": self.reason_code,
            "detail": self.detail,
        })).hexdigest()

    @classmethod
    def append(cls, tenant, action, decision, reason_code="", detail=None) -> "LedgerEntry":
        with transaction.atomic():
            Merchant.objects.select_for_update().get(pk=tenant.pk)
            last = cls.objects.filter(tenant=tenant).order_by("-seq").first()
            entry = cls(
                tenant=tenant,
                seq=(last.seq + 1) if last else 1,
                action=action,
                decision=decision,
                reason_code=reason_code or "",
                detail=detail or {},
                prev_hash=last.chain_hash if last else GENESIS,
            )
            entry.chain_hash = entry.compute_hash()
            entry.save()
            return entry

    @classmethod
    def verify(cls, tenant) -> dict:
        """Recompute the whole chain. Returns the first break, if any."""
        prev = GENESIS
        count = 0
        for entry in cls.objects.filter(tenant=tenant).order_by("seq"):
            count += 1
            if entry.prev_hash != prev:
                return {"ok": False, "broken_at_seq": entry.seq, "error": "prev_hash mismatch",
                        "verified": count - 1}
            if entry.compute_hash() != entry.chain_hash:
                return {"ok": False, "broken_at_seq": entry.seq, "error": "chain_hash mismatch",
                        "verified": count - 1}
            prev = entry.chain_hash
        return {"ok": True, "verified": count, "head": prev}
