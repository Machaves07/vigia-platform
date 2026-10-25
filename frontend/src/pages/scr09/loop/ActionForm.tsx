/**
 * Acción correctiva sobre la zona (IF-09 pasos 1 y 2; SUP-U05-07; H-24): tipo de la lista
 * cerrada, descripción, resultado esperado, responsable y plazo por defecto de la planta. No hay
 * ningún campo de persona observada ni «persona causante» (BR-APP-33): el responsable es el
 * usuario que se compromete a ejecutarla. Un rechazo (`409 loop_transition_not_allowed`,
 * diferencia 4 del PR #197) se ancla al formulario y conserva lo escrito.
 */
import { useId, useState } from 'react';
import assignBody from '../../../../openapi/bodies/assign_loop_findings__finding_state_id__actions_post.json';
import { FieldNotice } from '@/components/NoticeStack/NoticeStack';
import type { Notice } from '@/components/NoticeStack/notice';
import { ReasonField, reasonIssueKey } from '@/components/ReasonField/ReasonField';
import { reasonLimits } from '@/components/ReasonField/limits';
import { TimestampWithOffset } from '@/components/TimestampWithOffset/TimestampWithOffset';
import {
  ACTION_TYPES,
  classifyPort,
  localFailure,
  writeNotice,
  type Action,
  type ActionBody,
} from '@/ports/scr09Classify';
import { policyFor, screenKey, useScreenMutation, useScreenQuery } from '@/state/query';
import { text } from '@/texts';
import { label } from '@/texts/labels';
import type { LoopContext } from '../classify/common';

const DESCRIPTION_LIMITS = reasonLimits(assignBody, 'action_description');
const EXPECTED_MAX = 1000;

const ACTION_FIELDS: Readonly<Record<string, string>> = {
  loop_action_type_not_allowed: 'action_type',
  loop_action_on_person_forbidden: 'action_description',
  loop_blocked_vocabulary_term: 'action_description',
  loop_person_attribution_detected: 'action_description',
  loop_reason_too_short: 'action_description',
  loop_deadline_out_of_range: 'deadline_days',
};

function Anchored({ id, notice }: { readonly id: string; readonly notice: Notice | undefined }) {
  return notice === undefined ? null : <FieldNotice id={id} notice={notice} />;
}

