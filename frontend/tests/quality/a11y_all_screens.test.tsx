/**
 * NFR-APP-50 (LC-APP-28): cada pantalla registrada en `SCREENS` (las dieciséis, en cada uno de
 * sus grupos de rutas) se monta sobre el armazón real con los datos de MSW generados desde la
 * OpenAPI y pasa `axe` sin violaciones de impacto serio ni crítico. Las moderadas y menores se
 * informan, no fallan.
 *
 * La prueba exige antes que la pantalla muestre su contenido: `mountEntry` espera a que esté
 * pintada y pulsa su consulta auditada (el lazo, la clasificación, el acta, la causa raíz…), y
 * aquí se descartan la página de fallo del armazón, el estado de error de sección y cualquier
 * texto de carga. `axe` sobre un botón de consulta o sobre «No se pudo cargar esta pantalla.» no
 * diría nada de la pantalla.
 *
 * jsdom no calcula estilos ni geometría: `color-contrast` y las reglas de tamaño quedan como
 * «incompletas» y las cubre `tests/e2e/a11y.spec.ts` en navegadores reales. La revisión manual
 * con teclado y lector de pantalla está en `docs/u05-revision-manual-a11y.md`.
 */
import axeCore from 'axe-core';
import { describe, expect, it } from 'vitest';

import { SCREENS } from '@/app/screens';
import { text } from '@/texts';

import { mountEntry, revealButton, SCREEN_ENTRIES, unrevealed } from '../support/screenTour';

const BLOCKING = new Set(['serious', 'critical']);

/** Una línea del informe por pantalla y grupo (Evidence de VIG-336). */
function report(label: string, results: axeCore.AxeResults): string {
  const count = (impact: string) =>
    results.violations.filter((violation) => violation.impact === impact).length;
  return (
    `[axe] ${label}: críticas ${String(count('critical'))}, serias ${String(count('serious'))}, ` +
    `moderadas ${String(count('moderate'))}, menores ${String(count('minor'))}; ` +
    `reglas superadas ${String(results.passes.length)}`
  );
}

describe('NFR-APP-50: axe en todas las pantallas', { timeout: 60_000 }, () => {
  it('el recorrido cubre las dieciséis pantallas y todos sus grupos de rutas', () => {
    expect(new Set(SCREEN_ENTRIES.map((entry) => entry.screen)).size).toBe(16);
    expect(SCREEN_ENTRIES.length).toBe(
      SCREENS.reduce((total, screen) => total + screen.route_groups.length, 0),
    );
  });

  it.each(SCREEN_ENTRIES)('$screen · $group ($template)', async (entry) => {
    const fixture = await mountEntry(entry);
    expect(fixture.services.errors.getSnapshot()).toBeNull();
    expect(document.querySelector('main h1')?.textContent).not.toBe(text('app_shell.error.title'));
    expect(unrevealed()).toBeNull();

    const results = await axeCore.run(document.body, { resultTypes: ['violations', 'passes'] });
    const reveal = revealButton(entry);
    const label = `${entry.screen} ${entry.template}${reveal === null ? '' : ` (tras «${reveal}»)`}`;
    process.stdout.write(`${report(label, results)}\n`);
    const blocking = results.violations
      .filter((violation) => BLOCKING.has(violation.impact ?? ''))
      .map((violation) => ({
        id: violation.id,
        impact: violation.impact,
        nodes: violation.nodes.map((node) => node.target.join(' ')),
      }));
    expect(blocking).toEqual([]);
  });
});
