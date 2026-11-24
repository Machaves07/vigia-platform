/**
 * Puerto de la foto del hallazgo (TASK-590; A-128 §6; A-112 «Precisiones del 13-11»; BR-APP-58).
 *
 * Los metadatos de las fotos llegan con el detalle (SCR-08 y SCR-09) y la ficha de vida
 * (`RecordOut.photos` y `TimelineMilestoneOut.photos`, TASK-589): sin bytes, suma, tamaño ni URL,
 * con las horas agregadas en una zona unipersonal. La imagen se pide aparte:
 *
 * - `GET /loop/findings/{finding_state_id}/photos/{photo_id}/image`: el JPEG difuminado, del mismo
 *   origen y `no-store`, con `findings.read` y `evidence.read` en la zona del hallazgo. Cada
 *   petición deja **una** entrada `finding_photo_read`: la lectura se declara sin reintentos
 *   (`retry: false`), sin caché ni recarga al enfocar, y solo la pide UI-26 (`BlurredEpisodePhoto`)
 *   una vez por apertura o por gesto de la persona.
 *
 * Una foto vencida, purgada, ajena o sin permiso responde `not_found` (nunca `forbidden`); sin
 * compuerta legible, 503. El bloque de la foto lo muestra sin sacar de la pantalla el hallazgo.
 */
import type { components } from '@/client/schema';

import { defineScreenPort } from './base';

/** Una foto del hallazgo: solo metadatos (`FindingPhotoOut`). */
export type FindingPhoto = components['schemas']['FindingPhotoOut'];

export const findingPhotosPort = defineScreenPort('SCR-08', {
  /** `GET /loop/findings/{finding_state_id}/photos/{photo_id}/image`: una petición por apertura. */
  findingPhotoImage: {
    kind: 'read',
    status: 'fixed',
    operationId: 'finding_photo_image_loop_findings__finding_state_id__photos__photo_id__image_get',
    permission: 'evidence.read',
    retry: false,
  },
});

/** La imagen difuminada de una foto del hallazgo (`image/jpeg`): una petición, sin reintentos. */
export function readFindingPhotoImage(
  findingStateId: string,
  photoId: string,
  signal?: AbortSignal,
): Promise<Blob> {
  return findingPhotosPort.findingPhotoImage.read(
    { path: { finding_state_id: findingStateId, photo_id: photoId } },
    signal,
  );
}
