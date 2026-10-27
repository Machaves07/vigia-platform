/**
 * UI-21 `DirectUploadField` y `ports.upload` (BR-APP-48, PAT-APP-SEG-02, LC-APP-17), con MSW
 * registrando cada destino: el SHA-256 se calcula con `crypto.subtle` (comparado con el valor
 * conocido de un archivo sintético), la concesión solo lleva metadatos, el `PUT` va únicamente al
 * origen concedido con la cabecera de suma exacta, la casilla de U-04 es obligatoria antes de la
 * concesión y no aparece para U-03, los rechazos `attachment_*` se anclan al campo y el archivo se
 * conserva ante un fallo del almacén. Incluye la lectura de adjuntos de cierre por navegación en
 * pestaña nueva (SUP-U05-10, nota U05-H-12). Se comparan códigos, atributos y claves del catálogo.
 */
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { axe } from 'vitest-axe';

import { apiError, variant } from '../msw/handlers';
import { server } from '../setup/msw';
import { FakeClock } from '../support/fakeClock';
import { AttachmentReadLink } from '@/components/DirectUploadField/AttachmentReadLink';
import {
  DirectUploadField,
  type UploadFailure,
  type UploadResult,
  type UploadTarget,
} from '@/components/DirectUploadField/DirectUploadField';
import type { Notice } from '@/components/NoticeStack/notice';
import { createPortConnection, createTransport, installTransport } from '@/ports/base';
import catalog from '@/texts/media.json';

type CatalogKey = keyof typeof catalog;
const t = (key: CatalogKey): string => catalog[key];

/** Tope de espera holgado: no es lo que se prueba. */
const WAIT = { timeout: 10_000 };
const START = Date.UTC(2026, 9, 24, 12);
const PLANT_ID = '0192d3e4-5f60-7a1b-8c2d-000000000001';
const ACTION_ID = '0192d3e4-5f60-7a1b-8c2d-000000000002';
const DOCUMENT_ID = '0192d3e4-5f60-7a1b-8c2d-000000000003';
const ATTACHMENT_ID = '0192d3e4-5f60-7a1b-8c2d-000000000004';

/** Archivo sintético «abc»: SHA-256 conocido (FIPS 180-2, ejemplo B.1). */
const ABC_SHA256_HEX = 'ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad';
const ABC_SHA256_B64 = 'ungWv48Bz+pBQUDeXa4iI7ADYaOWF3qctBD/YfIAFa0=';

const STORE = 'https://vigia-evidence-000000000000-us-east-1.s3.us-east-1.amazonaws.com';
const DOCUMENT_KEY = `org/o/plant/${PLANT_ID}/documents/${DOCUMENT_ID}.png`;
const ATTACHMENT_KEY = `org/o/plant/p/zone/z/closure/s/${ATTACHMENT_ID}.png`;

const CORS = {
  'access-control-allow-origin': '*',
  'access-control-allow-methods': 'PUT',
  'access-control-allow-headers': 'content-type, x-amz-checksum-sha256',
};

interface Seen {
  readonly method: string;
  readonly origin: string;
  readonly path: string;
}
/** Todo destino de red de la prueba, en orden (PR-APP-28). */
let seen: Seen[];
/** Cuerpos de las concesiones recibidas. */
let grants: unknown[];
/** `PUT` recibidos por el depósito: cabeceras y bytes. */
let puts: { headers: Record<string, string>; bytes: number[] }[];

function abcFile(name = 'captura.png', type = 'image/png'): File {
  return new File(['abc'], name, { type });
}

function expiresAt(): string {
  return new Date(START + 15 * 60_000).toISOString();
}

/** El depósito simulado: preflight CORS y `PUT` que guarda cabeceras y bytes. */
function serveStore(key: string, status = 200) {
  server.use(
    http.options(`${STORE}/${key}`, () => new HttpResponse(null, { status: 200, headers: CORS })),
    http.put(`${STORE}/${key}`, async ({ request }) => {
      puts.push({
        headers: Object.fromEntries(request.headers.entries()),
        bytes: [...new Uint8Array(await request.arrayBuffer())],
      });
      return new HttpResponse(null, { status, headers: CORS });
    }),
  );
}

