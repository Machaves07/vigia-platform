/**
 * UI-06 `ConnectionNotice` (BR-APP-74, NFR-APP-37): aviso visible de `degraded` y `offline` con la
 * hora del último dato recibido y por qué las escrituras están deshabilitadas. Sin cola: nada se
 * guarda para enviarlo después; al volver la conexión, la persona repite lo pendiente.
 *
 * - `offline` (circuito `platform` abierto): `ports.base` no deja salir ninguna escritura.
 * - `degraded`: hay fallos recientes; las lecturas se reintentan y una escritura puede fallar.
 * - «Reintentar ahora» (`retryNow`) vuelve a pedir las lecturas montadas que no son auditadas:
 *   una lectura auditada solo se pide por acción explícita en su pantalla (A-6).
 *
 * Un estado fuera de la lista se muestra «desconocido» (NFR-APP-30).
 */
import { useSyncExternalStore, type ReactNode } from 'react';

import { TimestampWithOffset } from '@/components/TimestampWithOffset/TimestampWithOffset';
import type { ConnectionSnapshot } from '@/state/connection';
import type { ScreenQueryCache } from '@/state/query';
import { text } from '@/texts';

import styles from './AppShell.module.css';

/** Lo que el aviso usa de `state.connection`. */
interface ConnectionLike {
  readonly getSnapshot: () => ConnectionSnapshot;
  readonly subscribe: (listener: () => void) => () => void;
  readonly canWrite: () => boolean;
}

const STATE_MESSAGES: Readonly<Record<string, string>> = {
  degraded: 'app_shell.connection.degraded',
  offline: 'app_shell.connection.offline',
};

export function connectionMessageKey(state: string): string {
  return Object.hasOwn(STATE_MESSAGES, state) ? (STATE_MESSAGES[state] ?? '') : 'common.unknown';
}

/** Lecturas montadas que se pueden repetir sin acción explícita (no auditadas). */
function retryable(meta: Readonly<Record<string, unknown>> | undefined): boolean {
  const policy = meta?.policy;
  return (
    typeof policy === 'object' &&
    policy !== null &&
    (policy as { readonly explicitOnly?: unknown }).explicitOnly === false
  );
}

export function ConnectionNotice({
  connection,
  cache,
  timeZone,
}: {
  readonly connection: ConnectionLike;
  readonly cache: Pick<ScreenQueryCache, 'client'>;
  /** Zona en que se muestra la hora del último dato: la de la planta elegida, o UTC. */
  readonly timeZone: string;
}): ReactNode {
  const snapshot = useSyncExternalStore(connection.subscribe, connection.getSnapshot);
  const { state, last_success_at: lastSuccess } = snapshot.connection;
  if (state === 'online') return null;
  const messageKey = connectionMessageKey(state);
  const writesKey = connection.canWrite()
    ? 'app_shell.connection.writes_degraded'
    : 'app_shell.connection.writes_disabled';
  return (
    <section
      className={styles.notice}
      aria-label={text('app_shell.connection.label')}
      data-part="connection_notice"
      data-state={state}
    >
      <p className={styles.noticeText} role="status" data-text-key={messageKey}>
        {text(messageKey)}
      </p>
      <p className={styles.noticeText} data-part="last_data">
        {text('app_shell.connection.last_data')}:{' '}
        {lastSuccess === null ? (
          text('app_shell.connection.no_data')
        ) : (
          <TimestampWithOffset instant={lastSuccess} timeZone={timeZone} />
        )}
      </p>
      <p className={styles.noticeText} data-text-key={writesKey}>
        {text(writesKey)}
      </p>
      <button
        type="button"
        className={styles.button}
        onClick={() => {
          void cache.client.refetchQueries(
            { type: 'active', predicate: (query) => retryable(query.meta) },
            { throwOnError: false },
          );
        }}
      >
        {text('app_shell.connection.retry')}
      </button>
    </section>
  );
}
