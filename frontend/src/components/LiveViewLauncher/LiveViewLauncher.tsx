/**
 * UI-18 `LiveViewLauncher` (BR-APP-65 y 66, PAT-APP-SEG-03, LC-APP-18, PR-APP-20 y 29).
 *
 * Pantalla previa (BR-APP-66 con la nota U05-H-07) y apertura de la vista en vivo **en dos pasos**
 * (D-1): «abrir vista en vivo» pide el token al puerto, y con él aparece un enlace
 * `rel="noopener noreferrer"` a la dirección anunciada por el nodo con el token en el fragmento
 * (`<live_view_local_url>#token=<token>`) que la persona pulsa. El componente **nunca abre nada por sí mismo** (ni `window.open`): el enlace es el único
 * camino, también en tableta (U05-H-09).
 *
 * - El token vive en el `href` hasta el clic o hasta los 600 s (riesgo aceptado R-APP-4); el enlace
 *   **se elimina del DOM** al pulsarlo o al vencer, y con él se descarta el token de la memoria del
 *   componente. No pasa por la caché de consultas, ni por el almacenamiento, ni por el historial, ni
 *   por los registros: la petición se hace directamente al puerto.
 * - Sin `live_view_local_url` no hay enlace y se explica que el nodo no ha anunciado su dirección
 *   (pendiente nº 31). Un valor no utilizable tampoco produce enlace.
 * - Un rechazo de la plataforma (`zone_without_node`, `rate_limited` con sus 30 tokens cada 10 min)
 *   se presenta con UI-19 y su mapa cerrado de errores, y deshabilita «abrir de nuevo» exactamente
 *   el `retry_after_seconds` indicado.
 * - El tiempo entra por un `Clock` inyectado (PAT-APP-RES-06).
 */
import { useEffect, useLayoutEffect, useReducer } from 'react';

import { NoticeStack } from '@/components/NoticeStack/NoticeStack';
import {
  liveViewFailure,
  liveViewPort,
  LIVE_VIEW_LINK_REL,
  LIVE_VIEW_LINK_TARGET,
  type LiveViewPort,
} from '@/ports/liveview';
import type { Clock } from '@/state/clock';
import { isCountingDown, remainingSeconds, scheduleCountdownEnd } from '@/state/reducers/countdown';
import { text } from '@/texts';

import { initialLiveView, liveViewReducer, type LiveViewState } from './state';
import styles from './LiveViewLauncher.module.css';

/** Cadencia del tic de la cuenta regresiva; el último tic cae en el vencimiento exacto. */
const TICK_MS = 1000;
/** Identificador local del aviso de rechazo (nunca del servidor). */
const NOTICE_ID = 'live-view-token';

/** Lo que la pantalla previa explica antes de pedir nada (BR-APP-66 y nota U05-H-07). */
const PREVIEW_KEYS = [
  'live_view.local_network',
  'live_view.access_recorded',
  'live_view.blurred',
  'live_view.if_not_loading',
  'live_view.watermark',
  'live_view.open_again_token',
  'live_view.certificate_notice',
  'live_view.certificate_instruction',
] as const;

/** Causas de rechazo, con la etiqueta del nodo y de la plataforma. */
const REJECTION_KEYS = [
  'live_view.rejection.limit',
  'live_view.rejection.mode',
  'live_view.rejection.reused',
  'live_view.rejection.expired',
  'live_view.rejection.zone_without_node',
  'live_view.rejection.rate_limited',
] as const;

/** Acuse de apertura: sin el token, que ya se descartó (PR-APP-29). */
export interface LiveViewOpened {
  readonly zone_id: string;
  readonly opened_at_ms: number;
}

export interface LiveViewLauncherProps {
  readonly zone_id: string;
  readonly clock: Clock;
  /** Puerto de la vista en vivo (LC-APP-18); por omisión, `ports.liveview`. */
  readonly port?: LiveViewPort;
  /** Se emite al pulsar el enlace, nunca al pedir el token. */
  readonly onOpened?: (opened: LiveViewOpened) => void;
}

/** Clave del catálogo que explica por qué no hay enlace. */
function problemKey(problem: LiveViewState['problem']): string {
  return problem === 'unusable' ? 'live_view.unusable_url' : 'live_view.pending_local_url';
}

