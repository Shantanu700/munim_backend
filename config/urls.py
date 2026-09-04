"""
URL configuration for config project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/6.0/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""

from django.contrib import admin
from django.urls import path, include
from drf_spectacular.views import (
    SpectacularAPIView,
    SpectacularRedocView,
    SpectacularSwaggerView,
)

urlpatterns = [
    path("admin/", admin.site.urls),
    path(
        "api/schema/",
        SpectacularAPIView.as_view(),
        name="schema",
    ),
    path(
        "api/schema/swagger-ui/",
        SpectacularSwaggerView.as_view(url_name="schema"),
        name="swagger-ui",
    ),
    path(
        "api/schema/redoc/",
        SpectacularRedocView.as_view(url_name="schema"),
        name="redoc",
    ),
    path(
        "core/",
        include("modules.core.api.v1.urls"),
    ),
    path(
        "audit/",
        include("modules.ledger.api.v1.urls"),
    ),
    path(
        "webhooks/",
        include("modules.twin.api.v1.urls"),
    ),
    path(
        "ingest/",
        include("modules.twin.urls_ingest"),
    ),
    path(
        "policy/",
        include("modules.policy.api.v1.urls"),
    ),
    path(
        "buyer/",
        include("modules.policy.api.v1.buyer_urls"),
    ),
    path(
        "orders/",
        include("modules.twin.urls_orders"),
    ),
    path(
        "dashboard/",
        include("modules.twin.urls_dashboard"),
    ),
]
