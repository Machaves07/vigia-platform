/**
 * Regresiones de PR-APP-14 (PBT-10): contraejemplos reducidos que encontró la propiedad del
 * controlador de UI-22 mientras se construía (VIG-314). Los dos destaparon defectos del oráculo,
 * no del producto, y quedan como ejemplos fijos de sus dos bordes:
 *
 * - seed 1401919768, path 19:0:3:5:8:11:13:21:16:15:17:16:16:15:16:21:13:20:13:16:15:13:15:13:13:
 *   21:13:16:13:13:18:13:13:13:24:14:13:13:13:15:13:13:27:13:13 (perfil ci): una consulta que
 *   tarda más que el tope; al cancelarla no debe quedar ningún temporizador vivo.
 * - seed 852732576 (perfil nightly): `ready` llega dentro de la misma ventana del reloj que el
 *   tope de 2 min; el controlador lo procesa al cierre de la ventana, ya pasado el tope, y el
 *   panel queda «no disponible» por tope, sin consultar más.
 */
import fc from 'fast-check';
import { describe, it } from 'vitest';

import {
  assistantScenario,
  checkAssistantScenario,
  type AssistantScenario,
} from '../support/assistantScenario';

const examples: [AssistantScenario][] = [
  [
    {
      accept: { kind: 'pending', latency: 0 },
      responses: [
        { kind: 'pending', latency: 3000 },
        { kind: 'pending', latency: 0 },
        { kind: 'pending', latency: 0 },
        { kind: 'pending', latency: 0 },
        { kind: 'pending', latency: 0 },
        { kind: 'pending', latency: 132522 },
      ],
      ticks: [3482, 8997, 8, 13],
    },
  ],
  [
    {
      accept: { kind: 'pending', latency: 0 },
      responses: [
        { kind: 'pending', latency: 2631 },
        { kind: 'pending', latency: 19571 },
        { kind: 'pending', latency: 0 },
        { kind: 'pending', latency: 28386 },
        { kind: 'pending', latency: 2631 },
        { kind: 'ready', latency: 0 },
      ],
      ticks: [6383, 3981, 3649, 8815, 313, 8447, 7878, 113, 7819],
    },
  ],
];

describe('PR-APP-14 · regresiones', () => {
  it('los contraejemplos reducidos siguen cumpliendo la propiedad', async () => {
    await fc.assert(
      fc.asyncProperty(assistantScenario, async (input) => {
        await checkAssistantScenario(input);
      }),
      { examples, numRuns: examples.length },
    );
  });
});
