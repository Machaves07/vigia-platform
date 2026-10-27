/**
 * SCR-09, grupo `classify` (IF-08 pasos 3 y 4; SUP-U05-03 y 04): tres salidas con categoría y
 * motivo. Reclasificar registra una clasificación nueva y la anterior sigue visible; no existe
 * editar ni borrar (BR-APP-24). Los rechazos del servidor se anclan y conservan lo escrito.
 *
 * Aviso previo por estado (VIG-359): con el hallazgo consultado, antes de enviar, `in_review`
 * avisa de que se resuelve en la cola (con su enlace) y cualquier estado posterior a `received`
 * avisa de que es una reclasificación con motivo reforzado, y, en lazo abierto, de que lo
 * suspende (BR-LAZ-24). El formulario sigue disponible: el servidor decide (BR-APP-43).
 */
import { useId, useState } from 'react';
import { FieldNotice } from '@/components/NoticeStack/NoticeStack';
import { ReasonField, reasonIssueKey } from '@/components/ReasonField/ReasonField';
import { WriteReceipt } from '@/components/WriteReceipt/WriteReceipt';
import { FindingActions, findingAccess, RESUMING_STATE } from '@/pages/shared/FindingActions';
import {
  CLASSIFICATION_REASON_LIMITS,
  classifyPort,
  localFailure,
  loopIsOpen,
  OUTCOMES,
  reasonCategories,
  writeNotice,
  type ClassificationBody,
  type FindingDetail,
} from '@/ports/scr09Classify';
import { useScreenMutation } from '@/state/query';
import { text } from '@/texts';
import { label } from '@/texts/labels';
import {
  LoopNav,
  plantTimeZone,
  Refresh,
  Result,
  useAuditedRead,
  useLoopContext,
  type LoopContext,
} from './common';
import { ClassificationHistory } from './History';

const REASON_FIELDS: Readonly<Record<string, string>> = {
  loop_reason_required: 'reason_text',
  loop_reason_too_short: 'reason_text',
  loop_blocked_vocabulary_term: 'reason_text',
  loop_person_attribution_detected: 'reason_text',
};

interface Written {
  readonly classification_id: string;
  readonly outcome: string;
  readonly sequence: number;
}

function ClassifyForm({
  context,
  detail,
  previous,
  onWritten,
}: {
  readonly context: LoopContext;
  readonly detail: FindingDetail | undefined;
  readonly previous: number;
  readonly onWritten: (written: Written) => void;
}) {
  const formId = useId();
  const [outcome, setOutcome] = useState('');
  const [category, setCategory] = useState('');
  const [reason, setReason] = useState('');
  const [reinforced, setReinforced] = useState(false);
  const [showErrors, setShowErrors] = useState(false);
  const mutation = useScreenMutation({
    write: (body: ClassificationBody) =>
      classifyPort.classify.write({ path: { finding_state_id: context.findingId }, body }),
    invalidates: { screen: 'scr09', resources: ['classifications', 'findings'] },
  });
  const notice = writeNotice(mutation.error, REASON_FIELDS);
  const local = localFailure(mutation.error);
  const categories = reasonCategories(outcome);
  // El estado del servidor dice si es una reclasificación (VIG-359): un hallazgo confirmado en la
  // cola está abierto sin ninguna clasificación, y reclasificarlo exige el motivo reforzado.
  const state = detail?.lifecycle_state ?? null;
  const access = state === null ? null : findingAccess(state);
  const reclassification = access === null ? previous > 0 : access.includes('reclassify');
  const inReview = access?.includes('review_queue') ?? false;
  const missing = showErrors && (outcome === '' || category === '');
  const formNotice = notice !== null && notice.field !== 'reason_text' ? notice : null;
  const described = [
    inReview || reclassification ? `${formId}-state` : null,
    formNotice === null ? null : `${formId}-notice`,
  ].filter((id) => id !== null);
  return (
    <form
      aria-label={text('scr09.classify.form')}
      aria-describedby={described.length === 0 ? undefined : described.join(' ')}
      noValidate
      onSubmit={(event) => {
        event.preventDefault();
        setShowErrors(true);
        if (
          outcome === '' ||
          category === '' ||
          reasonIssueKey(reason, CLASSIFICATION_REASON_LIMITS) !== null
        )
          return;
        void mutation
          .run({
            outcome,
            reason_category: category,
            reason_text: reason,
            reinforced_reason: reinforced,
          })
          .then((result) => {
            setOutcome('');
            setCategory('');
            setReason('');
            setReinforced(false);
            setShowErrors(false);
            onWritten({
              classification_id: result.classification_id,
              outcome: result.outcome,
              sequence: result.sequence_in_subject,
            });
          })
          .catch(() => undefined);
      }}
    >
      <h2>{text('scr09.classify.form')}</h2>
      {formNotice !== null && <FieldNotice id={`${formId}-notice`} notice={formNotice} />}
      {local === 'invalid_input' && <p role="alert">{text('scr09.classify.invalid')}</p>}
      {local === 'unavailable' && <p role="alert">{text('screen_state.unavailable')}</p>}
      {inReview && detail !== undefined && (
        <div role="note" id={`${formId}-state`} data-part="in_review">
          <p>{text('finding_journey.in_review')}</p>
          <FindingActions
            findingId={context.findingId}
            state={detail.lifecycle_state}
            detectionId={detail.subject_kind === 'detection' ? detail.subject_id : null}
            permissions={new Set(context.permissions)}
          />
        </div>
      )}
      {reclassification && (
        <div id={`${formId}-state`} data-part="reclassification">
          <p>
            {text(
              previous > 0 ? 'scr09.classify.reclassification' : 'finding_journey.reclassification',
            )}
          </p>
          {state !== null && loopIsOpen(state) && (
            <p role="note" data-part="suspends">
              {text('scr09.classify.suspends')}
            </p>
          )}
          {state === RESUMING_STATE && (
            <p role="note" data-part="resumes">
              {text('finding_journey.resumes')}
            </p>
          )}
        </div>
      )}
      <fieldset>
        <legend>{text('scr09.classify.outcome')}</legend>
        {OUTCOMES.map((value) => (
          <label key={value}>
            <input
              type="radio"
              name="outcome"
              value={value}
              checked={outcome === value}
              onChange={() => {
                mutation.reset();
                setOutcome(value);
                setCategory('');
              }}
            />
            {label('classification_outcome', value)}
          </label>
        ))}
      </fieldset>
      <label>
        {text('scr09.classify.reason_category')}
        <select
          name="reason_category"
          value={category}
          aria-invalid={missing && category === ''}
          onChange={(event) => {
            mutation.reset();
            setCategory(event.currentTarget.value);
          }}
        >
          <option value="">{text('scr09.classify.choose')}</option>
          {categories.values.map((value) => (
            <option key={value} value={value}>
              {label(categories.list, value)}
            </option>
          ))}
        </select>
      </label>
      {missing && <p role="alert">{text('scr09.classify.missing')}</p>}
      <ReasonField
        label={text('scr09.classify.reason_text')}
        name="reason_text"
        value={reason}
        limits={CLASSIFICATION_REASON_LIMITS}
        showErrors={showErrors}
        notice={notice?.field === 'reason_text' ? notice : undefined}
        onChange={(value) => {
          mutation.reset();
          setReason(value);
        }}
      />
      {reclassification && (
        <label>
          <input
            type="checkbox"
            name="reinforced_reason"
            checked={reinforced}
            onChange={(event) => {
              mutation.reset();
              setReinforced(event.currentTarget.checked);
            }}
          />
          {text('scr09.classify.reinforced')}
        </label>
      )}
      <button type="submit" disabled={mutation.status === 'pending'}>
        {text('scr09.classify.submit')}
      </button>
    </form>
  );
}

