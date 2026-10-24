// @vitest-environment node
/**
 * PR-APP-19 (BR-APP-64, NFR-APP-28; VIG-280): para todo objeto generado por el esquema del contrato,
 * la validación con Ajv en la interfaz acepta; para todo objeto con un campo extra o un tipo
 * alterado, rechaza con el error de contrato; y ningún cuerpo construido por un puerto contiene
 * campos fuera del tipo generado.
 *
 * Los objetos del contrato son los `Finding` y `Heartbeat` que el kit de conformidad de
 * `vigia-contracts` generó desde el esquema (`tests/fixtures/synthetic/contracts/
 * contract_samples.json`, semilla fija). Las propiedades eligen el objeto, el lugar y la
 * alteración.
 */
import { readdirSync, readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

import fc from 'fast-check';
import { describe, expect, it } from 'vitest';

import {
  validateBody,
  validateContractFinding,
  validateContractHeartbeat,
} from '@/ports/validation';
import type { BodyOperationId, ContractViolation } from '@/ports/validation';

type Json = null | boolean | number | string | Json[] | { [key: string]: Json };
type JsonObject = Record<string, Json>;
type Path = readonly (string | number)[];

/** Un archivo de `openapi/bodies/`. */
interface BodyFile {
  readonly operationId: BodyOperationId;
  readonly body: Json;
  readonly query: Json;
}

const read = (relative: string): unknown =>
  JSON.parse(readFileSync(fileURLToPath(new URL(relative, import.meta.url)), 'utf8'));

const samples = read('../fixtures/synthetic/contracts/contract_samples.json') as {
  findings: JsonObject[];
  heartbeats: JsonObject[];
};

const KINDS = [
  { name: 'Finding', items: samples.findings, validate: validateContractFinding },
  { name: 'Heartbeat', items: samples.heartbeats, validate: validateContractHeartbeat },
] as const;

const isObject = (value: Json | undefined): value is JsonObject =>
  typeof value === 'object' && value !== null && !Array.isArray(value);

/** Rutas de todos los objetos (con la raíz) y de todas las hojas de `value`. */
function paths(value: Json, at: Path = []): { objects: Path[]; leaves: Path[] } {
  if (Array.isArray(value)) {
    const parts = value.map((item, index) => paths(item, [...at, index]));
    return { objects: parts.flatMap((p) => p.objects), leaves: parts.flatMap((p) => p.leaves) };
  }
  if (isObject(value)) {
    const parts = Object.entries(value).map(([key, item]) => paths(item, [...at, key]));
    return {
      objects: [at, ...parts.flatMap((p) => p.objects)],
      leaves: parts.flatMap((p) => p.leaves),
    };
  }
  return { objects: [], leaves: [at] };
}

function get(value: Json, path: Path): Json {
  return path.reduce<Json>(
    (node, step) => (node as Record<string | number, Json>)[step] ?? null,
    value,
  );
}

/** Copia de `value` con `change` aplicado al nodo de `path`. */
function edit(value: Json, path: Path, change: (node: Json) => Json): Json {
  const copy = structuredClone(value);
  if (path.length === 0) return change(copy);
  const parent = get(copy, path.slice(0, -1)) as Record<string | number, Json>;
  const last = path.at(-1) ?? 0;
  parent[last] = change(parent[last] ?? null);
  return copy;
}

/** Otro tipo JSON para una hoja: ningún campo del contrato admite a la vez los dos. */
function otherType(leaf: Json): Json {
  if (typeof leaf === 'string') return 7;
  if (typeof leaf === 'number') return String(leaf);
  if (typeof leaf === 'boolean') return leaf ? 'true' : 'false';
  return { altered: true };
}

/** Nombre de campo que no existe en el objeto (formato de los campos del contrato). */
const extraKey = fc.stringMatching(/^[a-z][a-z_]{0,23}$/);

const kind = fc.constantFrom(...KINDS);
const sample = kind.chain((k) => fc.tuple(fc.constant(k), fc.constantFrom(...k.items)));

describe('PR-APP-19: datos del contrato al recibir', () => {
  it('todo objeto generado desde el esquema del contrato valida', () => {
    for (const { items, validate } of KINDS) {
      expect(items.length).toBeGreaterThan(0);
      for (const item of items) expect(validate(item)).toEqual({ ok: true, value: item });
    }
  });

  it('con un campo extra en cualquier objeto anidado, se rechaza con el error de contrato', () => {
    fc.assert(
      fc.property(
        sample.chain(([k, item]) =>
          fc.tuple(fc.constant(k), fc.constant(item), fc.constantFrom(...paths(item).objects)),
        ),
        extraKey,
        ([{ validate }, item, at], key) => {
          const target = get(item, at) as JsonObject;
          fc.pre(!Object.hasOwn(target, key));
          const altered = edit(item, at, (node) => ({ ...(node as JsonObject), [key]: 'x' }));
          const result = validate(altered);
          expect(result.ok).toBe(false);
          if (!result.ok) {
            expect(result.error.code).toBe('contract_violation');
            expect(result.error.shownAs).toBe('internal_error');
          }
        },
      ),
    );
  });

  it('con el tipo de cualquier hoja alterado, se rechaza con el error de contrato', () => {
    fc.assert(
      fc.property(
        sample.chain(([k, item]) =>
          fc.tuple(fc.constant(k), fc.constant(item), fc.constantFrom(...paths(item).leaves)),
        ),
        ([{ validate }, item, at]) => {
          const result = validate(edit(item, at, otherType));
          expect(result).toMatchObject({ ok: false, error: { code: 'contract_violation' } });
        },
      ),
    );
  });

  it('un Finding con un campo de persona inyectado no valida y devuelve el error de contrato', () => {
    const [finding = {}] = samples.findings;
    const result = validateContractFinding({ ...finding, person_name: 'Persona Sintética' });
    const violation: ContractViolation = {
      code: 'contract_violation',
      shownAs: 'internal_error',
      schema: 'finding.schema.json',
      field: 'person_name',
    };
    expect(result).toEqual({ ok: false, error: violation });
    const camera = edit(finding, ['cameras', 0], (node) => ({
      ...(node as JsonObject),
      face_bbox: [0, 0, 10, 10],
    }));
    expect(validateContractFinding(camera)).toMatchObject({
      ok: false,
      error: { code: 'contract_violation', field: 'cameras[0].face_bbox' },
    });
  });

  it('lo que no es un objeto se rechaza sin lanzar', () => {
    for (const value of [null, 0, 'finding', [], undefined]) {
      expect(validateContractFinding(value)).toMatchObject({ ok: false });
      expect(validateContractHeartbeat(value)).toMatchObject({ ok: false });
    }
  });
});

describe('PR-APP-19: cuerpos de los puertos', () => {
  const directory = fileURLToPath(new URL('../../openapi/bodies/', import.meta.url));
  const files = readdirSync(directory)
    .filter((name) => name.endsWith('.json'))
    .map((name) => read(`../../openapi/bodies/${name}`) as BodyFile);

  /** Cada subesquema de objeto con `properties` de `schema`, con su lugar. */
  function objectSchemas(schema: Json, where: string): { where: string; schema: JsonObject }[] {
    if (Array.isArray(schema))
      return schema.flatMap((item, i) => objectSchemas(item, `${where}/${String(i)}`));
    if (!isObject(schema)) return [];
    const own = isObject(schema.properties) ? [{ where, schema }] : [];
    return [
      ...own,
      ...Object.entries(schema).flatMap(([key, value]) =>
        key === 'enum' || key === 'const' ? [] : objectSchemas(value, `${where}/${key}`),
      ),
    ];
  }

  it('todo objeto de los esquemas generados declara additionalProperties: false', () => {
    const open = files.flatMap((file) =>
      [
        ...objectSchemas(file.body, `${file.operationId}#body`),
        ...objectSchemas(file.query, `${file.operationId}#query`),
      ]
        .filter(({ schema }) => schema.additionalProperties !== false)
        .map(({ where }) => where),
    );
    expect(files.length).toBeGreaterThan(0);
    expect(open).toEqual([]);
  });

  /** Operaciones cuyo cuerpo es un objeto con propiedades (directo o por `$ref`). */
  const objectBodies = files.flatMap((file) => {
    const body = file.body;
    if (!isObject(body)) return [];
    const ref = typeof body.$ref === 'string' ? body.$ref.replace('#/$defs/', '') : null;
    const defs = isObject(body.$defs) ? body.$defs : {};
    const root = ref === null ? body : defs[ref];
    if (!isObject(root) || !isObject(root.properties)) return [];
    return [
      {
        operationId: file.operationId,
        properties: Object.keys(root.properties),
      },
    ];
  });

  it('un campo fuera del tipo en el cuerpo de cualquier operación se rechaza con unknown_field', () => {
    expect(objectBodies.length).toBeGreaterThan(0);
    fc.assert(
      fc.property(
        fc.constantFrom(...objectBodies),
        extraKey,
        ({ operationId, properties }, key) => {
          fc.pre(!properties.includes(key));
          const result = validateBody(operationId, { [key]: 'x' });
          expect(result.ok).toBe(false);
          if (!result.ok)
            expect(result.issues).toContainEqual({ field: key, key: 'validation.unknown_field' });
        },
      ),
    );
  });
});
