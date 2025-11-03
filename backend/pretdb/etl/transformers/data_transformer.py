"""
Base Transformer para convertir datos de Excel a modelos Django.
"""
from typing import Dict, Any, List
import pandas as pd
from django.db import transaction
from django.core.exceptions import ValidationError

class DataTransformer:
    def __init__(self, model_class):
        self.model_class = model_class

    def transform_row(self, row: pd.Series) -> Dict[str, Any]:
        """
        Transforma una fila de DataFrame en un diccionario para crear modelo.
        Sobreescribir en clases hijas para implementar lógica específica.
        """
        raise NotImplementedError("Debes implementar transform_row en la clase hija")

    def validate_data(self, data: Dict[str, Any]) -> bool:
        """
        Valida los datos antes de crear el modelo.
        Sobreescribir en clases hijas para agregar validaciones específicas.
        """
        return True

    @transaction.atomic
    def bulk_create_from_df(self, df: pd.DataFrame, batch_size: int = 1000) -> List[Any]:
        """
        Crea objetos en la base de datos a partir de un DataFrame.
        Usa transacciones y bulk_create para mejor rendimiento.
        """
        objects_to_create = []
        errors = []

        for index, row in df.iterrows():
            try:
                data = self.transform_row(row)
                if self.validate_data(data):
                    obj = self.model_class(**data)
                    objects_to_create.append(obj)
            except Exception as e:
                errors.append(f"Error en fila {index}: {str(e)}")

            # Crear en lotes para optimizar
            if len(objects_to_create) >= batch_size:
                self.model_class.objects.bulk_create(objects_to_create)
                objects_to_create = []

        # Crear el último lote si queda algo
        if objects_to_create:
            self.model_class.objects.bulk_create(objects_to_create)

        if errors:
            raise ValidationError({"errors": errors})

        return objects_to_create