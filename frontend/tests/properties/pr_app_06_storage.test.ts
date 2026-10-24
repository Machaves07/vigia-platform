/**
 * PR-APP-06 (BR-APP-19, LC-APP-07, NFR-APP-32): para toda secuencia de acciones de la persona
 * (filtrar, seleccionar planta, elegir columnas y pestañas, abrir un hallazgo, escribir un motivo,
 * cerrar sesión), lo que se escribe en el almacenamiento del navegador es solo la clave de
 * `UiPreferences`, su valor cumple el esquema cerrado y ningún nombre ni valor guardado es un
 * identificador de registro, un texto libre, un token ni una planta fuera del alcance; tras
 * cerrar sesión, el almacenamiento queda vacío.
 *
 * El oráculo del esquema es Ajv compilando `uiPreferences.schema.json` en la prueba,
 * independiente del validador autónomo que usa la aplicación. Las escrituras se observan con
 * espías sobre `Storage.prototype` (`localStorage` y `sessionStorage` reales de jsdom).
 */
import Ajv2020 from 'ajv/dist/2020.js';
import fc from 'fast-check';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import schema from '../../src/state/scope/uiPreferences.schema.json';
import { createFormDraft, formDraftReducer, type FormDraft } from '@/state/reducers/formDraft';
import { SCREENS, screenKey, type Screen } from '@/state/query';
import {
  createPreferencesStore,
  createScopeStore,
  PREFERENCES_KEY,
  type AvailablePlant,
  type PersistableFilterField,
} from '@/state/scope';

import { liveViewToken } from '../generators';
import { queryFixture } from '../support/queryCache';
import { PROPERTY_TIMEOUT_MS } from '../support/timeouts';

const oracle = new Ajv2020({ strict: true, allErrors: true }).compile(structuredClone(schema));

/** Valores de lista cerrada de los filtros de selección (los únicos que se pueden recordar). */
const CHOICES = ['open', 'in_progress', 'closed', 'degraded', 'not_observable'] as const;
const TOKEN = /^[a-z][a-z0-9_]{0,63}$/;

const COLUMNS = ['zone', 'received_at', 'status', 'severity'] as const;
const TABS = ['summary', 'evidence', 'labels'] as const;
/** Todo lo que sí puede aparecer guardado: nombres del esquema, pantallas, filtros y valores. */
const VOCABULARY = new Set<string>([
  ...CHOICES,
  ...COLUMNS,
  ...TABS,
  ...SCREENS,
  'all',
  'search',
  'from',
  'category',
  'note',
  'selected_plant_id',
  'list_filters',
  'columns',
  'active_tabs',
]);

const screen = fc.constantFrom(...SCREENS);
/** Texto libre: con forma de valor cerrado (lo más difícil de distinguir) o cualquiera. */
const freeText = fc
  .oneof(fc.stringMatching(/^[a-z][a-z0-9_]{2,20}$/), fc.string({ minLength: 1, maxLength: 60 }))
  .filter((value) => !VOCABULARY.has(value) && value.trim() !== '');
/** Texto libre con forma de valor cerrado: el esquema lo aceptaría, solo el filtro lo para. */
const freeToken = fc
  .stringMatching(/^[a-z][a-z0-9_]{2,20}$/)
  .filter((value) => !VOCABULARY.has(value));

type Action =
  | {
      readonly kind: 'filter';
      readonly screen: Screen;
      readonly status: (typeof CHOICES)[number] | '';
      readonly zone: string | null;
      readonly search: string;
      readonly from: string;
      /** Valor de una selección que no está entre sus opciones. */
      readonly undeclared: string;
      /** Valor de un campo de texto que declara opciones (y entre ellas, ese valor). */
      readonly note: string;
    }
  | { readonly kind: 'select_plant'; readonly plant: number | 'all' | 'outside' }
  | { readonly kind: 'columns'; readonly screen: Screen; readonly columns: readonly string[] }
  | { readonly kind: 'tab'; readonly screen: Screen; readonly tab: string }
  | { readonly kind: 'open_finding'; readonly screen: Screen; readonly findingId: string }
  | { readonly kind: 'zone'; readonly screen: Screen; readonly zoneId: string }
  | { readonly kind: 'write_reason'; readonly reason: string }
  | { readonly kind: 'token'; readonly token: string }
  | { readonly kind: 'reload' }
  | { readonly kind: 'logout' };

