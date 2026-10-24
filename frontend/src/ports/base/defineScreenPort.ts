/**
 * Marco de los puertos de pantalla (LC-APP-14, `ScreenPort` de `domain-entities.md` §2.6).
 *
 * `defineScreenPort('SCR-08', {listInbox: {kind: 'read', operationId, permission, status:
 * 'fixed'}, …})` da un puerto con tipos derivados del cliente generado: cada lectura expone
 * `read(params, signal)` y cada escritura `write(input)`, la interfaz mínima que consumen los
 * ganchos de `state.query` (VIG-293). Las pantallas solo ven su puerto (BR-APP-62).
 *
 * - `status: 'fixed'` nombra un `operationId` de `app.yaml`; una lectura solo puede ser `GET`.
 * - `status: 'assumed'` nombra la operación del diseño (`GET /zones/{id}/…`) que la
 *   especificación aún no tiene: responde `unavailable` sin llamar a la red (BR-APP-63).
 * - El cuerpo de una escritura se valida con `validateBody` antes de enviarse (BR-APP-64).
 * - `slowSince()` vale, mientras alguna petición del puerto lleve 1 s o más en curso, el instante
 *   en que la más antigua cruzó ese segundo; si no, `null` (carga prolongada, NFR-APP-04).
 */
import type { OPERATIONS } from '@/client';
import type { OperationKey } from '@/client/assumed';
import type { operations } from '@/client/schema';
import { bodies, type BodyOperationId } from '@/client/validators.js';
import type { Clock } from '@/state/clock';
import type { Dependency } from '@/state/connection';

import { validateBody } from '../validation';

import {
  currentTransport,
  PortError,
  type ApiRequest,
  type OperationId,
  type RequestKind,
} from './http';

type ScreenId = `SCR-${string}`;
type ScreenPortStatus = 'fixed' | 'assumed';

/** Umbral del indicador de carga prolongada (NFR-APP-04). */
export const SLOW_AFTER_MS = 1_000;

type Ops = typeof OPERATIONS;
type ReadOperationId = {
  [Id in OperationId]: Ops[Id]['method'] extends 'get' ? Id : never;
}[OperationId];
type WriteOperationId = Exclude<OperationId, ReadOperationId>;

/** Un campo de la entrada (`unknown` si la operación no lo tiene: neutro en la intersección). */
type Part<O, K extends keyof O, As extends string> = [NonNullable<O[K]>] extends [never]
  ? unknown
  : Partial<Pick<O, K>> extends Pick<O, K>
    ? Readonly<Partial<Record<As, NonNullable<O[K]>>>>
    : Readonly<Record<As, NonNullable<O[K]>>>;

type Parameters<Id extends OperationId> = operations[Id]['parameters'];
type JsonBody<Id extends OperationId> =
  NonNullable<operations[Id]['requestBody']> extends {
    readonly content: { readonly 'application/json': infer Body };
  }
    ? { readonly body: Body }
    : unknown;

/** Parámetros de ruta y de consulta de una operación, con los tipos del cliente generado. */
type ReadParams<Id extends OperationId> = Part<Parameters<Id>, 'path', 'path'> &
  Part<Parameters<Id>, 'query', 'query'>;
type WriteInput<Id extends OperationId> = ReadParams<Id> & JsonBody<Id>;

type Responses<Id extends OperationId> = operations[Id]['responses'];
type Content<R> = R extends { readonly content: { readonly 'application/json': infer Json } }
  ? Json
  : R extends { readonly content: Readonly<Record<string, unknown>> }
    ? Blob
    : undefined;
type SuccessCode<Id extends OperationId> = Extract<keyof Responses<Id>, 200 | 201 | 202 | 204>;
/** Cuerpo de la respuesta 2xx: JSON tipado, `Blob` (PDF) o `undefined` (204). */
type Result<Id extends OperationId> = {
  [Code in SuccessCode<Id>]: Content<Responses<Id>[Code]>;
}[SuccessCode<Id>];

interface DeclarationBase {
  readonly permission: string;
  /** Dependencia del circuito; por omisión `platform`. */
  readonly dependency?: Dependency;
}
interface FixedRead<Id extends ReadOperationId = ReadOperationId> extends DeclarationBase {
  readonly kind: 'read';
  readonly status: 'fixed';
  readonly operationId: Id;
}
interface FixedWrite<Id extends WriteOperationId = WriteOperationId> extends DeclarationBase {
  readonly kind: 'write';
  readonly status: 'fixed';
  readonly operationId: Id;
}
interface Assumed extends DeclarationBase {
  readonly kind: RequestKind;
  readonly status: 'assumed';
  /** Método y ruta del diseño; `api_operation_id` vacío mientras esté `assumed`. */
  readonly operation: OperationKey;
}
export type Declaration = FixedRead | FixedWrite | Assumed;

/** Todo opcional (o nada que pasar): el argumento también lo es. */
type ReadArgs<P> =
  Partial<P> extends P ? [params?: P, signal?: AbortSignal] : [params: P, signal?: AbortSignal];
