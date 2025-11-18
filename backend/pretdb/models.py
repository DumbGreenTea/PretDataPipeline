# pretdb/models.py
from django.db import models

# Create your models here.

# =============================================================================
# NOTA:
# Django crea automáticamente un campo 'id' (INT, PRIMARY KEY, AUTO_INCREMENT)
# para cada modelo. Por lo tanto, no necesitas definir 'trabajador_id',
# 'pod_id', etc., a menos que quieras un nombre diferente.
#
# Para tus campos 'INT GENERATED ALWAYS AS IDENTITY PRIMARY KEY',
# simplemente deja que Django use su 'id' automático.
#
# He traducido tus tablas al "estilo Django".
# =============================================================================

# -----------------------------------------------------------------------------
# TRABAJADOR
# -----------------------------------------------------------------------------
class Trabajador(models.Model):
    # trabajador_id se crea automáticamente como 'id'
    codigo_trabajador = models.CharField(max_length=20)
    nombre = models.CharField(max_length=30)
    cargo = models.CharField(max_length=20, blank=True, null=True)
    empresa = models.CharField(max_length=50, blank=True, null=True)
    correo = models.EmailField(max_length=30, blank=True, null=True)

    class Meta:
        # CONSTRAINT uq_trabajador_codigo_empresa UNIQUE (codigo_trabajador, empresa);
        constraints = [
            models.UniqueConstraint(fields=['codigo_trabajador', 'empresa'], name='uq_trabajador_codigo_empresa')
        ]
        # Opcional: indexar el correo si se usa mucho para búsquedas
        indexes = [
            models.Index(fields=['correo']),
        ]
        # Para que en el admin de Django no diga "Trabajadors"
        verbose_name = "Trabajador"
        verbose_name_plural = "Trabajadores"

    def __str__(self):
        return f"{self.nombre} ({self.codigo_trabajador})"


# -----------------------------------------------------------------------------
# POD y CONTRATO
# -----------------------------------------------------------------------------
class Pod(models.Model):
    # pod_id se crea automáticamente como 'id'
    numero_pod = models.IntegerField(unique=True) # uq_pod_numero
    fecha = models.DateField()
    turno = models.CharField(max_length=20, blank=True, null=True)
    
    # jefe_turno_codelco_id INT REFERENCES trabajador(trabajador_id)
    jefe_turno_codelco = models.ForeignKey(
        Trabajador,
        on_delete=models.SET_NULL, # Si se borra el trabajador, pone este campo a NULL
        related_name='pods_como_jefe_codelco',
        blank=True,
        null=True
    )
    
    # jefe_turno_contratista_id INT REFERENCES trabajador(trabajador_id)
    jefe_turno_contratista = models.ForeignKey(
        Trabajador,
        on_delete=models.SET_NULL,
        related_name='pods_como_jefe_contratista',
        blank=True,
        null=True
    )
    
    inicio_periodo = models.DateField(blank=True, null=True)
    fin_periodo = models.DateField(blank=True, null=True)

    class Meta:
        indexes = [
            models.Index(fields=['fecha']),
        ]
        verbose_name = "POD"
        verbose_name_plural = "PODs"

    def __str__(self):
        return f"POD N° {self.numero_pod} ({self.fecha})"


class Contrato(models.Model):
    # contrato_id se crea automáticamente como 'id'
    
    # pod_id INT NOT NULL REFERENCES pod(pod_id) ON DELETE CASCADE
    pod = models.ForeignKey(
        Pod,
        on_delete=models.CASCADE, # Si se borra el POD, se borra el contrato
        related_name='contratos'
    )
    
    nombre_contrato = models.CharField(max_length=120, blank=True, null=True)
    nombre_codigo = models.CharField(max_length=20, blank=True, null=True)

    class Meta:
        # CONSTRAINT uq_contrato_por_pod UNIQUE (pod_id, nombre_contrato, nombre_codigo);
        constraints = [
            models.UniqueConstraint(fields=['pod', 'nombre_contrato', 'nombre_codigo'], name='uq_contrato_por_pod')
        ]
        verbose_name = "Contrato"
        verbose_name_plural = "Contratos"

    def __str__(self):
        return self.nombre_contrato


