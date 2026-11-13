/**
 * UI-26 `BlurredEpisodePhoto` (TASK-574; A-112, «Precisiones del 13-11, lectura y purga»).
 *
 * - Una petición por apertura: al montar y nunca más sola (ni con el doble montaje de
 *   `StrictMode`, ni al enfocar la ventana, ni tras un fallo); volver a pedirla es un gesto.
 * - Se pinta en un `<canvas role="img">` con su texto alternativo, desde el `Blob` (sin URL).
 * - Sin descarga: ningún enlace ni `download`, lienzo no arrastrable y el menú contextual y el
 *   arrastre anulados (el evento real, no un atributo).
 * - Sin imagen: el motivo etiquetado y ninguna petición.
 * - El aviso y la superposición están en todos los estados; se comprueban por clave de texto.
 *
 * jsdom no tiene `createImageBitmap` ni contexto 2D: el decodificador se inyecta y el contexto se
 * sustituye por uno que anota lo que se dibuja. El navegador real lo prueba
 * `tests/browser/episode_photo_csp.spec.ts` contra la CSP del backend.
 */
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { StrictMode, type ReactElement } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { axe } from 'vitest-axe';

import {
  BlurredEpisodePhoto,
  type BlurredEpisodePhotoProps,
} from '@/components/BlurredEpisodePhoto/BlurredEpisodePhoto';
import { PortError } from '@/ports/shared';
import { label } from '@/texts/labels';
import { text } from '@/texts';

const WAIT = { timeout: 10_000 };
const PHOTO = '0190a1b2-0000-7000-8000-000000000201';
const ALT = 'Foto difuminada sintética';
const NOTICE = 'media.episode_photo.commissioning_notice';
const OVERLAY = 'media.episode_photo.blurred_automatically';

interface FakeBitmap {
  readonly width: number;
  readonly height: number;
  readonly close: ReturnType<typeof vi.fn>;
}

/** Mapa de bits sintético: solo lo que el componente usa. */
function bitmap(width = 64, height = 48): FakeBitmap {
  return { width, height, close: vi.fn() };
}

/** El mapa de bits sintético como lo recibe el componente. */
function asBitmap(value: FakeBitmap): ImageBitmap {
  return value as unknown as ImageBitmap;
}

function jpeg(): Blob {
  return new Blob([new Uint8Array([0xff, 0xd8, 0xff, 0xd9])], { type: 'image/jpeg' });
}

const drawImage = vi.fn();

beforeEach(() => {
  drawImage.mockReset();
  vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockImplementation(
    () => ({ drawImage }) as unknown as CanvasRenderingContext2D,
  );
});

afterEach(() => {
  vi.restoreAllMocks();
});

function deferred<T>() {
  let resolve: (value: T) => void = () => undefined;
  let reject: (error: unknown) => void = () => undefined;
  const promise = new Promise<T>((ok, fail) => {
    resolve = ok;
    reject = fail;
  });
  return { promise, resolve, reject };
}

function mount(props: Partial<BlurredEpisodePhotoProps> = {}, strict = true) {
  const requestImage = props.requestImage ?? vi.fn(() => Promise.resolve(jpeg()));
  const decodeImage = props.decodeImage ?? vi.fn(() => Promise.resolve(asBitmap(bitmap())));
  const element: ReactElement = (
    <BlurredEpisodePhoto
      photoId={PHOTO}
      hasImage
      absentReason={null}
      alt={ALT}
      {...props}
      requestImage={requestImage}
      decodeImage={decodeImage}
    />
  );
  const view = render(strict ? <StrictMode>{element}</StrictMode> : element);
  return { view, requestImage, decodeImage };
}

function byKey(key: string): Element | null {
  return document.querySelector(`[data-text-key="${key}"]`);
}

/** El aviso y la superposición, por clave y con su texto exacto del catálogo. */
function expectNoticeAndOverlay(): void {
  expect(byKey(NOTICE)?.textContent).toBe(text(NOTICE));
  expect(byKey(OVERLAY)?.textContent).toBe(text(OVERLAY));
}

async function painted(): Promise<HTMLCanvasElement> {
  const element = await screen.findByRole('img', { name: ALT }, WAIT);
  if (!(element instanceof HTMLCanvasElement)) throw new Error('La foto no está en un lienzo');
  return element;
}

