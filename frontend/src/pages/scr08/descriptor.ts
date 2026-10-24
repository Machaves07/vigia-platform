/**
 * SCR-08 · Bandeja y detalle de hallazgo con semáforo; etiquetas (U-04; U-02). Grupos: `home` e
 * `inbox` (VIG-312), `detail` y `labels` (VIG-327). El detalle lleva `finding_state_id` (nota de
 * `frontend-components.md` §6). `home` es el panel de inicio del coordinador y compone solo las
 * secciones cuyo permiso tiene; la cola es visible con `findings.read` (diferencia 1 del PR #197).
 */
import type { ScreenDescriptor } from '@/app/descriptor';

export const descriptor: ScreenDescriptor = {
  screen_id: 'SCR-08',
  title_key: 'navigation.scr08',
  route: '/findings',
  required_permissions: ['findings.read', 'evidence.read', 'labels.read', 'coverage.read'],
  route_groups: [
    {
      id: 'home',
      paths: ['/home'],
      access: 'permission',
      required_permissions: ['findings.read', 'coverage.read'],
      linked_screens: ['SCR-09', 'SCR-10', 'SCR-11'],
      load: () => import('./home'),
    },
    {
      id: 'inbox',
      paths: ['/findings'],
      access: 'permission',
      required_permissions: ['findings.read'],
      load: () => import('./inbox'),
    },
    {
      id: 'labels',
      paths: ['/findings/labels'],
      access: 'permission',
      required_permissions: ['labels.read'],
      load: () => import('./labels'),
    },
    {
      id: 'detail',
      paths: ['/findings/:finding_state_id'],
      access: 'permission',
      required_permissions: ['findings.read', 'evidence.read'],
      load: () => import('./detail'),
    },
  ],
  sections: [
    { id: 'observability', group: 'home', required_permissions: ['coverage.read'] },
    { id: 'new_findings', group: 'home', required_permissions: ['findings.read'] },
    { id: 'actions_due', group: 'home', required_permissions: ['findings.read'] },
    { id: 'review_queue', group: 'home', required_permissions: ['findings.read'] },
    { id: 'inbox', group: 'inbox', required_permissions: ['findings.read'] },
    { id: 'finding', group: 'detail', required_permissions: ['findings.read'] },
    { id: 'clips', group: 'detail', required_permissions: ['evidence.read'] },
    { id: 'coverage_at', group: 'detail', required_permissions: ['coverage.read'] },
    { id: 'labels', group: 'labels', required_permissions: ['labels.read'] },
  ],
  expected_roles: ['coordinator_sst'],
  backend_unit: 'U-04',
  home_for_roles: ['coordinator_sst'],
  home_group: 'home',
  device_classes: ['desktop'],
};
