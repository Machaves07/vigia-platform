/**
 * Editor de asignaciones (rol, nivel y alcance) de la invitación, la reactivación y «asignar un
 * rol». Solo comprueba que cada fila esté completa; si el rol se admite en ese alcance o es
 * compatible con otra asignación lo decide el servidor (`conflict` anclado, BR-APP-43).
 */
import { useId } from 'react';

import { ASSIGNABLE_ROLES, SCOPE_LEVELS, type AssignmentBody } from '@/ports/scr01Admin';
import { text } from '@/texts';
import { label } from '@/texts/labels';

import { scopeOptions, type ScopeDirectory } from '../scopes';

export interface AssignmentDraft {
  readonly role: string;
  readonly scope_level: string;
  readonly scope_id: string;
}

export const EMPTY_ASSIGNMENT: AssignmentDraft = { role: '', scope_level: '', scope_id: '' };

/** Las filas como cuerpo de la API, o `null` si alguna está incompleta o no hay ninguna. */
export function completeAssignments(drafts: readonly AssignmentDraft[]): AssignmentBody[] | null {
  if (drafts.length === 0) return null;
  const bodies: AssignmentBody[] = [];
  for (const draft of drafts) {
    const role = ASSIGNABLE_ROLES.find((value) => value === draft.role);
    const level = SCOPE_LEVELS.find((value) => value === draft.scope_level);
    if (role === undefined || level === undefined || draft.scope_id === '') return null;
    bodies.push({ role, scope_level: level, scope_id: draft.scope_id });
  }
  return bodies;
}

export function AssignmentRow({
  draft,
  directory,
  invalid,
  onChange,
}: {
  readonly draft: AssignmentDraft;
  readonly directory: ScopeDirectory;
  readonly invalid: boolean;
  readonly onChange: (draft: AssignmentDraft) => void;
}) {
  const id = useId();
  const options = scopeOptions(directory, draft.scope_level);
  return (
    <>
      <label htmlFor={`${id}-role`}>{text('scr01_admin.assignment.role')}</label>
      <select
        id={`${id}-role`}
        name="role"
        value={draft.role}
        aria-invalid={invalid && draft.role === ''}
        onChange={(event) => {
          onChange({ ...draft, role: event.currentTarget.value });
        }}
      >
        <option value="">{text('scr01_admin.assignment.choose')}</option>
        {ASSIGNABLE_ROLES.map((role) => (
          <option key={role} value={role}>
            {label('role', role)}
          </option>
        ))}
      </select>
      <label htmlFor={`${id}-level`}>{text('scr01_admin.assignment.level')}</label>
      <select
        id={`${id}-level`}
        name="scope_level"
        value={draft.scope_level}
        aria-invalid={invalid && draft.scope_level === ''}
        onChange={(event) => {
          onChange({ ...draft, scope_level: event.currentTarget.value, scope_id: '' });
        }}
      >
        <option value="">{text('scr01_admin.assignment.choose')}</option>
        {SCOPE_LEVELS.map((level) => (
          <option key={level} value={level}>
            {label('scope_level', level)}
          </option>
        ))}
      </select>
      <label htmlFor={`${id}-scope`}>{text('scr01_admin.assignment.scope')}</label>
      <select
        id={`${id}-scope`}
        name="scope_id"
        value={draft.scope_id}
        aria-invalid={invalid && draft.scope_id === ''}
        onChange={(event) => {
          onChange({ ...draft, scope_id: event.currentTarget.value });
        }}
      >
        <option value="">{text('scr01_admin.assignment.choose')}</option>
        {options.map((option) => (
          <option key={option.value} value={option.value}>
            {option.label}
          </option>
        ))}
      </select>
    </>
  );
}

/** Lista editable de asignaciones con «añadir otra» y «quitar esta». */
export function AssignmentList({
  drafts,
  directory,
  invalid,
  onChange,
}: {
  readonly drafts: readonly AssignmentDraft[];
  readonly directory: ScopeDirectory;
  readonly invalid: boolean;
  readonly onChange: (drafts: AssignmentDraft[]) => void;
}) {
  return (
    <>
      {drafts.map((draft, index) => (
        <fieldset key={index}>
          <legend>{`${text('scr01_admin.assignment.legend')} ${String(index + 1)}`}</legend>
          <AssignmentRow
            draft={draft}
            directory={directory}
            invalid={invalid}
            onChange={(changed) => {
              onChange(drafts.map((item, position) => (position === index ? changed : item)));
            }}
          />
          {drafts.length > 1 && (
            <button
              type="button"
              onClick={() => {
                onChange(drafts.filter((_, position) => position !== index));
              }}
            >
              {text('scr01_admin.assignment.remove_row')}
            </button>
          )}
        </fieldset>
      ))}
      <button
        type="button"
        onClick={() => {
          onChange([...drafts, EMPTY_ASSIGNMENT]);
        }}
      >
        {text('scr01_admin.assignment.add')}
      </button>
      {invalid && completeAssignments(drafts) === null && (
        <p role="alert" data-text-key="scr01_admin.assignment.incomplete">
          {text('scr01_admin.assignment.incomplete')}
        </p>
      )}
    </>
  );
}
