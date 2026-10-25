/**
 * Política de firmantes de la planta (IF-11 paso 4): roles exigidos y mínimo →
 * `PUT /plants/{plant_id}/signatory-policy`. La pantalla no impone el mínimo de tres ni la
 * representación de los trabajadores: `catalog_fewer_than_three` y
 * `catalog_workers_representation_missing` llegan del servidor y se anclan al formulario.
 */
import { useId, useState } from 'react';
import {
  gatesPort,
  SIGNATORY_ROLES,
  type Role,
  type SignatoryPolicy,
  type SignatoryPolicyBody,
} from '@/ports/scr05Gates';
import { useScreenMutation } from '@/state/query';
import { text } from '@/texts';
import { label } from '@/texts/labels';
import { WriteFailure } from '../shared';
import styles from '../scr05.module.css';

export function SignatoryPolicyForm({
  plantId,
  policy,
}: {
  readonly plantId: string;
  readonly policy: SignatoryPolicy | undefined;
}) {
  const id = useId();
  const [roles, setRoles] = useState<ReadonlySet<Role>>(
    () => new Set(policy?.required_roles ?? []),
  );
  const [minimum, setMinimum] = useState(String(policy?.minimum ?? 3));
  const [showErrors, setShowErrors] = useState(false);
  const mutation = useScreenMutation({
    write: (body: SignatoryPolicyBody) =>
      gatesPort.putSignatoryPolicy.write({ path: { plant_id: plantId }, body }),
    invalidates: { screen: 'scr05', resources: ['signatories'] },
  });
  const parsed = Number(minimum);
  const minimumInvalid = minimum.trim() === '' || !Number.isInteger(parsed) || parsed < 0;
  return (
    <section className={styles.panel} aria-labelledby={`${id}-title`} data-part="signatory_form">
      <h3 id={`${id}-title`}>{text('scr05.signatories.edit_title')}</h3>
      <form
        noValidate
        className={styles.section}
        onSubmit={(event) => {
          event.preventDefault();
          setShowErrors(true);
          if (minimumInvalid) return;
          void mutation
            .run({
              required_roles: SIGNATORY_ROLES.filter((role) => roles.has(role)),
              minimum: parsed,
            })
            .catch(() => undefined);
        }}
      >
        <fieldset className={styles.field}>
          <legend>{text('scr05.signatories.required_roles')}</legend>
          {SIGNATORY_ROLES.map((role) => (
            <label key={role} className={styles.inline}>
              <input
                type="checkbox"
                checked={roles.has(role)}
                onChange={(event) => {
                  const checked = event.target.checked;
                  setRoles((current) => {
                    const updated = new Set(current);
                    if (checked) updated.add(role);
                    else updated.delete(role);
                    return updated;
                  });
                }}
              />
              {label('role', role)}
            </label>
          ))}
        </fieldset>
        <div className={styles.field}>
          <label htmlFor={`${id}-minimum`}>{text('scr05.signatories.minimum')}</label>
          <input
            id={`${id}-minimum`}
            type="number"
            min={0}
            step={1}
            value={minimum}
            required
            aria-required="true"
            aria-invalid={showErrors && minimumInvalid}
            aria-describedby={showErrors && minimumInvalid ? `${id}-minimum-error` : undefined}
            onChange={(event) => {
              setMinimum(event.target.value);
            }}
          />
          {showErrors && minimumInvalid ? (
            <p
              id={`${id}-minimum-error`}
              data-part="local_error"
              data-text-key="scr05.signatories.minimum_invalid"
            >
              {text('scr05.signatories.minimum_invalid')}
            </p>
          ) : null}
        </div>
        <div className={styles.actions}>
          <button type="submit" className={styles.button} disabled={mutation.status === 'pending'}>
            {text('scr05.signatories.save')}
          </button>
        </div>
      </form>
      {mutation.status === 'success' ? (
        <p role="status" data-part="signatories_saved">
          {text('scr05.signatories.saved')}
        </p>
      ) : null}
      <WriteFailure error={mutation.error} field="signatory_policy" />
    </section>
  );
}
