/**
 * Las cinco comprobaciones de capacidades del arranque (LC-APP-01, NFR-APP-31; PAT-APP-RES-07):
 * cada una falla por sí sola, `missingCapabilities` las nombra en el orden de `CAPABILITIES` y la
 * versión del navegador se compara contra la mínima de la política generada, con sus bordes.
 */
import { describe, expect, it } from 'vitest';

import { DateTimeFormatWithoutZones, mediaEnvironment } from '../../support/capabilities';
import {
  browserProbe,
  CAPABILITIES,
  detectBrowser,
  H264_TYPE,
  missingCapabilities,
  supportsBrowserVersion,
  supportsH264,
  supportsTimeZones,
  type BrowserEngine,
  type CapabilityId,
  type CapabilityProbe,
} from '@/bootstrap/check';
import { MIN_BROWSER_VERSIONS } from '@/bootstrap/minVersions.generated';

const ZONE = 'America/Bogota';

/** Los motores de la política: las claves de la tabla generada. */
const ENGINES = Object.keys(MIN_BROWSER_VERSIONS) as readonly BrowserEngine[];

/** Cadena de agente de `engine` con la versión pedida (los patrones de los navegadores reales). */
function userAgent(engine: BrowserEngine, major: number, minor: number): string {
  const version = `${String(major)}.${String(minor)}`;
  switch (engine) {
    case 'chrome':
      return (
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) ' +
        `Chrome/${version}.0.0 Safari/537.36`
      );
    case 'edge':
      return (
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) ' +
        `Chrome/${version}.0.0 Safari/537.36 Edg/${version}.0`
      );
    case 'firefox':
      return (
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:' +
        `${version}) Gecko/20100101 Firefox/${version}`
      );
    case 'safari':
      return (
        'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) ' +
        `Version/${version} Safari/605.1.15`
      );
  }
}

/** Sonda completa: las cinco capacidades presentes. */
const COMPLETE: CapabilityProbe = browserProbe({
  fetch: () => undefined,
  Intl: { DateTimeFormat: Intl.DateTimeFormat },
  crypto: { subtle: { digest: () => undefined } },
  ...mediaEnvironment('probably'),
  navigator: { userAgent: userAgent('chrome', 156, 0) },
});

function withoutCapability(missing: CapabilityId): CapabilityProbe {
  return {
    ...COMPLETE,
    ...(missing === 'fetch' ? { fetch: undefined } : {}),
    ...(missing === 'intl_timezone' ? { dateTimeFormat: undefined } : {}),
    ...(missing === 'crypto_subtle' ? { subtle: undefined } : {}),
    ...(missing === 'video_h264' ? { canPlayType: undefined } : {}),
    ...(missing === 'browser_version' ? { userAgent: 'curl/8.5.0' } : {}),
  };
}

describe('missingCapabilities', () => {
  it('una sonda completa no tiene ninguna capacidad ausente', () => {
    expect(missingCapabilities(COMPLETE)).toEqual([]);
  });

  it('nombra cada capacidad ausente en el orden de CAPABILITIES', () => {
    expect(CAPABILITIES).toEqual([
      'fetch',
      'intl_timezone',
      'crypto_subtle',
      'video_h264',
      'browser_version',
    ]);
    for (const capability of CAPABILITIES) {
      expect(missingCapabilities(withoutCapability(capability)), capability).toEqual([capability]);
    }
    expect(missingCapabilities(browserProbe({}))).toEqual([...CAPABILITIES]);
  });
});

describe('fetch', () => {
  it('exige una función, no un valor cualquiera', () => {
    expect(missingCapabilities({ ...COMPLETE, fetch: 'fetch' })).toEqual(['fetch']);
    expect(missingCapabilities({ ...COMPLETE, fetch: () => undefined })).toEqual([]);
  });
});

