/** SCR-15: SUP-U05-33 a 35 y lectura de preferencias del propio usuario. */
import type { components } from '@/client/schema';
import { defineScreenPort, isPortError } from './base';
import { currentTransport } from './base/http';

export type Notification = components['schemas']['NotificationOut'];
export type Preferences = components['schemas']['NotificationPreferenceOut'];
export type PreferenceInput = components['schemas']['NotificationPreferenceUpdateIn'];

export const notificationsPort = defineScreenPort('SCR-15', {
  list: {
    kind: 'read',
    status: 'fixed',
    permission: 'notifications.read',
    operationId: 'list_notifications_notifications_get',
  },
  markRead: {
    kind: 'write',
    status: 'fixed',
    permission: 'notifications.read',
    operationId: 'mark_notification_read_notifications__notification_id__read_post',
  },
  preferences: {
    kind: 'read',
    status: 'fixed',
    permission: 'notifications.read',
    operationId: 'read_preferences_notifications_preferences_get',
  },
  savePreferences: {
    kind: 'write',
    status: 'fixed',
    permission: 'notifications.read',
    operationId: 'update_preferences_notifications_preferences_put',
  },
});

export function notificationsClock() {
  return currentTransport().clock;
}

export function notificationsFailure(
  error: unknown,
): 'error' | 'unavailable' | 'not_available_in_scope' {
  if (!isPortError(error)) return 'error';
  const failure = error.failure;
  if (failure.kind === 'offline' || failure.kind === 'unavailable') return 'unavailable';
  if (failure.kind === 'http' && failure.behaviour.kind === 'not_available_in_scope')
    return 'not_available_in_scope';
  return 'error';
}

/** El servidor usa `after`/`next_after` (no `cursor`) y no entrega un total. */
export async function readNotificationPages(pages: number, signal: AbortSignal, unread = false) {
  const items = new Map<string, Notification>();
  const seen = new Set<string>();
  let after: string | null = null;
  for (let page = 0; page < pages; page++) {
    const result = await notificationsPort.list.read(
      {
        query: {
          limit: unread ? 200 : 50,
          ...(unread ? { status: 'unread' } : {}),
          ...(after === null ? {} : { after }),
        },
      },
      signal,
    );
    for (const item of result.items) items.set(item.notification_id, item);
    after = result.next_after;
    if (after === null) break;
    if (seen.has(after)) throw new Error('Cursor de notificaciones repetido.');
    seen.add(after);
  }
  return { items: [...items.values()], next_after: after };
}
