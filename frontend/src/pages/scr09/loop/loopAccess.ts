/**
 * Registros del lazo que ofrece cada estado del hallazgo (VIG-363, BR-APP-43). No es una regla
 * propia de la interfaz: es la tabla de transiciones de U-04 (§4.1) leída por los registros del
 * lazo que escribe cada una, y `tests/components/scr09/loop_access.test.tsx` la contrasta por
 * igualdad con la sección `lifecycle` de `backend/openapi/u05-fixtures.json`:
 *
 * - `action`: comprometer la acción (T07, registro `action_assigned` sin reapertura ni revisión);
 * - `closure`: evidencia y cierre firmado (T09, `closure`);
 * - `unclosable`: declarar el hallazgo incerrable (T10, `unclosable_state_declared`);
 * - `unclosable_review`: revisar la declaración en su fecha (T11 y T12, `unclosable_review`);
 * - `reopening`: reabrir (T13 y T14, `reopening`).
 *
 * Las clasificaciones (T03, T04 y T15 a T20) no son registros del lazo: se ofrecen en el detalle y
 * en «Clasificación del hallazgo», con su aviso de reclasificación (VIG-359).
 */
export type LoopAccess = 'action' | 'closure' | 'unclosable' | 'unclosable_review' | 'reopening';

export const LOOP_ACCESS: Readonly<Record<string, readonly LoopAccess[]>> = {
  received: [],
  in_review: [],
  discarded: [],
  open: ['action', 'unclosable'],
  action_assigned: ['closure', 'unclosable'],
  overdue: ['closure', 'unclosable'],
  suspended_by_reclassification: [],
  closed: ['reopening'],
  unclosable: ['unclosable_review', 'reopening'],
};

/**
 * El registro que el estado pide primero y la pantalla destaca: el incerrable se sostiene con la
 * revisión en la fecha comprometida y su medida compensatoria (respuesta 7 de U-04).
 */
export const LOOP_PRIMARY: Readonly<Record<string, LoopAccess>> = {
  unclosable: 'unclosable_review',
};

/** Los registros que admite el estado; ninguno si el estado no está en la lista cerrada. */
export function loopAccess(state: string): readonly LoopAccess[] {
  return Object.hasOwn(LOOP_ACCESS, state) ? (LOOP_ACCESS[state] ?? []) : [];
}

/** El registro destacado del estado, si lo hay. */
export function loopPrimary(state: string): LoopAccess | null {
  return Object.hasOwn(LOOP_PRIMARY, state) ? (LOOP_PRIMARY[state] ?? null) : null;
}

/** La revisión comprometida de un incerrable: fecha vigente y medida compensatoria declarada. */
export interface UnclosableCommitment {
  readonly reviewDueAt: string;
  readonly compensatoryMeasure: string;
}

interface RecordLike {
  readonly record_type: string;
  readonly chain_sequence: number;
  readonly content: unknown;
}

function field(content: unknown, name: string): unknown {
  return content !== null && typeof content === 'object' && !Array.isArray(content)
    ? (content as Record<string, unknown>)[name]
    : undefined;
}

/**
 * Lo que el expediente del hallazgo dice de su última declaración de incerrable, en el orden de la
 * cadena: la fecha de revisión que declaró, o la de su última renovación, y su medida
 * compensatoria. `null` sin declaración.
 */
export function unclosableCommitment(records: readonly RecordLike[]): UnclosableCommitment | null {
  let declared: string | null = null;
  let reviewDueAt = '';
  let compensatoryMeasure = '';
  for (const record of [...records].sort((a, b) => a.chain_sequence - b.chain_sequence)) {
    const declaration = field(record.content, 'declaration_id');
    if (typeof declaration !== 'string') continue;
    if (record.record_type === 'unclosable_state_declared') {
      const due = field(record.content, 'review_due_at');
      const measure = field(record.content, 'compensatory_measure');
      const complete = typeof due === 'string' && typeof measure === 'string';
      declared = complete ? declaration : null;
      reviewDueAt = complete ? due : '';
      compensatoryMeasure = complete ? measure : '';
    } else if (
      record.record_type === 'unclosable_review' &&
      declared === declaration &&
      field(record.content, 'outcome') === 'renewed'
    ) {
      const due = field(record.content, 'new_review_due_at');
      if (typeof due === 'string') reviewDueAt = due;
    }
  }
  return declared === null ? null : { reviewDueAt, compensatoryMeasure };
}
