/**
 * Fechas y horas escritas por la persona (VIG-366): `dd/mm/aaaa` y `hh:mm` de 24 horas, sin
 * depender del idioma del navegador. Los valores internos son los de los campos nativos que
 * reemplazan (`aaaa-mm-dd`, `hh:mm`, `aaaa-mm`), así que las pantallas no cambian su lógica.
 */

const DATE_TEXT = /^(\d{1,2})[/.-](\d{1,2})[/.-](\d{4})$/;
const ISO_DATE = /^(\d{4})-(\d{2})-(\d{2})$/;
const TIME_TEXT = /^(\d{1,2}):(\d{2})$/;
const ISO_MONTH = /^(\d{4})-(\d{2})$/;

const pad = (value: number) => String(value).padStart(2, '0');

function validDay(year: number, month: number, day: number): boolean {
  if (year < 1900 || year > 9999 || month < 1 || month > 12 || day < 1) return false;
  const date = new Date(Date.UTC(year, month - 1, day));
  return date.getUTCMonth() === month - 1 && date.getUTCDate() === day;
}

/** `dd/mm/aaaa` → `aaaa-mm-dd`; `null` si no es una fecha real. */
export function parseDateText(value: string): string | null {
  const match = DATE_TEXT.exec(value.trim());
  if (match === null) return null;
  const [day, month, year] = [Number(match[1]), Number(match[2]), Number(match[3])];
  return validDay(year, month, day) ? `${String(year)}-${pad(month)}-${pad(day)}` : null;
}

/** `aaaa-mm-dd` → `dd/mm/aaaa`; cadena vacía si no es una fecha válida. */
export function dateText(iso: string): string {
  const match = ISO_DATE.exec(iso);
  if (match === null) return '';
  const [year, month, day] = [Number(match[1]), Number(match[2]), Number(match[3])];
  return validDay(year, month, day) ? `${pad(day)}/${pad(month)}/${String(year)}` : '';
}

/** `h:mm` o `hh:mm` de 24 horas → `hh:mm`; `null` si no es una hora válida. */
export function parseTimeText(value: string): string | null {
  const match = TIME_TEXT.exec(value.trim());
  if (match === null) return null;
  const [hours, minutes] = [Number(match[1]), Number(match[2])];
  return hours <= 23 && minutes <= 59 ? `${pad(hours)}:${pad(minutes)}` : null;
}

/** `aaaa-mm` válido (mes 01 a 12). */
export function isMonth(value: string): boolean {
  const match = ISO_MONTH.exec(value);
  if (match === null) return false;
  const month = Number(match[2]);
  return month >= 1 && month <= 12;
}

/** Los `count` meses que terminan en `latest` (`aaaa-mm`), del más reciente al más antiguo. */
export function recentMonths(latest: string, count: number): readonly string[] {
  if (!isMonth(latest)) return [];
  const [year, month] = latest.split('-').map(Number) as [number, number];
  const months: string[] = [];
  for (let index = 0; index < count; index += 1) {
    const total = year * 12 + (month - 1) - index;
    months.push(`${String(Math.floor(total / 12))}-${pad((total % 12) + 1)}`);
  }
  return months;
}

const monthName = new Intl.DateTimeFormat('es-CO', {
  month: 'long',
  year: 'numeric',
  timeZone: 'UTC',
});

/** `aaaa-mm` → «Octubre de 2026»; cadena vacía si no es un mes válido. */
export function monthText(value: string): string {
  if (!isMonth(value)) return '';
  const [year, month] = value.split('-').map(Number) as [number, number];
  const name = monthName.format(new Date(Date.UTC(year, month - 1, 1)));
  return name.charAt(0).toLocaleUpperCase('es-CO') + name.slice(1);
}
