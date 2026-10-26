/**
 * Arnés de los escenarios FS-APP-01 a 10 (LC-APP-26, PAT-APP-RES-08): semilla por escenario,
 * parámetros dibujados con fast-check, registro de las peticiones que llegan a MSW, avance del
 * reloj simulado y la marca que lee el informe (`report.ts`).
 *
 * La semilla de la corrida es la de fast-check (`FC_SEED`, que imprime `tests/setup/global.ts`):
 * `FC_SEED=<semilla> npm test -- tests/resilience` repite los mismos parámetros. La de cada
 * escenario se deriva de ella y de su identificador, así que añadir uno no cambia los demás.
 */
import { act, fireEvent, screen, within } from '@testing-library/react';
import fc from 'fast-check';
import { http, HttpResponse } from 'msw';
import { afterEach, onTestFinished, type TestContext } from 'vitest';

import type { ScreenDescriptor } from '@/app/descriptor';
import { SCREENS } from '@/app/screens';
import type { MeResponse } from '@/ports/shared';
import { text } from '@/texts';

import { parseSeed } from '../../config/fastCheckProfile';
import { scenario } from '../msw/scenario';
import {
  FS_PLANT,
  FS_ZONES,
  fsHierarchy,
  resetFaults,
  setScenarioClock,
} from '../msw/scenarios/fs_app';
import { server } from '../setup/msw';
import { mountShell, ORIGIN, shellMe, WAIT, type ShellFixture } from '../support/appShell';
import type { FakeClock } from '../support/fakeClock';
import { flush } from '../support/ports';

import { SCENARIOS, type ScenarioId } from './scenarios';

/** Semilla de la corrida (`FC_SEED`, resuelta por `vitest.config.ts`). */
function runSeed(): number {
  const seed = parseSeed(process.env.FC_SEED);
  if (seed === null) throw new Error('Falta FC_SEED: vitest.config.ts debe resolver la semilla.');
  return seed;
}

/** Semilla del escenario: la de la corrida combinada con su identificador (FNV-1a de 32 bits). */
function scenarioSeed(id: ScenarioId, seed = runSeed()): number {
  let hash = 0x811c9dc5;
  for (const char of id) {
    hash ^= char.codePointAt(0) ?? 0;
    hash = Math.imul(hash, 0x01000193);
  }
  return (hash ^ seed) | 0;
}

/**
 * Marca la prueba como el escenario `id` y dibuja sus parámetros con su semilla. La marca se
 * pone antes de ejecutar nada, así que el informe también recoge el escenario que falla.
 */
export function scenarioCase<T extends Record<string, unknown>>(
  context: TestContext,
  id: ScenarioId,
  parameters: fc.Arbitrary<T>,
): T {
  const seed = scenarioSeed(id);
  const [drawn] = fc.sample(parameters, { seed, numRuns: 1 });
  if (drawn === undefined) throw new Error(`${id}: fast-check no dibujó parámetros.`);
  context.task.meta.fsApp = {
    id,
    title: SCENARIOS[id],
    run_seed: runSeed(),
    scenario_seed: seed,
    parameters: drawn,
  };
  return drawn;
}

/** Entero en `[min, max]`. */
export function integer(min: number, max: number): fc.Arbitrary<number> {
  return fc.integer({ min, max });
}

/** Texto sintético legible, sin datos de personas: prefijo fijo y un sufijo dibujado. */
export function syntheticText(prefix: string): fc.Arbitrary<string> {
  return fc.stringMatching(/^[a-z]{4,12}$/).map((suffix) => `${prefix} ${suffix}`);
}

interface TrafficEntry {
  readonly method: string;
  readonly path: string;
  /** Lo que `note` devolvió al salir la petición (p. ej. el estado del circuito). */
  readonly note: string | null;
}

/**
 * Peticiones que llegan a MSW durante la prueba (método y ruta, sin consulta ni cuerpo); `note`
 * anota cada una en el instante en que sale.
 */
export function traffic(note: () => string | null = () => null) {
  const entries: TrafficEntry[] = [];
  const listener = ({ request }: { readonly request: Request }) => {
    entries.push({ method: request.method, path: new URL(request.url).pathname, note: note() });
  };
  server.events.on('request:start', listener);
  onTestFinished(() => {
    server.events.removeListener('request:start', listener);
  });
  return {
    entries,
    count(method: string, path: string | RegExp): number {
      return entries.filter(
        (entry) =>
          entry.method === method &&
          (typeof path === 'string' ? entry.path === path : path.test(entry.path)),
      ).length;
    },
  };
}

/** Avanza el reloj simulado de `stepMs` en `stepMs`, dejando correr MSW y React entre pasos. */
export async function pump(clock: FakeClock, ms: number, stepMs = 1_000): Promise<void> {
  for (let elapsed = 0; elapsed < ms; elapsed += stepMs) {
    await act(async () => {
      clock.advance(Math.min(stepMs, ms - elapsed));
      await flush();
    });
  }
}

/** Deja correr las promesas pendientes sin mover el reloj. */
export async function settle(): Promise<void> {
  await act(async () => {
    await flush();
  });
}

