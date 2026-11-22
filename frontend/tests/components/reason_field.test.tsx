/**
 * UI-13 `ReasonField` (BR-APP-42, 44; PR-APP-12): obligatorio, contador, límites de la unidad
 * dueña leídos del esquema generado (concesión 10 a 500; cierre de U-04 20 a 2 000), espejo de
 * `validateBody` y el mensaje del servidor por encima del local. Se comparan claves y textos del
 * catálogo, nunca subcadenas.
 *
 * A-126 (TASK-580): opcional, recorte de los extremos en los textos del lazo, aviso en vivo de los
 * saltos de línea con «Unir en una línea» y el motivo real de cada rechazo del servidor.
 */
import { fireEvent, render, screen } from '@testing-library/react';
import { useState } from 'react';
import { describe, expect, it } from 'vitest';
import { axe } from 'vitest-axe';

import assign from '../../openapi/bodies/assign_loop_findings__finding_state_id__actions_post.json';
import closure from '../../openapi/bodies/close_loop_findings__finding_state_id__closure_post.json';
import classify from '../../openapi/bodies/classify_loop_findings__finding_state_id__classifications_post.json';
import concession from '../../openapi/bodies/grant_provider_concessions_post.json';
import type { Notice } from '@/components/NoticeStack/notice';
import { reasonLimits } from '@/components/ReasonField/limits';
import {
  optionalText,
  ReasonField,
  reasonIssueKey,
  reasonVerdict,
  submittedText,
} from '@/components/ReasonField/ReasonField';
import { textFields } from '@/components/ReasonField/TextNotice';
import type { FreeTextLimits } from '@/ports/validation';
import { validateBody } from '@/ports/validation';
import { text } from '@/texts';
import { label } from '@/texts/labels';

const LABEL = 'Motivo de la concesión';

function Harness({
  limits,
  initial = '',
  notice,
}: {
  readonly limits: FreeTextLimits;
  readonly initial?: string;
  readonly notice?: Notice;
}) {
  const [value, setValue] = useState(initial);
  return (
    <ReasonField label={LABEL} value={value} onChange={setValue} limits={limits} notice={notice} />
  );
}

function field(): HTMLTextAreaElement {
  return screen.getByRole('textbox', {
    name: `${LABEL} (${text('components_forms.reason.required')})`,
  });
}

function type(value: string): void {
  fireEvent.change(field(), { target: { value } });
  fireEvent.blur(field());
}

function localErrorKey(container: HTMLElement): string | null {
  return (
    container
      .querySelector<HTMLElement>('[data-part="local_error"]')
      ?.getAttribute('data-text-key') ?? null
  );
}

function counter(container: HTMLElement): string | null {
  return container.querySelector('[data-part="counter"]')?.textContent ?? null;
}

function counterText(length: number, limits: FreeTextLimits): string {
  return `${String(length)} / ${String(limits.maxLength)} ${text('components_forms.reason.characters')} (${text('components_forms.reason.minimum')} ${String(limits.minLength)})`;
}

const CONCESSION = reasonLimits(concession, 'reason');
const CLOSURE = reasonLimits(closure, 'closure_description');

describe('límites del esquema generado', () => {
  it('concesión: 10 a 500 (BR-NUC-35)', () => {
    expect(CONCESSION).toEqual({ minLength: 10, maxLength: 500 });
  });

  it('cierre de U-04: los límites de closure_description', () => {
    expect(CLOSURE).toEqual({ minLength: 20, maxLength: 2000 });
  });

  it('un campo sin límites en el esquema es un error de programación, no un límite inventado', () => {
    expect(() => reasonLimits(classify, 'reason_text')).toThrow(RangeError);
    expect(() => reasonLimits(concession, 'no_such_field')).toThrow(RangeError);
  });
});

