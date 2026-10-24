/**
 * Cuenta regresiva pura (LC-APP-10, BR-APP-11, PR-APP-03).
 *
 * Deshabilita una acción **exactamente** el tiempo que el servidor indica y la rehabilita sola:
 * desde el instante de inicio hasta `untilMs` sin incluirlo. Sin `retry_after_seconds` (ausente,
 * nulo, negativo, no entero) no deshabilita nada: la interfaz no impone bloqueos propios. Un aviso
 * nuevo sustituye al anterior; no se cuentan intentos (BR-APP-11).
 *
 * La misma máquina sirve para los vencimientos: el token de vista en vivo (600 s) y la URL de
 * evidencia (≤ 5 min). El reductor no lee la hora: los eventos llevan `nowMs` de un `Clock`, y
 * `scheduleCountdownEnd` programa el fin con ese mismo reloj.
 */
import type { Clock } from '@/state/clock';

/** Vigencia del token de vista en vivo (PAT-APP-SEG-03). */
export const LIVE_VIEW_TOKEN_SECONDS = 600;

/** Vigencia máxima de una URL de evidencia (BR-APP-58). */
export const EVIDENCE_URL_MAX_SECONDS = 300;

export type CountdownState =
  { readonly kind: 'idle' } | { readonly kind: 'running'; readonly untilMs: number };

export type CountdownEvent =
  /** `retry_after_seconds` (o la vigencia en segundos) recibido en `nowMs`. */
  | { readonly type: 'start'; readonly seconds: number | null | undefined; readonly nowMs: number }
  /** Vencimiento absoluto `untilMs`, acotado a `maxSeconds` desde `nowMs`. */
  | {
      readonly type: 'start_until';
      readonly untilMs: number;
      readonly maxSeconds: number;
      readonly nowMs: number;
    }
  | { readonly type: 'tick'; readonly nowMs: number }
  | { readonly type: 'reset' };

export const idleCountdown: CountdownState = Object.freeze({ kind: 'idle' });

function isSeconds(value: number | null | undefined): value is number {
  return typeof value === 'number' && Number.isSafeInteger(value) && value >= 0;
}

function running(nowMs: number, untilMs: number): CountdownState {
  return untilMs > nowMs ? { kind: 'running', untilMs } : idleCountdown;
}

export function countdownReducer(state: CountdownState, event: CountdownEvent): CountdownState {
  switch (event.type) {
    case 'start':
      return isSeconds(event.seconds)
        ? running(event.nowMs, event.nowMs + event.seconds * 1000)
        : idleCountdown;
    case 'start_until': {
      if (!Number.isFinite(event.untilMs) || !isSeconds(event.maxSeconds)) return idleCountdown;
      return running(event.nowMs, Math.min(event.untilMs, event.nowMs + event.maxSeconds * 1000));
    }
    case 'tick':
      return state.kind === 'running' && event.nowMs >= state.untilMs ? idleCountdown : state;
    case 'reset':
      return idleCountdown;
  }
}

/** La acción está deshabilitada en `nowMs` (o el vencimiento aún no llegó). */
export function isCountingDown(state: CountdownState, nowMs: number): boolean {
  return state.kind === 'running' && nowMs < state.untilMs;
}

/** Segundos que faltan, redondeados hacia arriba, para mostrar; 0 si no hay cuenta. */
export function remainingSeconds(state: CountdownState, nowMs: number): number {
  return state.kind === 'running' && nowMs < state.untilMs
    ? Math.ceil((state.untilMs - nowMs) / 1000)
    : 0;
}

/**
 * Programa en `clock` el tic que cierra la cuenta y llama a `onEnd(nowMs)` al vencer.
 * Devuelve la cancelación; sin cuenta en curso no programa nada.
 */
export function scheduleCountdownEnd(
  clock: Clock,
  state: CountdownState,
  onEnd: (nowMs: number) => void,
): () => void {
  if (state.kind !== 'running') return () => undefined;
  const handle = clock.setTimeout(
    () => {
      onEnd(clock.now());
    },
    Math.max(0, state.untilMs - clock.now()),
  );
  return () => {
    clock.clearTimeout(handle);
  };
}
