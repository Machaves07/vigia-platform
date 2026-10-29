/**
 * Duraciones para personas (sistema visual v1, «Personas, no sistemas»; VIG-366): el servidor da
 * milisegundos enteros y la pantalla los muestra en segundos, minutos u horas, nunca en «ms».
 * Por debajo de un minuto, segundos con un decimal como máximo («4,2 s»); después, unidades
 * enteras sin las que valen cero («1 min 5 s», «2 h 3 min»).
 */
const seconds = new Intl.NumberFormat('es-CO', { maximumFractionDigits: 1 });

/** `null` si el valor no es un número finito y no negativo. */
export function formatDuration(ms: number): string | null {
  if (!Number.isFinite(ms) || ms < 0) return null;
  const tenths = Math.round(ms / 100) / 10;
  if (tenths < 60) return `${seconds.format(tenths)} s`;
  const total = Math.round(ms / 1000);
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const rest = total % 60;
  if (hours > 0)
    return minutes === 0 ? `${String(hours)} h` : `${String(hours)} h ${String(minutes)} min`;
  return rest === 0 ? `${String(minutes)} min` : `${String(minutes)} min ${String(rest)} s`;
}
