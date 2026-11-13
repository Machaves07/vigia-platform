/**
 * UI-26 `BlurredEpisodePhoto` (TASK-574; A-112, «Precisiones del 13-11, lectura y purga»;
 * BR-APP-58): la foto difuminada de un episodio de la sombra, o el motivo por el que no la tiene.
 *
 * - **Cómo llega.** La imagen se pide con `fetch` por el transporte del cliente (`ports.base`),
 *   que añade `X-Vigia-Concession`: un `<img src>` no puede enviarla, y la instaladora bajo
 *   concesión recibiría `not_found` y dejaría una entrada `denied`. Se decodifica con
 *   `createImageBitmap(blob)` y se pinta en un `<canvas role="img">` con su texto alternativo:
 *   ninguna URL de objeto, ningún `<img>` y la política de contenido sigue en `img-src 'self'`.
 * - **Una petición por apertura.** Cada petición deja una entrada `episode_photo_read`: se pide
 *   solo al montar el detalle, sin reintentos (el puerto la declara `retry: false`), sin recarga
 *   al enfocar ni caché, y el doble montaje de `StrictMode` no la repite. Volver a pedirla es un
 *   gesto de la persona («Volver a pedir la imagen»). Al desmontar se cancela la petición en vuelo
 *   y se cierra el mapa de bits.
 * - **Sin descarga.** Ningún enlace, `download` ni botón de guardar; el lienzo no es arrastrable
 *   y el menú contextual no se abre sobre la foto.
 * - **Textos.** Superposición «Imagen difuminada automáticamente» (nunca «por construcción»: un
 *   tercero no detectado puede salir nítido, riesgo R1) y aviso fijo de material de puesta en
 *   marcha. Sin imagen, el motivo etiquetado (`image_absent_reason`) y ninguna petición.
 */
import { useEffect, useLayoutEffect, useRef, useState } from 'react';

import { readEpisodePhotoImage } from '@/ports/scr06EpisodePhotos';
import { isPortError } from '@/ports/shared';
import { label } from '@/texts/labels';
import { text } from '@/texts';

import styles from './BlurredEpisodePhoto.module.css';

/** El único tipo que se pinta: lo que no es `image/jpeg` no llega al lienzo. */
const JPEG = 'image/jpeg';

export interface BlurredEpisodePhotoProps {
  /** Episodio cuya foto se muestra (`photo_id`). */
  readonly photoId: string;
  /** `true` si el episodio llegó con imagen (`image` no nulo). */
  readonly hasImage: boolean;
  /** `image_absent_reason` del episodio; se muestra etiquetado si no hay imagen. */
  readonly absentReason: string | null;
  /** Texto alternativo del lienzo, ya resuelto por la pantalla. */
  readonly alt: string;
  /** Puerto de la imagen; por omisión, `readEpisodePhotoImage` (una petición, sin reintentos). */
  readonly requestImage?: (photoId: string, signal: AbortSignal) => Promise<Blob>;
  /** Decodificador; por omisión, `createImageBitmap` del navegador. */
  readonly decodeImage?: (blob: Blob) => Promise<ImageBitmap>;
  /** La imagen no se pudo pedir o pintar; la pantalla decide el aviso (UI-19) o BR-APP-77. */
  readonly onImageError?: (error: unknown) => void;
}

type Phase = 'loading' | 'ready' | 'failed';

function decodeWithBrowser(blob: Blob): Promise<ImageBitmap> {
  return createImageBitmap(blob);
}

function aborted(error: unknown): boolean {
  return isPortError(error) && error.failure.kind === 'aborted';
}

function preventDefault(event: Event): void {
  event.preventDefault();
}

/** Eventos anulados sobre el marco de la foto: ni menú contextual («guardar imagen») ni arrastre. */
const BLOCKED_EVENTS = ['contextmenu', 'dragstart'] as const;

