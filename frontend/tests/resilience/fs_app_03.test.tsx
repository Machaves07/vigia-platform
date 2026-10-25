/**
 * FS-APP-03 (PAT-APP-RES-02, BR-APP-11): `rate_limited` con `retry_after_seconds`.
 *
 * El límite por origen rechaza con `429 rate_limited` toda petición (la solicitud de exportación
 * de SCR-14 y las lecturas que lleguen) al menos cuatro veces seguidas dentro de la ventana del
 * circuito, lo que lo abriría si contaran. Cada vez, «Repetir» queda deshabilitado exactamente
 * los segundos que dijo el servidor; el circuito de la plataforma no cuenta ningún fallo, no
 * aparece el aviso de conexión y la escritura sigue saliendo en cada pulsación hasta que el
 * servidor la acepta.
 */
import { screen, within } from '@testing-library/react';
import fc from 'fast-check';
import { describe, expect, it } from 'vitest';

import { text } from '@/texts';

import { scenario } from '../msw/scenario';
import { faults, FS_EXPORT } from '../msw/scenarios/fs_app';
import { server } from '../setup/msw';
import { WAIT } from '../support/appShell';
import {
  countdownText,
  exportDraft,
  exportFormValues,
  fillExportRequest,
  integer,
  mountExports,
  pump,
  retryButton,
  scenarioCase,
  submitExportRequest,
  traffic,
} from './harness';

const WRITE = '/accreditation/exports';

describe('FS-APP-03 · rate_limited con retry_after_seconds', { timeout: 120_000 }, () => {
  it('cuenta regresiva exacta en cada rechazo y el circuito no se abre', async (context) => {
    const parameters = scenarioCase(
      context,
      'FS-APP-03',
      fc.record({
        draft: exportDraft,
        rejections: integer(4, 6),
        // Cuatro rechazos caben en la ventana de fallos del circuito (60 s): si contaran, abrirían.
        retryAfterSeconds: integer(1, 15),
      }),
    );
    const { rejections, retryAfterSeconds: retryAfter } = parameters;
    const fixture = await mountExports();
    const { connection } = fixture.services;
    const log = traffic();
    server.use(...scenario('fs_app_03_rate_limited'));
    faults.retryAfterSeconds = retryAfter;

    fillExportRequest(parameters.draft);
    const written = exportFormValues();
    // El límite por origen rechaza todo: la escritura y también las lecturas que lleguen.
    faults.rateLimited = true;
    submitExportRequest();

    for (let attempt = 1; attempt <= rejections; attempt += 1) {
      const retry = await screen.findByRole(
        'button',
        { name: text('components_forms.notice.retry') },
        WAIT,
      );
      expect(log.count('POST', WRITE)).toBe(attempt);
      const notice = retry.closest('li') as HTMLElement;
      expect(notice.dataset.code).toBe('rate_limited');
      expect(within(notice).getByText(countdownText(retryAfter))).toBeDefined();
      // El límite no es un fallo del circuito: ni cuenta, ni abre, ni avisa de conexión.
      const platform = connection.getSnapshot().circuits.platform;
      expect(platform.state).toBe('closed');
      expect(platform.failures).toEqual([]);
      expect(connection.canWrite()).toBe(true);
      expect(screen.queryByRole('region', { name: text('app_shell.connection.label') })).toBeNull();
      expect(exportFormValues()).toEqual(written);

      await pump(fixture.clock, retryAfter * 1_000 - 1_000);
      expect(retryButton()).toHaveProperty('disabled', true);
      expect(within(notice).getByText(countdownText(1))).toBeDefined();
      await pump(fixture.clock, 1_000);
      expect(retryButton()).toHaveProperty('disabled', false);
      expect(log.count('POST', WRITE)).toBe(attempt);

      // Antes de la última pulsación termina el límite.
      if (attempt === rejections) faults.rateLimited = false;
      retryButton().click();
      await pump(fixture.clock, 1_000);
    }

    // Tras `rejections` límites seguidos la escritura salió también la última vez y se aceptó.
    const jobs = screen.getByRole('region', { name: text('scr14.jobs') });
    await within(jobs).findByRole('button', { name: FS_EXPORT }, WAIT);
    expect(log.count('POST', WRITE)).toBe(rejections + 1);
    expect(connection.getSnapshot().connection.state).toBe('online');
  });
});
