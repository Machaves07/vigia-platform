// @vitest-environment node
/**
 * Lector estricto de `app.yaml` (VIG-280): acepta el subconjunto que emite `export_openapi.py` y
 * falla nombrando la línea ante cualquier otra forma.
 */
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';

import { describe, expect, it } from 'vitest';

import { OpenApiYamlError, parseOpenApiYaml } from '../../../scripts/generate/openapiYaml.mjs';
import { listOperations, SPEC_PATH } from '../../../scripts/generate/render.mjs';

const root = fileURLToPath(new URL('../../..', import.meta.url));

describe('parseOpenApiYaml', () => {
  it('lee objetos, listas anidadas, vacíos y escalares JSON', () => {
    const source = [
      '# comentario de cabecera',
      '"a":',
      '  "b": 1',
      '  "c": "texto: con dos puntos"',
      '  "d": {}',
      '  "e": []',
      '  "f":',
      '    - "uno"',
      '    - "x": true',
      '      "y": null',
      '    - - 1.5',
      '      - -2',
      '"\\u00e1": "\\u2028"',
      '',
    ].join('\n');
    expect(parseOpenApiYaml(source)).toEqual({
      a: {
        b: 1,
        c: 'texto: con dos puntos',
        d: {},
        e: [],
        f: ['uno', { x: true, y: null }, [1.5, -2]],
      },
      á: '\u{2028}',
    });
  });

  it.each([
    ['sangría impar', '"a":\n   "b": 1\n', 'línea 2'],
    ['tabulador', '"a":\n\t"b": 1\n', 'línea 2'],
    ['clave sin comillas', 'a: 1\n', 'línea 1'],
    ['clave repetida', '"a": 1\n"a": 2\n', 'línea 2'],
    ['escalar que no es JSON', '"a": \'x\'\n', 'línea 1'],
    ['sin espacio tras los dos puntos', '"a":1\n', 'línea 1'],
    ['espacio al final', '"a": 1 \n', 'línea 1'],
    ['fin de línea CRLF', '"a": 1\r\n', 'LF'],
    ['sangría que sobra', '"a": 1\n  "b": 2\n', 'línea 2'],
  ])('rechaza %s', (_, source, where) => {
    expect(() => parseOpenApiYaml(source)).toThrow(OpenApiYamlError);
    expect(() => parseOpenApiYaml(source)).toThrow(where);
  });

  it('lee app.yaml entero con todas sus operaciones y esquemas', () => {
    const source = readFileSync(join(root, SPEC_PATH), 'utf8');
    const document = parseOpenApiYaml(source);
    const operations = listOperations(document);
    expect(operations).toHaveLength(
      source.split('\n').filter((line) => line.trimStart().startsWith('"operationId":')).length,
    );
    const components = document.components as { schemas: Record<string, unknown> };
    const lines = source.split('\n');
    const schemaNames = lines
      .slice(lines.indexOf('"components":'))
      .filter((line) => /^ {4}"[^"]+":( |$)/.test(line));
    expect(Object.keys(components.schemas)).toHaveLength(schemaNames.length);
  });
});
