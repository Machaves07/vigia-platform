/**
 * `fromSchema` (LC-APP-24): todo lo generado valida con Ajv (dialecto 2020-12, modo estricto,
 * formatos completos), palabra clave a palabra clave y para cada esquema de `app.yaml` (200
 * ejemplos por esquema con el perfil `ci`, 2 000 con `FC_PROFILE=nightly`), respeta
 * `additionalProperties: false` y los límites, y detiene la construcción ante lo que no soporta.
 */
import Ajv2020 from 'ajv/dist/2020.js';
import addFormats from 'ajv-formats';
import fc from 'fast-check';
import { describe, expect, it } from 'vitest';

import {
  API_DOCUMENT,
  API_SCHEMA_NAMES,
  apiSchemaArbitrary,
  canonicalKey,
  componentArbitrary,
  fromSchema,
  UnsatisfiableSchemaError,
  UnsupportedSchemaError,
  type FromSchemaOptions,
  type JsonSchema,
} from '../../generators';
import { openApiValidators } from '../../support/openapiAjv';
import { PROPERTY_TIMEOUT_MS } from '../../support/timeouts';

function ajv(): Ajv2020 {
  // `strictRequired` rechazaría `then: { not: { required: ['previous'] } }`, válido en 2020-12.
  const instance = new Ajv2020({ strict: true, strictTypes: false, strictRequired: false });
  addFormats(instance);
  return instance;
}

/** Comprueba que todo lo generado desde `schema` valida, y devuelve una muestra. */
function expectValid(
  schema: JsonSchema,
  options: FromSchemaOptions & { registry?: Record<string, object> } = {},
) {
  const instance = ajv();
  for (const document of Object.values(options.registry ?? {})) instance.addSchema(document);
  const validate = instance.compile(schema as object);
  const arbitrary = fromSchema(schema, options);
  fc.assert(
    fc.property(arbitrary, (value) => {
      expect(validate(value), `${JSON.stringify(value)} → ${JSON.stringify(validate.errors)}`).toBe(
        true,
      );
    }),
  );
  return fc.sample(arbitrary, { numRuns: 300, seed: 42 });
}

