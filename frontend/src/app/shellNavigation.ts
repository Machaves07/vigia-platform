/** IF-06: sin concesión el proveedor navega solo por su panel, no por las pantallas del cliente. */
import type { ScreenDescriptor } from './descriptor';
import { buildNavigationMap, type NavigationPrincipal } from './navigation';

export function shellNavigation(
  principal: NavigationPrincipal & {
    readonly organization: { readonly kind: string };
    readonly concession_id: string | null;
  },
  screens: readonly ScreenDescriptor[],
) {
  const unscopedProvider =
    principal.organization.kind === 'provider' && principal.concession_id === null;
  return buildNavigationMap(
    unscopedProvider
      ? {
          ...principal,
          effective_permissions: principal.effective_permissions.filter(
            (key) => key === 'concessions.grant',
          ),
        }
      : principal.concession_id === null
        ? principal
        : {
            ...principal,
            effective_permissions: principal.effective_permissions.filter(
              (key) => key !== 'notifications.read',
            ),
          },
    screens,
  );
}
