/**
 * UI-03 `ScopeSelector` (BR-APP-05 y 06): selector de planta de la cabecera con «Todas las de mi
 * alcance» por omisión. Persiste entre pantallas y sesiones a través de `state.scope`
 * (`UiPreferences`); la zona no se elige aquí, sino en cada pantalla. Si la planta guardada ya no
 * está en el alcance, muestra «todas» sin aviso ni error. Emite `plantChanged` con
 * `store.selectPlant`.
 */
import { useId } from 'react';

import { useScope, type ScopeStore } from '@/state/scope';
import { text } from '@/texts';

import styles from './ScopeSelector.module.css';

export interface ScopeSelectorProps {
  readonly store: ScopeStore;
}

const ALL = 'all';

export function ScopeSelector({ store }: ScopeSelectorProps) {
  const id = useId();
  const { plantId, availablePlants } = useScope(store);
  const plants = availablePlants ?? [];
  return (
    <div className={styles.selector}>
      <label htmlFor={id} className={styles.label}>
        {text('components_scope.selector.label')}
      </label>
      <select
        id={id}
        className={styles.select}
        value={plantId}
        disabled={availablePlants === null}
        onChange={(event) => {
          store.selectPlant(event.target.value);
        }}
      >
        <option value={ALL}>{text('components_scope.selector.all')}</option>
        {plants.map((plant) => (
          <option key={plant.plant_id} value={plant.plant_id}>
            {plant.name}
          </option>
        ))}
      </select>
    </div>
  );
}
