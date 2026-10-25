import { describe, expect, it } from 'vitest';

import { navItems } from '@/app/NavMenu';
import { SCREENS } from '@/app/screens';
import { shellNavigation } from '@/app/shellNavigation';

describe('IF-06: navegación del proveedor sin concesión', () => {
  const principal = {
    effective_permissions: [
      'concessions.grant',
      'fleet.read',
      'hierarchy.read',
      'notifications.read',
    ],
    assignments: [{ role: 'provider_installer' }],
    organization: { kind: 'provider' },
    concession_id: null,
  };

  it('no ofrece pantallas del cliente sin concesión, aunque el rol tenga permisos base', () => {
    expect(navItems(shellNavigation(principal, SCREENS), SCREENS).map((item) => item.path)).toEqual(
      ['/provider/concessions'],
    );
  });

  it('con una concesión vuelve a componer con los permisos efectivos del servidor', () => {
    const navigation = shellNavigation(
      { ...principal, concession_id: 'concesion-sintetica' },
      SCREENS,
    );
    expect(navItems(navigation, SCREENS).map((item) => item.path)).toContain('/fleet');
  });

  it('no restringe la navegación de las organizaciones cliente', () => {
    const navigation = shellNavigation({ ...principal, organization: { kind: 'client' } }, SCREENS);
    expect(navItems(navigation, SCREENS).map((item) => item.path)).toContain('/fleet');
  });
});