describe('UI-13 en concesión (10 a 500)', () => {
  it.each([
    ['', 'validation.required'],
    ['   ', 'validation.required'],
    ['a'.repeat(9), 'validation.free_text.too_short'],
    ['a'.repeat(10), null],
    ['a'.repeat(500), null],
    ['a'.repeat(501), 'validation.free_text.too_long'],
    ['😀'.repeat(10), null],
    ['😀'.repeat(9), 'validation.free_text.too_short'],
    ['motivo <b>con</b> marcado', 'validation.free_text.markup'],
  ])('%j → %s', (value, key) => {
    const { container } = render(<Harness limits={CONCESSION} />);
    type(value);
    expect(localErrorKey(container)).toBe(key);
    if (key !== null) {
      expect(container.querySelector('[data-part="local_error"]')?.textContent).toBe(text(key));
      expect(field().getAttribute('aria-invalid')).toBe('true');
    } else {
      expect(field().getAttribute('aria-invalid')).toBe('false');
    }
    expect(field().value).toBe(value);
  });

  it('el espejo coincide con validateBody del cuerpo de concesión en los bordes', () => {
    for (const length of [9, 10, 500, 501]) {
      const reason = 'm'.repeat(length);
      const body = {
        client_organization_id: '0190a3b2-7c4d-7e8f-9a0b-1c2d3e4f5a6b',
        scope_level: 'plant',
        scope_id: '0190a3b2-7c4d-7e8f-9a0b-1c2d3e4f5a6c',
        reason,
      };
      const server = validateBody('grant_provider_concessions_post', body).ok;
      expect(reasonIssueKey(reason, CONCESSION) === null).toBe(server);
    }
  });

  it('es obligatorio y muestra el contador desde el principio', () => {
    const { container } = render(<Harness limits={CONCESSION} />);
    expect(field().required).toBe(true);
    expect(field().getAttribute('aria-required')).toBe('true');
    expect(counter(container)).toBe(counterText(0, CONCESSION));
    expect(localErrorKey(container)).toBeNull();
    type('😀'.repeat(12));
    expect(counter(container)).toBe(counterText(12, CONCESSION));
  });
});

describe('UI-13 en un campo de U-04 (cierre, 20 a 2 000)', () => {
  it.each([
    [19, 'validation.free_text.too_short'],
    [20, null],
    [2000, null],
    [2001, 'validation.free_text.too_long'],
  ])('%i caracteres → %s', (length, key) => {
    const { container } = render(<Harness limits={CLOSURE} />);
    type('d'.repeat(length));
    expect(localErrorKey(container)).toBe(key);
    expect(counter(container)).toBe(counterText(length, CLOSURE));
  });
});

describe('el servidor prevalece', () => {
  it('un aviso anclado sustituye al error local y conserva lo escrito', () => {
    const written = 'corto';
    const notice: Notice = {
      id: 'n1',
      field: 'reason',
      error: {
        code: 'invalid_request',
        status: 422,
        detail_code: 'loop_future_rule',
        message_es: 'El servidor rechazó el motivo por una regla nueva.',
      },
    };
    const { container } = render(<Harness limits={CONCESSION} initial={written} notice={notice} />);
    fireEvent.blur(field());
    expect(localErrorKey(container)).toBeNull();
    const alert = screen.getByRole('alert');
    expect(alert.querySelector('[data-part="message"]')?.textContent).toBe(
      'El servidor rechazó el motivo por una regla nueva.',
    );
    expect(field().getAttribute('aria-describedby')?.split(' ')).toContain(alert.id);
    expect(field().getAttribute('aria-invalid')).toBe('true');
    expect(field().value).toBe(written);
  });
});

