/**
 * O-13: la sesión navega con el enrutador que esté montado. Si la navegación se pide mientras no
 * hay ninguno registrado (el anterior se desmontó al llegar el `Principal` y el nuevo aún no), el
 * nuevo la completa al registrarse, con el mismo `replace`; una vez alcanzada, no se repite.
 */
import { afterEach, describe, expect, it } from 'vitest';

import { createAppServices, type AppServices } from '@/app/bootstrap';
import { createAppVersion } from '@/app/version';

import { FakeClock } from '../../support/fakeClock';

const services: AppServices[] = [];
afterEach(() => {
  for (const item of services.splice(0)) item.dispose();
});

function create(): AppServices {
  const clock = new FakeClock(Date.parse('2026-01-01T12:00:00.000Z'));
  const created = createAppServices({
    clock,
    storage: null,
    origin: 'http://localhost',
    version: createAppVersion({ clock }),
    activityTarget: null,
  });
  services.push(created);
  return created;
}

/** Un enrutador falso en `route` que anota cada navegación que recibe. */
function bridge(route: string) {
  const calls: unknown[][] = [];
  return {
    calls,
    bridge: {
      route: () => route,
      navigate: (to: string, options?: { readonly replace?: boolean }) => {
        calls.push([to, options]);
      },
    },
  };
}

describe('O-13: navegación de la sesión con el enrutador montado', () => {
  it('sin enrutador registrado, el siguiente completa la navegación con su replace', () => {
    const app = create();
    app.attachRouter(null);
    app.session.navigate('/findings', { replace: true });
    const next = bridge('/login/second-factor');
    app.attachRouter(next.bridge);
    expect(next.calls).toEqual([['/findings', { replace: true }]]);
    // Alcanzada la ruta, otro enrutador que se registre no la repite.
    app.routeChanged('/findings');
    const later = bridge('/findings');
    app.attachRouter(later.bridge);
    expect(later.calls).toEqual([]);
  });

  it('con un enrutador registrado, navega con él y le pasa el replace', () => {
    const app = create();
    const mounted = bridge('/login/second-factor');
    app.attachRouter(mounted.bridge);
    app.session.navigate('/findings', { replace: true });
    expect(mounted.calls).toEqual([['/findings', { replace: true }]]);
  });
});
