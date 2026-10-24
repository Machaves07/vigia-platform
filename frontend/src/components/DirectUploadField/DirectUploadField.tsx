/**
 * UI-21 `DirectUploadField` (BR-APP-48, PAT-APP-SEG-02, LC-APP-17): elegir archivo, declarar la
 * ausencia de personas si la unidad lo exige, calcular el SHA-256, pedir la concesión a la unidad
 * dueña y subir con un `PUT` directo con progreso; el resultado (`document_ref` o el adjunto) va
 * a la pantalla con `onUploaded`.
 *
 * - La casilla «el adjunto no contiene ninguna persona identificable» solo aparece para los
 *   adjuntos de cierre de U-04; sin marcarla no se pide la concesión. Elegir otro archivo la
 *   desmarca: la declaración es de ese archivo.
 * - Los rechazos `attachment_*` (locales, de la concesión o los que la pantalla recibe al
 *   registrar la entrada, por `notice`) se anclan al campo con su texto del catálogo; el fallo se
 *   expone además con `onFailure` para que la pantalla lo presente con `NoticeStack`.
 * - Ante un fallo del almacén o de la API el archivo elegido se conserva en memoria y se puede
 *   reintentar sin elegirlo de nuevo.
 * - La URL prefirmada nunca llega a este componente (`ports.upload` la usa y la suelta).
 */
import { useId, useState } from 'react';

import { FieldNotice } from '@/components/NoticeStack/NoticeStack';
import type { Notice } from '@/components/NoticeStack/notice';
import {
  attachmentErrorCode,
  isUploadError,
  UPLOAD_RULES,
  uploadFile,
  type PutObject,
  type UploadFailure,
  type UploadResult,
  type UploadTarget,
} from '@/ports/upload';
import { text } from '@/texts';

import styles from './DirectUploadField.module.css';

export type { UploadFailure, UploadResult, UploadTarget } from '@/ports/upload';

export interface DirectUploadFieldProps {
  readonly label: string;
  /** Unidad dueña y destino de la concesión (U-03 documento o U-04 adjunto de cierre). */
  readonly target: UploadTarget;
  /** Evento `uploaded`: el archivo está en el depósito; la pantalla lo registra. */
  readonly onUploaded: (result: UploadResult) => void;
  /** Aviso del servidor anclado a este campo (`noticeForField`); prevalece sobre el local. */
  readonly notice?: Notice | undefined;
  /** El fallo de la subida, para que la pantalla lo presente con `NoticeStack`. */
  readonly onFailure?: (failure: UploadFailure) => void;
  readonly disabled?: boolean;
  /** `PUT` al depósito; solo lo sustituyen las pruebas. */
  readonly putObject?: PutObject;
}

type Phase =
  | { readonly kind: 'idle' }
  | { readonly kind: 'hashing' | 'granting' }
  | { readonly kind: 'uploading'; readonly loaded: number; readonly total: number }
  | { readonly kind: 'done'; readonly filename: string }
  | { readonly kind: 'failed'; readonly failure: UploadFailure };

const IDLE: Phase = { kind: 'idle' };

interface FieldError {
  readonly code: string;
  readonly textKey: string;
  readonly message: string;
  /** El archivo sigue elegido y se puede reintentar. */
  readonly kept: boolean;
}

function failureError(failure: UploadFailure): FieldError {
  switch (failure.kind) {
    case 'attachment': {
      const key = `media.upload.${failure.code}`;
      return { code: failure.code, textKey: key, message: text(key), kept: false };
    }
    case 'empty':
      return {
        code: 'empty',
        textKey: 'media.upload.empty',
        message: text('media.upload.empty'),
        kept: false,
      };
    case 'store':
      return {
        code: 'store',
        textKey: 'media.upload.store_failed',
        message: text('media.upload.store_failed'),
        kept: true,
      };
    case 'port': {
      const { behaviour } = failure;
      const code =
        failure.error.failure.kind === 'http'
          ? failure.error.failure.error.code
          : failure.error.failure.kind;
      return behaviour === null
        ? {
            code,
            textKey: 'errors.internal_error',
            message: text('errors.internal_error'),
            kept: true,
          }
        : { code, textKey: behaviour.text_key, message: behaviour.message_es, kept: true };
    }
  }
}

/** Megabytes enteros del tope, para el texto de ayuda. */
function megabytes(bytes: number): string {
  return String(Math.floor(bytes / (1024 * 1024)));
}

