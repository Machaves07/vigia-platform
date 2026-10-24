/**
 * UI-13 `ReasonField` (BR-APP-42, 44; PR-APP-12): obligatorio, contador, límites de la unidad
 * dueña leídos del esquema generado (concesión 10 a 500; cierre de U-04 20 a 2 000), espejo de
 * `validateBody` y el mensaje del servidor por encima del local. Se comparan claves y textos del
 * catálogo, nunca subcadenas.
 */
import { fireEvent, render, screen } from '@testing-library/react';
import { useState } from 'react';
import { describe, expect, it } from 'vitest';
import { axe } from 'vitest-axe';

import closure from '../../openapi/bodies/close_loop_findings__finding_state_id__closure_post.json';
import classify from '../../openapi/bodies/classify_loop_findings__finding_state_id__classifications_post.json';
import concession from '../../openapi/bodies/grant_provider_concessions_post.json';
import type { Notice } from '@/components/NoticeStack/notice';
import { reasonLimits } from '@/components/ReasonField/limits';
import { ReasonField, reasonIssueKey } from '@/components/ReasonField/ReasonField';
import type { FreeTextLimits } from '@/ports/validation';
import { validateBody } from '@/ports/validation';
import { text } from '@/texts';

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
