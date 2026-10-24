// @vitest-environment node
/**
 * Lector del YAML de `export_openapi` (`scripts/lib/openapi_yaml.mjs`) y copia reducida de la
 * especificación para MSW (`tests/msw/openapi.responses.json`): el lector es estricto y la copia
 * coincide con `backend/openapi/app.yaml` (la regenera `node scripts/msw_spec.mjs`).
 */
import { spawnSync } from 'node:child_process';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

import { describe, expect, it } from 'vitest';

import {
  OUTPUT_PATH,
  reduceSpec,
  renderMswSpec,
  stripAnnotations,
} from '../../../scripts/lib/msw_spec.mjs';
import { OpenApiYamlError, parseOpenApiYaml } from '../../../scripts/lib/openapi_yaml.mjs';
import { API_OPERATIONS, apiOperation, successStatus, type ApiOperation } from '../../generators';
import { readAppYaml } from '../../support/openapiAjv';
import { PROPERTY_TIMEOUT_MS } from '../../support/timeouts';

const SCRIPT = fileURLToPath(new URL('../../../scripts/msw_spec.mjs', import.meta.url));

describe('lector del YAML de export_openapi', { timeout: PROPERTY_TIMEOUT_MS }, () => {
  it('lee mapas, listas de mapas, listas anidadas, vacíos y escalares JSON', () => {
    const text = [
      '# cabecera',
      '"a":',
      '  "b": "x: y \\"z\\" \\u00e9"',
      '  "n": -1.5e-3',
      '  "i": 42',
      '  "t": true',
      '  "z": null',
      '  "e": {}',
      '  "l": []',
      '"items":',
      '  - "uno"',
      '  - "k": 1',
      '    "m": 2',
      '  - - 1',
      '    - 2',
      '  - {}',
      '',
    ].join('\n');
    expect(parseOpenApiYaml(text)).toEqual({
      a: { b: 'x: y "z" é', n: -1.5e-3, i: 42, t: true, z: null, e: {}, l: [] },
      items: ['uno', { k: 1, m: 2 }, [1, 2], {}],
    });
  });

  it('una clave __proto__ es una clave más, sin tocar el prototipo', () => {
    const value = parseOpenApiYaml('"__proto__":\n  "x": 1\n') as Record<string, unknown>;
    expect(Object.hasOwn(value, '__proto__')).toBe(true);
    expect(({} as Record<string, unknown>).x).toBeUndefined();
  });

  it.each([
    ['"a": 1\n\t"b": 2\n', /tabulador/],
    ['"a": 1 \n', /espacio final/],
    ['"a":\n   "b": 1\n', /sangría impar/],
    ['"a": 1\n"a": 2\n', /clave repetida «a»/],
    ['"a": yes\n', /escalar fuera del dialecto/],
    ['"a": "sin cerrar\n', /cadena sin cerrar/],
    ['"a":\n"b": 1\n', /«a» sin valor/],
    ['a: 1\n', /se esperaba una clave/],
    ['', /documento vacío/],
    ['"a": "x" "y"\n', /texto tras la cadena/],
  ])('rechaza %j', (text, message) => {
    expect(() => parseOpenApiYaml(text)).toThrow(OpenApiYamlError);
    expect(() => parseOpenApiYaml(text)).toThrow(message);
  });
});

describe('copia reducida de app.yaml para MSW', { timeout: PROPERTY_TIMEOUT_MS }, () => {
  it('coincide con backend/openapi/app.yaml', () => {
    expect(readFileSync(OUTPUT_PATH, 'utf8') === renderMswSpec()).toBe(true);
  });

  it('`node scripts/msw_spec.mjs --check` falla si la copia difiere', () => {
    const ok = spawnSync(process.execPath, [SCRIPT, '--check'], { encoding: 'utf8' });
    expect(ok.status, ok.stderr).toBe(0);
    const usage = spawnSync(process.execPath, [SCRIPT, '--otra'], { encoding: 'utf8' });
    expect(usage.status).toBe(2);
  });

  it('tiene las mismas operaciones, con un único 2xx cada una, que app.yaml', () => {
    const spec = readAppYaml();
    const reduced = reduceSpec(spec) as { operations: { operationId: string }[] };
    expect(API_OPERATIONS.map((op) => op.operationId)).toEqual(
      reduced.operations.map((op) => op.operationId),
    );
    for (const operation of API_OPERATIONS) expect(successStatus(operation)).toMatch(/^2\d\d$/);
    const [first] = API_OPERATIONS;
    if (first === undefined) throw new Error('sin operaciones');
    expect(apiOperation(first.operationId)).toBe(first);
    expect(() => apiOperation('no_existe')).toThrow(/Operación desconocida: no_existe/);
    const twoSuccesses: ApiOperation = { ...first, responses: { '200': {}, '201': {} } };
    expect(() => successStatus(twoSuccesses)).toThrow(/un único 2xx/);
  });

  it('quita anotaciones solo en posiciones de esquema', () => {
    expect(
      stripAnnotations({
        title: 'X',
        description: 'd',
        properties: { title: { type: 'string', title: 'T' } },
        const: { title: 'se queda' },
        items: { description: 'fuera', type: 'integer' },
        anyOf: [{ title: 'a', type: 'null' }],
      }),
    ).toEqual({
      properties: { title: { type: 'string' } },
      const: { title: 'se queda' },
      items: { type: 'integer' },
      anyOf: [{ type: 'null' }],
    });
  });
});
