/**
 * La foto del hallazgo con el aspecto de SCR-06 (TASK-590; A-128 §1, §3 y §6; UI-26; BR-APP-58),
 * en la clasificación (SCR-09), el detalle (SCR-08) y la ficha de vida.
 *
 * - **Cuándo se pide la imagen.** Cada petición deja una entrada `finding_photo_read`: con
 *   `request="on_mount"` (la clasificación y el registro ancla del detalle) se pide la foto `0` al
 *   montar, una vez por apertura (UI-26 la protege del doble montaje de `StrictMode`); con
 *   `request="on_gesture"` (los demás tramos del detalle), solo con «Ver foto»; con `folded` (la
 *   ficha), el panel entero espera a «Ver foto». Ninguna foto distinta de la que se ve se pide sin
 *   gesto, tampoco la ráfaga.
 * - **Fase 2.** Con más de una foto (la `0` y hasta 20 de ráfaga), «Foto n de N» por
 *   `photo_index`, con «anterior» y «siguiente» y sin miniaturas (cada imagen es una lectura
 *   auditada). En una zona unipersonal la navegación no da la hora de cada foto: las horas llegan
 *   agregadas y en la ráfaga serían todas la misma ventana (BR-LAZ-06).
 * - **Avisos en el bloque de la foto** (el resto del hallazgo sigue en pantalla): sin fotos, sin
 *   `evidence.read` (sin pedir nada), `not_found` de la imagen («La foto ya no está disponible»;
 *   a diferencia de SCR-06, no saca de la pantalla) y lo transitorio en la pila de UI-19, con
 *   «Volver a pedir la imagen» como gesto.
 */
import { useCallback, useEffect, useRef, useState } from 'react';

import { BlurredEpisodePhoto } from '@/components/BlurredEpisodePhoto/BlurredEpisodePhoto';
import { NoticeStack } from '@/components/NoticeStack/NoticeStack';
import type { Notice } from '@/components/NoticeStack/notice';
import { plantTime } from '@/components/TimestampWithOffset/format';
import { TimestampWithOffset } from '@/components/TimestampWithOffset/TimestampWithOffset';
import { readFindingPhotoImage, type FindingPhoto as Photo } from '@/ports/findingPhotos';
import { isPortError } from '@/ports/shared';
import type { Clock } from '@/state/clock';
import { text } from '@/texts';

import { EpisodePhotoPanel } from './EpisodePhotoPanel';
import styles from './EpisodePhotoPanel.module.css';

export interface FindingPhotoProps {
  readonly findingStateId: string;
  /** Las fotos del registro (`RecordOut.photos` o `TimelineMilestoneOut.photos`). */
  readonly photos: readonly Photo[];
  /** El tramo como se numera en la pantalla («Registro 2» → 2); `null` si no se conoce. */
  readonly segment: number | null;
  /** La sesión tiene `evidence.read`: sin él, el aviso del rol y ninguna petición. */
  readonly canView: boolean;
  /** `on_mount`: la foto que se ve se pide al montar; `on_gesture`: solo con «Ver foto». */
  readonly request: 'on_mount' | 'on_gesture';
  /** El panel entero plegado hasta «Ver foto» (la ficha de vida). */
  readonly folded?: boolean;
  readonly timeZone: string;
  /** Horas agregadas por el servidor (zona unipersonal). */
  readonly aggregated: boolean;
  readonly clock: Clock;
  readonly headingLevel: 2 | 3 | 4;
}

function isNotFound(error: unknown): boolean {
  return (
    isPortError(error) && error.failure.kind === 'http' && error.failure.error.code === 'not_found'
  );
}

/** El aviso de UI-19 de un fallo de la imagen; `null` si se canceló (cambio de pantalla). */
function noticeOf(error: unknown, id: string): Notice | null {
  if (isPortError(error)) {
    if (error.failure.kind === 'aborted') return null;
    if (error.failure.kind === 'http') return { id, error: error.failure.error };
  }
  return { id, error: { code: 'temporarily_unavailable', status: 0 } };
}

