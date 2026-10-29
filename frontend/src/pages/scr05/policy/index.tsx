/**
 * SCR-05, grupo `policy`: `/plants/{plant_id}/policy` (IF-11 paso 2). La política de hallazgos
 * incerrables vigente de la planta y, con `commissioning.run`, la carga de la versión siguiente:
 * documento firmado por `DirectUploadField` (`POST /documents`, `plant_policy`) y después
 * `POST /plants/{plant_id}/policy` con versión, fecha, firmante, referencia al concepto del
 * abogado y resumen de criterios. La versión la fija el servidor (la anterior más uno): otra
 * responde `conflict`; la anterior permanece.
 */
import { DateInput } from '@/components/DateFields/DateFields';
import { fromZonedTime } from 'date-fns-tz';
import { useId, useState } from 'react';
import { Link, useParams } from 'react-router';
import { DirectUploadField } from '@/components/DirectUploadField/DirectUploadField';
import { gatesPort, type PlantPolicy, type PlantPolicyBody } from '@/ports/scr05Gates';
import { ScreenState } from '@/pages/shared/ScreenState';
import { useScreenMutation } from '@/state/query';
import { text } from '@/texts';
import {
  GatesRefresh,
  GatesResult,
  usePlantPolicy,
  useGatesContext,
  WriteFailure,
} from '../shared';
import { PlantPolicyView } from '../views';
import styles from '../scr05.module.css';

const OPERATION = 'GET /plants/{plant_id}/policy';
const DATE = /^\d{4}-\d{2}-\d{2}$/;

type Field = 'signed_at' | 'signed_by' | 'legal_opinion' | 'criteria' | 'document';

