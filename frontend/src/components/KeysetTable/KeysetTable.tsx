/**
 * UI-14 `KeysetTable`: lista paginada por clave con «Cargar más» (BR-APP-53, PAT-APP-ESC-03),
 * filtros aplicados en bloque con el botón o Intro y nunca por tecla (BR-APP-57), `RefreshIndicator`
 * en la cabecera (UI-20) y ranura del conteo de vencidos, que la pantalla muestra con cualquier
 * filtro activo (BR-APP-25). La tabla no calcula totales: solo muestra el que dé el servidor.
 *
 * Teclado: las filas forman un grupo con un solo punto de tabulación; flechas arriba y abajo,
 * Inicio y Fin mueven el foco entre filas, e Intro activa la fila si la pantalla lo pide.
 *
 * Marcas de rendimiento (NFR-APP-03, las mide VIG-330): `keyset_table.page_received` al llegar
 * una página y `keyset_table.rows_committed` cuando sus filas están en el DOM, ambas con la hora
 * del `Clock` inyectado, y la medida `keyset_table.render` entre las dos.
 */
import { useEffect, useId, useLayoutEffect, useMemo, useReducer, useRef, useState } from 'react';
import type { KeyboardEvent, ReactNode, SubmitEvent } from 'react';

import { RefreshIndicator } from '@/components/RefreshIndicator/RefreshIndicator';
import { LoadingSkeleton } from '@/components/RefreshIndicator/LoadingSkeleton';
import type { Clock } from '@/state/clock';
import { text } from '@/texts';

import {
  assertKeysetCursor,
  canLoadMore,
  createKeysetState,
  keysetReducer,
  pendingRequest,
  type KeysetFilters,
  type KeysetPage,
  type KeysetRequest,
} from './keyset';
import styles from './KeysetTable.module.css';

export interface KeysetColumn<Row> {
  readonly id: string;
  /** Texto de la cabecera, ya resuelto desde el catálogo por la pantalla. */
  readonly header: string;
  readonly cell: (row: Row) => ReactNode;
}

interface FilterOption {
  readonly value: string;
  readonly label: string;
}

export type KeysetFilterField =
  | { readonly name: string; readonly label: string; readonly kind: 'text' | 'date' }
  | {
      readonly name: string;
      readonly label: string;
      readonly kind: 'select';
      readonly options: readonly FilterOption[];
    };

export interface KeysetTableProps<Row> {
  /** Título accesible de la lista. */
  readonly caption: string;
  readonly columns: readonly KeysetColumn<Row>[];
  readonly rowKey: (row: Row) => string;
  /** Puerto de la pantalla: pide una página con el cursor de la unidad dueña. */
  readonly fetchPage: (request: KeysetRequest, signal: AbortSignal) => Promise<KeysetPage<Row>>;
  readonly clock: Clock;
  readonly pageSize?: number;
  readonly filterFields?: readonly KeysetFilterField[];
  readonly initialFilters?: KeysetFilters;
  /** Conteo de vencidos de la cabecera (BR-APP-25); `undefined` si la lista no lo tiene. */
  readonly overdueCount?: number;
  readonly refreshPolicy?: 'on_demand' | 'periodic_60s';
  /** Texto de la lista vacía, específico de la pantalla. */
  readonly emptyText?: string;
  readonly onRowActivate?: (row: Row) => void;
  readonly onFiltersApplied?: (filters: KeysetFilters) => void;
  /**
   * En tableta (≤ 960 px), cada fila como una tarjeta con la cabecera de cada dato encima
   * (sistema visual v1, VIG-364): las acciones quedan a la vista sin desplazar en horizontal. La
   * tabla conserva su semántica (`role` explícito) aunque cambie su disposición.
   */
  readonly cardsOnTablet?: boolean;
}

const PAGE_RECEIVED = 'keyset_table.page_received';
const ROWS_COMMITTED = 'keyset_table.rows_committed';
const RENDER_MEASURE = 'keyset_table.render';

function mark(name: string, startTime: number, rows: number): void {
  if (typeof performance === 'undefined' || typeof performance.mark !== 'function') return;
  performance.mark(name, { startTime, detail: { rows } });
}

