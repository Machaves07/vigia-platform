/** Acción nueva completa, reutilizada solo dentro de las subrutas de VIG-328. */
import { ReasonField, reasonIssueKey } from '@/components/ReasonField/ReasonField';
import { ACTION_TYPES, closurePort, type ReopeningBody } from '@/ports/scr09Closure';
import { policyFor, screenKey, useScreenQuery } from '@/state/query';
import { text } from '@/texts';
import { label } from '@/texts/labels';
import type { ClosureContext } from '../closure/common';
import { LONG_TEXT } from '../closure/Evidence';

export type ActionDraft = Omit<ReopeningBody, 'reason_text'>;
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
  return (
    action.action_type !== '' &&
    action.assignee_user_id !== '' &&
    reasonIssueKey(action.action_description, LONG_TEXT) === null
  );
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
        limits={LONG_TEXT}
        showErrors={invalid}
      />
      <label>
        {text('scr09.reopening.expected_outcome')}
        <textarea
          name="expected_outcome"
          maxLength={1000}
          value={value.expected_outcome ?? ''}
          onChange={(e) => {
            onChange({ ...value, expected_outcome: e.target.value || null });
          }}
        />
      </label>
      <label>
        {text('scr09.reopening.assignee')}
        <select
          name="assignee_user_id"
          value={value.assignee_user_id}
          onChange={(e) => {
            onChange({ ...value, assignee_user_id: e.target.value });
          }}
        >
          {context.principal !== null && (
            <option value={context.principal.user_id}>{context.principal.display_name}</option>
          )}
        </select>
      </label>
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
