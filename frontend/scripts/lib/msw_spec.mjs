/**
 * Copia reducida de `backend/openapi/app.yaml` para los manejadores de MSW (LC-APP-25, VIG-286).
 *
 * El servidor de desarrollo de Vite no sirve archivos fuera de `frontend/` (`server.fs.allow`), así
 * que el modo `VITE_API_MODE=mock` del navegador no puede importar `app.yaml`. Esta copia guarda
 * solo lo que los manejadores necesitan (método, ruta, respuestas y esquemas, sin títulos ni
 * descripciones) y se regenera con `node scripts/msw_spec.mjs`; `--check` (y `npm run check`)
 * exige que coincida con `app.yaml`.
 */
import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

import { parseOpenApiYaml } from './openapi_yaml.mjs';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');

export const SPEC_PATH = resolve(root, '..', 'backend', 'openapi', 'app.yaml');
export const OUTPUT_PATH = resolve(root, 'tests', 'msw', 'openapi.responses.json');

const METHODS = ['get', 'put', 'post', 'delete', 'patch', 'head', 'options', 'trace'];

/** Anotaciones que no cambian qué documentos valida un esquema. */
const ANNOTATIONS = new Set(['title', 'description', 'examples', '$comment']);
/** Palabras clave cuyo valor es un esquema. */
const SCHEMA_KEYWORDS = new Set([
  'items',
  'additionalProperties',
  'propertyNames',
  'contains',
  'not',
  'if',
  'then',
  'else',
  'unevaluatedItems',
  'unevaluatedProperties',
]);
/** Palabras clave cuyo valor es una lista de esquemas. */
const SCHEMA_LIST_KEYWORDS = new Set(['allOf', 'anyOf', 'oneOf', 'prefixItems']);
/** Palabras clave cuyo valor es un mapa nombre → esquema. */
const SCHEMA_MAP_KEYWORDS = new Set([
  'properties',
  '$defs',
  'patternProperties',
  'dependentSchemas',
]);

/**
 * @typedef {import('./openapi_yaml.mjs').JsonValue} JsonValue
 * @typedef {{ [key: string]: JsonValue }} JsonObject
 */

/**
 * @param {JsonValue | undefined} value
 * @returns {value is JsonObject}
 */
function isObject(value) {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

/**
 * El esquema sin anotaciones, recorriendo solo posiciones de esquema: `const`, `enum` y
 * `default` se copian tal cual aunque contengan una clave `title`.
 *
 * @param {JsonValue} schema
 * @returns {JsonValue}
 */
export function stripAnnotations(schema) {
  if (!isObject(schema)) return schema;
  /** @type {JsonObject} */
  const result = {};
  for (const [key, value] of Object.entries(schema)) {
    if (ANNOTATIONS.has(key)) continue;
    let copy = value;
    if (SCHEMA_KEYWORDS.has(key)) copy = stripAnnotations(value);
    else if (SCHEMA_LIST_KEYWORDS.has(key) && Array.isArray(value)) {
      copy = value.map(stripAnnotations);
    } else if (SCHEMA_MAP_KEYWORDS.has(key) && isObject(value)) {
      copy = Object.fromEntries(Object.entries(value).map(([n, s]) => [n, stripAnnotations(s)]));
    }
    result[key] = copy;
  }
  return result;
}

/**
 * La copia reducida de un documento OpenAPI ya leído.
 *
 * @param {JsonValue} spec
 * @returns {JsonObject}
 */
export function reduceSpec(spec) {
  if (!isObject(spec) || !isObject(spec.paths) || !isObject(spec.components)) {
    throw new Error('app.yaml: falta `paths` o `components`.');
  }
  const schemas = spec.components.schemas;
  if (!isObject(schemas)) throw new Error('app.yaml: falta `components.schemas`.');
  /** @type {JsonObject[]} */
  const operations = [];
  for (const [path, item] of Object.entries(spec.paths)) {
    if (!isObject(item)) throw new Error(`app.yaml: ${path} no es un objeto.`);
    for (const [method, operation] of Object.entries(item)) {
      if (!METHODS.includes(method)) continue;
      if (!isObject(operation) || typeof operation.operationId !== 'string') {
        throw new Error(`app.yaml: ${method.toUpperCase()} ${path} sin operationId.`);
      }
      if (!isObject(operation.responses)) {
        throw new Error(`app.yaml: ${operation.operationId} sin respuestas.`);
      }
      /** @type {JsonObject} */
      const responses = {};
      for (const [status, response] of Object.entries(operation.responses)) {
        if (!isObject(response)) throw new Error(`app.yaml: ${operation.operationId} ${status}.`);
        const content = isObject(response.content) ? response.content : {};
        responses[status] = Object.fromEntries(
          Object.entries(content).map(([type, media]) => [
            type,
            stripAnnotations(isObject(media) && media.schema !== undefined ? media.schema : {}),
          ]),
        );
      }
      operations.push({ operationId: operation.operationId, method, path, responses });
    }
  }
  return {
    $comment:
      'Generado por `node scripts/msw_spec.mjs` desde backend/openapi/app.yaml (VIG-286); ' +
      'no se edita a mano: `node scripts/msw_spec.mjs --check` exige que coincida.',
    operations,
    schemas: Object.fromEntries(
      Object.entries(schemas).map(([name, schema]) => [name, stripAnnotations(schema)]),
    ),
  };
}

/**
 * El contenido esperado de `openapi.responses.json` para el `app.yaml` actual.
 *
 * @param {string} [yaml]
 * @returns {string}
 */
export function renderMswSpec(yaml = readFileSync(SPEC_PATH, 'utf8')) {
  return `${JSON.stringify(reduceSpec(parseOpenApiYaml(yaml)), null, 2)}\n`;
}
