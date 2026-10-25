/**
 * FS-APP-08 (PAT-APP-RES-04, LC-APP-05): versión de la API distinta tras un despliegue y porción
 * de código antigua que ya no se sirve.
 *
 * Con la solicitud de exportación de SCR-14 a medio escribir:
 * - una respuesta de la API anuncia otra versión (`X-Vigia-Api-Version`), o
 * - la precarga de una porción de código falla (el recurso con hash antiguo ya no existe).
 * En los dos casos aparece «hay una versión nueva; recargue», las escrituras quedan bloqueadas
 * sin salir a la red, el formulario conserva lo escrito y la aplicación **nunca recarga sola**:
 * solo «Recargar», por acción de la persona, pide la recarga y conserva la ruta.
 */
import { fireEvent, screen, within } from '@testing-library/react';
import fc from 'fast-check';
import { describe, expect, it, vi } from 'vitest';

import type { ScreenDescriptor } from '@/app/descriptor';
import { appVersion, createAppVersion } from '@/app/version';
import { descriptor as exportsDescriptor } from '@/pages/scr14/descriptor';
import { text } from '@/texts';

import { scenario } from '../msw/scenario';
import { faults } from '../msw/scenarios/fs_app';
import { server } from '../setup/msw';
import { shellMe, syntheticScreen, WAIT } from '../support/appShell';
import {
  EXPORT_PERMISSIONS,
  exportDraft,
  exportFormValues,
  fillExportRequest,
  integer,
  mountExports,
  pump,
  scenarioCase,
  submitExportRequest,
  syntheticText,
  traffic,
} from './harness';

/** Recarga del controlador común (`appVersion`): la porción antigua lo marca a él (router.tsx). */
const reloadShared = vi.hoisted(() => vi.fn());

vi.mock('@/app/version', async (importOriginal) => {
  const original = await importOriginal<typeof import('@/app/version')>();
  return { ...original, appVersion: original.createAppVersion({ reload: reloadShared }) };
});

const WRITE = '/accreditation/exports';

function versionNotice(): HTMLElement {
  return screen.getByRole('region', { name: text('app_shell.version.label') });
}

/** Lo común a los dos casos: aviso, escrituras bloqueadas sin red, borrador intacto, sin recargar. */
async function expectControlledReload(options: {
  readonly reload: ReturnType<typeof vi.fn>;
  readonly clock: Parameters<typeof pump>[0];
  readonly written: ReturnType<typeof exportFormValues>;
  readonly log: ReturnType<typeof traffic>;
  readonly route: () => string;
}): Promise<void> {
  const { reload, clock, written, log } = options;
  const notice = await screen.findByRole('region', { name: text('app_shell.version.label') }, WAIT);
  expect(notice.querySelector('[data-text-key="version.outdated"]')?.textContent).toBe(
    text('version.outdated'),
  );
  expect(exportFormValues()).toEqual(written);

  submitExportRequest();
  await pump(clock, 1_000);
  expect(log.count('POST', WRITE)).toBe(0);

  // Diez minutos después sigue sin recargar y con el borrador: la decisión es de la persona.
  await pump(clock, 600_000, 60_000);
  expect(reload).not.toHaveBeenCalled();
  expect(exportFormValues()).toEqual(written);
  expect(options.route()).toBe('/exports');

  fireEvent.click(
    within(versionNotice()).getByRole('button', { name: text('app_shell.version.reload') }),
  );
  expect(reload).toHaveBeenCalledTimes(1);
  expect(reload.mock.calls[0]?.[0]).toMatchObject({ route: '/exports' });
}

describe('FS-APP-08 · versión distinta y porción antigua', { timeout: 120_000 }, () => {
  it('versión de la API distinta: recarga controlada sin perder el borrador, nunca sola', async (context) => {
    const parameters = scenarioCase(
      context,
      'FS-APP-08',
      fc.record({
        case: fc.constant('api_version'),
        draft: exportDraft,
        deployedVersion: syntheticText('despliegue'),
        detectAfterMs: integer(0, 59_000),
      }),
    );
    const reload = vi.fn();
    const fixture = await mountExports({
      version: (clock) => createAppVersion({ clock, reload }),
    });
    const log = traffic();
    server.use(...scenario('fs_app_08_new_version'));
    fillExportRequest(parameters.draft);
    const written = exportFormValues();

    await pump(fixture.clock, parameters.detectAfterMs);
    // Despliegue: desde ahora la API anuncia otra versión; la ve el refresco de la campana.
    faults.apiVersion = parameters.deployedVersion;
    await pump(fixture.clock, 60_000);
    expect(fixture.services.version.getSnapshot().outdated).toBe(true);
    await expectControlledReload({
      reload,
      clock: fixture.clock,
      written,
      log,
      route: fixture.route,
    });
  });

  it('porción de código antigua no encontrada: el mismo bloqueo, sin recargar sola', async (context) => {
    const parameters = scenarioCase(
      context,
      'FS-APP-08',
      fc.record({ case: fc.constant('stale_chunk'), draft: exportDraft }),
    );
    // Inicio de la persona con una porción que ya no se sirve: la precarga tras la primera pintura
    // la pide y falla como falla un `import()` de un recurso con hash antiguo.
    const stale = syntheticScreen({
      id: 'SCR-15',
      title: 'navigation.scr15',
      path: '/notifications',
      permission: 'notifications.read',
      page: () => null,
      home: true,
    });
    const [group, ...others] = stale.route_groups;
    const home: ScreenDescriptor = {
      ...stale,
      route_groups: [
        {
          ...group,
          load: () => Promise.reject(new TypeError('Failed to fetch dynamically imported module')),
        },
        ...others,
      ],
    };
    const fixture = await mountExports({
      me: shellMe(EXPORT_PERMISSIONS),
      screens: [exportsDescriptor, home],
      version: () => appVersion,
    });
    const log = traffic();
    fillExportRequest(parameters.draft);
    const written = exportFormValues();
    expect(reloadShared).not.toHaveBeenCalled();
    await expectControlledReload({
      reload: reloadShared,
      clock: fixture.clock,
      written,
      log,
      route: fixture.route,
    });
  });
});
