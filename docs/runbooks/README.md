# Runbooks de operación de `vigia-platform`

**Unidades**: U-02, U-03, U-04 y U-05 · **Tareas**: TASK-152 (VIG-97), TASK-236 (VIG-172), TASK-334 (VIG-250) y TASK-458 (VIG-331) · **Requisitos**: RESILIENCY-11 y 13, NFR-NUC-12, NFR-NUC-50, NFR-GOB-65, NFR-GOB-21, NFR-GOB-48, NFR-LAZ-39 y 42, y NFR-APP-29, 40 y 41.
**Diseño**: `plataforma-nucleo/infrastructure-design/deployment-architecture.md` §6, `plataforma-gobernanza-flota/infrastructure-design/deployment-architecture.md` §6 `plataforma-lazo-acreditacion/infrastructure-design/deployment-architecture.md` §6 y `plataforma-aplicacion/infrastructure-design/deployment-architecture.md` §6, y sus notas fechadas del 2026-09-23, en `aidlc-docs/` del espacio de trabajo de Vigía, fuera de este repositorio. La adenda `docs/decisions/adenda-al-diseno.md` del plan manda donde difieran.

Estos procedimientos se escriben antes del incidente para que el dueño los ejecute sin reconstruirlos. Cada runbook de U-02 tiene tres secciones fijas: **Pasos**, **Validación posterior** y **Comunicación**. Los de U-03 (prefijo `u03-`), U-04 (prefijo `u04-`, numerados 7.1 a 7.6 a continuación de los de U-02 y U-03) y U-05 (prefijo `u05-`, numerados 6.1 a 6.4 como en su diseño) añaden **Disparador**, **Vuelta atrás** y **Estado en el código**, y cada uno se cita desde la alarma que lo dispara: en U-04, la descripción de cada alarma de `infra/stacks/loop.py` nombra su runbook. Los nombres de recursos, alarmas, métricas y órdenes son los del código de este repositorio (`infra/`, `backend/`, `.github/`), no los del diseño cuando difieren. Cada diferencia está anotada en la sección «Estado en el código» del runbook.

Los cinco runbooks de código de NFR-GOB-65 se reparten así: **revocar un nodo** y **reemplazar un nodo averiado** con 6.1 de U-03; **regenerar y publicar la lista de revocación** con 6.2 de U-03; **reabrir una sesión de walk-test incompleta** con 6.4 de U-03; **reprocesar una alarma atascada** con [6.3 de U-02](6.3-dead-letter-replay.md), que U-03 no duplica. El conteo de clips huérfanos (6.3 de U-03) no tiene runbook de código propio: es verificación y conteo.

## Índice

