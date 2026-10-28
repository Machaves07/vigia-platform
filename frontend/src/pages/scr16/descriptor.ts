/**
 * SCR-16 · Vista de transparencia (U-03; token U-02; flujo U-06), VIG-326, escritorio y tableta.
 * Grupos: `zone` (ruta canónica) y `entry` (`/transparency`, elección de zona), que es el inicio
 * del COPASST.
 */
import type { ScreenDescriptor } from '@/app/descriptor';

export const descriptor: ScreenDescriptor = {
  screen_id: 'SCR-16',
  title_text: 'navigation.scr16',
  route: '/transparency/:zone_id',
  required_permissions: ['transparency.read', 'live_view.open'],
  route_groups: [
    {
      id: 'entry',
      paths: ['/transparency'],
      access: 'permission',
      required_permissions: ['transparency.read'],
      menu: { title_text: 'navigation.menu.transparency' },
      load: () => import('./entry'),
    },
    {
      id: 'zone',
      paths: ['/transparency/:zone_id'],
      access: 'permission',
      required_permissions: ['transparency.read', 'live_view.open'],
      load: () => import('./zone'),
    },
  ],
  sections: [
    { id: 'transparency', group: 'zone', required_permissions: ['transparency.read'] },
    { id: 'live_view', group: 'zone', required_permissions: ['live_view.open'] },
  ],
  expected_roles: ['copasst'],
  backend_unit: 'U-03',
  home_for_roles: ['copasst'],
  home_group: 'entry',
  device_classes: ['desktop', 'tablet'],
};
