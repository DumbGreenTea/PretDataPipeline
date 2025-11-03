# Diseño de Base de Datos POD — Documento de Arquitectura (v1.2)

> Proyecto: PRET / VP Codelco — Data Platform del POD
> Autor: Diego Esteban Leiva Reyes
> Fecha: 2025-10-29

---

## 0) Resumen ejecutivo

Este documento describe el **modelo lógico** y las **decisiones de diseño** de la base de datos SQL que persistirá el POD diario y sus dominios (Asistencia, SSO, Plan Anterior, Plan del Día, PPC, Dotación/Maquinaria, Monografías, Compromisos, CNC, Layout). El objetivo es **estandarizar la carga**, **evitar duplicados**, **mantener trazabilidad histórica** y **servir dashboards** con consultas consistentes y performantes.

---

## 1) Principios y criterios de diseño

1. **Normalización ligera (hasta 3FN donde aporta)**: se separan maestros (p.ej., `trabajador`) de hechos diarios (p.ej., `asistencia`), con puentes donde corresponde (grupos del plan, etc.).
2. **Claves sustitutas + reglas de unicidad**: todas las tablas usan `IDENTITY` como PK; además se definen **claves de negocio** (`UNIQUE`) para **idempotencia** de carga (evitar insertar dos veces lo mismo si el Excel se reprocesa).
3. **Idempotencia del ETL**: cada dominio cuenta con su **par único** para UPSERT; el ETL puede ejecutarse n veces sin duplicar.
4. **Tipos y validaciones (respetando el Excel)**: se **mantienen los tipos definidos por el usuario**. Cuando un campo representa más de dos estados (p.ej., 0/1/2), se conserva como **`INT`** y se agrega `CHECK`.
5. **Longitudes prudentes**: tamaños suficientes en textos para realidad operativa; sin abusar de `VARCHAR` gigantes.
6. **Evolutividad**: columnas opcionales (`NULL`) donde el Excel es intermitente; catálogos para listas que cambian poco.
7. **Desacople del Excel**: no se modelan celdas, se modelan **conceptos**. Si cambia el layout de la planilla, se ajusta la **transformación**, no el modelo.
8. **Performance**: índices en FKs y filtros usuales (por `pod_id`, `fecha`). Posible **particionamiento temporal** a futuro si el volumen crece.

---

## 2) Modelo lógico (resumen)

### 2.1 Entidades núcleo

* **`pod`**: entidad central del día. Regla: `numero_pod` **NOT NULL + UNIQUE** (si en la operación real no fuese único, se cambiará a `UNIQUE (numero_pod, fecha, turno)`). Vínculos a jefes de turno vía `trabajador`.
* **`trabajador`**: **maestro global** (sin FK a `pod`). **UNIQUE (`codigo_trabajador`, `empresa`)** y opcional **UNIQUE por `LOWER(correo)`** si el correo es confiable.

### 2.2 Asistencia

* **Cabecera**: `asistencia(pod_id, trabajador_id)` con **`UNIQUE (pod_id, trabajador_id)`** (evita registrar dos veces al mismo trabajador en el mismo POD).
* **Detalle**: `asistencia_detalle` por fecha con **`UNIQUE (asistencia_id, fecha)`** y **`presente INT`** con `CHECK (presente IN (0,1,2))`
  *(0=Ausente, 1=Presente, 2=Reemplazo)*.

### 2.3 SSO

* `sso` asociado al `pod`.
* Cruz de seguridad y estado diario con **`CHECK (estado_dia IN (0,1,2))`** y **`UNIQUE (sso_cruz_seguridad_id, fecha)`** para no duplicar un día.

### 2.4 Planificación

* **Plan Anterior** (`plan_anterior`) con tablas hijas para horas, CNC y frentes.
* **Plan del Día** (`plan_dia`) conserva **`ruta_critica` como texto** (Si/No u otras variantes) según el Excel.
* **Grupos**: tablas puente `*_grupo` para múltiples grupos por plan, evitando multivalor.

### 2.5 Operación y control

* **Dotación/Maquinaria**: **`UNIQUE (pod_id, descripcion_equipo)`** para evitar duplicados del mismo equipo en un POD.
* **PPC**: `ppc_semanal` con `ppc_diario` (**`UNIQUE (ppc_id, fecha)`**).
* **Compromisos** y su resumen (posible vista a futuro para evitar desalineación).
* **CNC**: causa primaria/secundaria y códigos.
* **Layout**: rutas a artefactos (imágenes fuera de la BD).

---

## 3) Cambios vs. documento previo (lo más relevante)

