/**
 * UI-19 `NoticeStack` y el acuse de escritura (BR-APP-08, 43, 75, 78; NFR-APP-41; PR-APP-22):
 * mensaje del servidor anclado al campo con lo escrito conservado, claves distintas para
 * `forbidden` y `not_found`, `correlation_id` copiable fuera de la URL, cuenta regresiva exacta
 * de `retry_after_seconds` con el `FakeClock`, y el `role_in_use` devuelto por el servidor. Se
 * comparan claves, códigos y textos exactos del catálogo, nunca subcadenas.
 */
import { act, fireEvent, render, screen, within } from '@testing-library/react';
import { useState } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { axe } from 'vitest-axe';

import platform from '../../../backend/resources/labels.platform.es.json';
import loop from '../../../backend/resources/labels.loop.es.json';
import { FakeClock } from '../support/fakeClock';
import { NoticeStack } from '@/components/NoticeStack/NoticeStack';
import { noticeForField, type Notice } from '@/components/NoticeStack/notice';
import { reasonLimits } from '@/components/ReasonField/limits';
import { ReasonField } from '@/components/ReasonField/ReasonField';
import { WriteReceipt } from '@/components/WriteReceipt/WriteReceipt';
import type { HttpError } from '@/ports/errors/map';
import { text } from '@/texts';
import { capitalized } from '@/texts/labels';

import concession from '../../openapi/bodies/grant_provider_concessions_post.json';

const LIMITS = reasonLimits(concession, 'reason');
const CORRELATION = '0190a3b2-7c4d-7e8f-9a0b-1c2d3e4f5a6b';
const WRITTEN = 'Revisión de la zona de prensas solicitada por el cliente';

let writeText: ReturnType<typeof vi.fn<(value: string) => Promise<void>>>;

beforeEach(() => {
  writeText = vi.fn<(value: string) => Promise<void>>().mockResolvedValue(undefined);
  Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true });
});

afterEach(() => {
  Reflect.deleteProperty(navigator, 'clipboard');
});

/** Formulario mínimo: un motivo, la pila de avisos y una acción ajena a los avisos. */
function Form({
  notices,
  clock,
}: {
  readonly notices: readonly Notice[];
  readonly clock: FakeClock;
}) {
  const [value, setValue] = useState(WRITTEN);
  return (
    <form aria-label="Solicitud">
      <NoticeStack
        notices={notices}
        clock={clock}
        onDismiss={() => undefined}
        onRetry={() => undefined}
      />
      <ReasonField
        label="Motivo"
        name="reason"
        value={value}
        onChange={setValue}
        limits={LIMITS}
        notice={noticeForField(notices, 'reason')}
      />
      <button type="button">Ir al panel</button>
    </form>
  );
}

function notice(error: HttpError, extra: Partial<Notice> = {}): Notice {
  return { id: `n-${error.code}`, error, correlation_id: CORRELATION, ...extra };
}

function reason(): HTMLTextAreaElement {
  return screen.getByRole('textbox', {
    name: `Motivo (${text('components_forms.reason.required')})`,
  });
}

function stack(): HTMLElement {
  return screen.getByRole('region', { name: text('components_forms.notice.region_label') });
}

function stackItems(): HTMLElement[] {
  return within(stack()).queryAllByRole('listitem');
}

describe('BR-APP-78: anclado al campo, lo escrito se conserva', () => {
  it('invalid_request con detail_code: el mensaje del servidor junto al campo', () => {
    const clock = new FakeClock();
    const { rerender } = render(<Form notices={[]} clock={clock} />);
    expect(reason().value).toBe(WRITTEN);
    const anchored = notice(
      {
        code: 'invalid_request',
        status: 422,
        detail_code: 'loop_future_rule',
        message_es: 'El motivo no cumple una regla del servidor.',
      },
      { field: 'reason' },
    );
    rerender(<Form notices={[anchored]} clock={clock} />);
    const alert = screen.getByRole('alert');
    expect(alert.getAttribute('data-code')).toBe('invalid_request');
    expect(alert.querySelector('[data-part="message"]')?.textContent).toBe(
      'El motivo no cumple una regla del servidor.',
    );
    expect(reason().getAttribute('aria-describedby')?.split(' ')).toContain(alert.id);
    expect(reason().value).toBe(WRITTEN);
    expect(stackItems()).toEqual([]);
  });

  it('un detail_code con etiqueta de la unidad dueña usa esa etiqueta', () => {
    const anchored = notice(
      { code: 'conflict', status: 409, detail_code: 'loop_signer_license_missing' },
      { field: 'reason' },
    );
    render(<Form notices={[anchored]} clock={new FakeClock()} />);
    expect(screen.getByRole('alert').querySelector('[data-part="message"]')?.textContent).toBe(
      loop.loop_detail_code.loop_signer_license_missing,
    );
    expect(reason().value).toBe(WRITTEN);
  });

  it('payload_too_large va a la pila global con el límite de 1 MB y conserva lo escrito', () => {
    render(
      <Form
        notices={[notice({ code: 'payload_too_large', status: 413 }, { field: 'reason' })]}
        clock={new FakeClock()}
      />,
    );
    const [item] = stackItems();
    expect(item?.getAttribute('data-text-key')).toBe('errors.payload_too_large');
    expect(item?.querySelector('[data-part="message"]')?.textContent).toBe(
      text('errors.payload_too_large'),
    );
    expect(reason().value).toBe(WRITTEN);
  });

  it('un aviso sin campo no se ancla aunque sea invalid_request', () => {
    render(
      <Form notices={[notice({ code: 'invalid_request', status: 422 })]} clock={new FakeClock()} />,
    );
    expect(screen.queryByRole('alert')).toBeNull();
    expect(stackItems().map((item) => item.getAttribute('data-text-key'))).toEqual([
      'errors.anchored',
    ]);
  });
});

