/**
 * `ports.base` (LC-APP-13, IF-20): el único envoltorio HTTP de la aplicación.
 *
 * - Tiempo de espera en toda petición: 10 s en lecturas y 30 s en escrituras (BR-APP-61,
 *   NFR-APP-04). `request` exige `kind`, así que ninguna llamada queda sin tope; el tope cubre la
 *   respuesta y la lectura del cuerpo, y vence aunque el `fetch` subyacente ignore el aborto.
 * - Lecturas: hasta 3 reintentos con retroceso de 2, 4 y 8 s, solo ante tiempo de espera, fallo
 *   de red o `temporarily_unavailable` sin `detail_code` de negocio, y solo con el circuito de su
 *   dependencia cerrado (BR-APP-73, PAT-APP-RES-02). Agotados, la lectura falla como `offline`.
 *   Solo las operaciones `GET` pueden declararse lectura: un `POST` nunca se repite solo.
 * - Escrituras: cero reintentos (BR-APP-72). Un tiempo de espera o un fallo de red se informa
 *   como `temporarily_unavailable` (BR-APP-61): `behaviourFor` da `transient` con
 *   `retry_after_seconds` para el botón «repetir». Con el circuito `platform` abierto ni salen.
 * - Cada resultado se registra en `state.connection` por dependencia; una cancelación del
 *   llamador no es un fallo y no se registra.
 * - `credentials: 'same-origin'`; sin iframes ni token anti-falsificación (BR-APP-70); destino
 *   solo de la lista cerrada `OPERATIONS` del cliente generado, en el origen de la aplicación.
 * - `X-Vigia-Concession` en toda petición si y solo si el proveedor inyectado (lo conecta
 *   `state.concession`, VIG-301) devuelve una concesión (BR-APP-67, 69).
 * - `X-Vigia-Api-Version` de cada respuesta se entrega al observador inyectado (`app.version`,
 *   VIG-302).
 * - Toda respuesta no 2xx se traduce con `behaviourFor` (`ports.errors`); `unauthenticated` y
 *   `privacy_notice_required` se emiten además como eventos de sesión (`state.session`, VIG-300);
 *   cualquier otra respuesta HTTP se avisa como respuesta autenticada (renueva la inactividad).
 * - Registro de desarrollo (`devLog.ts`): método, plantilla de ruta, código y duración.
 */
import { OPERATIONS } from '@/client';
import type { Clock, TimerHandle } from '@/state/clock';
import {
  createConnection,
  type ConnectionSnapshot,
  type Dependency,
  type Outcome,
} from '@/state/connection';

import { behaviourFor, counts_for_circuit, type Behaviour, type HttpError } from '../errors/map';
import type { ValidationIssue } from '../validation';

import { createDevLog, type DevLog } from './devLog';

export type OperationId = keyof typeof OPERATIONS;
export type RequestKind = 'read' | 'write';

/** Tope de cada petición por tipo (BR-APP-61, NFR-APP-04) `[objetivo propio]`. */
export const TIMEOUT_MS: Readonly<Record<RequestKind, number>> = { read: 10_000, write: 30_000 };
/** Retroceso de los reintentos de lectura (BR-APP-73) `[objetivo propio]`. */
export const READ_RETRY_DELAYS_MS: readonly number[] = [2_000, 4_000, 8_000];

export const CONCESSION_HEADER = 'X-Vigia-Concession';
export const API_VERSION_HEADER = 'X-Vigia-Api-Version';

/** Fallo de transporte (sin respuesta HTTP) tal como lo cuenta el circuito. */
interface TransportFault {
  readonly kind: 'timeout' | 'network';
}
type CircuitError = HttpError | TransportFault;

/** Lo que `ports.base` usa de `state.connection` (LC-APP-09). */
export interface TransportConnection {
  getSnapshot(): ConnectionSnapshot;
  canRead(dependency: Dependency): boolean;
  canWrite(): boolean;
  register(dependency: Dependency, outcome: Outcome<CircuitError>): void;
}

/** Circuitos de la aplicación con la clasificación de `ports.errors` (`counts_for_circuit`). */
export function createPortConnection(clock: Clock): TransportConnection {
  return createConnection<CircuitError>({
    clock,
    counts_for_circuit: (error, dependency) =>
      counts_for_circuit(error) ||
      (dependency !== 'platform' && 'code' in error && error.code === 'storage_unavailable'),
  });
}

