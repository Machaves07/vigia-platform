/**
 * FS-APP-06 (PAT-APP-RES-05, BR-APP-16, NFR-APP-38): la sesión vence a mitad de un formulario.
 *
 * La aplicación completa en la solicitud de exportación de SCR-14 con el formulario escrito. La
 * sesión vence en el servidor: la siguiente petición (la escritura o el refresco de la campana,
 * lo que llegue antes) recibe `unauthenticated`. La pantalla queda bloqueada, sin cabecera ni
 * menú, con el inicio de sesión en la misma pestaña; al reautenticar, la misma persona vuelve a
 * la misma ruta con el borrador tal como estaba, y la escritura no se reenvía sola: la repite.
 */
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import fc from 'fast-check';
import { describe, expect, it } from 'vitest';

import { text } from '@/texts';

import { scenario } from '../msw/scenario';
import { faults, FS_EXPORT } from '../msw/scenarios/fs_app';
import { server } from '../setup/msw';
import { WAIT } from '../support/appShell';
import {
  exportDraft,
  exportFormValues,
  fillExportRequest,
  integer,
  mountExports,
  pump,
  scenarioCase,
  submitExportRequest,
  traffic,
} from './harness';

const WRITE = '/accreditation/exports';

describe('FS-APP-06 · sesión vencida a mitad de un formulario largo', { timeout: 120_000 }, () => {
  it('bloquea con el borrador conservado y, tras reautenticar en la pestaña, vuelve a la ruta con él', async (context) => {
    const parameters = scenarioCase(
      context,
      'FS-APP-06',
      fc.record({
        draft: exportDraft,
        writingMs: integer(0, 25 * 60_000),
        idleAfterExpiryMs: integer(0, 90_000),
      }),
    );
    const fixture = await mountExports();
    const log = traffic();
    server.use(...scenario('fs_app_06_session_expired'));

    fillExportRequest(parameters.draft);
    const written = exportFormValues();
    // Dentro de la ventana de 30 min: el aviso de los 28 min puede salir, pero la sesión sigue.
    await pump(fixture.clock, parameters.writingMs, 60_000);
    expect(fixture.services.session.getSnapshot().status).toBe('active');

    faults.sessionExpired = true;
    await pump(fixture.clock, parameters.idleAfterExpiryMs);
    const lockedByWrite = fixture.services.session.getSnapshot().status === 'active';
    if (lockedByWrite) submitExportRequest();
    const lock = await screen.findByRole('alert', {}, WAIT);
    expect(lock.dataset.textKey).toBe('errors.reauth');
    expect(fixture.services.session.getSnapshot().status).toBe('locked_for_reauth');
    expect(fixture.route()).toBe('/exports');
    // Bloqueada: ni el formulario, ni la cabecera, ni el menú.
    expect(screen.queryByRole('form', { name: text('scr14.request') })).toBeNull();
    expect(screen.queryByRole('banner')).toBeNull();
    expect(screen.queryByRole('navigation')).toBeNull();
    const writesBeforeLock = log.count('POST', WRITE);
    expect(writesBeforeLock).toBe(lockedByWrite ? 1 : 0);

    const user = userEvent.setup();
    await user.type(screen.getByLabelText(text('scr01.login.email')), 'persona@ejemplo.test');
    await user.type(screen.getByLabelText(text('scr01.login.password')), 'clave-sintetica-larga');
    await user.click(screen.getByRole('button', { name: text('scr01.login.submit') }));

    // Misma ruta, mismo borrador; la escritura que recibió `unauthenticated` no se reenvió.
    await screen.findByRole('form', { name: text('scr14.request') }, WAIT);
    await waitFor(() => {
      expect(exportFormValues()).toEqual(written);
    }, WAIT);
    expect(fixture.route()).toBe('/exports');
    expect(fixture.services.session.getSnapshot().status).toBe('active');
    expect(log.count('POST', '/auth/login')).toBe(1);
    await pump(fixture.clock, 60_000);
    expect(log.count('POST', WRITE)).toBe(writesBeforeLock);

    submitExportRequest();
    await screen.findByRole('button', { name: FS_EXPORT }, WAIT);
    expect(log.count('POST', WRITE)).toBe(writesBeforeLock + 1);
  });
});