describe('BR-APP-43: los avisos no bloquean ni ocultan acciones', () => {
  it('ningún diálogo modal y las acciones de la pantalla siguen habilitadas', () => {
    render(
      <Form
        notices={[
          notice({ code: 'forbidden', status: 403 }),
          notice({ code: 'rate_limited', status: 429, retry_after_seconds: 30 }),
        ]}
        clock={new FakeClock()}
      />,
    );
    expect(screen.queryByRole('dialog')).toBeNull();
    expect(screen.queryByRole('alertdialog')).toBeNull();
    expect(screen.getByRole<HTMLButtonElement>('button', { name: 'Ir al panel' }).disabled).toBe(
      false,
    );
    expect(reason().disabled).toBe(false);
  });
});

describe('forbidden y not_found', () => {
  it('producen claves distintas del catálogo', () => {
    render(
      <Form
        notices={[
          notice({ code: 'forbidden', status: 403 }),
          notice({ code: 'not_found', status: 404 }),
        ]}
        clock={new FakeClock()}
      />,
    );
    const keys = stackItems().map((item) => item.getAttribute('data-text-key'));
    expect(keys).toEqual(['errors.forbidden_reload', 'errors.not_available_in_scope']);
    expect(
      stackItems().map((item) => item.querySelector('[data-part="message"]')?.textContent),
    ).toEqual([text('errors.forbidden_reload'), text('errors.not_available_in_scope')]);
  });
});

describe('BR-APP-75, NFR-APP-41: correlation_id', () => {
  it('se muestra con la indicación, se copia y nunca va a la URL ni al historial', async () => {
    const before = window.location.href;
    render(
      <Form notices={[notice({ code: 'internal_error', status: 500 })]} clock={new FakeClock()} />,
    );
    const [item] = stackItems();
    if (item === undefined) throw new Error('falta el aviso');
    expect(item.querySelector('[data-part="correlation_id"]')?.textContent).toBe(CORRELATION);
    expect(item.querySelector('[data-part="correlation"] > span')?.textContent).toBe(
      `${text('components_forms.notice.correlation_label')}: `,
    );
    await act(async () => {
      fireEvent.click(
        within(item).getByRole('button', { name: text('components_forms.notice.copy') }),
      );
      await Promise.resolve();
    });
    expect(writeText).toHaveBeenCalledExactlyOnceWith(CORRELATION);
    expect(within(item).getByRole('status').textContent).toBe(
      text('components_forms.notice.copied'),
    );
    expect(window.location.href).toBe(before);
    expect(JSON.stringify(window.history.state)).not.toContain(CORRELATION);
    expect(window.location.href.includes(CORRELATION)).toBe(false);
  });

  it('se lee del cuerpo de error si no llega aparte, y un valor que no es UUID no se muestra', () => {
    const { rerender } = render(
      <NoticeStack
        clock={new FakeClock()}
        onDismiss={() => undefined}
        notices={[
          {
            id: 'a',
            error: { code: 'internal_error', status: 500, body: { correlation_id: CORRELATION } },
          },
        ]}
      />,
    );
    expect(document.querySelector('[data-part="correlation_id"]')?.textContent).toBe(CORRELATION);
    rerender(
      <NoticeStack
        clock={new FakeClock()}
        onDismiss={() => undefined}
        notices={[
          { id: 'b', error: { code: 'internal_error', status: 500 }, correlation_id: '<b>x</b>' },
        ]}
      />,
    );
    expect(document.querySelector('[data-part="correlation"]')).toBeNull();
  });
});

