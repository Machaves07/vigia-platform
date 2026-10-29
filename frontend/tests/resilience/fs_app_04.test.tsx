/**
 * FS-APP-04 (PAT-APP-RES-03, BR-APP-48, 58 y 74): el almacén de evidencias no está disponible.
 *
 * - En el detalle del hallazgo de SCR-08, el clip responde `storage_unavailable`: el reproductor
 *   dice «evidencia no disponible ahora» con «reintentar»; el resto del detalle sigue a la vista;
 *   los fallos cuentan en el circuito `evidence_store`, nunca en el de la plataforma. Con el
 *   almacén de vuelta (y su circuito ya en `half_open`), «reintentar» reproduce.
 * - En la evidencia del cierre de SCR-09, la concesión de subida responde `storage_unavailable`:
 *   la subida queda degradada, el archivo elegido sigue en memoria y la evidencia sin adjuntos
 *   se registra igual.
 */
import { fireEvent, screen, waitFor, within } from '@testing-library/react';
import fc from 'fast-check';
import { http, HttpResponse } from 'msw';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { text } from '@/texts';

import { fixture, STANDARD_TEXT, WAIT } from '../components/scr08/detail_support';
import {
  ACTION,
  DESCRIPTION,
  INSTANT,
  load,
  reasonName,
  renderClosure,
} from '../components/scr09/closure_support';
import { exampleFor } from '../msw/handlers';
import { changeDateTime } from '../support/dateFields';
import { scenario } from '../msw/scenario';
import { faults } from '../msw/scenarios/fs_app';
import { server } from '../setup/msw';
import { integer, pump, scenarioCase, scenarioClock, settle, traffic } from './harness';

const READ_URL = /^\/evidence\/[^/]+\/read-url$/;
const OPEN_MS = 30_000;

beforeEach(() => {
  vi.spyOn(HTMLMediaElement.prototype, 'play').mockImplementation(function (
    this: HTMLMediaElement,
  ) {
    this.dispatchEvent(new Event('play'));
    return Promise.resolve();
  });
  vi.spyOn(HTMLMediaElement.prototype, 'pause').mockReturnValue(undefined);
});

