/**
 * PR-APP-30 (oráculo; LC-APP-25, PAT-APP-MAN-02): para toda operación de `backend/openapi/app.yaml`
 * existe un manejador de MSW en modo simulado, y para todos los parámetros de ruta válidos la
 * petición la atiende **su** manejador con su código 2xx y un cuerpo que valida con Ajv contra el
 * esquema de respuesta. El número de operaciones sale de `app.yaml` (leído aquí directamente, no
 * de la copia reducida que usan los manejadores).
 *
 * Además: todo `apiError` valida contra `ApiErrorBody` (la respuesta `default` de toda operación),
 * cada código de error declarado tiene un ejemplo válido, y una variante sustituye la respuesta de
 * su operación.
 */
import fc from 'fast-check';
import { getResponse, http, HttpResponse } from 'msw';
import { afterEach, describe, expect, it } from 'vitest';

import { fromSchema } from '../generators/fromSchema';
import {
  API_ERROR_CODES,
  apiError,
  bySpecificity,
  exampleFor,
  handlers,
  handlersByOperation,
  MOCK_OPERATION_HEADER,
  mswPath,
  operationResponse,
  variant,
} from '../msw/handlers';
import { scenario } from '../msw/scenario';
import { server } from '../setup/msw';
import { openApiValidators, readAppYaml } from '../support/openapiAjv';
import { PROPERTY_TIMEOUT_MS } from '../support/timeouts';

interface Operation {
  readonly operationId: string;
  readonly method: string;
  readonly path: string;
  readonly parameters?: readonly {
    readonly name: string;
    readonly in: string;
    readonly schema: unknown;
  }[];
  readonly responses: Readonly<
    Record<string, { readonly content?: Readonly<Record<string, { readonly schema?: unknown }>> }>
  >;
}

const METHODS = ['get', 'put', 'post', 'delete', 'patch', 'head', 'options', 'trace'];
const spec = readAppYaml();
const paths = spec.paths as Record<string, Record<string, Operation>>;
const OPERATIONS: Operation[] = Object.entries(paths).flatMap(([path, item]) =>
  Object.entries(item)
    .filter(([method]) => METHODS.includes(method))
    .map(([method, operation]) => ({ ...operation, method, path })),
);
const SCHEMAS = (spec.components as { schemas: Record<string, unknown> }).schemas;
const validators = openApiValidators(SCHEMAS);

/** Ejemplos por operación: el cuerpo es fijo, los parámetros solo ejercitan el enrutado. */
const RUNS = Math.max(10, Math.ceil((fc.readConfigureGlobal().numRuns ?? 100) / 10));

function successStatus(operation: Operation): string {
  const statuses = Object.keys(operation.responses).filter((status) => /^2\d\d$/.test(status));
  expect(statuses, `${operation.operationId}: un único 2xx`).toHaveLength(1);
  return statuses[0] ?? '';
}

function pathParameters(operation: Operation): fc.Arbitrary<Record<string, string>> {
  const model: Record<string, fc.Arbitrary<string>> = {};
  for (const parameter of (operation.parameters ?? []).filter((p) => p.in === 'path')) {
    model[parameter.name] = fromSchema(parameter.schema as Record<string, unknown>, {
      document: spec,
      uri: 'urn:vigia:app.yaml',
    })
      .map(String)
      .filter((value) => value !== '' && value !== '.' && value !== '..');
  }
  return fc.record(model);
}

function concretePath(template: string, values: Readonly<Record<string, string>>): string {
  return template.replace(/\{([^}]+)\}/g, (_, name: string) =>
    encodeURIComponent(values[name] ?? ''),
  );
}

