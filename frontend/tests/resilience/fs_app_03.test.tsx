/**
 * FS-APP-03 (PAT-APP-RES-02, BR-APP-11): `rate_limited` con `retry_after_seconds`.
 *
 * La solicitud de exportación de SCR-14, en la aplicación completa, recibe `429 rate_limited`
 * varias veces seguidas (al menos cuatro, lo que abriría el circuito si contaran). Cada vez,
 * «Repetir» queda deshabilitado exactamente los segundos que dijo el servidor; el circuito de la
 * plataforma no cuenta ningún fallo, no aparece el aviso de conexión y la escritura sigue
 * saliendo en cada pulsación hasta que el servidor la acepta.
 */
import { screen, within } from '@testing-library/react';
import fc from 'fast-check';
import { describe, expect, it } from 'vitest';

import { text } from '@/texts';

import { scenario } from '../msw/scenario';
import { faults, FS_EXPORT } from '../msw/scenarios/fs_app';
import { server } from '../setup/msw';
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
        retryAfterSeconds: integer(1, 120),
      }),
    );
    const { rejections, retryAfterSeconds: retryAfter } = parameters;
    const fixture = await mountExports();
    const { connection } = fixture.services;
    const log = traffic();
    server.use(...scenario('fs_app_03_rate_limited'));
    faults.writeFailures = rejections;
    faults.retryAfterSeconds = retryAfter;

    fillExportRequest(parameters.draft);
    const written = exportFormValues();
    submitExportRequest();

    for (let attempt = 1; attempt <= rejections; attempt += 1) {
      const retry = await screen.findByRole('button', {
        name: text('components_forms.notice.retry'),
      });
      expect(log.count('POST', WRITE)).toBe(attempt);
      const notice = retry.closest('li') as HTMLElement;
      expect(notice.dataset.code).toBe('rate_limited');
      expect(within(notice).getByText(countdownText(retryAfter))).toBeDefined();
      // El límite no es un fallo del circuito: ni cuenta, ni abre, ni avisa de conexión.
      expect(connection.getSnapshot().circuits.platform).toMatchObject({
        state: 'closed',
        failures: [],
      });
      expect(connection.canWrite()).toBe(true);
      expect(screen.queryByRole('region', { name: text('app_shell.connection.label') })).toBeNull();
      expect(exportFormValues()).toEqual(written);

      await pump(fixture.clock, retryAfter * 1_000 - 1_000);
      expect(retryButton()).toHaveProperty('disabled', true);
      expect(within(notice).getByText(countdownText(1))).toBeDefined();
      await pump(fixture.clock, 1_000);
      expect(retryButton()).toHaveProperty('disabled', false);
      expect(log.count('POST', WRITE)).toBe(attempt);

      retryButton().click();
      await pump(fixture.clock, 1_000);
    }

    // Tras `rejections` límites seguidos la escritura salió también la última vez y se aceptó.
    const jobs = screen.getByRole('region', { name: text('scr14.jobs') });
    await within(jobs).findByRole('button', { name: FS_EXPORT });
    expect(log.count('POST', WRITE)).toBe(rejections + 1);
    expect(connection.getSnapshot().connection.state).toBe('online');
  });
});