export default function Page() {
  const context = useLoopContext();
  const [written, setWritten] = useState<Written | null>(null);
  const path = { finding_state_id: context.findingId };
  const detail = useAuditedRead(context, 'GET /loop/findings/{finding_state_id}', (signal) =>
    classifyPort.detail.read({ path }, signal),
  );
  const history = useAuditedRead(
    context,
    'GET /loop/findings/{finding_state_id}/classifications',
    (signal) => classifyPort.history.read({ path }, signal),
  );
  const timeZone = plantTimeZone(context, detail.data?.plant_id);
  const receipt = history.data?.classifications.find(
    (item) => item.classification_id === written?.classification_id,
  );
  return (
    <main>
      <LoopNav findingId={context.findingId} />
      <h1>{text('scr09.classify.title')}</h1>
      <p>{text('scr09.classify.audited')}</p>
      <button
        type="button"
        disabled={detail.isFetching || history.isFetching}
        onClick={() => {
          detail.request();
          history.request();
        }}
      >
        {text('scr09.classify.load')}
      </button>
      {written !== null &&
        (receipt === undefined ? (
          <p role="status">{text('scr09.classify.receipt_pending')}</p>
        ) : (
          <WriteReceipt
            record={`${text('scr09.classify.receipt')} ${String(written.sequence)}: ${label('classification_outcome', written.outcome)}`}
            roleInUse={receipt.classified_by.role_in_use}
          />
        ))}
      {history.requested && (
        <section aria-label={text('scr09.classify.history')}>
          <h2>{text('scr09.classify.history')}</h2>
          <Refresh
            query={history}
            operation="GET /loop/findings/{finding_state_id}/classifications"
          />
          <Result query={history}>
            {history.data !== undefined && (
              <>
                <ClassificationHistory
                  classifications={history.data.classifications}
                  reviewResolutions={history.data.review_resolutions}
                  timeAggregated={history.data.time_aggregated}
                  timeZone={timeZone}
                />
                <ClassifyForm
                  context={context}
                  detail={detail.data}
                  previous={Math.max(
                    history.data.classifications.length,
                    detail.data?.classification_count ?? 0,
                  )}
                  onWritten={(value) => {
                    setWritten(value);
                    // La escritura confirmada es la acción explícita que vuelve a pedir lo auditado.
                    history.request();
                    detail.request();
                  }}
                />
              </>
            )}
          </Result>
        </section>
      )}
    </main>
  );
}
