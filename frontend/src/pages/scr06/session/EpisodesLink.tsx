/**
 * Enlace «Episodios de la sombra» de la cabecera de la consola (TASK-574, A-112): solo si la
 * sesión puede abrir el grupo `episodes` (`commissioning.run`, `canEnterGroup`; BR-APP-01). Sin
 * sesión cargada (pruebas de la consola sin armazón) no se ofrece: un enlace a una ruta que la
 * sesión no abre no es un acceso.
 */
import { Link } from 'react-router';

import { canEnterGroup } from '@/app/navigation';
import { descriptor } from '@/pages/scr06/descriptor';
import { episodesPath } from '@/pages/scr06/episodes/paths';
import { useOptionalUiSession } from '@/state/session';
import { text } from '@/texts';

import styles from './session.module.css';

const EPISODES = descriptor.route_groups.find((group) => group.id === 'episodes');

/** `true` si con estos permisos efectivos se puede abrir el grupo `episodes`. */
export function canOpenEpisodes(permissions: readonly string[] | null): boolean {
  if (permissions === null || EPISODES === undefined) return false;
  return canEnterGroup(EPISODES, new Set(permissions));
}

export function EpisodesLink({ zoneId }: { readonly zoneId: string }) {
  const permissions = useOptionalUiSession()?.principal?.effective_permissions ?? null;
  if (!canOpenEpisodes(permissions)) return null;
  return (
    <p className={styles.links}>
      <Link to={episodesPath(zoneId)} data-part="episodes_link">
        {text('scr06_episodes.link')}
      </Link>
    </p>
  );
}
