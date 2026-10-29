/**
 * UI-12 `BlurredClipPlayer` (BR-APP-58, NFR-APP-34): `<video>` nativo sin descarga, sin imagen en
 * imagen ni reproducción remota; la URL se pide por `ports.upload` → `ports.base` solo al pulsar
 * reproducir (MSW cuenta las peticiones), se reutiliza mientras vale y se renueva al reanudar tras
 * vencer (reloj de la prueba); `storage_unavailable` degrada solo el reproductor, con «reintentar».
 * Se comparan atributos, claves del catálogo y nombres accesibles exactos.
 */
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { HttpResponse } from 'msw';
import { afterEach, beforeEach, describe, expect, it, vi, type MockInstance } from 'vitest';
import { axe } from 'vitest-axe';

import { apiError, variant } from '../msw/handlers';
import { server } from '../setup/msw';
import { FakeClock } from '../support/fakeClock';
import { BlurredClipPlayer } from '@/components/BlurredClipPlayer/BlurredClipPlayer';
import { createPortConnection, createTransport, installTransport } from '@/ports/base';
import catalog from '@/texts/media.json';

type CatalogKey = keyof typeof catalog;
const t = (key: CatalogKey): string => catalog[key];

/** Tope de espera holgado: no es lo que se prueba. */
const WAIT = { timeout: 10_000 };
const START = Date.UTC(2026, 9, 24, 12);
const EVIDENCE_ID = '0192d3e4-5f60-7a1b-8c2d-3e4f5a6b7c8d';
const READ_URL_OPERATION = 'read_url_evidence__evidence_id__read_url_post';

let clock: FakeClock;
let connection: ReturnType<typeof createPortConnection>;
let playSpy: MockInstance<HTMLMediaElement['play']>;
let pauseSpy: MockInstance<HTMLMediaElement['pause']>;
/** Peticiones de URL recibidas (ruta pedida) y URL entregadas, en orden. */
let requested: string[];
let issued: string[];

function iso(ms: number): string {
  return new Date(ms).toISOString();
}

/** Tope de peticiones de una prueba: un bucle de peticiones acaba en fallo, no cuelga el worker. */
const REQUEST_CAP = 20;

/**
 * URL prefirmada distinta en cada petición, vigente `ttlSeconds` según el reloj de la prueba
 * (negativo: ya vencida, como con el reloj del cliente adelantado respecto al servidor).
 */
function serveReadUrl(ttlSeconds = 300) {
  server.use(
    variant(READ_URL_OPERATION, ({ params }) => {
      const id = String(params.evidence_id);
      requested.push(id);
      if (requested.length > REQUEST_CAP) return apiError('storage_unavailable', 503);
      const url = `https://vigia-evidence-000000000000-us-east-1.s3.us-east-1.amazonaws.com/clip/${id}.mp4?X-Amz-Signature=${String(issued.length + 1).padStart(8, '0')}`;
      issued.push(url);
      return HttpResponse.json({
        evidence_id: id,
        url,
        expires_at: iso(clock.now() + ttlSeconds * 1000),
      });
    }),
  );
}

beforeEach(() => {
  clock = new FakeClock(START);
  requested = [];
  issued = [];
  connection = createPortConnection(clock);
  installTransport(createTransport({ clock, connection, devLog: () => undefined }));
  // Como el navegador: `play()` despacha el evento `play` en el elemento.
  playSpy = vi.spyOn(HTMLMediaElement.prototype, 'play').mockImplementation(function (
    this: HTMLMediaElement,
  ) {
    this.dispatchEvent(new Event('play'));
    return Promise.resolve();
  });
  pauseSpy = vi.spyOn(HTMLMediaElement.prototype, 'pause').mockReturnValue(undefined);
});

afterEach(() => {
  installTransport(null);
});

function renderPlayer() {
  const onReadUrlRequested = vi.fn<(id: string) => void>();
  const result = render(
    <main>
      <h1>Detalle del hallazgo</h1>
      <BlurredClipPlayer
        evidenceId={EVIDENCE_ID}
        clock={clock}
        onReadUrlRequested={onReadUrlRequested}
      />
      <p data-testid="resto">Resto de la pantalla</p>
    </main>,
  );
  const video = result.container.querySelector('video');
  if (video === null) throw new Error('sin <video>');
  return { ...result, video, onReadUrlRequested };
}