# -----------------------------------------------------------------------------
# ASISTENCIA
# -----------------------------------------------------------------------------
class Asistencia(models.Model):
    # asistencia_id se crea automáticamente como 'id'
    
    # pod_id INT NOT NULL REFERENCES pod(pod_id) ON DELETE CASCADE
    pod = models.ForeignKey(
        Pod,
        on_delete=models.CASCADE,
        related_name='asistencias'
    )
    
    # trabajador_id INT NOT NULL REFERENCES trabajador(trabajador_id)
    trabajador = models.ForeignKey(
        Trabajador,
        on_delete=models.CASCADE, # O quizás models.PROTECT si no se debe borrar un trabajador con asistencia
        related_name='asistencias'
    )

    class Meta:
        # CONSTRAINT uq_asistencia_pod_trab UNIQUE (pod_id, trabajador_id)
        constraints = [
            models.UniqueConstraint(fields=['pod', 'trabajador'], name='uq_asistencia_pod_trab')
        ]
        verbose_name = "Asistencia"
        verbose_name_plural = "Asistencias"

    def __str__(self):
        return f"{self.trabajador} en {self.pod}"


class AsistenciaDetalle(models.Model):
    # detalle_id se crea automáticamente como 'id'
    
    # asistencia_id INT NOT NULL REFERENCES asistencia(asistencia_id) ON DELETE CASCADE
    asistencia = models.ForeignKey(
        Asistencia,
        on_delete=models.CASCADE,
        related_name='detalles'
    )
    
    fecha = models.DateField()
    dia_nombre = models.CharField(max_length=10, blank=True, null=True)

    # 0=Ausente, 1=Presente, 2=Reemplazo
    ESTADO_ASISTENCIA = [
        (0, 'Ausente'),
        (1, 'Presente'),
        (2, 'Reemplazo'),
    ]
    
    presente = models.IntegerField(
        choices=ESTADO_ASISTENCIA,
        blank=True,
        null=True
        # Tu CHK_asistencia_presente (presente IN (0,1,2)) se maneja con 'choices'
    )

    class Meta:
        # CONSTRAINT uq_asistencia_detalle UNIQUE (asistencia_id, fecha)
        constraints = [
            models.UniqueConstraint(fields=['asistencia', 'fecha'], name='uq_asistencia_detalle')
        ]
        indexes = [
            models.Index(fields=['fecha']),
        ]
        verbose_name = "Detalle de Asistencia"
        verbose_name_plural = "Detalles de Asistencia"


# -----------------------------------------------------------------------------
# SSO
# -----------------------------------------------------------------------------
class Sso(models.Model):
    # sso_id se crea automáticamente como 'id'
    # Asumo que un POD solo tiene un registro SSO, por eso uso OneToOneField
    pod = models.OneToOneField(
        Pod,
        on_delete=models.CASCADE,
        related_name='sso'
    )

    class Meta:
        verbose_name = "SSO"
        verbose_name_plural = "SSOs"

    def __str__(self):
        return f"SSO para {self.pod}"


class SsoCruzSeguridad(models.Model):
    # sso_cruz_seguridad_id se crea automáticamente como 'id'
    sso = models.ForeignKey(
        Sso,
        on_delete=models.CASCADE,
        related_name='cruces_seguridad'
    )
    fecha_inicio = models.DateField(blank=True, null=True)
    fecha_fin = models.DateField(blank=True, null=True)

    class Meta:
        verbose_name = "SSO Cruz de Seguridad"
        verbose_name_plural = "SSO Cruces de Seguridad"


