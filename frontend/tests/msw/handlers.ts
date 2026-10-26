/**
 * Manejadores de MSW generados desde `backend/openapi/app.yaml` (LC-APP-25, PAT-APP-MAN-02,
 * PR-APP-30): uno por operación, que responde su código 2xx con un ejemplo válido por esquema,
 * generado con `fromSchema` y una semilla fija por operación (la misma respuesta en cada
 * petición y en cada corrida). Los usan las pruebas (`server.ts`) y el modo `VITE_API_MODE=mock`
 * del navegador (`src/mocks/browser.ts`).
 *
 * - Los errores van con `apiError(code, status)`: un `ApiErrorBody` válido (la respuesta
 *   `default` de toda operación); los códigos de error que una operación declara aparte
 *   (`RejectionResponse`, `ExportConflictOut`) con `operationResponse(operationId, código)`.
 * - Una variante sustituye la respuesta de una operación (`variant(operationId, resolver)`); los
 *   escenarios con nombre (`'platform_timeout'`, `'storage_unavailable'`…) agrupan variantes en
 *   `tests/msw/scenarios/<escenario>.ts` y se piden con `scenario(nombre)` de `scenario.ts`.
 * - Cada respuesta lleva `x-vigia-mock-operation` con el `operationId` que la dio.
 *
 * Las rutas son relativas al origen de la página (la API comparte origen con la aplicación) y los
 * manejadores van ordenados de la ruta más específica a la menos: `/users/me` antes que
 * `/users/{user_id}`.
 */
import fc from 'fast-check';
import { http, HttpResponse, type HttpHandler, type HttpResponseResolver } from 'msw';

import {
  API_DOCUMENT,
  API_OPERATIONS,
  apiOperation,
  apiSchemaArbitrary,
  componentArbitrary,
  successStatus,
} from '../generators/openapi';

/** Semilla base de los ejemplos; la de cada operación sale de su `operationId`. */
const MOCK_SEED = 286;
export const MOCK_OPERATION_HEADER = 'x-vigia-mock-operation';

const MINIMAL_PDF = '%PDF-1.4\n1 0 obj<</Type/Catalog>>endobj\ntrailer<</Root 1 0 R>>\n%%EOF\n';

/** Semilla estable de un texto (FNV-1a de 32 bits) combinada con `MOCK_SEED`. */
function seedOf(text: string): number {
  let hash = 0x811c9dc5;
  for (const char of text) {
    hash ^= char.codePointAt(0) ?? 0;
    hash = Math.imul(hash, 0x01000193);
  }
  return (hash ^ MOCK_SEED) | 0;
}

/** Ruta de MSW (`:nombre`) desde la de la especificación (`{nombre}`). */
export function mswPath(path: string): string {
  return path.replace(/\{([A-Za-z_][A-Za-z0-9_]*)\}/g, ':$1');
}

const examples = new Map<string, unknown>();

/** El ejemplo (fijo) del cuerpo de `status` de una operación; `undefined` si no tiene cuerpo. */
export function exampleFor(operationId: string, status?: string): unknown {
  const operation = apiOperation(operationId);
  const code = status ?? successStatus(operation);
  const key = `${operationId} ${code}`;
  if (examples.has(key)) return structuredClone(examples.get(key));
  const content = operation.responses[code];
  if (content === undefined) throw new Error(`${operationId} no declara la respuesta ${code}.`);
  const [entry] = Object.entries(content);
  let example: unknown;
  if (entry !== undefined) {
    const [type, schema] = entry;
    example =
      type === 'application/pdf'
        ? MINIMAL_PDF
        : fc.sample(apiSchemaArbitrary(schema), { seed: seedOf(key), numRuns: 1 })[0];
  }
  // /me describe la construcción simulada, no una versión aleatoria del generador de esquemas.
  if (operationId === 'me_me_get' && /^2\d\d$/.test(code)) {
    example = {
      ...(example as Record<string, unknown>),
      api_version: import.meta.env.VITE_APP_VERSION,
    };
  }
  examples.set(key, example);
  return structuredClone(example);
}

/**
 * La respuesta de ejemplo de una operación para un código que declara (por omisión, su 2xx):
 * por ejemplo `operationResponse(id, '429')` da el `RejectionResponse` de la API del nodo. Para el
 * `default` (`ApiErrorBody`) se usa `apiError`.
 */
export function operationResponse(operationId: string, declared?: string): Response {
  const operation = apiOperation(operationId);
  const code = declared ?? successStatus(operation);
  const status = Number(code);
  if (!Number.isInteger(status)) throw new Error(`${operationId}: usa apiError para «${code}».`);
  const headers = {
    [MOCK_OPERATION_HEADER]: operation.operationId,
    ...(operation.operationId === 'me_me_get'
      ? { 'X-Vigia-Api-Version': import.meta.env.VITE_APP_VERSION }
      : {}),
  };
  const [type] = Object.keys(operation.responses[code] ?? {});
  const body = exampleFor(operation.operationId, code);
  if (type === undefined) return new HttpResponse(null, { status, headers });
  if (type === 'application/json') return HttpResponse.json(body as never, { status, headers });
  return new HttpResponse(String(body), {
    status,
    headers: { ...headers, 'content-type': type },
  });
}

