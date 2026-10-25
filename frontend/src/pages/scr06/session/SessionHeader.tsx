/**
 * Cabecera de la sesión de walk-test (IF-13 paso 1, RV-09): estado, tipo, versión del catálogo,
 * pases por celda y las marcas del servidor (inicio y última actividad), más la reapertura
 * registrada si la hubo. Ninguna cifra se calcula aquí: todo sale de la sesión que devolvió el
 * servidor.
 */
import { ServerTimestamp } from '@/pages/scr06/common/ServerTimestamp';
import { fill } from '@/pages/scr06/common/format';
import type { WalkTestSession } from '@/ports/scr06Commissioning';
import { label } from '@/texts/labels';
import { text } from '@/texts';

import styles from './session.module.css';

export function SessionHeader({ session }: { readonly session: WalkTestSession }) {
  return (
    <header className={styles.header}>
      <h2 className={styles.title}>{text('scr06.session.title')}</h2>
      <dl className={styles.facts}>
        <dt>{text('scr06.session.status')}</dt>
        <dd data-part="session_status">{label('walk_test_status', session.status)}</dd>
        <dt>{text('scr06.session.kind')}</dt>
        <dd data-part="session_kind">{label('walk_test_kind', session.kind)}</dd>
        <dt>{text('scr06.session.catalog_version')}</dt>
        <dd data-part="catalog_version">{session.catalog_version}</dd>
        <dt>{text('scr06.session.passes_per_cell')}</dt>
        <dd data-part="passes_per_cell">{session.passes_per_cell}</dd>
        <dt>{text('scr06.session.started_at')}</dt>
        <dd>
          <ServerTimestamp instant={session.started_at} />
        </dd>
        <dt>{text('scr06.session.last_activity_at')}</dt>
        <dd>
          <ServerTimestamp instant={session.last_activity_at} />
        </dd>
      </dl>
      {session.reopened_at === null ? null : (
        <p className={styles.reopened} data-part="reopened">
          {fill(text('scr06.session.reopened'), { at: session.reopened_at })}
        </p>
      )}
      {session.reopen_reason_es === null ? null : (
        <p className={styles.reopenReason} data-part="reopen_reason">
          {`${text('scr06.session.reopen_reason')}: ${session.reopen_reason_es}`}
        </p>
      )}
    </header>
  );
}
