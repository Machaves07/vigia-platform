/**
 * Ajv 8 (dialecto 2020-12, modo estricto, formatos completos de `ajv-formats`; las mismas
 * versiones que `@vigia/contracts`) para validar contra los esquemas de `app.yaml`: el oráculo de
 * PR-APP-30 y de las propiedades de `fromSchema`.
 *
 * Los `$ref` `#/components/schemas/X` se reescriben a un documento propio con `$defs`, para que
 * Ajv no tenga que admitir `components` como palabra clave desconocida.
 */
import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

import Ajv2020, { type ValidateFunction } from 'ajv/dist/2020.js';
import addFormats from 'ajv-formats';

import { parseOpenApiYaml } from '../../scripts/lib/openapi_yaml.mjs';

const DOCUMENT_ID = 'urn:vigia:app.yaml';
const COMPONENTS = '#/components/schemas/';

/** `backend/openapi/app.yaml` leído directamente (no la copia reducida). */
export function readAppYaml(): Record<string, unknown> {
  const path = resolve(
    dirname(fileURLToPath(import.meta.url)),
    '../../../backend/openapi/app.yaml',
  );
  return parseOpenApiYaml(readFileSync(path, 'utf8')) as Record<string, unknown>;
}

function rewriteRefs(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(rewriteRefs);
  if (typeof value !== 'object' || value === null) return value;
  return Object.fromEntries(
    Object.entries(value).map(([key, item]) => [
      key,
      key === '$ref' && typeof item === 'string' && item.startsWith(COMPONENTS)
        ? `${DOCUMENT_ID}#/$defs/${item.slice(COMPONENTS.length)}`
        : rewriteRefs(item),
    ]),
  );
}

export interface SchemaValidators {
  /** Validador de un esquema de la especificación (en línea o con `$ref` a `components`). */
  readonly compile: (schema: unknown) => ValidateFunction;
  /** Validador de `components.schemas.<name>`. */
  readonly component: (name: string) => ValidateFunction;
}

/** Validadores contra `schemas` (los `components.schemas` de `app.yaml` o de su copia). */
export function openApiValidators(schemas: Readonly<Record<string, unknown>>): SchemaValidators {
  const ajv = new Ajv2020({
    strict: true,
    strictTypes: false,
    discriminator: true,
    allErrors: false,
  });
  addFormats(ajv);
  ajv.addSchema({ $id: DOCUMENT_ID, $defs: rewriteRefs(schemas) });
  const cache = new Map<string, ValidateFunction>();
  const compile = (schema: unknown): ValidateFunction => {
    const key = JSON.stringify(schema);
    const cached = cache.get(key);
    if (cached !== undefined) return cached;
    const validate = ajv.compile(rewriteRefs(schema) as object);
    cache.set(key, validate);
    return validate;
  };
  return {
    compile,
    component: (name) => compile({ $ref: `${COMPONENTS}${name}` }),
  };
}
