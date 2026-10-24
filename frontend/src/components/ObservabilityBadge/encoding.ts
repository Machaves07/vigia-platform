/**
 * `semaphore_encoding` fija (BR-APP-23, 35; domain-entities §1): icono y texto obligatorios, el
 * color solo refuerza y es siempre azul, ámbar o gris, nunca verde ni rojo. Un valor fuera de
 * `observability_state` se muestra como «Desconocido» con su propio icono y un tono neutro.
 *
 * Las causas llevan la etiqueta neutral de su unidad dueña (BR-APP-29): `degradation_cause` de
 * U-01 (`@/contracts`) o `platform_cause` de U-02 (`labels.platform.es.json`); una causa sin
 * etiqueta se muestra como «causa desconocida», nunca con su valor crudo.
 */
import platform from '../../../../backend/resources/labels.platform.es.json';
import { labels } from '@/contracts';

export type EyeIconKind = 'eye_open' | 'eye_half' | 'eye_closed' | 'eye_unknown';

/** Tono de refuerzo; el módulo CSS lo traduce a las variables del semáforo. */
type SemaphoreTone = 'blue' | 'amber' | 'grey' | 'neutral';

export interface SemaphoreEncoding {
  readonly icon: EyeIconKind;
  readonly textKey: string;
  readonly tone: SemaphoreTone;
}

const ENCODING: Readonly<Record<string, SemaphoreEncoding>> = Object.freeze({
  observable: {
    icon: 'eye_open',
    textKey: 'components_status.observability.observable',
    tone: 'blue',
  },
  degraded: {
    icon: 'eye_half',
    textKey: 'components_status.observability.degraded',
    tone: 'amber',
  },
  not_observable: {
    icon: 'eye_closed',
    textKey: 'components_status.observability.not_observable',
    tone: 'grey',
  },
});

const UNKNOWN: SemaphoreEncoding = Object.freeze({
  icon: 'eye_unknown',
  textKey: 'components_status.observability.unknown',
  tone: 'neutral',
});

export function isKnownState(state: string): boolean {
  return Object.hasOwn(ENCODING, state);
}

export function encodingFor(state: string): SemaphoreEncoding {
  return isKnownState(state) ? (ENCODING[state] ?? UNKNOWN) : UNKNOWN;
}

const CAUSE_LABELS: Readonly<Record<string, string>> = Object.freeze({
  ...labels.degradation_cause,
  ...platform.platform_cause,
});

/** Etiqueta neutral de la causa, o `undefined` si no está en ninguna lista cerrada. */
export function causeLabel(cause: string): string | undefined {
  return Object.hasOwn(CAUSE_LABELS, cause) ? CAUSE_LABELS[cause] : undefined;
}
