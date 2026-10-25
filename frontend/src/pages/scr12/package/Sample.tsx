/** La auditoría de la muestra solo añade un resultado, jamás una clasificación. */
import { useId, useState } from 'react';
import { TimestampWithOffset } from '@/components/TimestampWithOffset/TimestampWithOffset';
import { ScreenState } from '@/pages/shared/ScreenState';
import { monthlyPort, type SampleItem, type SampleResult } from '@/ports/scr12Monthly';
import { useScreenMutation } from '@/state/query';
import { text } from '@/texts';
import { label } from '@/texts/labels';
import {
  MonthlyRefresh,
  MonthlyResult,
  MonthlyWriteError,
  useMonthlyRead,
  type MonthlyContext,
} from '../common';

const OUTCOMES = ['consistent_criterion', 'doubtful_criterion', 'criterion_error'] as const;

export function SampleView({
  item,
  timeZone,
}: {
  readonly item: SampleItem;
  readonly timeZone: string;
}) {
  return (
    <div>
      <p>
        {text('scr12.classification')}:{' '}
        {label('classification_outcome', item.classification_outcome)}
      </p>
      <p>
        {text('scr12.classification_date')}:{' '}
        <TimestampWithOffset instant={item.classified_at} timeZone={timeZone} />
      </p>
      <p>
        {item.outcome === null
          ? text('scr12.pending')
          : label('sample_audit_outcome', item.outcome)}
      </p>
      {item.comment !== null && <p>{item.comment}</p>}
      {item.reviewed_at !== null && (
        <TimestampWithOffset instant={item.reviewed_at} timeZone={timeZone} />
      )}
    </div>
  );
}

function ResultForm({
  itemId,
  onSaved,
}: {
  readonly itemId: string;
  readonly onSaved: () => void;
}) {
  const id = useId();
  const [outcome, setOutcome] = useState('');
  const [comment, setComment] = useState('');
  const mutation = useScreenMutation({
    write: (body: SampleResult) =>
      monthlyPort.result.write({ path: { sample_item_id: itemId }, body }),
    invalidates: { screen: 'scr12', resources: ['monthly_packages'] },
  });
  return (
    <form
      aria-label={text('scr12.submit_result')}
      onSubmit={(event) => {
        event.preventDefault();
        void mutation
          .run({ outcome, comment: comment === '' ? null : comment })
          .then(onSaved)
          .catch(() => undefined);
      }}
    >
      <MonthlyWriteError error={mutation.error} id={`${id}-error`} />
      <label htmlFor={`${id}-outcome`}>{text('scr12.outcome')}</label>
      <select
        id={`${id}-outcome`}
        name="outcome"
        required
        value={outcome}
        onChange={(event) => {
          setOutcome(event.target.value);
          mutation.reset();
        }}
      >
        <option value="">{text('scr12.pending')}</option>
        {OUTCOMES.map((value) => (
          <option value={value} key={value}>
            {label('sample_audit_outcome', value)}
          </option>
        ))}
      </select>
      <label htmlFor={`${id}-comment`}>{text('scr12.comment')}</label>
      <textarea
        id={`${id}-comment`}
        name="comment"
        maxLength={1000}
        value={comment}
        onChange={(event) => {
          setComment(event.target.value);
          mutation.reset();
        }}
      />
      <button type="submit" disabled={mutation.status === 'pending'}>
        {text('scr12.submit_result')}
      </button>
      {mutation.status === 'success' && <p role="status">{text('scr12.saved')}</p>}
    </form>
  );
}

export function Sample({
  context,
  packageId,
  opening,
  timeZone,
}: {
  readonly context: MonthlyContext;
  readonly packageId: string;
  readonly opening: number;
  readonly timeZone: string;
}) {
  const [opened, setOpened] = useState(false);
  const query = useMonthlyRead(
    context,
    'GET /accreditation/monthly-packages/{package_id}/sample',
    { package_id: packageId, opening },
    (signal) => monthlyPort.sample.read({ path: { package_id: packageId } }, signal),
    opened,
  );
  return (
    <section aria-label={text('scr12.sample')}>
      <h2>{text('scr12.sample')}</h2>
      <p>{text('scr12.sample_readonly')}</p>
      {!opened ? (
        <button
          type="button"
          onClick={() => {
            setOpened(true);
          }}
        >
          {text('scr12.load_sample')}
        </button>
      ) : (
        <>
          <MonthlyRefresh query={query} />
          <MonthlyResult query={query}>
            {query.data?.length === 0 && (
              <ScreenState state="empty" message={text('scr12.sample_empty')} />
            )}
            <ul>
              {query.data?.map((item) => (
                <li key={item.sample_item_id}>
                  <SampleView item={item} timeZone={timeZone} />
                  {context.canReview && (
                    <ResultForm
                      itemId={item.sample_item_id}
                      onSaved={() => {
                        void query.refresh();
                      }}
                    />
                  )}
                </li>
              ))}
            </ul>
          </MonthlyResult>
        </>
      )}
    </section>
  );
}
