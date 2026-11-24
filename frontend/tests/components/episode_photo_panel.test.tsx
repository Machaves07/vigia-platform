/**
 * La foto del hallazgo con el aspecto de SCR-06 (TASK-590; A-128; UI-26; BR-APP-58, 77): el panel
 * compartido en SCR-06 (`commissioning`) y en la clasificación, el detalle y la ficha de vida
 * (`finding`), con MSW, el transporte real y JPEG **sintéticos** generados aquí (nunca una foto
 * real, P3).
 *
 * MSW cuenta cada petición de imagen (cada una es una lectura auditada): la clasificación pide una
 * al montar, el detalle la del registro ancla, la ficha ninguna hasta «Ver foto», y el doble
 * montaje de `StrictMode` no repite ninguna. Las aserciones van por `data-part`, `data-text-key` y
 * textos exactos del catálogo; nunca por subcadena.
 *
 * Mutaciones con nombre que estas pruebas detectan (ver el PR): `m_aviso_de_puesta_en_marcha_en_hallazgo`,
 * `m_pide_al_montar_en_la_ficha`, `m_pide_toda_la_rafaga`, `m_img_src`, `m_sin_permiso_pide`,
 * `m_not_found_saca_de_la_pantalla`.
 */
import { act, fireEvent, render, screen, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { StrictMode, type ReactElement } from 'react';
import { MemoryRouter } from 'react-router';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { axe } from 'vitest-axe';

import { BlurredEpisodePhoto } from '@/components/BlurredEpisodePhoto/BlurredEpisodePhoto';
import {
  EpisodePhotoPanel,
  type PanelPhoto,
} from '@/components/EpisodePhotoPanel/EpisodePhotoPanel';
import { DetailView } from '@/pages/scr08/detail/DetailView';
import { TimelineView } from '@/pages/scr08/timeline/TimelineView';
import { FindingEvidence } from '@/pages/scr09/classify/Evidence';
import { createPortConnection, createTransport, installTransport } from '@/ports/base';
import type { FindingPhoto } from '@/ports/findingPhotos';
import type { FindingDetail } from '@/ports/scr08Detail';
import type { FindingTimeline, TimelineMilestone } from '@/ports/scr08Timeline';
import { label } from '@/texts/labels';
import { text } from '@/texts';

import { apiError, mswPath } from '../msw/handlers';
import { server } from '../msw/server';
import { FakeClock } from '../support/fakeClock';
import { detail as detailFixture } from './scr08/detail_support';

const WAIT = { timeout: 10_000 };
const TEST_TIMEOUT = 30_000;
const TIME_ZONE = 'America/Bogota';
const FINDING = '0190a1b2-2222-7000-8000-000000000001';
const STANDARD = '0190a1b2-3333-7000-8000-000000000001';
const CAMERA = '0190a1b2-0000-7000-8000-000000000590';
const NOW = Date.parse('2026-11-23T15:00:00.000Z');
const START = Date.parse('2026-11-23T13:40:00.000Z');
const IMAGE_PATH = '/loop/findings/{finding_state_id}/photos/{photo_id}/image';

function photoId(index: number): string {
  return `0190a1b2-5900-7000-8000-${String(index).padStart(12, '0')}`;
}

/** Foto sintética del hallazgo: la `0` es el cuadro de mayor confianza; las demás, ráfaga cada 3 s. */
function photo(index: number, overrides: Partial<FindingPhoto> = {}): FindingPhoto {
  return {
    photo_id: photoId(index),
    photo_index: index,
    photo_role: index === 0 ? 'peak' : 'burst',
    classified_as: 'finding',
    camera_id: CAMERA,
    frame_at: new Date(START + 2_000 + index * 3_000).toISOString(),
    family: 'dwell',
    standard: { standard_id: STANDARD, version: 3 },
    max_confidence: 0.91,
    started_at: new Date(START).toISOString(),
    ended_at: new Date(START + 64_000).toISOString(),
    episode_duration_ms: 64_000,
    received_at: new Date(START + 65_000).toISOString(),
    expires_at: null,
    has_image: true,
    image_absent_reason: null,
    width: 64,
    height: 48,
    ...overrides,
  };
}

function burst(count: number): FindingPhoto[] {
  // Llegan desordenadas: la navegación va por `photo_index`.
  return Array.from({ length: count }, (_, index) => photo(index)).reverse();
}

type DetailRecord = FindingDetail['records'][number];

function findingDetail(
  photosBySegment: readonly (readonly FindingPhoto[])[],
  changes: Partial<FindingDetail> = {},
): FindingDetail {
  const base = detailFixture();
  const [template] = base.records;
  if (template === undefined) throw new Error('Sin registro sintético.');
  const records: DetailRecord[] = photosBySegment.map((photos, segment) => ({
    ...template,
    record_id: `0190a1b2-4444-7000-8000-${String(segment + 1).padStart(12, '0')}`,
    chain_sequence: 7 + segment,
    segment_index: segment,
    clips: [],
    photos: [...photos],
  }));
  // El ancla es el primer tramo aunque llegue después (orden por tramo).
  return { ...base, finding_state_id: FINDING, records: records.reverse(), ...changes };
}

const NO_DETAILS: TimelineMilestone['details'] = {
  outcome: null,
  reason_category: null,
  reason_text: null,
  action_type: null,
  description: null,
  expected_outcome: null,
  assignee_display_name: null,
  due_at: null,
  performed_at: null,
  performed_by_role: null,
  verification_note: null,
  days_overdue: null,
  escalation_level: null,
  professional_license: null,
  root_cause_category: null,
  root_cause_statement: null,
  conclusion: null,
  from_state: null,
  compensatory_measure: null,
  review_due_at: null,
  review_note: null,
};

function milestone(
  index: number,
  kind: TimelineMilestone['kind'],
  photos: readonly FindingPhoto[],
): TimelineMilestone {
  return {
    kind,
    record_type: kind,
    record_id: `0190a1b2-7777-7000-8000-${String(index).padStart(12, '0')}`,
    chain_sequence: 100 + index,
    at: new Date(START + index * 60_000).toISOString(),
    elapsed_ms_since_previous: null,
    actor: null,
    details: NO_DETAILS,
    deadline: null,
    photos: [...photos],
    process: [],
  };
}

function timeline(milestones: TimelineMilestone[]): FindingTimeline {
  return {
    finding_state_id: FINDING,
    subject_kind: 'finding',
    plant_id: '0190a1b2-0000-7000-8000-000000000001',
    zone_id: '0190a1b2-1111-7000-8000-000000000001',
    family: 'dwell',
    lifecycle_state: 'open',
    time_aggregated: false,
    aggregation_window_minutes: null,
    truncated: false,
    summary: {
      received_at: new Date(START).toISOString(),
      closed_at: null,
      business_days_to_close: null,
      classification_met: null,
      action_met: null,
    },
    milestones,
  };
}

/** JPEG sintético mínimo (SOI + EOI): nunca una foto real (P3). */
function syntheticJpeg(): Uint8Array<ArrayBuffer> {
  return new Uint8Array([0xff, 0xd8, 0xff, 0xd9]);
}

interface Traffic {
  readonly image: { finding: string; photo: string }[];
}

function serve(image?: () => Response): Traffic {
  const traffic: Traffic = { image: [] };
  server.use(
    http.get(mswPath(IMAGE_PATH), ({ params }) => {
      traffic.image.push({
        finding: String(params.finding_state_id),
        photo: String(params.photo_id),
      });
      if (image !== undefined) return image();
      return new HttpResponse(syntheticJpeg(), {
        headers: { 'content-type': 'image/jpeg', 'cache-control': 'private, no-store' },
      });
    }),
  );
  return traffic;
}

const drawImage = vi.fn();
let clock: FakeClock;

beforeEach(() => {
  drawImage.mockReset();
  clock = new FakeClock(NOW);
  installTransport(
    createTransport({
      clock,
      connection: createPortConnection(clock),
      devLog: () => undefined,
    }),
  );
  vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockImplementation(
    () => ({ drawImage }) as unknown as CanvasRenderingContext2D,
  );
  // jsdom no decodifica imágenes: el mapa de bits sintético tiene el tamaño declarado.
  vi.stubGlobal(
    'createImageBitmap',
    vi.fn(() => Promise.resolve({ width: 64, height: 48, close: vi.fn() })),
  );
});

afterEach(() => {
  installTransport(null);
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

function mount(element: ReactElement) {
  return render(
    <StrictMode>
      <MemoryRouter>{element}</MemoryRouter>
    </StrictMode>,
  );
}

async function settle(): Promise<void> {
  await act(async () => {
    for (let round = 0; round < 8; round += 1) await Promise.resolve();
  });
}

function classify(detail: FindingDetail, canView = true) {
  return mount(
    <FindingEvidence detail={detail} zone={null} timeZone={TIME_ZONE} canViewClips={canView} />,
  );
}

function openDetail(detail: FindingDetail, canView = true) {
  return mount(
    <DetailView
      detail={detail}
      zone={null}
      timeZone={TIME_ZONE}
      now={{ kind: 'hidden' }}
      records={{ kind: 'ready', records: [] }}
      clock={clock}
      canViewClips={canView}
      rootCauseHref={null}
    />,
  );
}

function openTimeline(value: FindingTimeline, canView = true) {
  return mount(
    <TimelineView timeline={value} timeZone={TIME_ZONE} canViewPhoto={canView} clock={clock} />,
  );
}

const ALT_PEAK = () => text('episode_photo_panel.finding.alt_peak');
const SHOW = () => text('episode_photo_panel.finding.show');

function blocks(container: HTMLElement): HTMLElement[] {
  return [...container.querySelectorAll<HTMLElement>('[data-part="finding_photo"]')];
}

function fail(): never {
  throw new Error('Falta el elemento esperado.');
}

function must<T>(value: T | null | undefined): T {
  return value ?? fail();
}

/** El primer bloque de la foto del hallazgo. */
function first(container: HTMLElement): HTMLElement {
  return blocks(container)[0] ?? fail();
}

function parts(panel: Element): (string | null)[] {
  return [...panel.querySelectorAll('[data-part^="fact_"]')].map((row) =>
    row.getAttribute('data-part'),
  );
}

function keys(panel: Element): (string | null)[] {
  return [...panel.querySelectorAll('[data-part^="fact_"] > dt')].map((name) =>
    name.getAttribute('data-text-key'),
  );
}

function byKey(root: ParentNode, key: string): Element | null {
  return root.querySelector(`[data-text-key="${key}"]`);
}

function noticeCodes(root: ParentNode): (string | null)[] {
  return [...root.querySelectorAll('li[data-code]')].map((item) => item.getAttribute('data-code'));
}

const FINDING_ROWS = [
  'fact_frame_at',
  'fact_camera',
  'fact_classified_as',
  'fact_family',
  'fact_standard',
  'fact_max_confidence',
  'fact_segment',
  'fact_started_at',
  'fact_ended_at',
  'fact_duration',
  'fact_received_at',
  'fact_photo',
];

describe('panel compartido', { timeout: TEST_TIMEOUT }, () => {
  function commissioningPhoto(): PanelPhoto {
    return { ...photo(0), expires_at: new Date(START + 30 * 86_400_000).toISOString() };
  }

  it('SCR-06 (commissioning): las 13 filas de siempre, en el mismo orden y con sus textos', () => {
    const view = render(
      <EpisodePhotoPanel
        mode="commissioning"
        photo={commissioningPhoto()}
        segment={1}
        durationMs={4_000}
        instant={(value) => <time dateTime={value}>{value}</time>}
        media={<p>foto</p>}
      />,
    );
    const panel = view.container.querySelector('[data-part="episode_photo_panel"]');
    expect(panel).not.toBeNull();
    expect(keys(must(panel))).toEqual([
      'episode_photo_panel.frame_at',
      'episode_photo_panel.camera',
      'episode_photo_panel.would_have_been',
      'episode_photo_panel.family',
      'episode_photo_panel.standard',
      'episode_photo_panel.max_confidence',
      'episode_photo_panel.segment',
      'episode_photo_panel.started_at',
      'episode_photo_panel.ended_at',
      'episode_photo_panel.duration',
      'episode_photo_panel.received_at',
      'episode_photo_panel.expires_at',
      'episode_photo_panel.photo',
    ]);
    // Los mismos textos que tenía SCR-06 antes de extraer el panel.
    expect([...view.container.querySelectorAll('dt')].map((name) => name.textContent)).toEqual([
      'Hora del cuadro',
      'Cámara',
      'Habría sido',
      'Familia',
      'Estándar',
      'Confianza máxima',
      'Tramo del episodio',
      'Inicio del tramo',
      'Fin del tramo',
      'Duración',
      'Recibido en la plataforma',
      'Vencimiento',
      'Foto',
    ]);
    expect(screen.getByRole('region', { name: text('episode_photo_panel.facts') })).toBeDefined();
  });

  it('finding: «Clasificado como» y «Se conserva hasta» solo si la foto vence', () => {
    const props = {
      mode: 'finding' as const,
      segment: 1,
      durationMs: 64_000,
      instant: (value: string) => <time dateTime={value}>{value}</time>,
      media: <p>foto</p>,
    };
    const forever = render(<EpisodePhotoPanel {...props} photo={photo(0)} />);
    expect(parts(forever.container)).toEqual(FINDING_ROWS);
    expect(
      within(forever.container).getByText(label('record_kind', 'finding'), {
        selector: '[data-part="fact_classified_as"] dd',
      }),
    ).toBeDefined();
    forever.unmount();
    const expiring = render(
      <EpisodePhotoPanel
        {...props}
        photo={photo(0, { expires_at: new Date(START + 86_400_000).toISOString() })}
      />,
    );
    expect(parts(expiring.container)).toEqual([
      ...FINDING_ROWS.slice(0, -1),
      'fact_kept_until',
      'fact_photo',
    ]);
  });

  it('UI-26 en el hallazgo: sin el aviso de puesta en marcha y con la superposición', async () => {
    const requestImage = vi.fn(() =>
      Promise.resolve(new Blob([syntheticJpeg()], { type: 'image/jpeg' })),
    );
    render(
      <BlurredEpisodePhoto
        photoId={photoId(0)}
        hasImage
        absentReason={null}
        alt={ALT_PEAK()}
        notice="finding"
        requestImage={requestImage}
      />,
    );
    await screen.findByRole('img', { name: ALT_PEAK() }, WAIT);
    expect(byKey(document, 'media.episode_photo.commissioning_notice')).toBeNull();
    const overlay = byKey(document, 'media.episode_photo.blurred_automatically');
    expect(overlay?.textContent).toBe(text('media.episode_photo.blurred_automatically'));
  });
});

describe('clasificación (SCR-09)', { timeout: TEST_TIMEOUT }, () => {
  it('el panel arriba con la foto 0 del ancla: una petición al montar y ninguna más', async () => {
    const traffic = serve();
    const view = classify(findingDetail([[photo(0)], [photo(7)]]));
    const canvas = await screen.findByRole('img', { name: ALT_PEAK() }, WAIT);
    expect(canvas.tagName).toBe('CANVAS');
    await settle();
    expect(traffic.image).toEqual([{ finding: FINDING, photo: photoId(0) }]);
    const block = first(view.container);
    expect(block).toBeDefined();
    // Arriba: antes del resumen del hallazgo.
    const summary = view.container.querySelector('[data-part="finding_evidence"]');
    expect(
      block.compareDocumentPosition(summary as Node) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
    expect(parts(block)).toEqual(FINDING_ROWS);
    // Sin aviso de puesta en marcha (m_aviso_de_puesta_en_marcha_en_hallazgo).
    expect(byKey(view.container, 'media.episode_photo.commissioning_notice')).toBeNull();
    expect(byKey(view.container, 'media.episode_photo.blurred_automatically')).not.toBeNull();
    // Enfocar la ventana no vuelve a pedirla.
    act(() => {
      window.dispatchEvent(new Event('focus'));
      document.dispatchEvent(new Event('visibilitychange'));
    });
    await settle();
    expect(traffic.image).toHaveLength(1);
  });

  it('con ráfaga, solo la foto 0 al montar; las demás con el gesto (m_pide_toda_la_rafaga)', async () => {
    const traffic = serve();
    const view = classify(findingDetail([burst(3)]));
    await screen.findByRole('img', { name: ALT_PEAK() }, WAIT);
    await settle();
    expect(traffic.image.map((entry) => entry.photo)).toEqual([photoId(0)]);
    const block = first(view.container);
    const navigation = within(block).getByRole('group', {
      name: text('episode_photo_panel.finding.navigation'),
    });
    expect(navigation.querySelector('[data-part="photo_count"]')?.textContent).toBe(
      `${text('episode_photo_panel.finding.position')} 1 ${text('episode_photo_panel.finding.of')} 3`,
    );
    fireEvent.click(
      within(navigation).getByRole('button', { name: text('episode_photo_panel.finding.next') }),
    );
    await screen.findByRole('img', { name: text('episode_photo_panel.finding.alt_burst') }, WAIT);
    await settle();
    expect(traffic.image.map((entry) => entry.photo)).toEqual([photoId(0), photoId(1)]);
  });

  it('sin evidence.read: el aviso del rol y ninguna petición (m_sin_permiso_pide)', async () => {
    const traffic = serve();
    const view = classify(findingDetail([[photo(0)]]), false);
    const block = first(view.container);
    const notice = byKey(block, 'episode_photo_panel.finding.no_permission');
    expect(notice?.textContent).toBe(text('episode_photo_panel.finding.no_permission'));
    // Los metadatos sí (findings.read).
    expect(parts(block)).toEqual(FINDING_ROWS);
    await settle();
    expect(traffic.image).toEqual([]);
    expect(view.container.querySelector('canvas')).toBeNull();
  });

  it('sin <img>, sin blob:, sin descarga, sin arrastre y sin menú contextual (m_img_src)', async () => {
    serve();
    const view = classify(findingDetail([[photo(0)]]));
    const canvas = await screen.findByRole('img', { name: ALT_PEAK() }, WAIT);
    expect(view.container.querySelectorAll('img')).toHaveLength(0);
    expect(view.container.innerHTML.includes('blob:')).toBe(false);
    expect(view.container.querySelectorAll('[download]')).toHaveLength(0);
    const block = first(view.container);
    expect(block.querySelectorAll('a')).toHaveLength(0);
    expect(canvas.getAttribute('draggable')).toBe('false');
    const frame = block.querySelector('[data-part="photo_frame"]');
    expect(fireEvent.contextMenu(must(frame))).toBe(false);
  });

  it('sin fotos en el registro: el aviso en el bloque y ninguna petición', async () => {
    const traffic = serve();
    const view = classify(findingDetail([[]]));
    const block = first(view.container);
    expect(byKey(block, 'episode_photo_panel.finding.none')?.textContent).toBe(
      text('episode_photo_panel.finding.none'),
    );
    await settle();
    expect(traffic.image).toEqual([]);
  });

  it('foto sin imagen: el motivo etiquetado y ninguna petición', async () => {
    const traffic = serve();
    classify(
      findingDetail([
        [photo(0, { has_image: false, image_absent_reason: 'disk_pressure', frame_at: null })],
      ]),
    );
    expect(
      await screen.findByText(
        label('image_absent_reason', 'disk_pressure'),
        { selector: 'figure [data-part="absent_reason"]' },
        WAIT,
      ),
    ).toBeDefined();
    await settle();
    expect(traffic.image).toEqual([]);
  });

  it('not_found de la imagen: «La foto ya no está disponible» y el hallazgo sigue (m_not_found_saca_de_la_pantalla)', async () => {
    const traffic = serve(() => apiError('not_found', 404));
    const view = classify(findingDetail([[photo(0)]]));
    const gone = await screen.findByText(text('episode_photo_panel.finding.gone'), {}, WAIT);
    expect(gone.getAttribute('data-text-key')).toBe('episode_photo_panel.finding.gone');
    expect(view.container.querySelector('[data-part="finding_evidence"]')).not.toBeNull();
    expect(view.container.querySelector('[data-part="episode_signals"]')).not.toBeNull();
    expect(
      screen.queryByRole('heading', { level: 1, name: text('errors.not_available_in_scope') }),
    ).toBeNull();
    expect(traffic.image).toHaveLength(1);
  });

  it('error transitorio: la pila de UI-19 y «Volver a pedir la imagen» como gesto', async () => {
    let fail = true;
    const traffic = serve(() =>
      fail
        ? apiError('temporarily_unavailable', 503, { retryAfterSeconds: 2 })
        : new HttpResponse(syntheticJpeg(), { headers: { 'content-type': 'image/jpeg' } }),
    );
    const view = classify(findingDetail([[photo(0)]]));
    const again = await screen.findByRole(
      'button',
      { name: text('media.episode_photo.request_again') },
      WAIT,
    );
    const block = first(view.container);
    expect(noticeCodes(block)).toEqual(['temporarily_unavailable']);
    // Sin reintentos automáticos.
    for (let second = 0; second < 20; second += 1) {
      act(() => {
        clock.advance(1_000);
      });
      await settle();
    }
    expect(traffic.image).toHaveLength(1);
    fail = false;
    fireEvent.click(again);
    await screen.findByRole('img', { name: ALT_PEAK() }, WAIT);
    expect(traffic.image).toHaveLength(2);
  });

  it('fase 2: 21 fotos (0 peak y 20 burst cada 3 s), una petición por foto vista', async () => {
    const traffic = serve();
    const view = classify(findingDetail([burst(21)]));
    await screen.findByRole('img', { name: ALT_PEAK() }, WAIT);
    const block = first(view.container);
    const navigation = within(block).getByRole('group', {
      name: text('episode_photo_panel.finding.navigation'),
    });
    const count = () => navigation.querySelector('[data-part="photo_count"]')?.textContent;
    const time = () => navigation.querySelector('[data-part="photo_time"]')?.textContent;
    const role = () =>
      navigation.querySelector('[data-part="photo_role"]')?.getAttribute('data-text-key');
    const of = text('episode_photo_panel.finding.of');
    const position = text('episode_photo_panel.finding.position');
    expect(count()).toBe(`${position} 1 ${of} 21`);
    // 13:40:02 UTC es 08:40:02 en Bogotá (UTC−05:00).
    expect(time()).toBe('08:40:02');
    expect(role()).toBe('episode_photo_panel.finding.role_peak');
    const previous = within(navigation).getByRole('button', {
      name: text('episode_photo_panel.finding.previous'),
    });
    expect((previous as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(
      within(navigation).getByRole('button', { name: text('episode_photo_panel.finding.next') }),
    );
    expect(count()).toBe(`${position} 2 ${of} 21`);
    expect(time()).toBe('08:40:05');
    expect(role()).toBe('episode_photo_panel.finding.role_burst');
    await settle();
    expect(traffic.image.map((entry) => entry.photo)).toEqual([photoId(0), photoId(1)]);
  });

  it('con una sola foto no hay navegación', async () => {
    serve();
    const view = classify(findingDetail([[photo(0)]]));
    await screen.findByRole('img', { name: ALT_PEAK() }, WAIT);
    expect(view.container.querySelector('[data-part="photo_navigation"]')).toBeNull();
  });

  it('zona unipersonal: las horas con la marca de agregada y la navegación sin hora', async () => {
    serve();
    const view = classify(findingDetail([burst(2)], { time_aggregated: true }));
    await screen.findByRole('img', { name: ALT_PEAK() }, WAIT);
    const block = first(view.container);
    expect(block.querySelector('[data-part="photo_time"]')).toBeNull();
    const marked = block.querySelectorAll(
      '[data-part="fact_received_at"] [data-text-key="episode_photo_panel.aggregated"]',
    );
    expect(marked).toHaveLength(1);
  });

  it('axe sin violaciones', async () => {
    serve();
    const view = classify(findingDetail([burst(2)]));
    await screen.findByRole('img', { name: ALT_PEAK() }, WAIT);
    expect(await axe(view.container)).toHaveNoViolations();
  });
});

describe('detalle (SCR-08)', { timeout: TEST_TIMEOUT }, () => {
  it('el ancla pide al montar; los demás tramos, solo con «Ver foto»', async () => {
    const traffic = serve();
    const view = openDetail(findingDetail([[photo(0)], [photo(0, { photo_id: photoId(50) })]]));
    await screen.findByRole('img', { name: ALT_PEAK() }, WAIT);
    await settle();
    expect(traffic.image.map((entry) => entry.photo)).toEqual([photoId(0)]);
    const found = blocks(view.container);
    expect(found).toHaveLength(2);
    for (const block of found) expect(parts(block)).toEqual(FINDING_ROWS);
    // Los registros llegan en orden inverso: el ancla (tramo 1) es el segundo de la lista.
    const later = found[0] ?? fail();
    fireEvent.click(within(later).getByRole('button', { name: SHOW() }));
    await settle();
    await screen.findAllByRole('img', { name: ALT_PEAK() }, WAIT);
    expect(traffic.image.map((entry) => entry.photo)).toEqual([photoId(0), photoId(50)]);
    expect(byKey(view.container, 'media.episode_photo.commissioning_notice')).toBeNull();
  });

  it('el tramo se numera como el registro («Registro 2» → 2)', () => {
    serve();
    const view = openDetail(findingDetail([[photo(0)], [photo(0, { photo_id: photoId(50) })]]));
    const values = blocks(view.container).map(
      (block) => block.querySelector('[data-part="fact_segment"] dd')?.textContent,
    );
    expect(values).toEqual(['2', '1']);
  });

  it('axe sin violaciones', async () => {
    serve();
    const view = openDetail(findingDetail([[photo(0)], [photo(0, { photo_id: photoId(50) })]]));
    await screen.findByRole('img', { name: ALT_PEAK() }, WAIT);
    expect(await axe(view.container)).toHaveNoViolations();
  });
});

describe('ficha de vida (SCR-08)', { timeout: TEST_TIMEOUT }, () => {
  function cycle(): FindingTimeline {
    return timeline([
      milestone(1, 'finding_received', [photo(0)]),
      milestone(2, 'late_segment_attached', [photo(0, { photo_id: photoId(60) })]),
      milestone(3, 'classification', []),
    ]);
  }

  it('ninguna petición al montar y una por «Ver foto» (m_pide_al_montar_en_la_ficha)', async () => {
    const traffic = serve();
    const view = openTimeline(cycle());
    await settle();
    expect(traffic.image).toEqual([]);
    const found = blocks(view.container);
    // «Hallazgo recibido» y el tramo tardío; la clasificación no lleva foto.
    expect(found).toHaveLength(2);
    for (const block of found) {
      expect(block.querySelector('[data-part="episode_photo_panel"]')).toBeNull();
      const show = within(block).getByRole('button', { name: SHOW() });
      expect(show.getAttribute('aria-expanded')).toBe('false');
    }
    const received = found[0] ?? fail();
    fireEvent.click(within(received).getByRole('button', { name: SHOW() }));
    await screen.findByRole('img', { name: ALT_PEAK() }, WAIT);
    await settle();
    expect(traffic.image.map((entry) => entry.photo)).toEqual([photoId(0)]);
    expect(parts(received)).toEqual(FINDING_ROWS);
  });

  it('sin evidence.read: al abrir, el aviso del rol y ninguna petición', async () => {
    const traffic = serve();
    const view = openTimeline(cycle(), false);
    const received = first(view.container);
    fireEvent.click(within(received).getByRole('button', { name: SHOW() }));
    expect(byKey(received, 'episode_photo_panel.finding.no_permission')).not.toBeNull();
    await settle();
    expect(traffic.image).toEqual([]);
  });

  it('«Hallazgo recibido» sin fotos: el aviso, sin botón', () => {
    serve();
    const view = openTimeline(timeline([milestone(1, 'finding_received', [])]));
    const received = first(view.container);
    expect(byKey(received, 'episode_photo_panel.finding.none')).not.toBeNull();
    expect(within(received).queryByRole('button', { name: SHOW() })).toBeNull();
  });

  it('axe sin violaciones, plegada y abierta', async () => {
    serve();
    const view = openTimeline(cycle());
    expect(await axe(view.container)).toHaveNoViolations();
    const received = first(view.container);
    fireEvent.click(within(received).getByRole('button', { name: SHOW() }));
    await screen.findByRole('img', { name: ALT_PEAK() }, WAIT);
    expect(await axe(view.container)).toHaveNoViolations();
  });

  it('el mismo botón abre y repliega sin perder el foco ni volver a pedir la imagen', async () => {
    const traffic = serve();
    const view = openTimeline(cycle());
    const received = first(view.container);
    const toggle = within(received).getByRole('button', { name: SHOW() });
    toggle.focus();
    fireEvent.click(toggle);
    await screen.findByRole('img', { name: ALT_PEAK() }, WAIT);
    await settle();
    // El botón sigue montado (el foco no se pierde) y ahora repliega.
    expect(toggle.isConnected).toBe(true);
    expect(document.activeElement).toBe(toggle);
    expect(toggle.getAttribute('aria-expanded')).toBe('true');
    expect(toggle.textContent).toBe(text('episode_photo_panel.finding.hide'));
    fireEvent.click(toggle);
    expect(toggle.getAttribute('aria-expanded')).toBe('false');
    expect(toggle.textContent).toBe(SHOW());
    expect(received.querySelector('[data-part="photo_fold"]')?.hasAttribute('hidden')).toBe(true);
    fireEvent.click(toggle);
    await settle();
    expect(received.querySelector('[data-part="photo_fold"]')?.hasAttribute('hidden')).toBe(false);
    expect(traffic.image.map((entry) => entry.photo)).toEqual([photoId(0)]);
  });
});

describe('«Ver foto» de un tramo sin ancla (SCR-08)', { timeout: TEST_TIMEOUT }, () => {
  it('al pulsarlo, el foco pasa a la zona de la foto y no al documento', async () => {
    serve();
    const view = openDetail(findingDetail([[photo(0)], [photo(0, { photo_id: photoId(50) })]]));
    await screen.findByRole('img', { name: ALT_PEAK() }, WAIT);
    const later = blocks(view.container)[0] ?? fail();
    fireEvent.click(within(later).getByRole('button', { name: SHOW() }));
    await settle();
    expect(document.activeElement).toBe(later.querySelector('[data-part="photo_media"]'));
  });
});
