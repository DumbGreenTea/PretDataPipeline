# pretdb/management/commands/run_etl.py
from django.core.management.base import BaseCommand
from pretdb.etl.processors.orchestrator import run_file
import logging

logger = logging.getLogger(__name__)

class Command(BaseCommand):
    help = 'Ejecuta el proceso ETL completo'
    
    def add_arguments(self, parser):
        parser.add_argument(
            '--file',
            type=str,
            required=True,
            help='Ruta al archivo Excel a procesar'
        )
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Ejecutar en modo prueba sin guardar'
        )
        parser.add_argument(
            '--sheets',
            type=str,
            help='Hojas específicas a procesar (separadas por coma)'
        )
        parser.add_argument(
            '--strategy',
            type=str,
            default='patterns',
            help='Estrategia de lectura de Excel (patterns, exact, fuzzy)'
        )
    
    def handle(self, *args, **options):
        file_path = options['file']
        dry_run = options['dry_run']
        sheets = options['sheets'].split(',') if options['sheets'] else None
        strategy = options['strategy']
        
        self.stdout.write(f"🚀 Iniciando ETL para: {file_path}")
        self.stdout.write(f"🎯 Estrategia: {strategy}")
        if dry_run:
            self.stdout.write("🔶 MODO PRUEBA - No se guardarán datos")
        
        # Usar el orchestrator original (NO MODIFICADO)
        results = run_file(
            file_path=file_path,
            strategy=strategy,
            only=sheets,
            dry_run=dry_run
        )
        
        # Mostrar resultados
        self.print_results(results)
    
    def print_results(self, results):
        self.stdout.write("\n" + "="*50)
        self.stdout.write("📊 RESULTADOS ETL")
        self.stdout.write("="*50)
        
        self.stdout.write(f"📁 Archivo: {results['file']}")
        self.stdout.write(f"📄 Hojas detectadas: {results['sheets_detected']}")
        self.stdout.write(f"✅ Hojas exitosas: {results['sheets_processed']}")
        self.stdout.write(f"❌ Hojas con error: {results['sheets_detected'] - results['sheets_processed']}")
        self.stdout.write(f"🔶 Dry Run: {results['dry_run']}")
        
        for sheet_key, sheet_result in results['results'].items():
            if sheet_key == "__file__":
                continue  # Saltar el resultado del archivo completo
                
            status = "✅" if sheet_result.get('success') else "❌"
            raw_name = sheet_result.get('raw_sheet_name', sheet_key)
            self.stdout.write(f"\n{status} Hoja: {sheet_key} (original: {raw_name})")
            
            if sheet_result.get('success'):
                loader_result = sheet_result.get('loader_result')
                summary = sheet_result.get('summary', '')
                
                # Mostrar el summary general
                if summary:
                    self.stdout.write(f"   ℹ️  {summary}")
                
                # ✅ MANEJAR DIFERENTES FORMATOS DE LOADER_RESULT
                if isinstance(loader_result, tuple):
                    # Formato tupla (asistencia) - (rows_upserted, warnings, errors)
                    rows_upserted, warnings, errors = loader_result
                    self.stdout.write(f"   📝 Registros procesados: {rows_upserted}")
                    
                    if errors:
                        self.stdout.write(f"   💥 Errores: {len(errors)}")
                        for key, error in list(errors.items())[:2]:  # Mostrar solo 2 errores
                            self.stdout.write(f"      ‣ {key}: {error}")
                    
                    if warnings:
                        self.stdout.write(f"   ⚠️  Advertencias: {len(warnings)}")
                        for key, warn in list(warnings.items())[:3]:  # Mostrar solo 3 warnings
                            if isinstance(warn, list):
                                for w in warn[:2]:  # Mostrar 2 elementos de lista
                                    self.stdout.write(f"      ‣ {key}: {w}")
                            else:
                                self.stdout.write(f"      ‣ {key}: {warn}")
                                
                elif isinstance(loader_result, dict):
                    # Formato diccionario (portada)
                    if loader_result.get('pod_id'):
                        action = "CREADO" if loader_result.get('created') else "ACTUALIZADO"
                        self.stdout.write(f"   📝 POD {action} - ID: {loader_result['pod_id']}")
                    
                    if loader_result.get('warnings'):
                        warnings_list = loader_result['warnings']
                        if isinstance(warnings_list, list):
                            self.stdout.write(f"   ⚠️  Advertencias: {len(warnings_list)}")
                            for warn in warnings_list[:3]:  # Mostrar solo 3 warnings
                                self.stdout.write(f"      ‣ {warn}")
                        else:
                            self.stdout.write(f"   ⚠️  {warnings_list}")
                    
                    # Mostrar estadísticas adicionales si existen
                    stats_to_show = ['created', 'updated', 'skipped', 'rows_upserted']
                    for stat in stats_to_show:
                        if loader_result.get(stat) is not None:
                            self.stdout.write(f"   📊 {stat}: {loader_result[stat]}")
                            
            else:
                # Hoja con error
                error = sheet_result.get('error', 'Error desconocido')
                self.stdout.write(f"   💥 Error: {error}")
        
        # Resumen final
        self.stdout.write("\n" + "="*50)
        if results.get('error'):
            self.stdout.write(f"❌ ERROR GENERAL: {results['error']}")
        else:
            self.stdout.write(f"🎉 PROCESO COMPLETADO: {results['sheets_processed']}/{results['sheets_detected']} hojas exitosas")