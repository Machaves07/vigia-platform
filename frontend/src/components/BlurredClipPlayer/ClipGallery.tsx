/**
 * Clips de una evidencia (sistema visual v1; VIG-366, S3-16): uno a la vez en 16:9 con su único
 * «Reproducir», y los demás como miniaturas que lo eligen. Antes cada clip era un reproductor de
 * unos 800 × 500 px apilado, con viñetas sueltas.
 *
 * Elegir otra miniatura monta un reproductor nuevo (una apertura por clip, UI-12): ninguna URL se
 * pide al montar ni al elegir, solo al pulsar «Reproducir».
 */
import { useState } from 'react';

import type { ReadUrl } from '@/ports/upload';
import type { Clock } from '@/state/clock';
import { text } from '@/texts';

import { BlurredClipPlayer } from './BlurredClipPlayer';
import styles from './ClipGallery.module.css';

export interface GalleryClip {
  /** Clave estable (p. ej. `clip_id`). */
  readonly key: string;
  readonly evidenceId: string;
  /** Nombre del clip («Clip de evidencia 1 · Antes del evento · Cámara 1»). */
  readonly label: string;
}

export function ClipGallery({
  clips,
  clock,
  requestUrl,
}: {
  readonly clips: readonly GalleryClip[];
  readonly clock: Clock;
  readonly requestUrl?: (evidenceId: string) => Promise<ReadUrl>;
}) {
  const [chosen, setChosen] = useState(0);
  const current = clips[Math.min(chosen, clips.length - 1)];
  if (current === undefined) return null;
  return (
    <div className={styles.gallery} data-part="clip_gallery">
      <BlurredClipPlayer
        key={current.key}
        evidenceId={current.evidenceId}
        clock={clock}
        label={current.label}
        {...(requestUrl === undefined ? {} : { requestUrl })}
      />
      {clips.length > 1 && (
        <ul className={styles.thumbs} aria-label={text('media.clip.others')}>
          {clips.map((clip, index) => (
            <li key={clip.key} data-part="clip">
              <button
                type="button"
                className={styles.thumb}
                aria-pressed={clip.key === current.key}
                onClick={() => {
                  setChosen(index);
                }}
              >
                <span className={styles.frame} aria-hidden="true">
                  <svg viewBox="0 0 16 16" focusable="false" fill="currentColor">
                    <path d="M5 3.5v9l7-4.5z" />
                  </svg>
                </span>
                <span>{clip.label}</span>
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