describe('cuenta regresiva de retry_after_seconds (FakeClock)', () => {
  function retry(): HTMLButtonElement {
    return screen.getByRole('button', { name: text('components_forms.notice.retry') });
  }
  function countdown(): string | null {
    return document.querySelector('[data-part="countdown"]')?.textContent ?? null;
  }
  const remaining = (seconds: number) =>
    `${text('components_forms.notice.retry_in')} ${String(seconds)} ${text('components_forms.notice.seconds')}`;

  it('deshabilita «Repetir» exactamente 3 s y lo rehabilita sola', () => {
    const clock = new FakeClock(1_000_000);
    const onRetry = vi.fn();
    render(
      <NoticeStack
        clock={clock}
        onDismiss={() => undefined}
        onRetry={onRetry}
        notices={[
          { id: 'r', error: { code: 'rate_limited', status: 429, retry_after_seconds: 3 } },
        ]}
      />,
    );
    expect(retry().disabled).toBe(true);
    expect(countdown()).toBe(remaining(3));
    act(() => {
      clock.advance(1000);
    });
    expect(countdown()).toBe(remaining(2));
    act(() => {
      clock.advance(1999);
    });
    expect(retry().disabled).toBe(true);
    expect(countdown()).toBe(remaining(1));
    act(() => {
      clock.advance(1);
    });
    expect(retry().disabled).toBe(false);
    expect(countdown()).toBeNull();
    fireEvent.click(retry());
    expect(onRetry).toHaveBeenCalledExactlyOnceWith('r');
    expect(clock.pendingCount).toBe(0);
  });

  it('sin retry_after_seconds no impone espera propia', () => {
    render(
      <NoticeStack
        clock={new FakeClock()}
        onDismiss={() => undefined}
        onRetry={() => undefined}
        notices={[{ id: 't', error: { code: 'temporarily_unavailable', status: 503 } }]}
      />,
    );
    expect(retry().disabled).toBe(false);
    expect(countdown()).toBeNull();
  });

  it('«Cerrar aviso» llama a onDismiss con su id', () => {
    const onDismiss = vi.fn();
    render(
      <NoticeStack
        clock={new FakeClock()}
        onDismiss={onDismiss}
        notices={[{ id: 'd', error: { code: 'not_found', status: 404 } }]}
      />,
    );
    fireEvent.click(screen.getByRole('button', { name: text('components_forms.notice.dismiss') }));
    expect(onDismiss).toHaveBeenCalledExactlyOnceWith('d');
  });
});

describe('BR-APP-08: acuse con el role_in_use del servidor', () => {
  it.each(Object.entries(platform.role))('%s se muestra con su etiqueta', (role, label) => {
    const { container } = render(<WriteReceipt record="Concesión CON-0012" roleInUse={role} />);
    const status = screen.getByRole('status');
    expect(status.querySelector('[data-part="record"]')?.textContent).toBe('Concesión CON-0012');
    expect(status.querySelector('[data-part="role_in_use"]')?.textContent).toBe(capitalized(label));
    expect(
      container.querySelectorAll('input, select, textarea, [role="combobox"], [role="radio"]'),
    ).toHaveLength(0);
  });

  it('un rol desconocido nunca se muestra crudo; sin rol, el texto propio', () => {
    const { rerender } = render(<WriteReceipt record="R" roleInUse="toString" />);
    expect(document.querySelector('[data-part="role_in_use"]')?.textContent).toBe(
      text('components_forms.receipt.role_unknown'),
    );
    rerender(<WriteReceipt record="R" roleInUse={null} />);
    expect(document.querySelector('[data-part="role_in_use"]')?.textContent).toBe(
      text('components_forms.receipt.role_none'),
    );
  });
});

describe('axe', () => {
  it.each([
    [
      'NoticeStack con avisos globales, anclado y cuenta regresiva',
      <Form
        key="f"
        clock={new FakeClock()}
        notices={[
          notice({ code: 'internal_error', status: 500 }),
          notice({ code: 'rate_limited', status: 429, retry_after_seconds: 5 }),
          notice(
            { code: 'invalid_request', status: 422, message_es: 'Revise el motivo.' },
            { field: 'reason' },
          ),
        ]}
      />,
    ],
    [
      'WriteReceipt',
      <WriteReceipt key="w" record="Concesión CON-0012" roleInUse="administrator" />,
    ],
  ])('%s', async (_name, ui) => {
    const { container } = render(<main>{ui}</main>);
    const results = await axe(container);
    const serious = results.violations.filter(
      (violation) => violation.impact === 'serious' || violation.impact === 'critical',
    );
    expect(serious).toEqual([]);
    expect(results).toHaveNoViolations();
  });
});
