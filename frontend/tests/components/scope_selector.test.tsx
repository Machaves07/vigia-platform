/**
 * UI-03 `ScopeSelector` (BR-APP-05 y 06): «Todas las de mi alcance» por omisión, la planta
 * elegida persiste entre pantallas y sesiones por `UiPreferences`, y una planta guardada que ya
 * no está en el alcance vuelve a «todas» sin aviso ni error. `axe` sin violaciones.
 */
import { act, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { axe } from 'vitest-axe';

import { ScopeSelector } from '@/components/ScopeSelector/ScopeSelector';
import {
  createPreferencesStore,
  createScopeStore,
  PREFERENCES_KEY,
  plantsFromHierarchy,
  type AvailablePlant,
} from '@/state/scope';
import { text } from '@/texts';

const NORTE = '7c9e6679-7425-40de-944b-e07fc1f90ae7';
const SUR = '16fd2706-8baf-433b-82eb-8c7fada847da';
const RETIRADA = '9b2d1c3e-5f4a-4b6c-8d7e-0f1a2b3c4d5e';

const PLANT_NORTE: AvailablePlant = {
  plant_id: NORTE,
  code: 'N',
  name: 'Planta Norte',
  timezone: 'America/Bogota',
  country: 'CO',
};
const PLANTS: AvailablePlant[] = [
  PLANT_NORTE,
  { plant_id: SUR, code: 'S', name: 'Planta Sur', timezone: 'America/Bogota', country: 'CO' },
];

function select(): HTMLSelectElement {
  return screen.getByRole('combobox', { name: text('components_scope.selector.label') });
}

function stored(): unknown {
  const raw = localStorage.getItem(PREFERENCES_KEY);
  return raw === null ? null : JSON.parse(raw);
}

/** Una sesión: preferencias leídas del almacenamiento y plantas de `GET /hierarchy`. */
function session(plants: readonly AvailablePlant[] | null = PLANTS) {
  const store = createScopeStore(createPreferencesStore(localStorage));
  if (plants !== null) store.setAvailablePlants(plants);
  return store;
}

beforeEach(() => {
  localStorage.clear();
});

afterEach(() => {
  localStorage.clear();
});

describe('UI-03 ScopeSelector', () => {
  it('ofrece «Todas las de mi alcance» por omisión y las plantas del alcance, sin la organización', () => {
    render(<ScopeSelector store={session()} />);
    expect(select().value).toBe('all');
    expect(screen.getAllByRole('option').map((option) => option.textContent)).toEqual([
      text('components_scope.selector.all'),
      'Planta Norte',
      'Planta Sur',
    ]);
  });

  it('hasta que llega GET /hierarchy, muestra «todas» y no se puede cambiar', () => {
    render(<ScopeSelector store={session(null)} />);
    expect(select().value).toBe('all');
    expect(select().disabled).toBe(true);
  });

  it('elegir una planta la guarda y persiste en la sesión siguiente (BR-APP-05)', () => {
    const store = session();
    const { unmount } = render(<ScopeSelector store={store} />);
    fireEvent.change(select(), { target: { value: SUR } });
    expect(select().value).toBe(SUR);
    expect(store.getSnapshot().plantId).toBe(SUR);
    expect(store.scopeParams('scr08')).toEqual({ plant_id: SUR });
    expect(stored()).toEqual({ selected_plant_id: SUR });
    unmount();

    render(<ScopeSelector store={session()} />);
    expect(select().value).toBe(SUR);
  });

  it('con la planta guardada fuera del alcance muestra «todas» sin aviso ni error (BR-APP-06)', () => {
    localStorage.setItem(PREFERENCES_KEY, JSON.stringify({ selected_plant_id: RETIRADA }));
    const errors = vi.spyOn(console, 'error');
    const warnings = vi.spyOn(console, 'warn');
    const store = session(null);
    const { container } = render(<ScopeSelector store={store} />);
    act(() => {
      store.setAvailablePlants(PLANTS);
    });

    expect(select().value).toBe('all');
    expect(store.getSnapshot().plantId).toBe('all');
    expect(store.scopeParams('scr08')).toEqual({});
    expect(screen.queryByRole('alert')).toBeNull();
    expect(screen.queryByRole('status')).toBeNull();
    expect(container.textContent).toBe(
      [
        text('components_scope.selector.label'),
        text('components_scope.selector.all'),
        'Planta Norte',
        'Planta Sur',
      ].join(''),
    );
    expect(errors).not.toHaveBeenCalled();
    expect(warnings).not.toHaveBeenCalled();
    // La planta retirada se olvida: la sesión siguiente tampoco la usa.
    expect(stored()).toEqual({ selected_plant_id: 'all' });
  });

  it('una planta que no está en el alcance no se puede elegir', () => {
    const store = session();
    store.selectPlant(RETIRADA);
    expect(store.getSnapshot().plantId).toBe('all');
    expect(stored()).toBeNull();
  });

  it('la zona es por pantalla y se olvida al cambiar de planta (BR-APP-05)', () => {
    const store = session();
    store.selectPlant(NORTE);
    store.setZone('scr08', 'zona-1');
    expect(store.scopeParams('scr08')).toEqual({ plant_id: NORTE, zone_id: 'zona-1' });
    expect(store.scopeParams('scr10')).toEqual({ plant_id: NORTE });
    store.selectPlant(SUR);
    expect(store.scopeParams('scr08')).toEqual({ plant_id: SUR });
    expect(stored()).toEqual({ selected_plant_id: SUR });
  });

  it('plantsFromHierarchy toma solo los campos de available_plants', () => {
    const view = {
      plants: [{ ...PLANT_NORTE, data_region: 'sa-east-1', status: 'active', zones: [] }],
    };
    expect(plantsFromHierarchy(view)).toEqual([PLANT_NORTE]);
  });

  it('axe: sin violaciones', async () => {
    const { container } = render(<ScopeSelector store={session()} />);
    expect(await axe(container)).toHaveNoViolations();
  });
});
