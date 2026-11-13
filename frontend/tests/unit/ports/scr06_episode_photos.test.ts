/**
 * Puerto de los episodios de la sombra (TASK-574; A-112, «Precisiones del 13-11, lectura y
 * purga»):
 *
 * - la imagen sale por el transporte con `X-Vigia-Concession` y **una sola vez**: sin los
 *   reintentos de lectura de `ports.base` (cada petición deja una entrada `episode_photo_read`),
 *   mientras el listado sí conserva los suyos;
 * - el cursor del listado viaja como `after_photo_id` en la tabla y como `after` al servidor;
 * - el detalle busca su episodio por páginas de 200, corta ante un cursor repetido y tiene tope.
 */
import { http, HttpResponse } from 'msw';
import { afterEach, describe, expect, it } from 'vitest';

import {
  CONCESSION_HEADER,
  createPortConnection,
  createTransport,
  installTransport,
  type TransportOptions,
} from '@/ports/base';
import {
  findEpisodePhoto,
  readEpisodePhotoImage,
  readEpisodePhotos,
  scr06EpisodePhotosPort,
  type EpisodePhoto,
} from '@/ports/scr06EpisodePhotos';
import { isPortError } from '@/ports/shared';

import { mswPath } from '../../msw/handlers';
import { server } from '../../msw/server';
import { FakeClock } from '../../support/fakeClock';
import { drive, scriptedFetch, type Step } from '../../support/ports';

const ZONE = '0190a1b2-0000-7000-8000-000000000101';
const PHOTO = '0190a1b2-0000-7000-8000-000000000102';
const CONCESSION = '0190a1b2-0000-7000-8000-000000000103';

function install(clock: FakeClock, extra: Partial<TransportOptions> = {}): void {
  installTransport(
    createTransport({
      clock,
      connection: createPortConnection(clock),
      devLog: () => undefined,
      ...extra,
    }),
  );
}

function failureKind(outcome: { ok: boolean; error?: unknown }): string | null {
  return !outcome.ok && isPortError(outcome.error) ? outcome.error.failure.kind : null;
}

function photo(id: string): EpisodePhoto {
  return {
    photo_id: id,
    zone_id: ZONE,
    node_id: '0190a1b2-0000-7000-8000-000000000104',
    episode_id: '0190a1b2-0000-7000-8000-000000000105',
    segment_index: 0,
    classified_as: 'finding',
    family: 'dwell',
    standard: { standard_id: '0190a1b2-0000-7000-8000-000000000106', version: 2 },
    max_confidence: 0.9,
    started_at: '2026-11-12T13:40:02.000Z',
    ended_at: '2026-11-12T13:40:06.000Z',
    received_at: '2026-11-12T13:40:07.100Z',
    expires_at: '2026-12-12T13:40:07.100Z',
    camera_id: null,
    frame_at: null,
    image: null,
    image_absent_reason: 'pending_cap',
  };
}

afterEach(() => {
  installTransport(null);
});

describe('imagen: una petición por apertura, con la concesión en la cabecera', () => {
  it('el puerto declara la imagen sin reintentos y con commissioning.run', () => {
    expect(
      scr06EpisodePhotosPort.operations.map(({ name, kind, permission }) => ({
        name,
        kind,
        permission,
      })),
    ).toEqual([
      { name: 'episodePhotos', kind: 'read', permission: 'commissioning.run' },
      { name: 'episodePhotoImage', kind: 'read', permission: 'commissioning.run' },
    ]);
  });

  it.each<Step>(['network', 'timeout', 'transient'])(
    'un fallo «%s» de la imagen no se repite: una sola petición',
    async (step) => {
      const clock = new FakeClock();
      const { calls, fetch } = scriptedFetch(clock, [step, step, step, step]);
      install(clock, { fetch });
      const outcome = await drive(clock, readEpisodePhotoImage(PHOTO));
      expect(failureKind(outcome)).toBe(step === 'transient' ? 'http' : step);
      expect(calls).toHaveLength(1);
    },
  );

  it('el listado conserva los reintentos de lectura (el ajuste es solo de la imagen)', async () => {
    const clock = new FakeClock();
    const { calls, fetch } = scriptedFetch(clock, ['network', 'ok']);
    install(clock, { fetch });
    const outcome = await drive(
      clock,
      scr06EpisodePhotosPort.episodePhotos.read({ path: { zone_id: ZONE } }),
    );
    expect(outcome.ok).toBe(true);
    expect(calls).toHaveLength(2);
  });

  it('la imagen viaja con X-Vigia-Concession por fetch y llega como Blob image/jpeg', async () => {
    const seen: { concession: string | null; path: string }[] = [];
    server.use(
      http.get(mswPath('/episode-photos/{photo_id}/image'), ({ request }) => {
        seen.push({
          concession: request.headers.get(CONCESSION_HEADER),
          path: new URL(request.url).pathname,
        });
        return new HttpResponse(new Uint8Array([0xff, 0xd8, 0xff, 0xd9]), {
          headers: { 'content-type': 'image/jpeg', 'cache-control': 'no-store' },
        });
      }),
    );
    install(new FakeClock(), { concession: () => CONCESSION });
    const blob = await readEpisodePhotoImage(PHOTO);
    expect(blob).toBeInstanceOf(Blob);
    expect(blob.type).toBe('image/jpeg');
    expect(seen).toEqual([{ concession: CONCESSION, path: `/episode-photos/${PHOTO}/image` }]);
  });
});

