/**
 * SCR-10 · Cola de revisión (U-04), VIG-329. Visible con `findings.read` o
 * `review_queue.resolve` (diferencia 1 del PR #197: `GET /loop/review-queue` exige
 * `findings.read`); la acción «resolver» solo con `review_queue.resolve`.
 */
import type { ScreenDescriptor } from '@/app/descriptor';

export const descriptor: ScreenDescriptor = {
  screen_id: 'SCR-10',
  title_text: 'navigation.scr10',
  route: '/review-queue',
  required_permissions: ['findings.read', 'review_queue.resolve'],
  route_groups: [
    {
      id: 'queue',
      paths: ['/review-queue'],
      access: 'permission',
      required_permissions: ['findings.read', 'review_queue.resolve'],
      load: () => import('./queue'),
    },
  ],
  sections: [
    {
      id: 'queue',
      group: 'queue',
      required_permissions: ['findings.read', 'review_queue.resolve'],
    },
    { id: 'resolve', group: 'queue', required_permissions: ['review_queue.resolve'] },
  ],
  expected_roles: ['coordinator_sst'],
  backend_unit: 'U-04',
  home_for_roles: [],
  home_group: null,
  device_classes: ['desktop'],
};