describe(
  'PR-APP-30: un manejador de MSW por operación de app.yaml con respuesta válida',
  { timeout: PROPERTY_TIMEOUT_MS },
  () => {
    afterEach(() => {
      server.resetHandlers();
    });

    it('cada operación de app.yaml tiene su manejador y no sobra ninguno', () => {
      expect(OPERATIONS.length).toBeGreaterThan(0);
      const missing = OPERATIONS.map((op) => op.operationId).filter(
        (id) => !handlersByOperation.has(id),
      );
      expect(missing, `operaciones sin manejador: ${missing.join(', ')}`).toEqual([]);
      const declared = new Set(OPERATIONS.map((op) => op.operationId));
      const extra = [...handlersByOperation.keys()].filter((id) => !declared.has(id));
      expect(extra, `manejadores sin operación: ${extra.join(', ')}`).toEqual([]);
      // version.json es público y no forma parte de app.yaml.
      expect(handlers).toHaveLength(OPERATIONS.length + 1);
    });

    it.each(OPERATIONS.map((operation) => [operation.operationId, operation] as const))(
      '%s: su manejador responde su 2xx con un cuerpo válido',
      async (operationId, operation) => {
        const status = successStatus(operation);
        const content = operation.responses[status]?.content ?? {};
        const [type, media] = Object.entries(content)[0] ?? [];
        await fc.assert(
          fc.asyncProperty(pathParameters(operation), async (values) => {
            const url = new URL(concretePath(operation.path, values), window.location.origin);
            const hasBody = ['post', 'put', 'patch'].includes(operation.method);
            const request = new Request(url, {
              method: operation.method.toUpperCase(),
              ...(hasBody ? { body: '{}', headers: { 'content-type': 'application/json' } } : {}),
            });
            const response = await getResponse([...handlers], request);
            if (response === undefined)
              throw new Error(`sin manejador para ${operationId} (${url.pathname})`);
            expect(response.headers.get(MOCK_OPERATION_HEADER), url.pathname).toBe(operationId);
            expect(String(response.status)).toBe(status);
            if (type === undefined) {
              expect(await response.text()).toBe('');
              return;
            }
            expect(response.headers.get('content-type')).toContain(type);
            const body: unknown =
              type === 'application/json' ? await response.json() : await response.text();
            const validate = validators.compile(media?.schema ?? {});
            expect(validate(body), JSON.stringify(validate.errors)).toBe(true);
            if (type === 'application/pdf') expect(String(body).startsWith('%PDF-')).toBe(true);
          }),
          { numRuns: RUNS },
        );
      },
    );
  },
);