describe('listado por clave', () => {
  it('after_photo_id de la tabla va como after; next_after vuelve como after_photo_id', async () => {
    const queries: Record<string, string>[] = [];
    server.use(
      http.get(mswPath('/zones/{zone_id}/episode-photos'), ({ request }) => {
        queries.push(Object.fromEntries(new URL(request.url).searchParams));
        return HttpResponse.json({ photos: [photo(PHOTO)], next_after: PHOTO });
      }),
    );
    install(new FakeClock());
    const first = await readEpisodePhotos(ZONE, null, 50);
    expect(first.nextCursor).toEqual({ after_photo_id: PHOTO });
    await readEpisodePhotos(ZONE, first.nextCursor, 50);
    expect(queries).toEqual([{ limit: '50' }, { limit: '50', after: PHOTO }]);
  });
});

describe('búsqueda del detalle en el listado', () => {
  const ids = (n: number) =>
    Array.from({ length: n }, (_, i) => `0190a1b2-0000-7000-8000-${String(i).padStart(12, '0')}`);

  it('recorre páginas de 200 hasta encontrarlo', async () => {
    const [a = '', b = '', c = ''] = ids(3);
    const pages: Record<string, { photos: EpisodePhoto[]; next_after: string | null }> = {
      '': { photos: [photo(a)], next_after: a },
      [a]: { photos: [photo(b)], next_after: b },
      [b]: { photos: [photo(c)], next_after: null },
    };
    const limits: string[] = [];
    server.use(
      http.get(mswPath('/zones/{zone_id}/episode-photos'), ({ request }) => {
        const params = new URL(request.url).searchParams;
        limits.push(params.get('limit') ?? '');
        return HttpResponse.json(pages[params.get('after') ?? ''] ?? null);
      }),
    );
    install(new FakeClock());
    expect((await findEpisodePhoto(ZONE, c))?.photo_id).toBe(c);
    expect(limits).toEqual(['200', '200', '200']);
    expect(await findEpisodePhoto(ZONE, PHOTO)).toBeNull();
  });

  it('un cursor repetido corta la búsqueda; el tope también', async () => {
    let calls = 0;
    server.use(
      http.get(mswPath('/zones/{zone_id}/episode-photos'), () => {
        calls += 1;
        return HttpResponse.json({ photos: [], next_after: ZONE });
      }),
    );
    install(new FakeClock());
    expect(await findEpisodePhoto(ZONE, PHOTO)).toBeNull();
    expect(calls).toBe(2);

    calls = 0;
    server.use(
      http.get(mswPath('/zones/{zone_id}/episode-photos'), () => {
        calls += 1;
        return HttpResponse.json({ photos: [], next_after: `${String(calls)}-cursor` });
      }),
    );
    expect(await findEpisodePhoto(ZONE, PHOTO)).toBeNull();
    // Tope del diseño: 50 páginas de 200 (10 000 episodios, por encima de 30 días de retención).
    expect(calls).toBe(50);
  });
});
