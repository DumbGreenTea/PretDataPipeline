from django.urls import path

from .api import dashboard as dashboard_api

app_name = "pretdb"

urlpatterns = [
    path(
        "dashboard/",
        dashboard_api.DashboardSummaryView.as_view(),
        name="dashboard-summary",
    ),
    path(
        "dashboard/ruta-critica/",
        dashboard_api.RutaCriticaView.as_view(),
        name="dashboard-ruta-critica",
    ),
    path(
        "dashboard/disponibilidad/",
        dashboard_api.DisponibilidadView.as_view(),
        name="dashboard-disponibilidad",
    ),
    path(
        "dashboard/avance/",
        dashboard_api.AvancePlanRealView.as_view(),
        name="dashboard-avance",
    ),
    path(
        "dashboard/tendencia-ppc/",
        dashboard_api.TendenciaPpcView.as_view(),
        name="dashboard-tendencia-ppc",
    ),
]
