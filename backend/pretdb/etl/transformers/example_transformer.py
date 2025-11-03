"""
Ejemplo de implementación del DataTransformer para un modelo específico.
"""
from typing import Dict, Any
import pandas as pd
from .data_transformer import DataTransformer
from core.models import ExampleModel  # Ajusta esto según tus modelos

class ExampleTransformer(DataTransformer):
    def __init__(self):
        super().__init__(ExampleModel)
    
    def transform_row(self, row: pd.Series) -> Dict[str, Any]:
        """
        Transforma una fila de Excel en datos para el modelo.
        Ajusta los nombres de columnas y transformaciones según tu Excel.
        """
        return {
            'nombre': row['Nombre'].strip(),
            'fecha': pd.to_datetime(row['Fecha']).date(),
            'valor': float(row['Valor']),
            'estado': row['Estado'].lower()
        }

    def validate_data(self, data: Dict[str, Any]) -> bool:
        """
        Implementa validaciones específicas para tus datos.
        """
        if not data['nombre']:
            return False
        if data['valor'] < 0:
            return False
        if data['estado'] not in ['activo', 'inactivo']:
            return False
        return True