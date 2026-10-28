/**
 * UI-04 `ConcessionBanner` (BR-APP-67 a 69): aviso permanente «Operando en <cliente> bajo
 * concesión hasta <fecha>; cada consulta queda registrada», con el alcance concedido, el motivo
 * cuando se conoce y el único botón para cambiar de concesión (`changeConcession`). Sin
 * concesión activa no pinta nada. No se cierra ni se oculta mientras la concesión siga activa.
 *
 * `concessionEndedKey` da el texto del aviso con que el armazón vuelve al panel del proveedor.
 */
import { TimestampWithOffset } from '@/components/TimestampWithOffset/TimestampWithOffset';
import { useConcession, type ConcessionEnd, type ConcessionStore } from '@/state/concession';
import { hasText, text } from '@/texts';

import styles from './ConcessionBanner.module.css';

export interface ConcessionBannerProps {
  readonly store: ConcessionStore;
  /** Zona IANA en que se muestra el vencimiento (la de la planta concedida, si se conoce). */
  readonly timeZone?: string;
}

/** Clave del texto con que se avisa del final de una concesión. */
export function concessionEndedKey(reason: ConcessionEnd): string {
  return `components_concession.ended.${reason}`;
}

export function ConcessionBanner({ store, timeZone = 'UTC' }: ConcessionBannerProps) {
  const { active } = useConcession(store);
  if (active === null) return null;
  const client =
    active.client_organization_name ?? text('components_concession.banner.client_pending');
  // Un alcance fuera de la lista se muestra «desconocido» (NFR-APP-30), nunca como uno válido.
  const scopeKey = `components_concession.scope.${active.scope_level}`;
  const scope = `${hasText(scopeKey) ? text(scopeKey) : text('common.unknown')}${
    active.scope_level !== 'organization' && active.scope_name !== null
      ? `: ${active.scope_name}`
      : ''
  }`;
  return (
    <section
      className={styles.banner}
      aria-label={text('components_concession.banner.label')}
      data-part="concession_banner"
    >
      <p className={styles.statement} data-part="statement">
        {text('components_concession.banner.operating_in')}{' '}
        <strong data-part="client">{client}</strong>{' '}
        {text('components_concession.banner.under_until')}{' '}
        <TimestampWithOffset instant={active.expires_at} timeZone={timeZone} />;{' '}
        {text('components_concession.banner.audited')}
      </p>
      <dl className={styles.details}>
        <div>
          <dt>{text('components_concession.banner.scope')}</dt>
          <dd data-part="scope">{scope}</dd>
        </div>
        {active.reason !== null && (
          <div>
            <dt>{text('components_concession.banner.reason')}</dt>
            <dd data-part="reason">{active.reason}</dd>
          </div>
        )}
      </dl>
      <button
        type="button"
        className={styles.change}
        onClick={() => {
          store.change();
        }}
      >
        {text('components_concession.banner.change')}
      </button>
    </section>
  );
}
