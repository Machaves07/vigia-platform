/**
 * SCR-01 · Inicio de sesión, cuenta y administración (U-02). Grupos: `login` e `invitation`
 * (públicos, VIG-321), `account` y `users` (VIG-305), `hierarchy` y `organization` (VIG-306).
 * Cada grupo con sesión tiene su entrada en el menú con el nombre de su página (VIG-363).
 */
import type { ScreenDescriptor } from '@/app/descriptor';

export const descriptor: ScreenDescriptor = {
  screen_id: 'SCR-01',
  title_text: 'navigation.scr01',
  route: '/login',
  required_permissions: [
    'users.manage',
    'roles.manage',
    'hierarchy.read',
    'hierarchy.manage',
    'organization.settings',
  ],
  route_groups: [
    {
      id: 'login',
      paths: ['/login', '/login/second-factor', '/login/enroll'],
      access: 'public',
      required_permissions: [],
      load: () => import('./login'),
    },
    {
      id: 'invitation',
      paths: ['/invitations/:token'],
      access: 'public',
      required_permissions: [],
      load: () => import('./invitation'),
    },
    {
      id: 'account',
      paths: ['/account'],
      access: 'authenticated',
      required_permissions: [],
      menu: { title_text: 'scr01_admin.account.title' },
      load: () => import('./account'),
    },
    {
      id: 'users',
      paths: ['/admin/users'],
      access: 'permission',
      required_permissions: ['users.manage', 'roles.manage'],
      menu: { title_text: 'scr01_admin.users.title' },
      load: () => import('./users'),
    },
    {
      id: 'hierarchy',
      paths: ['/admin/hierarchy'],
      access: 'permission',
      required_permissions: ['hierarchy.read', 'hierarchy.manage'],
      menu: { title_text: 'scr01_hierarchy.title' },
      load: () => import('./hierarchy'),
    },
    {
      id: 'organization',
      paths: ['/admin/organization'],
      access: 'permission',
      required_permissions: ['organization.settings'],
      menu: { title_text: 'scr01_hierarchy.organization' },
      load: () => import('./organization'),
    },
  ],
  sections: [
    { id: 'users', group: 'users', required_permissions: ['users.manage'] },
    { id: 'roles', group: 'users', required_permissions: ['roles.manage'] },
    { id: 'hierarchy', group: 'hierarchy', required_permissions: ['hierarchy.read'] },
    { id: 'hierarchy_manage', group: 'hierarchy', required_permissions: ['hierarchy.manage'] },
    { id: 'loop_configuration', group: 'hierarchy', required_permissions: ['hierarchy.read'] },
    {
      id: 'organization_settings',
      group: 'organization',
      required_permissions: ['organization.settings'],
    },
  ],
  expected_roles: [
    'coordinator_sst',
    'administrator',
    'provider_installer',
    'plant_manager',
    'line_manager',
    'copasst',
  ],
  backend_unit: 'U-02',
  home_for_roles: [],
  home_group: null,
  device_classes: ['desktop'],
};
