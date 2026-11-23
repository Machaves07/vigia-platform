/**
 * Reabrir añade una acción nueva; jamás sustituye el historial consultado. Registrada, el
 * formulario se vacía y el acuse recibe el foco con la vuelta al lazo (O-17).
 */
import { useEffect, useRef, useState } from 'react';
import { Link } from 'react-router';
import { ReasonField, reasonIssueKey, submittedText } from '@/components/ReasonField/ReasonField';
import { closurePort, type ReopeningBody } from '@/ports/scr09Closure';
import { useScreenMutation } from '@/state/query';
import { text } from '@/texts';
import { ClosureScreen, Failure, type ClosureContext } from '../closure/common';
import { LONG_TEXT } from '../closure/Evidence';
import { ActionFields, actionPayload, emptyAction, validAction } from './ActionFields';

function Reopening({
  context,
  plantId,
  onWritten,
}: {
  readonly context: ClosureContext;
  readonly plantId: string;
  readonly onWritten: () => void;
}) {
  const [reason, setReason] = useState('');
  const [action, setAction] = useState(() => emptyAction(context.principal?.user_id ?? ''));
  const [invalid, setInvalid] = useState(false);
  const receipt = useRef<HTMLDivElement>(null);
  const mutation = useScreenMutation({
    write: (body: ReopeningBody) =>
      closurePort.reopen.write({ path: { finding_state_id: context.findingId }, body }),
    invalidates: {
      screen: 'scr09',
      resources: ['findings', 'actions', 'unclosable', 'ledger_records'],
    },
  });
  const done = mutation.status === 'success';
  useEffect(() => {
    if (done) receipt.current?.focus();
  }, [done]);
  return (
    <form
      noValidate
      aria-label={text('scr09.reopening.title')}
      onSubmit={(e) => {
        e.preventDefault();
        if (
          reasonIssueKey(reason, LONG_TEXT, { loopText: true }) !== null ||
          !validAction(action)
        ) {
          setInvalid(true);
          return;
        }
        void mutation
          .run({ reason_text: submittedText(reason), ...actionPayload(action) })
          .then(() => {
            setReason('');
            setAction(emptyAction(context.principal?.user_id ?? ''));
            setInvalid(false);
            onWritten();
          })
          .catch(() => undefined);
      }}
    >
      <p>{text('scr09.reopening.history_kept')}</p>
      <Failure
        error={mutation.error}
        written={[reason, action.action_description, action.expected_outcome ?? ''].join('\n')}
      />
      {invalid && <p role="alert">{text('scr09.closure.invalid')}</p>}
      <ReasonField
        name="reason_text"
        label={text('scr09.reopening.reason')}
        value={reason}
        onChange={setReason}
        limits={LONG_TEXT}
        loopText
        showErrors={invalid}
      />
      <ActionFields
        context={context}
        plantId={plantId}
        value={action}
        onChange={setAction}
        invalid={invalid}
      />
      <button type="submit" disabled={mutation.status === 'pending'}>
        {text('scr09.reopening.submit')}
      </button>
      {done && (
        <div ref={receipt} tabIndex={-1} role="status" data-part="reopening_receipt">
          <p>{text('scr09.closure.saved')}</p>
          <Link to={`/findings/${context.findingId}/loop`}>{text('scr09.closure.back')}</Link>
        </div>
      )}
    </form>
  );
}

export default function Page() {
  return (
    <ClosureScreen title={text('scr09.reopening.title')}>
      {({ context, detail, onWritten }) => (
        <Reopening context={context} plantId={detail.plant_id} onWritten={onWritten} />
      )}
    </ClosureScreen>
  );
}
