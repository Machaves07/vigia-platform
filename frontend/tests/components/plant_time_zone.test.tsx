/**
 * Hora de la planta en las pantallas que no son de una planta (VIG-366, fallo observado nº 2):
 * `usePlantTimeZone` dentro del armazón real (`App`, `GET /hierarchy` sintético) con la marca de
 * tiempo del servidor de SCR-06. Con una planta, o con la planta elegida, su zona; con plantas de
 * zonas distintas y «todas», UTC, que la hora nombra; con la planta de la ruta, la suya. Datos sintéticos.
 */
import { act, screen, waitFor } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import type { ReactNode } from 'react';
import { describe, expect, it, vi } from 'vitest';
import { Link } from 'react-router';

import { usePlantTimeZone } from '@/app/AppShell';
import { ServerTimestamp } from '@/pages/scr06/common/ServerTimestamp';
import type { HierarchyView } from '@/ports/shared';
import { text } from '@/texts';

import { server } from '../setup/msw';
import {
  mountShell,
  ORIGIN,
  PLANT_ID,
  serveSession,
  shellHierarchy,
  shellMe,
  syntheticScreen,
  WAIT,
} from '../support/appShell';

vi.mock('@/pages/scr15/Bell', () => ({
  Bell: () => <Link to="/notifications">{text('navigation.bell')}</Link>,
}));

const TEST = { timeout: 60_000 };
const OTHER_PLANT = '0190a1b2-0000-7000-8000-0000000000a2';
const INSTANT = '2026-01-01T17:00:00.000Z';

function TimestampPage(): ReactNode {
  return (
    <main>
      <h1>Marca sintética</h1>
      <ServerTimestamp instant={INSTANT} />
      <p data-part="route_plant">{usePlantTimeZone(OTHER_PLANT)}</p>
    </main>
  );
}

const SCREENS = [
  syntheticScreen({
    id: 'SCR-06',
    title: 'Marca sintética',
    path: '/stamp',
    permission: 'finding.read',
    page: TimestampPage,
    home: true,
  }),
];

/** La jerarquía sintética con una segunda planta en `timezone`. */
function twoPlants(timezone: string): HierarchyView {
  const view = shellHierarchy();
  const [first] = view.plants;
  return {
    ...view,
    plants: [...view.plants, { ...first, plant_id: OTHER_PLANT, code: 'PL-DOS', timezone }],
  } as unknown as HierarchyView;
}

async function stamp(hierarchy: HierarchyView | null) {
  serveSession(shellMe(['finding.read']));
  if (hierarchy !== null) {
    server.use(http.get(`${ORIGIN}/hierarchy`, () => HttpResponse.json(hierarchy)));
  }
  const shell = mountShell('/stamp', SCREENS);
  await screen.findByRole('heading', { name: 'Marca sintética' }, WAIT);
  await waitFor(() => {
    expect(shell.services.scope.getSnapshot().availablePlants).not.toBeNull();
  }, WAIT);
  return shell;
}

function shown(): { readonly time: string; readonly zone: string } {
  const part = document.querySelector('[data-part="server_instant"]');
  return {
    time: part?.querySelector('time')?.textContent ?? '',
    zone: part?.querySelector('[data-part="zone"]')?.textContent ?? '',
  };
}

describe('hora de la planta fuera de una planta (VIG-366, usePlantTimeZone)', TEST, () => {
  it('con una sola planta, la hora va en su zona (America/Bogota, UTC-05:00)', async () => {
    await stamp(null);
    await waitFor(() => {
      expect(shown()).toEqual({
        time: '01/01/2026, 12:00:00',
        zone: '(America/Bogota, UTC-05:00)',
      });
    }, WAIT);
  });

  it('con «todas» y plantas de la misma zona, esa zona', async () => {
    await stamp(twoPlants('America/Bogota'));
    await waitFor(() => {
      expect(shown()).toEqual({
        time: '01/01/2026, 12:00:00',
        zone: '(America/Bogota, UTC-05:00)',
      });
    }, WAIT);
  });

  it('con «todas» y zonas distintas, UTC; al elegir una planta, la suya', async () => {
    const shell = await stamp(twoPlants('Europe/Madrid'));
    await waitFor(() => {
      expect(shown()).toEqual({ time: '01/01/2026, 17:00:00', zone: '(UTC, UTC+00:00)' });
    }, WAIT);
    // Una pantalla de una planta (admisiones) usa la de su ruta aunque el selector diga «todas».
    expect(document.querySelector('[data-part="route_plant"]')?.textContent).toBe('Europe/Madrid');
    act(() => {
      shell.services.scope.selectPlant(OTHER_PLANT);
    });
    await waitFor(() => {
      expect(shown()).toEqual({ time: '01/01/2026, 18:00:00', zone: '(Europe/Madrid, UTC+01:00)' });
    }, WAIT);
    act(() => {
      shell.services.scope.selectPlant(PLANT_ID);
    });
    await waitFor(() => {
      expect(shown()).toEqual({
        time: '01/01/2026, 12:00:00',
        zone: '(America/Bogota, UTC-05:00)',
      });
    }, WAIT);
  });
});