describe('fromSchema: palabras clave', { timeout: PROPERTY_TIMEOUT_MS }, () => {
  it('additionalProperties: false nunca produce claves fuera de properties', () => {
    const schema = {
      type: 'object',
      additionalProperties: false,
      required: ['a'],
      properties: { a: { type: 'string' }, b: { type: 'integer' } },
    };
    const sample = expectValid(schema);
    for (const value of sample as Record<string, unknown>[]) {
      expect(Object.keys(value).every((key) => key === 'a' || key === 'b')).toBe(true);
      expect(value).toHaveProperty('a');
    }
    expect(sample.some((value) => Object.hasOwn(value as object, 'b'))).toBe(true);
    expect(sample.some((value) => !Object.hasOwn(value as object, 'b'))).toBe(true);
  });

  it('un objeto abierto sí recibe claves no declaradas (lectura tolerante)', () => {
    const sample = expectValid({
      type: 'object',
      required: ['a'],
      properties: { a: { type: 'string' } },
    });
    expect(sample.some((value) => Object.keys(value as object).some((key) => key !== 'a'))).toBe(
      true,
    );
  });

  it('respeta minLength/maxLength en puntos de código, también en los bordes', () => {
    for (const [minLength, maxLength] of [
      [0, 0],
      [1, 1],
      [3, 5],
      [64, 64],
    ] as const) {
      const sample = expectValid({ type: 'string', minLength, maxLength }) as string[];
      expect(sample.some((text) => Array.from(text).length === minLength)).toBe(true);
      expect(sample.some((text) => Array.from(text).length === maxLength)).toBe(true);
    }
  });

  it('respeta minimum/maximum y los exclusivos, en enteros y reales', () => {
    const integers = expectValid({ type: 'integer', minimum: -3, maximum: 3 }) as number[];
    expect(new Set(integers)).toEqual(new Set([-3, -2, -1, 0, 1, 2, 3]));
    const exclusive = expectValid({
      type: 'integer',
      exclusiveMinimum: 0,
      exclusiveMaximum: 3,
    }) as number[];
    expect(new Set(exclusive)).toEqual(new Set([1, 2]));
    expectValid({ type: 'number', exclusiveMinimum: 0, maximum: 1 });
    expectValid({ type: 'integer', minimum: 0, maximum: 9007199254740991 });
    expectValid({ type: 'integer', minimum: -9007199254740991, maximum: -9007199254740000 });
  });

  it('enum, const, tipos múltiples y null', () => {
    expect(new Set(expectValid({ enum: ['a', 'b', 1, null] }))).toEqual(
      new Set(['a', 'b', 1, null]),
    );
    expect(new Set(expectValid({ const: { x: [1] } }).map(canonicalKey))).toEqual(
      new Set(['{"x":[1]}']),
    );
    const mixed = expectValid({ type: ['string', 'null'], maxLength: 2 });
    expect(mixed).toContain(null);
    expect(new Set(expectValid({ type: 'string', enum: ['a', 'b', 3] }))).toEqual(
      new Set(['a', 'b']),
    );
  });

  it('format uuid, date-time, date, email y binary, y pattern anclado o no', () => {
    expectValid({ type: 'string', format: 'uuid' });
    expectValid({ type: 'string', format: 'date-time' });
    expectValid({ type: 'string', format: 'date' });
    expectValid({ type: 'string', format: 'email' });
    expectValid({ type: 'string', format: 'binary' });
    expectValid({ type: 'string', pattern: '^[0-9a-f]{64}$', minLength: 64, maxLength: 64 });
    expectValid({ type: 'string', pattern: '[a-z][a-z0-9]*(?:_[a-z0-9]+)+', maxLength: 64 });
    expectValid({
      type: 'string',
      format: 'date-time',
      minLength: 24,
      maxLength: 24,
      pattern:
        '^[0-9]{4}-(0[1-9]|1[0-2])-(0[1-9]|[12][0-9]|3[01])T([01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9]\\.[0-9]{3}Z$',
    });
  });

  it('items, minItems/maxItems (también 0 y 1), uniqueItems', () => {
    expect(expectValid({ type: 'array', items: { type: 'integer' }, maxItems: 0 })).toEqual(
      Array.from({ length: 300 }, () => []),
    );
    const one = expectValid({
      type: 'array',
      items: { type: 'boolean' },
      minItems: 1,
      maxItems: 1,
    }) as unknown[][];
    expect(one.every((list) => list.length === 1)).toBe(true);
    const unique = expectValid({
      type: 'array',
      uniqueItems: true,
      maxItems: 3,
      items: { enum: ['a', 'b', 'c'] },
    }) as string[][];
    expect(unique.some((list) => list.length === 3)).toBe(true);
  });

  it('anyOf y oneOf con palabras clave hermanas, $ref local y allOf', () => {
    expectValid({
      type: 'object',
      additionalProperties: false,
      required: ['kind'],
      properties: { kind: { type: 'string' }, size: { type: 'integer' } },
      oneOf: [
        { properties: { kind: { const: 'small' }, size: { maximum: 9 } } },
        { properties: { kind: { const: 'big' }, size: { minimum: 10 } }, required: ['size'] },
      ],
    });
    expectValid({
      $defs: { Id: { type: 'string', pattern: '^[A-Z]{3}$' } },
      allOf: [{ $ref: '#/$defs/Id' }, { minLength: 3 }],
    });
    expectValid({ anyOf: [{ type: 'string', format: 'uuid' }, { type: 'null' }] });
  });

  it('if/then/else de guarda constante, en las dos ramas', () => {
    const sample = expectValid({
      type: 'object',
      additionalProperties: false,
      required: ['index', 'continues'],
      properties: {
        index: { type: 'integer', minimum: 0, maximum: 3 },
        continues: { type: 'boolean' },
        previous: { type: 'string' },
      },
      if: { required: ['index'], properties: { index: { const: 0 } } },
      then: { not: { required: ['previous'] } },
      else: { required: ['previous'] },
    }) as Record<string, unknown>[];
    expect(sample.some((value) => value.index === 0)).toBe(true);
    expect(sample.some((value) => value.index !== 0)).toBe(true);
  });

  it('not con const, enum, required, contains y guarda de objeto', () => {
    expect(new Set(expectValid({ enum: ['a', 'b', 'c'], not: { const: 'b' } }))).toEqual(
      new Set(['a', 'c']),
    );
    expect(expectValid({ type: 'boolean', not: { const: false } })).toEqual(
      Array.from({ length: 300 }, () => true),
    );
    expectValid({ type: 'string', not: { enum: ['', 'x'] }, maxLength: 1 });
    expectValid({
      type: 'array',
      items: { enum: ['a', 'b'] },
      not: { contains: { const: 'a' } },
    });
  });

  it('contains con minContains/maxContains: exactamente un elemento que cumple', () => {
    const sample = expectValid({
      type: 'array',
      minItems: 1,
      maxItems: 8,
      items: {
        type: 'object',
        additionalProperties: false,
        required: ['id'],
        properties: { id: { type: 'integer' }, originating: { type: 'boolean' } },
      },
      contains: { required: ['originating'], properties: { originating: { const: true } } },
      minContains: 1,
      maxContains: 1,
    }) as { originating?: boolean }[][];
    expect(sample.some((list) => list.length > 1)).toBe(true);
    expectValid({
      type: 'array',
      minItems: 2,
      items: {
        type: 'object',
        properties: { segment: { enum: ['start', 'middle', 'end', 'full'] } },
        required: ['segment'],
      },
      allOf: [
        { contains: { required: ['segment'], properties: { segment: { const: 'start' } } } },
        { contains: { required: ['segment'], properties: { segment: { const: 'end' } } } },
      ],
    });
  });

  it('additionalProperties con esquema y propertyNames (mapas)', () => {
    const sample = expectValid({
      type: 'object',
      additionalProperties: { type: 'integer', minimum: 0 },
      propertyNames: { pattern: '^[0-9]{4}-(0[1-9]|1[0-2])$' },
    }) as Record<string, unknown>[];
    expect(sample.some((value) => Object.keys(value).length > 0)).toBe(true);
  });

  it('$ref entre documentos por $id', () => {
    const common = {
      $id: 'https://example.test/schemas/common.schema.json',
      $defs: { Code: { type: 'string', pattern: '^[A-Z]{2,4}$' } },
    };
    const root = {
      $id: 'https://example.test/schemas/root.schema.json',
      type: 'object',
      additionalProperties: false,
      required: ['code'],
      properties: { code: { $ref: 'common.schema.json#/$defs/Code' } },
    };
    expectValid(root, { registry: { [common.$id]: common } });
  });

  it('esquemas booleanos y vacío', () => {
    expectValid(true);
    expectValid({});
    expect(() => fromSchema(false)).toThrow(UnsatisfiableSchemaError);
  });
});