class SsoEstadoDia(models.Model):
    # sso_estado_dia_id se crea automáticamente como 'id'
    sso_cruz_seguridad = models.ForeignKey(
        SsoCruzSeguridad,
        on_delete=models.CASCADE,
        related_name='estados_dia'
    )
    fecha = models.DateField()
    
    # 0=verde, 1=amarillo, 2=rojo
    ESTADO_DIA_CHOICES = [
        (0, 'Verde'),
        (1, 'Amarillo'),
        (2, 'Rojo'),
    ]
    estado_dia = models.IntegerField(choices=ESTADO_DIA_CHOICES, blank=True, null=True)

    class Meta:
        # CONSTRAINT uq_sso_estado_por_dia UNIQUE (sso_cruz_seguridad_id, fecha)
        constraints = [
            models.UniqueConstraint(fields=['sso_cruz_seguridad', 'fecha'], name='uq_sso_estado_por_dia')
        ]
        verbose_name = "SSO Estado Día"
        verbose_name_plural = "SSO Estados Días"


class SsoEvento(models.Model):
    # evento_id se crea automáticamente como 'id'
    sso = models.ForeignKey(
        Sso,
        on_delete=models.CASCADE,
        related_name='eventos'
    )
    fecha = models.DateField(blank=True, null=True)
    descripcion_dia = models.CharField(max_length=50, blank=True, null=True)
    calificacion = models.CharField(max_length=30, blank=True, null=True)
    fecha_compromiso = models.DateField(blank=True, null=True)
    
    # Asumiendo 0=Abierta, 1=Cerrada
    ESTADO_EVENTO_CHOICES = [
        (0, 'Abierta'),
        (1, 'Cerrada'),
    ]
    estado = models.IntegerField(choices=ESTADO_EVENTO_CHOICES, blank=True, null=True)

    class Meta:
        indexes = [models.Index(fields=['fecha'])]
        verbose_name = "SSO Evento"
        verbose_name_plural = "SSO Eventos"


class SsoReflexion(models.Model):
    # reflexion_id se crea automáticamente como 'id'
    sso = models.ForeignKey(
        Sso,
        on_delete=models.CASCADE,
        related_name='reflexiones'
    )
    fecha = models.DateField(blank=True, null=True)
    hallazgos = models.IntegerField(blank=True, null=True)
    tarjeta_verde = models.IntegerField(blank=True, null=True)
    traslado_policlinico = models.IntegerField(blank=True, null=True)

    class Meta:
        verbose_name = "SSO Reflexión"
        verbose_name_plural = "SSO Reflexiones"


# -----------------------------------------------------------------------------
# PLAN ANTERIOR
# -----------------------------------------------------------------------------
class PlanAnterior(models.Model):
    # plan_anterior_id se crea automáticamente como 'id'
    pod = models.ForeignKey(
        Pod,
        on_delete=models.CASCADE,
        related_name='planes_anteriores'
    )
    numero_pod = models.IntegerField(blank=True, null=True)
    tipo_plan = models.CharField(max_length=30, blank=True, null=True)
    fecha = models.DateField(blank=True, null=True)
    responsable = models.CharField(max_length=100, blank=True, null=True)
    id_p6 = models.CharField(max_length=50, blank=True, null=True)
    area_trabajo = models.CharField(max_length=30, blank=True, null=True)
    descripcion_item = models.CharField(max_length=50, blank=True, null=True)
    unidad = models.CharField(max_length=10, blank=True, null=True)
    cantidad_programada = models.DecimalField(max_digits=10, decimal_places=2, blank=True, null=True)
    cantidad_real = models.DecimalField(max_digits=10, decimal_places=2, blank=True, null=True)
    cumplimiento = models.DecimalField(max_digits=5, decimal_places=2, blank=True, null=True)
    
    # --- 💡 CAMBIO AÑADIDO AQUÍ 💡 ---
    # Para guardar si la actividad estaba en la sección "Realizadas" (True)
    # o "No Realizadas" (False).
    realizada = models.BooleanField(blank=True, null=True)
    # --- FIN DEL CAMBIO ---

    class Meta:
        indexes = [models.Index(fields=['fecha'])]
        verbose_name = "Plan Anterior"
        verbose_name_plural = "Planes Anteriores"


