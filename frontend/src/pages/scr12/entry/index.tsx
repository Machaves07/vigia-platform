/** Inicio del mando de línea: elegir zona y mes, aplicar en bloque, nunca por tecla. */
import { useId, useState } from 'react';
import { useNavigate } from 'react-router';
import { monthlyPort } from '@/ports/scr12Monthly';
import { text } from '@/texts';
import styles from '../monthly.module.css';
import { MonthlyRefresh, MonthlyResult, useMonthlyContext, useMonthlyRead } from '../common';

export default function Page() {
  const context = useMonthlyContext();
  const hierarchy = useMonthlyRead(context, 'GET /hierarchy', {}, (signal) =>
    monthlyPort.hierarchy.read({}, signal),
  );
  const id = useId();
  const [zone, setZone] = useState('');
  const [period, setPeriod] = useState('');
  const navigate = useNavigate();
  return (
    <main className={styles.page}>
      <h1>{text('navigation.scr12')}</h1>
      <p>{text('scr12.intro')}</p>
      <MonthlyRefresh query={hierarchy} />
      <MonthlyResult query={hierarchy}>
        <form
          onSubmit={(event) => {
            event.preventDefault();
            void navigate(
              `/zones/${encodeURIComponent(zone)}/monthly/${encodeURIComponent(period)}`,
            );
          }}
        >
          <label htmlFor={`${id}-zone`}>{text('scr12.zone')}</label>
          <select
            id={`${id}-zone`}
            required
            value={zone}
            onChange={(event) => {
              setZone(event.target.value);
            }}
          >
            <option value="">{text('scr12.choose_zone')}</option>
            {hierarchy.data?.plants.map((plant) => (
              <optgroup label={plant.name} key={plant.plant_id}>
                {plant.zones.map((entry) => (
                  <option key={entry.zone_id} value={entry.zone_id}>
                    {entry.name}
                  </option>
                ))}
              </optgroup>
            ))}
          </select>
          <label htmlFor={`${id}-period`}>{text('scr12.period')}</label>
          <input
            id={`${id}-period`}
            type="month"
            required
            pattern="[0-9]{4}-(0[1-9]|1[0-2])"
            value={period}
            onChange={(event) => {
              setPeriod(event.target.value);
            }}
          />
          <button type="submit">{text('scr12.open')}</button>
        </form>
      </MonthlyResult>
    </main>
  );
}
