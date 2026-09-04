from django.db.utils import IntegrityError
from django.utils import timezone
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import status
from rest_framework.response import Response

from agentmcp import checkout as checkout_core
from base.decorators import extend_response
from base.pagination import DefaultPagination
from base.permissions import APIAuthenticationPermission, IsMerchantPermission
from base.views import BaseAV
from modules.policy import analytics, services
from modules.policy.api.v1.serializers import (
    HistoryEntrySerializer,
    HistoryResponseSerializer,
    KillSwitchSerializer,
    MessageSerializer,
    OverviewSerializer,
    PolicyDetailSerializer,
    PolicySerializer,
    PolicyWriteSerializer,
    RuleCatalogEntrySerializer,
    RuleSerializer,
    RuleWriteSerializer,
    StepUpDecisionSerializer,
    StepUpOutcomeSerializer,
    StepUpSerializer,
    config_fields,
)
from modules.policy.models import StepUp, StorePolicy
from modules.policy.registry import REGISTRY, all_specs


NO_MERCHANT = "No merchant account is linked to this user."
NO_POLICY = "No such policy."
NO_RULE = "No such rule."


class _MerchantAV(BaseAV):
    """Resolve the caller's store, or answer 404.

    404 and not 403 for someone else's policy, matching the api-keys endpoint:
    a merchant should not be able to learn that another store's policy exists
    by watching which status code comes back.
    """

    authentication = True
    permission_classes = [APIAuthenticationPermission, IsMerchantPermission]

    def _tenant(self, request):
        tenant = request.user.get_merchant()
        if tenant is None:
            return None, Response({"msg": NO_MERCHANT}, status=status.HTTP_404_NOT_FOUND)
        return tenant, None

    def _policy(self, request, policy_uuid):
        tenant, error = self._tenant(request)
        if error is not None:
            return None, None, error
        policy = StorePolicy.objects.filter(merchant=tenant, uuid=policy_uuid) \
            .prefetch_related("rules").first()
        if policy is None:
            return None, None, Response({"msg": NO_POLICY},
                                        status=status.HTTP_404_NOT_FOUND)
        return tenant, policy, None


class PolicyOverviewAV(_MerchantAV):
    """The whole screen in one call.

    One request rather than five, because the tiles, the filter chips and the
    table all describe the same moment: assembled from separate calls, the
    screen can render a total that disagrees with the rows beneath it.
    """

    @extend_response(OverviewSerializer())
    def get(self, request):
        tenant, error = self._tenant(request)
        if error is not None:
            return error

        policies = list(StorePolicy.objects.filter(merchant=tenant).prefetch_related("rules"))
        active = next((p for p in policies if p.is_active), None)
        return {
            "agent_traffic_enabled": tenant.agent_traffic_enabled,
            "currency": tenant.currency,
            "active_policy": str(active.uuid) if active else None,
            "policies": PolicySerializer(policies, many=True).data,
            "tiles": analytics.tiles(tenant, active),
        }


class KillSwitchAV(_MerchantAV):
    """Agent traffic on or off, store-wide.

    Deliberately not a rule and not on a policy: it has to be reachable when
    the store has no active policy, which is exactly when a merchant most
    wants it. It overrides every rule rather than sitting among them.
    """

    @extend_schema(request=KillSwitchSerializer())
    @extend_response(KillSwitchSerializer())
    def put(self, request):
        tenant, error = self._tenant(request)
        if error is not None:
            return error

        body = KillSwitchSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        services.set_kill_switch(tenant, enabled=body.validated_data["agent_traffic_enabled"],
                                 by_user=request.user)
        return {"agent_traffic_enabled": tenant.agent_traffic_enabled}


class RuleCatalogAV(_MerchantAV):
    """The rule kinds this backend knows how to enforce.

    The editor renders its form from this rather than hardcoding a panel per
    kind, so a rule added to the registry needs no frontend release. The kinds
    are fixed here and not creatable: each one needs a checker and a place in
    the engine's sequence, neither of which a form can supply.

    Resolved against the caller's store even though the registry is global:
    the category rules' options are that store's own categories, so a catalogue
    served without a tenant would offer a picker with nothing in it.
    """

    @extend_response(RuleCatalogEntrySerializer(many=True))
    def get(self, request):
        tenant, error = self._tenant(request)
        if error is not None:
            return error
        return [{
            "kind": spec.kind,
            "label": spec.label,
            "description": spec.description,
            "reason_code": spec.reason_code,
            "default_on_breach": spec.default_on_breach,
            "default_config": spec.default_config,
            "config_fields": config_fields(spec.kind, tenant),
        } for spec in all_specs()]


