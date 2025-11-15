from __future__ import annotations

from pathlib import Path
from typing import Iterable, List, Sequence, Tuple, Type

from django.core.management.base import BaseCommand, CommandError
from django.db.models import Model, QuerySet

from pretdb.models import (
    Pod,
    AsistenciaDetalle,
    SsoEstadoDia,
    PlanAnterior,
    PlanDia,
    DotacionEquipo,
    EquipoCompromiso,
    PpcDiario,
    PpcSemanal,
    Compromiso,
    RegistroCnc,
)


class Command(BaseCommand):
    help = "Imprime en formato tabla Markdown los datos asociados a un POD."

    def add_arguments(self, parser):
        parser.add_argument(
            "--pod",
            type=int,
            required=True,
            help="Número de POD a consultar (ej: 288)",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=10,
            help="Filas máximas por tabla (por defecto 10)",
        )
        parser.add_argument(
            "--output",
            type=str,
            help="Ruta de archivo .md/.txt para volcar el reporte completo",
        )

    def handle(self, *args, **options):
        numero_pod: int = options["pod"]
        limit: int = options["limit"]

        try:
            pod = Pod.objects.get(numero_pod=numero_pod)
        except Pod.DoesNotExist as exc:
            raise CommandError(f"No existe un POD con numero_pod={numero_pod}") from exc

        sections_output: List[str] = [f"# POD {pod.numero_pod} ({pod.fecha})"]

        sections_output.append(
            self._render_queryset(
                title="Portada / POD",
                model=Pod,
                qs=Pod.objects.filter(pk=pod.id),
                limit=1,
            )
        )

        sections_output.append(
            self._render_queryset(
                title="Asistencia (Detalle)",
                model=AsistenciaDetalle,
                qs=AsistenciaDetalle.objects.filter(asistencia__pod=pod).order_by("fecha"),
                limit=limit,
            )
        )

        sections_output.append(
            self._render_queryset(
                title="SSO - Cruz de Seguridad",
                model=SsoEstadoDia,
                qs=SsoEstadoDia.objects.filter(sso_cruz_seguridad__sso__pod=pod).order_by("fecha"),
                limit=limit,
            )
        )

        sections_output.append(
            self._render_queryset(
                title="Plan Anterior",
                model=PlanAnterior,
                qs=PlanAnterior.objects.filter(pod=pod).order_by("fecha"),
                limit=limit,
            )
        )

        sections_output.append(
            self._render_queryset(
                title="Plan del Día",
                model=PlanDia,
                qs=PlanDia.objects.filter(pod=pod).order_by("fecha"),
                limit=limit,
            )
        )

        sections_output.append(
            self._render_queryset(
                title="Dotación y Maquinaria",
                model=DotacionEquipo,
                qs=DotacionEquipo.objects.filter(pod=pod).order_by("descripcion_equipo"),
                limit=limit,
            )
        )

        sections_output.append(
            self._render_queryset(
                title="Compromisos de Equipo",
                model=EquipoCompromiso,
                qs=EquipoCompromiso.objects.filter(equipo__pod=pod).order_by("id"),
                limit=limit,
            )
        )

        sections_output.append(
            self._render_queryset(
                title="PPC Semanal",
                model=PpcSemanal,
                qs=PpcSemanal.objects.filter(pod=pod).order_by("fecha_inicio"),
                limit=limit,
            )
        )

        sections_output.append(
            self._render_queryset(
                title="PPC Diario",
                model=PpcDiario,
                qs=PpcDiario.objects.filter(ppc__pod=pod).order_by("fecha"),
                limit=limit,
            )
        )

        sections_output.append(
            self._render_queryset(
                title="Compromisos",
                model=Compromiso,
                qs=Compromiso.objects.filter(pod=pod).order_by("item"),
                limit=limit,
            )
        )

        sections_output.append(
            self._render_queryset(
                title="Matriz CNC",
                model=RegistroCnc,
                qs=RegistroCnc.objects.filter(pod=pod).order_by("id"),
                limit=limit,
            )
        )

        report_text = "\n\n".join(filter(None, sections_output))

        output_path = options.get("output")
        if output_path:
            path = Path(output_path)
            path.write_text(report_text, encoding="utf-8")
            self.stdout.write(self.style.SUCCESS(f"Reporte guardado en {path}"))
        else:
            self.stdout.write(report_text)

    def _render_queryset(
        self,
        title: str,
        model: Type[Model],
        qs: QuerySet,
        limit: int,
    ) -> str:
        qs_limited = list(qs[:limit])
        field_names = [f.name for f in model._meta.concrete_fields]
        rows: List[Sequence[object]] = [
            [getattr(instance, name) for name in field_names] for instance in qs_limited
        ]
        section_lines = [f"## {title}"]
        if not rows:
            section_lines.append("> (sin registros)")
            return "\n".join(section_lines)

        table = self._markdown_table(field_names, rows)
        section_lines.append(table)
        if qs.count() > limit:
            section_lines.append(f"> mostrando {limit} de {qs.count()} registros")
        return "\n".join(section_lines)

    def _markdown_table(
        self, headers: Sequence[str], rows: Sequence[Sequence[object]]
    ) -> str:
        headers_str = [str(h) for h in headers]
        rows_str: List[List[str]] = [
            [self._format_cell(cell) for cell in row] for row in rows
        ]
        widths = [
            max(len(headers_str[idx]), *(len(row[idx]) for row in rows_str))
            for idx in range(len(headers_str))
        ]

        def fmt_row(row: Sequence[str]) -> str:
            return "| " + " | ".join(
                row[idx].ljust(widths[idx]) for idx in range(len(widths))
            ) + " |"

        lines = [
            fmt_row(headers_str),
            "| " + " | ".join("-" * widths[idx] for idx in range(len(widths))) + " |",
        ]
        lines.extend(fmt_row(row) for row in rows_str)
        return "\n".join(lines)

    @staticmethod
    def _format_cell(value: object) -> str:
        if value is None:
            return ""
        return str(value)