describe('una petición por apertura (A-112)', () => {
  it('pide la imagen una vez al montar, también con el doble montaje de StrictMode', async () => {
    const { requestImage } = mount();
    const canvas = await painted();
    expect(requestImage).toHaveBeenCalledTimes(1);
    expect(requestImage).toHaveBeenCalledWith(PHOTO, expect.any(AbortSignal));
    expect(canvas.tagName).toBe('CANVAS');
    expect([canvas.width, canvas.height]).toEqual([64, 48]);
    expect(drawImage).toHaveBeenCalledTimes(1);
  });

  it('enfocar la ventana o volver a la pestaña no la pide otra vez', async () => {
    const { requestImage } = mount();
    await painted();
    act(() => {
      window.dispatchEvent(new Event('focus'));
      document.dispatchEvent(new Event('visibilitychange'));
      window.dispatchEvent(new Event('online'));
    });
    await act(async () => {
      await Promise.resolve();
    });
    expect(requestImage).toHaveBeenCalledTimes(1);
  });

  it('un fallo no se repite solo; «Volver a pedir la imagen» es un gesto y pide una más', async () => {
    const failure = new PortError({ kind: 'network', behaviour: { kind: 'transient' } as never });
    const requestImage = vi
      .fn<(id: string, signal: AbortSignal) => Promise<Blob>>()
      .mockRejectedValueOnce(failure)
      .mockResolvedValue(jpeg());
    const onImageError = vi.fn();
    mount({ requestImage, onImageError });
    const retry = await screen.findByRole(
      'button',
      { name: text('media.episode_photo.request_again') },
      WAIT,
    );
    expect(onImageError).toHaveBeenCalledTimes(1);
    expect(onImageError).toHaveBeenCalledWith(failure);
    expect(requestImage).toHaveBeenCalledTimes(1);
    expectNoticeAndOverlay();
    fireEvent.click(retry);
    await painted();
    expect(requestImage).toHaveBeenCalledTimes(2);
  });

  it('el mapa de bits se cierra después de pintar', async () => {
    const image = bitmap();
    mount({ decodeImage: vi.fn(() => Promise.resolve(asBitmap(image))) });
    await painted();
    expect(drawImage).toHaveBeenCalledWith(image, 0, 0);
    expect(image.close).toHaveBeenCalledTimes(1);
  });

  it('al desmontar cancela la petición en vuelo y no pinta lo que llegue tarde', async () => {
    const pending = deferred<Blob>();
    let signal: AbortSignal | null = null;
    const image = bitmap();
    const { view } = mount({
      requestImage: vi.fn((_id: string, abort: AbortSignal) => {
        signal = abort;
        return pending.promise;
      }),
      decodeImage: vi.fn(() => Promise.resolve(asBitmap(image))),
    });
    await waitFor(() => {
      expect(signal).not.toBeNull();
    }, WAIT);
    view.unmount();
    await act(async () => {
      await Promise.resolve();
    });
    expect((signal as AbortSignal | null)?.aborted).toBe(true);
    pending.resolve(jpeg());
    await act(async () => {
      await pending.promise;
      await Promise.resolve();
    });
    expect(drawImage).not.toHaveBeenCalled();
  });

  it('si cambia el episodio, la imagen del anterior que llega tarde no se pinta', async () => {
    const first = deferred<Blob>();
    const second = deferred<Blob>();
    const stale = bitmap(10, 10);
    const fresh = bitmap(64, 48);
    const requestImage = vi.fn((id: string) => (id === PHOTO ? first.promise : second.promise));
    const decodeImage = vi.fn((blob: Blob) =>
      Promise.resolve(asBitmap(blob.size === 1 ? stale : fresh)),
    );
    const other = '0190a1b2-0000-7000-8000-000000000202';
    const props = { hasImage: true, absentReason: null, alt: ALT, requestImage, decodeImage };
    const view = render(<BlurredEpisodePhoto photoId={PHOTO} {...props} />);
    view.rerender(<BlurredEpisodePhoto photoId={other} {...props} />);
    await act(async () => {
      first.resolve(new Blob([new Uint8Array([1])], { type: 'image/jpeg' }));
      await first.promise;
      for (let round = 0; round < 5; round += 1) await Promise.resolve();
    });
    expect(drawImage).not.toHaveBeenCalled();
    expect(stale.close).toHaveBeenCalledTimes(1);
    second.resolve(jpeg());
    await painted();
    expect(drawImage).toHaveBeenCalledWith(fresh, 0, 0);
  });

  it('lo que no es image/jpeg no llega al lienzo', async () => {
    const decodeImage = vi.fn(() => Promise.resolve(asBitmap(bitmap())));
    const onImageError = vi.fn();
    mount({
      requestImage: vi.fn(() => Promise.resolve(new Blob(['<html>'], { type: 'text/html' }))),
      decodeImage,
      onImageError,
    });
    await screen.findByRole('button', { name: text('media.episode_photo.request_again') }, WAIT);
    expect(decodeImage).not.toHaveBeenCalled();
    expect(drawImage).not.toHaveBeenCalled();
    expect(onImageError).toHaveBeenCalledTimes(1);
  });
});

