/**
 * Mediciones del instalador del cierre del acta (D-2; interfaces U-03 v1.5, precisión b): lo que
 * el instalador lee de `LocalStatus` del nodo y teclea. La pantalla aplica la validación
 * estructural de rango antes de enviar (BR-APP-42): `beacon_latency_ms_p95` es un entero de
 * milisegundos entre 0 y 10 000, ambos incluidos; fuera de ese rango la petición no sale.
 *
 * El contrato fijo (`CloseBody`) exige `installer_measurements.beacon_latency_ms_p95`; solo las
 * líneas base son opcionales. La pantalla sigue al contrato, que manda sobre la nota D-2.
 */
import {
  BEACON_LATENCY_MAX_MS,
  BEACON_LATENCY_MIN_MS,
  type BaselineBody,
  type CloseBody,
} from '@/ports/scr06Commissioning';

/** Una línea base tal como se escribe: la zona es siempre la de la sesión. */
export interface BaselineDraft {
  readonly camera_id: string;
  readonly captured_at: string;
}

export type BeaconLatency =
  { readonly ok: true; readonly value: number } | { readonly ok: false; readonly key: string };

const INTEGER = /^-?\d+$/;

/** Lee `beacon_latency_ms_p95`: entero de milisegundos dentro de `[0, 10 000]`. */
export function parseBeaconLatency(raw: string): BeaconLatency {
  const value = raw.trim();
  if (value === '') return { ok: false, key: 'validation.required' };
  if (!INTEGER.test(value)) return { ok: false, key: 'validation.type' };
  const parsed = Number(value);
  if (
    !Number.isSafeInteger(parsed) ||
    parsed < BEACON_LATENCY_MIN_MS ||
    parsed > BEACON_LATENCY_MAX_MS
  ) {
    return { ok: false, key: 'validation.out_of_range' };
  }
  return { ok: true, value: parsed };
}

/** Las mediciones del cuerpo: sin líneas base, el campo opcional no viaja. */
export function installerMeasurements(
  beaconLatencyMs: number,
  baselines: readonly BaselineDraft[],
  zoneId: string,
): CloseBody['installer_measurements'] {
  const rows: BaselineBody[] = baselines.map((baseline) => ({
    camera_id: baseline.camera_id.trim(),
    zone_id: zoneId,
    captured_at: baseline.captured_at.trim(),
  }));
  return {
    beacon_latency_ms_p95: beaconLatencyMs,
    ...(rows.length === 0 ? {} : { baselines: rows }),
  };
}
