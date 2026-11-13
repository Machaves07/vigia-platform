/**
 * Episodios de la sombra en SCR-06 (TASK-574; A-112, QF10; BR-APP-58, 77): listado y detalle con
 * MSW, la imagen **sintética** y la concesión del transporte.
 *
 * - Listado: metadatos con las etiquetas del contrato y «sin foto» con su motivo; ninguna imagen.
 * - Detalle: una sola petición de imagen con `X-Vigia-Concession`, ninguna si el episodio llegó
 *   sin foto, y ningún reintento ante un fallo transitorio (aviso UI-19).
 * - `not_found` del listado o de la imagen (zona en `productive`, foto vencida o purgada, fuera de
 *   alcance) → «no disponible en su alcance».
 * - Aviso y superposición por clave de texto; axe sin violaciones en las dos vistas.
 */
import { act, fireEvent, render, screen, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { StrictMode, type ReactElement } from 'react';
import { MemoryRouter, Route, Routes } from 'react-router';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { axe } from 'vitest-axe';

import { EpisodeDetailScreen } from '@/pages/scr06/episodes/EpisodeDetailScreen';
import { EpisodesScreen } from '@/pages/scr06/episodes/EpisodesScreen';
import EpisodesPage from '@/pages/scr06/episodes';
import { canOpenEpisodes } from '@/pages/scr06/session/EpisodesLink';
import {
  CONCESSION_HEADER,
  createPortConnection,
  createTransport,
  installTransport,
} from '@/ports/base';
import type { EpisodePhoto } from '@/ports/scr06EpisodePhotos';
import { label } from '@/texts/labels';
import { text } from '@/texts';

import { apiError, mswPath } from '../../msw/handlers';
import { server } from '../../msw/server';
import { FakeClock } from '../../support/fakeClock';

const WAIT = { timeout: 10_000 };
const TEST_TIMEOUT = 30_000;
const ZONE = '0190a1b2-0000-7000-8000-000000000301';
const WITH_PHOTO = '0190a1b2-0000-7000-8000-000000000302';
const WITHOUT_PHOTO = '0190a1b2-0000-7000-8000-000000000303';
const CAMERA = '0190a1b2-0000-7000-8000-000000000304';
const CONCESSION = '0190a1b2-0000-7000-8000-000000000305';
const NOW = Date.parse('2026-11-12T15:00:00.000Z');

function episode(overrides: Partial<EpisodePhoto> = {}): EpisodePhoto {
  return {
    photo_id: WITH_PHOTO,
    zone_id: ZONE,
    node_id: '0190a1b2-0000-7000-8000-000000000306',
    episode_id: '0190a1b2-0000-7000-8000-000000000307',
    segment_index: 1,
    classified_as: 'finding',
    family: 'dwell',
    standard: { standard_id: '0190a1b2-0000-7000-8000-000000000308', version: 3 },
    max_confidence: 0.91,
    started_at: '2026-11-12T13:40:02.000Z',
    ended_at: '2026-11-12T13:40:06.000Z',
    received_at: '2026-11-12T13:40:07.100Z',
    expires_at: '2026-12-12T13:40:07.100Z',
    camera_id: CAMERA,
    frame_at: '2026-11-12T13:40:04.500Z',
    image: { width: 64, height: 48, size_bytes: 120, blur_params_version: 'blur-v1' },
    image_absent_reason: null,
    ...overrides,
  };
}

const withoutPhoto = (): EpisodePhoto =>
  episode({
    photo_id: WITHOUT_PHOTO,
    classified_as: 'detection_for_review',
    family: 'coexistence',
    camera_id: null,
    frame_at: null,
    image: null,
    image_absent_reason: 'disk_pressure',
  });

/** JPEG sintético mínimo (SOI + EOI): nunca una foto real (P3). */
function syntheticJpeg(): Uint8Array {
  return new Uint8Array([0xff, 0xd8, 0xff, 0xd9]);
}

interface Traffic {
  readonly list: { after: string | null; limit: string | null }[];
  readonly image: { photo: string; concession: string | null }[];
}

function serve(options: {
  readonly photos?: readonly EpisodePhoto[];
  readonly next?: string | null;
  readonly list?: () => Response;
  readonly image?: () => Response;
}): Traffic {
  const traffic: Traffic = { list: [], image: [] };
  server.use(
    http.get(mswPath('/zones/{zone_id}/episode-photos'), ({ request }) => {
      const params = new URL(request.url).searchParams;
      traffic.list.push({ after: params.get('after'), limit: params.get('limit') });
      if (options.list !== undefined) return options.list();
      // La página siguiente (con `after`) trae el episodio sin foto y es la última.
      if (params.get('after') !== null) {
        return HttpResponse.json({ photos: [withoutPhoto()], next_after: null });
      }
      return HttpResponse.json({
        photos: [...(options.photos ?? [episode(), withoutPhoto()])],
        next_after: options.next ?? null,
      });
    }),
    http.get(mswPath('/episode-photos/{photo_id}/image'), ({ request, params }) => {
      traffic.image.push({
        photo: String(params.photo_id),
        concession: request.headers.get(CONCESSION_HEADER),
      });
      if (options.image !== undefined) return options.image();
      return new HttpResponse(syntheticJpeg(), {
        headers: { 'content-type': 'image/jpeg', 'cache-control': 'no-store' },
      });
    }),
  );
  return traffic;
}

const drawImage = vi.fn();
let clock: FakeClock;

beforeEach(() => {
  drawImage.mockReset();
  clock = new FakeClock(NOW);
  installTransport(
    createTransport({
      clock,
      connection: createPortConnection(clock),
      concession: () => CONCESSION,
      devLog: () => undefined,
    }),
  );
  vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockImplementation(
    () => ({ drawImage }) as unknown as CanvasRenderingContext2D,
  );
  // jsdom no decodifica imágenes: el mapa de bits sintético tiene el tamaño declarado.
  vi.stubGlobal(
    'createImageBitmap',
    vi.fn(() => Promise.resolve({ width: 64, height: 48, close: vi.fn() })),
  );
});

afterEach(() => {
  installTransport(null);
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

function mount(element: ReactElement) {
  return render(
    <StrictMode>
      <MemoryRouter>{element}</MemoryRouter>
    </StrictMode>,
  );
}

const NOT_AVAILABLE = () => text('errors.not_available_in_scope');

async function settle(): Promise<void> {
  await act(async () => {
    for (let round = 0; round < 5; round += 1) await Promise.resolve();
  });
}

describe('enlace desde la consola', () => {
  it('solo con commissioning.run y con una sesión cargada', () => {
    expect(canOpenEpisodes(['commissioning.run'])).toBe(true);
    expect(canOpenEpisodes(['live_view.open', 'catalog.read'])).toBe(false);
    expect(canOpenEpisodes([])).toBe(false);
    expect(canOpenEpisodes(null)).toBe(false);
  });
});

describe('listado', { timeout: TEST_TIMEOUT }, () => {
  it('muestra los episodios con sus etiquetas y «sin foto» con su motivo; no pide imágenes', async () => {
    const traffic = serve({});
    const view = mount(<EpisodesScreen zoneId={ZONE} clock={clock} />);
    const table = await screen.findByRole('table', {}, WAIT);
    const rows = within(table).getAllByRole('row').slice(1);
    expect(rows).toHaveLength(2);
    const cell = (row: number, column: string) =>
      rows[row]?.querySelector(`[data-column="${column}"]`)?.textContent ?? null;
    expect(cell(0, 'would_have_been')).toBe(label('record_kind', 'finding'));
    expect(cell(0, 'family')).toBe(label('predicate_family', 'dwell'));
    expect(cell(0, 'camera')).toBe(CAMERA);
    expect(cell(0, 'standard')).toBe(`${text('scr06_episodes.standard_version')} 3`);
    expect(cell(0, 'max_confidence')).toBe('0,91');
    expect(cell(0, 'duration')).toBe('4 s');
    expect(cell(0, 'photo')).toBe(text('scr06_episodes.with_photo'));
    expect(cell(1, 'would_have_been')).toBe(label('record_kind', 'detection_for_review'));
    expect(cell(1, 'camera')).toBe(text('scr06_episodes.no_camera'));
    expect(rows[1]?.querySelector('[data-part="absent_reason"]')?.textContent).toBe(
      label('image_absent_reason', 'disk_pressure'),
    );
    expect(rows[1]?.querySelector('[data-part="segment_start"]')).not.toBeNull();
    const links = view.container.querySelectorAll<HTMLAnchorElement>('a[data-part="open_episode"]');
    expect([...links].map((link) => link.getAttribute('href'))).toEqual([
      `/zones/${ZONE}/commissioning/episodes/${WITH_PHOTO}`,
      `/zones/${ZONE}/commissioning/episodes/${WITHOUT_PHOTO}`,
    ]);
    expect(traffic.image).toEqual([]);
    expect(traffic.list.at(-1)).toEqual({ after: null, limit: '50' });
  });

  it('«cargar más» pide la página siguiente con after', async () => {
    const traffic = serve({ next: WITHOUT_PHOTO, photos: [episode()] });
    mount(<EpisodesScreen zoneId={ZONE} clock={clock} />);
    fireEvent.click(
      await screen.findByRole(
        'button',
        { name: text('components_structure.keyset.load_more') },
        WAIT,
      ),
    );
    await screen.findByText(text('components_structure.keyset.end'), {}, WAIT);
    expect(traffic.list.at(-1)).toEqual({ after: WITHOUT_PHOTO, limit: '50' });
  });

  it('not_found (zona en productive o fuera de alcance) → «no disponible en su alcance»', async () => {
    serve({ list: () => apiError('not_found', 404) });
    mount(<EpisodesScreen zoneId={ZONE} clock={clock} />);
    expect(
      await screen.findByRole('heading', { level: 1, name: NOT_AVAILABLE() }, WAIT),
    ).toBeDefined();
    expect(screen.queryByRole('table')).toBeNull();
  });

  it('un error del servidor va a la pila de avisos (UI-19)', async () => {
    serve({ list: () => apiError('internal_error', 500) });
    const view = mount(<EpisodesScreen zoneId={ZONE} clock={clock} />);
    await screen.findByText(text('components_structure.keyset.error'), {}, WAIT);
    expect(
      [...view.container.querySelectorAll('li[data-code]')].map((item) =>
        item.getAttribute('data-code'),
      ),
    ).toEqual(['internal_error']);
    expect(screen.queryByRole('heading', { level: 1, name: NOT_AVAILABLE() })).toBeNull();
  });

  it('axe sin violaciones en el listado', async () => {
    serve({});
    const view = mount(<EpisodesScreen zoneId={ZONE} clock={clock} />);
    await screen.findByRole('table', {}, WAIT);
    expect(await axe(view.container)).toHaveNoViolations();
  });
});

async function openDetail(photoId: string) {
  const view = mount(<EpisodeDetailScreen zoneId={ZONE} photoId={photoId} clock={clock} />);
  await screen.findByRole('heading', { level: 1 }, WAIT);
  return view;
}

function byKey(key: string): Element | null {
  return document.querySelector(`[data-text-key="${key}"]`);
}

/** Sin foto: el aviso sí y la superposición no (A-112). */
function expectNoticeWithoutOverlay(): void {
  const notice = 'media.episode_photo.commissioning_notice';
  expect(byKey(notice)?.textContent).toBe(text(notice));
  expect(byKey('media.episode_photo.blurred_automatically')).toBeNull();
}

function expectNoticeAndOverlay(): void {
  for (const key of [
    'media.episode_photo.commissioning_notice',
    'media.episode_photo.blurred_automatically',
  ]) {
    expect(byKey(key)?.textContent).toBe(text(key));
  }
}

describe('detalle', { timeout: TEST_TIMEOUT }, () => {
  it('pide la imagen una sola vez, con X-Vigia-Concession, y la pinta en el lienzo', async () => {
    const traffic = serve({});
    await openDetail(WITH_PHOTO);
    const canvas = await screen.findByRole(
      'img',
      { name: text('scr06_episodes.detail.photo_alt') },
      WAIT,
    );
    expect(canvas.tagName).toBe('CANVAS');
    expect(drawImage).toHaveBeenCalledTimes(1);
    expect(traffic.image).toEqual([{ photo: WITH_PHOTO, concession: CONCESSION }]);
    expectNoticeAndOverlay();
    // Enfocar la ventana no vuelve a pedirla.
    act(() => {
      window.dispatchEvent(new Event('focus'));
      document.dispatchEvent(new Event('visibilitychange'));
    });
    await settle();
    expect(traffic.image).toHaveLength(1);
  });

  it('un episodio sin foto muestra el motivo etiquetado y no pide ninguna imagen', async () => {
    const traffic = serve({});
    const view = await openDetail(WITHOUT_PHOTO);
    const reason = await screen.findByText(
      label('image_absent_reason', 'disk_pressure'),
      {
        selector: 'figure [data-part="absent_reason"]',
      },
      WAIT,
    );
    expect(reason).toBeDefined();
    await settle();
    expect(traffic.image).toEqual([]);
    expect(view.container.querySelector('canvas')).toBeNull();
    expectNoticeWithoutOverlay();
  });

  it('un episodio que no está en el listado → «no disponible en su alcance» sin pedir imagen', async () => {
    const traffic = serve({ photos: [withoutPhoto()] });
    await openDetail(WITH_PHOTO);
    expect(
      await screen.findByRole('heading', { level: 1, name: NOT_AVAILABLE() }, WAIT),
    ).toBeDefined();
    expect(traffic.image).toEqual([]);
  });

  it('el listado en not_found (zona en productive) → «no disponible» sin pedir imagen', async () => {
    const traffic = serve({ list: () => apiError('not_found', 404) });
    await openDetail(WITH_PHOTO);
    expect(
      await screen.findByRole('heading', { level: 1, name: NOT_AVAILABLE() }, WAIT),
    ).toBeDefined();
    expect(traffic.image).toEqual([]);
  });

  it('la imagen en not_found (vencida, purgada o zona en productive) → «no disponible»', async () => {
    const traffic = serve({ image: () => apiError('not_found', 404) });
    await openDetail(WITH_PHOTO);
    expect(
      await screen.findByRole('heading', { level: 1, name: NOT_AVAILABLE() }, WAIT),
    ).toBeDefined();
    expect(traffic.image).toHaveLength(1);
  });

  it('un fallo transitorio de la imagen: aviso UI-19 y ningún reintento automático', async () => {
    const traffic = serve({
      image: () => apiError('temporarily_unavailable', 503, { retryAfterSeconds: 2 }),
    });
    const view = await openDetail(WITH_PHOTO);
    await screen.findByRole('button', { name: text('media.episode_photo.request_again') }, WAIT);
    expect(
      [...view.container.querySelectorAll('li[data-code]')].map((item) =>
        item.getAttribute('data-code'),
      ),
    ).toEqual(['temporarily_unavailable']);
    // Los reintentos de lectura de ports.base esperarían 2, 4 y 8 s: ninguno sale.
    for (let second = 0; second < 30; second += 1) {
      act(() => {
        clock.advance(1_000);
      });
      await settle();
    }
    expect(traffic.image).toHaveLength(1);
    expectNoticeAndOverlay();
  });

  it('al salir del detalle se cancela la búsqueda en curso', async () => {
    let signal: AbortSignal | null = null;
    const find = vi.fn((_zone: string, _photo: string, abort: AbortSignal) => {
      signal = abort;
      return new Promise<EpisodePhoto | null>(() => undefined);
    });
    const view = render(
      <MemoryRouter>
        <EpisodeDetailScreen zoneId={ZONE} photoId={WITH_PHOTO} clock={clock} find={find} />
      </MemoryRouter>,
    );
    await settle();
    expect(find).toHaveBeenCalledTimes(1);
    expect((signal as AbortSignal | null)?.aborted).toBe(false);
    view.unmount();
    expect((signal as AbortSignal | null)?.aborted).toBe(true);
  });

  it('un identificador que no es UUID no pide nada y pinta «no disponible»', async () => {
    const traffic = serve({});
    const requests: string[] = [];
    const onRequest = ({ request }: { request: Request }) => {
      requests.push(new URL(request.url).pathname);
    };
    server.events.on('request:start', onRequest);
    try {
      for (const path of [
        `/zones/no-es-uuid/commissioning/episodes`,
        `/zones/${ZONE}/commissioning/episodes/no-es-uuid`,
      ]) {
        const view = render(
          <MemoryRouter initialEntries={[path]}>
            <Routes>
              <Route path="/zones/:zone_id/commissioning/episodes" element={<EpisodesPage />} />
              <Route
                path="/zones/:zone_id/commissioning/episodes/:photo_id"
                element={<EpisodesPage />}
              />
            </Routes>
          </MemoryRouter>,
        );
        expect(
          await screen.findByRole('heading', { level: 1, name: NOT_AVAILABLE() }, WAIT),
        ).toBeDefined();
        view.unmount();
      }
      await settle();
      expect(requests).toEqual([]);
      expect(traffic).toEqual({ list: [], image: [] });
    } finally {
      server.events.removeListener('request:start', onRequest);
    }
  });

  it('axe sin violaciones en el detalle con foto y sin foto', async () => {
    serve({});
    const withImage = await openDetail(WITH_PHOTO);
    await screen.findByRole('img', { name: text('scr06_episodes.detail.photo_alt') }, WAIT);
    expect(await axe(withImage.container)).toHaveNoViolations();
    withImage.unmount();
    const absent = await openDetail(WITHOUT_PHOTO);
    await screen.findByText(
      label('image_absent_reason', 'disk_pressure'),
      {
        selector: 'figure [data-part="absent_reason"]',
      },
      WAIT,
    );
    expect(await axe(absent.container)).toHaveNoViolations();
  });
});
