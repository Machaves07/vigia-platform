/**
 * Panel «Siguiente paso» (sistema visual v1; S3-13 y S3-24 de VIG-365): en el detalle y en el
 * lazo, una sola acción principal con la frase de por qué (plazo y qué falta) y, debajo, «Más
 * acciones». Las acciones salen del estado que da el servidor (`nextStep`, BR-APP-43) y cada una
 * aparece solo si la sesión puede abrir su destino (`canEnterGroup`, BR-APP-01).
 *
 * - En el lazo, «Comprometer una acción» no navega: abre el formulario de la misma pantalla
 *   (`onAction`). Desde el detalle lleva al lazo con el formulario ya abierto.
 * - Reclasificar conserva su aviso: en lazo abierto, que lo suspende (BR-LAZ-24).
 * - En un incerrable, la revisión lleva su fecha comprometida y su medida compensatoria.
 */
import { useId } from 'react';
import { Link } from 'react-router';
import type { ScreenDescriptor } from '@/app/descriptor';
import { canEnterGroup } from '@/app/navigation';
import { TimestampWithOffset } from '@/components/TimestampWithOffset/TimestampWithOffset';
import { descriptor as scr09 } from '@/pages/scr09/descriptor';
import { descriptor as scr10 } from '@/pages/scr10/descriptor';
import { text } from '@/texts';
import { SUSPENDING_STATES } from './FindingActions';
import { isKnownState, nextStep, type StepAccess } from './nextStep';
import styles from './NextStepPanel.module.css';

/** La revisión comprometida de un incerrable: fecha vigente y medida compensatoria declarada. */
export interface ReviewCommitment {
  readonly reviewDueAt: string;
  readonly compensatoryMeasure: string;
}

interface Target {
  readonly screen: ScreenDescriptor;
  readonly group: string;
  /** Sección cuya clave pide además el registro (la acción vive en la pantalla del lazo). */
  readonly section?: string;
}

const TARGETS: Readonly<Record<StepAccess, Target>> = {
  classify: { screen: scr09, group: 'classify' },
  reclassify: { screen: scr09, group: 'classify' },
  review_queue: { screen: scr10, group: 'queue' },
  action: { screen: scr09, group: 'loop', section: 'action' },
  closure: { screen: scr09, group: 'loop/closure' },
  unclosable: { screen: scr09, group: 'loop/unclosable' },
  unclosable_review: { screen: scr09, group: 'loop/unclosable' },
  reopening: { screen: scr09, group: 'loop/reopening' },
};

/** `true` si la sesión puede abrir el destino del acceso (y su sección, si la pide). */
export function canTake(access: StepAccess, permissions: ReadonlySet<string>): boolean {
  const target = TARGETS[access];
  const group = target.screen.route_groups.find((candidate) => candidate.id === target.group);
  if (group === undefined || !canEnterGroup(group, permissions)) return false;
  if (target.section === undefined) return true;
  const section = target.screen.sections.find((candidate) => candidate.id === target.section);
  return section?.required_permissions.some((key) => permissions.has(key)) ?? false;
}

/** Dirección del acceso. */
function stepPath(access: StepAccess, findingId: string): string {
  switch (access) {
    case 'review_queue':
      return '/review-queue';
    case 'classify':
    case 'reclassify':
      return `/findings/${findingId}/classify`;
    case 'action':
      return `/findings/${findingId}/loop`;
    case 'closure':
      return `/findings/${findingId}/loop/closure`;
    case 'unclosable':
    case 'unclosable_review':
      return `/findings/${findingId}/loop/unclosable`;
    case 'reopening':
      return `/findings/${findingId}/loop/reopening`;
  }
}

/**
 * Lo que viaja en el estado de la navegación, nunca en la URL: la detección que la cola señala,
 * la pestaña de revisión del incerrable, el formulario de la acción y la reclasificación abiertos.
 */
function navigationState(access: StepAccess, detectionId: string | null) {
  switch (access) {
    case 'review_queue':
      return detectionId === null ? undefined : { detection: detectionId };
    case 'unclosable_review':
      return { review: true };
    case 'action':
      return { action: true };
    case 'reclassify':
      return { reclassify: true };
    default:
      return undefined;
  }
}

/** `true` si la navegación pidió abrir `key` (`action` o `reclassify`) al llegar. */
export function openRequested(state: unknown, key: 'action' | 'reclassify'): boolean {
  if (typeof state !== 'object' || state === null || !(key in state)) return false;
  return (state as Readonly<Record<string, unknown>>)[key] === true;
}

/** Los accesos que el panel ofrece para `state` con `permissions`. */
function offeredSteps(
  state: string,
  permissions: ReadonlySet<string>,
): { readonly primary: StepAccess | null; readonly secondary: readonly StepAccess[] } {
  const step = nextStep(state);
  return {
    primary: step.primary !== null && canTake(step.primary, permissions) ? step.primary : null,
    secondary: step.secondary.filter((access) => canTake(access, permissions)),
  };
}