function documentGrant(headers?: Record<string, string>) {
  server.use(
    variant('grant_document_documents_post', async ({ request }) => {
      grants.push(await request.json());
      return HttpResponse.json(
        {
          document_id: DOCUMENT_ID,
          upload: {
            method: 'PUT',
            url: `${STORE}/${DOCUMENT_KEY}?X-Amz-Signature=0001`,
            required_headers: headers ?? {
              'content-type': 'image/png',
              'x-amz-checksum-sha256': ABC_SHA256_B64,
            },
            expires_at: expiresAt(),
          },
          document_ref: {
            document_id: DOCUMENT_ID,
            storage_key: DOCUMENT_KEY,
            sha256: ABC_SHA256_HEX,
            content_type: 'image/png',
            size_bytes: 3,
          },
        },
        { status: 201 },
      );
    }),
  );
}

function attachmentGrant(url = `${STORE}/${ATTACHMENT_KEY}?X-Amz-Signature=0002`) {
  server.use(
    variant(
      'upload_grant_loop_actions__action_id__attachments_upload_grant_post',
      async ({ request }) => {
        grants.push(await request.json());
        return HttpResponse.json(
          {
            attachment_id: ATTACHMENT_ID,
            storage_key: ATTACHMENT_KEY,
            upload: {
              method: 'PUT',
              url,
              required_headers: {
                'content-type': 'image/png',
                'x-amz-checksum-sha256': ABC_SHA256_B64,
              },
              expires_at: expiresAt(),
            },
          },
          { status: 201 },
        );
      },
    ),
  );
}

const DOCUMENT_TARGET: UploadTarget = {
  unit: 'catalog_document',
  plantId: PLANT_ID,
  kind: 'blur_check_capture',
};
const ATTACHMENT_TARGET: UploadTarget = { unit: 'loop_attachment', actionId: ACTION_ID };

beforeEach(() => {
  seen = [];
  grants = [];
  puts = [];
  const clock = new FakeClock(START);
  installTransport(
    createTransport({ clock, connection: createPortConnection(clock), devLog: () => undefined }),
  );
  server.events.on('request:start', ({ request }) => {
    const url = new URL(request.url);
    seen.push({ method: request.method, origin: url.origin, path: url.pathname });
  });
});

afterEach(() => {
  installTransport(null);
  server.events.removeAllListeners();
});

function renderField(target: UploadTarget, notice?: Notice) {
  const onUploaded = vi.fn<(result: UploadResult) => void>();
  const onFailure = vi.fn<(failure: UploadFailure) => void>();
  const result = render(
    <main>
      <DirectUploadField
        label="Captura del difuminado"
        target={target}
        onUploaded={onUploaded}
        onFailure={onFailure}
        notice={notice}
      />
    </main>,
  );
  const input = screen.getByLabelText<HTMLInputElement>('Captura del difuminado');
  return { ...result, input, onUploaded, onFailure };
}

function fieldError(): HTMLElement {
  return screen.getByRole('alert');
}

const user = () => userEvent.setup({ applyAccept: false });