| Runbook | Cuándo | Frecuencia |
|---|---|---|
| [6.1 Restauración de prueba trimestral](6.1-quarterly-restore-drill.md) | Ensayo de copia y restauración (DR-NUC-01 a 03) | Trimestral; alarma `vigia-restore-drill-overdue` a los 100 días |
| [6.2 Conmutación y vuelta](6.2-failover-and-failback.md) | Conmutación de la base o zona de disponibilidad caída (DR-NUC-04) | Ante el evento |
| [6.3 Reproceso de la cola muerta](6.3-dead-letter-replay.md) | Alarma `vigia-dead-letter-created` | Ante la alarma |
| [6.4 Rotación de claves](6.4-key-rotation.md) | Claves Ed25519 de firma y raíz de `vigia-node-ca` | Anual (firma), cada 10 años o ante compromiso (raíz) |
| [6.5 Archivado y restauración de auditoría](6.5-audit-archive-and-restore.md) | Archivado mensual y restauración para investigación | Mensual (automático) y bajo petición |
| [6.6 Rotación de la contraseña de la base](6.6-database-password-rotation.md) | Rotación automática o sospecha de compromiso | Cada 30 días (automática) o ante sospecha |
| [U-03 6.1 Rotación forzada de la credencial de un nodo](u03-6.1-forced-credential-rotation.md) | Revocar un nodo, reemplazar un equipo averiado o alarma de flota `certificate_expiring` (NFR-GOB-65) | Ante el evento |
| [U-03 6.2 Regeneración y publicación de la lista de revocación](u03-6.2-revocation-list-publication.md) | Alarmas `revocation-list-publish-failed` y `revocation-list-expiring`; tras una restauración (NFR-GOB-21) | Ante la alarma o la restauración |
| [U-03 6.3 Verificación y conteo de clips huérfanos](u03-6.3-orphan-clips.md) | Alarma de flota `orphan_clips_growing` o revisión de R5 (NFR-GOB-65) | Ante la alarma o el crecimiento sostenido |
| [U-03 6.4 Reapertura de una sesión de walk-test incompleta](u03-6.4-walk-test-session-reopen.md) | Sesión `incomplete` por `expire_walk_test_sessions` a los 7 días (NFR-GOB-65) | Bajo petición del coordinador |
| [U-04 7.1 Reconstrucción de la proyección del lazo por planta](u04-7.1-loop-projection-rebuild.md) | Alarmas `loop-projection-divergence` y `periodic-task-stale-verify-loop-projection`; restauración de la base (DR-LAZ-01 y 02) | Ante la alarma o la restauración |
| [U-04 7.2 Reproceso de las colas muertas del lazo](u04-7.2-dead-letter-replay.md) | Camino A: alarma `loop-dead-letter`. Camino B: alarmas `email-events-dlq`, `email-events-queue-age` y `periodic-task-stale-email-delivery-events-poll` (DR-LAZ-03) | Ante la alarma |
| [U-04 7.3 Objeto perdido de `closure/` o volumen de paquete](u04-7.3-lost-closure-object.md) | Adjunto de cierre ilegible o volumen ausente (DR-LAZ-04) | Ante el evento |
| [U-04 7.4 Rotación de la clave del proveedor del asistente](u04-7.4-assistant-key-rotation.md) | Rotación planificada o compromiso; alarmas `assistant-unavailable`, `assistant-token-budget` y `assistant-rate-margin` | Manual, ante el evento o la alarma |
| [U-04 7.5 Arenero, cuotas y reputación del correo](u04-7.5-email-sandbox-and-reputation.md) | Parte A: antes del primer despliegue. Parte B: alarmas `email-bounce-rate`, `email-complaint-rate`, sus informativas y `email-send-quota-high` | Una vez (A); ante la alarma (B) |
| [U-04 7.6 Exportación atascada o fallida](u04-7.6-export-stuck-or-failed.md) | Alarmas `export-stuck`, `export-failed` y `periodic-task-stale-monthly-package` | Ante la alarma |
| [U-05 6.1 La aplicación no carga](u05-6.1-la-aplicacion-no-carga.md) | Alarma `vigia-app-index-unhealthy` o comprobación de despliegue nº 9 en rojo | Ante la alarma |
| [U-05 6.2 La versión nueva no aparece en una planta](u05-6.2-version-nueva-no-aparece.md) | Una planta sigue viendo la versión anterior tras un release | Ante el reporte |
| [U-05 6.3 El cortafuegos bloquea a una planta](u05-6.3-cortafuegos-bloquea-una-planta.md) | Alarmas `waf-rate-blocked` y `waf-blocked-spike`; errores sin código de correlación en una planta | Ante la alarma o el reporte |
| [U-05 6.4 La política de contenido bloquea un recurso](u05-6.4-politica-de-contenido-bloquea.md) | Comprobación nº 8 en rojo; clips, subidas o pantallas bloqueados en el navegador | Ante el evento |
| [Soporte U-05: el código de correlación](u05-soporte-correlation-id.md) | Una persona reporta un error con su código (NFR-APP-41); por qué las alertas viven en el servidor y la interfaz no instrumenta (NFR-APP-29 y 40) | Ante el reporte |
| [Salud de lectura de la proyección del lazo](loop-projection-read-health.md) | Bandeja que declara una planta restaurada sin reconstruir (VIG-273) | Referencia de 7.1 |
| [Métricas de U-04](u04-metrics.md) y [registro de U-04](u04-registration.md) | Nombres de métricas, tareas, consumidores y órdenes de U-04 | Referencia |
| [Contingencia de subparticionado](subpartitioning-contingency.md) | La volumetría nocturna supera los 300 ms en la lista mayor (PAT-NUC-ESC-01) | Solo si la medición lo exige |
| [Recuperación de una cadena rota](broken-chain-recovery.md) | Alarma `vigia-integrity-compromised` (BR-NUC-58) | Ante la alarma |
| [Pruebas con la base en otra fecha](clock-shifted-database-tests.md) | Código sensible al reloj (retro 15 de `AGENTS.md`) | En la tarea que lo toque |
| [Traslado de la pila `vigia-datasets`](traslado-vigia-datasets.md) | Primer despliegue (TASK-150, VIG-28) | Una vez |

