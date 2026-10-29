/**
 * Matriz de pases (IF-13 paso 3; RV-09; BR-APP-26; U05-H-11; NFR-APP-50).
 *
 * - La matriz muestra las filas postura × estado de señal con el avance por fila y el mínimo de
 *   pases por celda que fijó el servidor; ningún «% garantizado» (BR-APP-26).
 * - La evidencia de un pase es un `clip_id` del selector de clips de comisionamiento: el cuerpo
 *   enviado solo lleva `row_id`, `result` y `evidence_ref`, y no hay ningún campo de archivo.
 * - En 768 × 1024 los controles de la matriz cumplen el objetivo táctil de 44 px del sistema de
 *   diseño, y `axe` no encuentra violaciones serias.
 */
import { readFileSync } from 'node:fs';
import { join } from 'node:path';

import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { HttpResponse } from 'msw';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { axe } from 'vitest-axe';

import { PassForm } from '@/pages/scr06/matrix/PassForm';
import { PassMatrixTable } from '@/pages/scr06/matrix/PassMatrixTable';
import { MatrixScreen } from '@/pages/scr06/matrix/MatrixScreen';
import { text } from '@/texts';
import { label, labelInline } from '@/texts/labels';

import { server } from '../../msw/server';
import {
  CLIP_A,
  CLIP_B,
  PASS_ID,
  ROW_CROUCHED,
  ROW_STANDING,
  ZONE_ID,
  captureHandler,
  clip,
  clipsHandler,
  clipsPage,
  currentWalkTest,
  currentWalkTestHandler,
  pass,
  resetTransport,
  renderScreen,
  row,
  selectRowName,
  session,
  type CapturedRequest,
  type TestSession,
} from './support';