describe('FS-APP-04 · almacén de evidencias caído', { timeout: 120_000 }, () => {
  it('reproductor en degradación declarada; el detalle sigue y la plataforma no lo cuenta', async (context) => {
    const parameters = scenarioCase(
      context,
      'FS-APP-04',
      fc.record({
        part: fc.constant('player'),
        retries: integer(1, 6),
        retryAfterSeconds: integer(1, 30),
      }),
    );
    const env = await fixture();
    scenarioClock(env.clock);
    server.use(...scenario('fs_app_04_storage_unavailable'));
    faults.storageDown = true;
    faults.retryAfterSeconds = parameters.retryAfterSeconds;
    const log = traffic();
    env.view();
    const summary = await screen.findByRole('region', { name: text('scr08.detail.summary') }, WAIT);
    const player = document.querySelector<HTMLElement>('[data-part="clip_gallery"] figure');
    const video = player?.querySelector('video');
    if (player == null || video == null) throw new Error('El detalle no tiene reproductor.');

    fireEvent.click(within(player).getByRole('button', { name: text('media.clip.play') }));
    const alert = await within(player).findByRole('alert', {}, WAIT);
    expect(alert.dataset.textKey).toBe('media.clip.unavailable');
    expect(video.hasAttribute('src')).toBe(false);

    // «reintentar» con el almacén aún caído: cada pulsación, como mucho una petición.
    for (let retry = 0; retry < parameters.retries; retry += 1) {
      fireEvent.click(within(player).getByRole('button', { name: text('media.clip.retry') }));
      await settle();
      expect(within(player).getByRole('alert').dataset.textKey).toBe('media.clip.unavailable');
    }
    // Ningún reintento automático: una petición por pulsación (la URL se pide con un POST).
    const attempts = log.count('POST', READ_URL);
    expect(attempts).toBe(1 + parameters.retries);
    const circuits = env.connection.getSnapshot().circuits;
    expect(circuits.platform).toMatchObject({ state: 'closed', failures: [] });
    expect(circuits.evidence_store.state).toBe(attempts >= 4 ? 'open' : 'closed');
    expect(env.connection.canWrite()).toBe(true);

    // El resto de la pantalla funciona: resumen y texto declarado del estándar.
    expect(summary.isConnected).toBe(true);
    expect(screen.getByText(STANDARD_TEXT)).toBeDefined();

    // Vuelve el almacén; pasado el tiempo de apertura, «reintentar» reproduce.
    faults.storageDown = false;
    await pump(env.clock, OPEN_MS);
    fireEvent.click(within(player).getByRole('button', { name: text('media.clip.retry') }));
    await waitFor(() => {
      expect(video.getAttribute('src')).toBe('https://evidencia.example/clip-sintetico.mp4');
    }, WAIT);
    expect(within(player).queryByRole('alert')).toBeNull();
    expect(log.count('POST', READ_URL)).toBe(attempts + 1);
    expect(env.connection.getSnapshot().circuits.evidence_store.state).toBe('closed');
  });

  it('subida de adjuntos degradada con el archivo conservado; la evidencia se registra igual', async (context) => {
    const parameters = scenarioCase(
      context,
      'FS-APP-04',
      fc.record({
        part: fc.constant('upload'),
        fileName: fc.stringMatching(/^[a-z]{3,10}$/).map((name) => `${name}.png`),
        retries: integer(1, 3),
      }),
    );
    const { connection } = await renderClosure();
    const user = await load();
    server.use(...scenario('fs_app_04_storage_unavailable'));
    faults.storageDown = true;
    const evidence: unknown[] = [];
    server.use(
      http.post(`http://localhost/loop/actions/${ACTION}/evidence`, async ({ request }) => {
        evidence.push(await request.json());
        return HttpResponse.json(
          {
            ...(exampleFor('add_evidence_loop_actions__action_id__evidence_post') as object),
            added_at: INSTANT,
          },
          { status: 201 },
        );
      }),
    );
    const log = traffic();

    await user.type(
      screen.getByRole('textbox', { name: reasonName('scr09.closure.description') }),
      DESCRIPTION,
    );
    changeDateTime(screen.getByLabelText(text('scr09.closure.performed_at')), '2026-10-24T10:00');
    await user.type(
      screen.getByLabelText(text('scr09.closure.performed_by_role')),
      'Mantenimiento',
    );
    const input = screen.getByLabelText<HTMLInputElement>(text('scr09.closure.attachments'), {
      selector: 'input',
    });
    await user.upload(input, new File(['abc'], parameters.fileName, { type: 'image/png' }));
    await user.click(screen.getByRole('checkbox', { name: text('media.upload.declaration') }));
    await user.click(screen.getByRole('button', { name: text('media.upload.submit') }));
    await screen.findByText(text('scr09.closure.storage_degraded'), {}, WAIT);

    for (let retry = 0; retry < parameters.retries; retry += 1) {
      await user.click(screen.getByRole('button', { name: text('scr09.closure.retry_storage') }));
      await user.click(screen.getByRole('button', { name: text('media.upload.retry') }));
      await screen.findByText(text('scr09.closure.storage_degraded'), {}, WAIT);
      // El archivo elegido sigue en memoria: «reintentar» no obliga a elegirlo otra vez.
      expect(input.files?.[0]?.name).toBe(parameters.fileName);
    }
    expect(log.count('POST', /\/attachments\/upload-grant$/)).toBe(1 + parameters.retries);
    expect(log.entries.filter((entry) => entry.method === 'PUT')).toEqual([]);
    expect(connection.canRead('platform')).toBe(true);
    expect(connection.canWrite()).toBe(true);

    await user.click(screen.getByRole('button', { name: text('scr09.closure.submit_evidence') }));
    await screen.findByText(text('scr09.closure.saved'), {}, WAIT);
    expect(evidence).toEqual([
      {
        description: DESCRIPTION,
        performed_at: '2026-10-24T15:00:00.000Z',
        performed_by_role: 'Mantenimiento',
        verification_note: null,
        attachments: [],
      },
    ]);
  });
});