const action: fc.Arbitrary<Action> = fc.oneof(
  fc.record({
    kind: fc.constant('filter' as const),
    screen,
    status: fc.constantFrom<(typeof CHOICES)[number] | ''>(...CHOICES, ''),
    zone: fc.option(fc.uuid(), { nil: null }),
    search: freeText,
    from: fc.constantFrom('2026-10-01', '2026-01-31'),
    undeclared: freeToken,
    note: freeToken,
  }),
  fc.record({
    kind: fc.constant('select_plant' as const),
    plant: fc.oneof(
      fc.integer({ min: 0, max: 3 }),
      fc.constant('all' as const),
      fc.constant('outside' as const),
    ),
  }),
  fc.record({
    kind: fc.constant('columns' as const),
    screen,
    columns: fc.uniqueArray(fc.constantFrom(...COLUMNS), { maxLength: 4 }),
  }),
  fc.record({ kind: fc.constant('tab' as const), screen, tab: fc.constantFrom(...TABS) }),
  fc.record({ kind: fc.constant('open_finding' as const), screen, findingId: fc.uuid() }),
  fc.record({ kind: fc.constant('zone' as const), screen, zoneId: fc.uuid() }),
  fc.record({ kind: fc.constant('write_reason' as const), reason: freeText }),
  fc.record({ kind: fc.constant('token' as const), token: liveViewToken() }),
  fc.constant({ kind: 'reload' as const }),
  fc.constant({ kind: 'logout' as const }),
);

function plant(plantId: string, index: number): AvailablePlant {
  return {
    plant_id: plantId,
    code: `P${String(index)}`,
    name: `Planta ${String(index)}`,
    timezone: 'America/Bogota',
    country: 'CO',
  };
}

/** Nombres y valores de texto de un JSON, a cualquier profundidad. */
function leaves(value: unknown): string[] {
  if (typeof value === 'string') return [value];
  if (Array.isArray(value)) return value.flatMap(leaves);
  if (typeof value === 'object' && value !== null) {
    return Object.entries(value).flatMap(([key, item]) => [key, ...leaves(item)]);
  }
  return [];
}

interface Write {
  readonly area: 'local' | 'session';
  readonly key: string;
  readonly value: string;
}

let writes: Write[] = [];
/** La escritura real, por debajo del espía (y para sembrar lo «guardado a mano»). */
const realSetItem = Object.getOwnPropertyDescriptor(Storage.prototype, 'setItem')?.value as (
  this: Storage,
  key: string,
  value: string,
) => void;

beforeEach(() => {
  localStorage.clear();
  sessionStorage.clear();
  writes = [];
  vi.spyOn(Storage.prototype, 'setItem').mockImplementation(function (
    this: Storage,
    key: string,
    value: string,
  ) {
    writes.push({ area: this === localStorage ? 'local' : 'session', key, value });
    realSetItem.call(this, key, value);
  });
});

afterEach(() => {
  localStorage.clear();
  sessionStorage.clear();
});

