/**
 * `parsePredicate` valida familia × predicado sin metadatos incrustados en `src/` (VIG-353). El
 * oráculo es el estándar declarado completo del catálogo sintético de la prueba, validado con el
 * Ajv del contrato: ambos deben aceptar y rechazar exactamente los mismos predicados.
 */
import fc from 'fast-check';
import { describe, expect, it } from 'vitest';
import { validateDeclaredStandard } from '@/contracts';
import { parsePredicate, predicateTemplate } from '@/ports/scr04Catalog';
import { payload, PREDICATE } from './catalogSupport';

const FAMILIES = ['coexistence', 'guard_bypass', 'dwell', 'startup_transition'] as const;

function oracle(family: string, predicate: unknown): boolean {
  return validateDeclaredStandard({ ...payload().standards[0], family, predicate });
}

function agrees(family: string, predicate: unknown) {
  const parsed = parsePredicate(family, JSON.stringify(predicate));
  expect(parsed !== null).toBe(oracle(family, predicate));
  if (parsed !== null) expect(parsed).toEqual(predicate);
}

const presence = { presence: true };
const condition = (signal_role: string, value: string) => ({ signal_role, value });
const CONDITIONS = [
  presence,
  { presence: false },
  condition('energy', 'asserted'),
  condition('energy', 'deasserted'),
  condition('guard', 'asserted'),
  condition('guard', 'deasserted'),
  condition('start_command', 'asserted'),
  condition('start_command', 'deasserted'),
  condition('person', 'asserted'),
  { ...presence, person_id: 'sintetico' },
];
const DURATIONS = [-1, 0, 1, 999, 1000, 60000, 60001, 3600000, 3600001, 2 ** 53, 0.5, '1000', null];

describe('parsePredicate frente al estándar completo (VIG-353)', () => {
  it.each(FAMILIES)('acepta la plantilla de %s, igual que el oráculo', (family) => {
    const predicate: unknown = JSON.parse(predicateTemplate(family));
    expect(oracle(family, predicate)).toBe(true);
    expect(parsePredicate(family, predicateTemplate(family))).toEqual(predicate);
  });

  it.each(FAMILIES)('rechaza en %s lo mismo que el oráculo en los bordes', (family) => {
    for (const first of CONDITIONS)
      for (const second of CONDITIONS)
        for (const duration of DURATIONS)
          agrees(family, { all_of: [first, second], min_duration_ms: duration });
    for (const third of CONDITIONS)
      agrees(family, {
        all_of: [presence, condition('guard', 'asserted'), third],
        min_duration_ms: 0,
      });
  });

  it.each(FAMILIES)(
    '%s: predicado incompleto o vacío, mismo veredicto que el oráculo',
    (family) => {
      const template = JSON.parse(predicateTemplate(family)) as Record<string, unknown>;
      for (const predicate of [
        {},
        { all_of: template.all_of },
        { min_duration_ms: template.min_duration_ms },
        { ...template, all_of: [] },
        { ...template, extra: true },
      ]) {
        expect(parsePredicate(family, JSON.stringify(predicate))).toBeNull();
        agrees(family, predicate);
      }
    },
  );

  it.each([
    ['dwell', {}],
    ['dwell', []],
    ['dwell', null],
    ['dwell', 'predicado'],
    ['dwell', 1000],
    ['dwell', { ...PREDICATE, person_id: 'identificador-sintetico' }],
    ['dwell', { all_of: [presence], min_duration_ms: 1000 }],
    ['dwell', { all_of: [presence, presence], min_duration_ms: 1000 }],
    ['dwell', { all_of: PREDICATE.all_of }],
    ['dwell', { min_duration_ms: 1000 }],
    ['dwell', { ...PREDICATE, all_of: [...PREDICATE.all_of, presence] }],
    ['dwell', { __proto__: null, ...PREDICATE }],
    ['dwell', { ...PREDICATE, standard_id: 'sintetico' }],
    ['unknown', PREDICATE],
    ['', PREDICATE],
    ['family', PREDICATE],
    ['predicate', PREDICATE],
    ['Dwell', PREDICATE],
    ['dwell ', PREDICATE],
    ['dwell​', PREDICATE],
  ])('entrada hostil %s %j: mismo veredicto que el oráculo', (family, predicate) => {
    agrees(family, predicate);
  });

  it.each(['', '{', 'NaN', 'Infinity', '1e400', 'undefined', '{"all_of":', '\u0000'])(
    'texto que no es un predicado JSON: %j',
    (source) => {
      expect(parsePredicate('dwell', source)).toBeNull();
    },
  );

  it('anidamiento profundo y cadenas gigantes terminan en null, sin excepción', () => {
    const deep = '['.repeat(100_000) + ']'.repeat(100_000);
    expect(parsePredicate('dwell', deep)).toBeNull();
    const huge = JSON.stringify({ ...PREDICATE, all_of: ['x'.repeat(1_000_000), presence] });
    expect(parsePredicate('dwell', huge)).toBeNull();
  });

  it('propiedad: cualquier familia y JSON da el mismo veredicto que el oráculo', () => {
    const family = fc.oneof(fc.constantFrom(...FAMILIES), fc.string({ maxLength: 20 }));
    const predicate = fc.oneof(
      fc.record({
        all_of: fc.array(fc.constantFrom(...CONDITIONS), { maxLength: 4 }),
        min_duration_ms: fc.oneof(fc.constantFrom(...DURATIONS), fc.integer()),
      }),
      fc.jsonValue({ maxDepth: 4 }),
    );
    fc.assert(
      fc.property(family, predicate, (name, value) => {
        agrees(name, value);
      }),
    );
  });
});
