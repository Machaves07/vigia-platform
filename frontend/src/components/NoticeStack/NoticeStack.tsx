/**
 * UI-19 `NoticeStack` (BR-APP-43, 71 a 78; NFR-APP-41; PR-APP-22).
 *
 * Presenta cada `Notice` con el comportamiento del mapa cerrado `behaviourFor`: los
 * `invalid_request` y `conflict` con campo van anclados al formulario (`FieldNotice`, que pinta
 * el campo) y el resto en la pila global. Los avisos nunca son modales ni deshabilitan la
 * navegación o las acciones de la pantalla (BR-APP-43); solo «Repetir» espera exactamente el
 * `retry_after_seconds` del servidor, con el reductor puro de cuenta regresiva y el `Clock`
 * inyectado. El `correlation_id` se muestra copiable y nunca se escribe en la URL (BR-APP-75).
 */
import { useEffect, useReducer, useState } from 'react';

import { behaviourFor, type Behaviour } from '@/ports/errors/map';
import type { Clock } from '@/state/clock';
import {
  countdownReducer,
  idleCountdown,
  isCountingDown,
  remainingSeconds,
} from '@/state/reducers/countdown';
import { text } from '@/texts';

import { correlationOf, isAnchored, type Notice } from './notice';
import styles from './NoticeStack.module.css';

export interface NoticeStackProps {
  readonly notices: readonly Notice[];
  readonly clock: Clock;
  readonly onDismiss: (id: string) => void;
  /** Sin `onRetry` no se ofrece «Repetir». */
  readonly onRetry?: (id: string) => void;
}

const TICK_MS = 1000;

/** El `correlation_id` con la indicación de reporte y la acción de copiar. */
function CorrelationCopy({ value }: { readonly value: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <p className={styles.correlation} data-part="correlation">
      <span>{text('components_forms.notice.correlation_label')}: </span>
      <code data-part="correlation_id">{value}</code>{' '}
      <button
        type="button"
        className={styles.button}
        onClick={() => {
          void navigator.clipboard.writeText(value).then(() => {
            setCopied(true);
          });
        }}
      >
        {text('components_forms.notice.copy')}
      </button>
      <span role="status" className={styles.copied}>
        {copied ? text('components_forms.notice.copied') : ''}
      </span>
    </p>
  );
}

function hasCountdown(behaviour: Behaviour): number | null {
  return behaviour.kind === 'countdown' || behaviour.kind === 'transient'
    ? behaviour.retry_after_seconds
    : null;
}

function RetryButton({
  seconds,
  clock,
  onRetry,
}: {
  readonly seconds: number | null;
  readonly clock: Clock;
  readonly onRetry: () => void;
}) {
  const [state, dispatch] = useReducer(countdownReducer, idleCountdown, (initial) =>
    countdownReducer(initial, { type: 'start', seconds, nowMs: clock.now() }),
  );
  const [, setTick] = useState(0);
  useEffect(() => {
    if (state.kind !== 'running') return undefined;
    const handle = clock.setTimeout(
      () => {
        dispatch({ type: 'tick', nowMs: clock.now() });
        setTick((value) => value + 1);
      },
      Math.min(TICK_MS, Math.max(0, state.untilMs - clock.now())),
    );
    return () => {
      clock.clearTimeout(handle);
    };
  });
  const nowMs = clock.now();
  const waiting = isCountingDown(state, nowMs);
  return (
    <>
      <button
        type="button"
        className={styles.button}
        disabled={waiting}
        onClick={onRetry}
        data-part="retry"
      >
        {text('components_forms.notice.retry')}
      </button>
      {waiting ? (
        <span data-part="countdown">
          {`${text('components_forms.notice.retry_in')} ${String(remainingSeconds(state, nowMs))} ${text('components_forms.notice.seconds')}`}
        </span>
      ) : null}
    </>
  );
}

function NoticeItem({
  notice,
  clock,
  onDismiss,
  onRetry,
}: {
  readonly notice: Notice;
  readonly clock: Clock;
  readonly onDismiss: (id: string) => void;
  readonly onRetry: ((id: string) => void) | undefined;
}) {
  const behaviour = behaviourFor(notice.error);
  const correlation = correlationOf(notice);
  const retryable =
    behaviour.kind === 'countdown' ||
    behaviour.kind === 'transient' ||
    behaviour.kind === 'internal';
  return (
    <li
      className={styles.notice}
      data-kind={behaviour.kind}
      data-code={notice.error.code}
      data-text-key={behaviour.text_key}
    >
      <p className={styles.message} data-part="message">
        {behaviour.message_es}
      </p>
      {correlation === null ? null : <CorrelationCopy value={correlation} />}
      <div className={styles.actions}>
        {retryable && onRetry !== undefined ? (
          <RetryButton
            key={notice.id}
            seconds={hasCountdown(behaviour)}
            clock={clock}
            onRetry={() => {
              onRetry(notice.id);
            }}
          />
        ) : null}
        <button
          type="button"
          className={styles.button}
          onClick={() => {
            onDismiss(notice.id);
          }}
        >
          {text('components_forms.notice.dismiss')}
        </button>
      </div>
    </li>
  );
}

/** Pila global: los avisos anclados a un campo los pinta su `FieldNotice`. */
export function NoticeStack({ notices, clock, onDismiss, onRetry }: NoticeStackProps) {
  const global = notices.filter((notice) => !isAnchored(notice));
  return (
    <section
      className={styles.stack}
      aria-label={text('components_forms.notice.region_label')}
      aria-live="polite"
    >
      {global.length === 0 ? null : (
        <ul className={styles.list}>
          {global.map((notice) => (
            <NoticeItem
              key={notice.id}
              notice={notice}
              clock={clock}
              onDismiss={onDismiss}
              onRetry={onRetry}
            />
          ))}
        </ul>
      )}
    </section>
  );
}

export interface FieldNoticeProps {
  readonly id: string;
  readonly notice: Notice;
}

/**
 * Aviso anclado a un campo (BR-APP-78): el mensaje del servidor (o la etiqueta de su
 * `detail_code`) junto al campo, que lo referencia con `aria-describedby`; lo escrito se conserva
 * porque el aviso no toca el valor del campo.
 */
export function FieldNotice({ id, notice }: FieldNoticeProps) {
  const behaviour = behaviourFor(notice.error);
  const correlation = correlationOf(notice);
  return (
    <div
      id={id}
      className={styles.field}
      role="alert"
      data-code={notice.error.code}
      data-text-key={behaviour.text_key}
    >
      <p className={styles.message} data-part="message">
        {behaviour.message_es}
      </p>
      {correlation === null ? null : <CorrelationCopy value={correlation} />}
    </div>
  );
}
