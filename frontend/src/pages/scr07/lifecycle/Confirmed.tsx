/**
 * Escritura con motivo y confirmación explícita (IF-14 paso 4): sin la casilla marcada no se
 * envía nada; el motivo sigue el espejo de la política de texto libre y el servidor decide.
 */
import { useId, useState } from 'react';
import { ReasonField, reasonIssueKey } from '@/components/ReasonField/ReasonField';
import { fleetNotice, NODE_REASON_LIMITS } from '@/ports/scr07Fleet';
import { text } from '@/texts';
import { FleetNotice } from '../shared';

/** El motivo rechazado por la política de texto libre se ancla al campo del motivo. */
export const REASON_ANCHORS = { fleet_free_text_rejected: 'reason' };

export function ConfirmedReasonForm({
  title,
  confirmation,
  submit,
  status,
  error,
  onSubmit,
  onEdit,
}: {
  readonly title: string;
  readonly confirmation: string;
  readonly submit: string;
  readonly status: 'idle' | 'pending' | 'error' | 'success';
  readonly error: Error | null;
  readonly onSubmit: (reason: string) => void;
  readonly onEdit: () => void;
}) {
  const id = useId();
  const [reason, setReason] = useState('');
  const [confirmed, setConfirmed] = useState(false);
  const [attempted, setAttempted] = useState(false);
  const notice = fleetNotice(error, REASON_ANCHORS);
  return (
    <form
      aria-label={title}
      noValidate
      onSubmit={(event) => {
        event.preventDefault();
        setAttempted(true);
        if (!confirmed || reasonIssueKey(reason, NODE_REASON_LIMITS) !== null) return;
        onSubmit(reason.normalize('NFC'));
      }}
    >
      <h2>{title}</h2>
      <FleetNotice notice={notice} field="form" id={`${id}-form`} />
      <ReasonField
        name="reason_es"
        label={text('scr07.lifecycle.reason')}
        value={reason}
        limits={NODE_REASON_LIMITS}
        showErrors={attempted}
        notice={notice?.field === 'reason' ? notice : undefined}
        onChange={(value) => {
          onEdit();
          setReason(value);
        }}
      />
      <label>
        <input
          type="checkbox"
          name="confirmation"
          checked={confirmed}
          aria-invalid={attempted && !confirmed}
          aria-describedby={attempted && !confirmed ? `${id}-confirm` : undefined}
          onChange={(event) => {
            onEdit();
            setConfirmed(event.currentTarget.checked);
          }}
        />
        {confirmation}
      </label>
      {attempted && !confirmed && (
        <p id={`${id}-confirm`} role="alert" data-text-key="scr07.lifecycle.confirm_required">
          {text('scr07.lifecycle.confirm_required')}
        </p>
      )}
      <button type="submit" disabled={status === 'pending'}>
        {submit}
      </button>
    </form>
  );
}
