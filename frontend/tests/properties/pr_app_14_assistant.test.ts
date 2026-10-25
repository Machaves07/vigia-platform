/**
 * PR-APP-14 (BR-APP-49): para toda secuencia de estados de la solicitud al asistente y toda
 * secuencia de tics del reloj, la interfaz deja de consultar en `ready`, `unavailable` o
 * `rejected_by_validators` (o ante un fallo), nunca consulta pasados 2 minutos desde que el
 * servidor aceptó la solicitud ni antes de 5 s desde la consulta anterior, y en todos los casos
 * el flujo guiado sigue disponible.
 *
 * Dos propiedades:
 * 1. El controlador de UI-22 con respuestas guionizadas de latencia arbitraria sobre `FakeClock`
 *    (`tests/support/assistantScenario.ts`), con todos los ejemplos del perfil.
 * 2. La pantalla real (`Workspace` de SCR-11) con el puerto sobre MSW: tras cualquier secuencia la
 *    plantilla guiada sigue en el árbol accesible y editable. Pintar la pantalla es caro: corre una
 *    décima parte de los ejemplos del perfil (mínimo 10), como PR-APP-30.
 */
import { cleanup, fireEvent, screen, waitFor } from '@testing-library/react';
import fc from 'fast-check';
import { HttpResponse } from 'msw';
import { createElement } from 'react';
import { describe, expect, it } from 'vitest';

import { Workspace } from '@/pages/scr11/root_cause/Workspace';
import { text } from '@/texts';

import {
  advance,
  analysis,
  apiError,
  assistantRequest,
  fakeLoop,
  providers,
  readyRequest,
  renderAt,
  WAIT,
} from '../components/scr11/support';
import {
  assistantScenario,
  checkAssistantScenario,
  type ScenarioOutcome,
} from '../support/assistantScenario';
import { PROPERTY_TIMEOUT_MS } from '../support/timeouts';

const RUNS = Math.max(10, Math.ceil((fc.readConfigureGlobal().numRuns ?? 100) / 10));

describe('PR-APP-14 · consulta del asistente', () => {
  it(
    'se detiene en un estado final o a los 2 min, cada 5 s como mínimo, y nunca queda en curso',
    async () => {
      const outcomes = new Set<ScenarioOutcome>();
      await fc.assert(
        fc.asyncProperty(assistantScenario, async (input) => {
          outcomes.add(await checkAssistantScenario(input));
        }),
      );
      // Las tres ramas del oráculo se recorren de verdad.
      expect([...outcomes].sort()).toEqual(['expired', 'not_accepted', 'terminal']);
    },
    PROPERTY_TIMEOUT_MS,
  );

  it(
    'la plantilla guiada sigue en el árbol accesible y editable con cualquier estado del asistente',
    async () => {
      const step = fc.constantFrom(
        'pending',
        'ready',
        'unavailable',
        'rejected_by_validators',
        'output_rejected',
        'not_found',
      );
      await fc.assert(
        fc.asyncProperty(
          fc.record({
            accept: fc.constantFrom('accepted', 'unavailable', 'rate_limited'),
            steps: fc.array(step, { maxLength: 30 }),
          }),
          async ({ accept, steps }) => {
            const env = await providers();
            const loop = fakeLoop();
            loop.install(env.clock);
            if (accept === 'unavailable')
              loop.state.assistantResponse = () =>
                apiError('temporarily_unavailable', 503, 'loop_assistant_unavailable');
            if (accept === 'rate_limited')
              loop.state.assistantResponse = () => apiError('rate_limited', 429);
            loop.state.statusSteps = steps.map((name) => () => {
              switch (name) {
                case 'ready':
                  return HttpResponse.json(readyRequest());
                case 'unavailable':
                case 'rejected_by_validators':
                  return HttpResponse.json(assistantRequest({ assistant_state: name }));
                case 'output_rejected':
                  return apiError('invalid_request', 400, 'loop_assistant_output_rejected');
                case 'not_found':
                  return apiError('not_found', 404);
                default:
                  return HttpResponse.json(assistantRequest());
              }
            });
            renderAt(createElement(Workspace, { initial: analysis() }), env.Providers);
            fireEvent.click(
              screen.getByRole('button', { name: text('components_assistant.request') }),
            );
            await waitFor(() => {
              expect(loop.calls.assistantRequests).toBe(1);
            }, WAIT);
            await advance(env.clock, 150_000, 5_000);
            const panel = screen.getByRole('region', { name: text('components_assistant.title') });
            expect(['requesting', 'polling']).not.toContain(panel.dataset.status);
            // A lo sumo una consulta cada 5 s antes de los 120 s: 23.
            expect(loop.calls.statusAt.length).toBeLessThanOrEqual(23);
            // El flujo guiado: la plantilla, su primer porqué editable y sus acciones.
            expect(screen.getByRole('region', { name: text('scr11.wizard_title') })).toBeDefined();
            const statement = screen.getByRole('textbox', { name: `${text('scr11.statement')} 1` });
            expect(statement).toHaveProperty('disabled', false);
            expect(screen.getByRole('button', { name: text('scr11.save_step') })).toHaveProperty(
              'disabled',
              false,
            );
            expect(
              screen.getByRole('button', {
                name: text('components_structure.wizard.save_partial'),
              }),
            ).toBeDefined();
            cleanup();
          },
        ),
        { numRuns: RUNS },
      );
    },
    PROPERTY_TIMEOUT_MS,
  );
});
