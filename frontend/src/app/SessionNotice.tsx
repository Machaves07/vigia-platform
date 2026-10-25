/**
 * UI-05 `SessionNotice` (BR-APP-14 a 16, LC-APP-06): los avisos de vencimiento y la pantalla
 * bloqueada.
 *
 * - Aviso de inactividad: sale cuando `state.session` lo emite (a los 28 min de la última respuesta
 *   autenticada, una vez por ventana) y ofrece «seguir trabajando» (`keepAlive`, `GET /me`).
 *   Desaparece en cuanto el servidor renueva la ventana.
 * - Aviso absoluto (10 min antes): ninguna acción lo prolonga ni lo retira (BR-APP-15).
 * - `SessionLock`: con `locked_for_reauth`, `expired_idle` o `expired_absolute` la pantalla se
 *   sustituye por el formulario de inicio de sesión de SCR-01 en la misma pestaña. Desmontar la
 *   pantalla deja su borrador en `state.session`; al reautenticar la misma persona vuelve a la ruta
 *   guardada y la pantalla lo recupera (`reauthenticated`). La escritura que recibió
 *   `unauthenticated` no se reenvía: la persona la repite (BR-APP-16).
 *
 * Un estado fuera de la lista se muestra «desconocido» (NFR-APP-30).
 */
import type { ReactNode } from 'react';

import { LoginForm } from '@/pages/scr01/login/LoginForm';
import { isAuthenticated, useSession, useUiSession } from '@/state/session';
import { text } from '@/texts';

import styles from './AppShell.module.css';

/** Mensaje de la pantalla bloqueada por estado; cualquier otro estado es «desconocido». */
const LOCK_MESSAGES: Readonly<Record<string, string>> = {
  locked_for_reauth: 'errors.reauth',
  expired_idle: 'app_shell.session.expired_idle',
  expired_absolute: 'app_shell.session.expired_absolute',
};

export function lockMessageKey(status: string): string {
  return Object.hasOwn(LOCK_MESSAGES, status) ? (LOCK_MESSAGES[status] ?? '') : 'common.unknown';
}

/** Avisos de vencimiento de una sesión en uso; sin aviso vigente no pinta nada. */
export function SessionNotice(): ReactNode {
  const store = useSession();
  const session = useUiSession();
  if (!isAuthenticated(session.status)) return null;
  const idle = session.idle_warning_issued;
  const absolute = session.absolute_warning_issued;
  if (!idle && !absolute) return null;
  return (
    <section className={styles.notice} aria-label={text('app_shell.session.label')}>
      {idle ? (
        <div className={styles.noticeRow} role="alert" data-text-key="app_shell.session.idle">
          <p className={styles.noticeText}>{text('app_shell.session.idle')}</p>
          <button
            type="button"
            className={styles.button}
            onClick={() => {
              // Si falla, el transporte ya llevó la sesión al estado que corresponde.
              store.continueWorking().catch(() => undefined);
            }}
          >
            {text('app_shell.session.keep_working')}
          </button>
        </div>
      ) : null}
      {absolute ? (
        <p className={styles.noticeText} role="alert" data-text-key="app_shell.session.absolute">
          {text('app_shell.session.absolute')}
        </p>
      ) : null}
    </section>
  );
}

/** Pantalla bloqueada: motivo y reautenticación en la misma pestaña. */
export function SessionLock({ status }: { readonly status: string }): ReactNode {
  const key = lockMessageKey(status);
  return (
    <div className={styles.lock} data-part="session_lock">
      <p className={styles.lockMessage} role="alert" data-text-key={key}>
        {text(key)}
      </p>
      <LoginForm />
    </div>
  );
}
