/**
 * FS-APP-01 (PAT-APP-RES-01 y 03, nota U05-H-10): la API deja de responder durante 2 minutos.
 *
 * La aplicación completa, abierta en la solicitud de exportación de SCR-14 con un formulario a
 * medio escribir; la campana de la cabecera refresca cada 60 s. Con la plataforma sin responder,
 * el refresco agota sus reintentos (2, 4 y 8 s) y el **cuarto** fallo abre el circuito: el aviso
 * dice «sin conexión», enseña la hora del último dato y que las escrituras están deshabilitadas;
 * la lectura de SCR-14 sigue en memoria y el formulario conserva lo escrito; mientras el circuito
 * está abierto no sale ninguna petición; la sonda `GET /health/live` lo prueba cada 30 s y lo
 * cierra cuando la plataforma vuelve, y entonces la escritura sale una sola vez.
 */
import { screen, within } from '@testing-library/react';
import fc from 'fast-check';
import { describe, expect, it } from 'vitest';

import { text } from '@/texts';

import { scenario } from '../msw/scenario';
import { faults, restorePlatform } from '../msw/scenarios/fs_app';
import { server } from '../setup/msw';
import {
  exportDraft,
  exportForm,
  exportFormValues,
  expectedExportValues,
  fillExportRequest,
  integer,
  mountExports,
  pump,
  scenarioCase,
  submitExportRequest,
  traffic,
} from './harness';

const OUTAGE_MS = 120_000;

describe('FS-APP-01 · la API no responde durante 2 minutos', { timeout: 120_000 }, () => {
  it('abre al cuarto fallo, conserva lecturas y borrador, deshabilita escrituras, sondea y se recupera', async (context) => {
    const parameters = scenarioCase(
      context,
      'FS-APP-01',
      fc.record({ draft: exportDraft, outageAfterMs: integer(0, 59_000) }),
    );
    const fixture = await mountExports();
    const { connection } = fixture.services;
    const platform = () => connection.getSnapshot().circuits.platform;
    const log = traffic(() => platform().state);
    server.use(...scenario('fs_app_01_platform_timeout'));

    fillExportRequest(parameters.draft);
    const written = exportFormValues();
    expect(written).toEqual(expectedExportValues(parameters.draft));
    await pump(fixture.clock, parameters.outageAfterMs);
    // El último dato recibido antes de la caída: es la hora que el aviso debe enseñar.
    const lastData = connection.getSnapshot().connection.last_success_at;
    expect(lastData).not.toBeNull();
    faults.platformDown = true;
    const outageStart = fixture.clock.now();

    // Cada segundo: fallos contados antes de que el circuito se abra.
    const failuresSeen: number[] = [];
    while (platform().state !== 'open') {
      if (fixture.clock.now() - outageStart > OUTAGE_MS)
        throw new Error('El circuito no se abrió.');
      const failures = platform().failures.length;
      if (failuresSeen.at(-1) !== failures) failuresSeen.push(failures);
      await pump(fixture.clock, 1_000);
    }
    expect(failuresSeen).toEqual([0, 1, 2, 3]);
    expect(connection.getSnapshot().connection.state).toBe('offline');

    // Aviso permanente con la hora del último dato recibido y las escrituras deshabilitadas.
    const notice = await screen.findByRole('region', { name: text('app_shell.connection.label') });
    expect(notice.dataset.state).toBe('offline');
    expect(within(notice).getByRole('status').dataset.textKey).toBe('app_shell.connection.offline');
    expect(notice.querySelector('[data-part="last_data"] time')?.getAttribute('datetime')).toBe(
      lastData,
    );
    expect(
      notice.querySelector('[data-text-key="app_shell.connection.writes_disabled"]'),
    ).not.toBeNull();

    // Lectura en memoria y borrador intactos; una escritura no sale con el circuito abierto.
    expect(within(exportForm()).getByRole('option', { name: 'Planta sintética' })).toBeDefined();
    expect(exportFormValues()).toEqual(written);
    submitExportRequest();
    await pump(fixture.clock, 1_000);
    expect(log.count('POST', '/accreditation/exports')).toBe(0);
    expect(exportFormValues()).toEqual(written);

    // La plataforma vuelve a los 2 minutos; la sonda la detecta en ≤ 30 s más su tope de 10 s.
    await pump(fixture.clock, Math.max(0, outageStart + OUTAGE_MS - fixture.clock.now()));
    // Según cuándo se abrió, la sonda ya falló alguna vez (y lo reabrió) o aún no le tocaba.
    const probesDuringOutage = log.count('GET', '/health/live');
    expect(['open', 'half_open']).toContain(platform().state);
    restorePlatform();
    while (connection.getSnapshot().connection.state !== 'online') {
      if (fixture.clock.now() - outageStart > OUTAGE_MS + 45_000) {
        throw new Error('La sonda no cerró el circuito tras la vuelta de la plataforma.');
      }
      await pump(fixture.clock, 1_000);
    }
    expect(log.count('GET', '/health/live')).toBe(probesDuringOutage + 1);
    expect(screen.queryByRole('region', { name: text('app_shell.connection.label') })).toBeNull();

    // Con el circuito abierto no salió nada: ni refrescos ni lecturas, solo la sonda en half_open.
    expect(log.entries.filter((entry) => entry.note === 'open')).toEqual([]);
    expect(
      log.entries.filter((entry) => entry.path === '/health/live').map((entry) => entry.note),
    ).toEqual(Array.from({ length: probesDuringOutage + 1 }, () => 'half_open'));

    // Recuperada: la persona repite la escritura y sale una sola vez, con lo que escribió.
    expect(exportFormValues()).toEqual(written);
    submitExportRequest();
    await pump(fixture.clock, 1_000);
    expect(log.count('POST', '/accreditation/exports')).toBe(1);
  });
});
