# Runbook · Recuperación de una cadena rota

**Unidad**: U-02 · **Tarea**: TASK-152 (VIG-97), por el seguimiento de la revisión de VIG-69 (motor `ledger.chain.verify`) · **Requisitos**: BR-NUC-56 y 58; S-PLA-09; NFR-NUC-38 (alarma de máxima severidad); P4 y P5.
**Diseño**: `business-rules.md` BR-NUC-58: «una cadena rota no se repara: el resultado queda en la auditoría y el expediente conserva lo escrito; la recuperación es una restauración desde copia con un punto de control nuevo que referencia el incidente».
**Ejecuta**: el dueño, con la organización afectada informada.
**Índice**: [runbooks de operación](README.md).

## Cuándo

La alarma `vigia-integrity-compromised` (métrica `integrity_compromised_total`) llega a `vigia-alerts`. Una verificación (`incremental`, `full` u `on_demand`) encontró una cadena `broken`: un hash que no coincide, una secuencia que falta, un punto de control con firma inválida o una cabeza que no corresponde. En la misma transacción se publicó el evento `integrity_compromised` y quedó la auditoría `integrity_verification` con el registro roto como recurso.

**Comportamiento esperado mientras dure la rotura**: cada verificación **incremental** diaria (`verify_chains_incremental`, 01:00 UTC) vuelve a recorrer la cadena desde el último punto íntegro **anterior a la rotura**, la encuentra rota otra vez y **vuelve a publicar** `integrity_compromised`. Es intencionado: la alarma no se oculta mientras la causa siga ahí. No es un fallo del motor ni hay que silenciarla.

## Pasos

1. **Contener.** No se edita ni se borra ninguna fila (P4). Abre el incidente y anota la hora de la primera alarma.
2. **Leer el resultado.** `GET /integrity/results` (permiso `integrity.verify`), con una sesión que tenga la cadena a su alcance:
   - `coordinator_sst`, `plant_manager` o `administrator` para las cadenas de su organización;
   - `platform_operator` para las de la organización proveedora.

   Anota por cadena: tipo (`ledger` de planta u organización, o `audit`), `mode`, `status`, `from_sequence`, `to_sequence` (último íntegro) y la secuencia rota con su motivo. Es lo que devuelve `IntegrityService.last_results(context)`.
3. **Determinar la causa** con la auditoría alrededor de la secuencia rota y el rastro de la cuenta (CloudTrail, accesos a la base). Puede ser un defecto de la aplicación (la forma canónica no coincide), una escritura fuera de `EscritorExpediente` o una manipulación. Si hay sospecha de acceso no autorizado, sigue también [6.6](6.6-database-password-rotation.md#ante-sospecha-de-compromiso).
4. **Restaurar desde copia** (BR-NUC-58): restaura la base al instante anterior a la rotura como en el paso 2 de [6.1](6.1-quarterly-restore-drill.md#pasos), sobre `vigia-drill-db`, y verifica allí que la cadena da `intact` hasta la cabeza de ese instante. Con eso se decide, con el dueño y la organización afectada:
   - qué tramo de la cadena se restituye desde la copia;
   - qué escrituras legítimas posteriores se vuelven a presentar (los nodos conservan lo no aceptado);
   - el punto de control nuevo que **referencia el incidente**.

   Lo escrito antes de la restauración se conserva como constancia, nunca se reescribe.

   **Cómo lo restaurado sustituye a `vigia-pilot-db`: se decide en el incidente.** Ni el diseño ni el código fijan el mecanismo. Hay dos caminos posibles:
   - promover la instancia restaurada como nueva `vigia-pilot-db` (renombrar instancias y volver a apuntar los secretos y la pila `vigia-data`);
   - restituir solo el tramo afectado en la base vigente.

   Cualquiera de los dos interrumpe el servicio y toca una pila que no se revierte de forma automática. La decisión, con el dueño y la organización afectada, se registra en la adenda al diseño antes de ejecutarla, con el procedimiento elegido y su validación.
5. **Verificar de nuevo desde la génesis**, una vez restaurada la cadena. Pide una verificación **completa bajo demanda** de cada cadena afectada:

   ```text
   POST /integrity/verify
   {"kind": "ledger", "plant_id": "<uuid de la planta>"}   # expediente de planta
   {"kind": "ledger", "plant_id": null}                    # expediente de organización
   {"kind": "audit",  "plant_id": null}                    # auditoría
   ```

   La ruta responde `202` con un `request_id`. El consumidor `integrity_on_demand` del worker ejecuta `verify(…, on_demand)` desde la génesis. Su resultado `intact` **cierra la rotura**: desde ahí la incremental parte del nuevo punto íntegro y deja de recorrer desde la rotura y de republicar `integrity_compromised`. Sin este paso, la incremental sigue volviendo a la rotura aunque la cadena ya esté restaurada.
6. **Comprobar antes de cerrar** con `GET /integrity/results` (`last_results(context)`): la cadena afectada muestra `mode = on_demand`, `status = intact`, `from_sequence` 1 y `to_sequence` igual a la cabeza actual.

## Validación posterior

- [ ] `GET /integrity/results` da `on_demand` e `intact` para cada cadena afectada, con `to_sequence` en la cabeza.
- [ ] La verificación incremental del día siguiente da `intact` y no vuelve a publicar `integrity_compromised`.
- [ ] La alarma `vigia-integrity-compromised` vuelve a `OK`.
- [ ] La auditoría conserva la `integrity_verification` rota original y la íntegra posterior: la historia completa del incidente.

## Comunicación

- **Máxima severidad**: aviso a los contactos de la organización afectada en la primera hora `[objetivo propio]`, con qué cadena, desde qué secuencia y que el expediente no se ha modificado. Actualización cada 4 horas.
- Informe de incidente en 5 días hábiles: causa, tramo afectado, qué se restituyó y el punto de control que referencia el incidente, con el resultado de la verificación final para que el cliente pueda repetirla con su verificador.
- Registro en `shared-infrastructure.md` §6.

## Estado en el código

- **Hecho**:
  - el motor `IntegrityService` (`backend/src/vigia_platform/ledger/chain/verify.py`): `verify`, `verify_all` y `last_results(context)`, con los modos `incremental`, `full` y `on_demand`;
  - la republicación intencionada de `integrity_compromised` por la incremental;
  - las rutas `POST /integrity/verify`, `GET /integrity/results` y `GET /integrity/checkpoints` (`ledger/adapters/http/integrity.py`), el consumidor `integrity_on_demand` y la alarma `vigia-integrity-compromised`.
- **Falta**:
  - **una orden de `vigia-admin` que pida la verificación bajo demanda** de todas las cadenas. Hoy es una petición por cadena por la ruta;
  - **el «punto de control nuevo que referencia el incidente»** de BR-NUC-58. No hay una operación que lo escriba: el paso 4 se decide por incidente y se registra en la adenda.
