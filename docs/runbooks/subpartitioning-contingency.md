# Runbook · Migración de contingencia: subparticionado por organización

**Unidad**: U-02 · **Tarea**: TASK-152 (VIG-97) · **Requisitos**: PAT-NUC-ESC-01; NFR-NUC-03, 05 y 08; PR-NUC-43.
**Diseño**: `nfr-design/nfr-design-patterns.md` PAT-NUC-ESC-01: «sin subparticionado: la migración de contingencia por hash de organización queda documentada en `docs/runbooks/` (tabla nueva, copia por lotes, intercambio de nombres)».
**Ejecuta**: una tarea de desarrollo que escribe la migración y la prueba, y después el dueño en una ventana de mantenimiento. **Es una contingencia**: no se ejecuta salvo que se cumpla la condición de entrada.
**Índice**: [runbooks de operación](README.md).

## Cuándo (condición de entrada)

Solo si la volumetría nocturna muestra que la **lista con alcance de la organización mayor, con un año de datos, supera los 300 ms** (p95). La mide el banco `ledger_list_200_scoped` (`backend/tests/benchmarks/test_nfr_nuc_01_ledger.py`, objetivo 300 ms) en el trabajo `volumetria` de `nightly.yml`, cuyo informe se guarda como artefacto `informe-volumetria`.

- Un solo nocturno por encima no basta: hacen falta **tres nocturnos seguidos** por encima, con la misma escala, para descartar ruido del runner `[objetivo propio]`.
- Antes de decidir, descarta las causas baratas: un índice que falta o no se usa (`EXPLAIN (ANALYZE, BUFFERS)` de la consulta del banco), estadísticas viejas (`ANALYZE`) o particiones mensuales sin crear (alarma `vigia-default-partition-rows`). Si una de ellas explica la medición, no hay contingencia.
- La decisión se registra en la adenda del plan antes de escribir la migración: cambia el esquema de tres tablas de solo anexar.

## Pasos

Hoy `ledger.ledger_record`, `shared.audit_entry` y `ledger.evidence` están particionadas por **rango mensual** (`received_at`; evidencias, `verified_at`), con una partición por defecto vigilada (`nuc_0002`, `nuc_0016`). La contingencia añade un segundo nivel: cada partición mensual se subdivide **por hash de `organization_id`**. Los pasos se describen para `ledger.ledger_record`, la tabla de la lista. Las otras dos solo se migran si su propia medición lo pide.

1. **Escribir la migración** (tarea de desarrollo, revisión obligatoria del dueño por ser ruta de `CODEOWNERS`). Revisión `nuc_<NNNN>` siguiente de la cadena, solo hacia adelante y conforme a `tools/lint_migrations.py`:
   1. **Tabla nueva** `ledger.ledger_record_v2`, con las mismas columnas, restricciones e índices, `PARTITION BY RANGE (received_at)`. Cada partición mensual es a su vez `PARTITION BY HASH (organization_id)` con `MODULUS` fijo `[objetivo propio: 8, a confirmar con la medición]`, más su partición por defecto.
   2. **Mismas políticas** de seguridad a nivel de fila en la tabla padre (`FORCE ROW LEVEL SECURITY`), mismos permisos para `vigia_app` y los mismos disparadores de solo anexar (`ENABLE ALWAYS`).
   3. **Sin el disparador de encadenado durante la copia.** `ledger.vigia_chain_link` calcula `sequence`, `previous_hash` y los hashes al insertar. Las filas copiadas ya los traen y **no deben recalcularse**. La copia inserta los valores tal cual y el disparador se crea en la tabla nueva **después** de la copia, en el paso de intercambio.
   4. `shared.vigia_create_month_partitions` y `shared.vigia_default_partition_rows` aprenden el segundo nivel: crear un mes crea sus particiones de hash.
2. **Ensayar en `staging-<n>` y en local** con la volumetría a escala objetivo:
   - la migración termina;
   - la verificación completa de todas las cadenas da `intact` antes y después;
   - el banco `ledger_list_200_scoped` baja de 300 ms.

   Sin las tres cosas, no se sigue.
