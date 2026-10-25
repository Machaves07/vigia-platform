/** Regresión de VIG-302 / PR-APP-25 y PR-APP-30: /me mock no debe enclavar el bloqueo.
 * Reproducción: startMockApi, puerto /me y logout con transporte y controlador por omisión.
 */
import { expect, it, vi } from 'vitest';

import { appVersion } from '@/app/version';
import { startMockApi } from '@/mocks/browser';
import { createPortConnection, createTransport, installTransport } from '@/ports/base';
import { sessionPort } from '@/ports/shared';

import { flush } from '../../support/ports';
import { FakeClock } from '../../support/fakeClock';

it('el modo mock permite escribir tras /me y el arranque con el transporte por omisión', async () => {
  expect(import.meta.env.VITE_APP_VERSION).toBe('dev');
  expect(import.meta.env.VITE_CONTRACT_TAG).toBe('v1.1.0');
  const network = vi
    .spyOn(globalThis, 'fetch')
    .mockRejectedValue(new Error('El modo mock no debe salir a la red.'));
  const api = startMockApi();
  const clock = new FakeClock();
  installTransport(createTransport({ clock, connection: createPortConnection(clock) }));
  try {
    const me = await sessionPort.me.read();
    expect(appVersion.canWrite()).toBe(true);
    await expect(sessionPort.logout.write()).resolves.toBeUndefined();
    expect(me).toMatchObject({ api_version: import.meta.env.VITE_APP_VERSION });
    await expect(appVersion.start()).resolves.toBe('checked');
    await sessionPort.me.read();
    await flush();
    expect(appVersion.getSnapshot()).toEqual({ outdated: false, message: null });
    await expect(sessionPort.logout.write()).resolves.toBeUndefined();
    expect(network).not.toHaveBeenCalled();
  } finally {
    installTransport(null);
    api.stop();
  }
});
