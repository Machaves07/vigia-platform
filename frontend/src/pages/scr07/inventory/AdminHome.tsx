/**
 * Inicio del administrador (sistema visual v1, «Administrador · Inicio»; VIG-365): «Pendiente de
 * su parte» con un botón por pendiente y las áreas de configuración de su menú, encima de la vista
 * de la flota. Va en la flota solo cuando es el inicio de la sesión (BR-APP-04).
 *
 * - Invitaciones sin aceptar: usuarios `invited` (`GET /users`, con `users.manage`).
 * - Zonas sin acta de alcance: compuerta con el montaje sin aprobar (`GET /zones/{id}/gates`, con
 *   `catalog.read`), hasta 50 zonas como el inicio del coordinador.
 * - Nodos sin comunicación: estado de comunicación distinto de `reachable` en el inventario sin
 *   filtros (el mismo que la flota pide al abrir).
 *
 * Cada pendiente se pide solo con su clave; sin ella no aparece. Las áreas son las entradas del
 * menú agrupado del rol (`menuSections`), así que solo hay las que la sesión puede abrir.
 */
import { useId, useState } from 'react';
import { Link, useNavigate } from 'react-router';
import { buildNavigationMap } from '@/app/navigation';
import { menuSections, navItems, type NavItem } from '@/app/NavMenu';
import { SCREENS } from '@/app/screens';
import { ScreenState } from '@/pages/shared/ScreenState';
import { fleetPort, readFleet, type FleetNode } from '@/ports/scr07Fleet';
import { policyFor, screenKey, useScreenQuery, type ReadOperation } from '@/state/query';
import { useUiSession } from '@/state/session';
import { text } from '@/texts';
import type { useFleetContext } from '../shared';
import styles from './inventory.module.css';

const NODES: ReadOperation = 'GET /fleet/nodes';
const USERS: ReadOperation = 'GET /users';
const GATES: ReadOperation = 'GET /zones/{zone_id}/gates';

/** Zonas cuya compuerta se consulta para «zonas sin acta». */
const PENDING_ZONES = 50;

/** Usuarios leídos para contar las invitaciones sin aceptar. */
const USERS_LIMIT = 200;

/** Grupos del menú del administrador que son áreas de configuración. */
const AREA_SECTIONS: readonly string[] = ['people', 'equipment', 'rules'];

type Context = ReturnType<typeof useFleetContext>;

/** Nodos sin comunicación: los que el servidor no da como alcanzables. */
function silentNodes(nodes: readonly FleetNode[]): readonly FleetNode[] {
  return nodes.filter((node) => node.communication_state !== 'reachable');
}

function Pending({
  id,
  count,
  partial = false,
  to,
  action,
}: {
  readonly id: string;
  readonly count: number;
  readonly partial?: boolean;
  readonly to: string;
  readonly action: string;
}) {
  return (
    <li className={styles.pending} data-pending={id} data-value={count}>
      <div>
        <h3>{text(`scr07.home.pending.${id}`)}</h3>
        <p>
          <strong>{partial ? `${text('scr07.home.more_than')} ${String(count)}` : count}</strong>
        </p>
      </div>
      {count > 0 && (
        <Link to={to} className={styles.pendingAction}>
          {text(action)}
        </Link>
      )}
    </li>
  );
}

/** Área que pide zona o planta: elige y «Abrir» lleva a la elegida (sin consultas en la URL). */
function SelectArea({ item, context }: { readonly item: NavItem; readonly context: Context }) {
  const id = useId();
  const navigate = useNavigate();
  const [choice, setChoice] = useState('');
  const zone = item.select === 'zone_id';
  return (
    <form
      aria-label={text(item.title_text)}
      onSubmit={(event) => {
        event.preventDefault();
        if (choice === '' || item.select === undefined) return;
        void navigate(item.path.replace(`:${item.select}`, encodeURIComponent(choice)));
      }}
    >
      <label htmlFor={`${id}-choice`}>
        {text(zone ? 'role_navigation.zone' : 'role_navigation.plant')}
      </label>
      <select
        id={`${id}-choice`}
        value={choice}
        onChange={(event) => {
          setChoice(event.currentTarget.value);
        }}
      >
        <option value="">{text('role_navigation.choose')}</option>
        {context.plants.map((plant) =>
          zone ? (
            <optgroup key={plant.plant_id} label={plant.name}>
              {plant.zones.map((entry) => (
                <option key={entry.zone_id} value={entry.zone_id}>
                  {entry.name}
                </option>
              ))}
            </optgroup>
          ) : (
            <option key={plant.plant_id} value={plant.plant_id}>
              {plant.name}
            </option>
          ),
        )}
      </select>
      <button type="submit">{text('role_navigation.open')}</button>
    </form>
  );
}

