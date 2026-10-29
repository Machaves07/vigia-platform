/**
 * Inicio del mando de línea: elegir zona y mes, aplicar en bloque, nunca por tecla. Como inicio de
 * la sesión (VIG-365) se llama «Inicio», dice para qué sirve y ofrece la transparencia de sus zonas;
 * desde el menú de otro rol es «Paquete mensual».
 *
 * Valores por defecto (VIG-366): el mes es el anterior al actual en la hora de la planta (el
 * paquete es de un mes cerrado) y, si el alcance tiene una sola zona, esa zona; entonces, desde el
 * menú, el paquete se abre solo, sin pantalla vacía. Volver desde el paquete («Elegir otro mes o
 * zona») no lo vuelve a abrir.
 */
import { formatInTimeZone } from 'date-fns-tz';
import { useEffect, useId, useState } from 'react';
import { useLocation, useNavigate } from 'react-router';
import { MonthInput } from '@/components/DateFields/DateFields';
import { recentMonths } from '@/components/DateFields/parse';
import { useFormCheck } from '@/components/DateFields/useFormCheck';
import { HomeCards, useIsHome } from '@/pages/shared/HomeCards';
import { monthlyClock, monthlyPort } from '@/ports/scr12Monthly';
import { text } from '@/texts';
import { descriptor } from '../descriptor';
import styles from '../monthly.module.css';
import { MonthlyRefresh, MonthlyResult, useMonthlyContext, useMonthlyRead } from '../common';

function packagePath(zone: string, period: string): string {
  return `/zones/${encodeURIComponent(zone)}/monthly/${encodeURIComponent(period)}`;
}

export default function Page() {
  const context = useMonthlyContext();
  const hierarchy = useMonthlyRead(context, 'GET /hierarchy', {}, (signal) =>
    monthlyPort.hierarchy.read({}, signal),
  );
  const id = useId();
  const check = useFormCheck();
  const navigate = useNavigate();
  const location = useLocation();
  const isHome = useIsHome(descriptor, 'entry');
  const plants = hierarchy.data?.plants ?? [];
  const zones = plants.flatMap((plant) => plant.zones);
  const only = zones.length === 1 ? zones[0] : undefined;
  const timeZone = plants.length === 1 ? (plants[0]?.timezone ?? 'UTC') : 'UTC';
  const current = formatInTimeZone(monthlyClock().now(), timeZone, 'yyyy-MM');
  const previous = recentMonths(current, 2)[1] ?? current;
  const [zone, setZone] = useState<string | null>(null);
  const [period, setPeriod] = useState<string | null>(null);
  const shownZone = zone ?? only?.zone_id ?? '';
  const shownPeriod = period ?? previous;
  const choosing =
    typeof location.state === 'object' && location.state !== null && 'choose' in location.state;
  // Como inicio de la sesión no se abre solo: el inicio propio del mando (VIG-365) se conserva y
  // la zona y el mes ya vienen elegidos.
  useEffect(() => {
    if (only !== undefined && !choosing && !isHome) {
      void navigate(packagePath(only.zone_id, previous), { replace: true });
    }
  }, [choosing, isHome, navigate, only, previous]);
  return (
    <main className={styles.page}>
      <h1>{text(isHome ? 'scr12.home.title' : 'navigation.scr12')}</h1>
      {isHome && <p>{text('scr12.home.lead')}</p>}
      <p>{text('scr12.intro')}</p>
      <MonthlyRefresh query={hierarchy} />
      <MonthlyResult query={hierarchy}>
        <form
          noValidate
          onSubmit={(event) => {
            event.preventDefault();
            if (!check.run(event.currentTarget)) return;
            void navigate(packagePath(shownZone, shownPeriod));
          }}
        >
          <label htmlFor={`${id}-zone`}>{text('scr12.zone')}</label>
          <select
            id={`${id}-zone`}
            name="zone"
            required
            value={shownZone}
            {...check.field('zone')}
            onChange={(event) => {
              setZone(event.target.value);
              check.clear('zone');
            }}
          >
            <option value="">{text('scr12.choose_zone')}</option>
            {plants.map((plant) => (
              <optgroup label={plant.name} key={plant.plant_id}>
                {plant.zones.map((entry) => (
                  <option key={entry.zone_id} value={entry.zone_id}>
                    {entry.name}
                  </option>
                ))}
              </optgroup>
            ))}
          </select>
          {check.error('zone')}
          <label htmlFor={`${id}-period`}>{text('scr12.period')}</label>
          <MonthInput
            id={`${id}-period`}
            name="period"
            required
            latest={current}
            value={shownPeriod}
            {...check.field('period')}
            onChange={(value) => {
              setPeriod(value);
              check.clear('period');
            }}
          />
          {check.error('period')}
          <button type="submit">{text('scr12.open')}</button>
        </form>
      </MonthlyResult>
      {isHome && (
        <HomeCards
          title="scr12.home.cards"
          cards={[{ slot: 'SCR-16:*', hint: 'scr12.home.card.transparency' }]}
        />
      )}
    </main>
  );
}
