/**
 * SCR-13 · Métricas, exposición y resumen del gerente (U-04), VIG-316. Grupos: `metrics` y
 * `weekly` (resumen semanal, inicio del gerente de planta). Claves de §6: `executive_summary.read`,
 * `metrics.read` y `exposure.read`. Cada grupo tiene su entrada en el menú con el nombre de su
 * página (VIG-363): el resumen del gerente deja de ser solo una dirección.
 */
import type { ScreenDescriptor } from '@/app/descriptor';

export const descriptor: ScreenDescriptor = {
  screen_id: 'SCR-13',
  title_text: 'navigation.scr13',
  route: '/metrics',
  required_permissions: ['executive_summary.read', 'metrics.read', 'exposure.read'],
  route_groups: [
    {
      id: 'metrics',
      paths: ['/metrics'],
      access: 'permission',
      required_permissions: ['metrics.read', 'exposure.read'],
      menu: { title_text: 'navigation.menu.metrics' },
      load: () => import('./metrics'),
    },
    {
      id: 'weekly',
      paths: ['/metrics/weekly'],
      access: 'permission',
      required_permissions: ['executive_summary.read'],
      linked_screens: ['SCR-03', 'SCR-14'],
      menu: { title_text: 'navigation.menu.weekly' },
      load: () => import('./weekly'),
    },
  ],
  sections: [
    { id: 'metrics', group: 'metrics', required_permissions: ['metrics.read'] },
    { id: 'exposure', group: 'metrics', required_permissions: ['exposure.read'] },
    { id: 'overdue', group: 'weekly', required_permissions: ['executive_summary.read'] },
    { id: 'weekly_metrics', group: 'weekly', required_permissions: ['metrics.read'] },
  ],
  expected_roles: ['plant_manager'],
  backend_unit: 'U-04',
  home_for_roles: ['plant_manager'],
  home_group: 'weekly',
  device_classes: ['desktop'],
};