describe('atributos del reproductor (BR-APP-58)', () => {
  it('sin descarga, imagen en imagen, reproducción remota, enlace de descarga ni lista', () => {
    serveReadUrl();
    const { container, video } = renderPlayer();
    expect(video.getAttribute('controlslist')?.split(/\s+/)).toContain('nodownload');
    expect(video.hasAttribute('disablepictureinpicture')).toBe(true);
    expect(video.hasAttribute('disableremoteplayback')).toBe(true);
    // VIG-366 (S3-16): una sola forma de reproducir; sin URL no hay controles nativos, solo el botón.
    expect(video.hasAttribute('controls')).toBe(false);
    expect(screen.getAllByRole('button').map((button) => button.textContent)).toEqual([
      t('media.clip.play'),
    ]);
    expect(video.hasAttribute('download')).toBe(false);
    expect(video.hasAttribute('loop')).toBe(false);
    expect(video.hasAttribute('src')).toBe(false);
    expect(container.querySelectorAll('a, source, [download]')).toHaveLength(0);
    // El menú contextual («guardar vídeo como…») se cancela.
    expect(fireEvent.contextMenu(video)).toBe(false);
  });

  it('muestra la superposición «difuminado por construcción» y un nombre accesible', () => {
    serveReadUrl();
    const { container } = renderPlayer();
    expect(container.querySelector('[data-part="overlay"]')?.textContent).toBe(
      t('media.clip.blurred_by_construction'),
    );
    expect(screen.getByRole('figure', { name: t('media.clip.label') })).toBeDefined();
  });

  it('axe sin violaciones serias, en reposo y con el fallo del almacén', async () => {
    server.use(variant(READ_URL_OPERATION, () => apiError('storage_unavailable', 503)));
    const { container } = renderPlayer();
    const serious = (result: Awaited<ReturnType<typeof axe>>) =>
      result.violations.filter((v) => v.impact === 'serious' || v.impact === 'critical');
    expect(serious(await axe(container))).toEqual([]);
    fireEvent.click(screen.getByRole('button', { name: t('media.clip.play') }));
    await screen.findByRole('alert', {}, WAIT);
    expect(serious(await axe(container))).toEqual([]);
  });
});