export function AdminHome({ context }: { readonly context: Context }) {
  const { principal } = useUiSession();
  // El inventario sin filtros, con la misma clave que la flota al abrir: una sola lectura.
  const fleet = useScreenQuery({
    key: screenKey({
      screen: 'scr07',
      operation: NODES,
      scope: `${context.scope}:all`,
      concession: context.concession,
      params: {},
    }),
    policy: policyFor(NODES),
    read: ({ signal }) => readFleet({}, signal),
  });
  const nodes = fleet.data;
  const permissions = new Set(principal?.effective_permissions ?? []);
  const canUsers = permissions.has('users.manage');
  const canGates = permissions.has('catalog.read');
  const zones = context.plants.flatMap((plant) => plant.zones).slice(0, PENDING_ZONES);
  const ids = zones.map((zone) => zone.zone_id);
  const users = useScreenQuery({
    key: screenKey({
      screen: 'scr07',
      operation: USERS,
      scope: context.scope,
      concession: context.concession,
      params: { limit: USERS_LIMIT },
    }),
    policy: policyFor(USERS),
    enabled: canUsers,
    read: ({ signal }) => fleetPort.users.read({ query: { limit: USERS_LIMIT } }, signal),
  });
  const gates = useScreenQuery({
    key: screenKey({
      screen: 'scr07',
      operation: GATES,
      scope: context.scope,
      concession: context.concession,
      params: { zones: ids },
    }),
    policy: policyFor(GATES),
    enabled: canGates && ids.length > 0,
    read: ({ signal }) =>
      Promise.all(ids.map((zone_id) => fleetPort.gates.read({ path: { zone_id } }, signal))),
  });
  const invited = users.data?.users.filter((user) => user.status === 'invited') ?? [];
  const withoutCharter = (gates.data ?? []).filter((gate) => gate.mounting.status !== 'approved');
  const silent = nodes === undefined ? undefined : silentNodes(nodes);
  const navigation = principal === null ? null : buildNavigationMap(principal, SCREENS);
  const home = navigation?.home ?? null;
  // Las áreas de configuración: los grupos de tarea del menú (personas, equipos y reglas), sin el
  // expediente ni «Más», y sin el propio inicio.
  const areas = menuSections(navigation === null ? [] : navItems(navigation, SCREENS), home)
    .filter((section) => AREA_SECTIONS.includes(section.id))
    .flatMap((section) => section.items)
    .filter(
      (item) => !(home !== null && item.screen_id === home.screen_id && item.group === home.group),
    );
  const loading =
    (canUsers && users.status === 'pending') ||
    (canGates && ids.length > 0 && gates.status === 'pending') ||
    fleet.status === 'pending';
  return (
    <>
      <section aria-label={text('scr07.home.pending')} className={styles.block}>
        <h2>{text('scr07.home.pending')}</h2>
        {loading ? (
          <ScreenState state="loading" />
        ) : (
          <ul className={styles.pendingList}>
            {canUsers && users.error === null && (
              <Pending
                id="invitations"
                count={invited.length}
                partial={users.data?.next_after != null}
                to="/admin/users"
                action="scr07.home.pending.invitations_action"
              />
            )}
            {canGates && gates.error === null && (
              <Pending
                id="charters"
                count={withoutCharter.length}
                to={
                  withoutCharter[0] === undefined
                    ? '/fleet'
                    : `/zones/${encodeURIComponent(withoutCharter[0].zone_id)}/gates`
                }
                action="scr07.home.pending.charters_action"
              />
            )}
            {silent !== undefined && (
              <Pending
                id="silent_nodes"
                count={silent.length}
                to="/fleet/alarms"
                action="scr07.home.pending.silent_nodes_action"
              />
            )}
          </ul>
        )}
        {(users.error !== null || gates.error !== null || fleet.error !== null) && (
          <p role="status">{text('scr07.home.pending_unavailable')}</p>
        )}
      </section>
      {areas.length > 0 && (
        <section aria-label={text('scr07.home.areas')} className={styles.block}>
          <h2>{text('scr07.home.areas')}</h2>
          <ul className={styles.areas}>
            {areas.map((item) => (
              <li key={`${item.screen_id}:${item.group}`} className={styles.area}>
                {item.select === undefined ? (
                  <Link to={item.path}>{text(item.title_text)}</Link>
                ) : (
                  <>
                    <h3>{text(item.title_text)}</h3>
                    <SelectArea item={item} context={context} />
                  </>
                )}
              </li>
            ))}
          </ul>
        </section>
      )}
    </>
  );
}