/** Lo que se escribe en la solicitud de exportación de SCR-14 (el formulario de varios campos). */
export interface ExportDraft extends Record<string, unknown> {
  readonly zones: number;
  readonly packageForm: 'with_clips' | 'without_clips';
  readonly day: number;
}

export const exportDraft: fc.Arbitrary<ExportDraft> = fc.record({
  zones: integer(1, FS_ZONES.length),
  packageForm: fc.constantFrom('with_clips' as const, 'without_clips' as const),
  day: integer(1, 27),
});

function localDay(day: number): string {
  return `2026-01-${String(day).padStart(2, '0')}T08:00`;
}

/** Los valores visibles del formulario de SCR-14. */
export interface ExportFormValues {
  readonly plant: string;
  readonly zones: readonly string[];
  readonly from: string;
  readonly to: string;
  readonly packageForm: string;
}

export function expectedExportValues(draft: ExportDraft): ExportFormValues {
  return {
    plant: FS_PLANT,
    zones: FS_ZONES.slice(0, draft.zones).map((zone) => zone.name),
    from: localDay(draft.day),
    to: localDay(draft.day + 1),
    packageForm: draft.packageForm,
  };
}

/** Escribe `draft` en el formulario de SCR-14 ya montado. */
export function fillExportRequest(draft: ExportDraft): void {
  const values = expectedExportValues(draft);
  const form = within(exportForm());
  fireEvent.change(form.getByLabelText(text('scr14.plant')), { target: { value: values.plant } });
  for (const zone of values.zones) fireEvent.click(form.getByRole('checkbox', { name: zone }));
  fireEvent.change(form.getByLabelText(text('scr14.from')), { target: { value: values.from } });
  fireEvent.change(form.getByLabelText(text('scr14.to')), { target: { value: values.to } });
  fireEvent.change(form.getByLabelText(text('scr14.package_form')), {
    target: { value: values.packageForm },
  });
}

export function exportForm(): HTMLElement {
  return screen.getByRole('form', { name: text('scr14.request') });
}

/** Lo que muestra ahora el formulario de SCR-14. */
export function exportFormValues(): ExportFormValues {
  const form = exportForm();
  const value = (name: string) => within(form).getByLabelText<HTMLInputElement>(text(name)).value;
  return {
    plant: value('scr14.plant'),
    zones: within(form)
      .getAllByRole<HTMLInputElement>('checkbox')
      .filter((box) => box.checked)
      .map((box) => box.getAttribute('aria-label') ?? ''),
    from: value('scr14.from'),
    to: value('scr14.to'),
    packageForm: value('scr14.package_form'),
  };
}

/** «Repetir» del aviso de la escritura (UI-19 `NoticeStack`). */
export function retryButton(): HTMLButtonElement {
  return screen.getByRole('button', { name: text('components_forms.notice.retry') });
}

/** Texto exacto de la cuenta regresiva de «Repetir» con `seconds` por delante. */
export function countdownText(seconds: number): string {
  return `${text('components_forms.notice.retry_in')} ${String(seconds)} ${text('components_forms.notice.seconds')}`;
}

export function submitExportRequest(): void {
  fireEvent.click(within(exportForm()).getByRole('button', { name: text('scr14.request') }));
}

/** Permisos de la persona de los escenarios con armazón: SCR-14 y la campana de SCR-15. */
export const EXPORT_PERMISSIONS = ['export.create', 'notifications.read'];

/**
 * La aplicación completa (armazón, servicios reales, tabla real de pantallas y transporte contra
 * MSW) abierta en la solicitud de exportación de SCR-14, con la jerarquía ya cargada.
 */
export async function mountExports(
  options: Parameters<typeof mountShell>[2] & {
    /** `GET /me` de la sesión; por omisión `shellMe(EXPORT_PERMISSIONS)`. */
    readonly me?: MeResponse;
    /** Tabla de pantallas; por omisión la real de la aplicación. */
    readonly screens?: readonly ScreenDescriptor[];
  } = {},
): Promise<ShellFixture> {
  const me = options.me ?? shellMe(EXPORT_PERMISSIONS);
  // El primero de la lista gana: la sesión sintética antes que el ejemplo generado de `GET /me`.
  server.use(
    http.get(`${ORIGIN}/me`, () =>
      HttpResponse.json(me, {
        headers: { 'X-Vigia-Api-Version': import.meta.env.VITE_APP_VERSION },
      }),
    ),
    ...scenario('fs_app_base'),
  );
  const fixture = mountShell('/exports', options.screens ?? SCREENS, options);
  scenarioClock(fixture.clock);
  const form = await screen.findByRole('form', { name: text('scr14.request') }, WAIT);
  await within(form).findByRole('option', { name: fsHierarchy().plants[0]?.name ?? '' }, WAIT);
  return fixture;
}

/** Los manejadores de FS-APP leen el instante del reloj de la prueba. */
export function scenarioClock(clock: FakeClock): void {
  setScenarioClock(() => clock.now());
}

afterEach(() => {
  resetFaults();
  setScenarioClock(() => 0);
});
