/** La revisión renueva con fecha o convierte en una acción nueva completa (T11/T12). */
import { useState } from 'react';
import { ReasonField, reasonIssueKey } from '@/components/ReasonField/ReasonField';
import { SignatureBlock } from '@/components/SignatureBlock/SignatureBlock';
import {
  closureClock,
  closureNotice,
  closurePort,
  recordContent,
  stringField,
  REVIEW_OUTCOMES,
  type LedgerRecord,
  type ReviewBody,
} from '@/ports/scr09Closure';
import { useScreenMutation } from '@/state/query';
import { text } from '@/texts';
import { label } from '@/texts/labels';
import { Failure, type ClosureContext } from '../closure/common';
import { reviewInstant, validReviewDate } from '../closure/dates';
import { LONG_TEXT } from '../closure/Evidence';
import { ActionFields, emptyAction, validAction } from '../reopening/ActionFields';

export function Review({
  context,
  records,
  plantId,
  timeZone,
  onWritten,
}: {
  readonly context: ClosureContext;
  readonly records: readonly LedgerRecord[];
  readonly plantId: string;
  readonly timeZone: string;
  readonly onWritten: () => void;
}) {
  const [declaration, setDeclaration] = useState('');
  const [outcome, setOutcome] = useState('renewed');
  const [note, setNote] = useState('');
  const [day, setDay] = useState('');
  const [action, setAction] = useState(() => emptyAction(context.principal?.user_id ?? ''));
  const [invalid, setInvalid] = useState(false);
  const mutation = useScreenMutation({
    write: (body: ReviewBody) =>
      closurePort.review.write({ path: { declaration_id: declaration }, body }),
    invalidates: {
      screen: 'scr09',
      resources: ['findings', 'unclosable', 'actions', 'ledger_records'],
    },
  });
  const notice = closureNotice(mutation.error, { loop_signer_license_missing: 'signature' });
  return (
    <section aria-label={text('scr09.unclosable.review')}>
      <h2>{text('scr09.unclosable.review')}</h2>
      <Failure error={notice?.field === 'signature' ? null : mutation.error} />
      {invalid && <p role="alert">{text('scr09.closure.invalid')}</p>}
      <label>
        {text('scr09.unclosable.declaration')}
        <select
          value={declaration}
          onChange={(e) => {
            setDeclaration(e.target.value);
          }}
        >
          <option value="">{text('scr09.closure.choose')}</option>
          {records
            .filter((record) => record.record_type === 'unclosable_state_declared')
            .map((record) => (
              <option
                key={record.record_id}
                value={stringField(recordContent(record).declaration_id)}
              >
                {stringField(recordContent(record).evaluated_alternative)}
              </option>
            ))}
        </select>
      </label>
      <label>
        {text('scr09.unclosable.outcome')}
        <select
          value={outcome}
          onChange={(e) => {
            setOutcome(e.target.value);
          }}
        >
          {REVIEW_OUTCOMES.map((value) => (
            <option key={value} value={value}>
              {label('unclosable_review_outcome', value)}
            </option>
          ))}
        </select>
      </label>
      <ReasonField
        name="review_note"
        label={text('scr09.unclosable.review_note')}
        value={note}
        onChange={setNote}
        limits={LONG_TEXT}
        showErrors={invalid}
      />
      {outcome === 'renewed' ? (
        <label>
          {text('scr09.unclosable.review_due_at')}
          <input
            type="date"
            value={day}
            onChange={(e) => {
              setDay(e.target.value);
            }}
          />
        </label>
      ) : (
        <ActionFields
          context={context}
          plantId={plantId}
          value={action}
          onChange={setAction}
          invalid={invalid}
        />
      )}
      {context.principal !== null && (
        <SignatureBlock
          principal={{
            display_name: context.principal.display_name,
            professional_license: context.principal.professional_license ?? null,
          }}
          textToSign={`${label('unclosable_review_outcome', outcome)}\n${note}\n${outcome === 'renewed' ? day : action.action_description}`}
          pending={mutation.status === 'pending'}
          notice={notice?.field === 'signature' ? notice : undefined}
          onSign={(snapshot) => {
            if (
              declaration === '' ||
              reasonIssueKey(note, LONG_TEXT) !== null ||
              (outcome === 'renewed'
                ? !validReviewDate(day, closureClock(), timeZone)
                : !validAction(action))
            ) {
              setInvalid(true);
              return;
            }
            void mutation
              .run({
                outcome,
                review_note: note,
                new_review_due_at: outcome === 'renewed' ? reviewInstant(day, timeZone) : null,
                action: outcome === 'converted_to_action' ? action : null,
                professional_license: snapshot.professional_license,
              })
              .then(onWritten)
              .catch(() => undefined);
          }}
        />
      )}
      {mutation.status === 'success' && <p role="status">{text('scr09.closure.saved')}</p>}
    </section>
  );
}
