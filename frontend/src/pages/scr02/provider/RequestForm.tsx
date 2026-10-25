/**
 * Solicitud de concesión del proveedor (IF-06 paso 3; BR-APP-51; VIG-322), en dos pasos:
 *
 * 1. Formulario: organización cliente, alcance (organización o planta), motivo de 10 a 500
 *    (`ReasonField`, límites del esquema generado) y vigencia en horas entre 1 y el tope del
 *    esquema. El tope de cada cliente (`concession_max_days`) puede ser menor: solo lo conoce el
 *    servidor y la pantalla no lo replica (BR-APP-43).
 * 2. Vista previa «el cliente verá: quién, motivo, vigencia y cada consulta». Solo desde ella se
 *    envía `POST /provider/concessions`.
 *
 * Si el servidor rechaza, se vuelve al formulario con todo lo escrito. Un `conflict` (vigencia por
 * encima del tope del cliente, según el diseño de SCR-02) se ancla al campo de la vigencia; otro
 * rechazo anclado (`invalid_request`, que no dice qué campo falló), al formulario.
 */
import { useId, useState } from 'react';

import { ReasonField, reasonIssueKey } from '@/components/ReasonField/ReasonField';
import { FieldNotice } from '@/components/NoticeStack/NoticeStack';
import {
  concessionsPort,
  GRANT_DURATION_HOURS,
  GRANT_REASON_LIMITS,
  GRANT_SCOPE_LEVELS,
  writeNotice,
  type GrantBody,
  type GrantScopeLevel,
  type ProviderConcession,
} from '@/ports/scr02Concessions';
import { useScreenMutation } from '@/state/query';
import { useUiSession } from '@/state/session';
import { text } from '@/texts';
import { label } from '@/texts/labels';

import { WriteNotices } from '../shared';

/** Identificador con la forma `format: uuid` del contrato. */
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const HOURS = /^[0-9]{1,6}$/;

interface Draft {
  readonly client: string;
  readonly level: GrantScopeLevel;
  readonly plant: string;
  readonly reason: string;
  readonly hours: string;
}

const EMPTY: Draft = { client: '', level: 'organization', plant: '', reason: '', hours: '' };

type FieldName = 'client' | 'plant' | 'reason' | 'hours';

function draftIssues(draft: Draft): Partial<Record<FieldName, string>> {
  const issues: Partial<Record<FieldName, string>> = {};
  if (!UUID.test(draft.client.trim())) issues.client = 'scr02.request.invalid_id';
  if (draft.level === 'plant' && !UUID.test(draft.plant.trim())) {
    issues.plant = 'scr02.request.invalid_id';
  }
  const reason = reasonIssueKey(draft.reason, GRANT_REASON_LIMITS);
  if (reason !== null) issues.reason = reason;
  const hours = Number(draft.hours.trim());
  if (
    !HOURS.test(draft.hours.trim()) ||
    hours < GRANT_DURATION_HOURS.min ||
    hours > GRANT_DURATION_HOURS.max
  ) {
    issues.hours = 'scr02.request.invalid_duration';
  }
  return issues;
}

function grantBody(draft: Draft): GrantBody {
  const client = draft.client.trim().toLowerCase();
  return {
    client_organization_id: client,
    scope_level: draft.level,
    scope_id: draft.level === 'organization' ? client : draft.plant.trim().toLowerCase(),
    reason: draft.reason,
    duration_hours: Number(draft.hours.trim()),
  };
}

/** Campo del formulario al que se ancla un rechazo del servidor. */
function anchorFor(code: string): string {
  return code === 'conflict' ? 'duration_hours' : 'form';
}

function fill(template: string, values: Readonly<Record<string, string>>): string {
  return template.replace(/\{([a-z_]+)\}/g, (whole, name: string) => values[name] ?? whole);
}

function TextInput({
  label: caption,
  name,
  value,
  issue,
  describedBy,
  hint,
  inputMode,
  onChange,
}: {
  readonly label: string;
  readonly name: string;
  readonly value: string;
  readonly issue: string | undefined;
  /** Aviso del servidor anclado a este campo, si lo hay. */
  readonly describedBy?: string | undefined;
  readonly hint?: string;
  readonly inputMode?: 'numeric';
  readonly onChange: (value: string) => void;
}) {
  const id = useId();
  const described = [
    hint === undefined ? null : `${id}-hint`,
    issue === undefined ? null : `${id}-error`,
    describedBy ?? null,
  ].filter((part): part is string => part !== null);
  return (
    <div>
      <label htmlFor={id}>{caption}</label>
      <input
        id={id}
        type="text"
        name={name}
        autoComplete="off"
        value={value}
        aria-invalid={issue !== undefined || describedBy !== undefined}
        aria-describedby={described.length === 0 ? undefined : described.join(' ')}
        {...(inputMode === undefined ? {} : { inputMode })}
        onChange={(event) => {
          onChange(event.currentTarget.value);
        }}
      />
      {hint === undefined ? null : <p id={`${id}-hint`}>{hint}</p>}
      {issue === undefined ? null : (
        <p id={`${id}-error`} role="alert" data-text-key={issue}>
          {text(issue)}
        </p>
      )}
    </div>
  );
}

