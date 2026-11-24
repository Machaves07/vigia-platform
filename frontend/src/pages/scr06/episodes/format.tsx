/**
 * Celdas del listado y datos del detalle de los episodios de la sombra: adaptan `EpisodePhotoOut`
 * a las celdas del panel compartido (`EpisodePhotoPanel`, TASK-590), que es de donde salen los
 * textos. Nada se calcula aparte de la duración (fin menos inicio del tramo que dio el servidor).
 */
import {
  durationText,
  FrameTime as PanelFrameTime,
  PhotoState as PanelPhotoState,
} from '@/components/EpisodePhotoPanel/EpisodePhotoPanel';
import { ServerTimestamp } from '@/pages/scr06/common/ServerTimestamp';
import type { EpisodePhoto } from '@/ports/scr06EpisodePhotos';
import { label } from '@/texts/labels';

export {
  cameraOf,
  confidenceOf,
  familyOf,
  standardOf,
} from '@/components/EpisodePhotoPanel/EpisodePhotoPanel';

/** Un instante del servidor en la zona de la planta del alcance (UI-25). */
export function serverInstant(instant: string) {
  return <ServerTimestamp instant={instant} />;
}

/** La hora del cuadro; sin cuadro (foto sin imagen), el inicio del tramo y lo dice. */
export function FrameTime({ photo }: { readonly photo: EpisodePhoto }) {
  return (
    <PanelFrameTime frameAt={photo.frame_at} startedAt={photo.started_at} instant={serverInstant} />
  );
}

/** Lo que el tramo habría sido en `productive`: hallazgo o detección (etiqueta del contrato). */
export function wouldHaveBeen(photo: EpisodePhoto): string {
  return label('record_kind', photo.classified_as);
}

/** Fin menos inicio del tramo (puede no ser un número si una marca es ilegible). */
export function durationMsOf(photo: EpisodePhoto): number {
  return Date.parse(photo.ended_at) - Date.parse(photo.started_at);
}

/** Duración del tramo; con marcas ilegibles o invertidas, «desconocido». */
export function durationOf(photo: EpisodePhoto): string {
  return durationText(durationMsOf(photo));
}

/** «Con foto» o «Sin foto: <motivo etiquetado>». */
export function PhotoState({ photo }: { readonly photo: EpisodePhoto }) {
  return (
    <PanelPhotoState hasImage={photo.image !== null} absentReason={photo.image_absent_reason} />
  );
}
