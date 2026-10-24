/**
 * UI-12 `BlurredClipPlayer` (BR-APP-58, NFR-APP-34, PR-APP-17): reproductor de un clip
 * difuminado con el `<video>` nativo, sin descarga, sin imagen en imagen, sin reproducción remota
 * y sin lista de reproducción.
 *
 * - La URL (`POST /evidence/{evidence_id}/read-url`) se pide **solo** al pulsar reproducir, nunca
 *   al montar: una por clip y apertura (una apertura es la vida del componente). Mientras se pide,
 *   otra pulsación no pide otra.
 * - Su vencimiento es el menor entre `expires_at` y 5 minutos desde que llegó, contado con el
 *   `Clock` y el reductor `countdownReducer`; al reanudar con la URL vencida se pide otra y se
 *   sigue desde la misma posición.
 * - La URL vive solo en el `src` del elemento: no entra en el estado de React, `state.query`,
 *   preferencias, almacenamiento ni texto visible, y se quita del elemento al desmontar.
 * - `storage_unavailable` → «evidencia no disponible ahora» con «reintentar», solo en este
 *   reproductor: el resto de la pantalla sigue (BR-APP-74).
 */
import { useCallback, useEffect, useRef, useState, type SyntheticEvent } from 'react';

import { isPortError } from '@/ports/shared';
import { readEvidenceUrl, type ReadUrl } from '@/ports/upload';
import type { Clock } from '@/state/clock';
import {
  countdownReducer,
  EVIDENCE_URL_MAX_SECONDS,
  idleCountdown,
  isCountingDown,
  type CountdownState,
} from '@/state/reducers/countdown';
import { text } from '@/texts';

import styles from './BlurredClipPlayer.module.css';

export interface BlurredClipPlayerProps {
  /** Referencia de la evidencia (clip) que se reproduce. */
  readonly evidenceId: string;
  readonly clock: Clock;
  /** Nombre accesible del reproductor; por omisión, «Clip de evidencia». */
  readonly label?: string;
  /** Puerto de la URL; por omisión `readEvidenceUrl` de `ports.upload`. */
  readonly requestUrl?: (evidenceId: string) => Promise<ReadUrl>;
  /** Evento `readUrlRequested`: se pidió una URL para este clip. */
  readonly onReadUrlRequested?: (evidenceId: string) => void;
}

type Phase =
  | { readonly kind: 'idle' }
  | { readonly kind: 'loading' }
  | { readonly kind: 'ready' }
  | { readonly kind: 'failed'; readonly textKey: string; readonly message: string };

const IDLE: Phase = { kind: 'idle' };

/** Qué se muestra cuando no se pudo obtener la URL. */
function failedPhase(error: unknown): Phase {
  if (isPortError(error)) {
    const { failure } = error;
    if (failure.kind === 'offline') {
      return { kind: 'failed', textKey: 'media.clip.unavailable', message: '' };
    }
    if (failure.kind === 'http' || failure.kind === 'timeout' || failure.kind === 'network') {
      const { behaviour } = failure;
      if (behaviour.kind === 'degraded_section' && behaviour.section === 'storage') {
        return { kind: 'failed', textKey: 'media.clip.unavailable', message: '' };
      }
      return { kind: 'failed', textKey: behaviour.text_key, message: behaviour.message_es };
    }
  }
  return { kind: 'failed', textKey: 'media.clip.failed', message: '' };
}

function startPlayback(video: HTMLVideoElement): void {
  // `play()` rechaza si el navegador lo impide; la persona vuelve a pulsar.
  const playing = video.play() as Promise<void> | undefined;
  playing?.catch(() => undefined);
}