export function RequestForm({
  onGranted,
  onCancel,
}: {
  readonly onGranted: (granted: ProviderConcession) => void;
  readonly onCancel: () => void;
}) {
  const formId = useId();
  const { principal } = useUiSession();
  const [draft, setDraft] = useState<Draft>(EMPTY);
  const [step, setStep] = useState<'edit' | 'preview'>('edit');
  const [attempted, setAttempted] = useState(false);
  const mutation = useScreenMutation({
    write: (body: GrantBody) => concessionsPort.grant.write({ body }),
    invalidates: { screen: 'scr02', resources: ['concessions'] },
  });
  const notice = writeNotice(mutation.error, formId, anchorFor);
  const durationNotice = notice?.field === 'duration_hours' ? notice : undefined;
  const durationNoticeId = `${formId}-duration-notice`;
  const issues = draftIssues(draft);
  const shown = attempted ? issues : {};

  const update = (patch: Partial<Draft>) => {
    mutation.reset();
    setDraft((current) => ({ ...current, ...patch }));
  };

  if (step === 'preview') {
    const body = grantBody(draft);
    const who = `${principal?.display_name ?? text('common.unknown')} · ${
      principal?.organization.name ?? text('common.unknown')
    }`;
    return (
      <section aria-labelledby={`${formId}-preview`}>
        <h3 id={`${formId}-preview`}>{text('scr02.preview.title')}</h3>
        <p>{text('scr02.preview.intro')}</p>
        <dl>
          <dt>{text('scr02.preview.who')}</dt>
          <dd>{who}</dd>
          <dt>{text('scr02.preview.scope')}</dt>
          <dd>
            {label('scope_level', body.scope_level)}: <code>{body.scope_id}</code>
          </dd>
          <dt>{text('scr02.preview.reason')}</dt>
          <dd data-part="reason">{body.reason}</dd>
          <dt>{text('scr02.preview.validity')}</dt>
          <dd>{fill(text('scr02.preview.validity_value'), { hours: draft.hours.trim() })}</dd>
          <dt>{text('scr02.preview.queries')}</dt>
          <dd>{text('scr02.preview.queries_value')}</dd>
        </dl>
        <WriteNotices notice={notice} error={mutation.error} />
        <button
          type="button"
          disabled={mutation.status === 'pending'}
          onClick={() => {
            void mutation
              .run(body)
              .then((granted) => {
                onGranted(granted);
              })
              .catch(() => {
                // Lo escrito se conserva: se vuelve al formulario con el aviso anclado.
                setStep('edit');
              });
          }}
        >
          {text('scr02.preview.submit')}
        </button>
        <button
          type="button"
          onClick={() => {
            mutation.reset();
            setStep('edit');
          }}
        >
          {text('scr02.preview.back')}
        </button>
      </section>
    );
  }

  return (
    <form
      aria-label={text('scr02.request.title')}
      aria-describedby={notice?.field === 'form' ? `${formId}-notice` : undefined}
      noValidate
      onSubmit={(event) => {
        event.preventDefault();
        setAttempted(true);
        if (Object.keys(issues).length > 0) return;
        mutation.reset();
        setStep('preview');
      }}
    >
      <h2>{text('scr02.request.title')}</h2>
      <WriteNotices notice={notice} error={mutation.error} anchoredId={`${formId}-notice`} />
      <TextInput
        label={text('scr02.request.client')}
        name="client_organization_id"
        value={draft.client}
        issue={shown.client}
        onChange={(client) => {
          update({ client });
        }}
      />
      <div>
        <label htmlFor={`${formId}-level`}>{text('scr02.request.level')}</label>
        <select
          id={`${formId}-level`}
          name="scope_level"
          value={draft.level}
          onChange={(event) => {
            const level = GRANT_SCOPE_LEVELS.find((item) => item === event.currentTarget.value);
            if (level !== undefined) update({ level });
          }}
        >
          {GRANT_SCOPE_LEVELS.map((level) => (
            <option key={level} value={level}>
              {label('scope_level', level)}
            </option>
          ))}
        </select>
      </div>
      {draft.level === 'plant' && (
        <TextInput
          label={text('scr02.request.plant')}
          name="scope_id"
          value={draft.plant}
          issue={shown.plant}
          onChange={(plant) => {
            update({ plant });
          }}
        />
      )}
      <ReasonField
        label={text('scr02.request.reason')}
        name="reason"
        value={draft.reason}
        limits={GRANT_REASON_LIMITS}
        showErrors={attempted}
        onChange={(reason) => {
          update({ reason });
        }}
      />
      <TextInput
        label={text('scr02.request.duration')}
        name="duration_hours"
        value={draft.hours}
        issue={durationNotice === undefined ? shown.hours : undefined}
        describedBy={durationNotice === undefined ? undefined : durationNoticeId}
        hint={text('scr02.request.duration_hint')}
        inputMode="numeric"
        onChange={(hours) => {
          update({ hours });
        }}
      />
      {durationNotice === undefined ? null : (
        <FieldNotice id={durationNoticeId} notice={durationNotice} />
      )}
      <button type="submit">{text('scr02.request.review')}</button>
      <button type="button" onClick={onCancel}>
        {text('scr02.request.cancel')}
      </button>
    </form>
  );
}
