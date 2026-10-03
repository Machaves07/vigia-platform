# Runbooks de operación de `vigia-platform`

**Unidad**: U-02 · **Tarea**: TASK-152 (VIG-97) · **Requisitos**: RESILIENCY-13, NFR-NUC-12 y NFR-NUC-50.
**Diseño**: `plataforma-nucleo/infrastructure-design/deployment-architecture.md` §6 y sus notas fechadas del 2026-09-23, en `aidlc-docs/` del espacio de trabajo de Vigía, fuera de este repositorio. La adenda `docs/decisions/adenda-al-diseno.md` del plan manda donde difieran.

Estos procedimientos se escriben antes del incidente para que el dueño los ejecute sin reconstruirlos. Cada runbook tiene tres secciones fijas: **Pasos**, **Validación posterior** y **Comunicación**. Los nombres de recursos, alarmas, métricas y órdenes son los del código de este repositorio (`infra/`, `backend/`, `.github/`), no los del diseño cuando difieren. Cada diferencia está anotada en la sección «Estado en el código» del runbook.

## Índice

| Runbook | Cuándo | Frecuencia |
|---|---|---|
| [6.1 Restauración de prueba trimestral](6.1-quarterly-restore-drill.md) | Ensayo de copia y restauración (DR-NUC-01 a 03) | Trimestral; alarma `vigia-restore-drill-overdue` a los 100 días |
| [6.2 Conmutación y vuelta](6.2-failover-and-failback.md) | Conmutación de la base o zona de disponibilidad caída (DR-NUC-04) | Ante el evento |
| [6.3 Reproceso de la cola muerta](6.3-dead-letter-replay.md) | Alarma `vigia-dead-letter-created` | Ante la alarma |
| [6.4 Rotación de claves](6.4-key-rotation.md) | Claves Ed25519 de firma y raíz de `vigia-node-ca` | Anual (firma), cada 10 años o ante compromiso (raíz) |
| [6.5 Archivado y restauración de auditoría](6.5-audit-archive-and-restore.md) | Archivado mensual y restauración para investigación | Mensual (automático) y bajo petición |
| [6.6 Rotación de la contraseña de la base](6.6-database-password-rotation.md) | Rotación automática o sospecha de compromiso | Cada 30 días (automática) o ante sospecha |
| [Contingencia de subparticionado](subpartitioning-contingency.md) | La volumetría nocturna supera los 300 ms en la lista mayor (PAT-NUC-ESC-01) | Solo si la medición lo exige |
| [Recuperación de una cadena rota](broken-chain-recovery.md) | Alarma `vigia-integrity-compromised` (BR-NUC-58) | Ante la alarma |
| [Pruebas con la base en otra fecha](clock-shifted-database-tests.md) | Código sensible al reloj (retro 15 de `AGENTS.md`) | En la tarea que lo toque |
| [Traslado de la pila `vigia-datasets`](traslado-vigia-datasets.md) | Primer despliegue (TASK-150, VIG-28) | Una vez |

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

- **Raíz de composición pendiente.** `vigia-api`, `vigia-worker` y `vigia-admin` reciben sus dependencias del constructor que nombran `VIGIA_API_RUNTIME`, `VIGIA_WORKER_RUNTIME` y `VIGIA_ADMIN_RUNTIME`. Ningún módulo de `backend/src/` implementa todavía ese constructor y `infra/stacks/compute.py` no fija esas variables; hoy solo existen los de las pruebas (`backend/tests/admin_support.py`, `backend/tests/worker_process.py`). Hasta que llegue la raíz de composición, las tareas desplegadas terminan con código 3 o 1 sin arrancar. Es una precondición del primer despliegue (VIG-98), no de estos runbooks.
- `staging.py run-task` acepta `--environment pilot` o `staging-<n>`. Una instancia dedicada (`instance=<cliente>`) todavía no tiene entrada en el guion.
