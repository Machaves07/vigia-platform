// @vitest-environment node
/**
 * Piezas de `vite.config.ts` (LC-APP-29): regla D-3 del proxy de desarrollo, porciones por
 * pantalla y `VITE_API_MODE`; y los perfiles de fast-check de `vitest.config.ts`.
 */
import { describe, expect, it } from 'vitest';

import {
  isNavigationRequest,
  isViteOwnedPath,
  parseApiMode,
  screenChunkName,
} from '../../../config/build';
import { NUM_RUNS, parseSeed, resolveProfile } from '../../../config/fastCheckProfile';

describe('D-3: solo la navegación recibe index.html; lo demás va a vigia-api', () => {
  it.each([
    [{ 'sec-fetch-mode': 'navigate' }, true],
    [{ 'sec-fetch-mode': ' Navigate ', accept: 'application/json' }, true],
    [{ accept: 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8' }, true],
    [{ 'sec-fetch-mode': 'cors', accept: 'text/html' }, true],
    [{ 'sec-fetch-mode': 'cors', accept: 'application/json' }, false],
    [{ accept: 'application/json' }, false],
    [{ accept: 'text/html, application/json' }, false],
    [{ accept: 'text/html;q=0' }, false],
    [{ accept: 'text/html, application/json;q=0' }, true],
    [{ accept: 'text/html;q=2' }, false],
    [{ accept: '*/*' }, false],
    [{}, false],
    [{ accept: ['text/html'] }, true],
  ])('%j → navegación %s', (headers, expected) => {
    expect(isNavigationRequest(headers)).toBe(expected);
  });

  it('las rutas de API que coinciden con pantallas se reenvían si no son navegación', () => {
    for (const path of ['/concessions', '/zones/z', '/fleet/nodes', '/loop/x', '/notifications']) {
      expect(isViteOwnedPath(path)).toBe(false);
    }
    expect(isNavigationRequest({ 'sec-fetch-mode': 'cors', accept: 'application/json' })).toBe(
      false,
    );
  });

  it('lo que sirve Vite nunca se reenvía', () => {
    for (const path of ['/@vite/client', '/src/main.tsx?t=1', '/node_modules/.vite/deps/x.js']) {
      expect(isViteOwnedPath(path)).toBe(true);
    }
    expect(isViteOwnedPath('/api/src/x')).toBe(false);
  });

  it('solo considera las 64 primeras entradas de Accept, como la API', () => {
    const padding = Array.from({ length: 64 }, (_, i) => `x/y${String(i)}`).join(',');
    expect(isNavigationRequest({ accept: `${padding},text/html` })).toBe(false);
  });
});

describe('porciones y modo de API', () => {
  it.each([
    ['/repo/frontend/src/pages/scr01/Login.tsx', 'scr01'],
    ['C:\\repo\\frontend\\src\\pages\\scr16\\index.ts', 'scr16'],
    ['/repo/frontend/src/pages/scr08/home/index.tsx', 'scr08'],
    ['/repo/frontend/src/pages/scr08/descriptor.ts', null],
    ['C:\\repo\\frontend\\src\\pages\\scr09\\descriptor.ts', null],
    ['/repo/frontend/src/pages/scr09/loop/descriptor.ts.tsx', 'scr09'],
    ['/repo/frontend/src/pages/shared/ScreenState.tsx', null],
    ['/repo/frontend/src/components/Badge.tsx', null],
    ['/repo/frontend/src/pages/scr1/x.tsx', null],
    ['/repo/node_modules/react/index.js', null],
  ])('%s → %s', (id, chunk) => {
    expect(screenChunkName(id)).toBe(chunk);
  });

  it('VITE_API_MODE es mock o real; sin valor, real; otro valor detiene', () => {
    expect(parseApiMode(undefined)).toBe('real');
    expect(parseApiMode('')).toBe('real');
    expect(parseApiMode('mock')).toBe('mock');
    expect(parseApiMode('real')).toBe('real');
    expect(() => parseApiMode('MOCK')).toThrow('VITE_API_MODE');
  });
});

describe('perfiles de fast-check', () => {
  it('ci usa 200 ejemplos y nightly 2 000', () => {
    expect(NUM_RUNS).toEqual({ ci: 200, nightly: 2000 });
    expect(resolveProfile({ FC_SEED: '7' }).numRuns).toBe(200);
    expect(resolveProfile({ FC_SEED: '7' }, 'nightly').numRuns).toBe(2000);
    expect(resolveProfile({ FC_PROFILE: 'ci', FC_SEED: '7' }, 'nightly').numRuns).toBe(200);
    expect(resolveProfile({ FC_PROFILE: 'nightly', FC_SEED: '7' }).numRuns).toBe(2000);
  });

  it('FC_SEED fija la semilla; sin ella se elige una', () => {
    expect(resolveProfile({ FC_SEED: '-42' }).seed).toBe(-42);
    expect(Number.isSafeInteger(resolveProfile({}).seed)).toBe(true);
  });

  it.each(['1.5', 'abc', '99999999999', '2147483648', ' 1'])('rechaza FC_SEED=%s', (value) => {
    expect(() => parseSeed(value)).toThrow('FC_SEED');
  });

  it('rechaza un perfil desconocido', () => {
    expect(() => resolveProfile({ FC_PROFILE: 'weekly' })).toThrow('FC_PROFILE');
  });
});