export function DirectUploadField({
  label,
  target,
  onUploaded,
  notice,
  onFailure,
  disabled = false,
  putObject,
}: DirectUploadFieldProps) {
  const id = useId();
  const rules = UPLOAD_RULES[target.unit];
  const [file, setFile] = useState<File | null>(null);
  const [declared, setDeclared] = useState(false);
  const [phase, setPhase] = useState<Phase>(IDLE);

  const busy = phase.kind === 'hashing' || phase.kind === 'granting' || phase.kind === 'uploading';
  const helpId = `${id}-help`;
  const errorId = `${id}-error`;
  const declarationId = `${id}-declaration`;

  // Un rechazo `attachment_*` que la pantalla recibió al registrar se ancla con el texto propio.
  const noticeCode = notice === undefined ? null : attachmentErrorCode(notice.error.detail_code);
  const localError = phase.kind === 'failed' ? failureError(phase.failure) : null;
  const fieldError: FieldError | null =
    noticeCode !== null
      ? {
          code: noticeCode,
          textKey: `media.upload.${noticeCode}`,
          message: text(`media.upload.${noticeCode}`),
          kept: false,
        }
      : notice === undefined
        ? localError
        : null;
  const invalid = notice !== undefined || localError !== null;

  const submit = () => {
    if (file === null || busy) return;
    const chosen = file;
    uploadFile(target, chosen, {
      declared,
      onStage: (stage) => {
        setPhase(
          stage === 'uploading'
            ? { kind: 'uploading', loaded: 0, total: chosen.size }
            : { kind: stage },
        );
      },
      onProgress: ({ loaded, total }) => {
        setPhase({ kind: 'uploading', loaded, total });
      },
      ...(putObject === undefined ? {} : { putObject }),
    }).then(
      (result) => {
        setPhase({ kind: 'done', filename: chosen.name });
        onUploaded(result);
      },
      (error: unknown) => {
        if (!isUploadError(error)) throw error;
        setPhase({ kind: 'failed', failure: error.failure });
        onFailure?.(error.failure);
      },
    );
  };

  return (
    <div className={styles.field}>
      <label htmlFor={id} className={styles.label}>
        {label}
      </label>
      <input
        id={id}
        type="file"
        className={styles.input}
        accept={rules.contentTypes.join(',')}
        disabled={disabled || busy}
        aria-invalid={invalid}
        aria-describedby={fieldError === null ? helpId : `${helpId} ${errorId}`}
        onChange={(event) => {
          setFile(event.target.files?.[0] ?? null);
          setDeclared(false);
          setPhase(IDLE);
        }}
      />
      <span id={helpId} className={styles.help}>
        {text('media.upload.allowed')} {megabytes(rules.maxBytes)} {text('media.upload.megabytes')}
      </span>
      {rules.declarationRequired ? (
        <div className={styles.declaration}>
          <input
            id={declarationId}
            type="checkbox"
            checked={declared}
            required
            aria-required="true"
            disabled={disabled || busy}
            onChange={(event) => {
              setDeclared(event.target.checked);
            }}
          />
          <label htmlFor={declarationId}>{text('media.upload.declaration')}</label>
        </div>
      ) : null}
      <div className={styles.actions}>
        <button
          type="button"
          className={styles.button}
          disabled={disabled || busy || file === null}
          onClick={submit}
        >
          {text(phase.kind === 'failed' ? 'media.upload.retry' : 'media.upload.submit')}
        </button>
        <span role="status" className={styles.status} data-phase={phase.kind}>
          {phase.kind === 'hashing'
            ? text('media.upload.hashing')
            : phase.kind === 'granting'
              ? text('media.upload.granting')
              : phase.kind === 'done'
                ? `${text('media.upload.done')}: ${phase.filename}`
                : ''}
        </span>
      </div>
      {phase.kind === 'uploading' ? (
        <progress
          className={styles.progress}
          aria-label={text('media.upload.progress')}
          value={phase.loaded}
          max={Math.max(phase.total, 1)}
        />
      ) : null}
      {fieldError !== null ? (
        <div
          id={errorId}
          className={styles.error}
          role="alert"
          data-code={fieldError.code}
          data-text-key={fieldError.textKey}
        >
          <p className={styles.message}>{fieldError.message}</p>
          {fieldError.kept && file !== null ? (
            <p className={styles.message}>{text('media.upload.kept')}</p>
          ) : null}
        </div>
      ) : notice !== undefined ? (
        <FieldNotice id={errorId} notice={notice} />
      ) : null}
    </div>
  );
}
