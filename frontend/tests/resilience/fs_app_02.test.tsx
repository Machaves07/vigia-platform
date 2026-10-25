/**
 * FS-APP-02 (PAT-APP-RES-02, BR-APP-72): `temporarily_unavailable` sostenido en escrituras.
 *
 * La solicitud de exportación de SCR-14, en la aplicación completa, recibe `503
 * temporarily_unavailable` con `retry_after_seconds` en dos envíos seguidos. Ninguna escritura se
 * repite sola, tampoco cuando termina la cuenta regresiva; «Repetir» espera exactamente lo que
 * dijo el servidor y cada pulsación envía una sola petición; el formulario conserva lo escrito.
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

describe(
  'FS-APP-02 · temporarily_unavailable sostenido en escrituras',
  { timeout: 120_000 },
  () => {
    it('cero reintentos automáticos, «Repetir» con su cuenta regresiva y lo escrito se conserva', async (context) => {
      const parameters = scenarioCase(
        context,
        'FS-APP-02',
        fc.record({ draft: exportDraft, retryAfterSeconds: integer(2, 90) }),
      );
      const retryAfter = parameters.retryAfterSeconds;
      const fixture = await mountExports();
      const log = traffic();
      server.use(...scenario('fs_app_02_write_unavailable'));
      faults.writeFailures = 2;
      faults.retryAfterSeconds = retryAfter;

      fillExportRequest(parameters.draft);
      const written = exportFormValues();
      submitExportRequest();

      for (let attempt = 1; attempt <= 2; attempt += 1) {
        const retry = await screen.findByRole('button', {
          name: text('components_forms.notice.retry'),
        });
        expect(log.count('POST', WRITE)).toBe(attempt);
        const notice = retry.closest('li');
        expect(notice?.dataset.code).toBe('temporarily_unavailable');
        expect(within(notice as HTMLElement).getByText(countdownText(retryAfter))).toBeDefined();
        expect(retry).toHaveProperty('disabled', true);
        expect(exportFormValues()).toEqual(written);

        // Un segundo antes del final sigue deshabilitado; al final se habilita; nada sale solo.
        await pump(fixture.clock, retryAfter * 1_000 - 1_000);
        expect(retryButton()).toHaveProperty('disabled', true);
        await pump(fixture.clock, 1_000);
        expect(retryButton()).toHaveProperty('disabled', false);
        await pump(fixture.clock, 60_000);
        expect(log.count('POST', WRITE)).toBe(attempt);
        expect(exportFormValues()).toEqual(written);

        retryButton().click();
        await pump(fixture.clock, 1_000);
      }

      // El tercer envío, por acción de la persona, se acepta: tres peticiones en total, ni una más.
      const jobs = screen.getByRole('region', { name: text('scr14.jobs') });
      await within(jobs).findByRole('button', { name: FS_EXPORT });
      expect(
        screen.queryByRole('button', { name: text('components_forms.notice.retry') }),
      ).toBeNull();
      expect(log.count('POST', WRITE)).toBe(3);
      await pump(fixture.clock, 60_000);
      expect(log.count('POST', WRITE)).toBe(3);
    });
  },
);
