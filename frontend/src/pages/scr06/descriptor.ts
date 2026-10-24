/**
 * SCR-06 · Consola de comisionamiento y walk-test (U-03; token U-02), escritorio y tableta.
 * Grupos: `session` y `matrix` (VIG-310), `occlusion`, `close` y `record` (segunda tarea de la
 * pantalla). La entrada canónica es `session`.
 */
import type { ScreenDescriptor } from '@/app/descriptor';

export const descriptor: ScreenDescriptor = {
  screen_id: 'SCR-06',
  title_key: 'navigation.scr06',
  route: '/zones/:zone_id/commissioning',
  required_permissions: ['commissioning.run', 'live_view.open'],
  route_groups: [
    {
      id: 'session',
      paths: ['/zones/:zone_id/commissioning'],
      access: 'permission',
      required_permissions: ['commissioning.run', 'live_view.open'],
      load: () => import('./session'),
    },
    {
      id: 'matrix',
      paths: ['/zones/:zone_id/commissioning/matrix'],
      access: 'permission',
      required_permissions: ['commissioning.run'],
      load: () => import('./matrix'),
    },
    {
      id: 'occlusion',
      paths: ['/zones/:zone_id/commissioning/occlusion'],
      access: 'permission',
      required_permissions: ['commissioning.run'],
      load: () => import('./occlusion'),
    },
    {
      id: 'close',
      paths: ['/zones/:zone_id/commissioning/close'],
      access: 'permission',
      required_permissions: ['commissioning.run'],
      load: () => import('./close'),
    },
    {
      id: 'record',
      paths: ['/zones/:zone_id/commissioning/records/:record_id'],
      access: 'permission',
      required_permissions: ['commissioning.run'],
      load: () => import('./record'),
    },
  ],
  sections: [
    { id: 'walk_test', group: 'session', required_permissions: ['commissioning.run'] },
    { id: 'live_view', group: 'session', required_permissions: ['live_view.open'] },
  ],
  expected_roles: ['provider_installer'],
  backend_unit: 'U-03',
  home_for_roles: [],
  home_group: null,
  device_classes: ['desktop', 'tablet'],
};
