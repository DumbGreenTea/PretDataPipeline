"""
Command para procesar archivos Excel e importar datos.
"""
from django.core.management.base import BaseCommand
import pandas as pd
from ...etl.transformers.example_transformer import ExampleTransformer

class Command(BaseCommand):
    help = 'Importa datos desde un archivo Excel usando el transformer especificado'

    def add_arguments(self, parser):
        parser.add_argument('excel_file', type=str, help='Ruta al archivo Excel')
        parser.add_argument(
            '--sheet', 
            type=str, 
            help='Nombre de la hoja a procesar (opcional)',
            required=False
        )

    def handle(self, *args, **options):
        try:
            excel_file = options['excel_file']
            sheet_name = options.get('sheet')
            
            # Cargar Excel
            if sheet_name:
                df = pd.read_excel(excel_file, sheet_name=sheet_name)
            else:
                df = pd.read_excel(excel_file)
            
            # Inicializar transformer
            transformer = ExampleTransformer()
            
            # Procesar datos
            try:
                transformer.bulk_create_from_df(df)
                self.stdout.write(
                    self.style.SUCCESS(
                        f'Datos importados exitosamente desde {excel_file}'
                    )
                )
            except Exception as e:
                self.stdout.write(
                    self.style.ERROR(
                        f'Error al procesar datos: {str(e)}'
                    )
                )
                
        except Exception as e:
            self.stdout.write(
                self.style.ERROR(
                    f'Error al leer el archivo Excel: {str(e)}'
                )
            )