/** Compromisos: el responsable ejecuta una acción sobre la zona, nunca es una persona observada. */
import { useId, useState } from 'react';
import { DateInput } from '@/components/DateFields/DateFields';
import { dateText } from '@/components/DateFields/parse';
import { useFormCheck } from '@/components/DateFields/useFormCheck';
import { ScreenState } from '@/pages/shared/ScreenState';
import {
  monthlyPort,
  type Commitment,
  type CommitmentInput,
  type CommitmentUpdate,
} from '@/ports/scr12Monthly';
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

const STATES = ['open', 'done', 'dropped'] as const;

export function CommitmentView({ item }: { readonly item: Commitment }) {
  return (
    <div>
      <p>{item.text}</p>
      <dl>
        <dt>{text('scr12.responsible')}</dt>
        <dd>{item.responsible_user_id}</dd>
        <dt>{text('scr12.due')}</dt>
        <dd>{dateText(item.due_date) || item.due_date}</dd>
        <dt>{text('scr12.state')}</dt>
        <dd>{label('commitment_state', item.state)}</dd>
      </dl>
      {item.closed_note !== null && <p>{item.closed_note}</p>}
    </div>
  );
}

function UpdateCommitment({
  item,
  onSaved,
}: {
  readonly item: Commitment;
  readonly onSaved: (item: Commitment) => void;
}) {
  const id = useId();
  const [state, setState] = useState(item.state);
  const [note, setNote] = useState('');
  const mutation = useScreenMutation({
    write: (body: CommitmentUpdate) =>
      monthlyPort.updateCommitment.write({ path: { commitment_id: item.commitment_id }, body }),
    invalidates: { screen: 'scr12', resources: ['commitments'] },
  });
  return (
    <form
      aria-label={text('scr12.update')}
      noValidate
      onSubmit={(event) => {
        event.preventDefault();
        void mutation
          .run({ state, note: note === '' ? null : note })
          .then((result) => {
            onSaved(result.commitment);
          })
          .catch(() => undefined);
      }}
    >
      <MonthlyWriteError error={mutation.error} id={`${id}-error`} />
      <label htmlFor={`${id}-state`}>{text('scr12.state')}</label>
      <select
        id={`${id}-state`}
        value={state}
        onChange={(event) => {
          setState(event.target.value);
          mutation.reset();
        }}
      >
        {STATES.map((value) => (
          <option key={value} value={value}>
            {label('commitment_state', value)}
          </option>
        ))}
      </select>
      <label htmlFor={`${id}-note`}>{text('scr12.note')}</label>
      <textarea
        id={`${id}-note`}
        maxLength={500}
        value={note}
        onChange={(event) => {
          setNote(event.target.value);
          mutation.reset();
        }}
      />
      <button type="submit" disabled={mutation.status === 'pending'}>
        {text('scr12.update')}
      </button>
      {mutation.status === 'success' && <p role="status">{text('scr12.saved')}</p>}
    </form>
  );
}

function CreateCommitment({
  packageId,
  onSaved,
}: {
  readonly packageId: string;
  readonly onSaved: () => void;
}) {
  const id = useId();
  const [value, setValue] = useState('');
  const [responsible, setResponsible] = useState('');
  const [due, setDue] = useState('');
  const check = useFormCheck();
  const mutation = useScreenMutation({
    write: (commitment: CommitmentInput) =>
      monthlyPort.recordCommitments.write({
        path: { package_id: packageId },
        body: { commitments: [commitment] },
      }),
    invalidates: { screen: 'scr12', resources: ['commitments'] },
  });
  return (
    <form
      aria-label={text('scr12.create')}
      noValidate
      onSubmit={(event) => {
        event.preventDefault();
        if (!check.run(event.currentTarget)) return;
        void mutation
          .run({ text: value, responsible_user_id: responsible, due_date: due })
          .then(() => {
            setValue('');
            setResponsible('');
            setDue('');
            onSaved();
          })
          .catch(() => undefined);
      }}
    >
      <h3>{text('scr12.create')}</h3>
      <MonthlyWriteError error={mutation.error} id={`${id}-error`} />
      <label htmlFor={`${id}-text`}>{text('scr12.text')}</label>
      <textarea
        id={`${id}-text`}
        name="text"
        required
        minLength={20}
        maxLength={1000}
        value={value}
        {...check.field('text')}
        onChange={(event) => {
          setValue(event.target.value);
          check.clear('text');
          mutation.reset();
        }}
      />
      {check.error('text')}
      <label htmlFor={`${id}-responsible`}>{text('scr12.responsible')}</label>
      <input
        id={`${id}-responsible`}
        name="responsible_user_id"
        required
        pattern="[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
        value={responsible}
        aria-invalid={check.field('responsible_user_id')['aria-invalid']}
        aria-describedby={[`${id}-help`, check.field('responsible_user_id')['aria-describedby']]
          .filter(Boolean)
          .join(' ')}
        onChange={(event) => {
          setResponsible(event.target.value);
          check.clear('responsible_user_id');
          mutation.reset();
        }}
      />
      {check.error('responsible_user_id')}
      <p id={`${id}-help`}>{text('scr12.responsible_help')}</p>
      <label htmlFor={`${id}-due`}>{text('scr12.due')}</label>
      <DateInput
        id={`${id}-due`}
        name="due_date"
        required
        value={due}
        {...check.field('due_date')}
        onChange={(value) => {
          setDue(value);
          check.clear('due_date');
          mutation.reset();
        }}
      />
      {check.error('due_date')}
      <button type="submit" disabled={mutation.status === 'pending'}>
        {text('scr12.create')}
      </button>
      {mutation.status === 'success' && <p role="status">{text('scr12.saved')}</p>}
    </form>
  );
}

export function Commitments({
  context,
  packageId,
  zoneId,
  period,
}: {
  readonly context: MonthlyContext;
  readonly packageId: string;
  readonly zoneId: string;
  readonly period: string;
}) {
  const query = useMonthlyRead(
    context,
    'GET /accreditation/commitments',
    { zone_id: zoneId, period },
    (signal) => monthlyPort.commitments.read({ query: { zone_id: zoneId, period } }, signal),
  );
  return (
    <section aria-label={text('scr12.commitments')}>
      <h2>{text('scr12.commitments')}</h2>
      <MonthlyRefresh query={query} />
      <MonthlyResult query={query}>
        {query.data?.length === 0 && (
          <ScreenState state="empty" message={text('scr12.commitments_empty')} />
        )}
        <ul>
          {query.data?.map((item) => {
            return (
              <li key={item.commitment_id}>
                <CommitmentView item={item} />
                {context.canCommit && (
                  <UpdateCommitment
                    item={item}
                    onSaved={() => {
                      void query.refresh();
                    }}
                  />
                )}
              </li>
            );
          })}
        </ul>
      </MonthlyResult>
      {context.canCommit && (
        <CreateCommitment
          packageId={packageId}
          onSaved={() => {
            void query.refresh();
          }}
        />
      )}
    </section>
  );
}