class PlanAnteriorHoras(models.Model):
    # horas_id se crea automáticamente como 'id'
    plan_anterior = models.ForeignKey(
        PlanAnterior,
        on_delete=models.CASCADE,
        related_name='horas'
    )
    cantidad_programada = models.DecimalField(max_digits=10, decimal_places=2, blank=True, null=True)
    cantidad_real = models.DecimalField(max_digits=10, decimal_places=2, blank=True, null=True)
    horas_hombre_programadas = models.DecimalField(max_digits=10, decimal_places=2, blank=True, null=True)
    horas_hombre_reales = models.DecimalField(max_digits=10, decimal_places=2, blank=True, null=True)
    porcentaje_avance = models.DecimalField(max_digits=5, decimal_places=2, blank=True, null=True)

    class Meta:
        verbose_name = "Plan Anterior Horas"
        verbose_name_plural = "Planes Anteriores Horas"


class PlanAnteriorCnc(models.Model):
    # cnc_id se crea automáticamente como 'id'
    plan_anterior = models.ForeignKey(
        PlanAnterior,
        on_delete=models.CASCADE,
        related_name='cnc'
    )
    causa = models.CharField(max_length=100, blank=True, null=True)
    subcausa = models.CharField(max_length=100, blank=True, null=True)
    tipo = models.CharField(max_length=30, blank=True, null=True)
    descripcion_cnc = models.CharField(max_length=200, blank=True, null=True)
    responsable = models.CharField(max_length=100, blank=True, null=True)

    class Meta:
        verbose_name = "Plan Anterior CNC"
        verbose_name_plural = "Planes Anteriores CNC"


class PlanAnteriorFront(models.Model):
    # front_id se crea automáticamente como 'id'
    plan_anterior = models.ForeignKey(
        PlanAnterior,
        on_delete=models.CASCADE,
        related_name='fronts'
    )
    frente = models.CharField(max_length=50, blank=True, null=True)
    hora_inicio_efectivo = models.TimeField(blank=True, null=True)
    comentario = models.CharField(max_length=200, blank=True, null=True)

    class Meta:
        verbose_name = "Plan Anterior Front"
        verbose_name_plural = "Planes Anteriores Fronts"


# --- Catálogo y grupos (Plan Anterior)
class TipoGrupo(models.Model):
    # tipo_grupo_id se crea automáticamente como 'id'
    nombre = models.CharField(max_length=60, unique=True)

    def __str__(self):
        return self.nombre


class GrupoActividadesAnterior(models.Model):
    # grupo_id se crea automáticamente como 'id'
    tipo_grupo = models.ForeignKey(
        TipoGrupo,
        on_delete=models.SET_NULL,
        blank=True,
        null=True
    )
    # Django recomienda tener un ManyToManyField en PlanAnterior
    # que apunte a este modelo, usando PlanAnteriorGrupo como 'through' table.
    # Pero para mantener la traducción 1-a-1 del SQL, definimos las tablas tal cual.
    planes_anteriores = models.ManyToManyField(
        PlanAnterior,
        through='PlanAnteriorGrupo',
        related_name='grupos_actividades'
    )

class PlanAnteriorGrupo(models.Model):
    # Esta es la tabla "through" (intermedia)
    plan_anterior = models.ForeignKey(PlanAnterior, on_delete=models.CASCADE)
    grupo = models.ForeignKey(GrupoActividadesAnterior, on_delete=models.CASCADE)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['plan_anterior', 'grupo'], name='uq_plan_anterior_grupo')
        ]


# -----------------------------------------------------------------------------
# PLAN DEL DÍA
# -----------------------------------------------------------------------------
class PlanDia(models.Model):
    # plan_dia_id se crea automáticamente como 'id'
    pod = models.ForeignKey(
        Pod,
        on_delete=models.CASCADE,
        related_name='planes_dia'
    )
    numero_pod = models.IntegerField(blank=True, null=True)
    tipo_plan = models.CharField(max_length=30, blank=True, null=True)
    fecha = models.DateField(blank=True, null=True)
    responsable = models.CharField(max_length=100, blank=True, null=True)
    id_p6 = models.CharField(max_length=50, blank=True, null=True)
    ruta_critica = models.CharField(max_length=100, blank=True, null=True)
    area_trabajo = models.CharField(max_length=100, blank=True, null=True)
    descripcion_item = models.CharField(max_length=150, blank=True, null=True)
    unidad = models.CharField(max_length=10, blank=True, null=True)
    cantidad_programada = models.DecimalField(max_digits=10, decimal_places=2, blank=True, null=True)
    cantidad_proyectada_dia = models.DecimalField(max_digits=10, decimal_places=2, blank=True, null=True)
    riesgos_criticos = models.CharField(max_length=200, blank=True, null=True)
    tipo_actividad = models.CharField(max_length=30, blank=True, null=True)

    class Meta:
        indexes = [models.Index(fields=['fecha'])]
        verbose_name = "Plan del Día"
        verbose_name_plural = "Planes del Día"


