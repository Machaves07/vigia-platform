/**
 * Panel de la foto del episodio (TASK-590; A-128 §6; UI-26; BR-APP-58), extraído de «Episodio de
 * la sombra» de SCR-06 (TASK-574): la foto a la izquierda y, a la derecha, la hora del cuadro, la
 * cámara, lo que el tramo fue, la familia, el estándar, la confianza máxima, el tramo, su inicio,
 * su fin y su duración, cuándo se recibió, el vencimiento y el estado de la foto.
 *
 * - `commissioning` (SCR-06): las 13 filas de siempre, con «Habría sido» y «Vencimiento».
 * - `finding` (la foto del hallazgo en SCR-08 y SCR-09): «Clasificado como» y «Se conserva hasta»
 *   solo si la foto vence (`expires_at` no nulo). Las horas vienen tal como las da el servidor,
 *   agregadas en una zona unipersonal (BR-LAZ-06); la pantalla decide cómo se pintan (`instant`).
 *
 * Nada se calcula aquí salvo la presentación: la duración llega ya hecha (en SCR-06, fin menos
 * inicio del tramo que dio el servidor; en el hallazgo, `episode_duration_ms`). Las filas llevan
 * `data-part` y `data-text-key` para comprobarlas sin comparar subcadenas.
 */
import type { ReactNode } from 'react';

import { formatDuration } from '@/texts/duration';
import { label } from '@/texts/labels';
import { text } from '@/texts';

import styles from './EpisodePhotoPanel.module.css';

const CONFIDENCE = new Intl.NumberFormat('es-CO', { maximumFractionDigits: 2 });

/** Lo que el panel muestra de una foto: lo común a `EpisodePhotoOut` y `FindingPhotoOut`. */
export interface PanelPhoto {
  readonly frame_at: string | null;
  readonly started_at: string;
  readonly ended_at: string;
  readonly received_at: string;
  readonly expires_at: string | null;
  readonly camera_id: string | null;
  readonly classified_as: string;
  readonly family: string;
  readonly standard: { readonly version: number };
  readonly max_confidence: number;
  readonly has_image: boolean;
  readonly image_absent_reason: string | null;
}

/** Cómo se pinta un instante del servidor (la zona de la planta y, si hace falta, la agregación). */
export type InstantView = (instant: string) => ReactNode;

/** La hora del cuadro; sin cuadro (foto sin imagen), el inicio del tramo y lo dice. */
export function FrameTime({
  frameAt,
  startedAt,
  instant,
}: {
  readonly frameAt: string | null;
  readonly startedAt: string;
  readonly instant: InstantView;
}) {
  if (frameAt !== null) return <>{instant(frameAt)}</>;
  return (
    <>
      {instant(startedAt)}{' '}
      <span data-part="segment_start">({text('episode_photo_panel.segment_start')})</span>
    </>
  );
}

export function familyOf(photo: { readonly family: string }): string {
  return label('predicate_family', photo.family);
}

export function standardOf(photo: { readonly standard: { readonly version: number } }): string {
  return `${text('episode_photo_panel.standard_version')} ${String(photo.standard.version)}`;
}

export function confidenceOf(photo: { readonly max_confidence: number }): string {
  return CONFIDENCE.format(photo.max_confidence);
}

export function cameraOf(photo: { readonly camera_id: string | null }): string {
  return photo.camera_id ?? text('episode_photo_panel.no_camera');
}

/** Duración del tramo; sin dato o con marcas ilegibles o invertidas, «desconocido». */
export function durationText(ms: number | null): string {
  return (ms === null ? null : formatDuration(ms)) ?? text('common.unknown');
}

/** «Con foto» o «Sin foto: <motivo etiquetado>». */
export function PhotoState({
  hasImage,
  absentReason,
}: {
  readonly hasImage: boolean;
  readonly absentReason: string | null;
}) {
  if (hasImage) return <>{text('episode_photo_panel.with_photo')}</>;
  return (
    <>
      {text('episode_photo_panel.without_photo')}:{' '}
      <span data-part="absent_reason">{label('image_absent_reason', absentReason ?? '')}</span>
    </>
  );
}

