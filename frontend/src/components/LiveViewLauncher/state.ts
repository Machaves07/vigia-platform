/**
 * Máquina de la entrega de la vista en vivo (LC-APP-18, PAT-APP-SEG-03, BR-APP-65, PR-APP-20 y 29).
 *
 * Estados: `idle` (nada pedido, o entrega ya consumida), `requesting`, `link` (hay enlace en el
 * DOM), `no_link` (la entrega llegó sin dirección anunciada o con un valor no utilizable) y
 * `rejected` (la plataforma rechazó la petición).
 *
 * El `LiveViewHandoff` y el `href` que lo contiene **solo** existen con `phase === 'link'`, y se
 * descartan al pulsar el enlace o al vencer la cuenta: después, ni el token ni la URL están en la
 * memoria del componente (`href` vuelve a `null`). Este reductor es puro: el tiempo entra por los
 * eventos (`nowMs` de un `Clock`) y la cuenta regresiva es la de VIG-283, la misma que usan los
 * avisos para `retry_after_seconds`.
 */
import type { HttpError } from '@/ports/errors/map';
import { behaviourFor } from '@/ports/errors/map';
import { liveViewLink, type LiveViewHandoff, type LiveViewLinkProblem } from '@/ports/liveview';
import {
  countdownReducer,
  idleCountdown,
  LIVE_VIEW_TOKEN_SECONDS,
  type CountdownState,
} from '@/state/reducers/countdown';

type LiveViewPhase = 'idle' | 'requesting' | 'link' | 'no_link' | 'rejected';

export interface LiveViewState {
  readonly phase: LiveViewPhase;
  /** Ya se pidió un token alguna vez: el botón dice «abrir de nuevo». */
  readonly attempted: boolean;
  /** Enlace vivo; `null` en todo lo demás. Es el único sitio donde vive el token. */
  readonly href: string | null;
  /** Motivo por el que no hay enlace; solo con `phase === 'no_link'`. */
  readonly problem: LiveViewLinkProblem | null;
  /** Último rechazo de la plataforma; solo con `phase === 'rejected'`. Nunca lleva el token. */
  readonly error: HttpError | null;
  /** Vigencia del enlace (600 s) o espera del rechazo (`retry_after_seconds`). */
  readonly countdown: CountdownState;
}

export const initialLiveView: LiveViewState = Object.freeze({
  phase: 'idle',
  attempted: false,
  href: null,
  problem: null,
  error: null,
  countdown: idleCountdown,
});

export type LiveViewEvent =
  /** La persona pide un token (o pulsa «abrir de nuevo»). */
  | { readonly type: 'request'; readonly nowMs: number }
  /** La plataforma entregó el token y la dirección local del nodo. */
  | { readonly type: 'granted'; readonly handoff: LiveViewHandoff; readonly nowMs: number }
  /** La plataforma rechazó la petición (`zone_without_node`, `rate_limited`, …). */
  | { readonly type: 'rejected'; readonly error: HttpError; readonly nowMs: number }
  /** La persona pulsó el enlace: la pestaña nueva ya se abrió. */
  | { readonly type: 'opened' }
  /** La persona cerró el aviso de rechazo (UI-19). */
  | { readonly type: 'dismiss' }
  /** Tic de la cuenta regresiva (o su vencimiento). */
  | { readonly type: 'tick'; readonly nowMs: number };

/** Espera que impone el rechazo: la del mapa cerrado, y solo si la impone (BR-APP-11, 71). */
function retrySeconds(error: HttpError): number | null {
  const behaviour = behaviourFor(error);
  return behaviour.kind === 'countdown' || behaviour.kind === 'transient'
    ? behaviour.retry_after_seconds
    : null;
}

/** Cierra la entrega: el enlace sale del DOM y el token se descarta de la memoria. */
function discard(state: LiveViewState): LiveViewState {
  return { ...state, phase: 'idle', href: null, countdown: idleCountdown };
}

export function liveViewReducer(state: LiveViewState, event: LiveViewEvent): LiveViewState {
  switch (event.type) {
    case 'request':
      return {
        ...state,
        phase: 'requesting',
        attempted: true,
        href: null,
        problem: null,
        error: null,
        countdown: idleCountdown,
      };
    case 'granted': {
      const link = liveViewLink(event.handoff);
      if (link.kind !== 'link') {
        return {
          ...state,
          phase: 'no_link',
          href: null,
          problem: link.kind,
          error: null,
          countdown: idleCountdown,
        };
      }
      return {
        ...state,
        phase: 'link',
        href: link.href,
        problem: null,
        error: null,
        countdown: countdownReducer(idleCountdown, {
          type: 'start',
          seconds: LIVE_VIEW_TOKEN_SECONDS,
          nowMs: event.nowMs,
        }),
      };
    }
    case 'rejected':
      return {
        ...state,
        phase: 'rejected',
        href: null,
        problem: null,
        error: event.error,
        countdown: countdownReducer(idleCountdown, {
          type: 'start',
          seconds: retrySeconds(event.error),
          nowMs: event.nowMs,
        }),
      };
    case 'opened':
      return state.phase === 'link' ? discard(state) : state;
    case 'dismiss':
      return state.phase === 'rejected'
        ? { ...state, phase: 'idle', error: null, countdown: idleCountdown }
        : state;
    case 'tick': {
      if (state.countdown.kind !== 'running') {
        // Un enlace no sobrevive sin cuenta regresiva: se cierra aquí (temporizador perdido).
        return state.phase === 'link' ? discard(state) : state;
      }
      const countdown = countdownReducer(state.countdown, { type: 'tick', nowMs: event.nowMs });
      // Al vencer, el enlace se elimina del DOM y el token se descarta con él.
      if (state.phase === 'link' && countdown.kind === 'idle') return discard(state);
      return { ...state, countdown };
    }
  }
}
