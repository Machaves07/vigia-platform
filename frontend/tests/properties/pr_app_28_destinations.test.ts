/**
 * PR-APP-28 (NFR-APP-22, BR-APP-79, PAT-APP-SEG-01): para toda petición emitida por cualquier
 * puerto en cualquier flujo, el destino pertenece a la lista cerrada {origen propio, orígenes
 * prefirmados declarados}; la URL del nodo solo aparece en una navegación, nunca en una petición.
 *
 * Cada flujo monta una pantalla registrada sobre el armazón real (`tests/support/screenTour`) y
 * ejecuta después acciones de los puertos que trabajan con destinos ajenos al origen: subida
 * directa de un documento o de un adjunto (concesión y `PUT` al depósito), URL de lectura de un
 * clip y de un adjunto, enlace de la vista en vivo y descarga del paquete mensual. MSW registra
 * el destino de **toda** petición (`fetch` y `XMLHttpRequest`, también el preflight) con
 * `request:start`, antes de resolverla: también las que ningún manejador atiende.
 *
 * Los orígenes declarados (`stores`, como `VIGIA_CSP_STORE_ORIGINS`) y el del nodo se generan; el
 * servidor simulado escribe esos orígenes en todo campo de URL de sus respuestas (`url`,
 * `upload_url`, `read_url` y `live_view_local_url`). Sin telemetría: ningún flujo pide nada que
 * el servidor no le haya dado.
 *
 * El origen propio en jsdom son dos: el de los servicios (`ORIGIN`, la API) y el de la página
 * (`location.origin`, donde `version.json` se pide con `globalThis.location.origin`). En
 * producción son el mismo.
 */
import { act, cleanup } from '@testing-library/react';
import fc from 'fast-check';
import { http, HttpResponse } from 'msw';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import { installTransport } from '@/ports/base';
import { liveViewLink, liveViewPort } from '@/ports/liveview';
import { downloadZone } from '@/ports/scr12Monthly';
import {
  readClosureAttachmentUrl,
  readEvidenceUrl,
  uploadFile,
  type UploadTarget,
} from '@/ports/upload';

import { exampleFor, variant } from '../msw/handlers';
import { server } from '../msw/server';
import { ORIGIN, PLANT_ID, type ShellFixture } from '../support/appShell';
import { mountEntry, SCREEN_ENTRIES, type ScreenEntry } from '../support/screenTour';
import { settle } from '../support/queryCache';
import { PROPERTY_TIMEOUT_MS } from '../support/timeouts';

/** Cada flujo monta una pantalla: un décimo de los ejemplos del perfil (20 en `ci`, 200 de noche). */
const RUNS = Math.max(10, Math.ceil((fc.readConfigureGlobal().numRuns ?? 100) / 10));

const ZONE_ID = '0190a1b2-1111-7000-8000-000000000001';
const MONTHLY_DOWNLOAD =
  'get_monthly_package_plant_download_accreditation_monthly_packages__plant_id___period__download_get';
/** Token sintético con la forma de una JWS compacta (`base64url` por puntos) que admite `liveViewLink`. */
const NODE_TOKEN = ['Y2FiZWNlcmE', 'Y3VlcnBv', 'ZmlybWE'].join('.');
/** El depósito simulado responde el preflight de jsdom y el `PUT`. */
const CORS = {
  'access-control-allow-origin': '*',
  'access-control-allow-methods': 'PUT',
  'access-control-allow-headers': 'content-type, x-amz-checksum-sha256',
};

type Action =
  | 'upload_document'
  | 'upload_attachment'
  | 'read_evidence'
  | 'read_attachment'
  | 'live_view'
  | 'monthly_download';
const ACTIONS: readonly Action[] = [
  'upload_document',
  'upload_attachment',
  'read_evidence',
  'read_attachment',
  'live_view',
  'monthly_download',
];

interface Destinations {
  readonly stores: readonly string[];
  readonly node: string;
}

