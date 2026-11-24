/**
 * Vista previa de una foto adjunta a la evidencia del cierre (TASK-609; O-22; A-112, «Precisiones
 * del 13-11»; BR-APP-58): un JPEG o PNG que se **mira** en la ficha de vida sin incrustarlo.
 *
 * - **A demanda.** Montar el componente no pide nada. «Ver foto» pide la URL del adjunto
 *   (`POST /loop/attachments/{attachment_id}/read-url`, `closure_attachments.read`; cada concesión
 *   deja su entrada `closure_attachment_read_granted`), la usa una sola vez para el `fetch` y la
 *   suelta: no entra en el estado, en el DOM ni en almacenamiento alguno. Sin reintentos
 *   automáticos: volver a pedirla es otro gesto de la persona («Volver a pedir la foto»).
 * - **Sin `<img>`.** Los bytes se decodifican con `createImageBitmap` y se pintan en un
 *   `<canvas role="img">` con su texto alternativo, como UI-26: ninguna URL de objeto (`blob:`) y
 *   la política de contenido sigue en `img-src 'self'`, que en AWS no admite el origen del
 *   depósito (el `fetch` va por `connect-src`). El `fetch` sale sin credenciales ni referente: la
 *   URL firmada es la única autorización.
 * - **Sin descarga.** Ningún enlace, `download` ni botón de guardar; el lienzo no es arrastrable,
 *   el menú contextual no se abre sobre la foto y una capa transparente lo cubre.
 * - **Fallos, solo en este bloque.** Un error de la concesión da el texto del mapa cerrado
 *   (`errors.storage_unavailable`, «no disponible en su alcance» de BR-APP-77…); un objeto que no
 *   llega, no es una imagen o no se decodifica, «Adjunto no disponible ahora».
 */
import { useEffect, useRef, useState } from 'react';

import { isPortError } from '@/ports/shared';
import {
  readClosureAttachmentObject,
  readClosureAttachmentUrl,
  type ReadUrl,
} from '@/ports/upload';
import type { Clock } from '@/state/clock';
import { text } from '@/texts';

import styles from './AttachmentPreview.module.css';

/** Los tipos que se pintan: los demás adjuntos (PDF) se descargan con `AttachmentReadLink`. */
const IMAGE_TYPES: ReadonlySet<string> = new Set(['image/jpeg', 'image/png']);

/** Tope del `fetch` al almacén (BR-APP-61: ninguna petición sin tope) `[objetivo propio]`. */
const FETCH_TIMEOUT_MS = 60_000;

const UNAVAILABLE = 'media.attachment.unavailable';

export interface AttachmentPreviewProps {
  readonly attachmentId: string;
  /** Texto alternativo del lienzo, ya resuelto por la pantalla. */
  readonly alt: string;
  readonly clock: Clock;
  /** Puerto de la URL; por omisión `readClosureAttachmentUrl` de `ports.upload`. */
  readonly requestUrl?: (attachmentId: string) => Promise<ReadUrl>;
  /** Bytes desde la URL firmada; por omisión `readClosureAttachmentObject` (sin credenciales). */
  readonly fetchObject?: (url: string, signal: AbortSignal) => Promise<Blob>;
  /** Decodificador; por omisión, `createImageBitmap` del navegador. */
  readonly decodeImage?: (blob: Blob) => Promise<ImageBitmap>;
}

type Phase =
  | { readonly kind: 'idle' | 'loading' | 'ready' }
  | { readonly kind: 'failed'; readonly textKey: string; readonly message: string };

/** Solo `https:` (o `http:` si la propia aplicación se sirve así, en desarrollo). */
function grantedUrl(url: string): string | null {
  try {
    const parsed = new URL(url);
    const page = globalThis.location.protocol;
    return parsed.protocol === 'https:' || (parsed.protocol === 'http:' && page === 'http:')
      ? parsed.href
      : null;
  } catch {
    return null;
  }
}

function decodeWithBrowser(blob: Blob): Promise<ImageBitmap> {
  return createImageBitmap(blob);
}

/** El texto del fallo de la concesión: el del mapa cerrado (BR-APP-71, 77). */
function grantFailure(error: unknown): Phase {
  if (isPortError(error)) {
    const { failure } = error;
    if (failure.kind === 'http' || failure.kind === 'timeout' || failure.kind === 'network') {
      const { behaviour } = failure;
      return { kind: 'failed', textKey: behaviour.text_key, message: behaviour.message_es };
    }
  }
  return { kind: 'failed', textKey: UNAVAILABLE, message: '' };
}

function preventDefault(event: Event): void {
  event.preventDefault();
}

/** Eventos anulados sobre el marco de la foto: ni menú contextual («guardar imagen») ni arrastre. */
const BLOCKED_EVENTS = ['contextmenu', 'dragstart'] as const;