Las alarmas de U-04 sin runbook propio son las `periodic-task-stale-*` de `evaluate_deadlines`, `review_reminders`, `unclosable_review_due`, `metrics_materialize`, `adoption_check`, `email_grouping` y `digest_weekly`: su descripción remite a los registros de `vigia-worker`. Todas sus tareas son idempotentes y se ponen al día solas tras una caída (PR-LAZ-48, [registro de U-04](u04-registration.md#verificación)).

## Reglas comunes

- **Identidad.** Todo runbook se ejecuta con la identidad administrativa del dueño con segundo factor, nunca con `vigia-deploy`. Los que leen evidencias o archivos asumen además el rol `vigia-restore`, que exige segundo factor y dura como máximo 4 horas.
- **Registro.** Cada ejecución queda en `shared-infrastructure.md` §6 del espacio de trabajo con fecha, runbook, resultado y enlace a las salidas. Los runbooks que escriben en la plataforma dejan además su rastro en la auditoría de la organización proveedora.
- **Datos.** Nunca se copian evidencias, registros ni salidas con datos de una planta a un repositorio, a un ticket ni a un prompt. Las consultas para una investigación se entregan por el canal que pida el cliente, no por correo con adjuntos.
- **Sin AWS por ahora (adenda A-47).** U-02 se cierra sin cuenta de AWS y el primer despliegue (TASK-154, VIG-98) está diferido. Estos runbooks quedan listos para ese momento; hasta entonces solo se ensaya lo que corre en local (FS-NUC-10 en `nightly.yml`, `vigia-admin --dry-run` con el entorno de `docker-compose.yml`).

## Cómo lanzar `vigia-admin` y `vigia-migrate` en un despliegue

Las órdenes administrativas corren como **tareas puntuales de ECS** con las definiciones `vigia-admin` y `vigia-migrate` de la pila `vigia-compute`, en las subredes de aplicación y con el grupo `sg-tasks`. El mismo guion que usa `release.yml` las lanza y espera a que terminen:

```text
# desde la raíz del repositorio, con el perfil de AWS de la identidad administrativa
python .github/scripts/staging.py run-task --environment pilot --task admin -- vigia-admin <orden> [opciones]
python .github/scripts/staging.py run-task --environment pilot --task migrate                    # alembic upgrade head
```

- El guion lee las salidas `ClusterName`, `AdminTaskDefinition`, `MigrateTaskDefinition`, `AppSubnets` y `OneOffSecurityGroup` de `vigia-compute`, lanza `aws ecs run-task` con la orden sustituida, espera hasta 1 hora y falla si el contenedor no termina con 0.
- La salida de `vigia-admin` es **una línea JSON** con identificadores, en el grupo de registros `/vigia/pilot/admin`. Nunca lleva enlaces ni contraseñas; el enlace de una invitación va al secreto de un solo uso `vigia/pilot/bootstrap/invitation`.
- Códigos de salida: `0` hecho, `1` configuración o error inesperado, `2` uso incorrecto, `3` sin confirmación, `4` rechazo de la operación, `5` dependencia no disponible (reintentable).
- Las órdenes con confirmación (`bootstrap`, `create-organization`, `rotate-node-ca`) necesitan `--yes` en una tarea sin consola. Antes de la ejecución real, lanza la misma orden con `--dry-run`: valida y muestra lo que haría sin escribir nada.
- Todas las órdenes salvo `bootstrap` y `restore-audit-partition` piden `--operator <UUID>`, el `platform_operator` activo que ejecuta la orden. Todas salvo un `bootstrap` nuevo (sin `--resume`), incluida `restore-audit-partition`, piden la variable `VIGIA_PROVIDER_ORGANIZATION_ID`.

### Estado en el código (común)

- **Raíz de composición (VIG-137, A-52).** `vigia-api`, `vigia-worker` y `vigia-admin` reciben sus dependencias de `backend/src/vigia_platform/shared/runtime/`. `infra/stacks/compute.py` fija `VIGIA_API_RUNTIME`, `VIGIA_WORKER_RUNTIME` y `VIGIA_ADMIN_RUNTIME`, y además `VIGIA_PUBLIC_ORIGIN` en la API y en `vigia-admin`.
  - La raíz lee la credencial de `vigia_app` del secreto de `VIGIA_DB_APP_SECRET`, sin contraseñas en variables.
  - Si falta una variable o el secreto no existe, la API y el worker salen con código 3, y `vigia-admin` con 1 y `config_invalid`. La salida de errores nombra la variable, nunca su valor.
- **`VIGIA_PROVIDER_ORGANIZATION_ID` llega con un contexto de CDK.** La API y el worker la exigen, igual que `vigia-admin`. La organización proveedora no existe hasta `vigia-admin bootstrap`, así que la variable entra en las tres definiciones de tarea solo con `--context provider_organization_id=<UUID que imprime bootstrap>`. Sin ese contexto, la API y el worker no arrancan. El orden de pasos lo fija el primer despliegue (VIG-98).
- **Precondiciones del primer despliegue que deja la raíz** (VIG-98):
  - la imagen no trae `resources/pwned-top100k.txt`: la API no arranca sin ese respaldo (`VIGIA_BREACH_LIST_PATH`);
  - la imagen no trae el certificado raíz de RDS. Con `PGSSLMODE=verify-full`, que es el valor por defecto, hace falta `PGSSLROOTCERT`.
- `staging.py run-task` acepta `--environment pilot` o `staging-<n>`. Una instancia dedicada (`instance=<cliente>`) todavía no tiene entrada en el guion.