function FilterBar({
  fields,
  applied,
  onApply,
}: {
  fields: readonly KeysetFilterField[];
  applied: KeysetFilters;
  onApply: (filters: KeysetFilters) => void;
}) {
  const [draft, setDraft] = useState<KeysetFilters>(applied);
  const prefix = useId();
  const submit = (event: SubmitEvent<HTMLFormElement>) => {
    event.preventDefault();
    onApply(draft);
  };
  const set = (name: string, value: string) => {
    setDraft((current) => ({ ...current, [name]: value }));
  };
  return (
    <form
      className={styles.filters}
      role="search"
      aria-label={text('components_structure.keyset.filters_label')}
      onSubmit={submit}
    >
      {fields.map((field) => {
        const id = `${prefix}-${field.name}`;
        const value = draft[field.name] ?? '';
        return (
          <div className={styles.field} key={field.name}>
            <label htmlFor={id}>{field.label}</label>
            {field.kind === 'select' ? (
              <select
                id={id}
                value={value}
                onChange={(event) => {
                  set(field.name, event.target.value);
                }}
              >
                <option value="">{text('components_structure.keyset.filter_any')}</option>
                {field.options.map((option) => (
                  <option key={option.value} value={option.value}>
                    {option.label}
                  </option>
                ))}
              </select>
            ) : (
              <input
                id={id}
                type={field.kind}
                value={value}
                onChange={(event) => {
                  set(field.name, event.target.value);
                }}
              />
            )}
          </div>
        );
      })}
      <button type="submit" className={styles.button}>
        {text('components_structure.keyset.apply_filters')}
      </button>
    </form>
  );
}

