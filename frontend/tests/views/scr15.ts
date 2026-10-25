/** RV-19 sobre componentes de producción; el contrato propio no contiene organization_id. */
import { createElement } from 'react';
import { MemoryRouter } from 'react-router';
import { NotificationView } from '@/pages/scr15/NotificationView';
import { PreferencesFields } from '@/pages/scr15/preferences/PreferencesFields';
import type { Notification, Preferences } from '@/ports/scr15Notifications';
import { readView } from '../generators';
import type { ViewRegistration } from './types';

export default {
  notification: {
    screen: 'SCR-15',
    view: 'RV-19',
    data: readView('NotificationOut'),
    render: (data) =>
      createElement(
        MemoryRouter,
        {},
        createElement(NotificationView, { item: data as unknown as Notification }),
      ),
    allowedFields: ['title_es', 'urgency', 'deep_link_ref', 'kind', 'id', 'created_at', 'read_at'],
  },
  preferences: {
    screen: 'SCR-15',
    view: 'RV-19',
    data: readView('NotificationPreferenceOut'),
    render: (data) =>
      createElement(PreferencesFields, {
        data: data as unknown as Preferences,
        draft: data as unknown as Preferences,
        onChange: () => undefined,
      }),
    allowedFields: [
      'email_enabled_normal',
      'email_enabled_digest',
      'weekly_digest_weekday',
      'email_suppressed_hard_bounce',
      'email_suppressed_complaint',
    ],
  },
} satisfies Record<string, ViewRegistration>;
