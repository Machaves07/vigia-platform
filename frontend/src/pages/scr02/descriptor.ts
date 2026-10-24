/**
 * SCR-02 · Concesiones (U-02), VIG-322. Grupos: `client` (gerente) y `provider` (proveedor; es su
 * inicio, IF-06).
 */
import type { ScreenDescriptor } from '@/app/descriptor';

export const descriptor: ScreenDescriptor = {
  screen_id: 'SCR-02',
  title_text: 'navigation.scr02',
  route: '/concessions',
  required_permissions: ['concessions.read', 'concessions.revoke', 'concessions.grant'],
  route_groups: [
    {
      id: 'client',
      paths: ['/concessions'],
      access: 'permission',
      required_permissions: ['concessions.read', 'concessions.revoke'],
      load: () => import('./client'),
    },
    {
      id: 'provider',
      paths: ['/provider/concessions'],
      access: 'permission',
      required_permissions: ['concessions.grant'],
      linked_screens: ['SCR-06', 'SCR-07'],
      load: () => import('./provider'),
    },
  ],
  sections: [
    { id: 'client_concessions', group: 'client', required_permissions: ['concessions.read'] },
    { id: 'revoke', group: 'client', required_permissions: ['concessions.revoke'] },
    { id: 'provider_concessions', group: 'provider', required_permissions: ['concessions.grant'] },
  ],
  expected_roles: ['plant_manager', 'provider_installer'],
  backend_unit: 'U-02',
  home_for_roles: ['provider_installer'],
  home_group: 'provider',
  device_classes: ['desktop'],
};
