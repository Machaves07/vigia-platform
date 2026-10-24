/**
 * PR-APP-15 (BR-APP-53): para toda secuencia de páginas con claves crecientes, la paginación por
 * cursor de UI-14 recorre cada elemento exactamente una vez, sin repetidos ni omitidos, con
 * página ≤ 200. El servidor simulado pagina por clave con los dos estilos de cursor (U-02:
 * `after_received_at` y `after_record_id`; U-04: `cursor` opaco), acorta páginas al azar y
 * rechaza todo parámetro que no sea un cursor suyo (un desplazamiento numérico, por ejemplo).
 */
import fc from 'fast-check';
import { describe, expect, it } from 'vitest';

import {
  assertKeysetCursor,
  canLoadMore,
  createKeysetState,
  DEFAULT_PAGE_SIZE,
  KeysetContractError,
  keysetReducer,
  MAX_PAGE_SIZE,
  normalizeFilters,
  normalizePageSize,
  pendingRequest,
  type KeysetCursor,
  type KeysetPage,
  type KeysetRequest,
  type KeysetState,
} from '@/components/KeysetTable/keyset';

import { PROPERTY_TIMEOUT_MS } from '../support/timeouts';

interface Item {
  readonly received_at: string;
  readonly record_id: string;
}

type CursorStyle = 'u02' | 'u04';

const BASE_MS = Date.UTC(2026, 9, 1);

/** Claves estrictamente crecientes por (`received_at`, `record_id`), con empates de instante. */
const items: fc.Arbitrary<Item[]> = fc
  .array(fc.tuple(fc.integer({ min: 0, max: 3 }), fc.boolean()), { maxLength: 450 })
  .map((steps) => {
    let ms = BASE_MS;
    return steps.map(([gap, sameInstant], index) => {
      if (!sameInstant || index === 0) ms += 1 + gap;
      return {
        received_at: new Date(ms).toISOString(),
        record_id: `00000000-0000-7000-8000-${String(index).padStart(12, '0')}`,
      };
    });
  });

function keyOf(item: Item): string {
  return `${item.received_at}|${item.record_id}`;
}

/** Servidor por clave: devuelve lo estrictamente posterior al cursor, hasta `pageSize`. */
function keysetServer(all: readonly Item[], style: CursorStyle, shorten: readonly number[]) {
  const requests: KeysetRequest[] = [];
  let call = 0;
  const encode = (item: Item): KeysetCursor =>
    style === 'u02'
      ? { after_received_at: item.received_at, after_record_id: item.record_id }
      : { cursor: btoa(keyOf(item)) };
  const decode = (cursor: KeysetCursor): string => {
    const keys = Object.keys(cursor).sort().join(',');
    if (style === 'u02' && keys === 'after_received_at,after_record_id') {
      return `${cursor.after_received_at ?? ''}|${cursor.after_record_id ?? ''}`;
    }
    if (style === 'u04' && keys === 'cursor') return atob(cursor.cursor ?? '');
    throw new Error(`Parámetros de cursor ajenos a la unidad: ${keys}`);
  };
  const serve = (request: KeysetRequest): KeysetPage<Item> => {
    requests.push(request);
    if (!Number.isSafeInteger(request.pageSize) || request.pageSize < 1) {
      throw new Error(`page_size fuera de rango: ${String(request.pageSize)}`);
    }
    if (request.pageSize > MAX_PAGE_SIZE) throw new Error('page_size > 200');
    const after = request.cursor === null ? null : decode(request.cursor);
    const start = after === null ? 0 : all.findIndex((item) => keyOf(item) > after);
    const from = start === -1 ? all.length : start;
    const cut = shorten[call++ % Math.max(shorten.length, 1)] ?? 0;
    const size = Math.max(1, request.pageSize - cut);
    const page = all.slice(from, from + size);
    const last = page.at(-1);
    const more = from + page.length < all.length;
    return { items: page, nextCursor: more && last !== undefined ? encode(last) : null };
  };
  return { serve, requests };
}

const style = fc.constantFrom<CursorStyle>('u02', 'u04');
const requestedSize = fc.oneof(
  fc.integer({ min: -5, max: 1000 }),
  fc.constantFrom(199, 200, 201, 0, 1, 50, Number.NaN, 2.5),
);
const shorten = fc.array(fc.integer({ min: 0, max: 60 }), { maxLength: 6 });

function walk(state: KeysetState<Item>, serve: (r: KeysetRequest) => KeysetPage<Item>) {
  let current = state;
  let guard = 0;
  for (;;) {
    const request = pendingRequest(current);
    if (request === null) break;
    if (guard++ > 10_000) throw new Error('El recorrido no termina.');
    current = keysetReducer(current, {
      type: 'page_loaded',
      requestId: current.requestId,
      page: serve(request),
      at: guard,
    });
    if (canLoadMore(current)) current = keysetReducer(current, { type: 'load_more' });
  }
  return current;
}