class PlanDiaRestricciones(models.Model):
    # restricciones_id se crea automáticamente como 'id'
    plan_dia = models.ForeignKey(
        PlanDia,
        on_delete=models.CASCADE,
        related_name='restricciones'
    )
    tipo = models.CharField(max_length=50, blank=True, null=True)
    descripcion = models.CharField(max_length=200, blank=True, null=True)

    class Meta:
        verbose_name = "Restricción Plan del Día"
        verbose_name_plural = "Restricciones Plan del Día"


# --- Grupos (Plan Día)
class GrupoActividadesDia(models.Model):
    # grupo_id se crea automáticamente como 'id'
    tipo_grupo = models.ForeignKey(
        TipoGrupo,
        on_delete=models.SET_NULL,
        blank=True,
        null=True
    )
    planes_dia = models.ManyToManyField(
        PlanDia,
        through='PlanDiaGrupo',
        related_name='grupos_actividades'
    )

class PlanDiaGrupo(models.Model):
    plan_dia = models.ForeignKey(PlanDia, on_delete=models.CASCADE)
    grupo = models.ForeignKey(GrupoActividadesDia, on_delete=models.CASCADE)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['plan_dia', 'grupo'], name='uq_plan_dia_grupo')
        ]


# -----------------------------------------------------------------------------
# MONOGRAFÍAS PEAD
# -----------------------------------------------------------------------------
class MonografiaPead(models.Model):
    # pead_id se crea automáticamente como 'id'
    pod = models.ForeignKey(
        Pod,
        on_delete=models.CASCADE,
        related_name='monografias_pead'
    )
    nombre_tramo = models.CharField(max_length=100, blank=True, null=True)
    dm_inicio = models.DecimalField(max_digits=10, decimal_places=2, blank=True, null=True)
    dm_fin = models.DecimalField(max_digits=10, decimal_places=2, blank=True, null=True)
    ruta_imagen = models.CharField(max_length=100, blank=True, null=True) # O models.ImageField si vas a subir archivos

    class Meta:
        verbose_name = "Monografía PEAD"
        verbose_name_plural = "Monografías PEAD"


class ActividadPead(models.Model):
    # actividad_id se crea automáticamente como 'id'
    pead = models.ForeignKey(
        MonografiaPead,
        on_delete=models.CASCADE,
        related_name='actividades'
    )
    nombre_actividad = models.CharField(max_length=100, blank=True, null=True)
    categoria = models.CharField(max_length=50, blank=True, null=True)
    unidad = models.CharField(max_length=10, blank=True, null=True)

    def __str__(self):
        return self.nombre_actividad


class MonografiaPeadActividad(models.Model):
    # grupo_pead_id se crea automáticamente como 'id'
    actividad = models.ForeignKey(
        ActividadPead,
        on_delete=models.CASCADE,
        related_name='grupos'
    )
    acumulado = models.DecimalField(max_digits=10, decimal_places=2, blank=True, null=True)
    estado = models.CharField(max_length=20, blank=True, null=True)

    class Meta:
        verbose_name = "Actividad de Monografía PEAD"
        verbose_name_plural = "Actividades de Monografía PEAD"


