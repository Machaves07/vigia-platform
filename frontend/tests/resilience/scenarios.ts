/**
 * Catálogo de los escenarios FS-APP-01 a 10 (PAT-APP-RES-08) y la marca que cada prueba deja en
 * `task.meta`. Sin dependencias de navegador: lo leen el arnés (`harness.tsx`) y el informe
 * (`report.ts`), que corre en el proceso principal de vitest.
 */
export const SCENARIOS = {
  'FS-APP-01': 'La API no responde (tiempo de espera) durante 2 minutos',
  'FS-APP-02': '`temporarily_unavailable` sostenido en escrituras',
  'FS-APP-03': '`rate_limited` con `retry_after_seconds`',
  'FS-APP-04': 'Almacén de evidencias caído (`storage_unavailable`)',
  'FS-APP-05': 'URL prefirmada vencida a mitad de reproducción',
  'FS-APP-06': 'Sesión vencida a mitad de un formulario largo',
  'FS-APP-07': '`privacy_notice_required` sobrevenido (versión nueva del aviso)',
  'FS-APP-08': 'Versión de la API distinta tras un despliegue; porción de código antigua',
  'FS-APP-09': 'Vista en vivo local inalcanzable desde fuera de la planta',
  'FS-APP-10': 'Asistente `unavailable` y `rejected_by_validators`',
} as const;

export type ScenarioId = keyof typeof SCENARIOS;

export const SCENARIO_IDS = Object.keys(SCENARIOS) as ScenarioId[];

/** Lo que cada prueba deja en `task.meta` para el informe. */
export interface ScenarioMeta {
  readonly id: ScenarioId;
  readonly title: string;
  readonly run_seed: number;
  readonly scenario_seed: number;
  readonly parameters: Readonly<Record<string, unknown>>;
}

declare module 'vitest' {
  interface TaskMeta {
    fsApp?: ScenarioMeta;
  }
}