class PolicyListAV(_MerchantAV):
    """The caller's policies, and new ones.

    Not paginated, unlike the catalogue: a merchant keeps a handful of these,
    and the screen shows all of them with counts.
    """

    @extend_response(PolicySerializer(many=True))
    def get(self, request):
        tenant, error = self._tenant(request)
        if error is not None:
            return error
        policies = StorePolicy.objects.filter(merchant=tenant).prefetch_related("rules")
        return PolicySerializer(policies, many=True).data

    @extend_schema(request=PolicyWriteSerializer())
    @extend_response(PolicyDetailSerializer())
    def post(self, request):
        tenant, error = self._tenant(request)
        if error is not None:
            return error

        body = PolicyWriteSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        name = body.validated_data["name"]
        try:
            policy = services.create_policy(tenant, name=name, by_user=request.user)
        except IntegrityError:
            return Response({"msg": f"A policy called {name!r} already exists."},
                            status=status.HTTP_400_BAD_REQUEST)
        return PolicyDetailSerializer(policy, context={"tenant": tenant}).data


class PolicyDetailAV(_MerchantAV):
    """One policy, its rules, and what each of them stopped."""

    @extend_schema(operation_id="get_policy")
    @extend_response(PolicyDetailSerializer())
    def get(self, request, policy_uuid):
        tenant, policy, error = self._policy(request, policy_uuid)
        if error is not None:
            return error

        if services.seed_rules(policy):
            policy = StorePolicy.objects.prefetch_related("rules").get(pk=policy.pk)

        return PolicyDetailSerializer(
            policy,
            context={"counts": analytics.rule_counts(tenant), "tenant": tenant},
        ).data

    @extend_schema(request=PolicyWriteSerializer())
    @extend_response(PolicySerializer())
    def put(self, request, policy_uuid):
        _, policy, error = self._policy(request, policy_uuid)
        if error is not None:
            return error

        body = PolicyWriteSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        name = body.validated_data["name"]
        try:
            services.rename(policy, name=name, by_user=request.user)
        except IntegrityError:
            return Response({"msg": f"A policy called {name!r} already exists."},
                            status=status.HTTP_400_BAD_REQUEST)
        return PolicySerializer(policy).data

    @extend_response(MessageSerializer())
    def delete(self, request, policy_uuid):
        _, policy, error = self._policy(request, policy_uuid)
        if error is not None:
            return error

        services.soft_delete(policy, by_user=request.user)
        return Response({"msg": "Deleted."}, status=status.HTTP_200_OK)


class PolicyActivateAV(_MerchantAV):
    """Put one policy in force, or leave the store with none in force."""

    @extend_schema(request=None)
    @extend_response(PolicySerializer())
    def post(self, request, policy_uuid):
        _, policy, error = self._policy(request, policy_uuid)
        if error is not None:
            return error
        services.activate(policy, by_user=request.user)
        return PolicySerializer(policy).data


class PolicyDeactivateAV(_MerchantAV):
    """Switch the active policy off without deleting it.

    A real state, not an absence of one: no active policy means no store rules
    are applied at all, which is what a merchant is choosing here.
    """

    @extend_schema(request=None)
    @extend_response(PolicySerializer())
    def post(self, request, policy_uuid):
        _, policy, error = self._policy(request, policy_uuid)
        if error is not None:
            return error
        services.deactivate(policy, by_user=request.user)
        return PolicySerializer(policy).data


