"""
Endpoints simples para alimentar el dashboard del frontend.

Estas vistas devuelven datos agregados o mock en formato JSON. La idea es que
reutilices tus modelos existentes para construir los indicadores reales; aquí
dejamos funciones utilitarias que pueden evolucionar con facilidad.
"""
from __future__ import annotations

from datetime import date
from typing import List, Sequence

from django.db.models import Count, F
from django.http import JsonResponse
from django.views import View

from ..models import Pod, Trabajador


def _fallback_dates(limit: int) -> List[str]:
    today = date.today()
    return [today.replace(day=max(1, today.day - i)) for i in range(limit)]


def get_ruta_critica_data(limit: int = 6):
    """
    Retorna actividades recientes basadas en los POD más nuevos.

    Si no existen PODs, se devuelven actividades mock para que el frontend
    siempre reciba una estructura consistente.
    """
    pods = Pod.objects.order_by("-fecha")[:limit]
    data = []

    if pods:
        for idx, pod in enumerate(pods, start=1):
            plan = min(100, 60 + idx * 5)
            real = max(0, plan - (idx * 3))
            data.append(
                {
                    "actividad": f"POD {pod.numero_pod} - {pod.turno or 'Turno'}",
                    "avancePlan": plan,
                    "avanceReal": real,
                }
            )
    else:
        for idx in range(1, limit + 1):
            data.append(
                {
                    "actividad": f"Actividad #{idx}",
                    "avancePlan": 50 + idx * 5,
                    "avanceReal": 40 + idx * 4,
                }
            )
    return data


def get_disponibilidad_data(limit: int = 6):
    """
    Calcula una disponibilidad promedio simulada usando el número de PODs por
    trabajador como proxy. Si aún no hay datos, genera valores mock.
    """
    trabajadores = (
        Trabajador.objects.annotate(total_pods=Count("asistencias__pod", distinct=True))
        .order_by("-total_pods")[:limit]
    )

    data = []
    if trabajadores:
        for trabajador in trabajadores:
            disponibilidad = 50 + min(50, trabajador.total_pods * 5)
            data.append(
                {
                    "equipo": trabajador.nombre,
                    "disponibilidad": disponibilidad,
                }
            )
    else:
        for idx in range(1, limit + 1):
            data.append(
                {
                    "equipo": f"Equipo {idx}",
                    "disponibilidad": 55 + idx * 6,
                }
            )
    return data


def get_avance_plan_real(limit: int = 6):
    """
    Usa los POD (fecha) para generar una serie temporal plan vs real.
    """
    pods = Pod.objects.order_by("-fecha")[:limit]
    data = []
    if pods:
        for idx, pod in enumerate(sorted(pods, key=lambda p: p.fecha)):
            plan = 40 + idx * 8
            real = plan - 5 if idx % 2 else plan + 3
            data.append(
                {
                    "fecha": pod.fecha.isoformat(),
                    "plan": min(100, plan),
                    "real": max(0, min(100, real)),
                }
            )
    else:
        for idx, fecha in enumerate(_fallback_dates(limit)):
            plan = 45 + idx * 7
            real = plan - 4
            data.append(
                {
                    "fecha": fecha.isoformat(),
                    "plan": min(100, plan),
                    "real": max(0, real),
                }
            )
    return data


def get_tendencia_ppc(limit: int = 6):
    """
    Simula un indicador PPC semanal basado en el promedio de pods por trabajador.
    """
    pods_por_turno = (
        Pod.objects.exclude(turno__isnull=True)
        .values(semana=F("turno"))
        .annotate(total=Count("id"))
        .order_by("-total")[:limit]
    )
    data: List[dict] = []
    if pods_por_turno:
        for idx, item in enumerate(pods_por_turno, start=1):
            valor = 60 + min(35, item["total"] * 5)
            data.append({"semana": f"{item['semana']}", "valor": valor})
    else:
        for idx in range(1, limit + 1):
            data.append({"semana": f"Semana {idx}", "valor": 65 + idx * 3})
    return data


class RutaCriticaView(View):
    def get(self, request):
        return JsonResponse(get_ruta_critica_data(), safe=False)


class DisponibilidadView(View):
    def get(self, request):
        return JsonResponse(get_disponibilidad_data(), safe=False)


class AvancePlanRealView(View):
    def get(self, request):
        return JsonResponse(get_avance_plan_real(), safe=False)


class TendenciaPpcView(View):
    def get(self, request):
        return JsonResponse(get_tendencia_ppc(), safe=False)


class DashboardSummaryView(View):
    """
    Endpoint opcional para obtener todo de una sola vez.
    """

    def get(self, request):
        payload = {
            "rutaCritica": get_ruta_critica_data(),
            "disponibilidad": get_disponibilidad_data(),
            "avance": get_avance_plan_real(),
            "tendenciaPpc": get_tendencia_ppc(),
        }
        return JsonResponse(payload)