export function KeysetTable<Row>({
  caption,
  columns,
  rowKey,
  fetchPage,
  clock,
  pageSize,
  filterFields = [],
  initialFilters = {},
  overdueCount,
  refreshPolicy = 'on_demand',
  emptyText,
  onRowActivate,
  onFiltersApplied,
  cardsOnTablet = false,
}: KeysetTableProps<Row>) {
  const [state, dispatch] = useReducer(keysetReducer<Row>, undefined, () =>
    createKeysetState<Row>(pageSize, initialFilters),
  );
  const [focusIndex, setFocusIndex] = useState(0);
  const rowRefs = useRef<(HTMLTableRowElement | null)[]>([]);
  const fetchRef = useRef(fetchPage);
  const headingId = useId();

  useLayoutEffect(() => {
    fetchRef.current = fetchPage;
  }, [fetchPage]);

  const { requestId, pending, nextCursor, pageSize: size, filters } = state;
  const request = useMemo(
    () => pendingRequest({ pending, nextCursor, pageSize: size, filters }),
    [pending, nextCursor, size, filters],
  );

  useEffect(() => {
    if (request === null) return undefined;
    const pendingKey = requestId;
    const controller = new AbortController();
    fetchRef.current(request, controller.signal).then(
      (page) => {
        if (controller.signal.aborted) return;
        try {
          if (page.nextCursor !== null) assertKeysetCursor(page.nextCursor);
        } catch {
          dispatch({ type: 'page_failed', requestId: pendingKey });
          return;
        }
        const at = clock.now();
        mark(PAGE_RECEIVED, at, page.items.length);
        dispatch({ type: 'page_loaded', requestId: pendingKey, page, at });
      },
      () => {
        if (!controller.signal.aborted) dispatch({ type: 'page_failed', requestId: pendingKey });
      },
    );
    return () => {
      controller.abort();
    };
  }, [request, requestId, clock]);

  const { fetchedAt, rows } = state;
  useLayoutEffect(() => {
    if (fetchedAt === null) return;
    const at = clock.now();
    mark(ROWS_COMMITTED, at, rows.length);
    if (typeof performance !== 'undefined' && typeof performance.measure === 'function') {
      performance.measure(RENDER_MEASURE, {
        start: fetchedAt,
        end: at,
        detail: { rows: rows.length },
      });
    }
  }, [fetchedAt, rows, clock]);

  const activeIndex = Math.min(focusIndex, Math.max(rows.length - 1, 0));

  const moveFocus = (index: number) => {
    const target = Math.min(Math.max(index, 0), rows.length - 1);
    setFocusIndex(target);
    rowRefs.current[target]?.focus();
  };

  const onRowKeyDown = (event: KeyboardEvent<HTMLTableRowElement>, index: number, row: Row) => {
    switch (event.key) {
      case 'ArrowDown':
        moveFocus(index + 1);
        break;
      case 'ArrowUp':
        moveFocus(index - 1);
        break;
      case 'Home':
        moveFocus(0);
        break;
      case 'End':
        moveFocus(rows.length - 1);
        break;
      case 'Enter':
        if (onRowActivate === undefined || event.target !== event.currentTarget) return;
        onRowActivate(row);
        break;
      default:
        return;
    }
    event.preventDefault();
  };

  const applyFilters = (filters: KeysetFilters) => {
    setFocusIndex(0);
    dispatch({ type: 'apply_filters', filters });
    onFiltersApplied?.(filters);
  };

  const loadingFirst = pending === 'first';

  return (
    <section
      className={styles.table}
      aria-labelledby={headingId}
      aria-busy={state.pending !== null}
    >
      <header className={styles.header}>
        <h2 id={headingId} className={styles.caption}>
          {caption}
        </h2>
        {overdueCount !== undefined && (
          <p className={styles.overdue} data-part="overdue">
            {text('components_structure.keyset.overdue')}: <strong>{overdueCount}</strong>
          </p>
        )}
        {state.total !== null && (
          <p className={styles.total} data-part="total">
            {text('components_structure.keyset.total')}: {state.total}
          </p>
        )}
        <RefreshIndicator
          fetchedAt={state.fetchedAt}
          refreshPolicy={refreshPolicy}
          isFetching={state.pending !== null}
          onRefresh={() => {
            setFocusIndex(0);
            dispatch({ type: 'refresh' });
          }}
          clock={clock}
        />
      </header>
      {filterFields.length > 0 && (
        <FilterBar fields={filterFields} applied={state.filters} onApply={applyFilters} />
      )}
      {loadingFirst ? (
        <LoadingSkeleton clock={clock} />
      ) : state.failed && rows.length === 0 ? (
        <p role="alert" data-part="error">
          {text('components_structure.keyset.error')}
        </p>
      ) : rows.length === 0 ? (
        <p data-part="empty">{emptyText ?? text('components_structure.keyset.empty')}</p>
      ) : (
        // La tabla va en su propia caja con desplazamiento horizontal: nunca desborda la página.
        <div className={styles.scroll} data-part="table_box">
          <table
            className={cardsOnTablet ? `${styles.grid ?? ''} ${styles.cards ?? ''}` : styles.grid}
            aria-labelledby={headingId}
            role={cardsOnTablet ? 'table' : undefined}
          >
            <thead role={cardsOnTablet ? 'rowgroup' : undefined}>
              <tr role={cardsOnTablet ? 'row' : undefined}>
                {columns.map((column) => (
                  <th key={column.id} scope="col" role={cardsOnTablet ? 'columnheader' : undefined}>
                    {column.header}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody role={cardsOnTablet ? 'rowgroup' : undefined}>
              {rows.map((row, index) => (
                <tr
                  key={rowKey(row)}
                  ref={(element) => {
                    rowRefs.current[index] = element;
                  }}
                  className={styles.row}
                  role={cardsOnTablet ? 'row' : undefined}
                  tabIndex={index === activeIndex ? 0 : -1}
                  onFocus={() => {
                    setFocusIndex(index);
                  }}
                  onKeyDown={(event) => {
                    onRowKeyDown(event, index, row);
                  }}
                >
                  {columns.map((column) => (
                    <td
                      key={column.id}
                      role={cardsOnTablet ? 'cell' : undefined}
                      data-label={cardsOnTablet ? column.header : undefined}
                      data-column={column.id}
                    >
                      {column.cell(row)}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {!loadingFirst && rows.length > 0 && (
        <footer className={styles.footer}>
          {state.failed && (
            <p role="alert" data-part="error-more">
              {text('components_structure.keyset.error_more')}
            </p>
          )}
          {state.exhausted ? (
            <p data-part="end">{text('components_structure.keyset.end')}</p>
          ) : (
            <button
              type="button"
              className={styles.button}
              disabled={!canLoadMore(state)}
              onClick={() => {
                dispatch({ type: 'load_more' });
              }}
            >
              {text(
                state.pending === 'more'
                  ? 'components_structure.keyset.loading_more'
                  : 'components_structure.keyset.load_more',
              )}
            </button>
          )}
        </footer>
      )}
    </section>
  );
}
