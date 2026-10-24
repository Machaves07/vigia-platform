/**
 * Operaciones y esquemas de `backend/openapi/app.yaml` para los generadores y los manejadores de
 * MSW (LC-APP-24 y 25), desde su copia reducida `tests/msw/openapi.responses.json` (la regenera
 * `node scripts/msw_spec.mjs`; `npm run check` y `pr_app_30_msw_oracle.test.ts` exigen que coincida
 * con `app.yaml`). Se importa como texto (`?raw`): así la lee también el navegador en modo `mock`
 * y TypeScript no infiere un tipo literal de 400 kB.
 *
 * No importa `src/client/` (decisión de la revisión del plan del 2026-10-23).
 */
import type fc from 'fast-check';

import specText from '../msw/openapi.responses.json?raw';

import { schemaArbitraries } from './fromSchema';

type HttpMethod = 'get' | 'put' | 'post' | 'delete' | 'patch';

export interface ApiOperation {
  readonly operationId: string;
  readonly method: HttpMethod;
  /** Ruta de la especificación, con parámetros `{nombre}`. */
  readonly path: string;
  /** Código de estado → tipo de contenido → esquema (`{}` sin cuerpo). */
  readonly responses: Readonly<Record<string, Readonly<Record<string, unknown>>>>;
}

interface ReducedSpec {
  readonly operations: readonly ApiOperation[];
  readonly schemas: Readonly<Record<string, unknown>>;
}

const METHODS: readonly string[] = ['get', 'put', 'post', 'delete', 'patch'];

function parseSpec(text: string): ReducedSpec {
  const value: unknown = JSON.parse(text);
  if (typeof value !== 'object' || value === null) throw new Error('openapi.responses.json vacío.');
  const { operations, schemas } = value as Partial<Record<string, unknown>>;
  if (!Array.isArray(operations) || typeof schemas !== 'object' || schemas === null) {
    throw new Error('openapi.responses.json: faltan `operations` o `schemas`.');
  }
  for (const operation of operations as Partial<Record<string, unknown>>[]) {
    if (
      typeof operation.operationId !== 'string' ||
      typeof operation.method !== 'string' ||
      !METHODS.includes(operation.method) ||
      typeof operation.path !== 'string' ||
      typeof operation.responses !== 'object'
    ) {
      throw new Error(
        `openapi.responses.json: operación mal formada ${JSON.stringify(operation)}.`,
      );
    }
  }
  return { operations: operations as ApiOperation[], schemas: schemas as Record<string, unknown> };
}

const spec = parseSpec(specText);

/** Las operaciones de `app.yaml`, en su orden. */
export const API_OPERATIONS: readonly ApiOperation[] = spec.operations;

/** Documento contra el que se resuelven los `$ref` `#/components/schemas/…`. */
export const API_DOCUMENT: Readonly<{
  components: { schemas: Readonly<Record<string, unknown>> };
}> = {
  components: { schemas: spec.schemas },
};

/** Nombres de los esquemas de `components.schemas`. */
export const API_SCHEMA_NAMES: readonly string[] = Object.keys(spec.schemas);

/** El único código 2xx de la operación (`app.yaml` declara exactamente uno por operación). */
export function successStatus(operation: ApiOperation): string {
  const statuses = Object.keys(operation.responses).filter((status) => /^2\d\d$/.test(status));
  const [status] = statuses;
  if (statuses.length !== 1 || status === undefined) {
    throw new Error(
      `${operation.operationId}: se esperaba un único 2xx, hay ${statuses.join(', ')}.`,
    );
  }
  return status;
}

/** La operación por `operationId`. */
export function apiOperation(operationId: string): ApiOperation {
  const operation = API_OPERATIONS.find((candidate) => candidate.operationId === operationId);
  if (operation === undefined) throw new Error(`Operación desconocida: ${operationId}.`);
  return operation;
}

/** Arbitrario de un esquema de la especificación (en línea o con `$ref` a `components`). */
export function apiSchemaArbitrary(schema: unknown): fc.Arbitrary<unknown> {
  return arbitraries(schema as Record<string, unknown>);
}

/** Un solo constructor para toda la especificación: cada esquema se construye una vez. */
const arbitraries = schemaArbitraries({ document: API_DOCUMENT, uri: 'urn:vigia:app.yaml' });
const components = new Map<string, fc.Arbitrary<unknown>>();

/** Arbitrario de `components.schemas.<name>`. */
export function componentArbitrary(name: string): fc.Arbitrary<unknown> {
  if (!Object.hasOwn(spec.schemas, name)) throw new Error(`Esquema desconocido: ${name}.`);
  let arbitrary = components.get(name);
  if (arbitrary === undefined) {
    arbitrary = apiSchemaArbitrary({ $ref: `#/components/schemas/${encodeURIComponent(name)}` });
    components.set(name, arbitrary);
  }
  return arbitrary;
}