describe('PR-APP-15 paginación por clave', () => {
  it(
    'cada elemento aparece exactamente una vez, en orden, con página entre 1 y 200',
    () => {
      fc.assert(
        fc.property(items, style, requestedSize, shorten, (all, cursorStyle, size, cuts) => {
          const server = keysetServer(all, cursorStyle, cuts);
          const final = walk(createKeysetState<Item>(size), server.serve);

          expect(final.rows.map(keyOf)).toEqual(all.map(keyOf));
          expect(new Set(final.rows.map(keyOf)).size).toBe(final.rows.length);
          expect(final.exhausted).toBe(true);
          expect(final.total).toBeNull();
          for (const request of server.requests) {
            expect(request.pageSize).toBeGreaterThanOrEqual(1);
            expect(request.pageSize).toBeLessThanOrEqual(MAX_PAGE_SIZE);
            expect(Object.keys(request).sort()).toEqual(['cursor', 'filters', 'pageSize']);
            if (request.cursor !== null) assertKeysetCursor(request.cursor);
          }
        }),
      );
    },
    PROPERTY_TIMEOUT_MS,
  );

  it(
    'actualizar o filtrar a mitad del recorrido vuelve a la primera página y descarta respuestas viejas',
    () => {
      fc.assert(
        fc.property(
          items,
          style,
          fc.array(fc.constantFrom('more', 'refresh', 'filters', 'stale'), { maxLength: 12 }),
          (all, cursorStyle, actions) => {
            const server = keysetServer(all, cursorStyle, []);
            let state = createKeysetState<Item>(37);
            for (const action of actions) {
              const request = pendingRequest(state);
              if (action === 'stale' && request !== null) {
                const page = server.serve(request);
                const before = state;
                state = keysetReducer(state, { type: 'refresh' });
                const after = keysetReducer(state, {
                  type: 'page_loaded',
                  requestId: before.requestId,
                  page,
                  at: 1,
                });
                expect(after).toBe(state);
                continue;
              }
              if (request !== null) {
                state = keysetReducer(state, {
                  type: 'page_loaded',
                  requestId: state.requestId,
                  page: server.serve(request),
                  at: 1,
                });
              }
              if (action === 'more') state = keysetReducer(state, { type: 'load_more' });
              if (action === 'refresh') state = keysetReducer(state, { type: 'refresh' });
              if (action === 'filters') {
                state = keysetReducer(state, { type: 'apply_filters', filters: { zone_id: 'z' } });
                expect(state.rows).toEqual([]);
                expect(pendingRequest(state)?.cursor).toBeNull();
              }
            }
            const final = walk(state, server.serve);
            expect(final.rows.map(keyOf)).toEqual(all.map(keyOf));
          },
        ),
      );
    },
    PROPERTY_TIMEOUT_MS,
  );

  it('la página se acota a 1…200 y vale 50 por omisión', () => {
    fc.assert(
      fc.property(requestedSize, (size) => {
        const normalized = normalizePageSize(size);
        expect(normalized).toBeGreaterThanOrEqual(1);
        expect(normalized).toBeLessThanOrEqual(MAX_PAGE_SIZE);
        if (Number.isSafeInteger(size) && size >= 1 && size <= 200) expect(normalized).toBe(size);
      }),
    );
    expect(normalizePageSize(undefined)).toBe(DEFAULT_PAGE_SIZE);
    expect(DEFAULT_PAGE_SIZE).toBe(50);
    expect(normalizePageSize(200)).toBe(200);
    expect(normalizePageSize(201)).toBe(200);
  });

  it('ningún desplazamiento ni número de página pasa como cursor', () => {
    const offsetKey = fc.constantFrom('offset', 'page', 'skip', 'start', 'page_number', 'Cursor');
    fc.assert(
      fc.property(offsetKey, fc.nat().map(String), (key, value) => {
        expect(() => assertKeysetCursor({ [key]: value })).toThrow(KeysetContractError);
        expect(() => assertKeysetCursor({ cursor: 'x', [key]: value })).toThrow(
          KeysetContractError,
        );
      }),
    );
    for (const bad of [null, [], {}, { cursor: '' }, { cursor: 'x'.repeat(129) }, { cursor: 1 }]) {
      expect(() => assertKeysetCursor(bad)).toThrow(KeysetContractError);
    }
    expect(assertKeysetCursor({ cursor: 'x'.repeat(128) })).toEqual({ cursor: 'x'.repeat(128) });
  });

  it('una página con un cursor que no es de la unidad no se acepta', () => {
    const state = createKeysetState<Item>();
    expect(() =>
      keysetReducer(state, {
        type: 'page_loaded',
        requestId: state.requestId,
        page: { items: [], nextCursor: { offset: '50' } },
        at: 0,
      }),
    ).toThrow(KeysetContractError);
  });

  it('los filtros vacíos no se envían y su orden no importa', () => {
    fc.assert(
      fc.property(
        fc.dictionary(
          fc.constantFrom('zone_id', 'family', 'tier', 'state'),
          fc.constantFrom('', ' ', 'a', 'b'),
        ),
        (filters) => {
          const normalized = normalizeFilters(filters);
          for (const [key, value] of Object.entries(filters)) {
            expect(normalized[key]).toBe(value.trim() === '' ? undefined : value);
          }
          expect(Object.keys(normalized)).toEqual(Object.keys(normalized).sort());
        },
      ),
    );
  });
});