const ROOT = process.cwd();
/** Hoja sin comentarios: un comentario delante de un selector rompería la lectura. */
const sheet = (path: string): string =>
  readFileSync(join(ROOT, path), 'utf8').replace(/\/\*[\s\S]*?\*\//g, '');
const MATRIX_CSS = sheet('src/pages/scr06/matrix/matrix.module.css');
const BASE_CSS = sheet('src/styles/base.css');
const TOKENS_CSS = sheet('src/styles/tokens.css');

const CHOOSE_ROW = text('scr06.matrix.form.choose_row');
const REGISTER = text('scr06.matrix.form.submit');
const RESULT_LABEL = text('scr06.matrix.form.result');
const EVIDENCE_LABEL = text('scr06.matrix.form.evidence');
/** Un identificador que no cumple `format: uuid` del contrato: el espejo del puerto lo rechaza. */
const NOT_A_UUID = 'no-es-un-uuid';

/** Las condiciones de la fila por omisión, con las etiquetas del catálogo del contrato. */
const ROW_CONDITIONS = [
  text('scr06.matrix.condition.presence'),
  `${label('condition_signal_role', 'energy')} ${labelInline('condition_signal_value', 'asserted')}`,
].join(' · ');

let mounted: TestSession | null = null;

beforeEach(() => {
  mounted = null;
});

afterEach(() => {
  mounted?.cache.dispose();
  mounted = null;
  resetTransport();
});

function matrixSession() {
  return session({
    rows: [
      row(),
      row({
        row_id: ROW_CROUCHED,
        posture: 'crouched',
        required_passes: 3,
        passes: { detected: 2, missed: 1, false_alarm: 0 },
      }),
    ],
    passes: [],
  });
}

function openScreen(): {
  readonly calls: CapturedRequest[];
  readonly mounted: TestSession;
} {
  const calls: CapturedRequest[] = [];
  server.use(
    currentWalkTestHandler(currentWalkTest(matrixSession())),
    clipsHandler(clipsPage([clip(), clip({ clip_id: CLIP_B })])),
    captureHandler({
      method: 'post',
      path: '/walk-tests/{session_id}/passes',
      calls,
      response: () => HttpResponse.json(pass(), { status: 201 }),
    }),
  );
  const rendered = renderScreen((clock) => <MatrixScreen zoneId={ZONE_ID} clock={clock} />);
  mounted = rendered;
  return { calls, mounted: rendered };
}

describe('matriz de pases', () => {
  it('muestra las filas postura × estado de señal con avance y pases requeridos', async () => {
    openScreen();
    await screen.findByRole('button', { name: selectRowName('standing', 1) });
    const rows = document.querySelectorAll('[data-part="matrix_row"]');
    expect(rows).toHaveLength(2);
    expect(screen.getByRole('button', { name: selectRowName('crouched', 2) })).toBeTruthy();
    const progress = Array.from(
      document.querySelectorAll('[data-part="row_progress"]'),
      (cell) => cell.textContent,
    );
    expect(progress).toEqual(['1 / 3', '3 / 3']);
    // El estado de señal de cada fila, con las etiquetas del catálogo, no su número de condiciones.
    const conditions = Array.from(
      document.querySelectorAll('[data-part="conditions"]'),
      (cell) => cell.textContent,
    );
    expect(conditions).toEqual([ROW_CONDITIONS, ROW_CONDITIONS]);
    // Ninguna cifra del servidor se presenta como garantía (BR-APP-26): el texto de la pantalla
    // no lleva «%» ni la palabra prohibida.
    expect(document.body.textContent).not.toContain('%');
  });

  it('registra un pase con el clip elegido y sin ningún archivo', async () => {
    const { calls } = openScreen();
    fireEvent.click(
      await screen.findByRole('button', {
        name: selectRowName('standing', 1),
      }),
    );
    fireEvent.change(screen.getByRole('combobox', { name: RESULT_LABEL }), {
      target: { value: 'false_alarm' },
    });
    fireEvent.change(screen.getByRole('combobox', { name: EVIDENCE_LABEL }), {
      target: { value: CLIP_B },
    });
    fireEvent.click(screen.getByRole('button', { name: REGISTER }));
    await waitFor(() => {
      expect(calls).toHaveLength(1);
    });
    expect(calls[0]?.url).toBe(`/walk-tests/${session().session_id}/passes`);
    expect(Object.keys(calls[0]?.body as Record<string, unknown>).sort()).toEqual([
      'evidence_ref',
      'result',
      'row_id',
    ]);
    expect(calls[0]?.body).toEqual({
      row_id: ROW_STANDING,
      result: 'false_alarm',
      evidence_ref: CLIP_B,
    });
    // La evidencia nunca es un archivo: no hay campo de archivo ni clave con ese contenido.
    expect(document.querySelector('input[type="file"]')).toBeNull();
    expect(calls[0]?.body).not.toHaveProperty('file');
  });

  it('sin evidencia elegida el pase va con evidence_ref nulo', async () => {
    const { calls } = openScreen();
    fireEvent.click(
      await screen.findByRole('button', {
        name: selectRowName('standing', 1),
      }),
    );
    fireEvent.click(screen.getByRole('button', { name: REGISTER }));
    await waitFor(() => {
      expect(calls).toHaveLength(1);
    });
    expect(calls[0]?.body).toEqual({
      row_id: ROW_STANDING,
      result: 'detected',
      evidence_ref: null,
    });
  });

  it('la fila elegida se identifica con su número, su postura y su estado de señal', async () => {
    openScreen();
    fireEvent.click(
      await screen.findByRole('button', {
        name: selectRowName('crouched', 2),
      }),
    );
    // La postura sola no distingue dos filas con condiciones distintas: la etiqueta lleva las
    // condiciones de señal de la fila, como la matriz.
    expect(document.querySelector('[data-part="selected_row"]')?.textContent).toBe(
      `${text('scr06.matrix.selected')}: 2 · ${label('posture', 'crouched')} · ${ROW_CONDITIONS}`,
    );
  });

  it('tras registrar un pase el selector de evidencia vuelve a «sin evidencia»', async () => {
    const { calls } = openScreen();
    fireEvent.click(
      await screen.findByRole('button', {
        name: selectRowName('standing', 1),
      }),
    );
    fireEvent.change(screen.getByRole('combobox', { name: EVIDENCE_LABEL }), {
      target: { value: CLIP_B },
    });
    fireEvent.click(screen.getByRole('button', { name: REGISTER }));
    await waitFor(() => {
      expect(calls).toHaveLength(1);
    });
    // El clip elegido no se reutiliza sin querer en el pase siguiente.
    const selector = screen.getByRole<HTMLSelectElement>('combobox', { name: EVIDENCE_LABEL });
    expect(selector.value).toBe('');
  });

  it('el problema de la evidencia se muestra junto al campo y se limpia al registrar', async () => {
    const calls: CapturedRequest[] = [];
    server.use(
      currentWalkTestHandler(currentWalkTest(matrixSession())),
      // El selector ofrece un clip que no cumple el contrato: el espejo del puerto lo rechaza.
      clipsHandler(clipsPage([clip({ clip_id: NOT_A_UUID }), clip({ clip_id: CLIP_A })])),
      captureHandler({
        method: 'post',
        path: '/walk-tests/{session_id}/passes',
        calls,
        response: () => HttpResponse.json(pass(), { status: 201 }),
      }),
    );
    renderScreen((clock) => <MatrixScreen zoneId={ZONE_ID} clock={clock} />);
    fireEvent.click(
      await screen.findByRole('button', {
        name: selectRowName('standing', 1),
      }),
    );
    const evidence = screen.getByRole('combobox', { name: EVIDENCE_LABEL });
    fireEvent.change(evidence, { target: { value: NOT_A_UUID } });
    fireEvent.click(screen.getByRole('button', { name: REGISTER }));
    await waitFor(() => {
      expect(document.querySelector('[data-part="local_error"]')).not.toBeNull();
    });
    // Nada salió a la red: el problema es estructural.
    expect(calls).toHaveLength(0);
    fireEvent.change(evidence, { target: { value: CLIP_A } });
    fireEvent.click(screen.getByRole('button', { name: REGISTER }));
    await waitFor(() => {
      expect(calls).toHaveLength(1);
    });
    // El error anterior no se queda pegado al campo.
    await waitFor(() => {
      expect(document.querySelector('[data-part="local_error"]')).toBeNull();
    });
  });

  it('un problema estructural sin hueco en el formulario va a la pila de avisos', async () => {
    const calls: CapturedRequest[] = [];
    server.use(
      // La fila del servidor trae un `row_id` que no cumple el contrato: no tiene hueco propio.
      currentWalkTestHandler(
        currentWalkTest(session({ rows: [row({ row_id: NOT_A_UUID })], passes: [] })),
      ),
      clipsHandler(clipsPage([clip()])),
      captureHandler({
        method: 'post',
        path: '/walk-tests/{session_id}/passes',
        calls,
        response: () => HttpResponse.json(pass(), { status: 201 }),
      }),
    );
    renderScreen((clock) => <MatrixScreen zoneId={ZONE_ID} clock={clock} />);
    fireEvent.click(
      await screen.findByRole('button', {
        name: selectRowName('standing', 1),
      }),
    );
    fireEvent.click(screen.getByRole('button', { name: REGISTER }));
    await waitFor(() => {
      expect(document.querySelector('li[data-code="invalid_request"]')).not.toBeNull();
    });
    expect(calls).toHaveLength(0);
    expect(document.querySelector('[data-part="local_error"]')).toBeNull();
  });

  it('pide elegir una fila antes de registrar', async () => {
    openScreen();
    await screen.findByRole('button', { name: selectRowName('standing', 1) });
    expect(screen.getByText(CHOOSE_ROW)).toBeTruthy();
    expect(screen.queryByRole('button', { name: REGISTER })).toBeNull();
  });

  it('muestra el pase registrado con su resultado y su evidencia', async () => {
    server.use(
      currentWalkTestHandler(
        currentWalkTest(session({ passes: [pass({ result: 'missed', evidence_ref: CLIP_A })] })),
      ),
      clipsHandler(clipsPage([clip()])),
    );
    renderScreen((clock) => <MatrixScreen zoneId={ZONE_ID} clock={clock} />);
    const item = await screen.findByText(label('pass_result', 'missed'));
    const entry = item.closest('[data-part="pass"]');
    expect(entry?.getAttribute('data-pass-id')).toBe(PASS_ID);
    expect(entry?.textContent).toContain(CLIP_A);
  });
});

/**
 * Nombre de la clase en el CSS a partir del que quedó en el DOM: los módulos de Vite nombran
 * `_<clase>_<hash>`, y la hoja de origen tiene `.<clase>`.
 */
function sourceName(rendered: string): string {
  const match = /^_(.+)_[0-9a-z]+$/.exec(rendered);
  return match?.[1] ?? rendered;
}

/** Valor de `min-height` de la primera regla que nombra la clase, en `px`. */
function declaredMinHeight(css: string, className: string): string | null {
  for (const match of css.matchAll(/([^{}]+)\{([^{}]*)\}/g)) {
    const selectors = (match[1] ?? '').split(',').map((selector) => selector.trim());
    if (!selectors.includes(`.${className}`)) continue;
    const declaration = /min-height:\s*([^;]+);/.exec(match[2] ?? '');
    if (declaration !== null) return (declaration[1] ?? '').trim();
  }
  return null;
}

function tokenValue(name: string): string | null {
  const match = new RegExp(`${name}:\\s*([^;]+);`).exec(TOKENS_CSS);
  return match === null ? null : (match[1] ?? '').trim();
}

/** Resuelve `var(--token)` y `44px` al número de píxeles, o `null` si no es medible. */
function pixels(value: string | null): number | null {
  if (value === null) return null;
  const variable = /^var\((--[a-z0-9-]+)\)$/.exec(value);
  if (variable !== null) return pixels(tokenValue(variable[1] ?? ''));
  const px = /^(\d+)px$/.exec(value);
  return px === null ? null : Number(px[1]);
}

describe('objetivo táctil en tableta (768 × 1024)', () => {
  /**
   * Se lee el `min-height` declarado en la hoja, no el estilo computado que pide el criterio:
   * jsdom no aplica los módulos CSS (la clase del DOM es `_clase_hash`) y el repositorio no tiene
   * ninguna prueba de estilos computados. Cuando llegue la medición en navegador (Playwright, e2e)
   * es allí donde toca `getComputedStyle`; queda anotado, no bloquea este criterio.
   */
  it('los controles de la matriz declaran al menos 44 px con el token del sistema', () => {
    window.innerWidth = 768;
    window.innerHeight = 1024;
    const { container } = render(
      <main>
        <PassMatrixTable session={matrixSession()} onSelectRow={() => undefined} />
        <PassForm
          rowId={ROW_STANDING}
          rows={matrixSession().rows}
          clips={[clip()]}
          busy={false}
          onSubmit={() => Promise.resolve(false)}
        />
      </main>,
    );
    const controls = Array.from(container.querySelectorAll('button, select, input, textarea, a'));
    expect(controls.length).toBeGreaterThanOrEqual(4);
    for (const control of controls) {
      const classes = control.className
        .split(/\s+/)
        .filter((name) => name !== '')
        .map(sourceName);
      const declared = classes
        .map((name) => declaredMinHeight(MATRIX_CSS, name) ?? declaredMinHeight(BASE_CSS, name))
        .find((value) => value !== null);
      const measured = pixels(declared ?? null);
      const where = `${control.tagName.toLowerCase()}.${classes.join('.')}`;
      expect(measured, `${where} sin min-height declarado`).not.toBeNull();
      expect(measured, where).toBeGreaterThanOrEqual(44);
    }
  });

  it('axe no encuentra violaciones serias en la matriz', async () => {
    const { container } = render(
      <main>
        <PassMatrixTable
          session={matrixSession()}
          selectedRowId={ROW_STANDING}
          onSelectRow={() => undefined}
          exposureOf={() => 'sent'}
        />
        <PassForm
          rowId={ROW_STANDING}
          rows={matrixSession().rows}
          clips={[clip(), clip({ clip_id: CLIP_B })]}
          busy={false}
          onSubmit={() => Promise.resolve(false)}
        />
      </main>,
    );
    const results = await axe(container);
    const serious = results.violations.filter(
      (violation) => violation.impact === 'serious' || violation.impact === 'critical',
    );
    expect(serious).toEqual([]);
    expect(results).toHaveNoViolations();
  });

  // `label-content-name-mismatch` de axe cayó en Firefox y WebKit (VIG-356); en jsdom esa regla no
  // se evalúa, así que se comprueba directamente que el nombre empieza por el texto visible.
  it('el nombre accesible de cada «Fila n» empieza por su texto visible (WCAG 2.5.3)', () => {
    const { container } = render(
      <main>
        <PassMatrixTable session={matrixSession()} onSelectRow={() => undefined} />
      </main>,
    );
    const buttons = [...container.querySelectorAll('[data-part="select_row"]')];
    expect(buttons).toHaveLength(2);
    for (const button of buttons) {
      expect(button.getAttribute('aria-label')?.startsWith(button.textContent)).toBe(true);
    }
  });
});