describe('Intl con zonas horarias', () => {
  it('acepta un Intl que resuelve una zona con nombre', () => {
    expect(supportsTimeZones(Intl.DateTimeFormat)).toBe(true);
    expect(new Intl.DateTimeFormat('es', { timeZone: ZONE }).resolvedOptions().timeZone).toBe(ZONE);
  });

  it('rechaza un Intl que ignora timeZone, uno que lanza y la ausencia de Intl', () => {
    expect(supportsTimeZones(DateTimeFormatWithoutZones)).toBe(false);
    expect(
      supportsTimeZones(
        class {
          constructor() {
            throw new RangeError('zona desconocida');
          }

          resolvedOptions(): { readonly timeZone?: string | undefined } {
            return {};
          }
        },
      ),
    ).toBe(false);
    expect(supportsTimeZones(undefined)).toBe(false);
  });
});

describe('crypto.subtle.digest', () => {
  it('exige digest dentro de subtle', () => {
    expect(missingCapabilities({ ...COMPLETE, subtle: { digest: () => undefined } })).toEqual([]);
    expect(missingCapabilities({ ...COMPLETE, subtle: {} })).toEqual(['crypto_subtle']);
    expect(missingCapabilities({ ...COMPLETE, subtle: undefined })).toEqual(['crypto_subtle']);
  });
});

describe('video con H.264', () => {
  it('pregunta por MP4/H.264 con perfil y nivel AVC explícitos', () => {
    const asked: string[] = [];
    expect(
      supportsH264((type) => {
        asked.push(type);
        return 'probably';
      }),
    ).toBe(true);
    expect(asked).toEqual([H264_TYPE]);
    expect(H264_TYPE).toBe('video/mp4; codecs="avc1.42E01E"');
  });

  it('acepta «maybe» y «probably» y rechaza el resto', () => {
    expect(supportsH264(() => 'maybe')).toBe(true);
    expect(supportsH264(() => '')).toBe(false);
    expect(supportsH264(() => 'no')).toBe(false);
    expect(supportsH264(() => 'probablemente')).toBe(false);
    expect(
      supportsH264(() => {
        throw new TypeError('sin descodificador');
      }),
    ).toBe(false);
    expect(supportsH264(undefined)).toBe(false);
  });
});

describe('versión mínima de la política de navegadores', () => {
  it('acepta la mínima de cada motor y rechaza la inmediatamente anterior', () => {
    for (const engine of ENGINES) {
      const [major, minor] = MIN_BROWSER_VERSIONS[engine];
      expect(detectBrowser(userAgent(engine, major, minor)), engine).toEqual({
        engine,
        major,
        minor,
      });
      expect(supportsBrowserVersion(userAgent(engine, major, minor)), engine).toBe(true);
      const previous: readonly [number, number] = minor > 0 ? [major, minor - 1] : [major - 1, 0];
      expect(detectBrowser(userAgent(engine, previous[0], previous[1])), engine).toEqual({
        engine,
        major: previous[0],
        minor: previous[1],
      });
      expect(supportsBrowserVersion(userAgent(engine, previous[0], previous[1])), engine).toBe(
        false,
      );
    }
  });

  it('rechaza los navegadores fuera de la política', () => {
    const outOfPolicy = [
      'Mozilla/5.0 (Windows NT 10.0; WOW64; Trident/7.0; rv:11.0) like Gecko',
      'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36 OPR/120.0.0.0',
      'Mozilla/5.0 (Linux; Android 14; SM-S918B) AppleWebKit/537.36 (KHTML, like Gecko) SamsungBrowser/26.0 Chrome/156.0.0.0 Mobile Safari/537.36',
      'Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/156.0.0.0 Mobile Safari/537.36; wv)',
      'Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) CriOS/156.0.0.0 Mobile/15E148 Safari/604.1',
      'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Electron/40.0.0 Chrome/156.0.0.0 Safari/537.36',
      'curl/8.5.0',
      '',
    ];
    for (const agent of outOfPolicy) {
      expect(detectBrowser(agent), agent).toBeNull();
      expect(supportsBrowserVersion(agent), agent).toBe(false);
      expect(missingCapabilities({ ...COMPLETE, userAgent: agent }), agent).toEqual([
        'browser_version',
      ]);
    }
  });
});
