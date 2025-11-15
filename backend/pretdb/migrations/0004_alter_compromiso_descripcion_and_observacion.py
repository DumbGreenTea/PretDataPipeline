from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("pretdb", "0003_alter_asistenciadetalle_dia_nombre_and_more"),
    ]

    operations = [
        migrations.AlterField(
            model_name="compromiso",
            name="descripcion",
            field=models.TextField(blank=True, null=True),
        ),
        migrations.AlterField(
            model_name="compromiso",
            name="observacion",
            field=models.TextField(blank=True, null=True),
        ),
    ]