export function LiveViewLauncher({
  zone_id,
  clock,
  port = liveViewPort,
  onOpened,
}: LiveViewLauncherProps) {
  const [state, dispatch] = useReducer(liveViewReducer, initialLiveView);

  useEffect(() => {
    if (state.countdown.kind !== 'running') return undefined;
    const handle = clock.setTimeout(
      () => {
        dispatch({ type: 'tick', nowMs: clock.now() });
      },
      Math.min(TICK_MS, Math.max(0, state.countdown.untilMs - clock.now())),
    );
    return () => {
      clock.clearTimeout(handle);
    };
  });

  // Cierre exacto en el vencimiento (VIG-283): el enlace sale del DOM en `untilMs`, ni antes ni
  // después. Se engancha en el mismo compromiso que pone el enlace en el DOM (`useLayoutEffect`),
  // así que no hay ni un instante con el token en el DOM y sin temporizador armado.
  useLayoutEffect(
    () =>
      scheduleCountdownEnd(clock, state.countdown, (nowMs) => {
        dispatch({ type: 'tick', nowMs });
      }),
    [clock, state.countdown],
  );

  const nowMs = clock.now();
  const waiting = isCountingDown(state.countdown, nowMs);
  const seconds = remainingSeconds(state.countdown, nowMs);

  const request = () => {
    dispatch({ type: 'request', nowMs: clock.now() });
    // Una sola petición en vuelo: el botón queda deshabilitado mientras se espera, así que ninguna
    // respuesta puede pisar a otra. El token no se guarda en ningún sitio hasta que llega.
    void port.requestToken.write({ path: { zone_id } }).then(
      (handoff) => {
        dispatch({ type: 'granted', handoff, nowMs: clock.now() });
      },
      (error: unknown) => {
        const failure = liveViewFailure(error);
        if (failure !== null) {
          dispatch({ type: 'rejected', error: failure, nowMs: clock.now() });
        }
      },
    );
  };

  const open = () => {
    onOpened?.({ zone_id, opened_at_ms: clock.now() });
    dispatch({ type: 'opened' });
  };

  return (
    <section
      className={styles.launcher}
      aria-label={text('live_view.region_label')}
      data-part="launcher"
    >
      <h2 className={styles.title}>{text('live_view.title')}</h2>
      <ul className={styles.list} data-part="preview">
        {PREVIEW_KEYS.map((key) => (
          <li key={key} data-text-key={key}>
            {text(key)}
          </li>
        ))}
      </ul>
      <h3 className={styles.subtitle}>{text('live_view.rejections.title')}</h3>
      <ul className={styles.list} data-part="rejections">
        {REJECTION_KEYS.map((key) => (
          <li key={key} data-text-key={key}>
            {text(key)}
          </li>
        ))}
      </ul>

      <div className={styles.state} data-part="state">
        {state.phase === 'link' && state.href !== null ? (
          <p className={styles.link}>
            <a
              className={styles.anchor}
              href={state.href}
              target={LIVE_VIEW_LINK_TARGET}
              rel={LIVE_VIEW_LINK_REL}
              data-part="link"
              onClick={open}
            >
              {text('live_view.link')}
            </a>{' '}
            <span data-part="countdown">
              {`${text('live_view.countdown.link')} ${String(seconds)} ${text('live_view.countdown.seconds')}`}
            </span>
          </p>
        ) : null}

        {state.phase === 'no_link' ? (
          <p
            className={styles.blocked}
            role="alert"
            data-part="no_link"
            data-problem={state.problem ?? 'no_local_url'}
            data-text-key={problemKey(state.problem)}
          >
            {text(problemKey(state.problem))}
          </p>
        ) : null}

        {state.phase === 'requesting' ? (
          <p className={styles.pending} data-part="pending">
            {text('live_view.pending')}
          </p>
        ) : null}

        {state.error === null ? null : (
          <NoticeStack
            notices={[{ id: NOTICE_ID, error: state.error }]}
            clock={clock}
            onDismiss={() => {
              dispatch({ type: 'dismiss' });
            }}
          />
        )}

        {state.phase === 'link' ? null : (
          <div className={styles.actions}>
            <button
              type="button"
              className={styles.button}
              disabled={state.phase === 'requesting' || waiting}
              onClick={request}
              data-part="request"
            >
              {text(state.attempted ? 'live_view.open_again' : 'live_view.open')}
            </button>
            {waiting ? (
              <span data-part="wait">
                {`${text('live_view.countdown.wait')} ${String(seconds)} ${text('live_view.countdown.seconds')}`}
              </span>
            ) : null}
          </div>
        )}
      </div>
    </section>
  );
}
