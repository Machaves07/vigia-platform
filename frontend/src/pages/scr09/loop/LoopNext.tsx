/**
 * «Siguientes registros del lazo» (VIG-363): solo los registros que admite el estado que da el
 * servidor (`loopAccess`) y cuyas rutas la sesión puede abrir (`canEnterGroup`, BR-APP-01). El
 * registro que el estado pide primero va destacado con lo que lo explica: en un incerrable, la
 * revisión con su fecha comprometida y su medida compensatoria. Comprometer la acción no es un
 * enlace: su formulario está en la misma pantalla.
 */
import { Link } from 'react-router';
import { canEnterGroup } from '@/app/navigation';
import { TimestampWithOffset } from '@/components/TimestampWithOffset/TimestampWithOffset';
import { text } from '@/texts';
import { descriptor } from '../descriptor';
import { loopAccess, loopPrimary, type LoopAccess, type UnclosableCommitment } from './loopAccess';
import styles from './loop.module.css';

type LoopLink = Exclude<LoopAccess, 'action'>;

/** La revisión abre la pestaña de revisión de la pantalla del incerrable (estado de navegación). */
export interface UnclosableTarget {
  readonly review: true;
}

const TARGETS: Readonly<
  Record<LoopLink, { readonly group: string; readonly key: string; readonly review?: true }>
> = {
  closure: { group: 'loop/closure', key: 'scr09.loop.closure' },
  unclosable: { group: 'loop/unclosable', key: 'scr09.loop.unclosable' },
  unclosable_review: {
    group: 'loop/unclosable',
    key: 'role_navigation.loop.unclosable_review',
    review: true,
  },
  reopening: { group: 'loop/reopening', key: 'scr09.loop.reopening' },
};

function canOpen(access: LoopLink, permissions: ReadonlySet<string>): boolean {
  const group = descriptor.route_groups.find((candidate) => candidate.id === TARGETS[access].group);
  return group !== undefined && canEnterGroup(group, permissions);
}

/** Los enlaces que la pantalla ofrece para `state` con `permissions`, en el orden de la tabla. */
export function loopLinks(state: string, permissions: ReadonlySet<string>): readonly LoopLink[] {
  return loopAccess(state).flatMap((access) =>
    access !== 'action' && canOpen(access, permissions) ? [access] : [],
  );
}

export function LoopNext({
  findingId,
  state,
  permissions,
  commitment,
  timeZone,
}: {
  readonly findingId: string;
  readonly state: string;
  readonly permissions: ReadonlySet<string>;
  /** La revisión comprometida del incerrable; `null` si no se conoce (aún no leída o sin ella). */
  readonly commitment: UnclosableCommitment | null;
  readonly timeZone: string;
}) {
  const links = loopLinks(state, permissions);
  const primary = loopPrimary(state);
  // Con registros que la sesión no puede abrir no hay nada que ofrecer; sin ninguno, se explica.
  if (links.length === 0 && loopAccess(state).length > 0) return null;
  return (
    <nav aria-label={text('scr09.loop.next')}>
      <h2>{text('scr09.loop.next')}</h2>
      {links.length === 0 ? (
        <p data-part="loop_next_none">{text('role_navigation.loop.none')}</p>
      ) : (
        <ul>
          {links.map((access) => {
            const target = TARGETS[access];
            const highlighted = access === primary;
            return (
              <li
                key={access}
                data-access={access}
                {...(highlighted
                  ? { 'data-primary': 'true', className: styles.primary }
                  : { className: styles.secondary })}
              >
                <Link
                  to={`/findings/${findingId}/${target.group}`}
                  {...(target.review === true
                    ? { state: { review: true } satisfies UnclosableTarget }
                    : {})}
                >
                  {text(target.key)}
                </Link>
                {highlighted && access === 'unclosable_review' && (
                  <div data-part="unclosable_commitment">
                    <p>{text('role_navigation.loop.unclosable_review_hint')}</p>
                    {commitment === null ? (
                      <p>{text('role_navigation.loop.commitment_unknown')}</p>
                    ) : (
                      <dl>
                        <dt>{text('role_navigation.loop.review_due_at')}</dt>
                        <dd>
                          <TimestampWithOffset
                            instant={commitment.reviewDueAt}
                            timeZone={timeZone}
                          />
                        </dd>
                        <dt>{text('scr09.unclosable.compensatory_measure')}</dt>
                        <dd>{commitment.compensatoryMeasure}</dd>
                      </dl>
                    )}
                  </div>
                )}
              </li>
            );
          })}
        </ul>
      )}
    </nav>
  );
}