export function BlurredEpisodePhoto({
  photoId,
  hasImage,
  absentReason,
  alt,
  requestImage = readEpisodePhotoImage,
  decodeImage = decodeWithBrowser,
  onImageError,
}: BlurredEpisodePhotoProps) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const frameRef = useRef<HTMLDivElement>(null);
  /** Petición de la apertura en curso (`photoId#intento`): una por apertura, también en StrictMode. */
  const requestedRef = useRef<string | null>(null);
  const controllerRef = useRef<AbortController | null>(null);
  /** El componente sigue montado (el doble montaje de StrictMode lo vuelve a poner a `true`). */
  const liveRef = useRef(true);
  const onErrorRef = useRef(onImageError);
  useLayoutEffect(() => {
    onErrorRef.current = onImageError;
  });
  const [attempt, setAttempt] = useState(0);
  const key = `${photoId}#${String(attempt)}`;
  const [state, setState] = useState<{ readonly key: string; readonly phase: Phase }>({
    key,
    phase: 'loading',
  });
  const phase: Phase = state.key === key ? state.phase : 'loading';

  useEffect(() => {
    const frame = frameRef.current;
    if (frame === null) return undefined;
    for (const name of BLOCKED_EVENTS) frame.addEventListener(name, preventDefault);
    return () => {
      for (const name of BLOCKED_EVENTS) frame.removeEventListener(name, preventDefault);
    };
  }, []);

  useEffect(() => {
    liveRef.current = true;
    if (hasImage && requestedRef.current !== key) {
      requestedRef.current = key;
      controllerRef.current?.abort();
      const controller = new AbortController();
      controllerRef.current = controller;
      const current = () => liveRef.current && requestedRef.current === key;
      const fail = (error: unknown) => {
        if (!current() || aborted(error)) return;
        setState({ key, phase: 'failed' });
        onErrorRef.current?.(error);
      };
      requestImage(photoId, controller.signal)
        .then((blob) => {
          if (blob.type !== JPEG) throw new TypeError('La imagen del episodio no es image/jpeg.');
          return decodeImage(blob);
        })
        .then((bitmap) => {
          try {
            if (!current()) return;
            const canvas = canvasRef.current;
            const context = canvas?.getContext('2d') ?? null;
            if (canvas === null || context === null) {
              fail(new TypeError('Sin lienzo donde pintar la imagen del episodio.'));
              return;
            }
            canvas.width = bitmap.width;
            canvas.height = bitmap.height;
            context.drawImage(bitmap, 0, 0);
            setState({ key, phase: 'ready' });
          } finally {
            bitmap.close();
          }
        }, fail);
    }
    return () => {
      liveRef.current = false;
      const controller = controllerRef.current;
      // StrictMode vuelve a montar en el acto: solo un desmontaje real llega a cancelar.
      queueMicrotask(() => {
        if (!liveRef.current) controller?.abort();
      });
    };
  }, [key, photoId, hasImage, requestImage, decodeImage]);

  return (
    <figure className={styles.photo} aria-label={text('media.episode_photo.label')}>
      <p className={styles.notice} data-text-key="media.episode_photo.commissioning_notice">
        {text('media.episode_photo.commissioning_notice')}
      </p>
      <div ref={frameRef} className={styles.frame} data-part="photo_frame">
        {hasImage ? (
          <canvas
            ref={canvasRef}
            className={styles.canvas}
            // Lienzo de solo lectura: ARIA en HTML admite cualquier rol en <canvas> y aquí es una
            // imagen con su texto alternativo (A-112).
            // eslint-disable-next-line jsx-a11y/no-interactive-element-to-noninteractive-role
            role="img"
            aria-label={alt}
            data-part="episode_canvas"
            data-phase={phase}
            hidden={phase !== 'ready'}
            draggable={false}
          />
        ) : (
          <p className={styles.absent} data-text-key="media.episode_photo.absent">
            {text('media.episode_photo.absent')}{' '}
            <span data-part="absent_reason">
              {label('image_absent_reason', absentReason ?? '')}
            </span>
          </p>
        )}
        {hasImage && phase === 'loading' ? (
          <p className={styles.status} role="status" data-text-key="media.episode_photo.loading">
            {text('media.episode_photo.loading')}
          </p>
        ) : null}
        <span
          className={styles.overlay}
          data-part="overlay"
          data-text-key="media.episode_photo.blurred_automatically"
        >
          {text('media.episode_photo.blurred_automatically')}
        </span>
      </div>
      {hasImage && phase === 'failed' ? (
        <div className={styles.failure} data-text-key="media.episode_photo.unavailable">
          <p className={styles.message}>{text('media.episode_photo.unavailable')}</p>
          <button
            type="button"
            className={styles.retry}
            onClick={() => {
              setAttempt((value) => value + 1);
            }}
          >
            {text('media.episode_photo.request_again')}
          </button>
        </div>
      ) : null}
    </figure>
  );
}
