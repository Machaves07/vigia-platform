# Registro de cambios de `vigia-platform`

Una sección por etiqueta `vX.Y.Z` (NFR-NUC-50). La crea `release.yml` al terminar un release verde en `pilot`, y su cuerpo en GitHub lleva el digest de la imagen, la SBOM y el verificador. Cada entrada cita el issue de Linear (`VIG-n`) y la tarea del plan (`TASK-nnn`) cuando la hay. El formato sigue [Keep a Changelog](https://keepachangelog.com/es-ES/1.1.0/) y las versiones, [SemVer](https://semver.org/lang/es/).

Mientras U-02 se cierra sin cuenta de AWS (adenda A-47), no hay ninguna etiqueta: todo lo fusionado en `main` está en «Sin publicar». El primer release sale con el primer despliegue (TASK-154, VIG-98).

## [Sin publicar]

### Añadido

- **Base del repositorio**: backend en Python 3.12 con `uv`, módulos `identity`, `ledger` y `shared` con puertos y adaptadores, y `vigia-contracts` fijado por commit (VIG-18, TASK-101). Entorno local con `docker-compose.yml` (PostgreSQL 16, LocalStack y colector), `Makefile` y testcontainers (VIG-21, TASK-103).
- **Observabilidad**: registro JSON con redacción, métricas de nombre fijo y OpenTelemetry con exportación acotada (VIG-22). Emisores de las alarmas p95 de NFR-NUC-01 (VIG-136).
- **Datos y migraciones**:
  - Alembic solo hacia adelante con lint de migraciones (VIG-31);
  - esquema `identity` con seguridad a nivel de fila forzada (VIG-38);
  - `ledger` y `shared` particionados por mes con encadenado en la base (VIG-39);
  - particiones por adelantado y archivado de auditoría verificado (VIG-85, TASK-131).
- **Expediente**:
  - registro cerrado de tipos (VIG-40), sobre canónico (VIG-46);
  - `EscritorExpediente` y `audit_writer` (VIG-53), `LectorExpediente` (VIG-59);
  - cobertura y línea de tiempo (VIG-64), etiquetas (VIG-63);
  - puntos de control firmados (VIG-62) y motor de verificación de cadenas con `integrity_compromised` (VIG-69);
  - verificador `vigia_verify.py` (VIG-54).
- **Identidad**:
  - contraseñas Argon2id con consulta de filtradas (VIG-33), segundo factor y cifrado de sobre (VIG-66);
  - sesiones en servidor (VIG-70), autorización con alcance (VIG-73);
  - jerarquía, cuentas e invitaciones (VIG-75), concesiones del proveedor (VIG-76) y `provider_query` bajo concesión (VIG-132).
- **Compartido**:
  - `ScopeContext` y adaptador de PostgreSQL (VIG-26);
  - almacenamiento y verificación de evidencias (VIG-32, VIG-65);
  - firma Ed25519 por propósito con rotación (VIG-60);
  - bandeja de salida con publicación transaccional (VIG-47) y despacho con cola muerta y reproceso (VIG-77);
  - `vigia-worker` con planificador con arrendamiento (VIG-81).
- **API**:
  - fábrica, errores y salud (VIG-67) y aplicación de página única servida desde `vigia-api` (VIG-71);
  - cadena de middleware, CSP y límites de tasa (VIG-78);
  - rutas de sesión y `GET /me` (VIG-82), de usuarios, jerarquía y concesiones (VIG-83), y del expediente, integridad, auditoría y operación (VIG-86);
  - vista en vivo (VIG-80).
- **Orden administrativa** `vigia-admin`: `bootstrap`, raíz de `vigia-node-ca`, `create-organization`, `rotate-key`, `rotate-node-ca`, `replay-dead-letter`, `create-partitions`, `restore-audit-partition` y `record-restore-drill` (VIG-88, TASK-132).
- **Pruebas**:
  - aislamiento entre organizaciones por ruta (VIG-89);
  - escenarios de abuso N-1 a N-16 (VIG-93);
  - arnés de resiliencia FS-NUC-01 a 10 con el ensayo de restauración (VIG-90);
  - bancos de NFR-NUC-01 con regresión bloqueante y volumetría a escala objetivo (VIG-91).
- **Infraestructura (CDK)**:
  - aplicación con contextos (VIG-23);
  - pilas `vigia-foundation` (VIG-27), `vigia-data` (VIG-34), `vigia-edge` (VIG-41), `vigia-compute` (VIG-48) y `vigia-observability` (VIG-55);
  - traslado de `vigia-datasets` desde `vigia-contracts` sin recrear los depósitos (VIG-28).
- **Canalización**:
  - `ci.yml` con la imagen `arm64` construida y escaneada sin publicar (VIG-94);
  - `nightly.yml`, `release.yml` con `staging` efímero y aprobación del dueño, `rollback.yml`, `trust-store.yml` y `staging-sweeper.yml`, con los trabajos de AWS tras `VIGIA_AWS_ENABLED` (VIG-95).
- **Documentación de operación**: [runbooks](docs/runbooks/README.md) 6.1 a 6.6, contingencia de subparticionado, recuperación de una cadena rota y pruebas con la base en otra fecha; `README.md` de operación; este registro; `backend/tools/check_links.py` (VIG-97, TASK-152).

### Corregido

- Cancelación en cola determinista del pool de CPU (VIG-130) y prueba del servidor colgado en tiempo virtual (VIG-134).
- Alias rechazados en el registro de tipos por privacidad (P3) (VIG-129).
- Concesiones sin fecha de caducidad y auditoría de `GET /hierarchy` bajo concesión (VIG-135, adenda A-50).
