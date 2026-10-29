/**
 * Campana real de UI-02; el sondeo lo gobierna state.query, incluida la puerta D-4.
 *
 * Sistema visual v1 (VIG-364): un control de 44 px (48 en tableta) con la insignia de no leídas a
 * su lado, separada del nombre. «Actualizar» solo aparece cuando el recuento no está disponible:
 * el sondeo periódico sigue igual y la cabecera cabe en una fila.
 */
import { Link } from 'react-router';
import { readNotificationPages } from '@/ports/scr15Notifications';
import { text } from '@/texts';
import { NotificationsRefresh, useNotificationsContext, useNotificationsRead } from './shared';
import styles from './Bell.module.css';

export function Bell() {
  const { concession } = useNotificationsContext();
  const query = useNotificationsRead(
    'GET /notifications',
    { status: 'unread', all_pages: true },
    (signal) => readNotificationPages(Infinity, signal, true),
    true,
  );
  const count = query.error === null ? query.data?.items.length : undefined;
  if (concession !== null) return <span>{text('scr15.own_without_concession')}</span>;
  return (
    <div className={styles.bell}>
      <Link className={styles.bellLink} to="/notifications" aria-label={text('navigation.bell')}>
        {text('navigation.bell')}
      </Link>
      <span
        className={count === undefined ? styles.bellUnavailable : styles.bellBadge}
        role="status"
        aria-live="polite"
        aria-atomic="true"
        data-unread={count === undefined ? undefined : String(count)}
      >
        {count === undefined
          ? text('scr15.count_unavailable')
          : `${text('scr15.unread')}: ${String(count)}`}
      </span>
      {count === undefined && query.error !== null ? <NotificationsRefresh query={query} /> : null}
    </div>
  );
}
