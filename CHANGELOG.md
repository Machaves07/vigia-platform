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

### Gobernanza y flota (U-03)

- **Base**: raíz de composición de producción con un registro por unidad (`shared/runtime/`, A-52) (VIG-137, TASK-201); esquema `catalog` `gob_0017` con exclusión GiST (VIG-138, TASK-202) y esquema `fleet` `gob_0018` con particiones y retención (VIG-141, TASK-203); los 26 tipos de registro, los 15 eventos, las 21 enumeraciones y la política de texto libre (VIG-139, TASK-204); mamparos por clase de ruta (VIG-140, TASK-205).
- **Contrato del nodo**: adaptador `node_api` sobre el esqueleto de U-01, identidad del nodo y límites (VIG-144, TASK-206); alta del nodo y rotación de credenciales por las rutas del contrato (VIG-151, TASK-219).
- **Catálogo y comisionamiento**:
  - admisión de tres preguntas por planta (VIG-142, TASK-207), catálogo por zona versionado y firmado (VIG-145, TASK-208) y rutas de SCR-04 con marca de regresión (VIG-148, TASK-209);
  - documentos firmados por URL prefirmada (VIG-143, TASK-210), compuertas por zona, acta de alcance y política de planta (VIG-146, TASK-211), acuerdos de uso, firmantes y transparencia (VIG-149, TASK-212);
  - puertos de lectura del catálogo y las compuertas (VIG-153, TASK-213), sesión de walk-test con matriz, pasos y pases (VIG-150, TASK-214), prueba de oclusión (VIG-154, TASK-215), cierre del acta con sus guardas (VIG-158, TASK-216) y documento legible del acta con WeasyPrint (VIG-160, TASK-217).
- **Flota**:
  - declaración, asignación y reemplazo por `replaces_node_id` (VIG-147, TASK-218), lista de revocación global de `vigia-node-ca` (`regenerate_revocation_list`) (VIG-155, TASK-220);
  - ingesta de hallazgos, detecciones para revisión y eventos (VIG-156, TASK-221), concesiones de clip de un solo `PUT` y clip de verificación (VIG-152, TASK-222), latido y catálogo firmado para el nodo (VIG-157, TASK-223);
  - inventario y umbrales (VIG-159, TASK-224), alarmas de flota por transición y nodos mudos (VIG-161, TASK-225), versión objetivo y resultado de actualización (VIG-162, TASK-226), registro de las siete tareas periódicas y salud profunda (VIG-163, TASK-227).
- **Pruebas**: aislamiento de U-03 por ruta (VIG-165, TASK-228); escenarios de abuso y ejemplos G-1 a G-15 (VIG-168, TASK-229); suite de conformidad de U-01 contra las rutas reales de `node_api` (VIG-164, TASK-230); perfiles de carga `ci`, `nightly` y `soak` con el nodo simulado de U-01 (VIG-166, TASK-231); escenarios de resiliencia FS-GOB-01 a 10 (VIG-169, TASK-232); bancos, volumetría y cardinalidad de métricas (VIG-170, TASK-233).
- **Infraestructura**: pendientes aditivos nº 14 a 18 sobre las pilas de U-02, con las alarmas y el tablero de flota (VIG-167, TASK-235).
- **Canalización**: `ci.yml` ejecuta las suites de U-03 (aislamiento, abuso, ejemplos, propiedades PR-GOB-01 a 32 con perfil `ci` y el perfil `ci` de carga) y exige cobertura ≥ 90 % en el dominio y la aplicación de `catalog` y `fleet`; `nightly.yml` gana la carga `nightly` de 100 nodos, los bancos de NFR-GOB-01, 03, 04, 05, 09 y 10, la volumetría de U-03 y FS-GOB-01 a 10; `release.yml` gana el `soak` de 8 h y `deploy_checks.py` las comprobaciones nº 3, 4 y 10 a 13 (VIG-171, TASK-234).
- **Documentación de operación**: runbooks [U-03 6.1 a 6.4](docs/runbooks/README.md) (rotación forzada de la credencial de un nodo, regeneración y publicación de la lista de revocación, clips huérfanos y reapertura de walk-test) y sección de U-03 del `README.md` (VIG-172, TASK-236).

### Lazo, acreditación y notificaciones (U-04)

