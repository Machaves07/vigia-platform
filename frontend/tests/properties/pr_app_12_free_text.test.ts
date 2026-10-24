// @vitest-environment node
/**
 * PR-APP-12 (BR-APP-42, 44; VIG-280): para todo texto generado, el validador estructural de la
 * interfaz (`checkFreeText`) acepta si y solo si el oráculo acepta.
 *
 * El oráculo son el corpus de `backend/openapi/u05-fixtures.json` (VIG-279, exportado desde la
 * política base de `ledger/free_text.py`), consumido por igualdad de `verdict` y `normalized`, y el
 * esquema Ajv del campo (`minLength`/`maxLength`). Las propiedades parten de los textos que el
 * corpus acepta y les aplican transformaciones cuyo veredicto el servidor fija: insertar un
 * carácter de cada clase rechazada, insertar un fragmento de marcado, descomponer en NFD (el
 * normalizado no cambia) y cruzar los límites de longitud.
 */
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

import Ajv2020 from 'ajv/dist/2020.js';
import fc from 'fast-check';
import { describe, expect, it } from 'vitest';

import { checkFreeText, type FreeTextLimits, type FreeTextVerdict } from '@/ports/validation';
import { codePointLength, type FreeTextRejection } from '@/ports/validation/freeText';

interface Route {
  readonly route: string;
  readonly min_length: number;
  readonly max_length: number;
}

interface Vector {
  readonly case: string;
  readonly route: string;
  readonly text: string;
  readonly verdict: 'accepted' | FreeTextRejection;
  readonly normalized: string | null;
}

interface Corpus {
  readonly free_text: {
    readonly policy: string;
    readonly length_unit: string;
    readonly verdicts: readonly string[];
    readonly routes: readonly Route[];
    readonly vectors: readonly Vector[];
  };
}

const FIXTURES = fileURLToPath(
  new URL('../../../backend/openapi/u05-fixtures.json', import.meta.url),
);
const corpus = (JSON.parse(readFileSync(FIXTURES, 'utf8')) as Corpus).free_text;
const routes = new Map(corpus.routes.map((route) => [route.route, route]));

function limitsOf(route: string): FreeTextLimits {
  const found = routes.get(route);
  if (found === undefined) throw new Error(`ruta del corpus sin límites: ${route}`);
  return { minLength: found.min_length, maxLength: found.max_length };
}

/** Veredicto en la forma del corpus. */
function verdictOf(text: string, route: string): { verdict: string; normalized: string | null } {
  const result: FreeTextVerdict = checkFreeText(text, limitsOf(route));
  return result.ok
    ? { verdict: 'accepted', normalized: result.normalized }
    : { verdict: result.reason, normalized: null };
}

const accepted = corpus.vectors.filter((vector) => vector.verdict === 'accepted');

/** Inserta `piece` en `text` en la posición `at` contada en caracteres Unicode. */
function insertAt(text: string, at: number, piece: string): string {
  const chars = Array.from(text);
  const index = at % (chars.length + 1);
  return [...chars.slice(0, index), piece, ...chars.slice(index)].join('');
}

/** Veredicto esperado al añadir algo a un texto aceptado: el primer tope es la expansión. */
function expectedAfterInsert(text: string, route: string, reason: FreeTextRejection): string {
  return codePointLength(text) > 4 * limitsOf(route).maxLength ? 'too_long' : reason;
}

const CLASSES: Readonly<
  Record<'control_character' | 'invisible_character' | 'disallowed_character', readonly string[]>
> = {
  control_character: [
    '\u0000',
    '\u0009',
    '\u000a',
    '\u000d',
    '\u001f',
    '\u007f',
    '\u0085',
    '\u009f',
    '\u{2028}',
    '\u{2029}',
  ],
  invisible_character: [
    '\u{ad}',
    '\u{34f}',
    '\u{61c}',
    '\u{115f}',
    '\u{1160}',
    '\u{17b4}',
    '\u{180e}',
    '\u{200b}',
    '\u{200d}',
    '\u{200e}',
    '\u{202e}',
    '\u{2060}',
    '\u{2064}',
    '\u{2800}',
    '\u{3164}',
    '\u{fe0f}',
    '\u{feff}',
    '\u{ffa0}',
    '\u{1bca0}',
    '\u{1d173}',
    '\u{e0001}',
    '\u{e0100}',
  ],
  disallowed_character: [
    '\ud800',
    '\udfff',
    '\u{e000}',
    '\u{f8ff}',
    '\u{f0000}',
    '\u{10fffd}',
    '\u{378}',
    '\u{ffff}',
    '\u{e0080}',
  ],
};

const MARKUP = [
  '<b',
  '</p',
  '<!--',
  '<?xml',
  '<script',
  '<A',
  '&lt',
  '&LT;',
  '&gt',
  '&amp;',
  '&quot',
  '&apos',
  '&nbsp',
  '&eacute;',
  '&#60',
  '&#x3C',
  '&#X3c',
  '\u{ff1c}b',
  '<\u{17f}',
  '<\u{212a}',
  '<\u{130}',
  '<\u{131}',
  '&apo\u{17f}',
  '&a\u{131};',
];

/** Alfabeto estable bajo NFC: su longitud es la que cuenta Ajv y la que cuenta el servidor. */
const SAFE = Array.from(
  'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 .,;:-()¿?¡!áéíóúñÁÉÍÓÚÑü',
);

