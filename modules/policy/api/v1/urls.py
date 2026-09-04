from django.urls import path

from modules.policy.api.v1.views import (
    KillSwitchAV,
    PolicyActivateAV,
    PolicyDeactivateAV,
    PolicyDetailAV,
    PolicyHistoryAV,
    PolicyListAV,
    PolicyOverviewAV,
    PolicyRuleAV,
    RuleCatalogAV,
    StepUpAV,
)

urlpatterns = [
    path("step-up/<uuid:token>/", StepUpAV.as_view()),
    path("overview/", PolicyOverviewAV.as_view()),
    path("kill-switch/", KillSwitchAV.as_view()),
    path("rule-catalog/", RuleCatalogAV.as_view()),
    path("policies/", PolicyListAV.as_view()),
    path("policies/<uuid:policy_uuid>/", PolicyDetailAV.as_view()),
    path("policies/<uuid:policy_uuid>/activate/", PolicyActivateAV.as_view()),
    path("policies/<uuid:policy_uuid>/deactivate/", PolicyDeactivateAV.as_view()),
    path("policies/<uuid:policy_uuid>/rules/<str:kind>/", PolicyRuleAV.as_view()),
    path("history/", PolicyHistoryAV.as_view()),
]