describe('subida de un documento de U-03 (POST /documents)', () => {
  it('SHA-256 conocido, cabecera de suma exacta y ningún destino fuera del concedido', async () => {
    documentGrant();
    serveStore(DOCUMENT_KEY);
    const { input, onUploaded } = renderField(DOCUMENT_TARGET);
    await user().upload(input, abcFile());
    await user().click(screen.getByRole('button', { name: t('media.upload.submit') }));
    await waitFor(() => {
      expect(onUploaded).toHaveBeenCalledTimes(1);
    }, WAIT);

    // La concesión solo lleva metadatos: nunca los bytes del archivo.
    expect(grants).toEqual([
      {
        plant_id: PLANT_ID,
        kind: 'blur_check_capture',
        content_type: 'image/png',
        size_bytes: 3,
        sha256: ABC_SHA256_HEX,
      },
    ]);
    expect(puts).toHaveLength(1);
    expect(puts[0]?.headers['x-amz-checksum-sha256']).toBe(ABC_SHA256_B64);
    expect(puts[0]?.headers['content-type']).toBe('image/png');
    expect(puts[0]?.bytes).toEqual([0x61, 0x62, 0x63]);
    // Destinos: la API en el origen propio y el archivo solo al origen concedido.
    expect(seen.filter((entry) => entry.method !== 'OPTIONS')).toEqual([
      { method: 'POST', origin: window.location.origin, path: '/documents' },
      { method: 'PUT', origin: STORE, path: `/${DOCUMENT_KEY}` },
    ]);
    expect(seen.every((entry) => [window.location.origin, STORE].includes(entry.origin))).toBe(
      true,
    );
    expect(onUploaded.mock.calls[0]?.[0]).toEqual({
      unit: 'catalog_document',
      document_ref: {
        document_id: DOCUMENT_ID,
        storage_key: DOCUMENT_KEY,
        sha256: ABC_SHA256_HEX,
        content_type: 'image/png',
        size_bytes: 3,
      },
    });
    // El resultado no lleva la URL prefirmada.
    expect(JSON.stringify(onUploaded.mock.calls).includes('X-Amz-Signature')).toBe(false);
    expect(screen.getByRole('status').dataset.phase).toBe('done');
  });

  it('para un documento de U-03 la casilla de declaración no aparece', () => {
    const { container } = renderField(DOCUMENT_TARGET);
    expect(screen.queryByRole('checkbox')).toBeNull();
    expect(container.querySelectorAll('input[type="checkbox"]')).toHaveLength(0);
  });

  it('una cabecera de suma de la concesión distinta a la del archivo no sube nada', async () => {
    documentGrant({ 'content-type': 'image/png', 'x-amz-checksum-sha256': 'AAAA' });
    serveStore(DOCUMENT_KEY);
    const { input, onFailure } = renderField(DOCUMENT_TARGET);
    await user().upload(input, abcFile());
    await user().click(screen.getByRole('button', { name: t('media.upload.submit') }));
    await waitFor(() => {
      expect(onFailure).toHaveBeenCalledTimes(1);
    }, WAIT);
    expect(fieldError().dataset.code).toBe('attachment_hash_mismatch');
    expect(puts).toEqual([]);
  });

  it('tipo y tamaño fuera de los de la unidad se rechazan sin pedir la concesión', async () => {
    const { input } = renderField(DOCUMENT_TARGET);
    await user().upload(input, abcFile('notas.txt', 'text/plain'));
    await user().click(screen.getByRole('button', { name: t('media.upload.submit') }));
    await waitFor(() => {
      expect(fieldError().dataset.code).toBe('attachment_type_not_allowed');
    }, WAIT);
    const big = new File([new Uint8Array(20 * 1024 * 1024 + 1)], 'acta.pdf', {
      type: 'application/pdf',
    });
    await user().upload(input, big);
    await user().click(screen.getByRole('button', { name: t('media.upload.submit') }));
    await waitFor(() => {
      expect(fieldError().dataset.code).toBe('attachment_too_large');
    }, WAIT);
    expect(fieldError().querySelector('p')?.textContent).toBe(
      t('media.upload.attachment_too_large'),
    );
    expect(seen).toEqual([]);
  });

  it('un archivo de exactamente 20 MB pide la concesión', async () => {
    documentGrant();
    const { input } = renderField(DOCUMENT_TARGET);
    const limit = new File([new Uint8Array(20 * 1024 * 1024)], 'acta.pdf', {
      type: 'application/pdf',
    });
    await user().upload(input, limit);
    await user().click(screen.getByRole('button', { name: t('media.upload.submit') }));
    await waitFor(() => {
      expect(grants).toHaveLength(1);
    }, WAIT);
    expect((grants[0] as { size_bytes: number }).size_bytes).toBe(20 * 1024 * 1024);
  });
});

