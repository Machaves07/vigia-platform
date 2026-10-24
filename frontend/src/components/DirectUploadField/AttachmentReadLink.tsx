/**
 * Lectura de un adjunto de cierre (SUP-U05-10, nota U05-H-12 de Infrastructure Design): el
 * adjunto **nunca se incrusta** en la página (`img-src 'self'` no cambia); se abre por navegación
 * en pestaña nueva con su URL prefirmada, que U-04 sirve con `Content-Disposition: attachment`.
 *
 * En dos pasos, como la vista en vivo (decisión D-1, nota U05-H-02 de UI-18): «preparar el
 * enlace» pide la URL y muestra un enlace `target="_blank"` con `rel="noopener noreferrer"` que
 * la persona pulsa; no hay `window.open` automático. El enlace desaparece del DOM al pulsarlo o
 * al vencer (el menor entre `expires_at` y 15 minutos, contado con el `Clock`).
 */
import { useEffect, useState } from 'react';

import { isPortError } from '@/ports/shared';
import { readClosureAttachmentUrl, type ReadUrl } from '@/ports/upload';
import type { Clock } from '@/state/clock';
import {
  countdownReducer,
  idleCountdown,
  scheduleCountdownEnd,
  type CountdownState,
} from '@/state/reducers/countdown';
import { text } from '@/texts';

import styles from './DirectUploadField.module.css';

/** Vigencia máxima de la URL de un adjunto de cierre (SUP-U05-10). */
const ATTACHMENT_URL_MAX_SECONDS = 900;

export interface AttachmentReadLinkProps {
  readonly attachmentId: string;
  readonly clock: Clock;
  /** Nombre del adjunto para el enlace; por omisión, el texto genérico. */
  readonly label?: string;
  /** Puerto de la URL; por omisión `readClosureAttachmentUrl` de `ports.upload`. */
  readonly requestUrl?: (attachmentId: string) => Promise<ReadUrl>;
}

type State =
  | { readonly kind: 'idle' | 'loading' }
  | { readonly kind: 'ready'; readonly url: string; readonly countdown: CountdownState }
  | { readonly kind: 'failed'; readonly textKey: string; readonly message: string };

const IDLE: State = { kind: 'idle' };

function failed(error: unknown): State {
  if (isPortError(error)) {
    const { failure } = error;
    if (failure.kind === 'http' || failure.kind === 'timeout' || failure.kind === 'network') {
      const { behaviour } = failure;
      if (behaviour.kind === 'degraded_section' && behaviour.section === 'storage') {
        return { kind: 'failed', textKey: 'media.attachment.unavailable', message: '' };
      }
      return { kind: 'failed', textKey: behaviour.text_key, message: behaviour.message_es };
    }
    if (failure.kind === 'offline') {
      return { kind: 'failed', textKey: 'media.attachment.unavailable', message: '' };
    }
  }
  return { kind: 'failed', textKey: 'media.attachment.failed', message: '' };
}

export function AttachmentReadLink({
  attachmentId,
  clock,
  label,
  requestUrl = readClosureAttachmentUrl,
}: AttachmentReadLinkProps) {
  const [state, setState] = useState<State>(IDLE);

  // Al vencer, el enlace sale del DOM.
  const countdown = state.kind === 'ready' ? state.countdown : idleCountdown;
  useEffect(
    () =>
      scheduleCountdownEnd(clock, countdown, () => {
        setState(IDLE);
      }),
    [clock, countdown],
  );

  const prepare = () => {
    setState({ kind: 'loading' });
    requestUrl(attachmentId).then(
      ({ url, expires_at }) => {
        const nowMs = clock.now();
        const untilMs = Date.parse(expires_at);
        const next = countdownReducer(idleCountdown, {
          type: 'start_until',
          untilMs: Number.isFinite(untilMs) ? untilMs : nowMs,
          maxSeconds: ATTACHMENT_URL_MAX_SECONDS,
          nowMs,
        });
        setState(next.kind === 'running' ? { kind: 'ready', url, countdown: next } : IDLE);
      },
      (error: unknown) => {
        setState(failed(error));
      },
    );
  };

  if (state.kind === 'ready') {
    return (
      <span className={styles.attachment}>
        <a
          className={styles.link}
          href={state.url}
          target="_blank"
          rel="noopener noreferrer"
          onClick={() => {
            // La navegación ya salió con el `href`: el enlace se retira en el mismo tic.
            setState(IDLE);
          }}
        >
          {label ?? text('media.attachment.open')}
        </a>
      </span>
    );
  }
  return (
    <span className={styles.attachment}>
      <button
        type="button"
        className={styles.button}
        disabled={state.kind === 'loading'}
        aria-busy={state.kind === 'loading'}
        onClick={prepare}
      >
        {text('media.attachment.prepare')}
      </button>
      {state.kind === 'failed' ? (
        <span role="alert" className={styles.error} data-text-key={state.textKey}>
          {state.message || text(state.textKey)}
        </span>
      ) : null}
    </span>
  );
}