describe('PR-APP-12: corpus de u05-fixtures.json', () => {
  it('trae la política base con todos sus motivos y longitudes tras NFC', () => {
    expect(corpus.policy).toBe('base');
    expect(corpus.length_unit).toBe('code_point_after_nfc');
    expect([...corpus.verdicts].sort()).toEqual([
      'accepted',
      'control_character',
      'disallowed_character',
      'invisible_character',
      'markup',
      'too_long',
      'too_short',
    ]);
    expect(corpus.vectors.length).toBeGreaterThan(0);
  });

  it('cada vector da en freeText.ts el mismo veredicto y el mismo normalizado (igualdad)', () => {
    const differences = corpus.vectors
      .map((vector) => ({
        case: vector.case,
        expected: { verdict: vector.verdict, normalized: vector.normalized },
        actual: verdictOf(vector.text, vector.route),
      }))
      .filter(
        ({ expected, actual }) =>
          expected.verdict !== actual.verdict || expected.normalized !== actual.normalized,
      );
    expect(differences).toEqual([]);
  });
});

describe('PR-APP-12: propiedades contra el oráculo', () => {
  const base = fc.constantFrom(...accepted);

  it('un carácter de una clase rechazada, insertado en un texto aceptado, da el motivo de su clase', () => {
    const reason = fc.constantFrom(...(Object.keys(CLASSES) as (keyof typeof CLASSES)[]));
    fc.assert(
      fc.property(
        base,
        reason.chain((name) => fc.tuple(fc.constant(name), fc.constantFrom(...CLASSES[name]))),
        fc.nat(),
        (vector, [name, char], at) => {
          const text = insertAt(vector.text, at, char);
          expect(verdictOf(text, vector.route).verdict).toBe(
            expectedAfterInsert(text, vector.route, name),
          );
        },
      ),
    );
  });

  it('un fragmento de marcado, insertado en un texto aceptado, da markup', () => {
    fc.assert(
      fc.property(base, fc.constantFrom(...MARKUP), fc.nat(), (vector, fragment, at) => {
        const text = insertAt(vector.text, at, fragment);
        expect(verdictOf(text, vector.route).verdict).toBe(
          expectedAfterInsert(text, vector.route, 'markup'),
        );
      }),
    );
  });

  it('descomponer en NFD un texto aceptado no cambia el veredicto ni el texto normalizado', () => {
    fc.assert(
      fc.property(base, fc.nat(), (vector, cut) => {
        const chars = Array.from(vector.text);
        const index = cut % (chars.length + 1);
        // Descompone un prefijo: el texto mezcla formas compuestas y descompuestas.
        const text = chars.slice(0, index).join('').normalize('NFD') + chars.slice(index).join('');
        expect(verdictOf(text, vector.route)).toEqual({
          verdict: 'accepted',
          normalized: vector.normalized,
        });
      }),
    );
  });

  it('con un alfabeto estable bajo NFC, acepta si y solo si el esquema Ajv del campo acepta', () => {
    const ajv = new Ajv2020({ strict: true });
    const schemas = new Map(
      corpus.routes.map((field) => [
        field.route,
        ajv.compile({ type: 'string', minLength: field.min_length, maxLength: field.max_length }),
      ]),
    );
    // Longitudes en los bordes de cada campo (mínimo y máximo, justo dentro y justo fuera) o en
    // cualquier punto entre 0 y el máximo más tres.
    const sample = fc.constantFrom(...corpus.routes).chain((field) => {
      const { min_length: min, max_length: max } = field;
      const length = fc.oneof(
        fc.integer({ min: Math.max(0, min - 2), max: min + 2 }),
        fc.integer({ min: Math.max(0, max - 2), max: max + 2 }),
        fc.integer({ min: 0, max: max + 3 }),
      );
      return fc.tuple(
        fc.constant(field),
        length.chain((size) =>
          fc.array(fc.constantFrom(...SAFE), { minLength: size, maxLength: size }),
        ),
      );
    });
    fc.assert(
      fc.property(sample, ([field, chars]) => {
        const text = chars.join('');
        const oracle = schemas.get(field.route)?.(text) ?? false;
        const length = codePointLength(text);
        const expected = oracle ? 'accepted' : length > field.max_length ? 'too_long' : 'too_short';
        expect(verdictOf(text, field.route).verdict).toBe(expected);
      }),
    );
  });
});

describe('PR-APP-12: bordes', () => {
  it('rechaza límites incoherentes como el servidor (FreeTextField)', () => {
    expect(() => checkFreeText('x', { minLength: -1, maxLength: 10 })).toThrow(RangeError);
    expect(() => checkFreeText('x', { minLength: 5, maxLength: 4 })).toThrow(RangeError);
    expect(() => checkFreeText('x', { minLength: 0, maxLength: 0 })).toThrow(RangeError);
  });

  it('cuenta caracteres Unicode, no unidades UTF-16, y un sustituto suelto como uno', () => {
    expect(codePointLength('\u{1f600}\u{1f600}')).toBe(2);
    expect(codePointLength('a\ud800b')).toBe(3);
    expect(checkFreeText('\u{1f600}\u{1f600}', { minLength: 1, maxLength: 2 })).toEqual({
      ok: true,
      normalized: '\u{1f600}\u{1f600}',
    });
  });

  it('una cadena gigante se rechaza por expansión antes de normalizar', () => {
    expect(checkFreeText('a'.repeat(41), { minLength: 1, maxLength: 10 })).toEqual({
      ok: false,
      reason: 'too_long',
      key: 'validation.free_text.too_long',
    });
  });

  it('el marcado escrito con signos de ancho completo se busca también en NFKC', () => {
    expect(
      checkFreeText('texto \u{ff1c}b\u{ff1e} de prueba', { minLength: 1, maxLength: 100 }),
    ).toMatchObject({
      ok: false,
      reason: 'markup',
    });
  });
});
