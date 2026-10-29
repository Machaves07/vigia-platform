/**
 * Escribir en los campos de fecha propios (VIG-366): la fecha se escribe `dd/mm/aaaa` y la hora
 * `hh:mm` en un segundo campo. Las pruebas siguen describiendo los valores en ISO (`aaaa-mm-dd`,
 * `aaaa-mm-ddThh:mm`), como los entregan los campos.
 */
import { fireEvent } from '@testing-library/react';

/** `aaaa-mm-dd` → `dd/mm/aaaa`; cualquier otro texto se escribe tal cual. */
export function esDate(iso: string): string {
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(iso);
  return match === null ? iso : `${match[3] ?? ''}/${match[2] ?? ''}/${match[1] ?? ''}`;
}

/** Escribe la fecha `iso` en el campo de fecha. */
export function changeDate(input: HTMLElement, iso: string): void {
  fireEvent.change(input, { target: { value: esDate(iso) } });
}

/** Escribe `aaaa-mm-ddThh:mm` en el campo de fecha y en su campo de hora. */
export function changeDateTime(input: HTMLElement, local: string): void {
  const [date = '', time = ''] = local.split('T');
  fireEvent.change(input, { target: { value: esDate(date) } });
  const group = input.closest('[data-part="date_time_input"]');
  const timeInput = group?.querySelectorAll('input[type="text"]')[1];
  if (!(timeInput instanceof HTMLElement)) throw new Error('Falta el campo de hora.');
  fireEvent.change(timeInput, { target: { value: time.slice(0, 5) } });
}
