/**
 * `FormDraft` en memoria (BR-APP-16, 19 y 20): el reductor nunca toca `localStorage`,
 * `sessionStorage` ni IndexedDB. Los espías fallan en cuanto se usan, también durante secuencias
 * de eventos generadas.
 */
import fc from 'fast-check';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import {
  createFormDraft,
  formDraftReducer,
  type FormDraft,
  type FormDraftEvent,
} from '@/state/reducers/formDraft';

import { PROPERTY_TIMEOUT_MS } from '../../support/timeouts';

const STORAGE_METHODS = ['getItem', 'setItem', 'removeItem', 'clear', 'key'] as const;
const forbidden = vi.fn((what: string) => {
  throw new Error(`BR-APP-19: el borrador tocó ${what}.`);
});

beforeEach(() => {
  forbidden.mockClear();
  for (const method of STORAGE_METHODS) {
    vi.spyOn(Storage.prototype, method).mockImplementation(() => forbidden(`Storage.${method}`));
  }
  const idb = {
    open: () => forbidden('indexedDB.open'),
    deleteDatabase: () => forbidden('indexedDB.deleteDatabase'),
    databases: () => forbidden('indexedDB.databases'),
  };
  vi.stubGlobal('indexedDB', idb);
});

afterEach(() => {
  expect(forbidden).not.toHaveBeenCalled();
});

const value = fc.oneof(
  fc.string(),
  fc.integer(),
  fc.boolean(),
  fc.constant(null),
  fc.array(fc.string(), { maxLength: 3 }),
);
const event: fc.Arbitrary<FormDraftEvent> = fc.oneof(
  fc
    .tuple(fc.constantFrom('reason', 'standard', 'remedy', 'notes'), value)
    .map(([field, v]) => ({ type: 'set_value', field, value: v }) as const),
  fc.integer({ min: -1, max: 8 }).map((index) => ({ type: 'set_step', index }) as const),
  fc.constantFrom('rca-0001', '').map((ref) => ({ type: 'server_progress', ref }) as const),
  fc.constantFrom(
    { type: 'saved' } as const,
    { type: 'lock' } as const,
    { type: 'restore' } as const,
    { type: 'discard' } as const,
  ),
);

describe('FormDraft', () => {
  it('los espías detectan un acceso al almacenamiento (control de la prueba)', () => {
    expect(() => {
      localStorage.setItem('x', 'y');
    }).toThrow(/BR-APP-19/);
    expect(() => sessionStorage.getItem('x')).toThrow(/BR-APP-19/);
    expect(() => indexedDB.open('x')).toThrow(/BR-APP-19/);
    forbidden.mockClear();
  });

  it(
    'ninguna secuencia de eventos usa el almacenamiento y dirty sigue a los cambios',
    () => {
      fc.assert(
        fc.property(fc.array(event, { maxLength: 40 }), (events) => {
          let draft: FormDraft = createFormDraft('scr11', 'root_cause');
          for (const e of events) {
            const before = draft;
            draft = formDraftReducer(before, e);
            if (e.type === 'set_value' && draft !== before) expect(draft.dirty).toBe(true);
            if (e.type === 'saved' || e.type === 'discard') expect(draft.dirty).toBe(false);
            if (e.type === 'lock' || e.type === 'restore') {
              // Bloquear y recuperar conservan lo escrito.
              expect(draft.values).toBe(before.values);
              expect(draft.dirty).toBe(before.dirty);
            }
          }
          expect(forbidden).not.toHaveBeenCalled();
        }),
      );
    },
    PROPERTY_TIMEOUT_MS,
  );

  it('escribir marca dirty; repetir el mismo valor no', () => {
    let draft = createFormDraft('scr09', 'classify');
    expect(draft.dirty).toBe(false);
    draft = formDraftReducer(draft, { type: 'set_value', field: 'reason', value: 'texto' });
    expect(draft).toMatchObject({ dirty: true, values: { reason: 'texto' } });
    const saved = formDraftReducer(draft, { type: 'saved' });
    expect(formDraftReducer(saved, { type: 'set_value', field: 'reason', value: 'texto' })).toBe(
      saved,
    );
    expect(formDraftReducer(saved, { type: 'set_value', field: 'other', value: null }).dirty).toBe(
      true,
    );
  });

  it('se conserva tras locked_for_reauth y se recupera al reautenticar (BR-APP-16)', () => {
    let draft = createFormDraft('scr11', 'root_cause', 'rca-0001');
    draft = formDraftReducer(draft, { type: 'set_value', field: 'notes', value: 'causa' });
    draft = formDraftReducer(draft, { type: 'set_step', index: 2 });
    const locked = formDraftReducer(draft, { type: 'lock' });
    expect(locked).toMatchObject({ preservedOnLock: true, dirty: true, stepIndex: 2 });
    const restored = formDraftReducer(locked, { type: 'restore' });
    expect(restored).toEqual({ ...draft, preservedOnLock: false });
  });

  it('descartar vacía el borrador y su referencia; el paso no admite índices inválidos', () => {
    let draft = createFormDraft('scr11', 'root_cause', 'rca-0001');
    draft = formDraftReducer(draft, { type: 'set_value', field: 'notes', value: 'x' });
    expect(formDraftReducer(draft, { type: 'discard' })).toEqual(
      createFormDraft('scr11', 'root_cause'),
    );
    expect(formDraftReducer(draft, { type: 'set_step', index: -1 })).toBe(draft);
    expect(formDraftReducer(draft, { type: 'set_step', index: 1.5 })).toBe(draft);
    expect(formDraftReducer(draft, { type: 'server_progress', ref: ' ' })).toBe(draft);
  });
});
