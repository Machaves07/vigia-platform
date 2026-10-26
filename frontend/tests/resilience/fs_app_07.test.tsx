/**
 * FS-APP-07 (BR-APP-13, UI-08): `privacy_notice_required` sobrevenido.
 *
 * Con la sesión en uso en SCR-14, el servidor publica una versión del aviso de tratamiento que
 * la persona aún no aceptó: la siguiente petición (la escritura o el refresco de la campana)
 * recibe `privacy_notice_required`. La puerta del aviso ocupa la pantalla con solo «Acepto» y
 * «Cerrar sesión», también pasado el tiempo; al aceptar se envía la versión mostrada una
 * sola vez y se vuelve a la ruta en la que estaba.
 */
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import fc from 'fast-check';
import { describe, expect, it } from 'vitest';

import { text } from '@/texts';

import { scenario } from '../msw/scenario';
import { faults } from '../msw/scenarios/fs_app';
import { server } from '../setup/msw';
import { shellMe, WAIT } from '../support/appShell';
import {
  EXPORT_PERMISSIONS,
  exportDraft,
  fillExportRequest,
  integer,
  mountExports,
  pump,
  scenarioCase,
  submitExportRequest,
  traffic,
} from './harness';

/** La única versión cuyo texto trae esta construcción (`backend/resources/privacy-notice`). */
const VERSION = 'v0-pendiente';

/** Elementos que se pueden activar con el teclado o el puntero. */
function reachable(): string[] {
  return [
    ...document.body.querySelectorAll<HTMLElement>(
      'a[href], button, input, select, textarea, summary, [tabindex], [contenteditable="true"]',
    ),
  ].map((element) => `${element.tagName.toLowerCase()}:${element.textContent}`);
}

describe('FS-APP-07 · privacy_notice_required sobrevenido', { timeout: 120_000 }, () => {
  it('pantalla de aceptación bloqueante en cualquier ruta y, tras aceptar, vuelta a la ruta', async (context) => {
    const parameters = scenarioCase(
      context,
      'FS-APP-07',
      fc.record({ draft: exportDraft, idleAfterPublishMs: integer(0, 90_000) }),
    );
    // El servidor anuncia la versión vigente en `GET /me`; el texto de esa versión viene en la
    // construcción. Al publicarse de nuevo, la aceptación previa deja de valer.
    const fixture = await mountExports({
      me: { ...shellMe(EXPORT_PERMISSIONS), privacy_notice_version: VERSION },
    });
    const log = traffic();
    server.use(...scenario('fs_app_07_privacy_notice'));
    faults.noticeVersion = VERSION;
    fillExportRequest(parameters.draft);

    faults.noticePending = true;
    await pump(fixture.clock, parameters.idleAfterPublishMs);
    if (fixture.services.session.getSnapshot().status === 'active') submitExportRequest();
    await screen.findByRole('heading', { level: 1, name: text('scr01.privacy.title') }, WAIT);
    expect(fixture.services.session.getSnapshot().status).toBe('privacy_notice_pending');
    expect(fixture.services.session.getSnapshot().return_route).toBe('/exports');
    expect(screen.getByText(VERSION).tagName).toBe('STRONG');
    expect(reachable()).toEqual([
      `button:${text('scr01.privacy.accept')}`,
      `button:${text('scr01.privacy.logout')}`,
    ]);
    expect(screen.queryByRole('form', { name: text('scr14.request') })).toBeNull();

    // Pasado un minuto (con su refresco periódico) la puerta sigue: no se sale sin aceptar.
    await pump(fixture.clock, 60_000);
    expect(reachable()).toHaveLength(2);
    expect(log.count('POST', '/privacy-notice/accept')).toBe(0);

    await userEvent
      .setup()
      .click(screen.getByRole('button', { name: text('scr01.privacy.accept') }));
    await waitFor(() => {
      expect(fixture.services.session.getSnapshot().status).toBe('active');
    }, WAIT);
    await screen.findByRole('form', { name: text('scr14.request') }, WAIT);
    expect(fixture.route()).toBe('/exports');
    expect(fixture.services.session.getSnapshot().return_route).toBeNull();
    expect(log.count('POST', '/privacy-notice/accept')).toBe(1);
  });
});
