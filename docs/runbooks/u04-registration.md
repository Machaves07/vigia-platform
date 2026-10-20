# Registro de U-04

La raíz de A-52 compone `loop`, `accreditation` y `notifications` mediante sus respectivos
`registration.py`. Ninguna ampliación de U-04 necesita editar `shared/runtime/units.py`.

## Catálogo

En `loop/registration.py`:

- `U04_RECORD_TYPES`: **17** tipos del expediente (13 del lazo y 4 de acreditación).
- `U04_DOMAIN_EVENT_TYPES`: **25** eventos del diseño (§11.3, nota R14).
- `U04_INTERNAL_EVENT_TYPES`: **3** trabajos adicionales: `export_requested`,
  `assistant_requested`, `email_delivery_events_poll`. `U04_EVENT_TYPES` conserva los **28**;
  no se retiran eventos persistidos para hacer coincidir un recuento histórico (P4).
- `U04_CONSUMERS`: **6**, con mamparos independientes por catálogo: `loop_projection` (4),
  `review_queue` (2), `notification_engine` (4), `assistant_worker` (2), `export_worker` (1),
  `email_delivery_events` (2). Los tamaños son objetivos configurables con `VIGIA_LOOP_BULKHEADS`.
- `U04_FREE_TEXT_VALIDATORS`: **2**, `blocked_vocabulary` y `person_attribution`.
- `U04_TASKS`: **10 periódicas**; son las nueve periódicas de LC-LAZ-37 más el sondeo global
  de correo añadido por TASK-325. La décima tarea del diseño, `rebuild_loop_projection`,
  **es administrativa a demanda**, sin horario ni alarma de retraso (U04-H-14).

| Periódica | Cadencia UTC | Iteración |
|---|---|---|
| evaluate_deadlines | cada hora | organización |
| review_reminders | cada hora; ventana interna configurada | organización |
| unclosable_review_due | diaria 06:00 | organización |
| monthly_package | diaria 04:00; periodos locales pendientes | organización |
| metrics_materialize | diaria 03:00 | organización |
| adoption_check | diaria 05:00 | organización |
| email_grouping | cada hora; ventana interna configurada | organización |
| digest_weekly | diaria 02:00; día semanal configurado | organización |
| verify_loop_projection | diaria 02:00 | organización |
| email_delivery_events_poll | cada 15 segundos | global |

Cada periódica se registra con unidad U-04. El catálogo completo tiene **11 + 7 + 10** periódicas.
Los tres procesos exigen el catálogo, los validadores y la ranura `notifications.registration.EMAIL_SENDERS`;
no tener adaptador de correo configurado sigue degradando según BR-NUC-32. La ausencia de la
ranura es un defecto de composición, no una caída de SES. Un consumidor sin su mamparo tampoco
supera la comprobación. `RegistrationIncomplete` nombra la pieza ausente y el proceso termina
con `STARTUP_FAILURE_EXIT_CODE`.

## Reconstrucción por planta

```sh
vigia-admin rebuild-loop-projection --organization UUID --plant UUID --operator UUID --dry-run
vigia-admin rebuild-loop-projection --organization UUID --plant UUID --operator UUID --yes
```

Exige un `platform_operator` vigente; sin `--yes` pide confirmación. `--dry-run` solo comprueba
la pertenencia de la planta. La ejecución audita intención y finalización en la proveedora y
usa el contexto de iteración del sistema U-04 en la organización indicada, no una sesión ni
un contexto de operador fabricado en el cliente. Comparte el constructor `projection_rebuilder`
con la verificación diaria: conserva arrendamiento, renovación e intercambio por tabla sombra.

Salida JSON con organización, planta, resumen y recuentos; nunca contenido del expediente.
Código 4: operador o planta no válidos; 5: otro proceso sostiene el arrendamiento (reintentable).
Un reintento no crea registros de negocio ni duplica filas de la proyección; sí deja auditoría.
Las lecturas internas de catálogo, cobertura, expediente y resultados de marcas aceptan los
contextos sellados de evento/iteración del sistema U-04, siempre bajo RLS y organización fija.
No obtienen por ello permisos de usuario ni URL de lectura de clips.

Las diez tareas tienen su nombre fijado en la política de atributos de métricas desde el
arranque; no dependen del registro dinámico de nombres, que rechaza los más largos. La prueba
de PR-LAZ-55 emite series para cada tarea y consumidor con una política nueva, exige que
ningún nombre se convierta en `other` y aplica la metapropiedad de cardinalidad de U-03.

## Reproceso

```sh
vigia-admin replay-dead-letter EVENTO review_queue --operator UUID
```

Se reutiliza la orden de U-02: mantiene `event_id`, reinicia la entrega y conserva la constancia
de cola muerta. El despachador vuelve a aplicar el consumidor idempotente; el efecto existente
no se duplica. La prueba administrativa reproduce este caso sobre `review_queue` real.

## Verificación y limitación registrada

`test_laz_startup_checks.py` retira las 61 piezas de dominio/registro/puerto y comprueba las
tres raíces y sus códigos de salida; los trabajos internos adicionales siguen comprobados
por `test_laz_bootstrap_closed.py`. También se alteran unidad, horario e iteración por tarea
y se quita o intercambia el mamparo de cada consumidor. `test_registered_tasks.py` fija las
cadencias de forma independiente de los manejadores.

**PR-LAZ-48 sigue pendiente**: la igualdad literal de notificaciones entre ejecución continua y
caída de 48 h no concuerda con la separación entre envíos de U04-H-07. El contraejemplo local
de VIG-241 deja 3 `review_overdue` en ejecución continua y 1 al volver. No se ha alterado esa
semántica ni se declara aprobada la propiedad transversal de las diez tareas. La decisión
pendiente está registrada en VIG-267, que bloquea el cierre de VIG-241.
