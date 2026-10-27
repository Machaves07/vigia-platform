/** IF-21: proveedores reales, API MSW y datos únicamente sintéticos. */
import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import fc from 'fast-check';
import { http, HttpResponse } from 'msw';
import { MemoryRouter } from 'react-router';
import { afterEach, describe, expect, it } from 'vitest';
import { axe } from 'vitest-axe';
import Page from '@/pages/scr10/queue';
import { createPortConnection, createTransport, installTransport } from '@/ports/base';
import {
  readDetection,
  REASON_LIMITS,
  reviewQueuePort,
  readQueue,
  type QueueItem,
  type QueuePage,
  type DetectionForReview,
} from '@/ports/scr10ReviewQueue';
import { createScreenQueryCache, ScreenQueryProvider } from '@/state/query';
import { createSessionStore, SessionProvider } from '@/state/session';
import texts from '@/texts/scr10.json';
import { text } from '@/texts';
import { label } from '@/texts/labels';
import { detectionForReview } from '../../generators';
import { apiError, exampleFor } from '../../msw/handlers';
import { server } from '../../msw/server';
import { FakeClock } from '../../support/fakeClock';
import { meResponse } from '../../support/session';
import registrations from '../../views/scr10';
import { assertNoPersonData } from '../../support/viewProperties';
import { PROPERTY_TIMEOUT_MS } from '../../support/timeouts';

const INSTANT = '2026-01-01T12:00:00.000Z';
const PLANT = '0190a1b2-0000-7000-8000-000000000001';
const ZONE = '0190a1b2-1111-7000-8000-000000000001';
const WAIT = { timeout: 10_000 };
const REASON = 'El clip muestra un reflejo sobre la guarda de la zona declarada';
const REASON_NAME = `${text('scr10.reason')} (${text('components_forms.reason.required')})`;
const dispose: (() => void)[] = [];

function required<T>(value: T | undefined | null): T {
  if (value === undefined || value === null) throw new Error('Falta un dato sintético requerido.');
  return value;
}

afterEach(() => {
  cleanup();
  dispose.splice(0).forEach((fn) => {
    fn();
  });
  installTransport(null);
});

function item(index = 1, due = '2026-01-01T11:00:00.000Z'): QueueItem {
  return {
    detection_id: `0190a1b2-2222-7000-8000-${String(index).padStart(12, '0')}`,
    plant_id: PLANT,
    zone_id: ZONE,
    family: 'coexistence',
    standard_id: '0190a1b2-3333-7000-8000-000000000001',
    standard_version: 1,
    max_confidence: 0.65,
    review_threshold: 0.5,
    publication_threshold: 0.8,
    review_reason: 'low_confidence',
    observability_state_at_event: 'observable',
    occurred_at: INSTANT,
    received_at: INSTANT,
    review_due_at: due,
    reminders_sent: 2,
  };
}

async function mount(
  permissions = ['findings.read', 'review_queue.resolve'],
  entry: string | { pathname: string; state: unknown } = '/review-queue',
) {
  const clock = new FakeClock(Date.parse(INSTANT));
  const connection = createPortConnection(clock);
  const cache = createScreenQueryCache({
    clock,
    gates: {
      isVisible: () => true,
      canReadPlatform: () => connection.canRead('platform'),
      isActive: () => true,
    },
  });
  const me = meResponse({
    permissions,
    idleExpiresAt: clock.now() + 1_800_000,
    absoluteExpiresAt: clock.now() + 28_800_000,
  });
  const session = createSessionStore({
    clock,
    port: { me: { read: () => Promise.resolve(me) }, logout: { write: () => Promise.resolve() } },
    cache,
    clearPreferences: () => undefined,
    currentRoute: () => '/review-queue',
    navigate: () => undefined,
    storages: [],
  });
  await session.load();
  installTransport(createTransport({ clock, connection, origin: 'http://localhost' }));
  server.use(
    http.get('http://localhost/hierarchy', () =>
      HttpResponse.json({
        organization: me.organization,
        nodes: [],
        plants: [
          {
            plant_id: PLANT,
            name: 'Planta sintética',
            code: 'P-SINT',
            timezone: 'America/Bogota',
            country: 'CO',
            status: 'active',
            data_region: 'co',
            zones: [
              {
                zone_id: ZONE,
                plant_id: PLANT,
                name: 'Prensas sintéticas',
                code: 'Z-SINT',
                node_id: null,
              },
            ],
          },
        ],
      }),
    ),
  );
  dispose.push(() => {
    session.dispose();
    cache.dispose();
  });
  const view = render(
    <SessionProvider store={session}>
      <ScreenQueryProvider cache={cache}>
        <MemoryRouter initialEntries={[entry]}>
          <Page />
        </MemoryRouter>
      </ScreenQueryProvider>
    </SessionProvider>,
  );
  return { ...view, clock, cache, principal: required(session.getSnapshot().principal) };
}