class PolicyRuleAV(_MerchantAV):
    """Configure one rule: its parameters, whether it enforces, and what
    happens when it is breached.

    Addressed by `kind` rather than by uuid. The kinds are a fixed backend
    registry, so the slug is a stable address the client already knows from the
    catalogue -- and it is what lets a rule row be seeded on read without the
    client having to re-fetch to learn its id.
    """

    def _rule(self, request, policy_uuid, kind):
        tenant, policy, error = self._policy(request, policy_uuid)
        if error is not None:
            return None, None, error
        if kind not in REGISTRY:
            return None, None, Response({"msg": NO_RULE},
                                        status=status.HTTP_404_NOT_FOUND)
        services.seed_rules(policy)
        rule = policy.rules.filter(kind=kind).first()
        if rule is None:
            return None, None, Response({"msg": NO_RULE},
                                        status=status.HTTP_404_NOT_FOUND)
        return tenant, rule, None

    @extend_schema(operation_id="get_policy_rule")
    @extend_response(RuleSerializer(component_name="PolicyRuleDetail"))
    def get(self, request, policy_uuid, kind):
        tenant, rule, error = self._rule(request, policy_uuid, kind)
        if error is not None:
            return error
        return RuleSerializer(
            rule, context={"counts": analytics.rule_counts(tenant), "tenant": tenant},
        ).data

    @extend_schema(request=RuleWriteSerializer())
    @extend_response(RuleSerializer())
    def put(self, request, policy_uuid, kind):
        """Partial on purpose: flipping the toggle must not require the client
        to echo back the whole config to avoid clobbering it."""
        tenant, rule, error = self._rule(request, policy_uuid, kind)
        if error is not None:
            return error

        body = RuleWriteSerializer(data=request.data,
                                   context={"kind": rule.kind, "tenant": tenant})
        body.is_valid(raise_exception=True)
        services.update_rule(rule, by_user=request.user, **body.validated_data)
        return RuleSerializer(
            rule, context={"counts": analytics.rule_counts(tenant), "tenant": tenant},
        ).data


class PolicyHistoryAV(_MerchantAV):
    """Every switch and edit, newest first, with who made it and when.

    Read from the money chain rather than a separate table, so a change is as
    tamper-evident as the decisions around it -- and so the order of the two
    relative to each other is a fact rather than a guess from timestamps.
    """

    @extend_schema(parameters=[
        OpenApiParameter("page", OpenApiTypes.INT, description="1-based page number."),
        OpenApiParameter("page_size", OpenApiTypes.INT,
                         description=f"Rows per page, default {DefaultPagination.page_size}, "
                                     f"max {DefaultPagination.max_page_size}."),
    ])
    @extend_response(HistoryResponseSerializer())
    def get(self, request):
        tenant, error = self._tenant(request)
        if error is not None:
            return error

        paginator = DefaultPagination()
        page = paginator.paginate_queryset(services.history(tenant), request, view=self)
        rows = [{
            "seq": entry.seq,
            "at": entry.created_at,
            "reason_code": entry.reason_code,
            "actor_name": entry.detail.get("actor_name") or "",
            "actor_email": entry.detail.get("actor_email") or "",
            "detail": entry.detail,
        } for entry in page]
        return paginator.get_paginated_response(HistoryEntrySerializer(rows, many=True).data)


class StepUpAV(BaseAV):
    """The human approval surface. A control surface, not a product surface.

    The token in the URL is the capability -- there is no login.
    """

    authentication = False

    def _get_pending(self, token):
        return StepUp.objects.filter(token=token).select_related("tenant", "mandate").first()

    @extend_response(StepUpSerializer())
    def get(self, request, token):
        pending = self._get_pending(token)
        if pending is None:
            return Response({"msg": "No such approval request."}, status=status.HTTP_404_NOT_FOUND)

        if pending.status != StepUp.Status.APPROVED and pending.is_expired():
            return Response({"msg": "This request has expired."}, status=status.HTTP_410_GONE)

        return StepUpSerializer(pending).data

    @extend_schema(request=StepUpDecisionSerializer())
    @extend_response(StepUpOutcomeSerializer())
    def post(self, request, token):
        pending = self._get_pending(token)
        if pending is None:
            return Response({"msg": "No such approval request."}, status=status.HTTP_404_NOT_FOUND)

        if not pending.is_actionable:
            reason = "already decided" if pending.status != StepUp.Status.PENDING else "expired"
            return Response({"msg": f"Cannot approve: {reason}."}, status=status.HTTP_409_CONFLICT)

        decision_serializer = StepUpDecisionSerializer(data=request.data)
        decision_serializer.is_valid(raise_exception=True)

        if decision_serializer.validated_data["decision"] == "decline":
            pending.status = StepUp.Status.DECLINED
            pending.decided_at = timezone.now()
            pending.save(update_fields=["status", "decided_at"])
            return Response({"step_up": StepUpSerializer(pending).data}, status=status.HTTP_200_OK)

        outcome = checkout_core.quote_and_gate(
            pending.tenant,
            mandate_token=pending.mandate.token,
            url=pending.product_url,
            quantity=pending.quantity,
            quoted_price_paise=pending.quoted_price_paise,
            approved_reason=pending.reason_code,
            step_up=pending,
        )
        pending.refresh_from_db()
        return Response(
            {"step_up": StepUpSerializer(pending).data, "outcome": vars(outcome)},
            status=status.HTTP_200_OK,
        )
