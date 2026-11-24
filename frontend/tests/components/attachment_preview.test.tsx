/**
 * `AttachmentPreview` (TASK-609; O-22; A-112, «Precisiones del 13-11»; BR-APP-58): la foto adjunta
 * a una evidencia del cierre se mira en un `<canvas role="img">`, sin `<img>`, sin URL de objeto y
 * sin descarga. La URL se pide por `ports.upload` → `ports.base` **solo** con «Ver foto» (MSW
 * cuenta las peticiones), se usa una vez para el `fetch` sin credenciales y nunca se reintenta
 * sola; los fallos se quedan en el bloque, con los textos del mapa cerrado.
 *
 * jsdom no tiene `createImageBitmap` ni contexto 2D: el decodificador se inyecta y el contexto se
 * simula. Los bytes son un PNG y un JPEG **sintéticos generados** aquí, nunca una imagen real. Se
 * comparan atributos, claves del catálogo y nombres accesibles exactos.
 */
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { HttpResponse, http } from 'msw';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { axe } from 'vitest-axe';

import { apiError, variant } from '../msw/handlers';
import { server } from '../setup/msw';
import { FakeClock } from '../support/fakeClock';
import {
  AttachmentPreview,
  type AttachmentPreviewProps,
} from '@/components/DirectUploadField/AttachmentPreview';
import { createPortConnection, createTransport, installTransport } from '@/ports/base';
import errors from '@/texts/errors.json';
import catalog from '@/texts/media.json';

type CatalogKey = keyof typeof catalog;
const t = (key: CatalogKey): string => catalog[key];

const WAIT = { timeout: 10_000 };
const START = Date.UTC(2026, 10, 24, 12);
const ATTACHMENT_ID = '0192d3e4-5f60-7a1b-8c2d-3e4f5a6b7c8d';
const ALT = 'Foto adjunta';
const READ_URL = 'read_url_loop_attachments__attachment_id__read_url_post';
const STORE = 'https://vigia-evidence-000000000000-us-east-1.s3.us-east-1.amazonaws.com';

let clock: FakeClock;
/** Peticiones de URL recibidas (adjunto pedido), URL entregadas y peticiones al almacén. */
let requested: string[];
let issued: string[];
let fetched: Request[];

/** Bytes sintéticos con la firma del formato y relleno generado: solo dan el tipo y el tamaño. */
function synthetic(kind: 'png' | 'jpeg', size = 64): Uint8Array {
  const signature =
    kind === 'png' ? [0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a] : [0xff, 0xd8, 0xff, 0xe0];
  const bytes = new Uint8Array(size);
  bytes.set(signature);
  for (let index = signature.length; index < size; index += 1) bytes[index] = (index * 37) % 251;
  return bytes;
}

function urlOf(input: RequestInfo | URL): string {
  if (input instanceof Request) return input.url;
  return input instanceof URL ? input.href : input;
}

function serveReadUrl() {
  server.use(
    variant(READ_URL, ({ params }) => {
      const id = String(params.attachment_id);
      requested.push(id);
      const url = `${STORE}/closure/${id}.png?X-Amz-Signature=${String(issued.length + 1).padStart(8, '0')}`;
      issued.push(url);
      return HttpResponse.json({
        attachment_id: id,
        url,
        expires_at: new Date(clock.now() + 900_000).toISOString(),
        content_disposition: 'attachment',
      });
    }),
  );
}

function serveStore(response: () => Response) {
  server.use(
    http.get(`${STORE}/closure/:name`, ({ request }) => {
      fetched.push(request);
      return response();
    }),
  );
}

const drawImage = vi.fn();
const close = vi.fn();
const decodeImage = vi.fn<(blob: Blob) => Promise<ImageBitmap>>(() =>
  Promise.resolve({ width: 64, height: 48, close } as unknown as ImageBitmap),
);
let objectUrls: ReturnType<typeof vi.spyOn>;

