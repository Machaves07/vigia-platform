/**
 * Evidencia estructurada y composición de un máximo de cinco adjuntos declarados. Registrada, el
 * formulario se vacía y un acuse dice qué, cuándo y qué cargo, con el foco y el siguiente paso; un
 * envío igual al último de la misma acción pide confirmación antes de salir, porque el expediente
 * no se borra (O-17).
 */
import { formatInTimeZone } from 'date-fns-tz';
import { DateTimeInput } from '@/components/DateFields/DateFields';
import { useEffect, useRef, useState } from 'react';
import {
  DirectUploadField,
  type UploadFailure,
} from '@/components/DirectUploadField/DirectUploadField';
import evidenceBody from '../../../../../openapi/bodies/add_evidence_loop_actions__action_id__evidence_post.json';
import { reasonLimits } from '@/components/ReasonField/limits';
import {
  optionalText,
  ReasonField,
  reasonIssueKey,
  submittedText,
} from '@/components/ReasonField/ReasonField';
import { textFields } from '@/components/ReasonField/TextNotice';
import { TimestampWithOffset } from '@/components/TimestampWithOffset/TimestampWithOffset';
import {
  closureNotice,
  closurePort,
  recordContent,
  type Detail,
  type EvidenceBody,
  type LedgerRecord,
} from '@/ports/scr09Closure';
import { useScreenMutation } from '@/state/query';
import { text } from '@/texts';
import { Failure } from './common';
import { executionInstant } from './dates';
import styles from './Closure.module.css';

/**
 * Límites de los textos del lazo que el esquema generado no publica (motivo de la reapertura,
 * declaración y revisión del incerrable, de 20 a 2000): la prueba los compara por igualdad con sus
 * rutas del corpus `u05-fixtures.json` (`free_text.routes`). El servidor decide.
 */
export const LONG_TEXT = { minLength: 20, maxLength: 2000 };

/** Una evidencia de una acción, con lo que la hace igual a otra (O-17). */
interface EvidenceEntry {
  readonly description: string;
  readonly performedAt: string;
  readonly role: string;
  readonly verification: string | null;
  /** Cuándo la registró el servidor. */
  readonly addedAt: string;
}

function textOf(value: unknown): string {
  return typeof value === 'string' ? value : '';
}

/** La última evidencia de `actionId` en el expediente ya consultado, en el orden de la cadena. */
function recordedEvidence(
  records: readonly LedgerRecord[],
  actionId: string,
): EvidenceEntry | null {
  const last = records
    .filter(
      (record) =>
        record.record_type === 'action_evidence_added' &&
        recordContent(record).action_id === actionId,
    )
    .sort((a, b) => a.chain_sequence - b.chain_sequence)
    .at(-1);
  if (last === undefined) return null;
  const content = recordContent(last);
  return {
    description: textOf(content.description),
    performedAt: textOf(content.performed_at),
    role: textOf(content.performed_by_role),
    verification: typeof content.verification_note === 'string' ? content.verification_note : null,
    addedAt: typeof content.added_at === 'string' ? content.added_at : last.received_at,
  };
}

/** La más reciente de las candidatas (la registrada desde esta página o la del expediente). */
function latest(entries: readonly (EvidenceEntry | null | undefined)[]): EvidenceEntry | null {
  let found: EvidenceEntry | null = null;
  for (const entry of entries) {
    if (entry === null || entry === undefined) continue;
    if (found === null || Date.parse(entry.addedAt) > Date.parse(found.addedAt)) found = entry;
  }
  return found;
}

/** Igual: descripción recortada, hora de ejecución, cargo y nota (los adjuntos no cuentan). */
function sameEvidence(a: Omit<EvidenceEntry, 'addedAt'>, b: EvidenceEntry): boolean {
  return (
    a.description.trim() === b.description.trim() &&
    Date.parse(a.performedAt) === Date.parse(b.performedAt) &&
    a.role.trim() === b.role.trim() &&
    (a.verification ?? '').trim() === (b.verification ?? '').trim()
  );
}