/** «hh:mm:ss» de la hora de la foto en la zona de la planta, o `null` si no se puede dar. */
function clockTime(instant: string, timeZone: string): string | null {
  const value = plantTime(instant, timeZone);
  return value === null ? null : value.local.slice(-8);
}

function Heading({ level, children }: { readonly level: 2 | 3 | 4; readonly children: string }) {
  if (level === 2) return <h2 className={styles.title}>{children}</h2>;
  if (level === 3) return <h3 className={styles.title}>{children}</h3>;
  return <h4 className={styles.title}>{children}</h4>;
}

function Message({ textKey }: { readonly textKey: string }) {
  return (
    <p className={styles.message} data-part="photo_notice" data-text-key={textKey}>
      {text(textKey)}
    </p>
  );
}

function Navigation({
  photos,
  index,
  timeZone,
  aggregated,
  onChange,
}: {
  readonly photos: readonly Photo[];
  readonly index: number;
  readonly timeZone: string;
  readonly aggregated: boolean;
  readonly onChange: (index: number) => void;
}) {
  const photo = photos[index];
  if (photo === undefined) return null;
  const at = aggregated ? null : clockTime(photo.frame_at ?? photo.started_at, timeZone);
  const role =
    photo.photo_role === 'peak'
      ? 'episode_photo_panel.finding.role_peak'
      : 'episode_photo_panel.finding.role_burst';
  return (
    <div
      className={styles.navigation}
      role="group"
      aria-label={text('episode_photo_panel.finding.navigation')}
      data-part="photo_navigation"
    >
      <button
        type="button"
        className={styles.button}
        data-part="previous_photo"
        disabled={index === 0}
        onClick={() => {
          onChange(index - 1);
        }}
      >
        {text('episode_photo_panel.finding.previous')}
      </button>
      <p className={styles.position} aria-live="polite" data-part="photo_position">
        <span data-part="photo_count">
          {`${text('episode_photo_panel.finding.position')} ${String(index + 1)} ${text(
            'episode_photo_panel.finding.of',
          )} ${String(photos.length)}`}
        </span>
        {at !== null && (
          <>
            {' · '}
            <span data-part="photo_time">{at}</span>
          </>
        )}
        {' · '}
        <span data-part="photo_role" data-text-key={role}>
          {text(role)}
        </span>
      </p>
      <button
        type="button"
        className={styles.button}
        data-part="next_photo"
        disabled={index === photos.length - 1}
        onClick={() => {
          onChange(index + 1);
        }}
      >
        {text('episode_photo_panel.finding.next')}
      </button>
    </div>
  );
}

