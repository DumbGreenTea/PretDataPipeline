# pretdb/management/commands/ingest_pod.py
from django.core.management.base import BaseCommand
from pretdb.etl.processors.orchestrator import run_file
from pretdb.etl.readers.excel_reader import ExcelReader

class Command(BaseCommand):
    help = "Ingesta un Excel POD: detecta hojas, despacha a transformers y persiste (si aplica)."

    def add_arguments(self, parser):
        parser.add_argument("file", help="Ruta al .xlsx")
        parser.add_argument("--only", help="Claves de hojas a procesar (coma), ej: asistencia,portada")
        parser.add_argument("--strategy", choices=["patterns", "canonical"], default="patterns")
        parser.add_argument("--dry-run", action="store_true", help="No escribir en DB si el transformer lo soporta")
        parser.add_argument("--list-keys", action="store_true", help="Solo listar claves detectadas y salir")

    def handle(self, *args, **opts):
        path = opts["file"]
        strategy = opts["strategy"]
        only = [s.strip() for s in opts["only"].split(",")] if opts.get("only") else None

        if opts["list_keys"]:
            r = ExcelReader(file_path=path)
            mapping = r.normalized_sheet_map(strategy=strategy)
            self.stdout.write(self.style.SUCCESS(f"Claves detectadas: {sorted(mapping.keys())}"))
            return

        summary = run_file(path, strategy=strategy, only=only, dry_run=opts["dry_run"])

        self.stdout.write(self.style.SUCCESS(f"Archivo: {summary['file']}"))
        self.stdout.write(f"Detectadas: {summary['sheets_detected']}")
        self.stdout.write(self.style.SUCCESS(f"Procesadas OK: {summary['sheets_processed']}"))

        for k, res in summary["results"].items():
            if res["status"] == "ok":
                self.stdout.write(f"  - {k}: {res['summary']}")
            else:
                self.stdout.write(self.style.ERROR(f"  - {k}: ERROR → {res['error']}"))