describe('errores y variantes', { timeout: PROPERTY_TIMEOUT_MS }, () => {
  it('ApiErrorCode de app.yaml es la lista de códigos de apiError', () => {
    expect(API_ERROR_CODES).toEqual((SCHEMAS.ApiErrorCode as { enum: string[] }).enum);
    expect(() => apiError('teapot', 418)).toThrow(/Código de error desconocido: teapot/);
  });

  it('todo apiError valida contra ApiErrorBody, la respuesta default de toda operación', async () => {
    const defaults = OPERATIONS.map(
      (operation) => operation.responses.default?.content?.['application/json']?.schema,
    );
    expect(defaults.every((schema) => schema !== undefined)).toBe(true);
    const errorBody = validators.component('ApiErrorBody');
    await fc.assert(
      fc.asyncProperty(
        fc.constantFrom(...API_ERROR_CODES),
        fc.integer({ min: 400, max: 599 }),
        fc.option(fc.integer({ min: 1, max: 3600 }), { nil: undefined }),
        fc.option(fc.stringMatching(/^[a-z][a-z0-9]{0,8}(_[a-z0-9]{1,8}){1,3}$/), {
          nil: undefined,
        }),
        fc.constantFrom(...defaults),
        async (code, status, retryAfterSeconds, detailCode, schema) => {
          const reply = apiError(code, status, {
            ...(retryAfterSeconds === undefined ? {} : { retryAfterSeconds }),
            ...(detailCode === undefined ? {} : { detailCode }),
          });
          expect(reply.status).toBe(status);
          const body: unknown = await reply.json();
          expect(errorBody(body), JSON.stringify(errorBody.errors)).toBe(true);
          expect(body).toMatchObject({ code, retry_after_seconds: retryAfterSeconds ?? null });
          const validate = validators.compile(schema);
          expect(validate(body), JSON.stringify(validate.errors)).toBe(true);
          expect(reply.headers.get('retry-after')).toBe(
            retryAfterSeconds === undefined ? null : String(retryAfterSeconds),
          );
        },
      ),
    );
  });

  it('operationResponse da un cuerpo válido para cada código de error que declara cada operación', async () => {
    const declared = OPERATIONS.flatMap((operation) =>
      Object.entries(operation.responses)
        .filter(([status]) => /^[45]\d\d$/.test(status))
        .map(([status, response]) => ({
          operation,
          status,
          schema: response.content?.['application/json']?.schema,
        })),
    );
    expect(declared.length).toBeGreaterThan(0);
    for (const { operation, status, schema } of declared) {
      const reply = operationResponse(operation.operationId, status);
      expect(reply.status).toBe(Number(status));
      const validate = validators.compile(schema ?? {});
      const body: unknown = await reply.json();
      expect(
        validate(body),
        `${operation.operationId} ${status}: ${JSON.stringify(validate.errors)}`,
      ).toBe(true);
    }
    expect(() => operationResponse(declared[0]?.operation.operationId ?? '', 'default')).toThrow(
      /apiError/,
    );
  });

  it('el ejemplo de una operación es fijo y cada petición recibe una copia', async () => {
    const me = OPERATIONS.find((op) => op.method === 'get' && op.path === '/me');
    if (me === undefined) throw new Error('app.yaml sin GET /me');
    const first = exampleFor(me.operationId) as Record<string, unknown>;
    first.alterado = true;
    expect(exampleFor(me.operationId)).not.toHaveProperty('alterado');
    const url = new URL('/me', window.location.origin);
    const [a, b] = await Promise.all([fetch(url), fetch(url)]);
    const bodies: unknown[] = await Promise.all([a.json(), b.json()]);
    expect(bodies[0]).toEqual(exampleFor(me.operationId));
    expect(bodies[1]).toEqual(bodies[0]);
    expect(() => exampleFor(me.operationId, '418')).toThrow(/no declara la respuesta 418/);
  });

  it('una variante sustituye la respuesta de su operación y resetHandlers la quita', async () => {
    const me = OPERATIONS.find((op) => op.method === 'get' && op.path === '/me');
    if (me === undefined) throw new Error('app.yaml sin GET /me');
    const url = new URL('/me', window.location.origin);
    server.use(
      variant(me.operationId, () => apiError('rate_limited', 429, { retryAfterSeconds: 5 })),
    );
    const limited = await fetch(url);
    expect(limited.status).toBe(429);
    expect(((await limited.json()) as { code: string }).code).toBe('rate_limited');
    server.resetHandlers();
    const normal = await fetch(url);
    expect(normal.status).toBe(200);
    expect(normal.headers.get(MOCK_OPERATION_HEADER)).toBe(me.operationId);
  });

  it('las rutas literales van antes que las de parámetro en el primer segmento distinto', async () => {
    const routes = [
      { method: 'get', path: '/users/{user_id}' },
      { method: 'get', path: '/users/me/sessions' },
      { method: 'get', path: '/users/me' },
      { method: 'get', path: '/{any}/me' },
      { method: 'post', path: '/users/me' },
    ];
    expect([...routes].sort(bySpecificity).map((r) => `${r.method} ${r.path}`)).toEqual([
      'get /users/me',
      'post /users/me',
      'get /users/me/sessions',
      'get /users/{user_id}',
      'get /{any}/me',
    ]);
    const ordered = [...routes]
      .sort(bySpecificity)
      .filter((r) => r.method === 'get')
      .map((r) => http.get(mswPath(r.path), () => HttpResponse.text(r.path)));
    const reply = await getResponse(
      ordered,
      new Request(new URL('/users/me', window.location.origin)),
    );
    expect(await reply?.text()).toBe('/users/me');
  });

  it('scenario() devuelve las variantes registradas y falla nombrando los registrados', () => {
    const timeout = [
      variant(OPERATIONS[0]?.operationId ?? '', () => new HttpResponse(null, { status: 504 })),
    ];
    expect(scenario('platform_timeout', { platform_timeout: timeout })).toBe(timeout);
    expect(() => scenario('storage_unavailable', { platform_timeout: timeout })).toThrow(
      /«storage_unavailable».*Registrados: platform_timeout/,
    );
    expect(() => scenario('x', {})).toThrow(/Registrados: ninguno/);
    expect(() => scenario('vacío', { vacío: [] })).toThrow(/lista no vacía/);
  });
});