function queueServer(items = [item()]) {
  const calls: URL[] = [];
  const bodies: { id: string; body: unknown }[] = [];
  let current = [...items];
  server.use(
    http.get('http://localhost/loop/review-queue', ({ request }) => {
      calls.push(new URL(request.url));
      return HttpResponse.json({ items: current, next_cursor: null } satisfies QueuePage);
    }),
    http.post(
      'http://localhost/loop/review-queue/:detection_id/resolution',
      async ({ params, request }) => {
        const body = (await request.json()) as { outcome: string; reason_category: string };
        bodies.push({ id: String(params.detection_id), body });
        current = current.filter((row) => row.detection_id !== params.detection_id);
        return HttpResponse.json(
          {
            detection_id: params.detection_id,
            resolution_id: item(8).detection_id,
            record_id: item(9).detection_id,
            outcome: body.outcome,
            reason_category: body.reason_category,
            lifecycle_state: 'received',
            resolved_at: INSTANT,
          },
          { status: 201 },
        );
      },
    ),
  );
  return { calls, bodies };
}

async function loaded() {
  await screen.findByRole('table', { name: text('scr10.title') }, WAIT);
  await screen.findAllByRole('rowheader', { name: 'Prensas sintéticas' }, WAIT);
}

async function fill(outcome = 'review_discarded', category = 'reflection_or_glare') {
  const user = userEvent.setup();
  await user.click(required(screen.getAllByRole('button', { name: text('scr10.resolve') })[0]));
  await user.click(
    screen.getByRole('radio', { name: label('review_resolution_outcome', outcome) }),
  );
  await user.selectOptions(
    screen.getByRole('combobox', { name: text('scr10.category') }),
    category,
  );
  await user.type(screen.getByRole('textbox', { name: REASON_NAME }), REASON);
  return user;
}