/** Códigos de error de la API (`ApiErrorCode` de `app.yaml`). */
export const API_ERROR_CODES: readonly string[] = (() => {
  const codes = (API_DOCUMENT.components.schemas.ApiErrorCode as { enum?: unknown } | undefined)
    ?.enum;
  if (!Array.isArray(codes) || !codes.every((code) => typeof code === 'string')) {
    throw new Error('app.yaml: ApiErrorCode sin enum de textos.');
  }
  return codes;
})();

/**
 * Respuesta de error con un `ApiErrorBody` válido (`code`, `message_es` y `correlation_id`
 * fijos por código; `retry_after_seconds`, `detail_code` y `rejection` si se dan: el rechazo de
 * un validador de texto libre de U-04, `{code, term?, suggestion?}`, VIG-344).
 */
export function apiError(
  code: string,
  status: number,
  extra: {
    readonly retryAfterSeconds?: number;
    readonly detailCode?: string;
    readonly operationId?: string;
    readonly rejection?: Readonly<{ code: string; term?: string; suggestion?: string }>;
  } = {},
): Response {
  if (!API_ERROR_CODES.includes(code)) {
    throw new Error(
      `Código de error desconocido: ${code} (ApiErrorCode: ${API_ERROR_CODES.join(', ')}).`,
    );
  }
  const base = fc.sample(componentArbitrary('ApiErrorBody'), {
    seed: seedOf(`error ${code}`),
    numRuns: 1,
  })[0] as Record<string, unknown>;
  const body = {
    ...base,
    code,
    detail_code: extra.detailCode ?? null,
    retry_after_seconds: extra.retryAfterSeconds ?? null,
    rejection: extra.rejection ?? null,
  };
  const headers: Record<string, string> = {};
  if (extra.operationId !== undefined) headers[MOCK_OPERATION_HEADER] = extra.operationId;
  if (extra.retryAfterSeconds !== undefined)
    headers['retry-after'] = String(extra.retryAfterSeconds);
  return HttpResponse.json(body, { status, headers });
}

/** Manejador que sustituye la respuesta de `operationId` (variante de un escenario). */
export function variant(operationId: string, resolver: HttpResponseResolver): HttpHandler {
  const operation = apiOperation(operationId);
  return http[operation.method](mswPath(operation.path), resolver);
}

/**
 * Orden total de especificidad: segmento a segmento, un parámetro (`\u{10FFFF}`) va detrás de
 * cualquier literal, así que en el primer segmento distinto el literal gana. Hoy `app.yaml` no
 * tiene rutas que se solapen; el orden protege la primera que lo haga (`/users/me` frente a
 * `/users/{user_id}`).
 */
export function bySpecificity(
  a: { readonly path: string; readonly method: string },
  b: { readonly path: string; readonly method: string },
): number {
  const key = (operation: { readonly path: string }): string[] =>
    operation.path.split('/').map((segment) => (segment.startsWith('{') ? '\u{10FFFF}' : segment));
  const left = key(a);
  const right = key(b);
  for (let index = 0; index < Math.min(left.length, right.length); index += 1) {
    const l = left[index] ?? '';
    const r = right[index] ?? '';
    if (l !== r) return l < r ? -1 : 1;
  }
  return left.length - right.length || a.method.localeCompare(b.method);
}

/** Un manejador por operación de `app.yaml`, por `operationId`. */
export const handlersByOperation: ReadonlyMap<string, HttpHandler> = new Map(
  [...API_OPERATIONS]
    .sort(bySpecificity)
    .map((operation) => [
      operation.operationId,
      http[operation.method](mswPath(operation.path), () =>
        operationResponse(operation.operationId),
      ),
    ]),
);

/** Operaciones de app.yaml y metadatos públicos de la misma construcción simulada. */
export const handlers: readonly HttpHandler[] = [
  ...handlersByOperation.values(),
  http.get('/version.json', () =>
    HttpResponse.json(
      {
        app_version: import.meta.env.VITE_APP_VERSION,
        contract_tag: import.meta.env.VITE_CONTRACT_TAG,
        // Marca sintética fija: no se lee la hora del sistema en los manejadores.
        built_at: '2026-10-24T00:00:00.000Z',
      },
      {
        headers: {
          'Cache-Control': 'no-store',
          'X-Vigia-Api-Version': import.meta.env.VITE_APP_VERSION,
        },
      },
    ),
  ),
];
