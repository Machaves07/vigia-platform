/**
 * Responsable de la acción (TASK-583; O-3 del recorrido SST; A-126; BR-APP-21, 33 y 57): la lista
 * de las personas que `assign` acepta (usuario activo con rol `coordinator_sst`, `line_manager` o
 * `plant_manager` sobre la zona), con sus roles etiquetados y la persona en sesión primero como
 * sugerencia. La búsqueda por nombre es **local** sobre la lista ya cargada: no pide nada por tecla
 * y la API no tiene búsqueda por persona. Solo aparece con muchas personas (pocos campos).
 *
 * Mientras la lista llega, o si no se puede leer, se ofrece a la persona en sesión, que siempre
 * puede ser responsable de lo que asigna; el servidor decide en `assign`.
 */
import { useEffect, useId, useState } from 'react';
import { readAssignees, type Assignee } from '@/ports/scr09Closure';
import { policyFor, screenKey, useScreenQuery } from '@/state/query';
import { text } from '@/texts';
import { label } from '@/texts/labels';

/** Desde cuántas personas se ofrece la búsqueda: con menos, la lista basta. */
const SEARCH_FROM = 6;

const collator = new Intl.Collator('es-CO', { sensitivity: 'base' });

/** El nombre sin tildes ni mayúsculas, para buscar «elena» y encontrar «Elena». */
function folded(value: string): string {
  return value.normalize('NFD').replace(/\p{M}/gu, '').toLocaleLowerCase('es-CO').trim();
}

/** Por nombre, con la persona en sesión primero (la sugerencia, regla 1 del recorrido). */
function ordered(people: readonly Assignee[], sessionId: string | undefined): Assignee[] {
  const sorted = [...people].sort(
    (a, b) => collator.compare(a.display_name, b.display_name) || (a.user_id < b.user_id ? -1 : 1),
  );
  const own = sorted.filter((person) => person.user_id === sessionId);
  return [...own, ...sorted.filter((person) => person.user_id !== sessionId)];
}

export function AssigneePicker({
  context,
  sessionUser,
  caption,
  value,
  onChange,
  invalid = false,
}: {
  readonly context: {
    readonly findingId: string;
    readonly scope: string;
    readonly concession: string | null;
    readonly permissions: readonly string[];
  };
  readonly sessionUser: { readonly id: string; readonly name: string } | null;
  readonly caption: string;
  readonly value: string;
  readonly onChange: (userId: string) => void;
  readonly invalid?: boolean;
}) {
  const id = useId();
  const [search, setSearch] = useState('');
  const query = useScreenQuery({
    key: screenKey({
      screen: 'scr09',
      operation: 'GET /loop/findings/{finding_state_id}/assignees',
      scope: context.scope,
      concession: context.concession,
      params: { finding_state_id: context.findingId },
    }),
    policy: policyFor('GET /loop/findings/{finding_state_id}/assignees'),
    enabled: context.permissions.includes('actions.manage') && context.findingId !== '',
    read: ({ signal }) => readAssignees(context.findingId, signal),
  });
  const loaded = query.data;
  const people: readonly Assignee[] =
    loaded !== undefined
      ? ordered(loaded, sessionUser?.id)
      : sessionUser === null
        ? []
        : [{ user_id: sessionUser.id, display_name: sessionUser.name, roles: [] }];
  // Con la lista cargada, el valor siempre es alguien de la lista (o ninguno si está vacía).
  const fallback = people[0]?.user_id ?? '';
  const known = people.some((person) => person.user_id === value);
  useEffect(() => {
    if (loaded !== undefined && !known && value !== fallback) onChange(fallback);
  }, [loaded, known, value, fallback, onChange]);
  const needle = folded(search);
  const matches =
    needle === ''
      ? people
      : people.filter((person) => folded(person.display_name).includes(needle));
  const shown = matches.some((person) => person.user_id === value)
    ? matches
    : [...people.filter((person) => person.user_id === value), ...matches];
  const captionOf = (person: Assignee) => {
    const own = person.user_id === sessionUser?.id ? ` (${text('scr09.assignee.you')})` : '';
    const roles = person.roles.map((role) => label('role', role)).join(', ');
    return `${person.display_name}${own}${roles === '' ? '' : ` · ${roles}`}`;
  };
  return (
    <>
      {people.length >= SEARCH_FROM && (
        <label>
          {text('scr09.assignee.search')}
          <input
            type="search"
            name="assignee_search"
            value={search}
            aria-describedby={needle !== '' && matches.length === 0 ? `${id}-none` : undefined}
            onChange={(event) => {
              const next = event.currentTarget.value;
              setSearch(next);
              const wanted = folded(next);
              const found = people.filter((person) => folded(person.display_name).includes(wanted));
              const first = found[0];
              if (first !== undefined && !found.some((person) => person.user_id === value))
                onChange(first.user_id);
            }}
          />
        </label>
      )}
      {needle !== '' && matches.length === 0 && (
        <p id={`${id}-none`} role="status">
          {text('scr09.assignee.no_match')}
        </p>
      )}
      <label>
        {caption}
        <select
          name="assignee_user_id"
          value={value}
          aria-invalid={invalid && value === ''}
          onChange={(event) => {
            onChange(event.currentTarget.value);
          }}
        >
          {shown.length === 0 && <option value="">{text('scr09.assignee.empty')}</option>}
          {shown.map((person) => (
            <option key={person.user_id} value={person.user_id}>
              {captionOf(person)}
            </option>
          ))}
        </select>
      </label>
      {query.error !== null && <p>{text('scr09.assignee.unavailable')}</p>}
    </>
  );
}
