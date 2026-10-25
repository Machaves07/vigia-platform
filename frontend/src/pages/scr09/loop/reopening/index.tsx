/** Reabrir añade una acción nueva; jamás sustituye el historial consultado. */
import { useState } from 'react';
import { ReasonField, reasonIssueKey } from '@/components/ReasonField/ReasonField';
import { closurePort, type ReopeningBody } from '@/ports/scr09Closure';
import { useScreenMutation } from '@/state/query';
import { text } from '@/texts';
import { ClosureScreen, Failure, type ClosureContext } from '../closure/common';
import { LONG_TEXT } from '../closure/Evidence';
import { ActionFields, emptyAction, validAction } from './ActionFields';

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
  const mutation = useScreenMutation({
    write: (body: ReopeningBody) =>
      closurePort.reopen.write({ path: { finding_state_id: context.findingId }, body }),
    invalidates: {
      screen: 'scr09',
      resources: ['findings', 'actions', 'unclosable', 'ledger_records'],
    },
  });
  return (
    <form
      noValidate
      aria-label={text('scr09.reopening.title')}
      onSubmit={(e) => {
        e.preventDefault();
        if (reasonIssueKey(reason, LONG_TEXT) !== null || !validAction(action)) {
          setInvalid(true);
          return;
        }
        void mutation
          .run({ reason_text: reason, ...action })
          .then(onWritten)
          .catch(() => undefined);
      }}
    >
      <p>{text('scr09.reopening.history_kept')}</p>
      <Failure error={mutation.error} />
      {invalid && <p role="alert">{text('scr09.closure.invalid')}</p>}
      <ReasonField
        name="reason_text"
        label={text('scr09.reopening.reason')}
        value={reason}
        onChange={setReason}
        limits={LONG_TEXT}
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
      {mutation.status === 'success' && <p role="status">{text('scr09.closure.saved')}</p>}
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
