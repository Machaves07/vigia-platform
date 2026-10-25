/* eslint-disable no-restricted-imports -- se recorre cada operación generada, incluidos todos los métodos de escritura. */
import { http, HttpResponse } from 'msw';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { guardChunkLoad, installChunkLoadGuard } from '@/app/chunkLoadGuard';
import { createAppVersion } from '@/app/version';
import { OPERATIONS } from '@/client';
import {
  createPortConnection,
  createTransport,
  defineScreenPort,
  installTransport,
  type OperationId,
  type Declaration,
} from '@/ports/base';
import { sessionPort } from '@/ports/shared';
import { uploadFile } from '@/ports/upload/upload';
import { createFormDraft, formDraftReducer } from '@/state/reducers/formDraft';

import { server } from '../../msw/server';
import { FakeClock } from '../../support/fakeClock';
import { flush } from '../../support/ports';

const LOCAL = 'release-local';
const RELEASE = {
  app_version: LOCAL,
  contract_tag: 'v1.1.0',
  built_at: '2026-10-24T00:00:00.000Z',
};
const NOTICE = { kind: 'version_outdated', message: 'Hay una versión nueva; recargue.' };

function setup(extra: Parameters<typeof createAppVersion>[0] = {}) {
  const clock = new FakeClock();
  const reload = vi.fn();
  const version = createAppVersion({
    appVersion: LOCAL,
    contractTag: 'v1.1.0',
    clock,
    reload,
    ...extra,
  });
  const transport = createTransport({
    clock,
    connection: createPortConnection(clock),
    version,
    devLog: () => undefined,
  });
  installTransport(transport);
  return { clock, reload, version, transport };
}

function serveRelease(body: Parameters<typeof HttpResponse.json>[0] = RELEASE) {
  server.use(http.get('*/version.json', () => HttpResponse.json(body)));
}

afterEach(() => {
  installTransport(null);
});