describe('URL de evidencia (BR-APP-58, PR-APP-17)', () => {
  it('con la URL llegan los controles nativos y desaparece el botón «Reproducir»', async () => {
    serveReadUrl();
    const { video } = renderPlayer();
    fireEvent.click(screen.getByRole('button', { name: t('media.clip.play') }));
    await waitFor(() => {
      expect(video.hasAttribute('controls')).toBe(true);
    }, WAIT);
    expect(screen.queryByRole('button', { name: t('media.clip.play') })).toBeNull();
  });

  it('no pide la URL al montar; la pide al pulsar reproducir y la pone solo en el src', async () => {
    serveReadUrl();
    const { video, container, onReadUrlRequested } = renderPlayer();
    await act(async () => {
      await Promise.resolve();
    });
    expect(requested).toEqual([]);

    fireEvent.click(screen.getByRole('button', { name: t('media.clip.play') }));
    await waitFor(() => {
      expect(video.getAttribute('src')).toBe(issued[0]);
    }, WAIT);
    expect(requested).toEqual([EVIDENCE_ID]);
    expect(onReadUrlRequested.mock.calls).toEqual([[EVIDENCE_ID]]);
    expect(playSpy).toHaveBeenCalled();
    // La URL no aparece como texto ni en otro atributo.
    const url = issued[0] ?? '';
    expect(container.textContent.includes(url)).toBe(false);
    expect(
      [...container.querySelectorAll('*')].filter((element) =>
        [...element.attributes].some((attribute) => attribute.value === url),
      ),
    ).toEqual([video]);
  });

  it('reanudar antes de vencer no pide otra; reanudar tras vencer pide una y sigue en la misma posición', async () => {
    serveReadUrl(300);
    const { video } = renderPlayer();
    fireEvent.click(screen.getByRole('button', { name: t('media.clip.play') }));
    await waitFor(() => {
      expect(video.getAttribute('src')).toBe(issued[0]);
    }, WAIT);

    clock.advance(299_999);
    fireEvent.pause(video);
    fireEvent.play(video);
    await act(async () => {
      await Promise.resolve();
    });
    expect(requested).toHaveLength(1);

    video.currentTime = 42;
    clock.advance(1);
    fireEvent.play(video);
    await waitFor(() => {
      expect(video.getAttribute('src')).toBe(issued[1]);
    }, WAIT);
    expect(requested).toEqual([EVIDENCE_ID, EVIDENCE_ID]);
    expect(video.currentTime).toBe(42);
    expect(pauseSpy).toHaveBeenCalled();
  });

  it('acota la vigencia a 5 minutos aunque expires_at sea posterior', async () => {
    serveReadUrl(3_600);
    const { video } = renderPlayer();
    fireEvent.click(screen.getByRole('button', { name: t('media.clip.play') }));
    await waitFor(() => {
      expect(video.getAttribute('src')).toBe(issued[0]);
    }, WAIT);
    clock.advance(300_000);
    fireEvent.play(video);
    await waitFor(() => {
      expect(requested).toHaveLength(2);
    }, WAIT);
  });

  it('una URL que llega ya vencida (reloj local adelantado 10 min) hace una sola petición por pulsación', async () => {
    serveReadUrl(-600);
    const { video } = renderPlayer();
    fireEvent.click(screen.getByRole('button', { name: t('media.clip.play') }));
    await waitFor(() => {
      expect(video.getAttribute('src')).toBe(issued[0]);
    }, WAIT);
    await act(async () => {
      for (let round = 0; round < 8; round += 1) await Promise.resolve();
    });
    expect(requested).toHaveLength(1);
    expect(playSpy).toHaveBeenCalledTimes(1);

    // Se cuenta un plazo local de 5 minutos: reanudar dentro no pide; tras vencer, una sola.
    clock.advance(299_999);
    fireEvent.play(video);
    await act(async () => {
      await Promise.resolve();
    });
    expect(requested).toHaveLength(1);
    clock.advance(1);
    fireEvent.play(video);
    await waitFor(() => {
      expect(video.getAttribute('src')).toBe(issued[1]);
    }, WAIT);
    await act(async () => {
      for (let round = 0; round < 8; round += 1) await Promise.resolve();
    });
    expect(requested).toHaveLength(2);
  });

  it('el play que lanza el reproductor no pide otra URL aunque la URL venza antes del evento', async () => {
    serveReadUrl(1);
    // El evento `play` llega 2 s después de recibir una URL de 1 s.
    playSpy.mockImplementation(function (this: HTMLMediaElement) {
      clock.advance(2_000);
      this.dispatchEvent(new Event('play'));
      return Promise.resolve();
    });
    const { video } = renderPlayer();
    fireEvent.click(screen.getByRole('button', { name: t('media.clip.play') }));
    await waitFor(() => {
      expect(video.getAttribute('src')).toBe(issued[0]);
    }, WAIT);
    await act(async () => {
      for (let round = 0; round < 8; round += 1) await Promise.resolve();
    });
    expect(requested).toHaveLength(1);
    expect(pauseSpy).not.toHaveBeenCalled();

    // Un gesto posterior con la URL vencida sí pide una (y solo una).
    fireEvent.play(video);
    await waitFor(() => {
      expect(video.getAttribute('src')).toBe(issued[1]);
    }, WAIT);
    await act(async () => {
      for (let round = 0; round < 8; round += 1) await Promise.resolve();
    });
    expect(requested).toHaveLength(2);
  });

  it('dos pulsaciones seguidas mientras se pide la URL hacen una sola petición', async () => {
    serveReadUrl();
    const { video } = renderPlayer();
    fireEvent.play(video);
    fireEvent.play(video);
    fireEvent.click(screen.getByRole('button', { name: t('media.clip.loading') }));
    await waitFor(() => {
      expect(video.getAttribute('src')).toBe(issued[0]);
    }, WAIT);
    expect(requested).toHaveLength(1);
  });

  it('al desmontar quita la URL del elemento; una nueva apertura pide otra', async () => {
    serveReadUrl();
    const first = renderPlayer();
    fireEvent.click(screen.getByRole('button', { name: t('media.clip.play') }));
    await waitFor(() => {
      expect(first.video.getAttribute('src')).toBe(issued[0]);
    }, WAIT);
    first.unmount();
    expect(first.video.hasAttribute('src')).toBe(false);

    const second = renderPlayer();
    fireEvent.click(screen.getByRole('button', { name: t('media.clip.play') }));
    await waitFor(() => {
      expect(second.video.getAttribute('src')).toBe(issued[1]);
    }, WAIT);
    expect(requested).toHaveLength(2);
  });
});