const label = fc.stringMatching(/^[a-z][a-z0-9-]{1,14}[a-z0-9]$/);
const storeOrigin = fc
  .tuple(label, fc.constantFrom('us-east-1', 'sa-east-1', 'eu-west-1'))
  .map(([bucket, region]) => `https://${bucket}.s3.${region}.amazonaws.com`);
const nodeOrigin = fc.oneof(
  fc
    .tuple(label, fc.integer({ min: 1024, max: 65_535 }))
    .map(([host, port]) => `https://${host}.local:${String(port)}`),
  fc
    .tuple(fc.integer({ min: 0, max: 255 }), fc.integer({ min: 1, max: 254 }))
    .map(([a, b]) => `https://10.20.${String(a)}.${String(b)}:8443`),
);
const destinations: fc.Arbitrary<Destinations> = fc.record({
  stores: fc.uniqueArray(storeOrigin, { minLength: 1, maxLength: 3 }),
  node: nodeOrigin,
});

/** El servidor simulado escribe los destinos generados en todo campo de URL. */
function rewriter({ stores, node }: Destinations): (body: unknown) => unknown {
  let next = 0;
  const store = (key: string) => {
    const origin = stores[next % stores.length] ?? '';
    next += 1;
    return `${origin}/${key}/${String(next)}?X-Amz-Signature=${'a'.repeat(64)}`;
  };
  const walk = (value: unknown): unknown => {
    if (Array.isArray(value)) return value.map(walk);
    if (typeof value !== 'object' || value === null) return value;
    const live = 'live_view_local_url' in value;
    return Object.fromEntries(
      Object.entries(value).map(([key, field]) => {
        if (typeof field === 'string' && ['url', 'upload_url', 'read_url'].includes(key)) {
          return [key, store(key)];
        }
        if (key === 'live_view_local_url') return [key, `${node}/`];
        if (live && key === 'token') return [key, NODE_TOKEN];
        if (key === 'required_headers') return [key, {}];
        return [key, walk(field)];
      }),
    );
  };
  return walk;
}

interface Observed {
  readonly requests: string[];
  readonly navigations: string[];
}

let observed: Observed = { requests: [], navigations: [] };
beforeEach(() => {
  observed = { requests: [], navigations: [] };
  server.events.on('request:start', ({ request }) => {
    observed.requests.push(request.url);
  });
});
afterEach(() => {
  server.events.removeAllListeners();
});

/** Espera `promise` avanzando el reloj simulado (reintentos y topes de `ports.base`). */
async function drive(fixture: ShellFixture, promise: Promise<unknown>): Promise<void> {
  const state = { done: false };
  promise.then(
    () => (state.done = true),
    () => (state.done = true), // Un rechazo del puerto no cambia la propiedad: importa a dónde pidió.
  );
  for (let round = 0; round < 600; round += 1) {
    await act(async () => {
      await settle(3);
    });
    if (state.done) return;
    fixture.clock.advance(1_000);
  }
  throw new Error('La acción no terminó en 10 minutos simulados.');
}

const PNG = [137, 80, 78, 71, 13, 10, 26, 10];
const TARGETS: Readonly<Record<'upload_document' | 'upload_attachment', UploadTarget>> = {
  upload_document: { unit: 'catalog_document', plantId: PLANT_ID, kind: 'scope_record' },
  upload_attachment: { unit: 'loop_attachment', actionId: '0190a1b2-8888-7000-8000-00000000000a' },
};

/** La acción `action` con el transporte que instaló la pantalla montada. */
async function perform(action: Action): Promise<void> {
  switch (action) {
    case 'upload_document':
    case 'upload_attachment': {
      const file = new File([new Uint8Array(PNG)], 'plano.png', { type: 'image/png' });
      await uploadFile(TARGETS[action], file, { declared: true });
      return;
    }
    case 'read_evidence':
      await readEvidenceUrl('0190a1b2-9999-7000-8000-00000000000b');
      return;
    case 'read_attachment':
      await readClosureAttachmentUrl('0190a1b2-9999-7000-8000-00000000000c');
      return;
    case 'live_view': {
      const handoff = await liveViewPort.requestToken.write({ path: { zone_id: ZONE_ID } });
      const link = liveViewLink(handoff);
      // La persona pulsa el enlace: la única salida hacia el nodo es esta navegación.
      if (link.kind === 'link') observed.navigations.push(link.href);
      return;
    }
    case 'monthly_download':
      await downloadZone(PLANT_ID, '2026-09', ZONE_ID, (url) => {
        observed.navigations.push(url);
      });
      return;
  }
}

