/**
 * Lo urgente del inicio del coordinador (sistema visual v1, principio 1; VIG-365, S3-02): de una
 * sola lectura de la bandeja salen las vencidas, las detecciones por revisar, lo que vence esta
 * semana y los hallazgos nuevos. No hay regla propia: el estado es el `lifecycle_state` que da el
 * servidor (la vencida la declara él, nunca se deduce de la fecha) y solo «vence esta semana» lee
 * el `due_at` de una acción asignada, como el chip.
 *
 * La compuerta manda como en la bandeja: el hallazgo de una zona no productiva (sin compuerta
 * leída, o con ella no productiva y sin revocación) no se muestra ni se cuenta (P9).
 */
import { daysUntil, DUE_SOON_DAYS } from '@/components/StatusChip/StatusChip';
import { gateDisplay, type Finding, type InboxPage, type ZoneGate } from '@/ports/scr08Findings';

/** Hallazgos nuevos que el inicio lista. */
const NEW_SHOWN = 5;

/** Páginas de 50 de la bandeja que el inicio recorre para contar (200 hallazgos). */
export const HOME_PAGES = 4;

export interface HomeSummary {
  readonly overdue: readonly Finding[];
  readonly inReview: readonly Finding[];
  readonly dueThisWeek: readonly Finding[];
  /** Los más recientes primero: recibidos y en revisión. */
  readonly recent: readonly Finding[];
  /** Hallazgos de la bandeja que se pueden mostrar. */
  readonly total: number;
  /** `true` si la bandeja tiene más de lo leído: los recuentos son un mínimo. */
  readonly partial: boolean;
}

/** `true` si la fila del hallazgo se muestra: zona productiva o acuerdo revocado (como la bandeja). */
function shownFinding(finding: Finding, gates: readonly ZoneGate[]): boolean {
  if (finding.notices.some((notice) => notice.code === 'agreement_revoked')) return true;
  const status = gates.find((gate) => gate.zone_id === finding.zone_id);
  return status !== undefined && gateDisplay(status.gate, status.regression).state === 'productive';
}

export function homeSummary(page: InboxPage, gates: readonly ZoneGate[], now: number): HomeSummary {
  const shown = page.items.filter((finding) => shownFinding(finding, gates));
  const of = (...states: readonly string[]) =>
    shown.filter((finding) => states.includes(finding.lifecycle_state));
  const dueThisWeek = of('action_assigned').filter((finding) => {
    const days = finding.due_at === null ? null : daysUntil(finding.due_at, now);
    return days !== null && days <= DUE_SOON_DAYS;
  });
  const recent = [...of('received', 'in_review')]
    .sort((a, b) => Date.parse(b.occurred_at) - Date.parse(a.occurred_at))
    .slice(0, NEW_SHOWN);
  return {
    overdue: of('overdue'),
    inReview: of('in_review'),
    dueThisWeek,
    recent,
    total: shown.length,
    partial: page.next_cursor !== null,
  };
}
