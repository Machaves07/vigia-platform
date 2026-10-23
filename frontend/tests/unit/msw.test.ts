/**
 * MSW por omisión en modo error (`tests/setup/msw.ts`): una petición sin manejador falla y
 * nunca sale a la red; con manejador, responde el manejador.
 */
import { http, HttpResponse } from 'msw';
import { describe, expect, it, vi } from 'vitest';

import { server } from '../setup/msw';

describe('MSW en las pruebas', () => {
  it('una petición sin manejador falla', async () => {
    const consoleError = vi.spyOn(console, 'error').mockImplementation(() => undefined);
    await expect(fetch('http://localhost/sin-manejador')).rejects.toThrow();
    expect(consoleError).toHaveBeenCalled();
  });

  it('una petición con manejador recibe su respuesta', async () => {
    server.use(http.get('http://localhost/me', () => HttpResponse.json({ ok: true })));
    const response = await fetch('http://localhost/me');
    expect(await response.json()).toEqual({ ok: true });
  });
});