describe('almacén no disponible (BR-APP-58, BR-APP-74)', () => {
  it('storage_unavailable → «evidencia no disponible ahora» con reintentar, sin afectar al resto', async () => {
    let fail = true;
    server.use(
      variant(READ_URL_OPERATION, ({ params }) => {
        requested.push(String(params.evidence_id));
        if (fail) return apiError('storage_unavailable', 503, { retryAfterSeconds: 5 });
        const url = 'https://vigia-evidence-000000000000-us-east-1.s3.amazonaws.com/c.mp4?s=1';
        issued.push(url);
        return HttpResponse.json({
          evidence_id: String(params.evidence_id),
          url,
          expires_at: iso(clock.now() + 300_000),
        });
      }),
    );
    const { video } = renderPlayer();
    fireEvent.click(screen.getByRole('button', { name: t('media.clip.play') }));
    const alert = await screen.findByRole('alert', {}, WAIT);
    expect(alert.dataset.textKey).toBe('media.clip.unavailable');
    expect(alert.querySelector('p')?.textContent).toBe(t('media.clip.unavailable'));
    expect(screen.getByTestId('resto').textContent).toBe('Resto de la pantalla');
    expect(screen.getByRole('heading', { name: 'Detalle del hallazgo' })).toBeDefined();
    // El fallo cuenta en el circuito del almacén de evidencias, nunca en el de la plataforma.
    expect(connection.getSnapshot().circuits.evidence_store.failures).toHaveLength(1);
    expect(connection.getSnapshot().circuits.platform.failures).toEqual([]);
    expect(video.hasAttribute('src')).toBe(false);

    fail = false;
    fireEvent.click(screen.getByRole('button', { name: t('media.clip.retry') }));
    await waitFor(() => {
      expect(video.getAttribute('src')).toBe(issued[0]);
    }, WAIT);
    expect(requested).toHaveLength(2);
    expect(screen.queryByRole('alert')).toBeNull();
  });

  it('otro error muestra el mensaje del mapa cerrado (not_found → fuera de alcance)', async () => {
    server.use(variant(READ_URL_OPERATION, () => apiError('not_found', 404)));
    renderPlayer();
    fireEvent.click(screen.getByRole('button', { name: t('media.clip.play') }));
    const alert = await screen.findByRole('alert', {}, WAIT);
    expect(alert.dataset.textKey).toBe('errors.not_available_in_scope');
  });

  it('la URL nunca pasa por la red de la aplicación más que en la respuesta de read-url', async () => {
    serveReadUrl();
    const seen: string[] = [];
    server.events.on('request:start', ({ request }) => {
      seen.push(`${request.method} ${new URL(request.url).pathname}`);
    });
    const { video } = renderPlayer();
    fireEvent.click(screen.getByRole('button', { name: t('media.clip.play') }));
    await waitFor(() => {
      expect(video.getAttribute('src')).toBe(issued[0]);
    }, WAIT);
    server.events.removeAllListeners();
    expect(seen).toEqual([`POST /evidence/${EVIDENCE_ID}/read-url`]);
  });
});
