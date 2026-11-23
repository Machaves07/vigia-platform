/** Acción nueva completa, reutilizada solo dentro de las subrutas de VIG-328. */
import reopenBody from '../../../../../openapi/bodies/reopen_loop_findings__finding_state_id__reopening_post.json';
import { reasonLimits } from '@/components/ReasonField/limits';
import {
  optionalText,
  ReasonField,
  reasonIssueKey,
  submittedText,
} from '@/components/ReasonField/ReasonField';
import { ACTION_TYPES, closurePort, type ReopeningBody } from '@/ports/scr09Closure';
import { policyFor, screenKey, useScreenQuery } from '@/state/query';
import { text } from '@/texts';
import { label } from '@/texts/labels';
import type { ClosureContext } from '../closure/common';
import { AssigneePicker } from '../AssigneePicker';

export type ActionDraft = Omit<ReopeningBody, 'reason_text'>;

/** Límites de la acción nueva, del esquema generado de la reapertura (BR-APP-44). */
function actionLimits() {
  return {
    description: reasonLimits(reopenBody, 'action_description'),
    expected: reasonLimits(reopenBody, 'expected_outcome'),
  };
}

export function emptyAction(userId: string): ActionDraft {
  return {
    action_type: '',
    action_description: '',
    expected_outcome: null,
    assignee_user_id: userId,
    deadline_days: null,
  };
}
export function validAction(action: ActionDraft): boolean {
  const limits = actionLimits();
  return (
    action.action_type !== '' &&
    action.assignee_user_id !== '' &&
    reasonIssueKey(action.action_description, limits.description, { loopText: true }) === null &&
    reasonIssueKey(action.expected_outcome ?? '', limits.expected, {
      loopText: true,
      optional: true,
    }) === null
  );
}
/** La acción que se envía: sus textos recortados en los extremos (A-126). */
export function actionPayload(action: ActionDraft): ActionDraft {
  return {
    ...action,
    action_description: submittedText(action.action_description),
    expected_outcome: optionalText(action.expected_outcome ?? ''),
  };
}
export function ActionFields({
  context,
  plantId,
  value,
  onChange,
  invalid = false,
}: {
  readonly context: ClosureContext;
  readonly plantId: string;
  readonly value: ActionDraft;
  readonly onChange: (value: ActionDraft) => void;
  readonly invalid?: boolean;
}) {
  const limits = actionLimits();
  const configuration = useScreenQuery({
    key: screenKey({
      screen: 'scr09',
      operation: 'GET /loop/plants/{plant_id}/configuration',
      scope: context.scope,
      concession: context.concession,
      params: { plant_id: plantId },
    }),
    policy: policyFor('GET /loop/plants/{plant_id}/configuration'),
    enabled: context.permissions.includes('hierarchy.read'),
    read: ({ signal }) => closurePort.configuration.read({ path: { plant_id: plantId } }, signal),
  });
  return (
    <fieldset>
      <legend>{text('scr09.reopening.title')}</legend>
      <label>
        {text('scr09.reopening.action_type')}
        <select
          name="action_type"
          value={value.action_type}
          onChange={(e) => {
            onChange({ ...value, action_type: e.target.value });
          }}
        >
          <option value="">{text('scr09.closure.choose')}</option>
          {ACTION_TYPES.map((type) => (
            <option key={type} value={type}>
              {label('action_type', type)}
            </option>
          ))}
        </select>
      </label>
      <ReasonField
        name="action_description"
        label={text('scr09.reopening.action_description')}
        value={value.action_description}
        onChange={(action_description) => {
          onChange({ ...value, action_description });
        }}
        limits={limits.description}
        loopText
        showErrors={invalid}
      />
      <ReasonField
        name="expected_outcome"
        label={text('scr09.reopening.expected_outcome')}
        value={value.expected_outcome ?? ''}
        onChange={(expected) => {
          onChange({ ...value, expected_outcome: expected === '' ? null : expected });
        }}
        limits={limits.expected}
        optional
        loopText
        showErrors={invalid}
      />
      <AssigneePicker
        context={context}
        sessionUser={
          context.principal === null
            ? null
            : { id: context.principal.user_id, name: context.principal.display_name }
        }
        caption={text('scr09.reopening.assignee')}
        value={value.assignee_user_id}
        onChange={(assignee_user_id) => {
          onChange({ ...value, assignee_user_id });
        }}
        invalid={invalid}
      />
      <label>
        {text('scr09.reopening.deadline')}
        <input
          type="number"
          name="deadline_days"
          min={configuration.data?.action_deadline_min_days}
          max={configuration.data?.action_deadline_max_days}
          placeholder={String(configuration.data?.action_deadline_default_days ?? '')}
          value={value.deadline_days ?? ''}
          onChange={(e) => {
            onChange({
              ...value,
              deadline_days: e.target.value === '' ? null : Number(e.target.value),
            });
          }}
        />
      </label>
    </fieldset>
  );
}
