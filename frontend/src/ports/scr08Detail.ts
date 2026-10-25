/**
 * Puerto de SCR-08 para el detalle auditado del hallazgo y la pestaña de etiquetas (IF-08 pasos 1
 * y 2; SUP-U05-02 y 04; RV-12). Todas las lecturas son auditadas (`on_demand` en `state.query`):
 * se piden al abrir el detalle o al consultar las etiquetas, nunca por refresco.
 *
 * - El `Finding` del contrato sale de `GET /ledger/records/{record_id}` y se valida con el Ajv de
 *   `@vigia/contracts` antes de pintarse (BR-APP-64): si no cumple, solo llega el error de
 *   contrato, nunca el objeto.
 * - Degradación declarada (A-64 de U-04): sin compuerta legible el detalle responde
 *   `503 loop_gate_state_unavailable` y sin texto del estándar, `standard_text_unavailable` (aviso
 *   del detalle o `503 loop_standard_text_unavailable`). Ninguno de los dos abre el circuito
 *   `platform` (`counts_for_circuit`): degradan su sección y el resto se pinta.
 */
import loop from '../../../backend/resources/labels.loop.es.json';
import type { components } from '@/client/schema';
import type { Finding as ContractFinding } from '@/contracts';
import { validateContractFinding, type ContractViolation } from '@/ports/validation';
import { defineScreenPort, isPortError } from './base';
import { currentTransport } from './base/http';

export type FindingDetail = components['schemas']['FindingDetailOut'];
export type DetailRecord = components['schemas']['RecordOut'];
export type DetailClip = components['schemas']['ClipOut'];
export type DetailNotice = components['schemas']['DetailNoticeOut'];
export type ClassificationHistory = components['schemas']['ClassificationHistoryOut'];
export type ClassificationItem = components['schemas']['ClassificationItemOut'];
export type CoverageNow = components['schemas']['CoverageStatusOut'];
export type Label = components['schemas']['LabelOut'];
export type LabelPage = components['schemas']['LabelPageOut'];
export type Plant = components['schemas']['PlantView'];
export type { ContractFinding, ContractViolation };

export const detailPort = defineScreenPort('SCR-08', {
  hierarchy: {
    kind: 'read',
    status: 'fixed',
    operationId: 'hierarchy_hierarchy_get',
    permission: 'session',
  },
  detail: {
    kind: 'read',
    status: 'fixed',
    operationId: 'get_finding_loop_findings__finding_state_id__get',
    permission: 'findings.read',
  },
  history: {
    kind: 'read',
    status: 'fixed',
    operationId: 'list_classifications_loop_findings__finding_state_id__classifications_get',
    permission: 'findings.read',
  },
  record: {
    kind: 'read',
    status: 'fixed',
    operationId: 'get_record_ledger_records__record_id__get',
    permission: 'findings.read',
  },
  coverageNow: {
    kind: 'read',
    status: 'fixed',
    operationId: 'status_at_zones__zone_id__coverage_at_get',
    permission: 'coverage.read',
  },
  labels: {
    kind: 'read',
    status: 'fixed',
    operationId: 'list_labels_labels_get',
    permission: 'labels.read',
  },
});

export function detailClock() {
  return currentTransport().clock;
}

/** Tipo de registro del expediente que lleva un `Finding` del contrato (`RecordKind` de U-01). */
const FINDING_RECORD = 'finding';

/** Un registro `finding` del expediente: su `Finding` validado o su error de contrato. */
export type FindingRecord =
  | { readonly record_id: string; readonly ok: true; readonly finding: ContractFinding }
  | { readonly record_id: string; readonly ok: false; readonly error: ContractViolation };

/**
 * Lee del expediente los registros `finding` del detalle, uno a uno y en el orden del detalle, y
 * valida su contenido contra `finding.schema.json`. El objeto que no cumple no se devuelve.
 */
export async function readFindingRecords(
  records: readonly DetailRecord[],
  signal: AbortSignal,
): Promise<FindingRecord[]> {
  const result: FindingRecord[] = [];
  for (const record of records) {
    if (record.record_type !== FINDING_RECORD) continue;
    const ledger = await detailPort.record.read({ path: { record_id: record.record_id } }, signal);
    const check = validateContractFinding(ledger.content);
    result.push(
      check.ok
        ? { record_id: record.record_id, ok: true, finding: check.value }
        : { record_id: record.record_id, ok: false, error: check.error },
    );
  }
  return result;
}

export type DetailFailure =
  | 'gate_state_unavailable'
  | 'standard_text_unavailable'
  | 'unavailable'
  | 'not_available_in_scope'
  | 'error';

/** Estado de una sección ante un fallo de su lectura. */
export function detailFailure(error: Error): DetailFailure {
  if (!isPortError(error)) return 'error';
  const failure = error.failure;
  if (failure.kind === 'offline' || failure.kind === 'unavailable') return 'unavailable';
  if (failure.kind !== 'http') return 'error';
  const { behaviour } = failure;
  if (behaviour.kind === 'degraded_section' && behaviour.section === 'gate_state')
    return 'gate_state_unavailable';
  if (behaviour.kind === 'degraded_section' && behaviour.section === 'standard_text')
    return 'standard_text_unavailable';
  if (behaviour.kind === 'not_available_in_scope') return 'not_available_in_scope';
  if (behaviour.kind === 'degraded_section' || behaviour.kind === 'transient') return 'unavailable';
  return 'error';
}

/** Listas de categorías de motivo por salida (DE §1 de U-04), para `label()`. */
const REASON_LISTS: Readonly<Record<string, string>> = {
  confirmed: 'reason_category_confirmed',
  authorized_operation: 'reason_category_authorized',
  false_positive: 'reason_category_false_positive',
};

/** Nombre de la lista de categorías de motivo de una salida, o `null` si no la tiene. */
export function reasonList(outcome: string): string | null {
  return Object.hasOwn(REASON_LISTS, outcome) ? (REASON_LISTS[outcome] ?? null) : null;
}

/** Salidas de `classification_outcome`, en el orden de la etiqueta de U-04. */
export const OUTCOMES: readonly string[] = Object.keys(loop.classification_outcome);

/** Todas las categorías de motivo con su lista, sin repetir (filtro de etiquetas). */
export const REASON_CATEGORIES: readonly { list: string; value: string }[] = Object.values(
  REASON_LISTS,
).flatMap((list) => Object.keys(loop[list as keyof typeof loop]).map((value) => ({ list, value })));

export interface LabelFilters {
  readonly from: string;
  readonly to: string;
  readonly zone_id?: string;
  readonly family?: string;
  readonly reason_category?: string;
}

/** Una página de etiquetas con el cursor opaco del servidor (`after_labeled_at`, `after_label_id`). */
export async function readLabels(
  filters: LabelFilters,
  cursor: Readonly<Record<string, string>> | null,
  pageSize: number,
  signal: AbortSignal,
) {
  const page = await detailPort.labels.read(
    {
      query: {
        ...filters,
        page_size: pageSize,
        ...(cursor === null
          ? {}
          : { after_labeled_at: cursor.after_labeled_at, after_label_id: cursor.after_label_id }),
      },
    },
    signal,
  );
  return {
    items: page.items,
    nextCursor:
      page.next_cursor === null
        ? null
        : {
            after_labeled_at: page.next_cursor.labeled_at,
            after_label_id: page.next_cursor.label_id,
          },
  };
}
