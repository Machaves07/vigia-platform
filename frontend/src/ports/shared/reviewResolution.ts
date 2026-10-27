/**
 * La resolución de la cola de revisión (T05 y T06), compartida por SCR-08 y SCR-09 (el historial
 * del hallazgo, VIG-359) y SCR-10 (el formulario de la cola). Las listas de motivos salen de las
 * etiquetas de U-04 (`labels.loop.es.json`): `review_discarded` junta las dos listas que en la
 * clasificación son salidas distintas (autorizada y falso positivo), como hace el servidor
 * (`REVIEW_REASONS_BY_OUTCOME` de `loop.application.review_queue`).
 */
import loop from '../../../../backend/resources/labels.loop.es.json';
import type { components } from '@/client/schema';

/** La resolución tal como la devuelve `GET /loop/findings/{id}/classifications`. */
export type ReviewResolutionItem = components['schemas']['ReviewResolutionItemOut'];

/** Las categorías de motivo de la salida, cada una con el nombre de su lista para `label()`. */
export function reviewReasonCategories(outcome: string) {
  const lists =
    outcome === 'review_confirmed'
      ? (['reason_category_confirmed'] as const)
      : outcome === 'review_discarded'
        ? (['reason_category_authorized', 'reason_category_false_positive'] as const)
        : [];
  return lists.flatMap((list) => Object.keys(loop[list]).map((value) => ({ list, value })));
}