export function ActionForm({
  context,
  plantId,
  timeZone,
  onWritten,
}: {
  readonly context: LoopContext;
  readonly plantId: string;
  readonly timeZone: string;
  readonly onWritten: () => void;
}) {
  const id = useId();
  const canReadConfiguration = context.permissions.includes('hierarchy.read');
  const configuration = useScreenQuery({
    key: screenKey({
      screen: 'scr09',
      operation: 'GET /loop/plants/{plant_id}/configuration',
      scope: context.scope,
      concession: context.concession,
      params: { plant_id: plantId },
    }),
    policy: policyFor('GET /loop/plants/{plant_id}/configuration'),
    enabled: canReadConfiguration,
    read: ({ signal }) => classifyPort.configuration.read({ path: { plant_id: plantId } }, signal),
  });
  const mutation = useScreenMutation({
    write: (body: ActionBody) =>
      classifyPort.assign.write({ path: { finding_state_id: context.findingId }, body }),
    invalidates: { screen: 'scr09', resources: ['actions', 'findings'] },
  });
  const [type, setType] = useState('');
  const [description, setDescription] = useState('');
  const [expected, setExpected] = useState('');
  const [assignee, setAssignee] = useState(context.user?.id ?? '');
  const [deadline, setDeadline] = useState<string | null>(null);
  const [showErrors, setShowErrors] = useState(false);
  const [saved, setSaved] = useState<Action | null>(null);
  const settings = configuration.data;
  const deadlineValue =
    deadline ?? (settings === undefined ? '' : String(settings.action_deadline_default_days));
  const notice = writeNotice(mutation.error, ACTION_FIELDS);
  const at = (field: string) => (notice?.field === field ? notice : undefined);
  const formNotice =
    notice !== null && (notice.field === 'form' || notice.field === null) ? notice : null;
  const local = localFailure(mutation.error);
  const edit =
    <T,>(set: (value: T) => void) =>
    (value: T) => {
      mutation.reset();
      setSaved(null);
      set(value);
    };
  return (
    <form
      aria-label={text('scr09.loop.action_form')}
      aria-describedby={formNotice === null ? `${id}-hint` : `${id}-hint ${id}-form`}
      noValidate
      onSubmit={(event) => {
        event.preventDefault();
        setShowErrors(true);
        if (
          type === '' ||
          assignee === '' ||
          reasonIssueKey(description, DESCRIPTION_LIMITS) !== null
        )
          return;
        void mutation
          .run({
            action_type: type,
            action_description: description,
            expected_outcome: expected.trim() === '' ? null : expected,
            assignee_user_id: assignee,
            deadline_days: deadlineValue === '' ? null : Number(deadlineValue),
          })
          .then((result) => {
            setSaved(result);
            setShowErrors(false);
            onWritten();
          })
          .catch(() => undefined);
      }}
    >
      <h2>{text('scr09.loop.action_form')}</h2>
      <p id={`${id}-hint`}>{text('scr09.loop.action_hint')}</p>
      {formNotice !== null && <FieldNotice id={`${id}-form`} notice={formNotice} />}
      {local === 'invalid_input' && <p role="alert">{text('scr09.loop.invalid')}</p>}
      {local === 'unavailable' && <p role="alert">{text('screen_state.unavailable')}</p>}
      <label>
        {text('scr09.loop.action_type')}
        <select
          name="action_type"
          value={type}
          aria-invalid={(showErrors && type === '') || at('action_type') !== undefined}
          aria-describedby={at('action_type') === undefined ? undefined : `${id}-type`}
          onChange={(event) => {
            edit(setType)(event.currentTarget.value);
          }}
        >
          <option value="">{text('scr09.classify.choose')}</option>
          {ACTION_TYPES.map((value) => (
            <option key={value} value={value}>
              {label('action_type', value)}
            </option>
          ))}
        </select>
      </label>
      <Anchored id={`${id}-type`} notice={at('action_type')} />
      <ReasonField
        label={text('scr09.loop.action_description')}
        name="action_description"
        value={description}
        limits={DESCRIPTION_LIMITS}
        showErrors={showErrors}
        notice={at('action_description')}
        onChange={edit(setDescription)}
      />
      <label>
        {text('scr09.loop.expected_outcome')}
        <textarea
          name="expected_outcome"
          value={expected}
          maxLength={EXPECTED_MAX}
          onChange={(event) => {
            edit(setExpected)(event.currentTarget.value);
          }}
        />
      </label>
      <label>
        {text('scr09.loop.assignee')}
        <select
          name="assignee_user_id"
          value={assignee}
          onChange={(event) => {
            edit(setAssignee)(event.currentTarget.value);
          }}
        >
          {context.user !== null && <option value={context.user.id}>{context.user.name}</option>}
        </select>
      </label>
      <label>
        {text('scr09.loop.deadline_days')}
        {settings !== undefined && (
          <>
            {' '}
            ({text('scr09.loop.deadline_default')}: {settings.action_deadline_default_days})
          </>
        )}
        <input
          type="number"
          name="deadline_days"
          step={1}
          min={settings?.action_deadline_min_days}
          max={settings?.action_deadline_max_days}
          value={deadlineValue}
          aria-invalid={at('deadline_days') !== undefined}
          aria-describedby={at('deadline_days') === undefined ? undefined : `${id}-deadline`}
          onChange={(event) => {
            edit(setDeadline)(event.currentTarget.value);
          }}
        />
      </label>
      <Anchored id={`${id}-deadline`} notice={at('deadline_days')} />
      {(!canReadConfiguration || configuration.error !== null) && (
        <p>{text('scr09.loop.deadline_unavailable')}</p>
      )}
      <button type="submit" disabled={mutation.status === 'pending'}>
        {text('scr09.loop.submit')}
      </button>
      {saved !== null && (
        <p role="status" data-part="action_saved">
          {text('scr09.loop.action_saved')}:{' '}
          <TimestampWithOffset instant={saved.due_at} timeZone={timeZone} />
        </p>
      )}
    </form>
  );
}