export function BlurredClipPlayer({
  evidenceId,
  clock,
  label,
  requestUrl = readEvidenceUrl,
  onReadUrlRequested,
}: BlurredClipPlayerProps) {
  const videoRef = useRef<HTMLVideoElement>(null);
  /** Vencimiento de la URL que está en el `src` (sin la URL). */
  const expiryRef = useRef<CountdownState>(idleCountdown);
  const pendingRef = useRef(false);
  /** Clip de la apertura en curso: una respuesta de otro clip se descarta. */
  const currentRef = useRef(evidenceId);
  /** Fase del clip `clip`; otro clip empieza en reposo. */
  const [state, setState] = useState<{ readonly clip: string; readonly phase: Phase }>({
    clip: evidenceId,
    phase: IDLE,
  });
  const phase = state.clip === evidenceId ? state.phase : IDLE;

  useEffect(() => {
    currentRef.current = evidenceId;
    expiryRef.current = idleCountdown;
    pendingRef.current = false;
    const video = videoRef.current;
    return () => {
      video?.removeAttribute('src');
    };
  }, [evidenceId]);

  /** Reproduce con la URL vigente o pide una nueva (una sola a la vez). */
  const acquire = useCallback(() => {
    const video = videoRef.current;
    if (video === null) return;
    if (isCountingDown(expiryRef.current, clock.now())) {
      startPlayback(video);
      return;
    }
    if (pendingRef.current) return;
    pendingRef.current = true;
    const clip = evidenceId;
    const position = video.currentTime;
    setState({ clip, phase: { kind: 'loading' } });
    onReadUrlRequested?.(clip);
    requestUrl(clip).then(
      ({ url, expires_at }) => {
        if (currentRef.current !== clip) return;
        pendingRef.current = false;
        const nowMs = clock.now();
        const untilMs = Date.parse(expires_at);
        expiryRef.current = countdownReducer(idleCountdown, {
          type: 'start_until',
          untilMs: Number.isFinite(untilMs) ? untilMs : nowMs + EVIDENCE_URL_MAX_SECONDS * 1000,
          maxSeconds: EVIDENCE_URL_MAX_SECONDS,
          nowMs,
        });
        video.src = url;
        if (position > 0) video.currentTime = position;
        setState({ clip, phase: { kind: 'ready' } });
        startPlayback(video);
      },
      (error: unknown) => {
        if (currentRef.current !== clip) return;
        pendingRef.current = false;
        setState({ clip, phase: failedPhase(error) });
      },
    );
  }, [clock, evidenceId, onReadUrlRequested, requestUrl]);

  /** Reanudar con los controles nativos: con la URL vencida, se detiene y se pide otra. */
  const onPlay = useCallback(
    (event: SyntheticEvent<HTMLVideoElement>) => {
      if (isCountingDown(expiryRef.current, clock.now())) return;
      event.currentTarget.pause();
      acquire();
    },
    [acquire, clock],
  );

  const name = label ?? text('media.clip.label');
  return (
    <figure className={styles.player} aria-label={name}>
      <div className={styles.frame}>
        <video
          ref={videoRef}
          className={styles.video}
          aria-label={name}
          controls
          controlsList="nodownload noremoteplayback noplaybackrate"
          disablePictureInPicture
          disableRemotePlayback
          playsInline
          preload="none"
          onPlay={onPlay}
          onContextMenu={(event) => {
            event.preventDefault();
          }}
        >
          <track kind="captions" />
        </video>
        <span className={styles.overlay} data-part="overlay">
          {text('media.clip.blurred_by_construction')}
        </span>
        {phase.kind === 'idle' || phase.kind === 'loading' ? (
          <button
            type="button"
            className={styles.play}
            onClick={acquire}
            disabled={phase.kind === 'loading'}
            aria-busy={phase.kind === 'loading'}
          >
            {text(phase.kind === 'loading' ? 'media.clip.loading' : 'media.clip.play')}
          </button>
        ) : null}
      </div>
      {phase.kind === 'failed' ? (
        <div className={styles.failure} role="alert" data-text-key={phase.textKey}>
          <p className={styles.message}>{phase.message || text(phase.textKey)}</p>
          <button type="button" className={styles.retry} onClick={acquire}>
            {text('media.clip.retry')}
          </button>
        </div>
      ) : null}
    </figure>
  );
}