beforeEach(() => {
  clock = new FakeClock(START);
  requested = [];
  issued = [];
  fetched = [];
  drawImage.mockReset();
  close.mockReset();
  decodeImage.mockClear();
  vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockImplementation(
    () => ({ drawImage }) as unknown as CanvasRenderingContext2D,
  );
  objectUrls = vi.spyOn(URL, 'createObjectURL');
  const connection = createPortConnection(clock);
  installTransport(createTransport({ clock, connection, devLog: () => undefined }));
});

afterEach(() => {
  installTransport(null);
  vi.restoreAllMocks();
});

function mount(props: Partial<AttachmentPreviewProps> = {}) {
  return render(
    <main>
      <h1>Ficha de vida del hallazgo</h1>
      <AttachmentPreview
        attachmentId={ATTACHMENT_ID}
        alt={ALT}
        clock={clock}
        decodeImage={decodeImage}
        {...props}
      />
      <p data-testid="resto">Resto de la ficha</p>
    </main>,
  );
}

function show() {
  fireEvent.click(screen.getByRole('button', { name: t('media.attachment.show_photo') }));
}

function png(): Response {
  return new HttpResponse(synthetic('png'), {
    headers: { 'Content-Type': 'image/png', 'Content-Disposition': 'attachment' },
  });
}

describe('a demanda (BR-APP-58; m_pide_al_montar)', () => {
  it('montar no pide ninguna URL ni toca el almacén; «Ver foto» pide una sola', async () => {
    serveReadUrl();
    serveStore(png);
    const { container } = mount();
    await act(async () => {
      clock.advance(60_000);
      await Promise.resolve();
    });
    expect(requested).toEqual([]);
    expect(fetched).toEqual([]);
    expect(container.querySelector('canvas')).toBeNull();
    show();
    const canvas = await screen.findByRole('img', { name: ALT }, WAIT);
    await waitFor(() => {
      expect(canvas.getAttribute('data-phase')).toBe('ready');
    }, WAIT);
    expect(requested).toEqual([ATTACHMENT_ID]);
    expect(fetched).toHaveLength(1);
    expect(drawImage).toHaveBeenCalledTimes(1);
    expect(close).toHaveBeenCalledTimes(1);
  });

  it('el fetch va a la URL concedida, sin credenciales ni referente', async () => {
    serveReadUrl();
    serveStore(png);
    const original = globalThis.fetch.bind(globalThis);
    const spy = vi
      .spyOn(globalThis, 'fetch')
      .mockImplementation((input, init) => original(input, init));
    mount();
    show();
    await waitFor(() => {
      expect(fetched).toHaveLength(1);
    }, WAIT);
    const toStore = spy.mock.calls.filter(([input]) => urlOf(input) === issued[0]);
    expect(toStore).toHaveLength(1);
    const init = toStore[0]?.[1];
    expect(init?.credentials).toBe('omit');
    expect(init?.referrerPolicy).toBe('no-referrer');
    expect(init?.headers).toBeUndefined();
    expect(fetched[0]?.headers.get('x-vigia-concession')).toBeNull();
  });
});

describe('sin <img src> ni descarga (A-112; m_img_src_adjunto)', () => {
  it('pinta en un canvas role="img" con su texto alternativo; ni <img>, ni blob:, ni enlace', async () => {
    serveReadUrl();
    serveStore(png);
    const { container } = mount();
    show();
    const canvas = await screen.findByRole('img', { name: ALT }, WAIT);
    await waitFor(() => {
      expect(canvas.hidden).toBe(false);
    }, WAIT);
    expect(canvas.tagName).toBe('CANVAS');
    expect(canvas.getAttribute('draggable')).toBe('false');
    expect(container.querySelectorAll('img, a, [download], source')).toHaveLength(0);
    expect(objectUrls).not.toHaveBeenCalled();
    expect(container.innerHTML.includes('blob:')).toBe(false);
    // La URL firmada no queda en el DOM.
    expect(container.innerHTML.includes(issued[0] ?? '#')).toBe(false);
    const frame = container.querySelector('[data-part="attachment_frame"]');
    if (frame === null) throw new Error('sin marco');
    expect(fireEvent.contextMenu(frame)).toBe(false);
    expect(fireEvent.dragStart(frame)).toBe(false);
    expect(container.querySelector('[data-part="shield"]')).not.toBeNull();
  });

  it('un objeto que no es una foto no se pinta: «Adjunto no disponible ahora»', async () => {
    serveReadUrl();
    serveStore(
      () =>
        new HttpResponse('<html></html>', {
          headers: { 'Content-Type': 'text/html' },
        }),
    );
    const { container } = mount();
    show();
    const alert = await screen.findByRole('alert', {}, WAIT);
    expect(alert.getAttribute('data-text-key')).toBe('media.attachment.unavailable');
    expect(decodeImage).not.toHaveBeenCalled();
    expect(drawImage).not.toHaveBeenCalled();
    expect(container.querySelectorAll('img')).toHaveLength(0);
  });
});

