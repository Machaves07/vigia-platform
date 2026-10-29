/**
 * IF-22: paginación por clave y lectura individual; no hay operación por lote.
 *
 * VIG-366: de 20 en 20 («1–20 de 37», Anterior y Siguiente; en la última página, Siguiente pide la
 * página siguiente al servidor) y «Marcar todas como leídas», que marca una a una las sin leer ya
 * cargadas con la misma lectura individual (no existe la operación por lote).
 */
import { useState } from 'react';
import { Link } from 'react-router';
import { usePlantTimeZone } from '@/app/AppShell';
import { Pager, usePaged } from '@/components/Pager/Pager';
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
  const [markingAll, setMarkingAll] = useState(false);
  const [markAllFailed, setMarkAllFailed] = useState(false);
  const timeZone = usePlantTimeZone();
  const query = useNotificationsRead('GET /notifications', { pages }, (signal) =>
    readNotificationPages(pages, signal),
  );
  const mutation = useScreenMutation({
    write: (id: string) => notificationsPort.markRead.write({ path: { notification_id: id } }),
    invalidates: { screen: 'scr15', resources: ['notifications'] },
  });
  const data = query.error === null ? query.data : undefined;
  const items = data?.items ?? [];
  const paged = usePaged(items);
  const unread = items.filter((item) => item.read_at === null);
  const busy = markingAll || mutation.status === 'pending';
  const markAll = async () => {
    setMarkingAll(true);
    setMarkAllFailed(false);
    try {
      // Una a una, en orden: si una falla, las demás quedan sin leer y se dice.
      for (const item of unread) await mutation.run(item.notification_id);
    } catch {
      setMarkAllFailed(true);
    } finally {
      setMarkingAll(false);
    }
  };
  return (
    <main className={styles.page}>
      <h1>{text('scr15.title')}</h1>
      <Link to="/notifications/preferences">{text('scr15.preferences')}</Link>
      <NotificationsRefresh query={query} />
      {mutation.error !== null && !markAllFailed && <ScreenState state="error" />}
      {markAllFailed && <p role="alert">{text('scr15.mark_all_partial')}</p>}
      <NotificationsResult query={query}>
        {items.length === 0 ? (
          <ScreenState state="empty" message={text('scr15.empty')} />
        ) : (
          <>
            {unread.length > 0 && (
              <button
                type="button"
                disabled={busy}
                aria-busy={markingAll}
                onClick={() => {
                  void markAll();
                }}
              >
                {`${text('scr15.mark_all_read')} (${String(unread.length)})`}
              </button>
            )}
            <ul className={styles.list} aria-label={text('scr15.title')}>
              {paged.visible.map((item) => (
                <li key={item.notification_id} data-read={item.read_at !== null}>
                  <NotificationView item={item} timeZone={timeZone} />
                  {item.read_at === null && (
                    <button
                      type="button"
                      disabled={busy}
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
            <Pager
              page={paged.page}
              total={items.length}
              onPage={paged.setPage}
              more={data?.next_after != null}
              loading={query.isFetching}
              onLoadMore={() => {
                setPages((n) => n + 1);
              }}
              label={text('scr15.pages')}
            />
          </>
        )}
      </NotificationsResult>
    </main>
  );
}

export default function Page() {
  const context = useNotificationsContext();
  return <List key={`${context.scope}:${context.concession ?? ''}`} />;
}
