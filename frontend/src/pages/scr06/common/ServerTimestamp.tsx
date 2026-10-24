/**
 * Marca de tiempo del servidor (IF-13, BR-APP-40): muestra el instante ISO 8601 UTC con
 * milisegundos y `Z` tal como lo dio el servidor, sin reinterpretarlo en otra zona horaria. La
 * zona de la planta no llega en RV-09; presentar la hora del navegador como si fuera la de la
 * planta sería una afirmación falsa.
 */
import styles from './common.module.css';

export function ServerTimestamp({ instant }: { readonly instant: string }) {
  return (
    <time className={styles.instant} dateTime={instant} data-part="server_instant">
      {instant}
    </time>
  );
}