- **Base**: esquemas `loop` (VIG-200, TASK-301), `accreditation` (VIG-208, TASK-338) y `notifications` (VIG-209, TASK-339) con seguridad a nivel de fila forzada; registro de los tres módulos en la raíz de composición (VIG-201, TASK-302) y arranque completo con los manifiestos `U04_TASKS` y `U04_CONSUMERS` y la orden `vigia-admin rebuild-loop-projection` (VIG-241, TASK-326); mamparos por consumidor, fábrica única de clientes salientes con lista cerrada de destinos y módulo único de nombres de métrica (VIG-210, TASK-349).
- **Lazo**:
  - validadores de texto `blocked_vocabulary` (VIG-204, TASK-303) y `person_attribution` (VIG-218, TASK-340), y vocabulario bloqueado por organización (VIG-220, TASK-350); configuración del lazo y calendario por planta (VIG-205, TASK-304);
  - máquina de estados del hallazgo y proyección del lazo (VIG-206, TASK-305; VIG-219, TASK-341), verificación diaria por muestra y reconstrucción por planta (VIG-221, TASK-306), bandeja por zona servida desde la proyección (VIG-222, TASK-307);
  - clasificación (VIG-212, TASK-308), cola de revisión (VIG-213, TASK-309), adjuntos de cierre bajo `closure/` (VIG-207, TASK-310), acción correctiva, cierre firmado y reapertura (VIG-223, TASK-311; VIG-233, TASK-343), estado sin alternativa segura (VIG-228, TASK-312), plazos y escalamiento (VIG-229, TASK-313) y detalle auditado (VIG-232, TASK-342);
  - análisis de causa raíz (VIG-214, TASK-315) con asistente opcional, su doble y su adaptador solo en `vigia-worker` (VIG-224, TASK-316; VIG-234, TASK-344);
  - declaración de un adjunto de cierre no recuperable con `vigia-admin declare-unrecoverable-attachment` (VIG-274).
- **Acreditación**: exportación con arrendamiento, ZIP en flujo por subida por partes, volúmenes y compositor determinista (VIG-215, TASK-317; VIG-226, TASK-345; VIG-230, TASK-318), documento portátil (VIG-236, TASK-319), verificador dentro del paquete (VIG-238, TASK-346), paquete mensual con auditoría muestral (VIG-239, TASK-320; VIG-240, TASK-347), métricas de definición cerrada (VIG-237, TASK-321), exposición (VIG-231, TASK-322) y adopción (VIG-216, TASK-323).
- **Notificaciones**: catálogo de avisos y motor idempotente (VIG-217, TASK-324), correo por SES con cortacircuito y consumidor de avisos de entrega (VIG-225, TASK-325), agrupación y resumen semanal (VIG-227, TASK-348).
- **Pruebas**: PR-LAZ-10 con estado contra un modelo (VIG-235, TASK-314); aislamiento de U-04 (VIG-242, TASK-327); abuso L-1 a L-12 y ejemplos (VIG-247, TASK-328); resiliencia FS-LAZ-01 a 12 y DR-LAZ-01 a 04 (VIG-243, TASK-329; VIG-248, TASK-352); bancos de NFR-LAZ-01 (VIG-244, TASK-330); carga de exportación y volumetría (VIG-246, TASK-353).
- **Infraestructura**: pila `vigia-loop` con mensajería, punto privado, secreto del asistente, política del worker, identidad de correo, registros DNS y conjunto de configuración (VIG-202, TASK-331; VIG-211, TASK-351); pendientes aditivos nº 20, 22 y 23, las 15 alarmas de U-04 y los cinco paneles en `vigia-pilot` (VIG-245, TASK-332).
- **Canalización**: `ci.yml` con las suites de U-04 y su cobertura, `nightly.yml` con FS-LAZ, carga de exportación, bancos y volumetría, y `release.yml` con `vigia-loop` al final y las comprobaciones nº 14 y 15 (VIG-249, TASK-333).
- **Documentación de operación**: runbooks [U-04 7.1 a 7.6](docs/runbooks/README.md) (reconstrucción de la proyección, colas muertas del lazo, objeto perdido de `closure/`, clave del asistente, arenero y reputación del correo, exportación atascada), ensayos DR-LAZ-01 a 04 dentro del [ensayo trimestral](docs/runbooks/6.1-quarterly-restore-drill.md#ensayos-de-u-04-dr-laz-01-a-04) y sección de U-04 del `README.md` (VIG-250, TASK-334).

### Corregido

- Cancelación en cola determinista del pool de CPU (VIG-130) y prueba del servidor colgado en tiempo virtual (VIG-134).
- Alias rechazados en el registro de tipos por privacidad (P3) (VIG-129).
- Concesiones sin fecha de caducidad y auditoría de `GET /hierarchy` bajo concesión (VIG-135, adenda A-50).
- U-04: la bandeja ya no sirve una planta restaurada con ingestas sin certificar (VIG-273); `verify_loop_projection` detecta un hallazgo del expediente ausente de la proyección (VIG-269); las métricas declaradas sin emisor se emiten y alimentan sus paneles y alarmas (VIG-270); la sonda del asistente no cierra el circuito con el proveedor aún caído (VIG-272); reclasificar tras resolver la cola de revisión ya no responde 500 (VIG-268).
- El archivado mensual verifica y registra la versión exacta del objeto subido (`archive_version_id_hex`, `audit_partition_archived` versión 3) y la restauración la pide: dos pasadas a la vez ya no dejan una referencia que restaura otra versión (VIG-255).
