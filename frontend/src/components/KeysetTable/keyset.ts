/**
 * Paginación por clave de UI-14 (`PageCursor` de domain-entities §2.8, BR-APP-53, 57;
 * PAT-APP-ESC-03; PR-APP-15).
 *
 * - El cursor es opaco: los parámetros de consulta que la unidad dueña devuelve para la página
 *   siguiente (`cursor` en U-04; `after_received_at` y `after_record_id` en U-02). La tabla
 *   nunca lo construye: solo reenvía el último que recibió. No existe desplazamiento numérico,
 *   «ir a la página N» ni total calculado en el cliente; el total solo se muestra si el servidor
 *   lo devuelve.
 * - Página de 50 por omisión `[objetivo propio]` y máximo 200.
 * - Los filtros se aplican en bloque: cambiar el borrador no pide nada; aplicar vuelve a la
 *   primera página con los filtros nuevos.
 * - La lista se da por recorrida cuando el servidor no devuelve cursor siguiente (no cuando una
 *   página llega corta: así no se omite nada si el servidor acorta una página).
 * - Una respuesta de una petición ya sustituida (filtros nuevos, actualizar) se descarta.
 */

export const DEFAULT_PAGE_SIZE = 50;
export const MAX_PAGE_SIZE = 200;

/** Parámetros de consulta del cursor de la unidad dueña. */
export type KeysetCursor = Readonly<Record<string, string>>;

/** Filtros aplicados: campo → valor; los vacíos no se envían. */
export type KeysetFilters = Readonly<Record<string, string>>;

export interface KeysetRequest {
  /** `null` en la primera página. */
  readonly cursor: KeysetCursor | null;
  readonly pageSize: number;
  readonly filters: KeysetFilters;
}

export interface KeysetPage<Row> {
  readonly items: readonly Row[];
  /** `null` cuando no hay más páginas. */
  readonly nextCursor: KeysetCursor | null;
  /** Conteo total, solo si el servidor lo devuelve. */
  readonly total?: number | null;
}

export class KeysetContractError extends Error {
  override readonly name = 'KeysetContractError';
}

/** Claves de cursor admitidas: el cursor opaco de U-04 o las claves `after_*` de U-02. */
const CURSOR_KEY = /^(cursor|after_[a-z][a-z0-9_]*)$/;
/** Tope del cursor opaco de U-04 (`cursor` ≤ 128) aplicado a todo valor de cursor. */
const MAX_CURSOR_VALUE = 128;

/** Comprueba que un cursor es una clave de la unidad dueña y nunca un desplazamiento. */
export function assertKeysetCursor(cursor: unknown): KeysetCursor {
  if (typeof cursor !== 'object' || cursor === null || Array.isArray(cursor)) {
    throw new KeysetContractError('El cursor debe ser un objeto de parámetros de consulta.');
  }
  const entries = Object.entries(cursor);
  if (entries.length === 0) throw new KeysetContractError('El cursor no puede estar vacío.');
  for (const [key, value] of entries) {
    if (!CURSOR_KEY.test(key)) {
      throw new KeysetContractError(`Clave de cursor no admitida: «${key}».`);
    }
    if (typeof value !== 'string' || value === '' || value.length > MAX_CURSOR_VALUE) {
      throw new KeysetContractError(`Valor de cursor no admitido en «${key}».`);
    }
  }
  return Object.freeze({ ...(cursor as Record<string, string>) });
}

/** Página de `requested` acotada a 1…200; lo que no es un entero usa la de omisión. */
export function normalizePageSize(requested: number | undefined): number {
  if (requested === undefined || !Number.isSafeInteger(requested)) return DEFAULT_PAGE_SIZE;
  return Math.min(Math.max(requested, 1), MAX_PAGE_SIZE);
}

/** Filtros sin valores vacíos (espacios incluidos), en orden de clave. */
export function normalizeFilters(filters: KeysetFilters): KeysetFilters {
  const kept = Object.entries(filters)
    .filter(([, value]) => value.trim() !== '')
    .sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0));
  return Object.freeze(Object.fromEntries(kept));
}

type PendingKind = 'first' | 'more';

export interface KeysetState<Row> {
  readonly pageSize: number;
  readonly filters: KeysetFilters;
  readonly rows: readonly Row[];
  readonly nextCursor: KeysetCursor | null;
  readonly exhausted: boolean;
  readonly total: number | null;
  /** Petición en curso; `requestId` crece con cada una. */
  readonly pending: PendingKind | null;
  readonly requestId: number;
  readonly failed: boolean;
  /** Milisegundos del `Clock` de la última página recibida. */
  readonly fetchedAt: number | null;
}

export type KeysetEvent<Row> =
  | { readonly type: 'apply_filters'; readonly filters: KeysetFilters }
  | { readonly type: 'refresh' }
  | { readonly type: 'load_more' }
  | {
      readonly type: 'page_loaded';
      readonly requestId: number;
      readonly page: KeysetPage<Row>;
      readonly at: number;
    }
  | { readonly type: 'page_failed'; readonly requestId: number };

/** Estado inicial: pide la primera página con `filters`. */
export function createKeysetState<Row>(
  pageSize?: number,
  filters: KeysetFilters = {},
): KeysetState<Row> {
  return {
    pageSize: normalizePageSize(pageSize),
    filters: normalizeFilters(filters),
    rows: [],
    nextCursor: null,
    exhausted: false,
    total: null,
    pending: 'first',
    requestId: 1,
    failed: false,
    fetchedAt: null,
  };
}

/** La petición que corresponde a `state.pending`, o `null` si no hay ninguna. */
export function pendingRequest(
  state: Pick<KeysetState<unknown>, 'pending' | 'nextCursor' | 'pageSize' | 'filters'>,
): KeysetRequest | null {
  if (state.pending === null) return null;
  return {
    cursor: state.pending === 'more' ? state.nextCursor : null,
    pageSize: state.pageSize,
    filters: state.filters,
  };
}

export function canLoadMore<Row>(state: KeysetState<Row>): boolean {
  return state.pending === null && !state.exhausted && state.nextCursor !== null;
}

function restart<Row>(state: KeysetState<Row>, filters: KeysetFilters): KeysetState<Row> {
  return {
    ...state,
    filters,
    rows: [],
    nextCursor: null,
    exhausted: false,
    total: null,
    pending: 'first',
    requestId: state.requestId + 1,
    failed: false,
  };
}

function serverTotal(total: number | null | undefined): number | null {
  return typeof total === 'number' && Number.isSafeInteger(total) && total >= 0 ? total : null;
}

export function keysetReducer<Row>(
  state: KeysetState<Row>,
  event: KeysetEvent<Row>,
): KeysetState<Row> {
  switch (event.type) {
    case 'apply_filters':
      return restart(state, normalizeFilters(event.filters));
    case 'refresh':
      return restart(state, state.filters);
    case 'load_more':
      return canLoadMore(state)
        ? { ...state, pending: 'more', requestId: state.requestId + 1, failed: false }
        : state;
    case 'page_loaded': {
      if (state.pending === null || event.requestId !== state.requestId) return state;
      const nextCursor =
        event.page.nextCursor === null ? null : assertKeysetCursor(event.page.nextCursor);
      return {
        ...state,
        rows: state.pending === 'first' ? event.page.items : [...state.rows, ...event.page.items],
        nextCursor,
        exhausted: nextCursor === null,
        total: serverTotal(event.page.total),
        pending: null,
        failed: false,
        fetchedAt: event.at,
      };
    }
    case 'page_failed':
      if (state.pending === null || event.requestId !== state.requestId) return state;
      return { ...state, pending: null, failed: true };
  }
}