type WriteArgs<I> = Partial<I> extends I ? [input?: I] : [input: I];

type PortOperation<D> =
  D extends FixedRead<infer Id>
    ? { read(...args: ReadArgs<ReadParams<Id>>): Promise<Result<Id>> }
    : D extends FixedWrite<infer Id>
      ? { write(...args: WriteArgs<WriteInput<Id>>): Promise<Result<Id>> }
      : D extends Assumed & { readonly kind: 'read' }
        ? { read(params?: unknown, signal?: AbortSignal): Promise<never> }
        : { write(input?: unknown): Promise<never> };

/** Una fila de `ScreenPort.operations` (`screen_port_status` incluido). */
interface OperationInfo {
  readonly name: string;
  readonly kind: RequestKind;
  readonly status: ScreenPortStatus;
  readonly api_operation_id: OperationId | null;
  readonly permission: string;
}

const RESERVED = ['screen', 'operations', 'slowSince', 'subscribeSlow'] as const;
type Reserved = (typeof RESERVED)[number];

export type ScreenPort<D extends Readonly<Record<string, Declaration>>> = {
  readonly screen: ScreenId;
  readonly operations: readonly OperationInfo[];
  slowSince(): number | null;
  subscribeSlow(listener: () => void): () => void;
} & { readonly [Name in keyof D]: PortOperation<D[Name]> };

type Input = Readonly<{
  path?: Readonly<Record<string, string | number | boolean>>;
  query?: Readonly<Record<string, unknown>>;
  body?: unknown;
}>;

function isBodyOperation(operationId: OperationId): operationId is BodyOperationId {
  return Object.hasOwn(bodies, operationId);
}

/** Peticiones en curso de un puerto, para `slowSince`. */
function slowTracker() {
  const started = new Map<number, number>();
  const listeners = new Set<() => void>();
  let next = 0;
  let now: () => number = () => 0;
  const notify = () => {
    for (const listener of listeners) listener();
  };
  return {
    begin(clock: Clock) {
      const id = next++;
      now = () => clock.now();
      started.set(id, clock.now());
      const timer = clock.setTimeout(notify, SLOW_AFTER_MS);
      return () => {
        clock.clearTimeout(timer);
        started.delete(id);
        notify();
      };
    },
    slowSince(): number | null {
      if (started.size === 0) return null;
      const oldest = Math.min(...started.values());
      return now() - oldest >= SLOW_AFTER_MS ? oldest + SLOW_AFTER_MS : null;
    },
    subscribe(listener: () => void) {
      listeners.add(listener);
      return () => {
        listeners.delete(listener);
      };
    },
  };
}

export function defineScreenPort<
  const D extends Readonly<Record<string, Declaration>> &
    Readonly<Partial<Record<Reserved, never>>>,
>(screen: ScreenId, declarations: D): ScreenPort<D> {
  const slow = slowTracker();
  const port: Record<string, unknown> = {};
  const infos: OperationInfo[] = [];

  for (const [name, declaration] of Object.entries<Declaration>(declarations)) {
    if ((RESERVED as readonly string[]).includes(name)) {
      throw new TypeError(`${screen}: «${name}» es un nombre reservado del puerto.`);
    }
    infos.push({
      name,
      kind: declaration.kind,
      status: declaration.status,
      api_operation_id: declaration.status === 'fixed' ? declaration.operationId : null,
      permission: declaration.permission,
    });

    if (declaration.status === 'assumed') {
      const unavailable = () => Promise.reject(new PortError({ kind: 'unavailable' }));
      port[name] = declaration.kind === 'read' ? { read: unavailable } : { write: unavailable };
      continue;
    }

    const { operationId } = declaration;
    const dependency = declaration.dependency ?? 'platform';
    const send = async (kind: RequestKind, input: Input = {}, signal?: AbortSignal) => {
      // Todas las operaciones con cuerpo lo exigen (`requestBody` obligatorio): sin él, invalid_input.
      if (kind === 'write' && isBodyOperation(operationId)) {
        const verdict = validateBody(operationId, input.body);
        if (!verdict.ok) throw new PortError({ kind: 'invalid_input', issues: verdict.issues });
      }
      const transport = currentTransport();
      const end = slow.begin(transport.clock);
      try {
        return await transport.request({
          operationId,
          kind,
          dependency,
          signal,
          ...(input.path === undefined ? {} : { path: input.path }),
          ...(input.query === undefined
            ? {}
            : { query: input.query as NonNullable<ApiRequest['query']> }),
          ...(input.body === undefined ? {} : { body: input.body }),
        });
      } finally {
        end();
      }
    };
    port[name] =
      declaration.kind === 'read'
        ? { read: (params?: Input, signal?: AbortSignal) => send('read', params, signal) }
        : { write: (input?: Input) => send('write', input) };
  }

  return Object.freeze({
    ...port,
    screen,
    operations: Object.freeze(infos),
    slowSince: () => slow.slowSince(),
    subscribeSlow: (listener: () => void) => slow.subscribe(listener),
  }) as unknown as ScreenPort<D>;
}