describe('versiones y bloqueo de escrituras', () => {
  it.each(['VITE_APP_VERSION', 'VITE_CONTRACT_TAG'] as const)(
    'si falta %s falla explícitamente al crear el controlador, no durante la comprobación remota',
    (name) => {
      for (const value of [undefined, '', ' ']) {
        vi.stubEnv(name, value);
        expect(() => createAppVersion()).toThrow(`app.version: falta ${name} en la construcción.`);
      }
    },
  );

  it('lee ambas versiones incrustadas y consulta version.json con no-store en el arranque y después de cada /me', async () => {
    vi.stubEnv('VITE_APP_VERSION', LOCAL);
    vi.stubEnv('VITE_CONTRACT_TAG', 'v1.1.0');
    const cache: (string | undefined)[] = [];
    const fetchRelease = vi.fn<typeof fetch>((_, init) => {
      cache.push(init?.cache);
      return Promise.resolve(Response.json(RELEASE));
    });
    const clock = new FakeClock();
    const version = createAppVersion({ fetch: fetchRelease, clock });
    installTransport(createTransport({ clock, connection: createPortConnection(clock), version }));
    await version.start();
    expect(version.canWrite()).toBe(true);
    server.use(http.get('*/me', () => HttpResponse.json({ api_version: LOCAL })));
    await sessionPort.me.read();
    await sessionPort.me.read();
    await flush();
    expect(cache).toEqual(['no-store', 'no-store', 'no-store']);
    expect(
      fetchRelease.mock.calls.every(([url]) =>
        (url instanceof URL ? url.href : typeof url === 'string' ? url : url.url).endsWith(
          '/version.json',
        ),
      ),
    ).toBe(true);
  });

  it.each(['body', 'header', 'error_header', 'release'] as const)(
    'detecta %s sin depender de state.session y no desbloquea con una respuesta antigua',
    async (source) => {
      serveRelease(source === 'release' ? { ...RELEASE, app_version: 'next' } : RELEASE);
      server.use(
        http.get('*/me', () =>
          HttpResponse.json(
            source === 'error_header'
              ? { code: 'forbidden' }
              : { api_version: source === 'body' ? 'next' : LOCAL },
            {
              status: source === 'error_header' ? 403 : 200,
              headers: source.includes('header') ? { 'X-Vigia-Api-Version': 'next' } : {},
            },
          ),
        ),
      );
      const { version, reload } = setup();
      const listener = vi.fn();
      const unsubscribe = version.subscribe(listener);
      await version.start();
      await sessionPort.me.read().catch(() => undefined);
      await flush();
      expect(version.getSnapshot()).toEqual({ outdated: true, message: NOTICE.message });
      version.observeApiVersion(LOCAL);
      expect(version.canWrite()).toBe(false);
      expect(listener).toHaveBeenCalledTimes(1);
      expect(reload).not.toHaveBeenCalled();
      unsubscribe();
    },
  );

  it('API real local sin estáticos: dev frente a unknown bloquea; un 404 de version.json no lo levanta', async () => {
    const { version } = setup({
      appVersion: 'dev',
      fetch: () => Promise.resolve(new Response(null, { status: 404 })),
    });
    version.observeMe({ api_version: 'unknown' });
    expect(await version.start()).toBe('unavailable');
    expect(version.canWrite()).toBe(false);
    await expect(sessionPort.logout.write()).rejects.toMatchObject({ failure: NOTICE });
  });

  it.each(['v2.0.0', 'v1.4.0', 'invalid', '', null])(
    'contrato incompatible o malformado %s: mismo bloqueo',
    async (contract_tag) => {
      serveRelease({ ...RELEASE, contract_tag });
      const { version, reload } = setup();
      await version.start();
      expect(version.canWrite()).toBe(false);
      await expect(sessionPort.logout.write()).rejects.toMatchObject({ failure: NOTICE });
      expect(reload).not.toHaveBeenCalled();
    },
  );

  it.each(['v1.1.0', 'v1.2.0', 'v1.3.0', '1.1.9'])(
    'contrato compatible %s: no compara etiquetas por igualdad',
    async (contract_tag) => {
      serveRelease({ ...RELEASE, contract_tag });
      const { version } = setup();
      await version.start();
      expect(version.canWrite()).toBe(true);
    },
  );

  it('ninguna escritura de cualquier operación, puerto fixed/assumed o subida llega a MSW; las lecturas continúan', async () => {
    let posts = 0;
    let writes = 0;
    server.use(
      http.all('*', ({ request }) => {
        if (request.method === 'POST') posts += 1;
        if (request.method !== 'GET') writes += 1;
        return HttpResponse.json({});
      }),
    );
    const { version, transport } = setup();
    // Control positivo: el contador no es vacuo.
    await sessionPort.logout.write();
    expect(posts).toBe(1);
    posts = 0;
    writes = 0;
    version.observeApiVersion('next');
    const ids = (Object.keys(OPERATIONS) as OperationId[]).filter(
      (id) => OPERATIONS[id].method !== 'get',
    );
    expect(ids.length).toBeGreaterThan(0);
    for (const operationId of ids) {
      const declaration = {
        kind: 'write',
        status: 'fixed',
        operationId,
        permission: 'synthetic',
      } as Declaration;
      const port = defineScreenPort('SCR-08', { save: declaration });
      // No hacen falta cuerpos ni parámetros válidos: el bloqueo precede a su validación.
      const save = port.save as { write(): Promise<unknown> };
      await expect(save.write()).rejects.toMatchObject({ failure: NOTICE });
      const path = Object.fromEntries(
        [...OPERATIONS[operationId].path.matchAll(/\{([^}]+)\}/g)].map(
          (match): [string, string] => [match[1] ?? '', 'synthetic'],
        ),
      );
      await expect(transport.request({ kind: 'write', operationId, path })).rejects.toMatchObject({
        failure: NOTICE,
      });
    }
    const assumed = defineScreenPort('SCR-08', {
      save: {
        kind: 'write',
        status: 'assumed',
        operation: 'POST /auth/login',
        permission: 'synthetic',
      },
    });
    await expect(assumed.save.write()).rejects.toMatchObject({ failure: NOTICE });
    await expect(
      uploadFile(
        { unit: 'catalog_document', plantId: 'p', kind: 'use_agreement' },
        new File(['abc'], 'synthetic.png', { type: 'image/png' }),
        { declared: false },
      ),
    ).rejects.toMatchObject({ failure: { kind: 'port', error: { failure: NOTICE } } });
    await sessionPort.me.read();
    expect({ posts, writes }).toEqual({ posts: 0, writes: 0 });
  });

  it('impide PUT si el cambio llega después de obtener la concesión', async () => {
    const { version } = setup();
    server.use(
      http.post('*/documents', () =>
        HttpResponse.json({
          document_ref: {},
          upload: { url: 'https://store.example.test/synthetic.png', required_headers: {} },
        }),
      ),
    );
    const putObject = vi.fn().mockResolvedValue(undefined);
    await expect(
      uploadFile(
        {
          unit: 'catalog_document',
          plantId: '0192d3e4-5f60-7a1b-8c2d-000000000001',
          kind: 'use_agreement',
        },
        new File(['abc'], 'synthetic.png', { type: 'image/png' }),
        {
          declared: false,
          putObject,
          onStage: (stage) => {
            if (stage === 'uploading') version.markOutdated();
          },
        },
      ),
    ).rejects.toMatchObject({ failure: { kind: 'port', error: { failure: NOTICE } } });
    expect(putObject).not.toHaveBeenCalled();
  });
});

