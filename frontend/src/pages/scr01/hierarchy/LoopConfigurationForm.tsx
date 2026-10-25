/** SUP-U05-39: solo cambios explícitos; límites generados, motivo y rechazo del servidor. */
import { useId, useState } from 'react';
import { ReasonField } from '@/components/ReasonField/ReasonField';
import {
  configurationIssues,
  configurationNotice,
  configurationReasonLimits,
  hierarchyPort,
  LOOP_FIELDS,
  loopLimits,
  type ConfigurationChange,
  type LoopConfiguration,
} from '@/ports/scr01Hierarchy';
import type { ValidationIssue } from '@/ports/validation';
import { useScreenMutation } from '@/state/query';
import { text } from '@/texts';
import { AdminField, AdminMutationResult } from './FormFields';
import styles from '../scr01.module.css';

const WEEKDAYS = [
  'monday',
  'tuesday',
  'wednesday',
  'thursday',
  'friday',
  'saturday',
  'sunday',
] as const;

export function LoopConfigurationForm({
  initial,
  plantId,
}: {
  readonly initial: LoopConfiguration;
  readonly plantId: string;
}) {
  const noticeId = useId();
  const calendarId = useId();
  // La invalidación puede refrescar la lectura durante un borrador: no convierte campos
  // intactos en cambios ni descarta lo escrito. Solo una escritura aceptada renueva la base.
  const [baseline, setBaseline] = useState(initial);
  const [values, setValues] = useState(
    () =>
      Object.fromEntries(LOOP_FIELDS.map((field) => [field, String(initial[field])])) as Record<
        (typeof LOOP_FIELDS)[number],
        string
      >,
  );
  const [weekdays, setWeekdays] = useState(initial.business_calendar.working_weekdays);
  const [dates, setDates] = useState(initial.business_calendar.non_working_dates.join('\n'));
  const [reason, setReason] = useState('');
  const [reasonEpoch, setReasonEpoch] = useState(0);
  const [attempted, setAttempted] = useState(false);
  const [issues, setIssues] = useState<readonly ValidationIssue[]>([]);
  const mutation = useScreenMutation({
    write: (body: ConfigurationChange) =>
      hierarchyPort.saveConfiguration.write({ path: { plant_id: plantId }, body }),
    invalidates: { screen: 'scr01', resources: ['loop_configuration'] },
  });
  const calendarIssue = issues.find(
    (issue) => issue.field?.startsWith('business_calendar') === true,
  );
  const reasonNotice = configurationNotice(mutation.error);
  return (
    <form
      className={styles.form}
      aria-label={text('scr01_hierarchy.edit_loop')}
      noValidate
      aria-describedby={mutation.error === null ? undefined : noticeId}
      onSubmit={(event) => {
        event.preventDefault();
        if (mutation.status === 'pending') return;
        setAttempted(true);
        const body: ConfigurationChange = { reason };
        for (const field of LOOP_FIELDS) {
          if (values[field] !== String(baseline[field]))
            body[field] = values[field] === '' ? NaN : Number(values[field]);
        }
        if (
          weekdays.join(',') !== baseline.business_calendar.working_weekdays.join(',') ||
          dates !== baseline.business_calendar.non_working_dates.join('\n')
        ) {
          body.business_calendar = {
            working_weekdays: weekdays,
            non_working_dates: dates
              .split('\n')
              .map((value) => value.trim())
              .filter((value) => value !== ''),
          };
        }
        const found = configurationIssues(body);
        setIssues(found);
        if (found.length > 0) return;
        void mutation.run(body).then(
          (saved) => {
            setBaseline(saved);
            setValues(
              Object.fromEntries(
                LOOP_FIELDS.map((field) => [field, String(saved[field])]),
              ) as Record<(typeof LOOP_FIELDS)[number], string>,
            );
            setWeekdays(saved.business_calendar.working_weekdays);
            setDates(saved.business_calendar.non_working_dates.join('\n'));
            setReason('');
            setAttempted(false);
            setReasonEpoch((epoch) => epoch + 1);
          },
          () => undefined,
        );
      }}
    >
      <h4>{text('scr01_hierarchy.edit_loop')}</h4>
      {LOOP_FIELDS.map((field) => (
        <AdminField
          key={field}
          name={field}
          value={values[field]}
          issues={issues}
          type="number"
          min={loopLimits[field].min}
          max={loopLimits[field].max}
          disabled={mutation.status === 'pending'}
          onChange={(value) => {
            setValues({ ...values, [field]: value });
          }}
        />
      ))}
      <fieldset
        disabled={mutation.status === 'pending'}
        aria-invalid={calendarIssue !== undefined}
        aria-describedby={calendarIssue === undefined ? undefined : `${calendarId}-error`}
      >
        <legend>{text('scr01_hierarchy.working_weekdays')}</legend>
        {WEEKDAYS.map((day, index) => (
          <label key={day}>
            <input
              type="checkbox"
              checked={weekdays.includes(index + 1)}
              onChange={(event) => {
                setWeekdays(
                  event.currentTarget.checked
                    ? [...weekdays, index + 1].sort((a, b) => a - b)
                    : weekdays.filter((value) => value !== index + 1),
                );
              }}
            />
            {text(`scr01_hierarchy.${day}`)}
          </label>
        ))}
        <label htmlFor={calendarId}>{text('scr01_hierarchy.non_working_dates_input')}</label>
        <textarea
          className={styles.input}
          id={calendarId}
          value={dates}
          onChange={(event) => {
            setDates(event.currentTarget.value);
          }}
        />
        {calendarIssue !== undefined && (
          <p id={`${calendarId}-error`} data-text-key={calendarIssue.key}>
            {text(calendarIssue.key)}
          </p>
        )}
      </fieldset>
      <fieldset
        disabled={mutation.status === 'pending'}
        aria-label={text('scr01_hierarchy.reason')}
      >
        <ReasonField
          key={reasonEpoch}
          label={text('scr01_hierarchy.reason')}
          name="reason"
          value={reason}
          onChange={setReason}
          limits={configurationReasonLimits}
          showErrors={attempted}
          notice={reasonNotice}
        />
      </fieldset>
      <button className={styles.button} type="submit" disabled={mutation.status === 'pending'}>
        {text('scr01_hierarchy.save_loop')}
      </button>
      <AdminMutationResult
        id={noticeId}
        error={reasonNotice === undefined ? mutation.error : null}
        status={mutation.status}
        onDismiss={mutation.reset}
      />
    </form>
  );
}