# -----------------------------------------------------------------------------
# MONOGRAFÍAS CRUCES
# -----------------------------------------------------------------------------
class MonografiaCruce(models.Model):
    # cruce_id se crea automáticamente como 'id'
    nombre_cruce = models.CharField(max_length=100, blank=True, null=True)
    dm = models.DecimalField(max_digits=10, decimal_places=2, blank=True, null=True)
    estado = models.CharField(max_length=50, blank=True, null=True)

    class Meta:
        verbose_name = "Monografía Cruce"
        verbose_name_plural = "Monografías Cruces"


class MonografiaCruceActividad(models.Model):
    # actividad_id se crea automáticamente como 'id'
    cruce = models.ForeignKey(
        MonografiaCruce,
        on_delete=models.CASCADE,
        related_name='actividades'
    )
    pod = models.ForeignKey(
        Pod,
        on_delete=models.SET_NULL, # ON DELETE SET NULL
        blank=True,
        null=True
    )
    tipo_actividad = models.CharField(max_length=50, blank=True, null=True)
    actividad = models.CharField(max_length=200, blank=True, null=True)
    unidad = models.CharField(max_length=10, blank=True, null=True)
    acumulado = models.DecimalField(max_digits=10, decimal_places=2, blank=True, null=True)
    ruta_imagen = models.CharField(max_length=255, blank=True, null=True) # O models.ImageField

    class Meta:
        verbose_name = "Actividad de Monografía Cruce"
        verbose_name_plural = "Actividades de Monografía Cruce"


# -----------------------------------------------------------------------------
# DOTACIÓN Y MAQUINARIA
# -----------------------------------------------------------------------------
class DotacionEquipo(models.Model):
    # equipo_id se crea automáticamente como 'id'
    pod = models.ForeignKey(
        Pod,
        on_delete=models.CASCADE,
        related_name='dotacion_equipos'
    )
    fecha = models.DateField(blank=True, null=True)
    descripcion_equipo = models.CharField(max_length=100, blank=True, null=True)
    proyectado_rev2 = models.IntegerField(blank=True, null=True)
    total_obra = models.IntegerField(blank=True, null=True)
    en_falla = models.IntegerField(blank=True, null=True)
    en_mantencion = models.IntegerField(blank=True, null=True)
    operadores_disponibles = models.IntegerField(blank=True, null=True)
    operativos = models.IntegerField(blank=True, null=True)
    reserva = models.IntegerField(blank=True, null=True)
    observacion = models.CharField(max_length=200, blank=True, null=True)

    class Meta:
        # CONSTRAINT uq_dotacion_equipo UNIQUE (pod_id, descripcion_equipo)
        constraints = [
            models.UniqueConstraint(fields=['pod', 'descripcion_equipo'], name='uq_dotacion_equipo')
        ]
        verbose_name = "Dotación Equipo"
        verbose_name_plural = "Dotaciones Equipos"


class EquipoCompromiso(models.Model):
    # compromiso_id se crea automáticamente como 'id'
    equipo = models.ForeignKey(
        DotacionEquipo,
        on_delete=models.CASCADE,
        related_name='compromisos'
    )
    descripcion_falla = models.CharField(max_length=100, blank=True, null=True)
    compromiso_equipo = models.CharField(max_length=200, blank=True, null=True)
    fecha_equipo = models.DateField(blank=True, null=True)
    compromiso_operador = models.CharField(max_length=100, blank=True, null=True)
    fecha_operador = models.DateField(blank=True, null=True)

    class Meta:
        verbose_name = "Compromiso de Equipo"
        verbose_name_plural = "Compromisos de Equipos"


# -----------------------------------------------------------------------------
# PPC
# -----------------------------------------------------------------------------
class PpcSemanal(models.Model):
    # ppc_id se crea automáticamente como 'id'
    pod = models.ForeignKey(
        Pod,
        on_delete=models.CASCADE,
        related_name='ppc_semanales'
    )
    fecha_inicio = models.DateField(blank=True, null=True)
    fecha_fin = models.DateField(blank=True, null=True)
    ppc_total = models.DecimalField(max_digits=5, decimal_places=2, blank=True, null=True)
    programadas_total = models.IntegerField(blank=True, null=True)
    realizadas_total = models.IntegerField(blank=True, null=True)

    class Meta:
        indexes = [
            models.Index(fields=['fecha_inicio', 'fecha_fin']),
        ]
        verbose_name = "PPC Semanal"
        verbose_name_plural = "PPCs Semanales"