3. **Anunciar la ventana** a los contactos del cliente con 48 horas (NFR-NUC-10). La migración larga se ejecuta en ventana y la tarea `vigia-migrate` no tiene `statement_timeout`.
4. **Copia por lotes en caliente**, antes de la ventana: copia los meses cerrados de `ledger.ledger_record` a `ledger.ledger_record_v2` por lotes de una partición mensual, en orden de `received_at`. Cada lote es una transacción y queda anotado con su recuento. Las escrituras siguen yendo a la tabla antigua.
5. **Ventana: congelar y completar.**
   1. Escala `vigia-api` y `vigia-worker` a 0 tareas. Los nodos reencolan y no se pierde nada (el nodo inicia toda conexión).
   2. Copia el resto: el mes en curso y la partición por defecto.
   3. Comprueba la igualdad, que debe salir vacía en los dos sentidos:

      ```sql
      (SELECT * FROM ledger.ledger_record EXCEPT ALL SELECT * FROM ledger.ledger_record_v2)
      UNION ALL
      (SELECT * FROM ledger.ledger_record_v2 EXCEPT ALL SELECT * FROM ledger.ledger_record);
      ```

6. **Intercambio de nombres**, en una sola transacción de `vigia-migrate`:
   - `ALTER TABLE ledger.ledger_record RENAME TO ledger_record_unpartitioned`;
   - `ALTER TABLE ledger.ledger_record_v2 RENAME TO ledger_record`;
   - crear en la tabla nueva el disparador de encadenado y reasignar las claves externas que apuntan a ella.

   La tabla antigua **no se borra** (P4: el registro no se borra). Queda de solo lectura y solo anexar, fuera del camino de escritura. Retirarla es otra decisión registrada, después de un ensayo trimestral correcto ([6.1](6.1-quarterly-restore-drill.md)) sobre la estructura nueva.
7. **Reabrir**: escala `vigia-api` a 2 tareas y `vigia-worker` a 1, y pide una verificación completa bajo demanda de cada cadena de expediente (`POST /integrity/verify`, ver [recuperación de una cadena rota](broken-chain-recovery.md#pasos)).

## Validación posterior

- [ ] La consulta de igualdad del paso 5 dio vacío, y los recuentos por mes y por organización coinciden.
- [ ] Todas las cadenas de expediente dan `intact` en la verificación bajo demanda (`GET /integrity/results`), con la misma cabeza (secuencia y hash) que antes de la ventana.
- [ ] Una escritura nueva tras reabrir enlaza con la cabeza anterior (`previous_hash` igual al hash de la última fila copiada).
- [ ] `alembic heads` da una sola cabeza y `shared.vigia_schema_version()` es la de la migración.
- [ ] Las comprobaciones de despliegue de `deployment-architecture.md` §3.2 pasan, la alarma `vigia-default-partition-rows` está en `OK` y el siguiente nocturno mide la lista por debajo de 300 ms.

## Comunicación

- Ventana anunciada con 48 horas a los contactos del cliente, con la duración estimada del ensayo del paso 2 más un margen del 50 % `[objetivo propio]`.
- Aviso de inicio y de cierre de la ventana. Si la validación falla, se avisa de que la ventana se alarga y se vuelve a la tabla antigua: el intercambio de nombres inverso, en la misma transacción, mientras no haya escrituras nuevas.
- Registro en `shared-infrastructure.md` §6 y entrada en el `CHANGELOG.md` de la versión que lleve la migración.

## Estado en el código

- **No hay subparticionado**, por diseño: es una contingencia. El particionado mensual, la partición por defecto y su alarma están en `nuc_0002` y `nuc_0016` (`backend/migrations/versions/`) y en `backend/src/vigia_platform/shared/archive/partitions.py`. El banco de la condición de entrada está en `backend/tests/benchmarks/test_nfr_nuc_01_ledger.py`.
- El módulo del hash y la forma exacta del intercambio, con las claves externas de `ledger.evidence` y la cabeza de cadena, se fijan en la tarea que escriba la migración, con el ensayo del paso 2. Este runbook fija el orden y las comprobaciones, no el SQL definitivo.
