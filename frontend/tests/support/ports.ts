/**
 * Apoyo de las pruebas de `ports.base` (VIG-292): un `fetch` guionizado que anota cuándo se
 * llamó y cuándo terminó cada intento según el `FakeClock`, y `drive`, que avanza el reloj de
 * segundo en segundo hasta que la promesa termina. Sin esperas reales: entre avances solo se
 * vacía la cola de tareas.
 */
import { createPortConnection, type TransportConnection } from '@/ports/base';
import type { ConnectionSnapshot } from '@/state/connection';

import { FakeClock } from './fakeClock';

/** Resultado guionizado de un intento. */
export type Step =
  'ok' | 'network' | 'timeout' | 'transient' | 'business' | 'not_found' | 'rate_limited';

export const RETRYABLE: ReadonlySet<Step> = new Set(['network', 'timeout', 'transient']);
export const TRANSIENT_RETRY_AFTER = 7;

interface Call {
  readonly at: number;
  readonly url: string;
  readonly init: RequestInit | undefined;
  /** Instante en que el intento terminó (respuesta, fallo de red o aborto). */
  endedAt: number | null;
}

function errorBody(code: string, status: number, extra: Record<string, unknown> = {}): Response {
  return Response.json(
    { code, message_es: 'Mensaje de prueba.', correlation_id: 'c-1', ...extra },
    { status },
  );
}

/** `fetch` que responde según `steps` (después del guion, `ok`). */
export function scriptedFetch(clock: FakeClock, steps: readonly Step[] = []) {
  const calls: Call[] = [];
  const fetch = (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
    const step = steps[calls.length] ?? 'ok';
    const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url;
    const call: Call = { at: clock.now(), url, init, endedAt: null };
    calls.push(call);
    const end = <T>(value: T): T => {
      call.endedAt = clock.now();
      return value;
    };
    switch (step) {
      case 'timeout':
        return new Promise((_, reject) => {
          init?.signal?.addEventListener('abort', () => {
            reject(end(new DOMException('abortada', 'AbortError')));
          });
        });
      case 'network':
        return Promise.reject(end(new TypeError('Failed to fetch')));
      case 'transient':
        return Promise.resolve(
          end(
            errorBody('temporarily_unavailable', 503, {
              retry_after_seconds: TRANSIENT_RETRY_AFTER,
            }),
          ),
        );
      case 'business':
        return Promise.resolve(
          end(
            errorBody('temporarily_unavailable', 503, {
              detail_code: 'loop_assistant_unavailable',
            }),
          ),
        );
      case 'not_found':
        return Promise.resolve(end(errorBody('not_found', 404)));
      case 'rate_limited':
        return Promise.resolve(end(errorBody('rate_limited', 429, { retry_after_seconds: 30 })));
      case 'ok':
        return Promise.resolve(end(Response.json({ answer: calls.length })));
    }
  };
  return { calls, fetch };
}

/** Un `fetch` que nunca responde salvo por aborto. */
export function silentFetch(clock: FakeClock) {
  return scriptedFetch(
    clock,
    Array.from({ length: 10_000 }, () => 'timeout' as const),
  );
}

/** Conexión real que anota el estado del circuito justo después de cada registro. */
export function recordingConnection(clock: FakeClock) {
  const inner = createPortConnection(clock);
  const after: ConnectionSnapshot[] = [];
  const connection: TransportConnection = {
    getSnapshot: () => inner.getSnapshot(),
    canRead: (dependency) => inner.canRead(dependency),
    canWrite: () => inner.canWrite(),
    register: (dependency, outcome) => {
      inner.register(dependency, outcome);
      after.push(inner.getSnapshot());
    },
  };
  return { connection, after };
}

export async function flush(): Promise<void> {
  for (let round = 0; round < 4; round += 1) {
    await new Promise<void>((resolve) => {
      setImmediate(resolve);
    });
  }
}

export type Settled<T> =
  { readonly ok: true; readonly value: T } | { readonly ok: false; readonly error: unknown };

/** Avanza el reloj de `stepMs` en `stepMs` hasta que `promise` termina (tope: `maxMs`). */
export async function drive<T>(
  clock: FakeClock,
  promise: Promise<T>,
  { stepMs = 1_000, maxMs = 300_000 } = {},
): Promise<Settled<T>> {
  const state: { settled: Settled<T> | null } = { settled: null };
  promise.then(
    (value) => (state.settled = { ok: true, value }),
    (error: unknown) => (state.settled = { ok: false, error }),
  );
  for (let elapsed = 0; ; elapsed += stepMs) {
    await flush();
    if (state.settled !== null) return state.settled;
    if (elapsed >= maxMs) throw new Error(`drive: la promesa no terminó en ${String(maxMs)} ms.`);
    clock.advance(stepMs);
  }
}