export function FindingPhoto({
  findingStateId,
  photos,
  segment,
  canView,
  request,
  folded = false,
  timeZone,
  aggregated,
  clock,
  headingLevel,
}: FindingPhotoProps) {
  const [index, setIndex] = useState(0);
  const [opened, setOpened] = useState(request === 'on_mount' && !folded);
  const [unfolded, setUnfolded] = useState(!folded);
  const [gone, setGone] = useState<ReadonlySet<string>>(() => new Set());
  const [notices, setNotices] = useState<readonly Notice[]>([]);
  const counter = useRef(0);
  const mediaRef = useRef<HTMLDivElement>(null);
  const focusMedia = useRef(false);

  useEffect(() => {
    if (!opened || !focusMedia.current) return;
    focusMedia.current = false;
    mediaRef.current?.focus();
  }, [opened]);

  const requestImage = useCallback(
    (photoId: string, signal: AbortSignal) =>
      readFindingPhotoImage(findingStateId, photoId, signal),
    [findingStateId],
  );

  const sorted = [...photos].sort((a, b) => a.photo_index - b.photo_index);
  const current = sorted[Math.min(index, sorted.length - 1)];
  const photoId = current?.photo_id ?? null;

  // UI-26 guarda la última versión en una referencia y descarta lo que llega de una foto que ya no
  // se ve: no hace falta memorizarla.
  const onImageError = (error: unknown) => {
    if (isNotFound(error)) {
      if (photoId !== null) setGone((previous) => new Set(previous).add(photoId));
      return;
    }
    counter.current += 1;
    const notice = noticeOf(error, `finding-photo-${String(counter.current)}`);
    if (notice !== null) setNotices((previous) => [...previous, notice]);
  };

  const title = text('episode_photo_panel.finding.title');
  if (current === undefined) {
    return (
      <div className={styles.block} data-part="finding_photo">
        <Heading level={headingLevel}>{title}</Heading>
        <Message textKey="episode_photo_panel.finding.none" />
      </div>
    );
  }

  // Plegada (la ficha): el mismo botón abre y repliega, así que el foco no se pierde al pulsarlo.
  // Replegada, el panel queda montado y oculto: volver a abrirla no pide la imagen otra vez.
  const toggle = folded && (
    <p>
      <button
        type="button"
        className={styles.button}
        data-part="show_photo"
        aria-expanded={unfolded}
        onClick={() => {
          setUnfolded((previous) => !previous);
          setOpened(true);
        }}
      >
        {text(unfolded ? 'episode_photo_panel.finding.hide' : 'episode_photo_panel.finding.show')}
      </button>
    </p>
  );

  if (!unfolded && !opened) {
    return (
      <div className={styles.block} data-part="finding_photo">
        <Heading level={headingLevel}>{title}</Heading>
        {toggle}
      </div>
    );
  }

  const instant = (value: string) => (
    <>
      <TimestampWithOffset instant={value} timeZone={timeZone} />
      {aggregated && (
        <>
          {' '}
          <span data-part="aggregated" data-text-key="episode_photo_panel.aggregated">
            ({text('episode_photo_panel.aggregated')})
          </span>
        </>
      )}
    </>
  );

  const slot = !canView ? (
    <Message textKey="episode_photo_panel.finding.no_permission" />
  ) : gone.has(current.photo_id) ? (
    <Message textKey="episode_photo_panel.finding.gone" />
  ) : current.has_image && !opened ? (
    <div className={styles.placeholder} data-part="photo_placeholder">
      <button
        type="button"
        className={styles.button}
        data-part="show_photo"
        onClick={() => {
          // El botón desaparece con la foto: el foco pasa a la zona de la foto, no al documento.
          focusMedia.current = true;
          setOpened(true);
        }}
      >
        {text('episode_photo_panel.finding.show')}
      </button>
    </div>
  ) : (
    <BlurredEpisodePhoto
      photoId={current.photo_id}
      hasImage={current.has_image}
      absentReason={current.image_absent_reason}
      alt={text(
        current.photo_role === 'peak'
          ? 'episode_photo_panel.finding.alt_peak'
          : 'episode_photo_panel.finding.alt_burst',
      )}
      notice="finding"
      requestImage={requestImage}
      onImageError={onImageError}
    />
  );

  const panel = (
    <EpisodePhotoPanel
      mode="finding"
      photo={current}
      segment={segment}
      durationMs={current.episode_duration_ms}
      instant={instant}
      media={
        <div className={styles.media} ref={mediaRef} tabIndex={-1} data-part="photo_media">
          {sorted.length > 1 && (
            <Navigation
              photos={sorted}
              index={Math.min(index, sorted.length - 1)}
              timeZone={timeZone}
              aggregated={aggregated}
              onChange={setIndex}
            />
          )}
          {slot}
          {notices.length > 0 && (
            <NoticeStack
              notices={notices}
              clock={clock}
              onDismiss={(id) => {
                setNotices((previous) => previous.filter((notice) => notice.id !== id));
              }}
            />
          )}
        </div>
      }
    />
  );

  return (
    <div className={styles.block} data-part="finding_photo">
      <Heading level={headingLevel}>{title}</Heading>
      {toggle}
      {folded ? (
        <div hidden={!unfolded} data-part="photo_fold">
          {panel}
        </div>
      ) : (
        panel
      )}
    </div>
  );
}
