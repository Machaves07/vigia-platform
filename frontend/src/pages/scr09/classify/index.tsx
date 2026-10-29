/**
 * SCR-09, grupo `classify` (IF-08 pasos 3 y 4; SUP-U05-03 y 04): tres salidas con categoría y
 * motivo. Reclasificar registra una clasificación nueva y la anterior sigue visible; no existe
 * editar ni borrar (BR-APP-24). Los rechazos del servidor se anclan y conservan lo escrito.
 *
 * Aviso previo por estado (VIG-359): con el hallazgo consultado, antes de enviar, `in_review`
 * avisa de que se resuelve en la cola (con su enlace) y cualquier estado posterior a `received`
 * avisa de que es una reclasificación con motivo reforzado, y, en lazo abierto, de que lo
 * suspende (BR-LAZ-24). El formulario sigue disponible: el servidor decide (BR-APP-43).
 *
 * Con el hallazgo consultado, su resumen y sus clips van antes del formulario (VIG-363): se
 * clasifica viendo la evidencia, y cada clip pide su URL solo al pulsar reproducir.
 */
import { useId, useState } from 'react';
import { Link, useLocation } from 'react-router';
import { canEnterGroup } from '@/app/navigation';
import { FieldNotice } from '@/components/NoticeStack/NoticeStack';
import { ReasonField, reasonIssueKey } from '@/components/ReasonField/ReasonField';
import { WriteReceipt } from '@/components/WriteReceipt/WriteReceipt';
import { FindingActions, findingAccess, RESUMING_STATE } from '@/pages/shared/FindingActions';
import layout from '@/pages/shared/findingLayout.module.css';
import { openRequested } from '@/pages/shared/NextStepPanel';
import panel from '@/pages/shared/NextStepPanel.module.css';
import { PageRefresh } from '@/pages/shared/PageRefresh';
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
import { descriptor } from '../descriptor';
import {
  LoopNav,
  plantTimeZone,
  Result,
  useAuditedRead,
  useLoopContext,
  useOpening,
  zoneName,
  type LoopContext,
} from './common';
import { FindingEvidence } from './Evidence';
import { ClassificationHistory } from './History';

const DETAIL = 'GET /loop/findings/{finding_state_id}';
const HISTORY = 'GET /loop/findings/{finding_state_id}/classifications';

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

/** El acceso al lazo del recibo: solo si la sesión puede abrir su grupo de rutas (BR-APP-01). */
function canOpenLoop(permissions: readonly string[]): boolean {
  const group = descriptor.route_groups.find((candidate) => candidate.id === 'loop');
  return group !== undefined && canEnterGroup(group, new Set(permissions));
}

export default function Page() {
  const context = useLoopContext();
  const opening = useOpening();
  const location = useLocation();
  const [written, setWritten] = useState<Written | null>(null);
  // Reclasificar se abre a propósito (VIG-365, S3-20): con el botón o al llegar del panel
  // «Siguiente paso» con ese paso; cada apertura monta un formulario nuevo, sin errores previos.
  const [reclassifying, setReclassifying] = useState(() =>
    openRequested(location.state, 'reclassify'),
  );
  const [formKey, setFormKey] = useState(0);
  const path = { finding_state_id: context.findingId };
  const detail = useAuditedRead(context, DETAIL, opening, (signal) =>
    classifyPort.detail.read({ path }, signal),
  );
  const history = useAuditedRead(context, HISTORY, opening, (signal) =>
    classifyPort.history.read({ path }, signal),
  );
  const timeZone = plantTimeZone(context, detail.data?.plant_id);
  const receipt = history.data?.classifications.find(
    (item) => item.classification_id === written?.classification_id,
  );
  const state = detail.data?.lifecycle_state;
  // Solo la primera clasificación (o la cola, en revisión) se ofrece abierta: todo lo demás es una
  // reclasificación, que suspende o reanuda el lazo (BR-LAZ-24) y no se abre por descuido.
  const intentional = state === undefined || findingAccess(state).includes('reclassify');
  const formShown = state !== undefined && (reclassifying || (!intentional && written === null));
  return (
    <main className={layout.page}>
      <LoopNav findingId={context.findingId} />
      <header className={layout.header}>
        <div className={layout.heading}>
          <h1>{text('scr09.classify.title')}</h1>
        </div>
        <PageRefresh
          screen="scr09"
          reads={[
            { fetchedAt: detail.fetchedAt, operation: DETAIL },
            { fetchedAt: history.fetchedAt, operation: HISTORY },
          ]}
        />
      </header>
      <p>{text('scr09.classify.audited')}</p>
      {written !== null && (
        <div data-part="classification_receipt">
          {receipt === undefined ? (
            <p role="status">{text('scr09.classify.receipt_pending')}</p>
          ) : (
            <WriteReceipt
              record={`${text('scr09.classify.receipt')} ${String(written.sequence)}: ${label('classification_outcome', written.outcome)}`}
              roleInUse={receipt.classified_by.role_in_use}
            />
          )}
          {canOpenLoop(context.permissions) && (
            <>
              <p>{text('scr09.classify.receipt_next')}</p>
              <Link
                to={`/findings/${context.findingId}/loop`}
                className={panel.primary}
                data-primary="true"
              >
                {text('scr09.classify.go_to_loop')}
              </Link>
            </>
          )}
        </div>
      )}
      {detail.data !== undefined && (
        <FindingEvidence
          detail={detail.data}
          zone={zoneName(context, detail.data.zone_id)}
          timeZone={timeZone}
          canViewClips={context.permissions.includes('evidence.read')}
        />
      )}
      <section aria-label={text('scr09.classify.history')}>
        <h2>{text('scr09.classify.history')}</h2>
        <Result query={history}>
          {history.data !== undefined && (
            <>
              <ClassificationHistory
                classifications={history.data.classifications}
                reviewResolutions={history.data.review_resolutions}
                timeAggregated={history.data.time_aggregated}
                timeZone={timeZone}
              />
              {intentional && state !== undefined && (
                <button
                  type="button"
                  className={panel.secondary}
                  aria-expanded={reclassifying}
                  data-part="reclassify_toggle"
                  onClick={() => {
                    setReclassifying((open) => !open);
                    setFormKey((value) => value + 1);
                  }}
                >
                  {text(
                    reclassifying
                      ? 'scr09.classify.close_reclassification'
                      : 'scr09.classify.open_reclassification',
                  )}
                </button>
              )}
              {formShown && (
                <ClassifyForm
                  key={formKey}
                  context={context}
                  detail={detail.data}
                  previous={Math.max(
                    history.data.classifications.length,
                    detail.data?.classification_count ?? 0,
                  )}
                  onWritten={(value) => {
                    setWritten(value);
                    // Registrada, el formulario se pliega: reclasificar exige volver a abrirlo.
                    setReclassifying(false);
                    setFormKey((current) => current + 1);
                    // La escritura confirmada es la acción explícita que vuelve a pedir lo auditado.
                    history.request();
                    detail.request();
                  }}
                />
              )}
            </>
          )}
        </Result>
      </section>
    </main>
  );
}
