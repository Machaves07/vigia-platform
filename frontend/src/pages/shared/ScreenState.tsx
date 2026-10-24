/**
 * Estados comunes de toda pantalla (LC-APP-20, `load_state` de `domain-entities.md` §1).
 *
 * - `loading`: esqueleto sin números ni textos de datos.
 * - `ready`: pinta el contenido de la pantalla.
 * - `empty`: el texto específico de la pantalla, que llega ya resuelto del catálogo.
 * - `error`: aviso genérico con el `correlation_id` copiable, si lo hay, y «repetir».
 * - `unavailable`: puerto sin implementación o modo sin conexión (también las páginas de relevo).
 * - `not_available_in_scope`: «no disponible en su alcance» con la vuelta al inicio. Es el mismo
 *   árbol para una pantalla sin permiso y para un recurso `not_found`: la interfaz nunca distingue
 *   «no existe» de «no es suyo» (BR-APP-03, 77).
 */
import type { ReactNode } from 'react';
import { Link } from 'react-router';

import { text } from '@/texts';

import styles from './ScreenState.module.css';

export type ScreenStateProps =
  | { readonly state: 'loading' }
  | { readonly state: 'ready'; readonly children: ReactNode }
  | { readonly state: 'empty'; readonly message: string }
  | { readonly state: 'error'; readonly correlationId?: string; readonly onRetry?: () => void }
  | { readonly state: 'unavailable' }
  | { readonly state: 'not_available_in_scope' };

export function ScreenState(props: ScreenStateProps): ReactNode {
  switch (props.state) {
    case 'loading':
      return (
        <div className={styles.skeleton} role="status" aria-busy="true">
          <span className={styles.visuallyHidden}>{text('screen_state.loading')}</span>
          <span className={styles.bar} />
          <span className={styles.bar} />
          <span className={styles.bar} />
        </div>
      );
    case 'ready':
      return props.children;
    case 'empty':
      return <p className={styles.message}>{props.message}</p>;
    case 'error':
      return (
        <div className={styles.panel} role="alert">
          <p className={styles.message}>{text('screen_state.error')}</p>
          {props.correlationId === undefined ? null : (
            <p className={styles.detail}>
              {text('screen_state.correlation_id')}: <code>{props.correlationId}</code>
            </p>
          )}
          {props.onRetry === undefined ? null : (
            <button type="button" onClick={props.onRetry}>
              {text('screen_state.retry')}
            </button>
          )}
        </div>
      );
    case 'unavailable':
      return (
        <p className={styles.message} role="status">
          {text('screen_state.unavailable')}
        </p>
      );
    case 'not_available_in_scope':
      return <NotAvailableInScope />;
  }
}

/** Pantalla uniforme de BR-APP-03 y 77, también para la ruta que no existe. */
export function NotAvailableInScope(): ReactNode {
  return (
    <main className={styles.panel}>
      <h1 className={styles.title}>{text('errors.not_available_in_scope')}</h1>
      <Link to="/">{text('screen_state.back_home')}</Link>
    </main>
  );
}
