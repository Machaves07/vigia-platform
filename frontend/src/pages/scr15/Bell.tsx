/** Campana real de UI-02; el sondeo lo gobierna state.query, incluida la puerta D-4. */
import { Link } from 'react-router';
import { readNotificationPages } from '@/ports/scr15Notifications';
import { text } from '@/texts';
import { NotificationsRefresh, useNotificationsContext, useNotificationsRead } from './shared';

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
    <div>
      <Link to="/notifications" aria-label={text('navigation.bell')}>
        {text('navigation.bell')}
      </Link>
      <span role="status" aria-live="polite" aria-atomic="true">
        {count === undefined
          ? text('scr15.count_unavailable')
          : `${text('scr15.unread')}: ${String(count)}`}
      </span>
      <NotificationsRefresh query={query} />
    </div>
  );
}
