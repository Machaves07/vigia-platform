/**
 * Celdas comunes del listado y del detalle de los episodios de la sombra: hora del cuadro
 * (UI-25), etiquetas del contrato («habría sido» y familia), confianza y duración del tramo.
 * Nada se calcula aparte de la duración (fin menos inicio del tramo que dio el servidor).
 */
import { ServerTimestamp } from '@/pages/scr06/common/ServerTimestamp';
import type { EpisodePhoto } from '@/ports/scr06EpisodePhotos';
import { formatDuration } from '@/texts/duration';
import { label } from '@/texts/labels';
import { text } from '@/texts';

const CONFIDENCE = new Intl.NumberFormat('es-CO', { maximumFractionDigits: 2 });

/** La hora del cuadro; sin cuadro (foto sin imagen), el inicio del tramo y lo dice. */
export function FrameTime({ photo }: { readonly photo: EpisodePhoto }) {
  if (photo.frame_at !== null) return <ServerTimestamp instant={photo.frame_at} />;
  return (
    <>
      <ServerTimestamp instant={photo.started_at} />{' '}
      <span data-part="segment_start">({text('scr06_episodes.segment_start')})</span>
    </>
  );
}

/** Lo que el tramo habría sido en `productive`: hallazgo o detección (etiqueta del contrato). */
export function wouldHaveBeen(photo: EpisodePhoto): string {
  return label('record_kind', photo.classified_as);
}

export function familyOf(photo: EpisodePhoto): string {
  return label('predicate_family', photo.family);
}

export function standardOf(photo: EpisodePhoto): string {
  return `${text('scr06_episodes.standard_version')} ${String(photo.standard.version)}`;
}

export function confidenceOf(photo: EpisodePhoto): string {
  return CONFIDENCE.format(photo.max_confidence);
}

/** Duración del tramo; con marcas ilegibles o invertidas, «desconocido». */
export function durationOf(photo: EpisodePhoto): string {
  const ms = Date.parse(photo.ended_at) - Date.parse(photo.started_at);
  return formatDuration(ms) ?? text('common.unknown');
}

export function cameraOf(photo: EpisodePhoto): string {
  return photo.camera_id ?? text('scr06_episodes.no_camera');
}

/** «Con foto» o «Sin foto: <motivo etiquetado>». */
export function PhotoState({ photo }: { readonly photo: EpisodePhoto }) {
  if (photo.image !== null) return <>{text('scr06_episodes.with_photo')}</>;
  return (
    <>
      {text('scr06_episodes.without_photo')}:{' '}
      <span data-part="absent_reason">
        {label('image_absent_reason', photo.image_absent_reason ?? '')}
      </span>
    </>
  );
}