1. **Se elimina `trabajador.pod_id`** para romper ciclo trabajador⇄pod; la relación trabajador↔pod se modela por **`asistencia`** y, para jefes, en `pod.jefe_turno_*_id`.
2. **`numero_pod` ahora es NOT NULL + UNIQUE** (o triple `(numero_pod, fecha, turno)` si el negocio lo requiere).
3. **`asistencia_detalle.presente` sigue siendo `INT`** (no se cambia a booleano) y se agrega `CHECK (0,1,2)` + `UNIQUE (asistencia_id, fecha)`.
4. **Unicidades y/o checks adicionales** sin alterar tipos: `sso_estado_dia (cruz, fecha)`, `contrato` por POD, `ppc_diario` por día, `dotacion_equipo` por POD+equipo.
5. **Índices en FKs** y columnas de filtrado frecuentes para mejorar rendimiento.

> **Nota**: En “Layout” se asume 1:N (varias imágenes/versiones por POD). Si se confirma 1:1, se cambia a `UNIQUE (pod_id)`.

---

## 4) Claves de negocio para UPSERT (idempotencia del ETL)

* `trabajador`: (`codigo_trabajador`, `empresa`) y/o `LOWER(correo)` si es confiable.
* `pod`: `numero_pod` (o `(numero_pod, fecha, turno)`).
* `asistencia`: `(pod_id, trabajador_id)`.
* `asistencia_detalle`: `(asistencia_id, fecha)`.
* `ppc_diario`: `(ppc_id, fecha)`.
* `dotacion_equipo`: `(pod_id, descripcion_equipo)`.

Estas claves permiten `ON CONFLICT (...) DO UPDATE` en Postgres sin duplicados.

---

## 5) Artefactos y ubicación

* **DDL completo** (creación de tablas, FKs, `UNIQUE`, `CHECK`, índices): `backend/sql/pret_schema.sql`
* **Semillas opcionales** (catálogos, e.g. `tipo_grupo`): `backend/sql/seed_tipo_grupo.sql`
* **Mantenimiento** (vistas, índices extra, utilitarios): `backend/sql/maint.sql`

---

## 6) Estrategia de ingestión (ETL) — alto nivel

1. **Extract**: lectura de Excel, tipificación y normalización de encabezados.
2. **Upsert maestros**: `trabajador` (clave de negocio) y cache de `trabajador_id`.
3. **Upsert `pod`** por clave; obtención de `pod_id`.
4. **Hechos**: `asistencia` → `asistencia_detalle`; SSO; planificación; PPC; dotación; etc., usando las **claves de negocio** para idempotencia.
5. **Transacciones por hoja/dominio** + log de rechazos.

**Orden sugerido**: `trabajador` → `pod` → `asistencia` → `asistencia_detalle` → `sso` → `plan_anterior` (+hijos) → `plan_dia` (+hijos) → `dotacion_equipo` (+`equipo_compromiso`) → `ppc_semanal` (+`ppc_diario`) → `compromiso` (+`compromiso_resumen`) → `registro_cnc` → `layout`.

---

## 7) Consultas típicas (ejemplos)

**PPC semanal por POD**

```sql
SELECT p.numero_pod, d.fecha, d.ppc_dia
FROM pret.ppc_semanal s
JOIN pret.ppc_diario  d ON d.ppc_id = s.ppc_id
JOIN pret.pod         p ON p.pod_id = s.pod_id
WHERE p.fecha BETWEEN :ini AND :fin;
```

**Dotación por día**

```sql
SELECT p.numero_pod, e.descripcion_equipo, e.operativos
FROM pret.dotacion_equipo e
JOIN pret.pod p ON p.pod_id = e.pod_id
WHERE p.fecha = :fecha;
```

---

## 8) Operación y Mantenimiento

* **Backups**: `pg_dump` diarios; retención 7–30 días.
* **`ANALYZE`** tras cargas grandes; **`VACUUM`** periódico.
* **Particionamiento** opcional por mes si crece el volumen.
* **Control de versiones** del DDL vía scripts/migraciones (Django/Alembic). Evitar cambios manuales en prod.

---

## 9) Checklist de calidad

* [ ] Todas las FKs con índices asociados.
* [ ] Claves de negocio definidas en tablas con UPSERT.
* [ ] `CHECK` aplicados donde hay multi-estado (`presente`, `estado_dia`).
* [ ] `ON DELETE CASCADE` solo cuando el negocio lo permite.
* [ ] Pruebas de **idempotencia** (reprocesar un mismo POD no duplica filas).

---

## 10) Variantes y notas

* Si **`numero_pod` no es único** a nivel global, cambiar a `UNIQUE (numero_pod, fecha, turno)`.
* `compromiso_resumen` puede reemplazarse por **VIEW** para evitar desalineación con `compromiso`.
* Si `ruta_critica` pasa a ser estrictamente binaria, considerar `BOOLEAN` en una futura versión.

---

**Estado**: *Aprobado para repositorio (v1.2)*.
Sugerencia de ubicación:

```
/docs/SQL/SQL_Design_and_Rationale.md        ← este documento
/backend/sql/pret_schema.sql                 ← DDL completo
/backend/sql/seed_tipo_grupo.sql             ← semillas (opcional)
/backend/sql/maint.sql                       ← vistas/índices extra (opcional)
```
