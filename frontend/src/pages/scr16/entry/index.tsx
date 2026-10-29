/**
 * SCR-16, grupo `entry`: `/transparency`, inicio del COPASST. Elige una zona del alcance de la
 * sesión (`GET /hierarchy`) y lleva a su vista de transparencia; no muestra ningún dato de gestión.
 * Como inicio de la sesión (VIG-365) se llama «Inicio», lleva la nota de privacidad (P3) y el acceso
 * a sus avisos; desde el menú de otro rol es «Transparencia».
 */
import { Link } from 'react-router';
import { HomeCards, useIsHome } from '@/pages/shared/HomeCards';
import { text } from '@/texts';
import { descriptor } from '../descriptor';
import { TransparencyRefresh, TransparencyResult, useTransparencyContext } from '../shared';
import styles from '../scr16.module.css';

export default function Page() {
  const { hierarchy, plants } = useTransparencyContext();
  const withZones = plants.filter((plant) => plant.zones.length > 0);
  const isHome = useIsHome(descriptor, 'entry');
  return (
    <main className={styles.page}>
      <h1>{text(isHome ? 'scr16.home.title' : 'navigation.scr16')}</h1>
      <p>{text('scr16.entry.intro')}</p>
      {isHome && (
        <p role="note" data-part="privacy">
          {text('scr16.home.privacy')}
        </p>
      )}
      <TransparencyRefresh query={hierarchy} operation="GET /hierarchy" />
      <TransparencyResult
        query={hierarchy}
        empty={withZones.length === 0 ? text('scr16.entry.empty') : null}
      >
        <nav aria-label={text('scr16.entry.zones')}>
          {withZones.map((plant) => (
            <section key={plant.plant_id} className={styles.section}>
              <h2>{plant.name}</h2>
              <ul className={styles.zones}>
                {plant.zones.map((zone) => (
                  <li key={zone.zone_id}>
                    <Link className={styles.zoneLink} to={`/transparency/${zone.zone_id}`}>
                      {zone.name}
                    </Link>
                  </li>
                ))}
              </ul>
            </section>
          ))}
        </nav>
      </TransparencyResult>
      {isHome && (
        <HomeCards
          title="scr16.home.cards"
          cards={[{ slot: 'SCR-15:list', hint: 'scr16.home.card.notifications' }]}
        />
      )}
    </main>
  );
}