describe('fromSchema: lo que no sabe hacer lo dice', { timeout: PROPERTY_TIMEOUT_MS }, () => {
  it.each([
    [{ type: 'object', minProperties: 1 }, /«minProperties» no soportada \(en #\)/],
    [
      { type: 'object', properties: { a: { type: 'array', prefixItems: [{}] } } },
      /«prefixItems».*#\/properties\/a/,
    ],
    [{ type: 'number', multipleOf: 2 }, /«multipleOf»/],
    [{ type: 'string', format: 'hostname' }, /format «hostname» no soportado/],
    [{ $ref: 'other.json#/x' }, /sin URI base|no registrado/],
    [{ $ref: '#/$defs/missing' }, /\$ref sin destino/],
    [{ not: { type: 'string' } }, /no se sabe negar/],
    [{ if: { type: 'string' }, then: { minLength: 1 } }, /no se sabe negar/],
  ])('%j', (schema, message) => {
    expect(() => fc.sample(fromSchema(schema), { numRuns: 1, seed: 1 })).toThrow(
      UnsupportedSchemaError,
    );
    expect(() => fc.sample(fromSchema(schema), { numRuns: 1, seed: 1 })).toThrow(message);
  });

  it.each([
    [{ allOf: [{ const: 'a' }, { const: 'b' }] }],
    [{ type: 'string', minLength: 3, maxLength: 2 }],
    [{ type: 'integer', exclusiveMinimum: 1, exclusiveMaximum: 2 }],
    [{ type: 'object', additionalProperties: false, required: ['a'] }],
    [{ type: 'array', minItems: 1, items: false }],
  ])('insatisfacible: %j', (schema) => {
    expect(() => fromSchema(schema)).toThrow(UnsatisfiableSchemaError);
  });
});

describe('fromSchema sobre cada esquema de app.yaml', { timeout: PROPERTY_TIMEOUT_MS }, () => {
  const validators = openApiValidators(API_DOCUMENT.components.schemas);

  it('todos los esquemas de components se construyen', () => {
    expect(API_SCHEMA_NAMES.length).toBeGreaterThan(400);
  });

  it('apiSchemaArbitrary resuelve $ref a components desde un esquema en línea', () => {
    const schema = {
      type: 'array',
      minItems: 1,
      items: { $ref: '#/components/schemas/AssignmentView' },
    };
    const validate = validators.compile(schema);
    fc.assert(
      fc.property(apiSchemaArbitrary(schema), (value) => {
        expect(validate(value), JSON.stringify(validate.errors)).toBe(true);
      }),
    );
    expect(() => componentArbitrary('NoExiste')).toThrow(/Esquema desconocido: NoExiste/);
  });

  it.each(API_SCHEMA_NAMES)('%s: lo generado valida con Ajv', (name) => {
    const validate = validators.component(name);
    fc.assert(
      fc.property(componentArbitrary(name), (value) => {
        expect(
          validate(value),
          `${JSON.stringify(value)} → ${JSON.stringify(validate.errors)}`,
        ).toBe(true);
      }),
    );
  });
});
