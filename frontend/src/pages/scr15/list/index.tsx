/** IF-22: paginación por clave y lectura individual; no hay operación por lote. */
import { useState } from 'react';
import { Link } from 'react-router';
import { ScreenState } from '@/pages/shared/ScreenState';
import { notificationsPort, readNotificationPages } from '@/ports/scr15Notifications';
import { useScreenMutation } from '@/state/query';
import { text } from '@/texts';
import { NotificationView } from '../NotificationView';
import {
  NotificationsRefresh,
  NotificationsResult,
  useNotificationsContext,
  useNotificationsRead,
} from '../shared';
import styles from '../scr15.module.css';

function List() {
  const [pages, setPages] = useState(1);
  const query = useNotificationsRead('GET /notifications', { pages }, (signal) =>
    readNotificationPages(pages, signal),
  );
  const mutation = useScreenMutation({
    write: (id: string) => notificationsPort.markRead.write({ path: { notification_id: id } }),
    invalidates: { screen: 'scr15', resources: ['notifications'] },
  });
  const data = query.error === null ? query.data : undefined;
  return (
    <main className={styles.page}>
      <h1>{text('scr15.title')}</h1>
      <Link to="/notifications/preferences">{text('scr15.preferences')}</Link>
      <NotificationsRefresh query={query} />
      {mutation.error !== null && <ScreenState state="error" />}
      <NotificationsResult query={query}>
        {data?.items.length === 0 ? (
          <ScreenState state="empty" message={text('scr15.empty')} />
        ) : (
          <ul className={styles.list} aria-label={text('scr15.title')}>
            {data?.items.map((item) => (
              <li key={item.notification_id}>
                <NotificationView item={item} />
                {item.read_at === null && (
                  <button
                    type="button"
                    disabled={mutation.status === 'pending'}
                    onClick={() => {
                      void mutation.run(item.notification_id).catch(() => undefined);
                    }}
                  >
                    {text('scr15.mark_read')}
                  </button>
                )}
              </li>
            ))}
          </ul>
        )}
        {data?.next_after != null && (
          <button
            type="button"
            disabled={query.isFetching}
            onClick={() => {
              setPages((n) => n + 1);
            }}
          >
            {text('scr15.load_more')}
          </button>
        )}
      </NotificationsResult>
    </main>
  );
}

export default function Page() {
  const context = useNotificationsContext();
  return <List key={`${context.scope}:${context.concession ?? ''}`} />;
}
