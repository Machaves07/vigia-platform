/**
 * Acuerdo de uso (IF-11 paso 5): alta con firmantes `{role, user_id}` (usuarios con el rol que la
 * planta configuró), documento escaneado opcional y `replaces_agreement_id` cuando sustituye al
 * vigente; y la aprobación, que deja la compuerta de uso aprobada si el servidor lo admite. Tras
 * aprobar se invalidan las compuertas de esta pantalla, del panel de inicio y de SCR-06/16.
 *
 * Los rechazos (`catalog_signatory_role_not_in_policy`, `catalog_signatory_user_role_mismatch`,
 * `catalog_workers_representation_missing`, `catalog_fewer_than_three`,
 * `catalog_agreement_reused_from_other_zone`; al aprobar `catalog_signatures_incomplete`,
 * `catalog_commissioning_record_missing`, `catalog_mounting_gate_pending` y
 * `catalog_plant_policy_missing`, que solo aparece aquí) llegan del servidor y se anclan.
 */
import { useId, useState } from 'react';
import { DirectUploadField } from '@/components/DirectUploadField/DirectUploadField';
import {
  gatesPort,
  SIGNATORY_ROLES,
  type Agreement,
  type Approval,
  type Role,
  type SignatoryPolicy,
  type UseAgreementBody,
} from '@/ports/scr05Gates';
import { useScreenMutation } from '@/state/query';
import { text } from '@/texts';
import { label } from '@/texts/labels';
import { WriteFailure } from '../shared';
import styles from '../scr05.module.css';

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

interface Row {
  readonly key: number;
  readonly role: Role;
  readonly userId: string;
}

/** Roles que se ofrecen: los de la política de la planta o, sin ella, la lista cerrada. */
function offeredRoles(policy: SignatoryPolicy | undefined): readonly Role[] {
  const required = policy?.configured === true ? (policy.required_roles ?? []) : [];
  return required.length > 0 ? required : SIGNATORY_ROLES;
}

