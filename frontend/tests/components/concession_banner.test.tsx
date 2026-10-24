/**
 * UI-04 `ConcessionBanner` (BR-APP-67 a 69): sin concesión no pinta nada; con concesión, el
 * aviso permanente «Operando en <cliente> bajo concesión hasta <fecha>; cada consulta queda
 * registrada» con el alcance, y el único botón para cambiar de concesión, que vacía la caché y
 * descarta los borradores. `axe` sin violaciones.
 */
import { act, fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { axe } from 'vitest-axe';

import {
  ConcessionBanner,
  concessionEndedKey,
} from '@/components/ConcessionBanner/ConcessionBanner';
import { createConcessionStore, type ConcessionEnd } from '@/state/concession';
import { text } from '@/texts';

import { FakeClock } from '../support/fakeClock';

const NOW = Date.parse('2026-10-24T12:00:00.000Z');
const EXPIRES = '2026-10-24T18:30:00.000Z';
const CONCESSION = {
  concession_id: 'c-1',
  client_organization_id: 'org-cliente',
  scope_level: 'plant' as const,
  scope_id: 'planta-1',
  expires_at: EXPIRES,
  status: 'active',
};
const HIERARCHY = {
  organization: { name: 'Cliente Andino' },
  plants: [{ plant_id: 'planta-1', name: 'Planta Norte', zones: [] }],
};

function fixture(kind = 'provider') {
  const clock = new FakeClock(NOW);
  let cleared = 0;
  let discarded = 0;
  const ended: ConcessionEnd[] = [];
  const store = createConcessionStore({
    clock,
    organizationKind: () => kind,
    cache: { clearAll: () => (cleared += 1) },
    onEnded: (reason) => ended.push(reason),
  });
  store.onDiscard(() => (discarded += 1));
  return {
    clock,
    store,
    ended,
    counts: () => ({ cleared, discarded }),
  };
}

function banner(): HTMLElement {
  return screen.getByRole('region', { name: text('components_concession.banner.label') });
}

function statement(): string | null | undefined {
  return banner().querySelector('[data-part="statement"]')?.textContent;
}

describe('UI-04 ConcessionBanner', () => {
  it('sin concesión activa no pinta nada', () => {
    const { container } = render(<ConcessionBanner store={fixture().store} />);
    expect(container.childElementCount).toBe(0);
  });

  it('un usuario de una organización cliente nunca activa concesión ni ve el aviso (BR-APP-69)', () => {
    const { store } = fixture('client');
    expect(store.activate(CONCESSION)).toBe(false);
    expect(store.header()).toBeNull();
    const { container } = render(<ConcessionBanner store={store} />);
    expect(container.childElementCount).toBe(0);
  });

  it('con concesión, el texto del aviso con el cliente, el vencimiento en la zona dada y el registro', () => {
    const { store } = fixture();
    expect(store.activate(CONCESSION, 'Soporte del montaje de la planta norte')).toBe(true);
    render(<ConcessionBanner store={store} timeZone="America/Bogota" />);
    expect(statement()).toBe(
      `${text('components_concession.banner.operating_in')} ${text('components_concession.banner.client_pending')} ${text('components_concession.banner.under_until')} 24/10/2026, 13:30:00 (America/Bogota, UTC-05:00); ${text('components_concession.banner.audited')}`,
    );
    expect(banner().querySelector('time')?.getAttribute('dateTime')).toBe(EXPIRES);
    expect(banner().querySelector('[data-part="reason"]')?.textContent).toBe(
      'Soporte del montaje de la planta norte',
    );

    act(() => {
      store.describeClient(HIERARCHY);
    });
    expect(banner().querySelector('[data-part="client"]')?.textContent).toBe('Cliente Andino');
    expect(banner().querySelector('[data-part="scope"]')?.textContent).toBe(
      `${text('components_concession.scope.plant')}: Planta Norte`,
    );
  });

  it('el aviso no tiene forma de cerrarse: su único control es cambiar de concesión', () => {
    const { store } = fixture();
    store.activate(CONCESSION);
    render(<ConcessionBanner store={store} />);
    expect(screen.getAllByRole('button').map((button) => button.textContent)).toEqual([
      text('components_concession.banner.change'),
    ]);
  });

  it('cambiar de concesión desde el aviso vacía la caché, descarta el borrador y quita la cabecera (BR-APP-68)', () => {
    const { store, ended, counts } = fixture();
    store.activate(CONCESSION);
    const before = counts();
    const { container } = render(<ConcessionBanner store={store} />);
    expect(store.header()).toBe('c-1');
    fireEvent.click(
      screen.getByRole('button', { name: text('components_concession.banner.change') }),
    );
    expect(container.childElementCount).toBe(0);
    expect(store.header()).toBeNull();
    expect(counts()).toEqual({ cleared: before.cleared + 1, discarded: before.discarded + 1 });
    expect(ended).toEqual(['changed']);
    expect(text(concessionEndedKey('changed'))).toBe(
      'Dejó de operar bajo la concesión. Elija otra en su panel de concesiones.',
    );
  });

  it('al vencer con el Clock desaparece y avisa para volver al panel (BR-APP-68)', () => {
    const { clock, store, ended } = fixture();
    store.activate(CONCESSION);
    const { container } = render(<ConcessionBanner store={store} />);
    act(() => {
      clock.advance(Date.parse(EXPIRES) - NOW - 1);
    });
    expect(container.childElementCount).toBe(1);
    expect(store.header()).toBe('c-1');
    act(() => {
      clock.advance(1);
    });
    expect(container.childElementCount).toBe(0);
    expect(store.header()).toBeNull();
    expect(ended).toEqual(['expired']);
    expect(text(concessionEndedKey('expired'))).toBe(
      'La concesión venció. Volvió a su panel de concesiones.',
    );
  });

  it('con otra concesión activa no se puede activar una nueva sin pasar por el aviso', () => {
    const { store } = fixture();
    expect(store.activate(CONCESSION)).toBe(true);
    expect(store.activate({ ...CONCESSION, concession_id: 'c-2' })).toBe(false);
    expect(store.header()).toBe('c-1');
    store.change();
    expect(store.activate({ ...CONCESSION, concession_id: 'c-2' })).toBe(true);
    expect(store.header()).toBe('c-2');
  });

  it('el alcance de organización y de zona se nombran desde la jerarquía', () => {
    const organization = fixture();
    organization.store.activate({ ...CONCESSION, scope_level: 'organization', scope_id: 'org' });
    organization.store.describeClient(HIERARCHY);
    expect(organization.store.getSnapshot().active?.scope_name).toBe('Cliente Andino');
    const zone = fixture();
    zone.store.activate({ ...CONCESSION, scope_level: 'zone', scope_id: 'zona-7' });
    zone.store.describeClient({
      ...HIERARCHY,
      plants: [
        {
          plant_id: 'planta-1',
          name: 'Planta Norte',
          zones: [{ zone_id: 'zona-7', name: 'Muelle' }],
        },
      ],
    });
    expect(zone.store.getSnapshot().active?.scope_name).toBe('Muelle');
  });

  it('axe: sin violaciones', async () => {
    const { store } = fixture();
    store.activate(CONCESSION, 'Soporte del montaje de la planta norte');
    store.describeClient(HIERARCHY);
    const { container } = render(<ConcessionBanner store={store} />);
    expect(await axe(container)).toHaveNoViolations();
  });
});
