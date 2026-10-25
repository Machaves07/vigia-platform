/**
 * Regresión de VIG-342 (LC-APP-01, R-APP-2; NFR-APP-31): la sonda H.264 invoca
 * `HTMLMediaElement.prototype.canPlayType` sobre un elemento `video` del documento. El método
 * nativo, llamado sin receptor, lanza `TypeError` («Illegal invocation» en Chromium); la sonda lo
 * convertía en `video_h264` ausente y ningún navegador compatible llegaba a cargar React.
 *
 * Se prueba con el `HTMLMediaElement` de jsdom —un método nativo de verdad, que comprueba su
 * receptor como el del navegador— y con uno sintético que responde «maybe». El rechazo de un
 * navegador sin H.264 sigue cerrado: jsdom no reproduce vídeo y responde `''`.
 */
import { describe, expect, it } from 'vitest';

import { mediaEnvironment } from '../../support/capabilities';
import {
  browserProbe,
  currentEnvironment,
  H264_TYPE,
  missingCapabilities,
  supportsH264,
  type BrowserEnvironment,
} from '@/bootstrap/check';

/** El método del prototipo tal cual, sin receptor: como lo extraía la sonda antes de VIG-342. */
function unboundCanPlayType(media: BrowserEnvironment['HTMLMediaElement']): () => unknown {
  const canPlayType = media?.prototype.canPlayType;
  if (typeof canPlayType !== 'function') throw new Error('falta canPlayType en el prototipo');
  return () => Reflect.apply(canPlayType, undefined, [H264_TYPE]) as unknown;
}

/**
 * Nombre del error que lanza `call`, o `null` si no lanza. Se compara el nombre y no la clase: los
 * errores de jsdom nacen en otro reino y no son instancias del `TypeError` de la prueba.
 */
function thrownName(call: () => unknown): string | null {
  try {
    call();
    return null;
  } catch (error) {
    return typeof error === 'object' && error !== null && 'name' in error
      ? String(error.name)
      : 'sin nombre';
  }
}

describe('sonda H.264 con receptor (VIG-342)', () => {
  it('el método nativo de jsdom lanza sin receptor, como el del navegador', () => {
    expect(thrownName(unboundCanPlayType(globalThis.HTMLMediaElement))).toBe('TypeError');
  });

  it('con el entorno en marcha, la sonda pregunta sin lanzar y jsdom declara que no reproduce', () => {
    const { canPlayType } = browserProbe(currentEnvironment());
    expect(canPlayType).toBeTypeOf('function');
    expect(canPlayType?.(H264_TYPE)).toBe('');
    // jsdom no tiene H.264: la capacidad sigue ausente, sin forzarla.
    expect(supportsH264(canPlayType)).toBe(false);
  });

  it('un método que exige su receptor responde a la sonda: el navegador compatible arranca', () => {
    const media = mediaEnvironment('maybe');
    expect(unboundCanPlayType(media.HTMLMediaElement)).toThrow(new TypeError('Illegal invocation'));
    const { canPlayType } = browserProbe(media);
    expect(canPlayType?.(H264_TYPE)).toBe('maybe');
    expect(supportsH264(canPlayType)).toBe(true);
    expect(missingCapabilities(browserProbe(media))).not.toContain('video_h264');
  });

  it('el receptor es un elemento nuevo del documento, creado como «video»', () => {
    const tags: unknown[] = [];
    const receivers: unknown[] = [];
    const created = { kind: 'video' };
    const probe = browserProbe({
      HTMLMediaElement: {
        prototype: {
          canPlayType(this: unknown) {
            receivers.push(this);
            return 'probably';
          },
        },
      },
      document: {
        createElement(tagName) {
          tags.push(tagName);
          return created;
        },
      },
    });
    expect(supportsH264(probe.canPlayType)).toBe(true);
    expect(tags).toEqual(['video']);
    expect(receivers).toHaveLength(1);
    expect(receivers[0]).toBe(created);
  });
});

describe('el rechazo de un navegador sin H.264 sigue cerrado', () => {
  it('un receptor que no es un HTMLMediaElement hace lanzar al método nativo', () => {
    const probe = browserProbe({
      HTMLMediaElement: globalThis.HTMLMediaElement,
      document: { createElement: () => document.createElement('div') },
    });
    expect(thrownName(() => probe.canPlayType?.(H264_TYPE))).toBe('TypeError');
    expect(supportsH264(probe.canPlayType)).toBe(false);
  });

  it('sin documento, sin createElement o si crear el elemento lanza, falta el vídeo', () => {
    const { HTMLMediaElement } = mediaEnvironment('probably');
    expect(browserProbe({ HTMLMediaElement }).canPlayType).toBeUndefined();
    expect(browserProbe({ HTMLMediaElement, document: {} }).canPlayType).toBeUndefined();
    expect(browserProbe({ HTMLMediaElement, document: undefined }).canPlayType).toBeUndefined();
    const throwing = browserProbe({
      HTMLMediaElement,
      document: {
        createElement: () => {
          throw new DOMException('sin elementos', 'NotSupportedError');
        },
      },
    });
    expect(supportsH264(throwing.canPlayType)).toBe(false);
    for (const probe of [browserProbe({ HTMLMediaElement }), throwing]) {
      expect(missingCapabilities(probe)).toContain('video_h264');
    }
  });

  it('sin HTMLMediaElement, o si el método no responde con texto, falta el vídeo', () => {
    const { document: page } = mediaEnvironment('probably');
    expect(browserProbe({ document: page }).canPlayType).toBeUndefined();
    expect(
      browserProbe({ HTMLMediaElement: { prototype: { canPlayType: 'probably' } }, document: page })
        .canPlayType,
    ).toBeUndefined();
    const nonText = browserProbe({
      HTMLMediaElement: { prototype: { canPlayType: () => ({ toString: () => 'probably' }) } },
      document: page,
    });
    expect(nonText.canPlayType?.(H264_TYPE)).toBe('');
    expect(supportsH264(nonText.canPlayType)).toBe(false);
  });

  it('un método con receptor que declara que no reproduce H.264 deja la capacidad ausente', () => {
    for (const verdict of ['', 'no', 'probablemente']) {
      const probe = browserProbe(mediaEnvironment(verdict));
      expect(supportsH264(probe.canPlayType), verdict).toBe(false);
      expect(missingCapabilities(probe), verdict).toContain('video_h264');
    }
  });
});
