/**
 * SCR-09 · Clasificación y lazo de cierre (U-04). Grupos: `classify` y `loop` (VIG-313);
 * `loop/closure`, `loop/unclosable` y `loop/reopening` (VIG-328). Rutas con `finding_state_id`
 * (nota de `frontend-components.md` §6); claves de §6: `loop.manage` no existe.
 */
import type { ScreenDescriptor } from '@/app/descriptor';

export const descriptor: ScreenDescriptor = {
  screen_id: 'SCR-09',
  title_text: 'navigation.scr09',
  route: '/findings/:finding_state_id/loop',
  required_permissions: [
    'findings.read',
    'findings.classify',
    'actions.manage',
    'closure_attachments.read',
    'findings.close_and_sign',
  ],
  route_groups: [
    {
      id: 'classify',
      paths: ['/findings/:finding_state_id/classify'],
      access: 'permission',
      required_permissions: ['findings.classify'],
      load: () => import('./classify'),
    },
    {
      id: 'loop',
      paths: ['/findings/:finding_state_id/loop'],
      access: 'permission',
      required_permissions: ['findings.read', 'actions.manage'],
      load: () => import('./loop'),
    },
    {
      id: 'loop/closure',
      paths: ['/findings/:finding_state_id/loop/closure'],
      access: 'permission',
      required_permissions: [
        'actions.manage',
        'closure_attachments.read',
        'findings.close_and_sign',
      ],
      load: () => import('./loop/closure'),
    },
    {
      id: 'loop/unclosable',
      paths: ['/findings/:finding_state_id/loop/unclosable'],
      access: 'permission',
      required_permissions: ['findings.close_and_sign'],
      load: () => import('./loop/unclosable'),
    },
    {
      id: 'loop/reopening',
      paths: ['/findings/:finding_state_id/loop/reopening'],
      access: 'permission',
      required_permissions: ['actions.manage'],
      load: () => import('./loop/reopening'),
    },
  ],
  sections: [
    { id: 'classification', group: 'classify', required_permissions: ['findings.classify'] },
    { id: 'history', group: 'loop', required_permissions: ['findings.read'] },
    { id: 'action', group: 'loop', required_permissions: ['actions.manage'] },
    { id: 'closure_evidence', group: 'loop/closure', required_permissions: ['actions.manage'] },
    {
      id: 'closure_attachments',
      group: 'loop/closure',
      required_permissions: ['closure_attachments.read'],
    },
    {
      id: 'close_and_sign',
      group: 'loop/closure',
      required_permissions: ['findings.close_and_sign'],
    },
    {
      id: 'unclosable',
      group: 'loop/unclosable',
      required_permissions: ['findings.close_and_sign'],
    },
    { id: 'reopening', group: 'loop/reopening', required_permissions: ['actions.manage'] },
  ],
  expected_roles: ['coordinator_sst'],
  backend_unit: 'U-04',
  home_for_roles: [],
  home_group: null,
  device_classes: ['desktop'],
  // La evidencia de la clasificación reutiliza los textos del detalle de SCR-08.
  texts_from: ['SCR-08'],
};
