from django.urls import path

from modules.ledger.api.v1.views import (
    AuditVerifyAV,
    LedgerEntryAV,
    LedgerExportAV,
    LedgerListAV,
)

urlpatterns = [
    path("verify", AuditVerifyAV.as_view()),
    path("entries", LedgerListAV.as_view()),
    path("entries/<int:seq>", LedgerEntryAV.as_view()),
    path("export", LedgerExportAV.as_view()),
]