class PpcDiario(models.Model):
    # ppc_dia_id se crea automáticamente como 'id'
    ppc = models.ForeignKey(
        PpcSemanal,
        on_delete=models.CASCADE,
        related_name='ppc_diarios'
    )
    fecha = models.DateField(blank=True, null=True)
    dia_nombre = models.CharField(max_length=10, blank=True, null=True)
    programadas = models.IntegerField(blank=True, null=True)
    realizadas = models.IntegerField(blank=True, null=True)
    ppc_dia = models.DecimalField(max_digits=5, decimal_places=2, blank=True, null=True)

    class Meta:
        # CONSTRAINT uq_ppc_diario UNIQUE (ppc_id, fecha)
        constraints = [
            models.UniqueConstraint(fields=['ppc', 'fecha'], name='uq_ppc_diario')
        ]
        verbose_name = "PPC Diario"
        verbose_name_plural = "PPCs Diarios"


# -----------------------------------------------------------------------------
# COMPROMISOS
# -----------------------------------------------------------------------------
class Compromiso(models.Model):
    # compromiso_id se crea automáticamente como 'id'
    pod = models.ForeignKey(
        Pod,
        on_delete=models.CASCADE,
        related_name='compromisos'
    )
    item = models.CharField(max_length=50, blank=True, null=True)
    descripcion = models.TextField(blank=True, null=True)
    fecha_toma_compromiso = models.DateField(blank=True, null=True)
    fecha_cierre_proyectada = models.DateField(blank=True, null=True)
    fecha_cierre_efectiva = models.DateField(blank=True, null=True)
    responsable = models.CharField(max_length=100, blank=True, null=True)
    status = models.CharField(max_length=20, blank=True, null=True)
    observacion = models.TextField(blank=True, null=True)

    class Meta:
        indexes = [
            models.Index(fields=['fecha_toma_compromiso', 'fecha_cierre_efectiva']),
        ]
        verbose_name = "Compromiso"
        verbose_name_plural = "Compromisos"


class CompromisoResumen(models.Model):
    # resumen_id se crea automáticamente como 'id'
    compromiso = models.ForeignKey(
        Compromiso,
        on_delete=models.CASCADE,
        related_name='resumenes'
    )
    empresa = models.CharField(max_length=50, blank=True, null=True)
    numero_compromiso = models.IntegerField(blank=True, null=True)
    abierto = models.IntegerField(blank=True, null=True)
    cerrado = models.IntegerField(blank=True, null=True)

    class Meta:
        verbose_name = "Resumen de Compromiso"
        verbose_name_plural = "Resúmenes de Compromisos"


# -----------------------------------------------------------------------------
# MATRIZ CNC
# -----------------------------------------------------------------------------
class MatrizCNC(models.Model):
    # registro_cnc_id se crea automáticamente como 'id'
    pod = models.ForeignKey(
        Pod,
        on_delete=models.CASCADE,
        related_name='registros_cnc'
    )
    gestion_atribuible = models.CharField(max_length=50, blank=True, null=True)
    causa_primaria_num = models.IntegerField(blank=True, null=True)
    causa_primaria = models.CharField(max_length=100, blank=True, null=True)
    causa_secundaria_codigo = models.CharField(max_length=20, blank=True, null=True)
    causa_secundaria = models.CharField(max_length=100, blank=True, null=True)

    class Meta:
        verbose_name = "Registro CNC"
        verbose_name_plural = "Registros CNC"


# -----------------------------------------------------------------------------
# LAYOUT
# -----------------------------------------------------------------------------
class Layout(models.Model):
    # layout_id se crea automáticamente como 'id'
    pod = models.ForeignKey(
        Pod,
        on_delete=models.CASCADE,
        related_name='layouts'
    )
    ruta_imagen = models.CharField(max_length=255, blank=True, null=True) # O models.ImageField

    class Meta:
        verbose_name = "Layout"
        verbose_name_plural = "Layouts"