/** Por qué falló una operación de un puerto; la pantalla decide qué mostrar con `behaviour`. */
export type PortFailure =
  /** Operación `assumed`: la especificación aún no la tiene; no se llamó a la red (BR-APP-63). */
  | { readonly kind: 'unavailable' }
  /** Circuito abierto o reintentos agotados (BR-APP-73, 74). */
  | { readonly kind: 'offline' }
  /** El cuerpo no cumple su esquema espejo; no se envió (BR-APP-64). */
  | { readonly kind: 'invalid_input'; readonly issues: readonly ValidationIssue[] }
  /** El llamador canceló (cambio de pantalla, consulta descartada). */
  | { readonly kind: 'aborted' }
  /** Sin respuesta (tiempo de espera o red): se presenta como `temporarily_unavailable`. */
  | { readonly kind: 'timeout' | 'network'; readonly behaviour: Behaviour }
  /** Respuesta no 2xx, traducida por el mapa cerrado (BR-APP-71). */
  | { readonly kind: 'http'; readonly error: HttpError; readonly behaviour: Behaviour };

export class PortError extends Error {
  readonly failure: PortFailure;

  constructor(failure: PortFailure) {
    super(`ports.base: ${failure.kind}`);
    this.name = 'PortError';
    this.failure = failure;
  }
}

export function isPortError(value: unknown): value is PortError {
  return value instanceof PortError;
}

type SessionEvent = 'unauthenticated' | 'privacy_notice_required';

export interface TransportOptions {
  readonly clock: Clock;
  readonly connection: TransportConnection;
  /** Concesión activa del proveedor (VIG-301); `null` o vacía: sin cabecera. */
  readonly concession?: () => string | null | undefined;
  /** Observador de `X-Vigia-Api-Version` (VIG-302). */
  readonly onApiVersion?: (version: string) => void;
  /** Eventos de sesión (VIG-300). */
  readonly onSessionEvent?: (event: SessionEvent) => void;
  /**
   * Respuesta HTTP que no es `unauthenticated` ni `privacy_notice_required`: el servidor renovó la
   * inactividad de la sesión (BR-NUC-25), y `state.session` reinicia su ventana (VIG-300).
   */
  readonly onAuthenticatedResponse?: () => void;
  /** Origen de la API; por omisión, el de la página (la API comparte origen). */
  readonly origin?: string;
  readonly fetch?: typeof fetch;
  readonly devLog?: DevLog;
}

type ParamValue = string | number | boolean;

export interface ApiRequest {
  readonly operationId: OperationId;
  readonly kind: RequestKind;
  readonly path?: Readonly<Record<string, ParamValue>>;
  readonly query?: Readonly<Record<string, ParamValue | readonly ParamValue[] | null | undefined>>;
  readonly body?: unknown;
  /** Cancelación del llamador (solo lecturas: una escritura enviada no se cancela). */
  readonly signal?: AbortSignal | undefined;
  readonly dependency?: Dependency;
}

export interface Transport {
  readonly clock: Clock;
  request(request: ApiRequest): Promise<unknown>;
}

const TRANSIENT: HttpError = { code: 'temporarily_unavailable', status: 0 };
const GATEWAY_STATUSES = new Set([502, 503, 504]);
const MAX_API_VERSION_LENGTH = 128;

type Attempt =
  | { readonly type: 'ok'; readonly status: number; readonly value: unknown }
  | { readonly type: 'fault'; readonly fault: TransportFault }
  | { readonly type: 'http'; readonly error: HttpError }
  | { readonly type: 'aborted' };

function fillPath(template: string, params: ApiRequest['path'] = {}): string {
  return template.replace(/\{([^}]+)\}/g, (_, name: string) => {
    const value = Object.hasOwn(params, name) ? params[name] : undefined;
    if (value === undefined || value === '') {
      throw new TypeError(`ports.base: falta el parámetro de ruta «${name}» de ${template}.`);
    }
    const segment = encodeURIComponent(String(value));
    // `encodeURIComponent` deja `.` y `..`, que `URL` normaliza hacia otra operación.
    if (segment === '.' || segment === '..') {
      throw new TypeError(`ports.base: el parámetro de ruta «${name}» no puede ser «.» ni «..».`);
    }
    return segment;
  });
}

function buildUrl(origin: string, template: string, request: ApiRequest): URL {
  const url = new URL(fillPath(template, request.path), origin);
  if (url.origin !== new URL(origin).origin) {
    throw new TypeError('ports.base: destino fuera del origen de la aplicación.');
  }
  for (const [name, value] of Object.entries(request.query ?? {})) {
    if (value === null || value === undefined) continue;
    const values: readonly ParamValue[] = Array.isArray(value) ? value : [value as ParamValue];
    for (const item of values) url.searchParams.append(name, String(item));
  }
  return url;
}

