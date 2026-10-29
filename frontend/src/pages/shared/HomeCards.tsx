/**
 * Tarjetas de acceso de un inicio por rol (sistema visual v1; VIG-365): las entradas del menú de la
 * sesión que el inicio destaca (`slots`, como en `MENU_PROFILES`: `pantalla:grupo` o
 * `pantalla:*`), en ese orden, cada una con su frase. Solo aparecen las que la sesión puede abrir:
 * salen de `navItems`, que ya filtra por permisos (BR-APP-01). Las que piden zona o planta no son
 * tarjetas: se eligen en el menú.
 */
import { Link } from 'react-router';
import type { ScreenDescriptor } from '@/app/descriptor';
import { buildNavigationMap, homeScreen } from '@/app/navigation';
import { navItems, type NavItem } from '@/app/NavMenu';
import { SCREENS } from '@/app/screens';
import { useUiSession } from '@/state/session';
import { text } from '@/texts';
import styles from './HomeCards.module.css';

export interface HomeCard {
  readonly slot: string;
  /** Frase de la tarjeta (clave del catálogo). */
  readonly hint: string;
}

/**
 * `true` si el grupo `group` de `screen` es el inicio de la sesión (BR-APP-04). Se calcula entre
 * todas las pantallas: con varios roles manda el primero de `HOME_ROLE_ORDER`.
 */
export function useIsHome(screen: ScreenDescriptor, group: string): boolean {
  const { principal } = useUiSession();
  if (principal === null) return false;
  const home = homeScreen(principal.assignments, new Set(principal.effective_permissions), SCREENS);
  return home?.screen_id === screen.screen_id && home.group === group;
}

function fills(slot: string, item: NavItem): boolean {
  return slot === `${item.screen_id}:${item.group}` || slot === `${item.screen_id}:*`;
}

/** Las tarjetas que la sesión puede abrir, con su entrada del menú. */
function useHomeCards(
  cards: readonly HomeCard[],
): readonly { readonly card: HomeCard; readonly item: NavItem }[] {
  const { principal } = useUiSession();
  if (principal === null) return [];
  const items = navItems(buildNavigationMap(principal, SCREENS), SCREENS).filter(
    (item) => item.select === undefined,
  );
  return cards.flatMap((card) => {
    const item = items.find((candidate) => fills(card.slot, candidate));
    return item === undefined ? [] : [{ card, item }];
  });
}

export function HomeCards({
  title,
  cards,
}: {
  /** Encabezado de la sección (clave del catálogo). */
  readonly title: string;
  readonly cards: readonly HomeCard[];
}) {
  const shown = useHomeCards(cards);
  if (shown.length === 0) return null;
  return (
    <section aria-label={text(title)} className={styles.section}>
      <h2>{text(title)}</h2>
      <ul className={styles.cards}>
        {shown.map(({ card, item }) => (
          <li key={card.slot} className={styles.card} data-slot={card.slot}>
            <Link to={item.path}>{text(item.title_text)}</Link>
            <p>{text(card.hint)}</p>
          </li>
        ))}
      </ul>
    </section>
  );
}