describe('PR-APP-06: el almacenamiento solo recibe UiPreferences', () => {
  it(
    'para toda secuencia de acciones, cada escritura es UiPreferences sin registros, texto libre ni tokens; tras cerrar sesión queda vacío',
    () => {
      fc.assert(
        fc.property(
          // Cuatro plantas del alcance y una quinta fuera de él, distinta de todas.
          fc.uniqueArray(fc.uuid(), { minLength: 5, maxLength: 5 }),
          fc.array(action, { minLength: 1, maxLength: 25 }),
          ([outsidePlant = '', ...plantIds], actions) => {
            localStorage.clear();
            sessionStorage.clear();
            writes = [];
            const plants = plantIds.map(plant);
            const { cache } = queryFixture();
            let preferences = createPreferencesStore(localStorage);
            let scope = createScopeStore(preferences);
            scope.setAvailablePlants(plants);
            let draft: FormDraft = createFormDraft('scr09', 'closure');
            /** Lo que nunca puede aparecer en el almacenamiento. */
            const forbidden = new Set<string>([outsidePlant]);

            for (const step of actions) {
              switch (step.kind) {
                case 'filter': {
                  const fields: PersistableFilterField[] = [
                    {
                      name: 'status',
                      kind: 'select',
                      options: CHOICES.map((value) => ({ value })),
                    },
                    // Una selección cuyas opciones son UUID (zonas) no se recuerda.
                    ...(step.zone === null
                      ? []
                      : [{ name: 'zone', kind: 'select', options: [{ value: step.zone }] }]),
                    // Selección con un valor que no es ninguna de sus opciones.
                    {
                      name: 'category',
                      kind: 'select',
                      options: CHOICES.map((value) => ({ value })),
                    },
                    { name: 'search', kind: 'text' },
                    // Texto libre aunque declare opciones: solo se recuerda `kind: 'select'`.
                    { name: 'note', kind: 'text', options: [{ value: step.note }] },
                    { name: 'from', kind: 'date' },
                  ];
                  forbidden.add(step.search);
                  forbidden.add(step.from);
                  forbidden.add(step.undeclared);
                  forbidden.add(step.note);
                  if (step.zone !== null) forbidden.add(step.zone);
                  preferences.rememberListFilters(step.screen, fields, {
                    ...(step.status === '' ? {} : { status: step.status }),
                    ...(step.zone === null ? {} : { zone: step.zone }),
                    category: step.undeclared,
                    search: step.search,
                    note: step.note,
                    from: step.from,
                  });
                  break;
                }
                case 'select_plant':
                  scope.selectPlant(
                    step.plant === 'all'
                      ? 'all'
                      : step.plant === 'outside'
                        ? outsidePlant
                        : (plantIds[step.plant] ?? 'all'),
                  );
                  break;
                case 'columns':
                  preferences.setColumns(step.screen, step.columns);
                  break;
                case 'tab':
                  preferences.setActiveTab(step.screen, step.tab);
                  break;
                case 'open_finding':
                  forbidden.add(step.findingId);
                  cache.client.setQueryData(
                    screenKey({
                      screen: step.screen,
                      operation: 'GET /loop/findings/{finding_state_id}',
                      scope: scope.getSnapshot().plantId,
                      concession: null,
                      params: { finding_state_id: step.findingId },
                    }),
                    { finding_state_id: step.findingId },
                  );
                  break;
                case 'zone':
                  forbidden.add(step.zoneId);
                  scope.setZone(step.screen, step.zoneId);
                  break;
                case 'write_reason':
                  forbidden.add(step.reason);
                  draft = formDraftReducer(draft, {
                    type: 'set_value',
                    field: 'reason',
                    value: step.reason,
                  });
                  break;
                case 'token':
                  forbidden.add(step.token);
                  break;
                case 'reload':
                  // Otra sesión de la misma persona: lo guardado se relee y se revalida.
                  preferences = createPreferencesStore(localStorage);
                  scope = createScopeStore(preferences);
                  scope.setAvailablePlants(plants);
                  break;
                case 'logout':
                  preferences.clear();
                  scope.reset();
                  cache.clearAll();
                  draft = formDraftReducer(draft, { type: 'discard' });
                  expect(localStorage.length).toBe(0);
                  expect(sessionStorage.length).toBe(0);
                  scope.setAvailablePlants(plants);
                  break;
              }
            }
            expect(draft.screenId).toBe('scr09');

            for (const write of writes) {
              expect(write.area).toBe('local');
              expect(write.key).toBe(PREFERENCES_KEY);
              const value: unknown = JSON.parse(write.value);
              expect(oracle(value), JSON.stringify(oracle.errors)).toBe(true);
              for (const leaf of leaves(value)) {
                expect(forbidden.has(leaf), leaf).toBe(false);
                // Solo la planta seleccionada es un UUID, y siempre una del alcance.
                if (!TOKEN.test(leaf) && leaf !== 'all') expect(plantIds).toContain(leaf);
              }
            }
            const stored = localStorage.getItem(PREFERENCES_KEY);
            expect(localStorage.length).toBe(stored === null ? 0 : 1);
            expect(sessionStorage.length).toBe(0);

            preferences.clear();
            expect(localStorage.length).toBe(0);
            expect(sessionStorage.length).toBe(0);
            cache.dispose();
          },
        ),
      );
    },
    PROPERTY_TIMEOUT_MS,
  );

  it('una escritura directa que no cumple el esquema no se escribe ni sustituye lo vigente', () => {
    fc.assert(
      fc.property(
        fc.oneof(
          fc.record({
            kind: fc.constant('plant' as const),
            value: fc.oneof(freeText, liveViewToken()),
          }),
          fc.record({ kind: fc.constant('tab' as const), value: fc.oneof(freeText, fc.uuid()) }),
          fc.record({
            kind: fc.constant('columns' as const),
            value: fc.oneof(freeText, fc.uuid()),
          }),
        ),
        (attempt) => {
          fc.pre(!TOKEN.test(attempt.value) || attempt.kind === 'plant');
          localStorage.clear();
          writes = [];
          const preferences = createPreferencesStore(localStorage);
          preferences.setActiveTab('scr08', 'summary');
          const before = preferences.read();
          if (attempt.kind === 'plant') preferences.selectPlant(attempt.value);
          if (attempt.kind === 'tab') preferences.setActiveTab('scr08', attempt.value);
          if (attempt.kind === 'columns') preferences.setColumns('scr08', [attempt.value]);
          expect(writes).toHaveLength(1);
          expect(preferences.read()).toBe(before);
          expect(JSON.parse(localStorage.getItem(PREFERENCES_KEY) ?? 'null')).toEqual({
            active_tabs: { scr08: 'summary' },
          });
        },
      ),
    );
  });

  it('lo guardado a mano que no cumple el esquema se descarta y se borra al leerlo', () => {
    fc.assert(
      fc.property(
        fc.oneof(
          fc.record({ selected_plant_id: fc.uuid(), finding_state_id: fc.uuid() }),
          fc.record({ list_filters: fc.record({ scr08: fc.record({ search: freeText }) }) }),
          fc.record({ token: liveViewToken() }),
          fc.jsonValue(),
        ),
        (stored) => {
          fc.pre(!oracle(stored));
          localStorage.clear();
          realSetItem.call(localStorage, PREFERENCES_KEY, JSON.stringify(stored));
          const preferences = createPreferencesStore(localStorage);
          expect(preferences.read()).toEqual({});
          expect(localStorage.getItem(PREFERENCES_KEY)).toBeNull();
        },
      ),
    );
  });

  it('el validador autónomo y el oráculo dan el mismo veredicto, y el esquema nombra las pantallas de state.query', () => {
    expect(schema.$defs.screen.enum).toEqual([...SCREENS]);
    fc.assert(
      fc.property(
        fc.record(
          {
            selected_plant_id: fc.oneof(fc.uuid(), fc.constant('all'), fc.string()),
            list_filters: fc.dictionary(
              fc.oneof(screen, fc.string()),
              fc.dictionary(fc.oneof(fc.constantFrom(...CHOICES), fc.string()), fc.string()),
            ),
            columns: fc.dictionary(screen, fc.array(fc.constantFrom(...CHOICES, 'A b'))),
            active_tabs: fc.dictionary(screen, fc.oneof(fc.constantFrom(...CHOICES), fc.uuid())),
            extra: fc.string(),
          },
          { requiredKeys: [] },
        ),
        (value) => {
          const preferences = createPreferencesStore(null);
          expect(preferences.read()).toEqual({});
          const accepted = oracle(value);
          localStorage.clear();
          realSetItem.call(localStorage, PREFERENCES_KEY, JSON.stringify(value));
          expect(createPreferencesStore(localStorage).read()).toEqual(accepted ? value : {});
        },
      ),
    );
  });
});