describe('adjunto de cierre de U-04 (SUP-U05-09)', () => {
  it('sin la casilla marcada no se pide la concesión (MSW cuenta cero)', async () => {
    attachmentGrant();
    serveStore(ATTACHMENT_KEY);
    const { input, onFailure } = renderField(ATTACHMENT_TARGET);
    const box = screen.getByRole('checkbox', { name: t('media.upload.declaration') });
    expect((box as HTMLInputElement).checked).toBe(false);
    await user().upload(input, abcFile());
    await user().click(screen.getByRole('button', { name: t('media.upload.submit') }));
    await waitFor(() => {
      expect(onFailure).toHaveBeenCalledTimes(1);
    }, WAIT);
    expect(fieldError().dataset.code).toBe('attachment_declaration_missing');
    expect(input.getAttribute('aria-invalid')).toBe('true');
    expect(input.getAttribute('aria-describedby')?.split(' ')).toContain(fieldError().id);
    expect(grants).toEqual([]);
    expect(seen).toEqual([]);
  });

  it('con la casilla, concesión con metadatos y resultado con la declaración', async () => {
    attachmentGrant();
    serveStore(ATTACHMENT_KEY);
    const { input, onUploaded } = renderField(ATTACHMENT_TARGET);
    await user().upload(input, abcFile());
    await user().click(screen.getByRole('checkbox', { name: t('media.upload.declaration') }));
    await user().click(screen.getByRole('button', { name: t('media.upload.submit') }));
    await waitFor(() => {
      expect(onUploaded).toHaveBeenCalledTimes(1);
    }, WAIT);
    expect(grants).toEqual([{ content_type: 'image/png', size_bytes: 3, sha256: ABC_SHA256_HEX }]);
    expect(seen.filter((entry) => entry.method !== 'OPTIONS')).toEqual([
      {
        method: 'POST',
        origin: window.location.origin,
        path: `/loop/actions/${ACTION_ID}/attachments/upload-grant`,
      },
      { method: 'PUT', origin: STORE, path: `/${ATTACHMENT_KEY}` },
    ]);
    expect(puts[0]?.headers['x-amz-checksum-sha256']).toBe(ABC_SHA256_B64);
    expect(onUploaded.mock.calls[0]?.[0]).toEqual({
      unit: 'loop_attachment',
      attachment: {
        attachment_id: ATTACHMENT_ID,
        content_type: 'image/png',
        size_bytes: 3,
        sha256: ABC_SHA256_HEX,
        filename: 'captura.png',
        no_identifiable_person_declared: true,
      },
      storage_key: ATTACHMENT_KEY,
    });
  });

  it('elegir otro archivo desmarca la declaración', async () => {
    const { input } = renderField(ATTACHMENT_TARGET);
    await user().upload(input, abcFile());
    const box = screen.getByRole<HTMLInputElement>('checkbox', {
      name: t('media.upload.declaration'),
    });
    await user().click(box);
    expect(box.checked).toBe(true);
    await user().upload(input, abcFile('otra.png'));
    expect(box.checked).toBe(false);
  });

  it('un archivo de más de 10 MB se rechaza en local; uno de 10 MB exactos pide la concesión', async () => {
    attachmentGrant();
    const { input } = renderField(ATTACHMENT_TARGET);
    await user().upload(
      input,
      new File([new Uint8Array(10 * 1024 * 1024 + 1)], 'foto.jpg', { type: 'image/jpeg' }),
    );
    await user().click(screen.getByRole('checkbox', { name: t('media.upload.declaration') }));
    await user().click(screen.getByRole('button', { name: t('media.upload.submit') }));
    await waitFor(() => {
      expect(fieldError().dataset.code).toBe('attachment_too_large');
    }, WAIT);
    expect(grants).toEqual([]);
    await user().upload(
      input,
      new File([new Uint8Array(10 * 1024 * 1024)], 'foto.jpg', { type: 'image/jpeg' }),
    );
    await user().click(screen.getByRole('checkbox', { name: t('media.upload.declaration') }));
    await user().click(screen.getByRole('button', { name: t('media.upload.submit') }));
    await waitFor(() => {
      expect(grants).toHaveLength(1);
    }, WAIT);
  });

  it.each([
    ['loop_attachment_type_not_allowed', 'attachment_type_not_allowed'],
    ['loop_attachment_too_large', 'attachment_too_large'],
  ])('el rechazo %s de la concesión se ancla al campo como %s', async (detail, code) => {
    server.use(
      variant('upload_grant_loop_actions__action_id__attachments_upload_grant_post', () =>
        apiError('invalid_request', 400, { detailCode: detail }),
      ),
    );
    const { input, onFailure } = renderField(ATTACHMENT_TARGET);
    await user().upload(input, abcFile());
    await user().click(screen.getByRole('checkbox', { name: t('media.upload.declaration') }));
    await user().click(screen.getByRole('button', { name: t('media.upload.submit') }));
    await waitFor(() => {
      expect(onFailure).toHaveBeenCalledTimes(1);
    }, WAIT);
    expect(onFailure.mock.calls[0]?.[0]).toEqual({ kind: 'attachment', code });
    expect(fieldError().dataset.code).toBe(code);
    expect(fieldError().dataset.textKey).toBe(`media.upload.${code}`);
  });

  it.each([
    'loop_attachment_hash_mismatch',
    'loop_attachment_declaration_missing',
    'loop_attachment_metadata_not_allowed',
  ])('el aviso %s que la pantalla recibe al registrar se ancla al campo', (detail) => {
    const notice: Notice = {
      id: 'evidence',
      field: 'attachments',
      error: { code: 'invalid_request', status: 400, detail_code: detail },
    };
    const { input } = renderField(ATTACHMENT_TARGET, notice);
    const code = detail.slice('loop_'.length);
    expect(fieldError().dataset.code).toBe(code);
    expect(fieldError().dataset.textKey).toBe(`media.upload.${code}`);
    expect(input.getAttribute('aria-invalid')).toBe('true');
    expect(input.getAttribute('aria-describedby')?.split(' ')).toContain(fieldError().id);
  });

  it('attachment_metadata_not_allowed muestra «la imagen contiene datos de posición…»', () => {
    renderField(ATTACHMENT_TARGET, {
      id: 'evidence',
      field: 'attachments',
      error: {
        code: 'invalid_request',
        status: 400,
        detail_code: 'loop_attachment_metadata_not_allowed',
      },
    });
    expect(fieldError().querySelector('p')?.textContent).toBe(
      'La imagen contiene datos de posición; quítelos y vuelva a subirla.',
    );
  });

  it('un aviso ajeno al adjunto se presenta con FieldNotice del mapa cerrado', () => {
    renderField(ATTACHMENT_TARGET, {
      id: 'evidence',
      field: 'attachments',
      error: { code: 'conflict', status: 409, detail_code: null },
    });
    expect(fieldError().dataset.textKey).toBe('errors.anchored');
  });
});

