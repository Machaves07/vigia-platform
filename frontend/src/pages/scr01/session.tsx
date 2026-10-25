/**
 * Piezas comunes de las rutas de sesión de SCR-01 (VIG-321): validación estructural (BR-APP-42),
 * la cuenta regresiva de `throttled` (BR-APP-11), el aviso de rechazo con **una sola clave de
 * texto** para cualquier causa (BR-APP-10) y el cierre del inicio de sesión (IF-01, paso 5).
 */
import { useEffect, useReducer, useState, type ReactNode } from 'react';
import type { NavigateFunction } from 'react-router';

import { sessionClock, sessionFailure, type SessionFailure } from '@/ports/scr01Session';
import {
  countdownReducer,
  idleCountdown,
  isCountingDown,
  remainingSeconds,
} from '@/state/reducers/countdown';
import type { SessionStore } from '@/state/session/store';
import { text } from '@/texts';

import styles from './scr01.module.css';

/** Forma de un correo (la interfaz solo valida estructura; el servidor decide). */
export const EMAIL = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
/** Longitud mínima de la contraseña (BR-NUC-20); las listas filtradas las verifica el servidor. */
export const PASSWORD_MIN_LENGTH = 8;
/** TOTP de 6 dígitos. */
export const TOTP = /^\d{6}$/;
/** Código de recuperación: 10 letras o dígitos, con guiones o espacios al copiarlo. */
const RECOVERY = /^[A-Za-z0-9]{10}$/;

export function isSecondFactorCode(code: string): boolean {
  const trimmed = code.trim();
  return TOTP.test(trimmed) || RECOVERY.test(trimmed.replace(/[\s-]/g, ''));
}

/** Rutas a las que no se vuelve tras entrar: la de inicio de sesión y la de invitación. */
function isEntryRoute(route: string): boolean {
  return /^\/(?:login|invitations)(?:[/?#]|$)/.test(route);
}

/** La ruta guardada (bloqueo, aviso) o el inicio; nunca otra vez el inicio de sesión. */
export function returnRoute(session: SessionStore): string {
  const route = session.takeReturnRoute();
  return route === null || isEntryRoute(route) ? '/' : route;
}

/**
 * Sesión completa en el servidor: carga `GET /me` y vuelve a la ruta guardada. Si falta aceptar el
 * aviso, el transporte ya pasó la sesión a `privacy_notice_pending` (`transportHooks`) y la puerta
 * del aviso ocupa la pantalla: no se navega, la ruta guardada la usa la aceptación.
 */
export async function completeSignIn(
  session: SessionStore,
  navigate: NavigateFunction,
): Promise<void> {
  try {
    await session.load();
  } catch (error) {
    if (sessionFailure(error).kind === 'privacy_notice_required') return;
    throw error;
  }
  await navigate(returnRoute(session), { replace: true });
}

/**
 * Cuenta regresiva de `throttled` con el reloj del transporte: deshabilita el envío exactamente
 * `retry_after_seconds` y lo rehabilita sola; sin valor, no bloquea (BR-APP-11).
 */
export function useThrottle() {
  const [state, dispatch] = useReducer(countdownReducer, idleCountdown);
  const [now, setNow] = useState(0);
  useEffect(() => {
    if (state.kind !== 'running') return undefined;
    const clock = sessionClock();
    const left = state.untilMs - clock.now();
    const handle = clock.setTimeout(
      () => {
        const at = clock.now();
        setNow(at);
        dispatch({ type: 'tick', nowMs: at });
      },
      Math.max(0, Math.min(left, left % 1000 || 1000)),
    );
    return () => {
      clock.clearTimeout(handle);
    };
  }, [state, now]);
  return {
    blocked: isCountingDown(state, now),
    remaining: remainingSeconds(state, now),
    start(seconds: number | null) {
      const at = sessionClock().now();
      setNow(at);
      dispatch({ type: 'start', seconds, nowMs: at });
    },
  };
}

/**
 * Aviso de un rechazo. `invalidKey` es la única clave para las credenciales o el código
 * rechazados; nunca se pinta `message_es` ni un detalle del servidor (BR-APP-10).
 */
export function FailureNotice({
  failure,
  invalidKey,
  remaining,
}: {
  readonly failure: SessionFailure | null;
  readonly invalidKey: string;
  readonly remaining: number;
}): ReactNode {
  if (failure === null) return null;
  switch (failure.kind) {
    case 'throttled':
      // Terminada la cuenta, el envío vuelve a estar disponible y el aviso se retira.
      if (failure.retryAfterSeconds !== null && remaining === 0) return null;
      return remaining > 0 ? (
        <p className={styles.error} role="status" data-text-key="scr01.throttled">
          {text('scr01.throttled')} {String(remaining)} {text('scr01.seconds')}
        </p>
      ) : (
        <p className={styles.error} role="status" data-text-key="scr01.throttled_later">
          {text('scr01.throttled_later')}
        </p>
      );
    case 'unavailable':
      return (
        <p className={styles.error} role="alert" data-text-key="scr01.unavailable">
          {text('scr01.unavailable')}
        </p>
      );
    case 'error':
      return (
        <div role="alert" data-text-key="scr01.error">
          <p className={styles.error}>{text('scr01.error')}</p>
          {failure.correlationId === undefined ? null : (
            <p className={styles.hint}>
              {text('scr01.correlation_id')}: <code>{failure.correlationId}</code>
            </p>
          )}
        </div>
      );
    default:
      return (
        <p className={styles.error} role="alert" data-text-key={invalidKey}>
          {text(invalidKey)}
        </p>
      );
  }
}