function field(body: unknown, key: string): unknown {
  if (typeof body !== 'object' || body === null || Array.isArray(body)) return undefined;
  return Object.hasOwn(body, key) ? (body as Record<string, unknown>)[key] : undefined;
}

function retryAfterHeader(response: Response): number | null {
  const value = response.headers.get('retry-after');
  return value !== null && /^\d{1,4}$/.test(value.trim()) ? Number(value.trim()) : null;
}

/** `ApiErrorBody` de una respuesta no 2xx; sin cuerpo válido, un 502/503/504 es transitorio. */
function httpError(response: Response, body: unknown): HttpError {
  const code = field(body, 'code');
  const detail = field(body, 'detail_code');
  const retry = field(body, 'retry_after_seconds');
  const message = field(body, 'message_es');
  return {
    code:
      typeof code === 'string'
        ? code
        : GATEWAY_STATUSES.has(response.status)
          ? 'temporarily_unavailable'
          : 'internal_error',
    status: response.status,
    detail_code: typeof detail === 'string' ? detail : null,
    retry_after_seconds: typeof retry === 'number' ? retry : retryAfterHeader(response),
    ...(typeof message === 'string' ? { message_es: message } : {}),
    body,
  };
}

async function readBody(response: Response): Promise<unknown> {
  if (response.status === 204 || response.status === 205) return undefined;
  const type = response.headers.get('content-type') ?? '';
  if (/^application\/(?:[\w.+-]+\+)?json\b/i.test(type)) return response.json() as Promise<unknown>;
  if (!response.ok) return undefined;
  return response.blob();
}

function retryable(attempt: Attempt): boolean {
  if (attempt.type === 'fault') return true;
  return attempt.type === 'http' && counts_for_circuit(attempt.error);
}

const SILENT = (): void => undefined;

