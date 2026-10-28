import { fireEvent, screen, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { describe, expect, it } from 'vitest';
import { axe } from 'vitest-axe';
import { reviewInstant, validReviewDate } from '@/pages/scr09/loop/closure/dates';
import { text } from '@/texts';
import { apiError, exampleFor } from '../../msw/handlers';
import { server } from '../../msw/server';
import { FakeClock } from '../../support/fakeClock';
import {
  DECLARATION,
  DESCRIPTION,
  FINDING,
  LICENSE,
  START,
  USER,
  WAIT,
  ledgerRecord,
  load,
  reasonName,
  renderClosure,
} from './closure_support';

const MORNING = Date.parse('2026-10-24T13:00:00.000Z'); // 08:00 de Bogotá.
const DATE_CASES = [
  ['2026-10-24', false],
  ['2027-10-25', false],
  ['2026-10-25', true],
  ['2027-10-24', true],
] as const;
const REVIEW_CASES = [MORNING, START].flatMap((now) =>
  DATE_CASES.map(([day, accepted]) => ({ now, day, accepted })),
);

/** Oráculo de U-04: at < fecha <= add_months(at, 12), no la validación de la interfaz. */
function reviewResponse(
  body: Record<string, unknown>,
  field: string,
  now: number,
  operation: string,
) {
  const value = body[field];
  const instant = typeof value === 'string' ? Date.parse(value) : NaN;
  const upper = Date.parse(
    now === MORNING ? '2027-10-24T13:00:00.000Z' : '2027-10-25T02:00:00.000Z',
  );
  if (!(now < instant && instant <= upper)) {
    return apiError('invalid_request', 400, {
      detailCode: 'loop_unclosable_review_date_out_of_range',
    });
  }
  return HttpResponse.json({ ...(exampleFor(operation) as object) }, { status: 201 });
}

async function wizardToReview(user: Awaited<ReturnType<typeof load>>) {
  const wizard = screen.getByRole('region', { name: text('scr09.unclosable.title') });
  for (const field of ['evaluated_alternative', 'rejection_reason']) {
    fireEvent.change(
      within(wizard).getByRole('textbox', { name: reasonName(`scr09.unclosable.${field}`) }),
      { target: { value: DESCRIPTION } },
    );
    await user.click(
      within(wizard).getByRole('button', { name: text('components_structure.wizard.next') }),
    );
  }
  await user.click(
    within(wizard).getByRole('button', { name: text('components_structure.wizard.next') }),
  );
  fireEvent.change(within(wizard).getByLabelText(text('scr09.unclosable.decision_date')), {
    target: { value: '2026-10-24' },
  });
  await user.click(
    within(wizard).getByRole('button', { name: text('components_structure.wizard.next') }),
  );
  fireEvent.change(
    within(wizard).getByRole('textbox', {
      name: reasonName('scr09.unclosable.compensatory_measure'),
    }),
    { target: { value: DESCRIPTION } },
  );
  await user.click(
    within(wizard).getByRole('button', { name: text('components_structure.wizard.next') }),
  );
  return wizard;
}
describe('SCR-09 incerrable', { timeout: 40_000 }, () => {
  it.each(REVIEW_CASES)(
    'fecha de revisión $day: envío $accepted con reloj $now en Bogotá',
    async ({ day, accepted, now }) => {
      const bodies: unknown[] = [];
      const { container } = await renderClosure('unclosable', { now });
      const user = await load();
      server.use(
        http.post(`http://localhost/loop/findings/${FINDING}/unclosable`, async ({ request }) => {
          const body = (await request.json()) as Record<string, unknown>;
          bodies.push(body);
          return reviewResponse(
            body,
            'review_due_at',
            now,
            'declare_loop_findings__finding_state_id__unclosable_post',
          );
        }),
      );
      const wizard = await wizardToReview(user);
      fireEvent.change(within(wizard).getByLabelText(text('scr09.unclosable.review_due_at')), {
        target: { value: day },
      });
      await user.click(
        within(wizard).getByRole('button', { name: text('components_forms.signature.sign') }),
      );
      const next = within(wizard).getByRole<HTMLButtonElement>('button', {
        name: text('components_structure.wizard.next'),
      });
      if (accepted) {
        await user.click(next);
        await user.click(
          within(wizard).getByRole('button', { name: text('components_structure.wizard.submit') }),
        );
        await within(wizard).findByText(text('components_structure.wizard.submitted'), {}, WAIT);
        expect(bodies).toEqual([
          {
            evaluated_alternative: DESCRIPTION,
            rejection_reason: DESCRIPTION,
            compensatory_measure: DESCRIPTION,
            responsible_user_id: USER,
            decision_date: '2026-10-24',
            review_due_at: `${day}T05:00:00.000Z`,
            professional_license: LICENSE,
          },
        ]);
      } else {
        expect(next.disabled).toBe(true);
        await user.click(next);
        expect(bodies).toEqual([]);
      }
      expect(await axe(container)).toHaveNoViolations();
    },
  );

  it('el borde de doce meses ajusta el 29 de febrero y rechaza fechas imposibles', () => {
    const clock = new FakeClock(Date.parse('2028-02-29T17:00:00.000Z'));
    expect(validReviewDate('2029-02-28', clock, 'America/Bogota')).toBe(true);
    expect(validReviewDate('2029-03-01', clock, 'America/Bogota')).toBe(false);
    expect(validReviewDate('2028-02-30', clock, 'America/Bogota')).toBe(false);
    expect(validReviewDate('2026-10-24', new FakeClock(START), 'America/Bogota')).toBe(false);
  });

  it.each(['00:00', '08:00', '11:59'])(
    'el último día se envía al inicio civil también a las %s de planta',
    (time) => {
      const [hour, minute] = time.split(':').map(Number);
      const clock = new FakeClock(Date.UTC(2026, 9, 24, (hour ?? 0) + 5, minute));
      expect(reviewInstant('2027-10-24', clock, 'America/Bogota')).toBe('2027-10-24T05:00:00.000Z');
    },
  );

  it('acota el inicio civil al aniversario UTC recortado en un año bisiesto', () => {
    const clock = new FakeClock(Date.parse('2028-02-29T07:30:00.000Z'));
    expect(validReviewDate('2029-02-28', clock, 'America/Los_Angeles')).toBe(true);
    expect(reviewInstant('2029-02-28', clock, 'America/Los_Angeles')).toBe(
      '2029-02-28T07:30:00.000Z',
    );
  });

  it.each([
    'loop_unclosable_policy_not_signed',
    'loop_unclosable_policy_unavailable',
    'loop_unclosable_policy_revoked',
    'loop_transition_not_allowed',
  ])('conserva los cinco campos y muestra %s con su guía', async (code) => {
    const { container } = await renderClosure('unclosable');
    const user = await load();
    server.use(
      http.post(`http://localhost/loop/findings/${FINDING}/unclosable`, () =>
        apiError(
          code === 'loop_unclosable_policy_unavailable'
            ? 'temporarily_unavailable'
            : code === 'loop_transition_not_allowed'
              ? 'conflict'
              : 'invalid_request',
          code === 'loop_unclosable_policy_unavailable'
            ? 503
            : code === 'loop_transition_not_allowed'
              ? 409
              : 400,
          { detailCode: code },
        ),
      ),
    );
    const wizard = await wizardToReview(user);
    fireEvent.change(within(wizard).getByLabelText(text('scr09.unclosable.review_due_at')), {
      target: { value: '2026-10-25' },
    });
    await user.click(
      within(wizard).getByRole('button', { name: text('components_forms.signature.sign') }),
    );
    await user.click(
      within(wizard).getByRole('button', { name: text('components_structure.wizard.next') }),
    );
    await user.click(
      within(wizard).getByRole('button', { name: text('components_structure.wizard.submit') }),
    );
    await within(wizard).findByText(text('components_structure.wizard.submit_failed'), {}, WAIT);
    expect(
      container.querySelector(`[data-text-key="loop_detail_code.${code}"]`) ??
        container.querySelector('[data-code]'),
    ).not.toBeNull();
    expect(screen.getByText(text('scr09.unclosable.policy_guide'))).not.toBeNull();
    expect(within(wizard).getAllByText(DESCRIPTION)).toHaveLength(3);
  });

  it.each(REVIEW_CASES)(
    'renueva con fecha $day: envío $accepted con reloj $now en Bogotá',
    async ({ day, accepted, now }) => {
      const bodies: unknown[] = [];
      await renderClosure('unclosable', {
        now,
        records: [
          ledgerRecord('unclosable_state_declared', {
            declaration_id: DECLARATION,
            evaluated_alternative: DESCRIPTION,
          }),
        ],
      });
      const user = await load();
      server.use(
        http.post(
          `http://localhost/loop/unclosable/${DECLARATION}/reviews`,
          async ({ request }) => {
            const body = (await request.json()) as Record<string, unknown>;
            bodies.push(body);
            return reviewResponse(
              body,
              'new_review_due_at',
              now,
              'review_loop_unclosable__declaration_id__reviews_post',
            );
          },
        ),
      );
      await user.click(screen.getByRole('button', { name: text('scr09.unclosable.review') }));
      const review = screen.getByRole('region', { name: text('scr09.unclosable.review') });
      await user.selectOptions(
        within(review).getByLabelText(text('scr09.unclosable.declaration')),
        DECLARATION,
      );
      fireEvent.change(
        within(review).getByRole('textbox', { name: reasonName('scr09.unclosable.review_note') }),
        { target: { value: DESCRIPTION } },
      );
      fireEvent.change(within(review).getByLabelText(text('scr09.unclosable.review_due_at')), {
        target: { value: day },
      });
      await user.click(
        within(review).getByRole('button', { name: text('components_forms.signature.sign') }),
      );
      if (!accepted) {
        expect(within(review).getByRole('alert').textContent).toBe(text('scr09.closure.invalid'));
        expect(bodies).toEqual([]);
        return;
      }
      await within(review).findByText(text('scr09.closure.saved'), {}, WAIT);
      expect(bodies).toEqual([
        {
          outcome: 'renewed',
          review_note: DESCRIPTION,
          new_review_due_at: `${day}T05:00:00.000Z`,
          action: null,
          professional_license: LICENSE,
        },
      ]);
    },
  );
});

describe('SCR-09 incerrable desde la revisión destacada del lazo (VIG-363)', () => {
  function visible(name: string): boolean {
    const region = screen.getByRole('region', { name, hidden: true });
    return region.closest('[hidden]') === null;
  }

  it('con el estado de la navegación de la revisión, abre directamente la revisión', async () => {
    await renderClosure('unclosable', { entryState: { review: true } });
    await load();
    expect(visible(text('scr09.unclosable.review'))).toBe(true);
    expect(visible(text('scr09.unclosable.title'))).toBe(false);
  });

  it.each([null, { review: 'true' }, { review: false }, 'review'])(
    'sin él (%j), abre la declaración como siempre',
    async (entryState) => {
      await renderClosure('unclosable', { entryState });
      await load();
      expect(visible(text('scr09.unclosable.review'))).toBe(false);
      expect(visible(text('scr09.unclosable.title'))).toBe(true);
    },
  );
});