describe('fallos solo en el bloque y sin reintento automático', () => {
  it('storage_unavailable: el texto del mapa cerrado y «Volver a pedir la foto» por gesto', async () => {
    server.use(
      variant(READ_URL, ({ params }) => {
        requested.push(String(params.attachment_id));
        return apiError('storage_unavailable', 503);
      }),
    );
    mount();
    show();
    const alert = await screen.findByRole('alert', {}, WAIT);
    expect(alert.getAttribute('data-text-key')).toBe('errors.storage_unavailable');
    expect(alert.querySelector('p')?.textContent).toBe(errors['errors.storage_unavailable']);
    expect(screen.getByTestId('resto').textContent).toBe('Resto de la ficha');
    await act(async () => {
      clock.advance(10 * 60_000);
      await Promise.resolve();
    });
    expect(requested).toEqual([ATTACHMENT_ID]);
    fireEvent.click(screen.getByRole('button', { name: t('media.attachment.request_again') }));
    await waitFor(() => {
      expect(requested).toEqual([ATTACHMENT_ID, ATTACHMENT_ID]);
    }, WAIT);
  });

  it('not_found: «no disponible en su alcance» (BR-APP-77), sin tocar el almacén', async () => {
    server.use(variant(READ_URL, () => apiError('not_found', 404)));
    serveStore(png);
    mount();
    show();
    const alert = await screen.findByRole('alert', {}, WAIT);
    expect(alert.getAttribute('data-text-key')).toBe('errors.not_available_in_scope');
    expect(fetched).toEqual([]);
  });

  it('el almacén no entrega el objeto: «Adjunto no disponible ahora»', async () => {
    serveReadUrl();
    serveStore(() => new HttpResponse(null, { status: 403 }));
    mount();
    show();
    const alert = await screen.findByRole('alert', {}, WAIT);
    expect(alert.getAttribute('data-text-key')).toBe('media.attachment.unavailable');
    expect(requested).toEqual([ATTACHMENT_ID]);
  });

  it('axe sin violaciones, en reposo, con la foto y con un fallo', async () => {
    serveReadUrl();
    serveStore(png);
    const { container, unmount } = mount();
    expect(await axe(container)).toHaveNoViolations();
    show();
    const canvas = await screen.findByRole('img', { name: ALT }, WAIT);
    await waitFor(() => {
      expect(canvas.hidden).toBe(false);
    }, WAIT);
    expect(await axe(container)).toHaveNoViolations();
    unmount();
    server.use(variant(READ_URL, () => apiError('storage_unavailable', 503)));
    const failed = mount();
    show();
    await screen.findByRole('alert', {}, WAIT);
    expect(await axe(failed.container)).toHaveNoViolations();
  });
});

it('un JPEG sintético también se pinta', async () => {
  serveReadUrl();
  serveStore(
    () => new HttpResponse(synthetic('jpeg'), { headers: { 'Content-Type': 'image/jpeg' } }),
  );
  mount();
  show();
  const canvas = await screen.findByRole('img', { name: ALT }, WAIT);
  await waitFor(() => {
    expect(canvas.getAttribute('data-phase')).toBe('ready');
  }, WAIT);
  expect(decodeImage.mock.calls[0]?.[0].type).toBe('image/jpeg');
});