/** La descarga del paquete trae la zona pedida (el ejemplo generado trae otras). */
const monthlyDownload = variant(MONTHLY_DOWNLOAD, () => {
  const example = exampleFor(MONTHLY_DOWNLOAD) as { zones: Record<string, unknown>[] };
  return HttpResponse.json({
    ...example,
    zones: example.zones.map((zone, index) => (index === 0 ? { ...zone, zone_id: ZONE_ID } : zone)),
  } as never);
});

/** Monta `entry` con los destinos generados, ejecuta `actions` y comprueba la propiedad. */
async function flow(
  entry: ScreenEntry,
  actions: readonly Action[],
  generated: Destinations,
): Promise<Observed> {
  observed.requests.length = 0;
  observed.navigations.length = 0;
  server.use(
    ...generated.stores.map((store) =>
      http.all(`${store}/*`, () => new HttpResponse(null, { status: 200, headers: CORS })),
    ),
  );
  const fixture = await mountEntry(entry, {
    rewrite: rewriter(generated),
    extra: [monthlyDownload],
  });
  try {
    for (const action of actions) await drive(fixture, perform(action));
  } finally {
    cleanup();
    fixture.services.dispose();
    installTransport(null);
    server.resetHandlers();
  }

  const own = [new URL(ORIGIN).origin, location.origin];
  const allowed = new Set([...own, ...generated.stores]);
  const origin = (url: string) => new URL(url).origin;
  expect(
    observed.requests.filter((url) => !allowed.has(origin(url))),
    `${entry.template} ${actions.join(',')}: destinos fuera de la lista cerrada`,
  ).toEqual([]);
  expect(observed.requests.filter((url) => origin(url) === generated.node)).toEqual([]);
  for (const url of observed.navigations) {
    expect(allowed.has(origin(url)) || origin(url) === generated.node, url).toBe(true);
  }
  return { requests: [...observed.requests], navigations: [...observed.navigations] };
}

describe('PR-APP-28: destinos de red en la lista cerrada', () => {
  it(
    'todo flujo (pantalla y acciones de los puertos) pide solo al origen propio y a los declarados',
    async () => {
      await fc.assert(
        fc.asyncProperty(
          fc.constantFrom(...SCREEN_ENTRIES),
          fc.array(fc.constantFrom(...ACTIONS), { maxLength: 4 }),
          destinations,
          async (entry, actions, generated) => {
            await flow(entry, actions, generated);
          },
        ),
        { numRuns: RUNS },
      );
    },
    PROPERTY_TIMEOUT_MS,
  );

  it(
    'cada pantalla registrada y cada acción, una vez: la subida llega al depósito y el nodo solo por navegación',
    async () => {
      const [generated] = fc.sample(destinations, { numRuns: 1, seed: 28 });
      if (generated === undefined) throw new Error('Sin destinos generados.');
      for (const entry of SCREEN_ENTRIES) await flow(entry, [], generated);

      const home = SCREEN_ENTRIES.find((entry) => entry.template === '/home');
      if (home === undefined) throw new Error('Falta la entrada /home.');
      const { requests, navigations } = await flow(home, ACTIONS, generated);
      // La propiedad no es vacía: hubo `PUT` al depósito y el enlace del nodo se compuso.
      const origins = new Set(requests.map((url) => new URL(url).origin));
      expect(generated.stores.some((store) => origins.has(store))).toBe(true);
      expect(navigations).toContain(`${generated.node}/#token=${NODE_TOKEN}`);
      expect(navigations.some((url) => generated.stores.includes(new URL(url).origin))).toBe(true);
    },
    PROPERTY_TIMEOUT_MS,
  );
});
