"""Backend de la plataforma Vigía (U-02): identidad, expediente y servicios transversales.

Módulos según ``tech-stack-decisions.md`` §7: ``identity``, ``ledger`` y ``shared``. Cada uno
sigue puertos y adaptadores; ``domain/`` nunca importa FastAPI, SQLAlchemy ni boto3.
"""
