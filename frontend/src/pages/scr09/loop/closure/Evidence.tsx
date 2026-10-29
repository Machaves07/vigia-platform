/** Evidencia estructurada y composición de un máximo de cinco adjuntos declarados. */
import { DateTimeInput } from '@/components/DateFields/DateFields';
import { useState } from 'react';
import {
  DirectUploadField,
  type UploadFailure,
} from '@/components/DirectUploadField/DirectUploadField';
import { ReasonField, reasonIssueKey } from '@/components/ReasonField/ReasonField';
import { closureNotice, closurePort, type Detail, type EvidenceBody } from '@/ports/scr09Closure';
import { useScreenMutation } from '@/state/query';
import { text } from '@/texts';
import { Failure } from './common';
import { executionInstant } from './dates';

export const LONG_TEXT = { minLength: 20, maxLength: 2000 };
export function Evidence({
  detail,
  timeZone,
  onWritten,
}: {
  readonly detail: Detail;
  readonly timeZone: string;
  readonly onWritten: () => void;
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
  });
  const uploadFailure = (failure: UploadFailure) => {
    setUploading(false);
    if (failure.kind === 'store') setDegraded(true);
    if (failure.kind === 'port') {
      setUploadError(failure.error);
      if (failure.behaviour?.kind === 'degraded_section') setDegraded(true);
    }
  };
  return (
    <form
      noValidate
      aria-label={text('scr09.closure.evidence')}
      onSubmit={(event) => {
        event.preventDefault();
        if (uploading || mutation.status === 'pending') return;
        const instant = executionInstant(performed, timeZone);
        if (
          action === '' ||
          instant === null ||
          role.trim() === '' ||
          role.length > 64 ||
          reasonIssueKey(description, LONG_TEXT) !== null ||
          attachments.length > 5
        ) {
          setInvalid(true);
          return;
        }
        void mutation
          .run({
            description,
            performed_at: instant,
            performed_by_role: role,
            verification_note: verification.trim() === '' ? null : verification,
            attachments,
          })
          .then(() => {
            setAttachments([]);
            setInvalid(false);
            onWritten();
          })
          .catch(() => undefined);
      }}
    >
      <h2>{text('scr09.closure.evidence')}</h2>
      <Failure error={notice?.field === 'attachments' ? null : mutation.error} />
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
        limits={LONG_TEXT}
        showErrors={invalid}
        name="description"
      />
      <label>
        {text('scr09.closure.verification')}
        <textarea
          name="verification_note"
          value={verification}
          maxLength={1000}
          onChange={(e) => {
            setVerification(e.target.value);
          }}
        />
      </label>
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
      {mutation.status === 'success' && <p role="status">{text('scr09.closure.saved')}</p>}
    </form>
  );
}
