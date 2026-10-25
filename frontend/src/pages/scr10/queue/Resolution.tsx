/** Una decisión humana por detección; nunca se reintenta la escritura automáticamente. */
import { useId, useState } from 'react';
import { FieldNotice, NoticeStack } from '@/components/NoticeStack/NoticeStack';
import { ReasonField, reasonIssueKey } from '@/components/ReasonField/ReasonField';
import {
  OUTCOMES,
  REASON_LIMITS,
  queueClock,
  reasonCategories,
  resolutionNotice,
  reviewQueuePort,
  type QueueItem,
  type ResolutionBody,
  type ResolutionResult,
} from '@/ports/scr10ReviewQueue';
import { useScreenMutation } from '@/state/query';
import { text } from '@/texts';
import { label } from '@/texts/labels';

export function Resolution({
  item,
  onWritten,
}: {
  readonly item: QueueItem;
  readonly onWritten: (result: ResolutionResult) => void;
}) {
  const id = useId();
  const [outcome, setOutcome] = useState('');
  const [category, setCategory] = useState('');
  const [reason, setReason] = useState('');
  const [showErrors, setShowErrors] = useState(false);
  const mutation = useScreenMutation({
    write: (body: ResolutionBody) =>
      reviewQueuePort.resolve.write({ path: { detection_id: item.detection_id }, body }),
    invalidates: { screen: 'scr10', resources: ['review_queue', 'findings'] },
  });
  const notice = resolutionNotice(mutation.error);
  const formNotice = notice?.field === 'form' ? notice : null;
  return (
    <form
      noValidate
      aria-label={text('scr10.form')}
      aria-describedby={formNotice === null ? undefined : `${id}-notice`}
      onSubmit={(event) => {
        event.preventDefault();
        setShowErrors(true);
        if (outcome === '' || category === '' || reasonIssueKey(reason, REASON_LIMITS) !== null)
          return;
        void mutation
          .run({ outcome, reason_category: category, reason_text: reason })
          .then((result) => {
            onWritten(result);
          })
          .catch(() => undefined);
      }}
    >
      <h2>{text('scr10.form')}</h2>
      {formNotice !== null && <FieldNotice id={`${id}-notice`} notice={formNotice} />}
      {notice?.field === null && (
        <NoticeStack
          notices={[notice]}
          clock={queueClock()}
          onDismiss={() => {
            mutation.reset();
          }}
        />
      )}
      {mutation.error !== null && notice === null && (
        <p role="alert">{text('scr10.local_error')}</p>
      )}
      <fieldset>
        <legend>{text('scr10.outcome')}</legend>
        {OUTCOMES.map((value) => (
          <label key={value}>
            <input
              type="radio"
              name={`${id}-outcome`}
              value={value}
              checked={outcome === value}
              onChange={() => {
                mutation.reset();
                setOutcome(value);
                setCategory('');
              }}
            />
            {label('review_resolution_outcome', value)}
          </label>
        ))}
      </fieldset>
      <label>
        {text('scr10.category')}
        <select
          value={category}
          aria-invalid={showErrors && category === ''}
          onChange={(event) => {
            mutation.reset();
            setCategory(event.currentTarget.value);
          }}
        >
          <option value="">{text('scr10.choose')}</option>
          {reasonCategories(outcome).map(({ list, value }) => (
            <option key={value} value={value}>
              {label(list, value)}
            </option>
          ))}
        </select>
      </label>
      {showErrors && (outcome === '' || category === '') && (
        <p role="alert">{text('scr10.missing')}</p>
      )}
      <ReasonField
        label={text('scr10.reason')}
        name="reason_text"
        value={reason}
        limits={REASON_LIMITS}
        showErrors={showErrors}
        notice={notice?.field === 'reason_text' ? notice : undefined}
        onChange={(value) => {
          mutation.reset();
          setReason(value);
        }}
      />
      <button type="submit" disabled={mutation.status === 'pending'}>
        {text('scr10.submit')}
      </button>
    </form>
  );
}
