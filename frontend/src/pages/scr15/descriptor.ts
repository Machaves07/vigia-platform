/**
 * SCR-15 · Notificaciones y preferencias (U-04), VIG-318. Grupos: `list` y `preferences`; la
 * campana de la cabecera es `Bell.tsx`, fuera de los grupos de rutas.
 */
import type { ScreenDescriptor } from '@/app/descriptor';

export const descriptor: ScreenDescriptor = {
  screen_id: 'SCR-15',
  title_text: 'navigation.scr15',
  route: '/notifications',
  required_permissions: ['notifications.read'],
  route_groups: [
    {
      id: 'list',
      paths: ['/notifications'],
      access: 'permission',
      required_permissions: ['notifications.read'],
      load: () => import('./list'),
    },
    {
      id: 'preferences',
      paths: ['/notifications/preferences'],
      access: 'permission',
      required_permissions: ['notifications.read'],
      load: () => import('./preferences'),
    },
  ],
  sections: [
    { id: 'notifications', group: 'list', required_permissions: ['notifications.read'] },
    { id: 'preferences', group: 'preferences', required_permissions: ['notifications.read'] },
  ],
  expected_roles: [
    'coordinator_sst',
    'administrator',
    'provider_installer',
    'plant_manager',
    'line_manager',
    'copasst',
  ],
  backend_unit: 'U-04',
  home_for_roles: [],
  home_group: null,
  device_classes: ['desktop'],
};