describe('sin descarga (BR-APP-58)', () => {
  it('ningún enlace ni download; lienzo no arrastrable; menú contextual y arrastre anulados', async () => {
    const { view } = mount();
    const canvas = await painted();
    expect(view.container.querySelectorAll('a')).toHaveLength(0);
    expect(view.container.querySelectorAll('[download]')).toHaveLength(0);
    expect(view.container.querySelectorAll('img, video, object, embed, iframe')).toHaveLength(0);
    expect(canvas.getAttribute('draggable')).toBe('false');
    const buttons = [...view.container.querySelectorAll('button')];
    expect(buttons).toHaveLength(0);

    const contextMenu = new MouseEvent('contextmenu', { bubbles: true, cancelable: true });
    canvas.dispatchEvent(contextMenu);
    expect(contextMenu.defaultPrevented).toBe(true);
    const overlay = byKey(OVERLAY);
    const onOverlay = new MouseEvent('contextmenu', { bubbles: true, cancelable: true });
    overlay?.dispatchEvent(onOverlay);
    expect(onOverlay.defaultPrevented).toBe(true);
    const drag = new Event('dragstart', { bubbles: true, cancelable: true });
    canvas.dispatchEvent(drag);
    expect(drag.defaultPrevented).toBe(true);
  });
});

describe('sin imagen', () => {
  it.each(['frame_unavailable', 'too_large', 'disk_pressure', 'pending_cap'])(
    'con «%s» muestra el motivo etiquetado y no pide nada',
    async (reason) => {
      const { requestImage, view } = mount({ hasImage: false, absentReason: reason });
      await act(async () => {
        await Promise.resolve();
      });
      expect(requestImage).not.toHaveBeenCalled();
      expect(view.container.querySelector('canvas')).toBeNull();
      expect(view.container.querySelector('[data-part="absent_reason"]')?.textContent).toBe(
        label('image_absent_reason', reason),
      );
      expect(byKey('media.episode_photo.absent')).not.toBeNull();
      expectNoticeAndOverlay();
    },
  );
});

describe('textos y accesibilidad', () => {
  it('aviso y superposición mientras carga y ya pintada, con el texto «automáticamente»', async () => {
    const pending = deferred<Blob>();
    mount({ requestImage: vi.fn(() => pending.promise) });
    expectNoticeAndOverlay();
    expect(byKey('media.episode_photo.loading')).not.toBeNull();
    pending.resolve(jpeg());
    await painted();
    expectNoticeAndOverlay();
    expect(text(OVERLAY)).toBe('Imagen difuminada automáticamente');
  });

  it('el lienzo es role="img" con su texto alternativo y axe no encuentra violaciones', async () => {
    const { view } = mount();
    const canvas = await painted();
    expect(canvas.getAttribute('role')).toBe('img');
    expect(canvas.getAttribute('aria-label')).toBe(ALT);
    expect(await axe(view.container)).toHaveNoViolations();
  });

  it('axe sin violaciones con el motivo de la ausencia', async () => {
    const { view } = mount({ hasImage: false, absentReason: 'too_large' });
    expect(await axe(view.container)).toHaveNoViolations();
  });
});