describe('axe', () => {
  it('ReasonField con error local y con aviso del servidor', async () => {
    const { container } = render(
      <main>
        <Harness limits={CONCESSION} />
        <ReasonField
          label="Motivo de retiro"
          value="x"
          onChange={() => undefined}
          limits={CONCESSION}
          showErrors
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
});

// --- A-126 (TASK-580): textos libres del lazo que guían en vez de rechazar ---------------------

const LOOP_LABEL = 'Descripción sintética';
const LOOP = { minLength: 20, maxLength: 1000 };
const VALID = 'Texto sintético del lazo con longitud suficiente';
const CORRELATION = '0190a1b2-7777-7000-8000-000000000580';

function LoopHarness({
  initial = '',
  optional = false,
  notice,
  limits = LOOP,
  onValue,
}: {
  readonly initial?: string;
  readonly optional?: boolean;
  readonly notice?: Notice;
  readonly limits?: FreeTextLimits;
  readonly onValue?: (value: string) => void;
}) {
  const [value, setValue] = useState(initial);
  return (
    <ReasonField
      label={LOOP_LABEL}
      value={value}
      onChange={(next) => {
        setValue(next);
        onValue?.(next);
      }}
      limits={limits}
      notice={notice}
      optional={optional}
      loopText
    />
  );
}

function loopField(requirement: 'required' | 'optional' = 'required'): HTMLTextAreaElement {
  return screen.getByRole('textbox', {
    name: `${LOOP_LABEL} (${text(`components_forms.reason.${requirement}`)})`,
  });
}

function lineBreakWarning(container: HTMLElement): HTMLElement | null {
  return container.querySelector<HTMLElement>('[data-part="line_break"]');
}

function serverNotice(
  detail_code: string | null,
  rejection: Readonly<Record<string, string>> | null = null,
): Notice {
  return {
    id: 'n-580',
    field: 'description',
    error: {
      code: 'invalid_request',
      status: 400,
      detail_code,
      message_es: 'La petición no es válida.',
      body: {
        code: 'invalid_request',
        detail_code,
        message_es: 'La petición no es válida.',
        correlation_id: CORRELATION,
        rejection,
      },
    },
  };
}

describe('A-126: el texto del lazo se mide y se envía recortado en los extremos', () => {
  it('el contador cuenta lo que se enviaría: NFC y sin espacios ni saltos de los extremos', () => {
    const { container } = render(<LoopHarness />);
    fireEvent.change(loopField(), { target: { value: ` \u00a0${'a'.repeat(14)}\n\u3000\n` } });
    expect(counter(container)).toBe(counterText(14, LOOP));
    fireEvent.blur(loopField());
    expect(localErrorKey(container)).toBe('validation.free_text.too_short');
    // NFD: e + U+0301 cuenta como un carácter, como en el servidor.
    fireEvent.change(loopField(), { target: { value: 'e\u0301'.repeat(3) } });
    expect(counter(container)).toBe(counterText(3, LOOP));
  });

  it('un Enter final no avisa ni es un error: se recorta al enviar', () => {
    const { container } = render(<LoopHarness />);
    fireEvent.change(loopField(), { target: { value: `${VALID}\n` } });
    fireEvent.blur(loopField());
    expect(lineBreakWarning(container)).toBeNull();
    expect(localErrorKey(container)).toBeNull();
    expect(reasonIssueKey(`${VALID}\n`, LOOP, { loopText: true })).toBeNull();
    expect(submittedText(` \n${VALID}\r\n\u2028 `)).toBe(VALID);
    // Fuera del lazo no se recorta nada: el salto final se enviaría y el espejo lo rechaza.
    expect(reasonIssueKey(`${VALID}\n`, LOOP)).toBe('validation.free_text.line_break');
  });

  it('el recorte nunca toca el interior ni quita invisibles de los extremos', () => {
    expect(submittedText(`  uno  dos\u00a0tres  `)).toBe('uno  dos\u00a0tres');
    expect(submittedText(`\u200b${VALID}\ufeff`)).toBe(`\u200b${VALID}\ufeff`);
    // Un invisible suelto no es «en blanco»: da su motivo, como el servidor.
    expect(reasonIssueKey('\ufeff', LOOP, { loopText: true })).toBe(
      'validation.free_text.invisible_character',
    );
    expect(reasonIssueKey(`\u200b${VALID}`, LOOP, { loopText: true })).toBe(
      'validation.free_text.invisible_character',
    );
  });

  it('un salto intermedio avisa en vivo y no cambia el texto hasta pulsar «Unir en una línea»', () => {
    const values: string[] = [];
    const { container } = render(
      <LoopHarness
        onValue={(value) => {
          values.push(value);
        }}
      />,
    );
    // jsdom, como el navegador, normaliza `\r\n` del textarea a `\n`: se usan saltos ya normales.
    const typed = 'Texto sintético con un salto\nen medio y otro\n\n\u2028más abajo';
    fireEvent.change(loopField(), { target: { value: typed } });
    // Sin perder el foco: el aviso sale mientras se escribe, con su clave.
    const warning = lineBreakWarning(container);
    expect(warning?.querySelector('[data-text-key]')?.getAttribute('data-text-key')).toBe(
      'validation.free_text.line_break',
    );
    expect(warning?.querySelector('[data-text-key]')?.textContent).toBe(
      text('validation.free_text.line_break'),
    );
    expect(loopField().value).toBe(typed);
    expect(values).toEqual([typed]);
    expect(loopField().getAttribute('aria-invalid')).toBe('true');
    expect(loopField().getAttribute('aria-describedby')?.split(' ')).toContain(warning?.id);
    expect(reasonIssueKey(typed, LOOP, { loopText: true })).toBe('validation.free_text.line_break');
    fireEvent.click(
      screen.getByRole('button', { name: text('components_forms.reason.join_lines') }),
    );
    expect(loopField().value).toBe('Texto sintético con un salto en medio y otro más abajo');
    expect(lineBreakWarning(container)).toBeNull();
    expect(loopField().getAttribute('aria-invalid')).toBe('false');
  });

  it('opcional: en blanco no hay error, la etiqueta lo dice y el contador no pide mínimo', () => {
    const { container } = render(
      <LoopHarness optional limits={{ minLength: 1, maxLength: 1000 }} />,
    );
    const input = loopField('optional');
    expect(input.required).toBe(false);
    expect(input.getAttribute('aria-required')).toBe('false');
    fireEvent.change(input, { target: { value: ' \n ' } });
    fireEvent.blur(input);
    expect(localErrorKey(container)).toBeNull();
    expect(counter(container)).toBe(`0 / 1000 ${text('components_forms.reason.characters')}`);
    expect(optionalText(' \n ')).toBeNull();
    expect(optionalText(` ${VALID}\n`)).toBe(VALID);
    // Lo escrito sí se valida.
    fireEvent.change(input, { target: { value: 'uno <b>dos</b>' } });
    expect(localErrorKey(container)).toBe('validation.free_text.markup');
  });

  it('obligatorio: en blanco (o solo saltos) es «obligatorio», nunca una petición vacía', () => {
    expect(reasonIssueKey('\n\n', LOOP, { loopText: true })).toBe('validation.required');
    expect(reasonVerdict('\n\n', LOOP, { loopText: true, optional: true })).toEqual({
      ok: true,
      sent: null,
    });
  });
});

describe('A-126: el motivo real del rechazo del servidor', () => {
  it('un 400 sin detail_code de un texto del lazo dice qué revisar y da el correlation_id', () => {
    const { container } = render(<LoopHarness initial={VALID} notice={serverNotice(null)} />);
    const alert = screen.getByRole('alert');
    expect(alert.getAttribute('data-text-key')).toBe('components_forms.reason.review');
    expect(alert.querySelector('[data-part="message"]')?.textContent).toBe(
      text('components_forms.reason.review'),
    );
    expect(alert.querySelector('[data-part="correlation_id"]')?.textContent).toBe(CORRELATION);
    expect(loopField().getAttribute('aria-describedby')?.split(' ')).toContain(alert.id);
    expect(localErrorKey(container)).toBeNull();
    expect(loopField().value).toBe(VALID);
  });

  it('fuera del lazo el mismo 400 conserva el aviso de siempre', () => {
    render(<Harness limits={CONCESSION} initial="motivo sintético" notice={serverNotice(null)} />);
    expect(screen.getByRole('alert').getAttribute('data-text-key')).toBe('errors.anchored');
  });

  it('una atribución a persona muestra la sugerencia del patrón como motivo principal', () => {
    const suggestion = 'Sugerencia sintética del patrón 580';
    render(
      <LoopHarness
        initial={VALID}
        notice={serverNotice('loop_person_attribution_detected', {
          code: 'person_attribution_detected',
          suggestion,
        })}
      />,
    );
    const alert = screen.getByRole('alert');
    expect(alert.querySelector('[data-part="message"]')?.textContent).toBe(suggestion);
    expect(alert.querySelector('[data-part="rejection-code"]')?.textContent).toBe(
      text('components_assistant.rejection.person_attribution_detected'),
    );
    expect(alert.querySelector('[data-part="rejection-suggestion"]')?.textContent).toBe(suggestion);
    // Sin la forma de `proper_name` en lo escrito, no hay pista de minúsculas.
    expect(alert.querySelector('[data-part="lowercase_hint"]')).toBeNull();
    expect(loopField().value).toBe(VALID);
  });

  it.each([
    ['dos palabras con mayúscula inicial', 'Revisión sintética del Turno Alfa en la prensa'],
    ['en mayúsculas o siglas', 'Revisión sintética del TURNO ALFA en la prensa'],
    ['con tildes', 'Revisión sintética de Ángel Óscar en la prensa'],
  ])('con %s, añade «pruebe en minúsculas»', (_case, written) => {
    render(
      <LoopHarness
        initial={written}
        notice={serverNotice('loop_person_attribution_detected', {
          code: 'person_attribution_detected',
          suggestion: 'Sugerencia sintética',
        })}
      />,
    );
    const hint = screen.getByRole('alert').querySelector('[data-part="lowercase_hint"]');
    expect(hint?.getAttribute('data-text-key')).toBe('components_forms.reason.lowercase_hint');
    expect(hint?.textContent).toBe(text('components_forms.reason.lowercase_hint'));
  });

  it.each([
    ['una sola palabra con mayúscula', 'Revisión sintética de la prensa del turno'],
    ['dos palabras separadas por una coma', 'Revisión sintética en Prensa, Lateral de la zona'],
    ['una letra suelta', 'Revisión sintética A B de la prensa'],
  ])('con %s no añade la pista', (_case, written) => {
    render(
      <LoopHarness
        initial={written}
        notice={serverNotice('loop_person_attribution_detected', {
          code: 'person_attribution_detected',
          suggestion: 'Sugerencia sintética',
        })}
      />,
    );
    expect(screen.getByRole('alert').querySelector('[data-part="lowercase_hint"]')).toBeNull();
  });

  it('la pista es solo de la atribución a persona: un término bloqueado no la lleva', () => {
    const term = 'término-sintético-580';
    const suggestion = 'alternativa sintética 580';
    render(
      <LoopHarness
        initial="Revisión sintética del TURNO ALFA en la prensa"
        notice={serverNotice('loop_blocked_vocabulary_term', {
          code: 'blocked_vocabulary_term',
          term,
          suggestion,
        })}
      />,
    );
    const alert = screen.getByRole('alert');
    expect(alert.querySelector('[data-part="message"]')?.textContent).toBe(
      label('loop_detail_code', 'loop_blocked_vocabulary_term'),
    );
    expect(alert.querySelector('[data-part="rejection-term"]')?.textContent).toBe(term);
    expect(alert.querySelector('[data-part="rejection-suggestion"]')?.textContent).toBe(suggestion);
    expect(alert.querySelector('[data-part="lowercase_hint"]')).toBeNull();
  });
});

describe('A-126: dónde se ancla un rechazo de texto', () => {
  it('al único texto escrito; con varios, al formulario', () => {
    expect(textFields({ a: VALID, b: null })).toEqual({
      loop_reason_required: 'a',
      loop_reason_too_short: 'a',
      loop_blocked_vocabulary_term: 'a',
      loop_person_attribution_detected: 'a',
    });
    expect(textFields({ a: ' ', b: VALID }).loop_blocked_vocabulary_term).toBe('b');
    expect(textFields({ a: VALID, b: VALID }).loop_person_attribution_detected).toBe('form');
  });
});

describe('límites de un campo anulable del esquema generado', () => {
  const nullable = (branch: Record<string, unknown>) => ({
    operationId: 'sintetica',
    body: {
      $ref: '#/$defs/Body',
      $defs: { Body: { properties: { note: { anyOf: [branch, { type: 'null' }] } } } },
    },
  });

  it('toma la rama de texto de anyOf (expected_outcome, verification_note)', () => {
    expect(reasonLimits(nullable({ type: 'string', minLength: 5, maxLength: 9 }), 'note')).toEqual({
      minLength: 5,
      maxLength: 9,
    });
    expect(reasonLimits(assign, 'expected_outcome')).toEqual({ minLength: 1, maxLength: 1000 });
  });

  it('sin longitudes en la rama de texto sigue siendo un error de programación', () => {
    expect(() => reasonLimits(nullable({ type: 'string' }), 'note')).toThrow(RangeError);
  });
});