describe('fallos del almacén: el archivo se conserva (BR-APP-48)', () => {
  it('un PUT fallido conserva el archivo y «reintentar» sube sin volver a elegirlo', async () => {
    attachmentGrant();
    serveStore(ATTACHMENT_KEY, 500);
    const { input, onUploaded } = renderField(ATTACHMENT_TARGET);
    await user().upload(input, abcFile());
    await user().click(screen.getByRole('checkbox', { name: t('media.upload.declaration') }));
    await user().click(screen.getByRole('button', { name: t('media.upload.submit') }));
    await waitFor(() => {
      expect(fieldError().dataset.code).toBe('store');
    }, WAIT);
    expect([...fieldError().querySelectorAll('p')].map((p) => p.textContent)).toEqual([
      t('media.upload.store_failed'),
      t('media.upload.kept'),
    ]);
    expect(input.files?.[0]?.name).toBe('captura.png');

    serveStore(ATTACHMENT_KEY, 200);
    await user().click(screen.getByRole('button', { name: t('media.upload.retry') }));
    await waitFor(() => {
      expect(onUploaded).toHaveBeenCalledTimes(1);
    }, WAIT);
    expect(grants).toHaveLength(2);
    expect(puts).toHaveLength(2);
  });

  it('storage_unavailable en la concesión conserva el archivo y expone el comportamiento', async () => {
    server.use(
      variant('grant_document_documents_post', () =>
        apiError('storage_unavailable', 503, { retryAfterSeconds: 5 }),
      ),
    );
    const { input, onFailure } = renderField(DOCUMENT_TARGET);
    await user().upload(input, abcFile());
    await user().click(screen.getByRole('button', { name: t('media.upload.submit') }));
    await waitFor(() => {
      expect(onFailure).toHaveBeenCalledTimes(1);
    }, WAIT);
    const failure = onFailure.mock.calls[0]?.[0];
    expect(failure?.kind === 'port' ? failure.behaviour?.kind : null).toBe('degraded_section');
    expect(fieldError().dataset.code).toBe('storage_unavailable');
    expect(fieldError().dataset.textKey).toBe('errors.storage_unavailable');
    expect(screen.getByRole('button', { name: t('media.upload.retry') })).toBeDefined();
  });

  it('una URL concedida que no es https (ftp:) no recibe el archivo', async () => {
    attachmentGrant('ftp://vigia-evidence.example/adjunto.png');
    const { input } = renderField(ATTACHMENT_TARGET);
    await user().upload(input, abcFile());
    await user().click(screen.getByRole('checkbox', { name: t('media.upload.declaration') }));
    await user().click(screen.getByRole('button', { name: t('media.upload.submit') }));
    await waitFor(() => {
      expect(fieldError().dataset.code).toBe('store');
    }, WAIT);
    expect(seen.map((entry) => entry.method)).toEqual(['POST']);
  });

  it('axe sin violaciones serias con la casilla y con un error anclado', async () => {
    const { container, input } = renderField(ATTACHMENT_TARGET);
    const serious = (result: Awaited<ReturnType<typeof axe>>) =>
      result.violations.filter((v) => v.impact === 'serious' || v.impact === 'critical');
    expect(serious(await axe(container))).toEqual([]);
    await user().upload(input, abcFile());
    await user().click(screen.getByRole('button', { name: t('media.upload.submit') }));
    await screen.findByRole('alert', {}, WAIT);
    expect(serious(await axe(container))).toEqual([]);
  });
});