type PanelMode = 'commissioning' | 'finding';

export interface EpisodePhotoPanelProps {
  readonly mode: PanelMode;
  readonly photo: PanelPhoto;
  /** El tramo tal como se muestra a la persona; `null` si la lectura no lo trae. */
  readonly segment: number | null;
  /** Duración del tramo en milisegundos; `null` si no se conoce. */
  readonly durationMs: number | null;
  readonly instant: InstantView;
  /** Lo que va a la izquierda: la foto (UI-26) o el aviso que la sustituye. */
  readonly media: ReactNode;
}

interface Row {
  readonly part: string;
  readonly key: string;
  readonly value: ReactNode;
}

function rows({ mode, photo, segment, durationMs, instant }: EpisodePhotoPanelProps): Row[] {
  const list: Row[] = [
    {
      part: 'frame_at',
      key: 'episode_photo_panel.frame_at',
      value: <FrameTime frameAt={photo.frame_at} startedAt={photo.started_at} instant={instant} />,
    },
    { part: 'camera', key: 'episode_photo_panel.camera', value: cameraOf(photo) },
    mode === 'commissioning'
      ? {
          part: 'would_have_been',
          key: 'episode_photo_panel.would_have_been',
          value: label('record_kind', photo.classified_as),
        }
      : {
          part: 'classified_as',
          key: 'episode_photo_panel.classified_as',
          value: label('record_kind', photo.classified_as),
        },
    { part: 'family', key: 'episode_photo_panel.family', value: familyOf(photo) },
    { part: 'standard', key: 'episode_photo_panel.standard', value: standardOf(photo) },
    {
      part: 'max_confidence',
      key: 'episode_photo_panel.max_confidence',
      value: confidenceOf(photo),
    },
    {
      part: 'segment',
      key: 'episode_photo_panel.segment',
      value: segment === null ? text('common.unknown') : String(segment),
    },
    { part: 'started_at', key: 'episode_photo_panel.started_at', value: instant(photo.started_at) },
    { part: 'ended_at', key: 'episode_photo_panel.ended_at', value: instant(photo.ended_at) },
    { part: 'duration', key: 'episode_photo_panel.duration', value: durationText(durationMs) },
    {
      part: 'received_at',
      key: 'episode_photo_panel.received_at',
      value: instant(photo.received_at),
    },
  ];
  if (mode === 'commissioning') {
    list.push({
      part: 'expires_at',
      key: 'episode_photo_panel.expires_at',
      value: photo.expires_at === null ? text('common.unknown') : instant(photo.expires_at),
    });
  } else if (photo.expires_at !== null) {
    list.push({
      part: 'kept_until',
      key: 'episode_photo_panel.kept_until',
      value: instant(photo.expires_at),
    });
  }
  list.push({
    part: 'photo',
    key: 'episode_photo_panel.photo',
    value: <PhotoState hasImage={photo.has_image} absentReason={photo.image_absent_reason} />,
  });
  return list;
}

export function EpisodePhotoPanel(props: EpisodePhotoPanelProps) {
  const facts = (
    <dl className={styles.facts}>
      {rows(props).map((row) => (
        <div key={row.part} className={styles.fact} data-part={`fact_${row.part}`}>
          <dt data-text-key={row.key}>{text(row.key)}</dt>
          <dd>{row.value}</dd>
        </div>
      ))}
    </dl>
  );
  return (
    <div className={styles.layout} data-part="episode_photo_panel" data-mode={props.mode}>
      {props.media}
      {props.mode === 'commissioning' ? (
        // SCR-06: una región por pantalla, como antes de extraer el panel.
        <section className={styles.panel} aria-label={text('episode_photo_panel.facts')}>
          {facts}
        </section>
      ) : (
        // En el hallazgo hay un panel por tramo: un grupo, no una región repetida.
        <div className={styles.panel} role="group" aria-label={text('episode_photo_panel.facts')}>
          {facts}
        </div>
      )}
    </div>
  );
}
