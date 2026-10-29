/**
 * Tipografía propia (VIG-364): al arrancar se piden todas las caras aún sin cargar, una sola vez,
 * y un fallo de carga no rompe nada (queda la fuente del sistema).
 */
import { describe, expect, it, vi } from 'vitest';

import { preloadFonts } from '@/app/fonts';

function face(status: FontFaceLoadStatus, fails = false) {
  return {
    status,
    load: vi.fn(() => (fails ? Promise.reject(new Error('sin red')) : Promise.resolve())),
  };
}

describe('preloadFonts', () => {
  it('pide las caras sin cargar y no repite las que cargan o ya cargaron', async () => {
    const pending = face('unloaded');
    const failing = face('unloaded', true);
    const loading = face('loading');
    const loaded = face('loaded');
    const fonts = [pending, failing, loading, loaded] as unknown as FontFace[];
    expect(() => {
      preloadFonts(fonts);
    }).not.toThrow();
    await Promise.resolve();
    expect(pending.load).toHaveBeenCalledTimes(1);
    expect(failing.load).toHaveBeenCalledTimes(1);
    expect(loading.load).not.toHaveBeenCalled();
    expect(loaded.load).not.toHaveBeenCalled();
  });

  it('sin caras declaradas no hace nada', () => {
    expect(() => {
      preloadFonts([]);
    }).not.toThrow();
  });
});