function Why({
  state,
  dueAt,
  commitment,
  timeZone,
}: {
  readonly state: string;
  readonly dueAt: string | null;
  readonly commitment: ReviewCommitment | null;
  readonly timeZone: string;
}) {
  return (
    <>
      <p data-part="why">
        {text(isKnownState(state) ? `next_step.why.${state}` : 'next_step.why.unknown')}
      </p>
      {(state === 'action_assigned' || state === 'overdue') && dueAt !== null && (
        <p data-part="due">
          {text('next_step.due_at')}: <TimestampWithOffset instant={dueAt} timeZone={timeZone} />
        </p>
      )}
      {state === 'unclosable' &&
        (commitment === null ? (
          <p data-part="commitment_unknown">{text('role_navigation.loop.commitment_unknown')}</p>
        ) : (
          <dl data-part="unclosable_commitment">
            <dt>{text('role_navigation.loop.review_due_at')}</dt>
            <dd>
              <TimestampWithOffset instant={commitment.reviewDueAt} timeZone={timeZone} />
            </dd>
            <dt>{text('scr09.unclosable.compensatory_measure')}</dt>
            <dd>{commitment.compensatoryMeasure}</dd>
          </dl>
        ))}
    </>
  );
}

function StepLink({
  access,
  findingId,
  detectionId,
  primary,
}: {
  readonly access: StepAccess;
  readonly findingId: string;
  readonly detectionId: string | null;
  readonly primary: boolean;
}) {
  const state = navigationState(access, detectionId);
  return (
    <Link
      to={stepPath(access, findingId)}
      {...(state === undefined ? {} : { state })}
      className={primary ? styles.primary : styles.secondary}
      data-access={access}
      {...(primary ? { 'data-primary': 'true' } : {})}
    >
      {text(`next_step.access.${access}`)}
    </Link>
  );
}

/**
 * El siguiente paso de una fila (inicio y bandeja): solo la acción principal del estado, si la
 * sesión puede abrirla; nada en un estado sin acción principal o fuera de la lista cerrada.
 */
export function NextStepLink({
  findingId,
  state,
  permissions,
}: {
  readonly findingId: string;
  readonly state: string;
  readonly permissions: ReadonlySet<string>;
}) {
  const { primary } = offeredSteps(state, permissions);
  return primary === null ? null : (
    <StepLink access={primary} findingId={findingId} detectionId={null} primary={false} />
  );
}

export function NextStepPanel({
  findingId,
  state,
  permissions,
  dueAt = null,
  detectionId = null,
  commitment = null,
  timeZone,
  onAction,
}: {
  readonly findingId: string;
  /** `finding_lifecycle_state` que da el servidor. */
  readonly state: string;
  readonly permissions: ReadonlySet<string>;
  /** Plazo de la acción vigente (`due_at` del hallazgo). */
  readonly dueAt?: string | null;
  /** La detección que la cola debe señalar (`subject_id` de una detección). */
  readonly detectionId?: string | null;
  /** La revisión comprometida del incerrable; `null` si no se conoce. */
  readonly commitment?: ReviewCommitment | null;
  readonly timeZone: string;
  /** En el lazo, abre el formulario de la acción en la misma pantalla. */
  readonly onAction?: () => void;
}) {
  const id = useId();
  const { primary, secondary } = offeredSteps(state, permissions);
  const step = nextStep(state);
  // Con pasos que la sesión no puede abrir, nada que ofrecer: el panel no aparece.
  if (primary === null && secondary.length === 0 && step.primary !== null) return null;
  const suspends = SUSPENDING_STATES.includes(state);
  return (
    <section className={styles.panel} aria-labelledby={`${id}-title`} data-part="next_step">
      <h2 id={`${id}-title`} className={styles.title}>
        {text('next_step.title')}
      </h2>
      <Why state={state} dueAt={dueAt} commitment={commitment} timeZone={timeZone} />
      {primary === null ? (
        isKnownState(state) && (
          <p data-part="nothing_pending">{text('next_step.nothing_pending')}</p>
        )
      ) : primary === 'action' && onAction !== undefined ? (
        <button
          type="button"
          className={styles.primary}
          data-access="action"
          data-primary="true"
          onClick={onAction}
        >
          {text('next_step.access.action')}
        </button>
      ) : (
        <StepLink access={primary} findingId={findingId} detectionId={detectionId} primary />
      )}
      {secondary.length > 0 && (
        <>
          <h3 className={styles.more}>{text('next_step.more')}</h3>
          <ul className={styles.list}>
            {secondary.map((access) => (
              <li key={access}>
                <StepLink
                  access={access}
                  findingId={findingId}
                  detectionId={detectionId}
                  primary={false}
                />
                {access === 'reclassify' && suspends && (
                  <p role="note" data-part="suspends" className={styles.note}>
                    {text('finding_journey.suspends')}
                  </p>
                )}
              </li>
            ))}
          </ul>
        </>
      )}
    </section>
  );
}