describe('porciones antiguas y recarga controlada', () => {
  it('un import rechazado (404) y vite:preloadError bloquean, pero no recargan; desinstalar libera el oyente', async () => {
    serveRelease();
    const { version, reload } = setup();
    const error = new TypeError(
      'Failed to fetch dynamically imported module: /assets/scr08-old.js (404)',
    );
    await expect(guardChunkLoad(() => Promise.reject(error), version)).rejects.toBe(error);
    await flush();
    expect(version.canWrite()).toBe(false);
    expect(reload).not.toHaveBeenCalled();
    const other = setup();
    const uninstall = installChunkLoadGuard(window, other.version);
    window.dispatchEvent(new Event('vite:preloadError'));
    expect(other.version.canWrite()).toBe(false);
    expect(other.reload).not.toHaveBeenCalled();
    uninstall();
    const mark = vi.spyOn(other.version, 'markOutdated');
    window.dispatchEvent(new Event('vite:preloadError'));
    expect(mark).not.toHaveBeenCalled();
    await flush();
  });

  it('código cargado correctamente no bloquea', async () => {
    const { version } = setup();
    expect(await guardChunkLoad(() => Promise.resolve('module'), version)).toBe('module');
    expect(version.canWrite()).toBe(true);
  });

  it('ruta y borrador se conservan; cancelar no recarga, confirmar descarte tampoco; el botón posterior recarga en la ruta actual', () => {
    const { version, reload } = setup();
    let draft = formDraftReducer(createFormDraft('SCR-08', 'reason'), {
      type: 'set_value',
      field: 'reason',
      value: 'Texto sintético',
    });
    const original = draft;
    const route = '/findings/synthetic?tab=history#details';
    version.observeApiVersion('next');
    expect(version.requestReload({ route, draft })).toBe('confirmation_required');
    expect(draft).toBe(original);
    expect(reload).not.toHaveBeenCalled();
    draft = formDraftReducer(draft, { type: 'discard' });
    expect(reload).not.toHaveBeenCalled();
    expect(version.requestReload({ route, draft })).toBe('reloading');
    expect(reload).toHaveBeenCalledExactlyOnceWith({ route, draft });
  });

  it('el efecto real usa location.reload sin cambiar ruta, búsqueda ni fragmento', () => {
    const reload = vi.fn();
    const location = {
      href: 'https://vigia.example.test/findings/synthetic?tab=history#details',
      reload,
    };
    vi.stubGlobal('location', location);
    const version = createAppVersion({ appVersion: LOCAL });
    expect(
      version.requestReload({ route: '/findings/synthetic?tab=history#details', draft: null }),
    ).toBe('not_needed');
    version.markOutdated();
    version.requestReload({ route: '/findings/synthetic?tab=history#details', draft: null });
    expect(reload).toHaveBeenCalledTimes(1);
    expect(location.href).toBe('https://vigia.example.test/findings/synthetic?tab=history#details');
  });

  it('una comprobación caída o lenta no equivale a versión nueva ni levanta bloqueos', async () => {
    const fetchRelease = vi.fn<typeof fetch>(() => Promise.reject(new Error('red caída')));
    const { version, clock } = setup({ fetch: fetchRelease });
    expect(await version.start()).toBe('unavailable');
    expect(version.canWrite()).toBe(true);
    version.markOutdated();
    expect(await version.check()).toBe('unavailable');
    expect(version.canWrite()).toBe(false);
    let resolve!: (value: Response) => void;
    fetchRelease.mockImplementation(
      () =>
        new Promise((done) => {
          resolve = done;
        }),
    );
    const pending = version.check();
    const settled = vi.fn();
    void pending.then(settled);
    clock.advance(10_000);
    await flush();
    expect(settled).toHaveBeenCalledWith('unavailable');
    expect(await pending).toBe('unavailable');
    resolve(Response.json(RELEASE));
    await flush();
    expect(version.canWrite()).toBe(false);
  });

  it('ignora metadatos de una petición vencida aunque fetch no respete el aborto', async () => {
    let resolve!: (value: Response) => void;
    const { version, clock } = setup({
      fetch: () =>
        new Promise((done) => {
          resolve = done;
        }),
    });
    const pending = version.check();
    clock.advance(10_000);
    expect(await pending).toBe('unavailable');
    resolve(Response.json({ ...RELEASE, app_version: 'late' }));
    await flush();
    expect(version.canWrite()).toBe(true);
  });
});