export function AgreementForm({
  zoneId,
  plantId,
  signatoryPolicy,
  currentAgreementId,
  onCreated,
}: {
  readonly zoneId: string;
  readonly plantId: string;
  readonly signatoryPolicy: SignatoryPolicy | undefined;
  /** Acuerdo vigente de la compuerta de uso, si lo hay (`usage.agreement_id`). */
  readonly currentAgreementId: string | null;
  readonly onCreated: (agreement: Agreement) => void;
}) {
  const id = useId();
  const roles = offeredRoles(signatoryPolicy);
  const [rows, setRows] = useState<readonly Row[]>(() =>
    roles.map((role, index) => ({ key: index, role, userId: '' })),
  );
  const [next, setNext] = useState(roles.length);
  const [document, setDocument] = useState<Record<string, unknown> | null>(null);
  const [replaces, setReplaces] = useState(false);
  const [showErrors, setShowErrors] = useState(false);
  const mutation = useScreenMutation({
    write: (body: UseAgreementBody) =>
      gatesPort.createAgreement.write({ path: { zone_id: zoneId }, body }),
    invalidates: { screen: 'scr05', resources: ['agreements', 'transparency'] },
  });
  const invalidRow = (row: Row) => !UUID.test(row.userId.trim());
  const update = (key: number, change: Partial<Omit<Row, 'key'>>) => {
    setRows((current) => current.map((row) => (row.key === key ? { ...row, ...change } : row)));
  };
  return (
    <section className={styles.panel} aria-labelledby={`${id}-title`} data-part="agreement_form">
      <h3 id={`${id}-title`}>{text('scr05.agreement.create_title')}</h3>
      <p>{text('scr05.agreement.create_intro')}</p>
      <form
        noValidate
        className={styles.section}
        onSubmit={(event) => {
          event.preventDefault();
          setShowErrors(true);
          if (rows.length === 0 || rows.some(invalidRow)) return;
          const body: UseAgreementBody = {
            signatories: rows.map((row) => ({ role: row.role, user_id: row.userId.trim() })),
            document_ref: document,
            replaces_agreement_id: replaces ? currentAgreementId : null,
          };
          void mutation.run(body).then(onCreated, () => undefined);
        }}
      >
        <ul className={styles.list} aria-label={text('scr05.agreement.signatories')}>
          {rows.map((row, index) => {
            const invalid = showErrors && invalidRow(row);
            const position = String(index + 1);
            return (
              <li key={row.key} data-part="signatory_row">
                <div className={styles.field}>
                  <label htmlFor={`${id}-role-${String(row.key)}`}>
                    {text('scr05.agreement.role')} {position}
                  </label>
                  <select
                    id={`${id}-role-${String(row.key)}`}
                    value={row.role}
                    onChange={(event) => {
                      update(row.key, { role: event.target.value as Role });
                    }}
                  >
                    {SIGNATORY_ROLES.map((role) => (
                      <option key={role} value={role}>
                        {label('role', role)}
                      </option>
                    ))}
                  </select>
                </div>
                <div className={styles.field}>
                  <label htmlFor={`${id}-user-${String(row.key)}`}>
                    {text('scr05.agreement.user_id')} {position}
                  </label>
                  <input
                    id={`${id}-user-${String(row.key)}`}
                    value={row.userId}
                    required
                    aria-required="true"
                    aria-invalid={invalid}
                    aria-describedby={invalid ? `${id}-user-error-${String(row.key)}` : undefined}
                    autoComplete="off"
                    spellCheck={false}
                    onChange={(event) => {
                      update(row.key, { userId: event.target.value });
                    }}
                  />
                  {invalid ? (
                    <p
                      id={`${id}-user-error-${String(row.key)}`}
                      data-part="local_error"
                      data-text-key="scr05.agreement.user_id_invalid"
                    >
                      {text('scr05.agreement.user_id_invalid')}
                    </p>
                  ) : null}
                </div>
                <button
                  type="button"
                  className={styles.button}
                  onClick={() => {
                    setRows((current) => current.filter((entry) => entry.key !== row.key));
                  }}
                >
                  {text('scr05.agreement.remove_signatory')} {position}
                </button>
              </li>
            );
          })}
        </ul>
        <div className={styles.actions}>
          <button
            type="button"
            className={styles.button}
            onClick={() => {
              setRows((current) => [
                ...current,
                { key: next, role: roles[0] ?? 'copasst', userId: '' },
              ]);
              setNext((value) => value + 1);
            }}
          >
            {text('scr05.agreement.add_signatory')}
          </button>
        </div>
        <DirectUploadField
          label={text('scr05.agreement.document_upload')}
          target={{ unit: 'catalog_document', plantId, kind: 'use_agreement' }}
          onUploaded={(result) => {
            if (result.unit === 'catalog_document') setDocument({ ...result.document_ref });
          }}
        />
        {currentAgreementId === null ? null : (
          <label className={styles.inline}>
            <input
              type="checkbox"
              checked={replaces}
              onChange={(event) => {
                setReplaces(event.target.checked);
              }}
            />
            {text('scr05.agreement.replaces')}
          </label>
        )}
        <div className={styles.actions}>
          <button type="submit" className={styles.button} disabled={mutation.status === 'pending'}>
            {text('scr05.agreement.create')}
          </button>
        </div>
      </form>
      <WriteFailure error={mutation.error} field="agreement" />
    </section>
  );
}

export function ApprovalSection({
  agreementId,
  onApproved,
}: {
  readonly agreementId: string;
  readonly onApproved: (approval: Approval) => void;
}) {
  const id = useId();
  const mutation = useScreenMutation({
    write: (agreement_id: string) => gatesPort.approve.write({ path: { agreement_id } }),
    // `gates` llega también al panel de inicio, a SCR-06 y a SCR-16 (grafo de invalidación).
    invalidates: { screen: 'scr05', resources: ['gates', 'agreements', 'transparency'] },
  });
  return (
    <section className={styles.panel} aria-labelledby={`${id}-title`} data-part="approval">
      <h3 id={`${id}-title`}>{text('scr05.approval.title')}</h3>
      <p>{text('scr05.approval.intro')}</p>
      <div className={styles.actions}>
        <button
          type="button"
          className={styles.button}
          disabled={mutation.status === 'pending'}
          onClick={() => {
            void mutation.run(agreementId).then(onApproved, () => undefined);
          }}
        >
          {text('scr05.approval.action')}
        </button>
      </div>
      {mutation.status === 'success' ? (
        <p role="status" data-part="approved">
          {text('scr05.approval.done')}
        </p>
      ) : null}
      <WriteFailure error={mutation.error} field="approval" />
    </section>
  );
}
