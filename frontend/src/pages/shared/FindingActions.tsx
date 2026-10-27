/**
 * Recorrido diario guiado por el estado del hallazgo (VIG-359; IF-07 a IF-09): el acceso que se
 * ofrece en el detalle (SCR-08), en cada fila de la bandeja y antes de clasificar (SCR-09) sale
 * del `finding_lifecycle_state` que da el servidor y de la tabla de transiciones de U-04, nunca de
 * una regla propia de la interfaz (BR-APP-43): `FINDING_ACCESS` se contrasta por igualdad con la
 * sección `lifecycle` de `backend/openapi/u05-fixtures.json`. Son enlaces de navegación; ninguna
 * escritura se oculta ni se deshabilita: si una transición no existe, el servidor lo dice al
 * registrar.
 *
 * Cada enlace aparece además solo si la sesión puede abrir el grupo de rutas de destino
 * (`canEnterGroup`, BR-APP-01): un enlace a una ruta que la sesión no abre no es un acceso.
 */
import { Link } from 'react-router';
import { canEnterGroup } from '@/app/navigation';
import type { ScreenDescriptor } from '@/app/descriptor';
import { descriptor as scr09 } from '@/pages/scr09/descriptor';
import { descriptor as scr10 } from '@/pages/scr10/descriptor';
import { text } from '@/texts';

/** Los accesos del recorrido: cola (T05/T06), clasificar (T03/T04), reclasificar (T15 a T20). */
export type FindingAccess = 'review_queue' | 'classify' | 'reclassify' | 'loop';

/**
 * Lo que la tabla de transiciones admite desde cada estado (§4.1 de U-04): `in_review` solo sale
 * por la cola; `received`, por la primera clasificación; los demás tienen lazo y toda clasificación
 * es una reclasificación con motivo reforzado.
 */
export const FINDING_ACCESS: Readonly<Record<string, readonly FindingAccess[]>> = {
  received: ['classify'],
  in_review: ['review_queue'],
  open: ['loop', 'reclassify'],
  action_assigned: ['loop', 'reclassify'],
  overdue: ['loop', 'reclassify'],
  suspended_by_reclassification: ['loop', 'reclassify'],
  closed: ['loop', 'reclassify'],
  unclosable: ['loop', 'reclassify'],
  discarded: ['loop', 'reclassify'],
};

/** Estados de lazo abierto: reclasificar como no confirmado los suspende (T15, BR-LAZ-24). */
export const SUSPENDING_STATES: readonly string[] = ['open', 'action_assigned', 'overdue'];

/** Estado en que reclasificar como confirmado reanuda el lazo (T16). */
export const RESUMING_STATE = 'suspended_by_reclassification';

/** El acceso que admite el estado; ninguno si el estado no está en la lista cerrada. */
export function findingAccess(state: string): readonly FindingAccess[] {
  return Object.hasOwn(FINDING_ACCESS, state) ? (FINDING_ACCESS[state] ?? []) : [];
}

/**
 * La detección que la cola debe señalar viaja en el estado de la navegación, nunca en la URL: la
 * aplicación no pone consultas ni datos en la dirección (`urlProblem` de la política de e2e).
 */
interface ReviewQueueTarget {
  readonly detection: string;
}

/** La detección señalada en el estado de la navegación, si la hay y tiene la forma esperada. */
export function reviewQueueTarget(state: unknown): string | null {
  if (typeof state !== 'object' || state === null || !('detection' in state)) return null;
  const { detection } = state;
  return typeof detection === 'string' && detection !== '' ? detection : null;
}

const TARGETS: Readonly<
  Record<FindingAccess, { readonly screen: ScreenDescriptor; readonly group: string }>
> = {
  review_queue: { screen: scr10, group: 'queue' },
  classify: { screen: scr09, group: 'classify' },
  reclassify: { screen: scr09, group: 'classify' },
  loop: { screen: scr09, group: 'loop' },
};

function canOpen(access: FindingAccess, permissions: ReadonlySet<string>): boolean {
  const target = TARGETS[access];
  const group = target.screen.route_groups.find((candidate) => candidate.id === target.group);
  return group !== undefined && canEnterGroup(group, permissions);
}

function path(access: FindingAccess, findingId: string): string {
  switch (access) {
    case 'review_queue':
      return '/review-queue';
    case 'classify':
    case 'reclassify':
      return `/findings/${findingId}/classify`;
    case 'loop':
      return `/findings/${findingId}/loop`;
  }
}

/**
 * Los enlaces del estado. `detectionId` (el `subject_id` de una detección) viaja en el estado de la
 * navegación para que la cola la señale; la bandeja no lo tiene y enlaza a la cola entera.
 * `landmark` envuelve los enlaces en una región de navegación (el detalle); en las filas de la
 * bandeja van como lista, sin repetir regiones con el mismo nombre.
 */
export function FindingActions({
  findingId,
  state,
  detectionId = null,
  permissions,
  landmark = false,
}: {
  readonly findingId: string;
  readonly state: string;
  readonly detectionId?: string | null;
  readonly permissions: ReadonlySet<string>;
  readonly landmark?: boolean;
}) {
  const offered = findingAccess(state).filter((access) => canOpen(access, permissions));
  if (offered.length === 0) return null;
  const suspends = offered.includes('reclassify') && SUSPENDING_STATES.includes(state);
  const list = (
    <ul data-part="finding_actions">
      {offered.map((access) => (
        <li key={access}>
          <Link
            to={path(access, findingId)}
            {...(access === 'review_queue' && detectionId !== null
              ? { state: { detection: detectionId } satisfies ReviewQueueTarget }
              : {})}
            data-access={access}
          >
            {text(`finding_journey.${access}`)}
          </Link>
          {access === 'reclassify' && suspends && (
            <span role="note" data-part="suspends">
              {' '}
              {text('finding_journey.suspends')}
            </span>
          )}
        </li>
      ))}
    </ul>
  );
  return landmark ? (
    <nav aria-label={text('finding_journey.navigation')}>
      <h2>{text('finding_journey.navigation')}</h2>
      {list}
    </nav>
  ) : (
    list
  );
}
