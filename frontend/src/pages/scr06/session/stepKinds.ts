/**
 * Los ocho tipos de paso de `StepKind` (`app.yaml`), en el orden de la lista cerrada de U-03. La
 * etiqueta de cada uno sale de `labels.platform.es.json` (`label('step_kind', …)`): la pantalla no
 * escribe valores crudos.
 */
import type { StepKind } from '@/ports/scr06Commissioning';

export const STEP_KINDS: readonly StepKind[] = [
  'physical_setup',
  'signal_mapping',
  'framing',
  'walk_test_passes',
  'occlusion_test',
  'latency_measurement',
  'review_and_signatures',
  'other',
];