describe('lectura de adjuntos de cierre (SUP-U05-10, U05-H-12)', () => {
  const READ = 'read_url_loop_attachments__attachment_id__read_url_post';
  const URL_1 = `${STORE}/${ATTACHMENT_KEY}?X-Amz-Signature=read1`;

  function serveRead(ttlMs: number, clock: FakeClock) {
    const calls: string[] = [];
    server.use(
      variant(READ, ({ params }) => {
        calls.push(String(params.attachment_id));
        return HttpResponse.json({
          attachment_id: String(params.attachment_id),
          url: URL_1,
          expires_at: new Date(clock.now() + ttlMs).toISOString(),
          content_disposition: 'attachment',
        });
      }),
    );
    return calls;
  }

  it('enlace a pestaña nueva sin opener ni referente, sin incrustar; se retira al pulsarlo', async () => {
    const clock = new FakeClock(START);
    const calls = serveRead(15 * 60_000, clock);
    const { container } = render(<AttachmentReadLink attachmentId={ATTACHMENT_ID} clock={clock} />);
    expect(calls).toEqual([]);
    fireEvent.click(screen.getByRole('button', { name: t('media.attachment.prepare') }));
    const link = await screen.findByRole('link', { name: t('media.attachment.open') }, WAIT);
    expect(calls).toEqual([ATTACHMENT_ID]);
    expect(link.getAttribute('href')).toBe(URL_1);
    expect(link.getAttribute('target')).toBe('_blank');
    expect(link.getAttribute('rel')?.split(' ').sort()).toEqual(['noopener', 'noreferrer']);
    expect(container.querySelectorAll('img, iframe, embed, object, video, picture')).toHaveLength(
      0,
    );
    expect(container.textContent.includes(URL_1)).toBe(false);
    fireEvent.click(link);
    expect(screen.queryByRole('link')).toBeNull();
    expect(container.innerHTML.includes('X-Amz-Signature')).toBe(false);
  });

  it('el enlace se retira al vencer (el menor entre expires_at y 15 minutos)', async () => {
    const clock = new FakeClock(START);
    serveRead(60 * 60_000, clock);
    render(<AttachmentReadLink attachmentId={ATTACHMENT_ID} clock={clock} />);
    fireEvent.click(screen.getByRole('button', { name: t('media.attachment.prepare') }));
    await screen.findByRole('link', {}, WAIT);
    // El enlace llega al DOM en el compromiso; el vencimiento lo programa un `useEffect` que React
    // ejecuta después. Bajo carga, avanzar el reloj antes deja el enlace vivo (VIG-362).
    await waitFor(() => {
      expect(clock.pendingCount).toBe(1);
    }, WAIT);
    act(() => {
      clock.advance(15 * 60_000 - 1);
    });
    expect(screen.queryByRole('link')).not.toBeNull();
    act(() => {
      clock.advance(1);
    });
    expect(screen.queryByRole('link')).toBeNull();
  });

  it('storage_unavailable → «adjunto no disponible ahora»', async () => {
    server.use(variant(READ, () => apiError('storage_unavailable', 503)));
    const clock = new FakeClock(START);
    render(<AttachmentReadLink attachmentId={ATTACHMENT_ID} clock={clock} />);
    fireEvent.click(screen.getByRole('button', { name: t('media.attachment.prepare') }));
    const alert = await screen.findByRole('alert', {}, WAIT);
    expect(alert.dataset.textKey).toBe('media.attachment.unavailable');
  });
});