describe('SCR-10 cola de revisión', { timeout: 40_000 }, () => {
  it('puerto fijo con lectura findings.read y escritura review_queue.resolve', () => {
    expect(reviewQueuePort.screen).toBe('SCR-10');
    expect(reviewQueuePort.operations.find((operation) => operation.name === 'list')).toEqual({
      name: 'list',
      kind: 'read',
      status: 'fixed',
      permission: 'findings.read',
      api_operation_id: 'list_review_queue_loop_review_queue_get',
    });
    expect(reviewQueuePort.operations.find((operation) => operation.name === 'resolve')).toEqual({
      name: 'resolve',
      kind: 'write',
      status: 'fixed',
      permission: 'review_queue.resolve',
      api_operation_id: 'resolve_loop_review_queue__detection_id__resolution_post',
    });
    expect(reviewQueuePort.operations.every((operation) => operation.status === 'fixed')).toBe(
      true,
    );
  });
  it(
    'RV-14: campos permitidos también con el perfil de propiedades seleccionado',
    () => {
      for (const registration of Object.values(registrations)) assertNoPersonData(registration);
    },
    PROPERTY_TIMEOUT_MS,
  );

  it('los límites del motivo coinciden con review_resolution del corpus de U-04', () => {
    const fixtures = JSON.parse(
      readFileSync(
        resolve(
          dirname(fileURLToPath(import.meta.url)),
          '../../../../backend/openapi/u05-fixtures.json',
        ),
        'utf8',
      ),
    ) as {
      free_text: { routes: { route: string; min_length: number; max_length: number }[] };
    };
    const route = required(
      fixtures.free_text.routes.find((entry) => entry.route === 'review_resolution/reason_text'),
    );
    expect(REASON_LIMITS).toEqual({ minLength: route.min_length, maxLength: route.max_length });
  });
  it.each([
    { permissions: ['findings.read'], resolver: false },
    { permissions: ['findings.read', 'review_queue.resolve'], resolver: true },
  ])('dos Principal: lectura visible y resolución $resolver', async ({ permissions, resolver }) => {
    queueServer();
    const { principal, container } = await mount(permissions);
    expect(principal.effective_permissions).toEqual(permissions);
    await loaded();
    expect(screen.queryByRole('button', { name: text('scr10.resolve') }) !== null).toBe(resolver);
    expect(await axe(container)).toHaveNoViolations();
  });

  it('árbol accesible solo con acciones individuales, sin selección, edición ni lote', async () => {
    queueServer([item(), item(2)]);
    await mount();
    await loaded();
    expect(screen.queryAllByRole('checkbox')).toHaveLength(0);
    expect(screen.queryAllByRole('listbox')).toHaveLength(0);
    expect(screen.getAllByRole('row')).toHaveLength(3);
    const allowed = new Set([
      text('scr10.resolve'),
      text('scr10.evidence'),
      text('components_status.refresh.button'),
    ]);
    for (const button of screen.getAllByRole('button'))
      expect(allowed.has(button.textContent)).toBe(true);
    expect(screen.getAllByRole('button', { name: text('scr10.resolve') })).toHaveLength(2);
  });

  it('contexto junto a confianza, orden por plazo y vencidos conservados tras refrescar', async () => {
    const early = item(1, '2025-12-01T11:00:00.000Z');
    const late = item(2, '2026-01-02T11:00:00.000Z');
    const { calls } = queueServer([late, early]);
    const { container, clock, cache } = await mount();
    await loaded();
    const rows = screen.getAllByRole('row').slice(1);
    expect(rows.map((row) => row.getAttribute('data-overdue'))).toEqual(['true', 'false']);
    const confidence = rows[0]?.querySelector('dl');
    expect(confidence?.querySelector('[data-field="max_confidence"]')?.textContent).toBe('0.65');
    expect(confidence?.querySelector('[data-field="review_threshold"]')?.textContent).toBe('0.5');
    expect(confidence?.querySelector('[data-field="publication_threshold"]')?.textContent).toBe(
      '0.8',
    );
    expect(rows[0]?.querySelector('time')?.dateTime).toBe(early.review_due_at);
    expect(within(required(rows[0])).getByText(text('scr10.overdue'))).toBeTruthy();
    await waitFor(() => {
      expect(cache.client.isFetching()).toBe(0);
    }, WAIT);
    clock.advance(60_000);
    await waitFor(() => {
      expect(calls.length).toBe(2);
    }, WAIT);
    expect(screen.getAllByRole('row')).toHaveLength(3);
    expect(container.querySelector('[data-part="zone"]')?.textContent).toBe(
      '(America/Bogota, UTC-05:00)',
    );
  });

  it('recorre todos los textos sin equiparar puntuación del modelo a certeza', () => {
    for (const value of Object.values(texts))
      expect(value).not.toMatch(/certeza|infalible|100\s*%\s*segur/iu);
    expect(texts['scr10.confidence_context']).toBe(text('scr10.confidence_context'));
  });

  it.each([
    ['review_confirmed', 'procedure_gap'],
    ['review_discarded', 'scheduled_maintenance'],
    ['review_discarded', 'reflection_or_glare'],
  ])(
    'registra %s con categoría %s para una sola detección e invalida la cola',
    async (outcome, category) => {
      const { bodies, calls } = queueServer([item(), item(2)]);
      await mount();
      await loaded();
      const user = await fill(outcome, category);
      await user.click(screen.getByRole('button', { name: text('scr10.submit') }));
      await waitFor(() => {
        expect(screen.getAllByRole('row')).toHaveLength(2);
      }, WAIT);
      expect(bodies).toEqual([
        {
          id: item().detection_id,
          body: { outcome, reason_category: category, reason_text: REASON },
        },
      ]);
      expect(calls.length).toBeGreaterThan(1);
      expect(screen.queryByRole('form', { name: text('scr10.form') })).toBeNull();
    },
  );

  it('motivo obligatorio con límites 20–1000 y categoría reiniciada al cambiar salida', async () => {
    const { bodies } = queueServer();
    await mount();
    await loaded();
    const user = await fill();
    const field = screen.getByRole<HTMLTextAreaElement>('textbox', { name: REASON_NAME });
    await user.clear(field);
    await user.click(screen.getByRole('button', { name: text('scr10.submit') }));
    expect(field.getAttribute('aria-invalid')).toBe('true');
    expect(bodies).toHaveLength(0);
    expect(REASON_LIMITS).toEqual({ minLength: 20, maxLength: 1000 });
    await user.click(
      screen.getByRole('radio', { name: label('review_resolution_outcome', 'review_confirmed') }),
    );
    expect(
      screen.getByRole<HTMLSelectElement>('combobox', { name: text('scr10.category') }).value,
    ).toBe('');
  });

  it.each([19, 20, 1000, 1001])('borde de motivo de %i caracteres', async (length) => {
    const { bodies } = queueServer();
    await mount();
    await loaded();
    const user = await fill();
    const field = screen.getByRole('textbox', { name: REASON_NAME });
    fireEvent.change(field, { target: { value: 'a'.repeat(length) } });
    await user.click(screen.getByRole('button', { name: text('scr10.submit') }));
    if (length === 19 || length === 1001) {
      expect(bodies).toHaveLength(0);
      expect(field.getAttribute('aria-invalid')).toBe('true');
    } else {
      await waitFor(() => {
        expect(bodies).toHaveLength(1);
      }, WAIT);
    }
  });

  it('rechazo del servidor anclado conserva el motivo y no reenvía la decisión', async () => {
    queueServer();
    let posts = 0;
    server.use(
      http.post('http://localhost/loop/review-queue/:id/resolution', () => {
        posts++;
        return apiError('invalid_request', 400, { detailCode: 'loop_person_attribution_detected' });
      }),
    );
    const { container, clock } = await mount();
    await loaded();
    const user = await fill();
    await user.click(screen.getByRole('button', { name: text('scr10.submit') }));
    const field = screen.getByRole<HTMLTextAreaElement>('textbox', { name: REASON_NAME });
    await waitFor(() => {
      expect(field.getAttribute('aria-invalid')).toBe('true');
    }, WAIT);
    expect(field.value).toBe(REASON);
    expect(
      container.querySelector('[data-code="invalid_request"] [data-part="message"]')?.textContent,
    ).toBe(label('loop_detail_code', 'loop_person_attribution_detected'));
    expect(await axe(container)).toHaveNoViolations();
    clock.advance(60_000);
    expect(posts).toBe(1);
  });

  it('cursor opaco: cargar más mantiene las páginas y el refresco vuelve al principio', async () => {
    const cursors: (string | null)[] = [];
    server.use(
      http.get('http://localhost/loop/review-queue', ({ request }) => {
        const cursor = new URL(request.url).searchParams.get('cursor');
        cursors.push(cursor);
        return HttpResponse.json({
          items: [item(cursor === null ? 1 : 2)],
          next_cursor: cursor === null ? 'opaque_next' : null,
        });
      }),
    );
    await mount();
    await loaded();
    const user = userEvent.setup();
    await user.click(screen.getByRole('button', { name: text('scr10.more') }));
    await waitFor(() => {
      expect(screen.getAllByRole('row')).toHaveLength(3);
    }, WAIT);
    expect(cursors).toEqual([null, null, 'opaque_next']);
  });

  it.each(['cursor repetido', 'cursor inválido'])(
    'rechaza %s sin mantener un bucle de consultas',
    async (scenario) => {
      queueServer();
      await mount();
      await loaded();
      server.use(
        http.get('http://localhost/loop/review-queue', () =>
          HttpResponse.json({
            items: [],
            next_cursor: scenario === 'cursor repetido' ? 'same_cursor' : 'bad cursor',
          }),
        ),
      );
      await expect(readQueue(3, new AbortController().signal)).rejects.toThrow(
        'Cursor de revisión inválido o repetido.',
      );
    },
  );

  it('acuse usa el rol del registro del servidor, nunca el de las asignaciones de la sesión', async () => {
    queueServer();
    server.use(
      http.get('http://localhost/ledger/records/:id', () =>
        HttpResponse.json({
          ...(exampleFor('get_record_ledger_records__record_id__get') as Record<string, unknown>),
          actor: {
            kind: 'user',
            id: item(9).detection_id,
            display_name_snapshot: 'Cuenta sintética',
            role_in_use: 'coordinator_sst',
            concession_id: null,
            unit: 'loop',
          },
        }),
      ),
    );
    const { container } = await mount();
    await loaded();
    const user = await fill();
    await user.click(screen.getByRole('button', { name: text('scr10.submit') }));
    await waitFor(() => {
      expect(container.querySelector('[data-role-in-use]')?.getAttribute('data-role-in-use')).toBe(
        'coordinator_sst',
      );
    }, WAIT);
  });

  it('evidencia solo por apertura explícita, señales etiquetadas y URL solo al reproducir', async () => {
    queueServer();
    let records = 0;
    let urls = 0;
    const { principal, cache } = await mount(['findings.read', 'evidence.read']);
    await loaded();
    const detection = required(fc.sample(detectionForReview(), 1)[0]);
    const data: DetectionForReview = {
      ...detection,
      detection_id: item().detection_id,
      organization_id: principal.organization.organization_id,
      plant_id: PLANT,
      zone_id: ZONE,
      signals: [
        {
          signal_id: item(7).detection_id,
          role: 'energy',
          value: 'asserted',
          valid: true,
          read_at: INSTANT,
        },
      ],
    };
    const clipId = required(required(data.cameras[0]).clips[0]).clip_id;
    server.use(
      http.get('http://localhost/ledger/records', ({ request }) => {
        records++;
        expect(Object.fromEntries(new URL(request.url).searchParams)).toEqual({
          record_type: 'detection_for_review',
          plant_id: PLANT,
          zone_id: ZONE,
          received_from: INSTANT,
          received_before: '2026-01-01T12:00:00.001Z',
          page_size: '50',
        });
        return HttpResponse.json({
          items: [
            {
              content: data,
              evidences: [
                { evidence_id: item(6).detection_id, clip_id: clipId },
                { evidence_id: item(5).detection_id, clip_id: item(99).detection_id },
              ],
            },
          ],
          next_cursor: null,
        });
      }),
      http.post('http://localhost/evidence/:id/read-url', () => {
        urls++;
        return HttpResponse.json({
          url: 'http://localhost/synthetic.mp4',
          expires_at: '2026-01-01T12:05:00.000Z',
        });
      }),
    );
    await waitFor(() => {
      expect(cache.client.isFetching()).toBe(0);
    }, WAIT);
    expect(records).toBe(0);
    expect(urls).toBe(0);
    const user = userEvent.setup();
    await user.click(screen.getByRole('button', { name: text('scr10.evidence') }));
    await screen.findByRole('list', { name: text('scr10.signals') }, WAIT);
    expect(records).toBe(1);
    expect(urls).toBe(0);
    expect(screen.getAllByRole('figure', { name: text('media.clip.label') })).toHaveLength(1);
    await user.click(screen.getByRole('button', { name: text('media.clip.play') }));
    await waitFor(() => {
      expect(urls).toBe(1);
    }, WAIT);
  });

  it.each(['organization_id', 'plant_id', 'zone_id', 'detection_id'] as const)(
    'rechaza evidencia con %s ajeno',
    async (field) => {
      queueServer();
      const { principal } = await mount();
      await loaded();
      const base = required(fc.sample(detectionForReview(), 1)[0]);
      const data = {
        ...base,
        detection_id: item().detection_id,
        organization_id: principal.organization.organization_id,
        plant_id: PLANT,
        zone_id: ZONE,
        [field]: item(99).detection_id,
      };
      server.use(
        http.get('http://localhost/ledger/records', () =>
          HttpResponse.json({ items: [{ content: data, evidences: [] }], next_cursor: null }),
        ),
      );
      expect(
        await readDetection(
          item(),
          principal.organization.organization_id,
          new AbortController().signal,
        ),
      ).toBeNull();
    },
  );

  it('rechaza contenido que viola el esquema en lugar de mostrar sus señales', async () => {
    queueServer();
    const { principal } = await mount();
    await loaded();
    const data = {
      ...required(fc.sample(detectionForReview(), 1)[0]),
      detection_id: item().detection_id,
      organization_id: principal.organization.organization_id,
      plant_id: PLANT,
      zone_id: ZONE,
      observed_person_name: 'dato sintético prohibido',
    };
    server.use(
      http.get('http://localhost/ledger/records', () =>
        HttpResponse.json({
          items: [{ content: data, evidences: [] }],
          next_cursor: null,
        }),
      ),
    );
    expect(
      await readDetection(
        item(),
        principal.organization.organization_id,
        new AbortController().signal,
      ),
    ).toBeNull();
  });

  it('rechaza un cursor repetido del expediente antes de seguir consultando', async () => {
    queueServer();
    const { principal } = await mount();
    await loaded();
    let reads = 0;
    server.use(
      http.get('http://localhost/ledger/records', () => {
        reads++;
        return HttpResponse.json({
          items: [],
          next_cursor:
            reads < 3 ? { received_at: INSTANT, record_id: item(90).detection_id } : null,
        });
      }),
    );
    await expect(
      readDetection(item(), principal.organization.organization_id, new AbortController().signal),
    ).rejects.toThrow('Cursor del expediente repetido.');
    expect(reads).toBe(2);
  });

  it('vacío honesto y fallo not_found con estado común', async () => {
    queueServer([]);
    await mount();
    await screen.findByText(text('scr10.empty'), {}, WAIT);
    server.use(http.get('http://localhost/loop/review-queue', () => apiError('not_found', 404)));
    await userEvent
      .setup()
      .click(screen.getByRole('button', { name: text('components_status.refresh.button') }));
    await screen.findByRole('heading', { name: text('errors.not_available_in_scope') }, WAIT);
  });

  // --- VIG-359: paneles junto a la fila, foco gestionado y detección señalada -------------------

  function panelName(kind: 'resolve' | 'evidence') {
    return `${text(`finding_journey.panel_${kind}`)}: Prensas sintéticas · ${label('predicate_family', 'coexistence')}`;
  }

  it.each(['resolve', 'evidence'] as const)(
    'VIG-359: el panel %s se abre en la fila siguiente a la suya, con el foco, y lo devuelve al cerrar',
    async (kind) => {
      queueServer([item(), item(2), item(3)]);
      server.use(
        http.get('http://localhost/ledger/records', () =>
          HttpResponse.json({ items: [], next_cursor: null }),
        ),
      );
      const { container } = await mount(['findings.read', 'review_queue.resolve']);
      await loaded();
      const user = userEvent.setup();
      const name = text(kind === 'resolve' ? 'scr10.resolve' : 'scr10.evidence');
      const trigger = required(screen.getAllByRole('button', { name })[1]);
      expect(trigger.getAttribute('aria-expanded')).toBe('false');
      await user.click(trigger);
      const panel = await screen.findByRole('region', { name: panelName(kind) }, WAIT);
      // Junto a la fila: la fila del panel es la siguiente a la de la detección, no el final.
      const row = required(trigger.closest('tr'));
      expect(row.nextElementSibling).toBe(panel.closest('tr'));
      expect(panel.closest('tr')?.nextElementSibling).not.toBeNull();
      expect(trigger.getAttribute('aria-expanded')).toBe('true');
      expect(trigger.getAttribute('aria-controls')).toBe(panel.id);
      const heading = within(panel).getByRole('heading', { name: panelName(kind) });
      await waitFor(() => {
        expect(document.activeElement).toBe(heading);
      }, WAIT);
      expect(await axe(container)).toHaveNoViolations();
      await user.click(within(panel).getByRole('button', { name: text('scr10.cancel') }));
      expect(screen.queryByRole('region', { name: panelName(kind) })).toBeNull();
      expect(document.activeElement).toBe(trigger);
      expect(trigger.getAttribute('aria-expanded')).toBe('false');
    },
  );

  it('VIG-359: la detección del estado de la navegación se señala y enfoca, y se resuelve junto a ella', async () => {
    const { bodies } = queueServer([item(), item(2), item(3)]);
    server.use(
      http.get('http://localhost/ledger/records/:id', () =>
        HttpResponse.json(
          exampleFor('get_record_ledger_records__record_id__get') as Record<string, unknown>,
        ),
      ),
    );
    await mount(undefined, {
      pathname: '/review-queue',
      state: { detection: item(2).detection_id },
    });
    await loaded();
    const current = screen
      .getAllByRole('row')
      .filter((row) => row.getAttribute('aria-current') === 'true');
    expect(current).toHaveLength(1);
    const row = required(current[0]);
    const header = within(row).getByRole('rowheader');
    await waitFor(() => {
      expect(document.activeElement).toBe(header);
    }, WAIT);
    expect(within(row).getByText(text('finding_journey.queue_target'))).toBeTruthy();
    expect(screen.queryByText(text('finding_journey.queue_target_missing'))).toBeNull();
    const user = userEvent.setup();
    await user.click(within(row).getByRole('button', { name: text('scr10.resolve') }));
    const panel = await screen.findByRole('region', { name: panelName('resolve') }, WAIT);
    expect(row.nextElementSibling).toBe(panel.closest('tr'));
    await user.click(
      within(panel).getByRole('radio', {
        name: label('review_resolution_outcome', 'review_confirmed'),
      }),
    );
    await user.selectOptions(
      within(panel).getByRole('combobox', { name: text('scr10.category') }),
      'real_coexistence',
    );
    await user.type(within(panel).getByRole('textbox', { name: REASON_NAME }), REASON);
    await user.click(within(panel).getByRole('button', { name: text('scr10.submit') }));
    await waitFor(() => {
      expect(bodies).toHaveLength(1);
    }, WAIT);
    expect(required(bodies[0]).id).toBe(item(2).detection_id);
    // Resuelta, sale de la cola: no se avisa de que «ya no está» la que se acaba de resolver.
    await waitFor(() => {
      expect(screen.getAllByRole('rowheader')).toHaveLength(2);
    }, WAIT);
    expect(screen.queryByText(text('finding_journey.queue_target_missing'))).toBeNull();
  });

  it('VIG-359: una detección señalada que ya no está en la cola lo dice sin señalar otra', async () => {
    queueServer([item(), item(2)]);
    await mount(undefined, {
      pathname: '/review-queue',
      state: { detection: item(7).detection_id },
    });
    await loaded();
    expect(
      await screen.findByText(text('finding_journey.queue_target_missing'), {}, WAIT),
    ).toBeTruthy();
    expect(
      screen.getAllByRole('row').filter((row) => row.getAttribute('aria-current') === 'true'),
    ).toHaveLength(0);
  });
});
