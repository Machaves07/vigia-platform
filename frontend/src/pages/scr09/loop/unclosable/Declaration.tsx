/** Cinco campos y fecha civil de revisión; el servidor decide la transición y la política. */
import { useState } from 'react';
import { ReasonField, reasonIssueKey } from '@/components/ReasonField/ReasonField';
import { SignatureBlock, type SignerProfile } from '@/components/SignatureBlock/SignatureBlock';
import { StepWizard } from '@/components/StepWizard/StepWizard';
import {
  closureClock,
  closureNotice,
  closurePort,
  type DeclarationBody,
} from '@/ports/scr09Closure';
import { policyFor, screenKey, useScreenMutation, useScreenQuery } from '@/state/query';
import { text } from '@/texts';
import { Failure, type ClosureContext } from '../closure/common';
import { reviewInstant, validReviewDate } from '../closure/dates';
import { LONG_TEXT } from '../closure/Evidence';

const TEXTS = ['evaluated_alternative', 'rejection_reason', 'compensatory_measure'] as const;
export function Declaration({
  context,
  plantId,
  timeZone,
  onWritten,
}: {
  readonly context: ClosureContext;
  readonly plantId: string;
  readonly timeZone: string;
  readonly onWritten: () => void;
}) {
  const [draft, setDraft] = useState({
    evaluated_alternative: '',
    rejection_reason: '',
    compensatory_measure: '',
    responsible_user_id: context.principal?.user_id ?? '',
    decision_date: '',
    review_due_at: '',
  });
  const [signed, setSigned] = useState<SignerProfile | null>(null);
  const [dirty, setDirty] = useState(false);
  const [invalid, setInvalid] = useState(false);
  const mutation = useScreenMutation({
    write: (body: DeclarationBody) =>
      closurePort.declare.write({ path: { finding_state_id: context.findingId }, body }),
    invalidates: { screen: 'scr09', resources: ['findings', 'unclosable', 'ledger_records'] },
  });
  const policy = useScreenQuery({
    key: screenKey({
      screen: 'scr09',
      operation: 'GET /plants/{plant_id}/policy',
      scope: context.scope,
      concession: context.concession,
      params: { plant_id: plantId },
    }),
    policy: policyFor('GET /plants/{plant_id}/policy'),
    enabled: context.permissions.includes('hierarchy.read'),
    read: ({ signal }) => closurePort.policy.read({ path: { plant_id: plantId } }, signal),
  });
  const notice = closureNotice(mutation.error, { loop_signer_license_missing: 'signature' });
  const change = (field: keyof typeof draft, value: string) => {
    setDraft({ ...draft, [field]: value });
    setSigned(null);
    setDirty(true);
    mutation.reset();
  };
  const dateValid = validReviewDate(draft.review_due_at, closureClock(), timeZone);
  const textSteps = TEXTS.map((field) => ({
    id: field,
    title: text(`scr09.unclosable.${field}`),
    complete: reasonIssueKey(draft[field], LONG_TEXT) === null,
    content: (
      <ReasonField
        name={field}
        label={text(`scr09.unclosable.${field}`)}
        value={draft[field]}
        limits={LONG_TEXT}
        onChange={(value) => {
          change(field, value);
        }}
      />
    ),
  }));
  return (
    <>
      <section aria-label={text('scr09.unclosable.policy')}>
        <h2>{text('scr09.unclosable.policy')}</h2>
        <p>{text('scr09.unclosable.policy_guide')}</p>
        {policy.data !== undefined && (
          <p>
            {policy.data.criteria_summary_es} · {policy.data.version} ·{' '}
            {policy.data.signed_by_display_name}
          </p>
        )}
        <Failure error={policy.error} />
      </section>
      <Failure error={mutation.error} />
      {notice?.field === 'signature' && (
        <p>{text('components_forms.signature.guide_license_missing')}</p>
      )}
      {invalid && <p role="alert">{text('scr09.unclosable.review_range')}</p>}
      <StepWizard
        title={text('scr09.unclosable.title')}
        dirty={dirty}
        steps={[
          ...textSteps.slice(0, 2),
          {
            id: 'responsible',
            title: text('scr09.unclosable.responsible_user_id'),
            complete: draft.responsible_user_id !== '',
            content: (
              <label>
                {text('scr09.unclosable.responsible_user_id')}
                <select
                  name="responsible_user_id"
                  value={draft.responsible_user_id}
                  onChange={(e) => {
                    change('responsible_user_id', e.target.value);
                  }}
                >
                  {context.principal !== null && (
                    <option value={context.principal.user_id}>
                      {context.principal.display_name}
                    </option>
                  )}
                </select>
              </label>
            ),
          },
          {
            id: 'decision',
            title: text('scr09.unclosable.decision_date'),
            complete: /^\d{4}-\d{2}-\d{2}$/.test(draft.decision_date),
            content: (
              <label>
                {text('scr09.unclosable.decision_date')}
                <input
                  type="date"
                  name="decision_date"
                  value={draft.decision_date}
                  onChange={(e) => {
                    change('decision_date', e.target.value);
                  }}
                />
              </label>
            ),
          },
          ...textSteps.slice(2),
          {
            id: 'review',
            title: text('scr09.unclosable.review_due_at'),
            complete: dateValid && signed !== null,
            content: (
              <>
                <label>
                  {text('scr09.unclosable.review_due_at')}
                  <input
                    type="date"
                    name="review_due_at"
                    value={draft.review_due_at}
                    aria-invalid={draft.review_due_at !== '' && !dateValid}
                    aria-describedby="review-range"
                    onChange={(e) => {
                      change('review_due_at', e.target.value);
                    }}
                  />
                </label>
                <p id="review-range">{text('scr09.unclosable.review_range')}</p>
                {context.principal !== null && (
                  <SignatureBlock
                    principal={{
                      display_name: context.principal.display_name,
                      professional_license: context.principal.professional_license ?? null,
                    }}
                    textToSign={`${text('scr09.unclosable.sign_text')}\n${TEXTS.map((field) => draft[field]).join('\n')}\n${draft.decision_date}\n${draft.review_due_at}`}
                    onSign={setSigned}
                    notice={notice?.field === 'signature' ? notice : undefined}
                  />
                )}
              </>
            ),
          },
        ]}
        summary={
          <dl>
            {Object.entries(draft).map(([field, value]) => (
              <div key={field}>
                <dt>{text(`scr09.unclosable.${field}`)}</dt>
                <dd>{field === 'responsible_user_id' ? context.principal?.display_name : value}</dd>
              </div>
            ))}
          </dl>
        }
        onSubmit={async () => {
          if (
            !validReviewDate(draft.review_due_at, closureClock(), timeZone) ||
            signed === null ||
            TEXTS.some((field) => reasonIssueKey(draft[field], LONG_TEXT) !== null)
          ) {
            setInvalid(true);
            throw new Error('Fecha o campos de declaración no válidos.');
          }
          try {
            await mutation.run({
              ...draft,
              review_due_at: reviewInstant(draft.review_due_at, timeZone),
              professional_license: signed.professional_license,
            });
            setDirty(false);
            setInvalid(false);
            onWritten();
          } catch (error) {
            setSigned(null);
            throw error;
          }
        }}
      />
    </>
  );
}
