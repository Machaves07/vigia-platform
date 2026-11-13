/**
 * Puerto de los episodios de la sombra en SCR-06 (TASK-574; A-112, QF10; BR-APP-58, 77).
 *
 * Dos operaciones `fixed` de TASK-573, las dos con `commissioning.run` (solo la instaladora, bajo
 * concesión vigente). La concesión viaja en `X-Vigia-Concession`, que pone `ports.base` en toda
 * petición; nunca en una cookie, en la URL ni en un `<img src>` (que no puede enviarla).
 *
 * - `GET /zones/{zone_id}/episode-photos`: el listado por clave, el más reciente primero; solo
 *   metadatos y el motivo de la ausencia de imagen, sin bytes, suma ni URL. El cursor del servidor
 *   (`next_after`) viaja en la tabla como `after_photo_id` (las claves de cursor de UI-14 son
 *   `cursor` o `after_*`) y vuelve al servidor como `after`.
 * - `GET /episode-photos/{photo_id}/image`: el JPEG difuminado, del mismo origen y `no-store`.
 *   Cada petición deja **una** entrada `episode_photo_read`: la lectura se declara sin
 *   reintentos (`retry: false`) y solo la pide el detalle, una vez por apertura (UI-26).
 *
 * Una zona en `productive`, una foto vencida o purgada y todo lo que está fuera de alcance
 * responden `not_found`: la pantalla muestra «no disponible en su alcance» (BR-APP-77).
 */
import type { components } from '@/client/schema';

import { defineScreenPort } from './base';

/** Un episodio de la sombra: metadatos y la imagen declarada o el motivo de su ausencia. */
export type EpisodePhoto = components['schemas']['EpisodePhotoOut'];
/** Página del listado (`EpisodePhotosOut`). */
export type EpisodePhotos = components['schemas']['EpisodePhotosOut'];

/** Motivos cerrados de una foto sin imagen (contrato v1.5.0, `ImageAbsentReason`). */
export const IMAGE_ABSENT_REASONS: readonly string[] = [
  'frame_unavailable',
  'too_large',
  'disk_pressure',
  'pending_cap',
];

/** Clave del cursor del listado en la tabla (UI-14 admite `cursor` o `after_*`). */
export const EPISODE_CURSOR = 'after_photo_id';

/** Tamaño de página con que el detalle busca su episodio en el listado (máximo del servidor). */
export const LOOKUP_PAGE_SIZE = 200;
/**
 * Páginas que el detalle recorre como mucho: 50 × 200 = 10 000 episodios, por encima de los
 * 30 días de retención a unos 200 episodios al día (A-112). Pasado el tope, «no disponible».
 */
export const LOOKUP_MAX_PAGES = 50;

export const scr06EpisodePhotosPort = defineScreenPort('SCR-06', {
  /** `GET /zones/{zone_id}/episode-photos`: listado por clave, solo metadatos. */
  episodePhotos: {
    kind: 'read',
    status: 'fixed',
    operationId: 'episode_photos_zones__zone_id__episode_photos_get',
    permission: 'commissioning.run',
  },
  /** `GET /episode-photos/{photo_id}/image`: el JPEG difuminado, una petición por apertura. */
  episodePhotoImage: {
    kind: 'read',
    status: 'fixed',
    operationId: 'episode_photo_image_episode_photos__photo_id__image_get',
    permission: 'commissioning.run',
    retry: false,
  },
});

export interface EpisodePhotoPage {
  readonly items: readonly EpisodePhoto[];
  /** `{after_photo_id}` para la página siguiente, o `null` si no hay más. */
  readonly nextCursor: Readonly<Record<string, string>> | null;
}

/** Una página del listado de la zona, con el cursor de la página anterior (o `null`). */
export async function readEpisodePhotos(
  zoneId: string,
  cursor: Readonly<Record<string, string>> | null,
  pageSize: number,
  signal?: AbortSignal,
): Promise<EpisodePhotoPage> {
  const after = cursor?.[EPISODE_CURSOR] ?? null;
  const page = await scr06EpisodePhotosPort.episodePhotos.read(
    {
      path: { zone_id: zoneId },
      query: { limit: pageSize, ...(after === null ? {} : { after }) },
    },
    signal,
  );
  return {
    items: page.photos,
    nextCursor: page.next_after === null ? null : { [EPISODE_CURSOR]: page.next_after },
  };
}

/**
 * El episodio `photoId` de la zona, buscado en el listado (la API no tiene lectura de un solo
 * episodio): páginas de 200 hasta encontrarlo, hasta que no haya más o hasta el tope. `null` si
 * no está: la pantalla lo trata como `not_found` (BR-APP-77). Un cursor repetido corta la
 * búsqueda en vez de pedir la misma página otra vez.
 */
export async function findEpisodePhoto(
  zoneId: string,
  photoId: string,
  signal?: AbortSignal,
): Promise<EpisodePhoto | null> {
  let cursor: Readonly<Record<string, string>> | null = null;
  const seen = new Set<string>();
  for (let page = 0; page < LOOKUP_MAX_PAGES; page += 1) {
    const result = await readEpisodePhotos(zoneId, cursor, LOOKUP_PAGE_SIZE, signal);
    const found = result.items.find((photo) => photo.photo_id === photoId);
    if (found !== undefined) return found;
    const next = result.nextCursor?.[EPISODE_CURSOR];
    if (next === undefined || seen.has(next)) return null;
    seen.add(next);
    cursor = result.nextCursor;
  }
  return null;
}

/** La imagen difuminada de un episodio (`image/jpeg`): una petición, sin reintentos. */
export function readEpisodePhotoImage(photoId: string, signal?: AbortSignal): Promise<Blob> {
  return scr06EpisodePhotosPort.episodePhotoImage.read({ path: { photo_id: photoId } }, signal);
}