export function createTransport(options: TransportOptions): Transport {
  const { clock, connection } = options;
  // Sin `fetch` inyectado se resuelve el global en cada llamada (MSW lo sustituye en pruebas).
  const fetchImpl =
    options.fetch ??
    ((input: RequestInfo | URL, init?: RequestInit) => globalThis.fetch(input, init));
  const devLog = options.devLog ?? createDevLog();

  const headersFor = (request: ApiRequest): Headers => {
    const headers = new Headers({ Accept: 'application/json' });
    if (request.body !== undefined) headers.set('Content-Type', 'application/json');
    const concession = options.concession?.();
    if (typeof concession === 'string' && concession.length > 0) {
      headers.set(CONCESSION_HEADER, concession);
    }
    return headers;
  };

  const observe = (response: Response): void => {
    const version = response.headers.get(API_VERSION_HEADER)?.trim();
    if (version !== undefined && version.length > 0 && version.length <= MAX_API_VERSION_LENGTH) {
      options.onApiVersion?.(version);
    }
  };

  /** Un intento con su tope; el temporizador y los oyentes se liberan siempre. */
  const attempt = async (request: ApiRequest): Promise<Attempt> => {
    const operation = OPERATIONS[request.operationId];
    const url = buildUrl(options.origin ?? globalThis.location.origin, operation.path, request);
    const started = clock.now();
    const controller = new AbortController();
    let timer: TimerHandle | undefined;
    let onCallerAbort: (() => void) | undefined;

    const deadline = new Promise<Attempt>((resolve) => {
      timer = clock.setTimeout(() => {
        resolve({ type: 'fault', fault: { kind: 'timeout' } });
        controller.abort();
      }, TIMEOUT_MS[request.kind]);
    });
    const cancelled = new Promise<Attempt>((resolve) => {
      onCallerAbort = () => {
        resolve({ type: 'aborted' });
        controller.abort();
      };
      request.signal?.addEventListener('abort', onCallerAbort, { once: true });
    });
    const exchange = (async (): Promise<Attempt> => {
      let response: Response;
      try {
        response = await fetchImpl(url, {
          method: operation.method.toUpperCase(),
          headers: headersFor(request),
          credentials: 'same-origin',
          signal: controller.signal,
          ...(request.body === undefined ? {} : { body: JSON.stringify(request.body) }),
        });
      } catch {
        return { type: 'fault', fault: { kind: 'network' } };
      }
      observe(response);
      let body: unknown;
      try {
        body = await readBody(response);
      } catch {
        if (response.ok) return { type: 'fault', fault: { kind: 'network' } };
      }
      return response.ok
        ? { type: 'ok', status: response.status, value: body }
        : { type: 'http', error: httpError(response, body) };
    })();
    // El intercambio puede rechazar tras perder la carrera; nunca queda un rechazo sin atender.
    exchange.catch(SILENT);

    try {
      const result = await Promise.race([exchange, deadline, cancelled]);
      devLog({
        method: operation.method,
        route: operation.path,
        status:
          result.type === 'http'
            ? result.error.status
            : result.type === 'ok'
              ? result.status
              : null,
        code:
          result.type === 'http'
            ? result.error.code
            : result.type === 'fault'
              ? result.fault.kind
              : result.type,
        durationMs: clock.now() - started,
      });
      return result;
    } finally {
      if (timer !== undefined) clock.clearTimeout(timer);
      if (onCallerAbort !== undefined) request.signal?.removeEventListener('abort', onCallerAbort);
    }
  };

  /** Registra el intento en su circuito y emite los eventos de sesión. */
  const settle = (request: ApiRequest, result: Attempt): void => {
    const dependency = request.dependency ?? 'platform';
    if (result.type === 'ok') {
      connection.register(dependency, { ok: true });
      options.onAuthenticatedResponse?.();
    } else if (result.type === 'fault')
      connection.register(dependency, { ok: false, error: result.fault });
    else if (result.type === 'http') {
      connection.register(dependency, { ok: false, error: result.error });
      if (
        result.error.code === 'unauthenticated' ||
        result.error.code === 'privacy_notice_required'
      ) {
        options.onSessionEvent?.(result.error.code);
      } else {
        options.onAuthenticatedResponse?.();
      }
    }
  };

  const failure = (result: Exclude<Attempt, { type: 'ok' }>): PortError => {
    if (result.type === 'aborted') return new PortError({ kind: 'aborted' });
    if (result.type === 'fault') {
      return new PortError({ kind: result.fault.kind, behaviour: behaviourFor(TRANSIENT) });
    }
    return new PortError({
      kind: 'http',
      error: result.error,
      behaviour: behaviourFor(result.error),
    });
  };

  /** Espera de retroceso con el reloj inyectado; la cancelación del llamador la corta. */
  const backoff = (delayMs: number, signal: AbortSignal | undefined): Promise<boolean> =>
    new Promise((resolve) => {
      const onAbort = () => {
        clock.clearTimeout(handle);
        resolve(false);
      };
      const handle = clock.setTimeout(() => {
        signal?.removeEventListener('abort', onAbort);
        resolve(true);
      }, delayMs);
      signal?.addEventListener('abort', onAbort, { once: true });
    });

  const circuitClosed = (dependency: Dependency): boolean =>
    connection.getSnapshot().circuits[dependency].state === 'closed';

  const write = async (request: ApiRequest): Promise<unknown> => {
    if (!connection.canWrite()) throw new PortError({ kind: 'offline' });
    // Una escritura enviada no se cancela desde la pantalla: su resultado se conoce siempre.
    const result = await attempt({ ...request, signal: undefined });
    settle(request, result);
    if (result.type === 'ok') return result.value;
    throw failure(result);
  };

  const read = async (request: ApiRequest): Promise<unknown> => {
    const dependency = request.dependency ?? 'platform';
    if (request.signal?.aborted === true) throw new PortError({ kind: 'aborted' });
    if (!connection.canRead(dependency)) throw new PortError({ kind: 'offline' });
    for (let retry = 0; ; retry += 1) {
      const result = await attempt(request);
      settle(request, result);
      if (result.type === 'ok') return result.value;
      if (!retryable(result)) throw failure(result);
      const delay = READ_RETRY_DELAYS_MS[retry];
      if (delay === undefined || !circuitClosed(dependency))
        throw new PortError({ kind: 'offline' });
      if (!(await backoff(delay, request.signal))) throw new PortError({ kind: 'aborted' });
      if (!circuitClosed(dependency)) throw new PortError({ kind: 'offline' });
    }
  };

  return {
    clock,
    request: (request) => {
      const { method } = OPERATIONS[request.operationId];
      if (request.kind === 'read' && method !== 'get') {
        return Promise.reject(
          new TypeError(`ports.base: ${request.operationId} no es GET; decláralo escritura.`),
        );
      }
      return request.kind === 'read' ? read(request) : write(request);
    },
  };
}

let installed: Transport | null = null;

/** El arranque de la aplicación instala el transporte con sus proveedores; las pruebas, el suyo. */
export function installTransport(transport: Transport | null): void {
  installed = transport;
}

export function currentTransport(): Transport {
  if (installed === null) {
    throw new Error('ports.base: falta installTransport en el arranque de la aplicación.');
  }
  return installed;
}