function PolicyForm({
  plantId,
  timeZone,
  current,
}: {
  readonly plantId: string;
  readonly timeZone: string;
  readonly current: PlantPolicy;
}) {
  const id = useId();
  const version = (current.version ?? 0) + 1;
  const [signedAt, setSignedAt] = useState('');
  const [signedBy, setSignedBy] = useState('');
  const [legalOpinion, setLegalOpinion] = useState('');
  const [criteria, setCriteria] = useState('');
  const [document, setDocument] = useState<Record<string, unknown> | null>(null);
  const [showErrors, setShowErrors] = useState(false);
  const mutation = useScreenMutation({
    write: (body: PlantPolicyBody) =>
      gatesPort.signPlantPolicy.write({ path: { plant_id: plantId }, body }),
    invalidates: { screen: 'scr05', resources: ['policy'] },
  });
  const missing: Record<Field, boolean> = {
    signed_at: !DATE.test(signedAt),
    signed_by: signedBy.trim() === '',
    legal_opinion: legalOpinion.trim() === '',
    criteria: criteria.trim() === '',
    document: document === null,
  };
  const error = (field: Field) => showErrors && missing[field];
  const describedBy = (field: Field) => (error(field) ? `${id}-${field}-error` : undefined);
  const required = (field: Field) =>
    error(field) ? (
      <p id={`${id}-${field}-error`} data-part="local_error" data-text-key="validation.required">
        {text('validation.required')}
      </p>
    ) : null;
  return (
    <section className={styles.panel} aria-labelledby={`${id}-title`} data-part="policy_form">
      <h2 id={`${id}-title`}>{text('scr05.policy.form_title')}</h2>
      <p>
        {text('scr05.policy.next_version')} <span data-part="next_version">{String(version)}</span>
      </p>
      <form
        noValidate
        className={styles.section}
        onSubmit={(event) => {
          event.preventDefault();
          setShowErrors(true);
          if (Object.values(missing).some(Boolean) || document === null) return;
          void mutation
            .run({
              version,
              signed_at: fromZonedTime(`${signedAt}T00:00:00`, timeZone).toISOString(),
              signed_by_display_name: signedBy.trim(),
              legal_opinion_reference: legalOpinion.trim(),
              criteria_summary_es: criteria.trim(),
              document_ref: document,
            })
            .catch(() => undefined);
        }}
      >
        <div className={styles.field}>
          <label htmlFor={`${id}-signed_at`}>{text('scr05.policy.signed_at')}</label>
          <DateInput
            id={`${id}-signed_at`}
            value={signedAt}
            required
            aria-invalid={error('signed_at')}
            aria-describedby={describedBy('signed_at')}
            onChange={setSignedAt}
          />
          {required('signed_at')}
        </div>
        <div className={styles.field}>
          <label htmlFor={`${id}-signed_by`}>{text('scr05.policy.signed_by')}</label>
          <input
            id={`${id}-signed_by`}
            value={signedBy}
            maxLength={120}
            required
            aria-required="true"
            aria-invalid={error('signed_by')}
            aria-describedby={describedBy('signed_by')}
            onChange={(event) => {
              setSignedBy(event.target.value);
            }}
          />
          {required('signed_by')}
        </div>
        <div className={styles.field}>
          <label htmlFor={`${id}-legal_opinion`}>{text('scr05.policy.legal_opinion')}</label>
          <input
            id={`${id}-legal_opinion`}
            value={legalOpinion}
            maxLength={120}
            required
            aria-required="true"
            aria-invalid={error('legal_opinion')}
            aria-describedby={describedBy('legal_opinion')}
            onChange={(event) => {
              setLegalOpinion(event.target.value);
            }}
          />
          {required('legal_opinion')}
        </div>
        <div className={styles.field}>
          <label htmlFor={`${id}-criteria`}>{text('scr05.policy.criteria')}</label>
          <textarea
            id={`${id}-criteria`}
            value={criteria}
            maxLength={2000}
            required
            aria-required="true"
            aria-invalid={error('criteria')}
            aria-describedby={describedBy('criteria')}
            onChange={(event) => {
              setCriteria(event.target.value);
            }}
          />
          {required('criteria')}
        </div>
        <DirectUploadField
          label={text('scr05.policy.document')}
          target={{ unit: 'catalog_document', plantId, kind: 'plant_policy' }}
          onUploaded={(result) => {
            if (result.unit === 'catalog_document') setDocument({ ...result.document_ref });
          }}
        />
        {required('document')}
        <div className={styles.actions}>
          <button type="submit" className={styles.button} disabled={mutation.status === 'pending'}>
            {text('scr05.policy.submit')}
          </button>
        </div>
      </form>
      {mutation.status === 'success' ? (
        <p role="status" data-part="policy_recorded">
          {text('scr05.policy.recorded')}
        </p>
      ) : null}
      <WriteFailure error={mutation.error} field="policy" />
    </section>
  );
}

function PlantPolicyPage({ plantId }: { readonly plantId: string }) {
  const context = useGatesContext();
  const plant = context.plants.find((entry) => entry.plant_id === plantId);
  const policy = usePlantPolicy(context, plantId);
  const timeZone = plant?.timezone ?? 'UTC';
  return (
    <main className={styles.page}>
      <nav aria-label={text('scr05.navigation')}>
        <Link to={`/plants/${plantId}/gates`}>{text('scr05.back_to_plant')}</Link>
      </nav>
      <h1>{text('scr05.policy.title')}</h1>
      {plant === undefined ? null : (
        <p data-part="plant">
          {text('scr05.plant')} {plant.name}
        </p>
      )}
      <section className={styles.section} aria-labelledby="scr05-policy">
        <h2 id="scr05-policy">{text('scr05.policy.current')}</h2>
        <GatesRefresh query={policy} operation={OPERATION} />
        <GatesResult query={policy} empty={null}>
          {policy.data === undefined ? null : (
            <PlantPolicyView policy={policy.data} timeZone={timeZone} />
          )}
        </GatesResult>
      </section>
      {context.can('commissioning.run') && policy.data !== undefined ? (
        <PolicyForm
          key={String(policy.data.version ?? 0)}
          plantId={plantId}
          timeZone={timeZone}
          current={policy.data}
        />
      ) : null}
    </main>
  );
}

export default function Page() {
  const { plant_id } = useParams();
  if (plant_id === undefined) return <ScreenState state="not_available_in_scope" />;
  return <PlantPolicyPage key={plant_id} plantId={plant_id} />;
}