export function Evidence({
  detail,
  timeZone,
  records,
  onWritten,
  onNextStep,
}: {
  readonly detail: Detail;
  readonly timeZone: string;
  /** El expediente que la página ya consultó: la última evidencia de cada acción (O-17). */
  readonly records: readonly LedgerRecord[];
  readonly onWritten: () => void;
  /** Lleva al siguiente paso («Cerrar y firmar»), si la persona puede darlo. */
  readonly onNextStep?: () => void;
}) {
  const [action, setAction] = useState(detail.actions.at(-1)?.action_id ?? '');
  const [description, setDescription] = useState('');
  const [verification, setVerification] = useState('');
  const [performed, setPerformed] = useState('');
  const [role, setRole] = useState('');
  const [attachments, setAttachments] = useState<NonNullable<EvidenceBody['attachments']>>([]);
  const [degraded, setDegraded] = useState(false);
  const [invalid, setInvalid] = useState(false);
  const [uploadError, setUploadError] = useState<Error | null>(null);
  const [uploading, setUploading] = useState(false);
  // Lo registrado desde esta página, por acción: el expediente no se vuelve a pedir solo (A-6).
  const [sent, setSent] = useState<Readonly<Record<string, EvidenceEntry>>>({});
  const [receipt, setReceipt] = useState<EvidenceEntry | null>(null);
  const [repeat, setRepeat] = useState<EvidenceEntry | null>(null);
  const receiptRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (receipt !== null) receiptRef.current?.focus();
  }, [receipt]);
  const [limits] = useState(() => ({
    description: reasonLimits(evidenceBody, 'description'),
    verification: reasonLimits(evidenceBody, 'verification_note'),
  }));
  const mutation = useScreenMutation({
    write: (body: EvidenceBody) =>
      closurePort.evidence.write({ path: { action_id: action }, body }),
    invalidates: { screen: 'scr09', resources: ['actions', 'findings', 'ledger_records'] },
  });
  const notice = closureNotice(mutation.error, {
    loop_attachment_metadata_not_allowed: 'attachments',
    loop_attachment_declaration_missing: 'attachments',
    loop_attachment_hash_mismatch: 'attachments',
    loop_attachment_too_large: 'attachments',
    loop_attachment_type_not_allowed: 'attachments',
    ...textFields({ description, verification_note: verification }),
  });
  const anchored = notice?.field === 'description' || notice?.field === 'verification_note';
  const uploadFailure = (failure: UploadFailure) => {
    setUploading(false);
    if (failure.kind === 'store') setDegraded(true);
    if (failure.kind === 'port') {
      setUploadError(failure.error);
      if (failure.behaviour?.kind === 'degraded_section') setDegraded(true);
    }
  };
  const submit = (confirmed: boolean) => {
    if (uploading || mutation.status === 'pending') return;
    const instant = executionInstant(performed, timeZone);
    if (
      action === '' ||
      instant === null ||
      role.trim() === '' ||
      role.length > 64 ||
      reasonIssueKey(description, limits.description, { loopText: true }) !== null ||
      reasonIssueKey(verification, limits.verification, { loopText: true, optional: true }) !==
        null ||
      attachments.length > 5
    ) {
      setInvalid(true);
      return;
    }
    const body = {
      description: submittedText(description),
      performed_at: instant,
      performed_by_role: role,
      verification_note: optionalText(verification),
      attachments,
    };
    const candidate = {
      description: body.description,
      performedAt: instant,
      role,
      verification: body.verification_note ?? null,
    };
    const previous = latest([sent[action], recordedEvidence(records, action)]);
    if (!confirmed && previous !== null && sameEvidence(candidate, previous)) {
      setRepeat(previous);
      return;
    }
    setRepeat(null);
    setReceipt(null);
    void mutation
      .run(body)
      .then((result) => {
        const entry = { ...candidate, addedAt: result.added_at };
        setSent((current) => ({ ...current, [action]: entry }));
        setReceipt(entry);
        setDescription('');
        setVerification('');
        setPerformed('');
        setRole('');
        setAttachments([]);
        setInvalid(false);
        onWritten();
      })
      .catch(() => undefined);
  };
  return (
    <form
      noValidate
      aria-label={text('scr09.closure.evidence')}
      onSubmit={(event) => {
        event.preventDefault();
        submit(false);
      }}
    >
      <h2>{text('scr09.closure.evidence')}</h2>
      <Failure
        error={notice?.field === 'attachments' || anchored ? null : mutation.error}
        written={[description, verification].join('\n')}
      />
      {invalid && <p role="alert">{text('scr09.closure.invalid')}</p>}
      <label>
        {text('scr09.closure.action')}
        <select
          name="action_id"
          value={action}
          onChange={(e) => {
            setAction(e.target.value);
            setAttachments([]);
            mutation.reset();
          }}
          disabled={attachments.length > 0 || uploading || mutation.status === 'pending'}
        >
          <option value="">{text('scr09.closure.choose')}</option>
          {detail.actions.map((item) => (
            <option key={item.action_id} value={item.action_id}>
              {item.action_description}
            </option>
          ))}
        </select>
      </label>
      <ReasonField
        label={text('scr09.closure.description')}
        value={description}
        onChange={setDescription}
        limits={limits.description}
        loopText
        showErrors={invalid}
        notice={notice?.field === 'description' ? notice : undefined}
        name="description"
      />
      <ReasonField
        label={text('scr09.closure.verification')}
        value={verification}
        onChange={setVerification}
        limits={limits.verification}
        optional
        loopText
        showErrors={invalid}
        notice={notice?.field === 'verification_note' ? notice : undefined}
        name="verification_note"
      />
      <label>
        {text('scr09.closure.performed_at')}
        <DateTimeInput name="performed_at" value={performed} onChange={setPerformed} required />
      </label>
      <label>
        {text('scr09.closure.performed_by_role')}
        <input
          name="performed_by_role"
          value={role}
          maxLength={64}
          onChange={(e) => {
            setRole(e.target.value);
          }}
          required
        />
      </label>
      <p>{text('scr09.closure.attachment_limit')}</p>
      <ul>
        {attachments.map((item) => (
          <li key={item.attachment_id}>
            {item.filename}{' '}
            <button
              type="button"
              disabled={uploading || mutation.status === 'pending'}
              onClick={() => {
                setAttachments((previous) =>
                  previous.filter((candidate) => candidate.attachment_id !== item.attachment_id),
                );
                mutation.reset();
              }}
            >
              {text('scr09.closure.remove_pending_attachment')}
            </button>
          </li>
        ))}
      </ul>
      <fieldset
        disabled={
          mutation.status === 'pending' || attachments.length >= 5 || action === '' || degraded
        }
      >
        <legend>{text('scr09.closure.attachments')}</legend>
        <div
          onClickCapture={(event) => {
            // UI-21 solo contiene el botón de subir/reintentar. El clic, también desde teclado,
            // abre la fase; sus eventos uploaded/failure la cierran, sin modificar el compartido.
            const button = event.target instanceof Element ? event.target.closest('button') : null;
            if (
              button instanceof HTMLButtonElement &&
              button === event.currentTarget.querySelector('button') &&
              !button.matches(':disabled')
            )
              setUploading(true);
          }}
        >
          <DirectUploadField
            key={`${action}:${String(attachments.length)}`}
            label={text('scr09.closure.attachments')}
            target={{ unit: 'loop_attachment', actionId: action }}
            disabled={attachments.length >= 5 || degraded}
            notice={notice?.field === 'attachments' ? notice : undefined}
            onFailure={uploadFailure}
            onUploaded={(result) => {
              setUploading(false);
              if (result.unit === 'loop_attachment')
                setAttachments((previous) =>
                  previous.length < 5 ? [...previous, result.attachment] : previous,
                );
            }}
          />
        </div>
      </fieldset>
      {degraded && <p role="status">{text('scr09.closure.storage_degraded')}</p>}
      <Failure error={uploadError} />
      {degraded && (
        <button
          type="button"
          onClick={() => {
            setDegraded(false);
            setUploadError(null);
          }}
        >
          {text('scr09.closure.retry_storage')}
        </button>
      )}
      <button type="submit" disabled={mutation.status === 'pending' || uploading}>
        {text('scr09.closure.submit_evidence')}
      </button>
      {repeat !== null && (
        <div role="alert" className={styles.notice} data-part="repeat_confirmation">
          <p data-part="repeat_text">
            {`${text('scr09.closure.repeat_notice')} ${formatInTimeZone(repeat.addedAt, timeZone, 'HH:mm')}. ${text('scr09.closure.repeat_question')}`}
          </p>
          <button
            type="button"
            onClick={() => {
              submit(true);
            }}
          >
            {text('scr09.closure.repeat_confirm')}
          </button>{' '}
          <button
            type="button"
            onClick={() => {
              setRepeat(null);
            }}
          >
            {text('scr09.closure.repeat_cancel')}
          </button>
        </div>
      )}
      {receipt !== null && (
        <div
          ref={receiptRef}
          tabIndex={-1}
          role="status"
          className={styles.notice}
          data-part="evidence_receipt"
        >
          <p>
            <strong data-part="receipt_title">{text('scr09.closure.evidence_registered')}</strong>
          </p>
          <dl>
            <dt>{text('scr09.closure.receipt_what')}</dt>
            <dd data-part="receipt_what">{receipt.description}</dd>
            <dt>{text('scr09.closure.receipt_when')}</dt>
            <dd data-part="receipt_when">
              <TimestampWithOffset instant={receipt.performedAt} timeZone={timeZone} />
            </dd>
            <dt>{text('scr09.closure.receipt_role')}</dt>
            <dd data-part="receipt_role">{receipt.role}</dd>
          </dl>
          {onNextStep !== undefined && (
            <p>
              {text('scr09.closure.receipt_next')}{' '}
              <button type="button" onClick={onNextStep}>
                {text('scr09.closure.go_to_sign')}
              </button>
            </p>
          )}
        </div>
      )}
    </form>
  );
}
