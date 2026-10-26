# Término y sugerencia en los rechazos de los validadores de texto libre

Fecha: 2026-10-26 · VIG-344 (seguimiento de VIG-314, TASK-444, SCR-11) · **Propuesta para la adenda:
la registra el dueño** (la adenda está fuera de este repositorio).

## Problema

BR-APP-49, UI-22 y `AssistantRequest` (`plataforma-aplicacion/functional-design/domain-entities.md`
§2.19) piden que un rechazo de los validadores muestre el término rechazado y la sugerencia
(`rejection: {code, term?, suggestion?}`). La etiqueta de `loop_blocked_vocabulary_term` dice lo
mismo: «la respuesta indica cuál y una alternativa». Hasta este cambio, U-04 respondía solo
`detail_code`: el validador calculaba el término y la alternativa, pero no llegaban a la respuesta.

## Decisión propuesta

1. **Forma** (aditiva, sin cambio de versión del contrato). `ApiErrorBody` gana un campo opcional
   `rejection` (`ApiErrorRejection`, `additionalProperties: false`):

   | Campo | Tipo | Regla |
   | --- | --- | --- |
   | `code` | cadena, obligatoria | Motivo cerrado del validador, `snake_case` anclado, 1 a 64: `blocked_vocabulary_term` o `person_attribution_detected` |
   | `term` | cadena o ausente | 1 a 200 caracteres; si no cabe o va en blanco, no sale (nunca se recorta) |
   | `suggestion` | cadena o ausente | 1 a 200 caracteres, igual regla |

   Solo va en un `400 invalid_request` con `detail_code` (A-68); `ApiError` rechaza cualquier otra
   combinación. Un `detail_code` sin registrar sigue convirtiéndose en `internal_error`, sin
   `rejection`.

2. **Alcance en las rutas de texto libre de U-04** (todas las que devuelven
   `loop_blocked_vocabulary_term` o `loop_person_attribution_detected`): pasos y firma del análisis
   (SUP-U05-17 y 18), clasificación, resolución de la cola de revisión, acción correctiva y su
   evidencia, cierre y reapertura, incerrable, vocabulario de la organización y, en acreditación,
   auditoría por muestra y compromisos.

   - `blocked_vocabulary_term`: `term` es el **tramo de la forma de validación que coincidió**
     (plegado: minúsculas, sin tildes ni confusables), nunca el texto entero; `suggestion` es la
     alternativa del término de la lista.
   - `person_attribution_detected`: **solo `suggestion`** (la del patrón). `term` no sale nunca:
     lo que coincidió es un dato de una persona (P3).

3. **Asistente** (SUP-U05-20). `GET /loop/assistant-requests/{id}` sigue respondiendo `400
   loop_assistant_output_rejected` (A-68) y añade `rejection: {code}` **solo con el motivo** cuando
   la salida la rechazó uno de los dos validadores. Ni `term` ni `suggestion`: saldrían del texto
   rechazado, que nunca se guarda (A-68, TASK-316, PR-LAZ-20). Para eso el consumidor guarda el
   motivo cerrado en `loop.assistant_request.rejection_reason` (`laz_0041`): un cierre más del
   guardián ⛓, nulo fuera de `rejected_by_validators`. Los otros motivos (`schema_invalid`,
   `citation_outside_list`, `no_evidence`, `free_text_policy`) se guardan pero no se muestran.

## Lo que no cambia

- `code`, `detail_code`, `message_es`, `correlation_id` y `retry_after_seconds`; los estados HTTP de
  A-68; la lista cerrada `api_error_code`.
- La pista nunca llega al expediente, a la auditoría ni al registro: `FreeTextHint` y
  `LedgerRejection` no muestran el término en `repr`, y la pista no cuenta para la igualdad del
  rechazo.
- La pantalla SCR-11 (VIG-314) ya lee esta forma por lectura tolerante; no cambia.
- Compatibilidad N-1: la imagen anterior no escribe `rejection_reason` y su rechazo sale sin
  `rejection`, como antes.

## Defecto encontrado al aplicarla

El cierre firmado (SUP-U05-11) respondía `500 internal_error` cuando el escritor rechazaba
`closure_description` o `verification_note` con uno de los dos validadores: la ruta no traducía el
rechazo. Ahora responde `400` con su `detail_code` y `rejection`, como la reapertura.
