# 🏗️ PRET DataPipeline (ETL) for Productivity

**Proyecto de Título – Ingeniería Civil Informática, UAI**  
Sistema ETL desarrollado para el área de Construcción y Montaje de **Codelco VP (Proyecto de Relaves Espesados Talabre, PRET)**.  
Su objetivo es **centralizar, estandarizar y dar trazabilidad a la información de los archivos POD**, permitiendo mejorar el análisis de productividad y la toma de decisiones.

---

## 🚀 Objetivos del Proyecto

- Automatizar la **extracción, transformación y carga (ETL)** de los archivos POD generados en terreno.
- Consolidar información histórica en una **base de datos relacional (PostgreSQL)**.
- Entregar datos limpios y actualizados para su explotación en **dashboards de productividad**.
- Garantizar la **trazabilidad y gobernanza de datos** a través de un pipeline reproducible y auditable.

---

## 🧩 Estructura del Proyecto

```bash
pret-platform/
├── backend/              # API + lógica del ETL (Django/ Python)
│   ├── app/
│   │   ├── main.py
│   │   ├── etl/          # Módulos de extracción, transformación y carga
│   │   ├── models/
│   │   ├── schemas/
│   │   └── utils/
│   └── requirements.txt
│
├── frontend/             # (Opcional) Dashboard web / interfaz visual (React/ Next Typescript)
│   └── package.json
│
├── docs/                 # Documentación técnica y académica
│   ├── arquitectura.md
│   ├── etl_pipeline.md
│   └── diagramas/
│
├── docker-compose.yml    # Orquestación de servicios
├── .env.example          # Variables de entorno de ejemplo
└── README.md