class ObjectUnavailable extends Error {}

export function AttachmentPreview({
  attachmentId,
  alt,
  clock,
  requestUrl = readClosureAttachmentUrl,
  fetchObject = readClosureAttachmentObject,
  decodeImage = decodeWithBrowser,
}: AttachmentPreviewProps) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const frameRef = useRef<HTMLDivElement>(null);
  const controllerRef = useRef<AbortController | null>(null);
  /** El intento en curso: lo que llega de uno anterior (o tras desmontar) se descarta. */
  const attemptRef = useRef(0);
  const [phase, setPhase] = useState<Phase>({ kind: 'idle' });

  useEffect(() => {
    const frame = frameRef.current;
    if (frame === null) return undefined;
    for (const name of BLOCKED_EVENTS) frame.addEventListener(name, preventDefault);
    return () => {
      for (const name of BLOCKED_EVENTS) frame.removeEventListener(name, preventDefault);
    };
  }, [phase.kind]);

  useEffect(
    () => () => {
      attemptRef.current += 1;
      controllerRef.current?.abort();
    },
    [],
  );

  /** Un gesto, una URL: nunca se pide sola. */
  const show = () => {
    attemptRef.current += 1;
    const attempt = attemptRef.current;
    const current = () => attemptRef.current === attempt;
    controllerRef.current?.abort();
    const controller = new AbortController();
    controllerRef.current = controller;
    setPhase({ kind: 'loading' });
    const timer = clock.setTimeout(() => {
      controller.abort();
    }, FETCH_TIMEOUT_MS);
    const settle = () => {
      clock.clearTimeout(timer);
    };
    requestUrl(attachmentId)
      .then(({ url }) => {
        const granted = grantedUrl(url);
        if (granted === null) throw new ObjectUnavailable('URL de lectura no admitida.');
        return fetchObject(granted, controller.signal).catch(() => {
          throw new ObjectUnavailable('El almacén no entregó el adjunto.');
        });
      })
      .then((blob) => {
        if (!IMAGE_TYPES.has(blob.type)) throw new ObjectUnavailable('El adjunto no es una foto.');
        return decodeImage(blob).catch(() => {
          throw new ObjectUnavailable('La foto no se pudo decodificar.');
        });
      })
      .then(
        (bitmap) => {
          settle();
          try {
            if (!current()) return;
            const canvas = canvasRef.current;
            const context = canvas?.getContext('2d') ?? null;
            if (canvas === null || context === null) {
              setPhase({ kind: 'failed', textKey: UNAVAILABLE, message: '' });
              return;
            }
            canvas.width = bitmap.width;
            canvas.height = bitmap.height;
            context.drawImage(bitmap, 0, 0);
            setPhase({ kind: 'ready' });
          } finally {
            bitmap.close();
          }
        },
        (error: unknown) => {
          settle();
          if (!current()) return;
          setPhase(
            error instanceof ObjectUnavailable
              ? { kind: 'failed', textKey: UNAVAILABLE, message: '' }
              : grantFailure(error),
          );
        },
      );
  };

  const opened = phase.kind !== 'idle';
  return (
    <figure className={styles.preview} aria-label={alt} data-part="attachment_preview">
      {opened ? (
        <div ref={frameRef} className={styles.frame} data-part="attachment_frame">
          <canvas
            ref={canvasRef}
            className={styles.canvas}
            // Lienzo de solo lectura: ARIA en HTML admite cualquier rol en <canvas> y aquí es una
            // imagen con su texto alternativo (A-112).
            // eslint-disable-next-line jsx-a11y/no-interactive-element-to-noninteractive-role
            role="img"
            aria-label={alt}
            data-part="attachment_canvas"
            data-phase={phase.kind}
            hidden={phase.kind !== 'ready'}
            draggable={false}
          />
          {phase.kind === 'loading' ? (
            <p className={styles.status} role="status" data-text-key="media.attachment.loading">
              {text('media.attachment.loading')}
            </p>
          ) : null}
          {/* Capa transparente sobre el lienzo: el clic derecho cae en ella y no en la foto. */}
          <span className={styles.shield} aria-hidden="true" data-part="shield" />
        </div>
      ) : (
        <button type="button" className={styles.button} data-part="show_attachment" onClick={show}>
          {text('media.attachment.show_photo')}
        </button>
      )}
      {phase.kind === 'failed' ? (
        <div className={styles.failure} role="alert" data-text-key={phase.textKey}>
          <p className={styles.message}>{phase.message || text(phase.textKey)}</p>
          <button
            type="button"
            className={styles.button}
            data-part="request_attachment_again"
            onClick={show}
          >
            {text('media.attachment.request_again')}
          </button>
        </div>
      ) : null}
    </figure>
  );
}
