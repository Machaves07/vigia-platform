/**
 * SCR-07 · Inventario de flota y panel de salud (U-03). Grupos: `inventory` (inventario, detalle
 * de nodo y umbrales de cola) y `alarms` (VIG-311); `enrollment`, `lifecycle` y `target_version`
 * (alta y ciclo de vida del nodo). `inventory` es el inicio del administrador.
 */
import type { ScreenDescriptor } from '@/app/descriptor';

export const descriptor: ScreenDescriptor = {
  screen_id: 'SCR-07',
  title_text: 'navigation.scr07',
  route: '/fleet',
  required_permissions: ['fleet.read', 'fleet.manage', 'commissioning.run'],
  route_groups: [
    {
      id: 'inventory',
      paths: ['/fleet', '/fleet/nodes/:node_id', '/plants/:plant_id/fleet-thresholds'],
      access: 'permission',
      required_permissions: ['fleet.read', 'fleet.manage'],
      linked_screens: ['SCR-05'],
      menu: { title_text: 'navigation.menu.fleet' },
      load: () => import('./inventory'),
    },
    {
      id: 'alarms',
      paths: ['/fleet/alarms'],
      access: 'permission',
      required_permissions: ['fleet.read'],
      load: () => import('./alarms'),
    },
    {
      id: 'enrollment',
      paths: ['/fleet/enrollment', '/fleet/nodes/:node_id/enrollment'],
      access: 'permission',
      required_permissions: ['commissioning.run'],
      load: () => import('./enrollment'),
    },
    {
      id: 'lifecycle',
      paths: ['/fleet/nodes/:node_id/lifecycle'],
      access: 'permission',
      required_permissions: ['fleet.manage', 'commissioning.run'],
      load: () => import('./lifecycle'),
    },
    {
      id: 'target_version',
      paths: ['/fleet/target-versions'],
      access: 'permission',
      required_permissions: ['fleet.manage'],
      load: () => import('./target_version'),
    },
  ],
  sections: [
    { id: 'inventory', group: 'inventory', required_permissions: ['fleet.read'] },
    { id: 'queue_thresholds', group: 'inventory', required_permissions: ['fleet.manage'] },
    { id: 'alarms', group: 'alarms', required_permissions: ['fleet.read'] },
    { id: 'declare_node', group: 'enrollment', required_permissions: ['commissioning.run'] },
    { id: 'enrollment_code', group: 'enrollment', required_permissions: ['commissioning.run'] },
    { id: 'zone_assignment', group: 'lifecycle', required_permissions: ['commissioning.run'] },
    { id: 'revocation', group: 'lifecycle', required_permissions: ['fleet.manage'] },
    { id: 'target_version', group: 'target_version', required_permissions: ['fleet.manage'] },
  ],
  expected_roles: ['provider_installer', 'administrator'],
  backend_unit: 'U-03',
  home_for_roles: ['administrator'],
  home_group: 'inventory',
  device_classes: ['desktop'],
};
