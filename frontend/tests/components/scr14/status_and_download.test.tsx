/** BR-APP-54/59: consultas explícitas, diferencia 14 y pruebas de integridad. */
import { act, fireEvent, screen, within } from '@testing-library/react';
import { HttpResponse, http } from 'msw';
import { describe, expect, it, vi } from 'vitest';
import { axe } from 'vitest-axe';
import { downloadHref, manifestHash, validRequest } from '@/ports/scr14Exports';
import { text, texts } from '@/texts';
import catalog from '@/texts/scr14.json';
import { label } from '@/texts/labels';
import { server } from '../../msw/server';
import {
  API,
  DOCUMENT_HASH,
  EXPORT,
  HASH,
  MANIFEST_HASH,
  ORG,
  PLANT,
  WAIT,
  fillRequest,
  fixture,
  job,
  ledgerPage,
  openJob,
} from './support';

describe('SCR-14 estado y descargas', { timeout: 30_000 }, () => {
  it('no consulta estado ni descargas al montar ni en cinco minutos; solo por acción explícita', async () => {
    const { clock } = await fixture(true);
    const counts = { status: 0, document: 0, volumes: 0, verifier: 0, checkpoints: 0 };
    server.use(
      http.get(`${API}/accreditation/exports/:export_id`, () => {
        counts.status++;
        return HttpResponse.json(job());
      }),
      http.post(`${API}/accreditation/exports/:export_id/download-url`, () => {
        counts.document++;
        return HttpResponse.json({});
      }),
      http.get(`${API}/accreditation/exports/:export_id/download`, () => {
        counts.volumes++;
        return HttpResponse.json({});
      }),
      http.get(`${API}/.well-known/vigia-verifier`, () => {
        counts.verifier++;
        return HttpResponse.json({ sha256: HASH });
      }),
      http.get(`${API}/integrity/checkpoints`, () => {
        counts.checkpoints++;
        return HttpResponse.json({ organization_id: ORG, checkpoints: [] });
      }),
    );
    act(() => {
      clock.advance(300_000);
    });
    expect(counts).toEqual({ status: 0, document: 0, volumes: 0, verifier: 0, checkpoints: 0 });
    await openJob();
    expect(counts.status).toBe(1);
    for (let minute = 0; minute < 5; minute++)
      await act(async () => {
        clock.advance(60_000);
        await Promise.resolve();
      });
    fireEvent.focus(window);
    fireEvent(document, new Event('visibilitychange'));
    expect(counts).toEqual({ status: 1, document: 0, volumes: 0, verifier: 0, checkpoints: 0 });
    const status = screen.getByRole('region', { name: EXPORT });
    const refresh = within(status).getAllByRole('button', {
      name: text('components_status.refresh.button'),
    })[0];
    if (refresh === undefined) throw new Error('Falta actualizar estado.');
    fireEvent.click(refresh);
    await vi.waitFor(() => {
      expect(counts.status).toBe(2);
    }, WAIT);
  });

  it('el catálogo no tiene la clave composing y usa las cinco etiquetas de U-04', () => {
    expect(Object.hasOwn(catalog, 'scr14.composing')).toBe(false);
    expect(Object.keys(texts).filter((key) => /^scr14\..*\bcomposing\b/.test(key))).toEqual([]);
    expect(
      ['requested', 'in_progress', 'ready', 'failed', 'expired'].map((state) =>
        label('export_state', state),
      ),
    ).toEqual(['Solicitada', 'En curso', 'Lista', 'Fallida', 'Caducada para descarga']);
  });

  it.each(['requested', 'in_progress', 'ready', 'failed', 'expired'] as const)(
    'representa %s sin ocultar acciones según reglas locales',
    async (state) => {
      await fixture();
      server.use(
        http.get(`${API}/accreditation/exports/:export_id`, () => HttpResponse.json(job(state))),
      );
      await openJob();
      expect(screen.getByText(label('export_state', state))).toBeDefined();
      expect(
        screen.getByRole<HTMLButtonElement>('button', { name: text('scr14.document') }).disabled,
      ).toBe(false);
    },
  );

  it.each(['document', 'volumes'] as const)(
    '409 expired sin detail_code en %s muestra caducada para descarga',
    async (kind) => {
      await fixture();
      const response = () =>
        HttpResponse.json(
          {
            code: 'conflict',
            message_es: 'Conflicto sintético',
            correlation_id: EXPORT,
            export_state: 'expired',
          },
          { status: 409 },
        );
      server.use(
        kind === 'document'
          ? http.post(`${API}/accreditation/exports/:export_id/download-url`, response)
          : http.get(`${API}/accreditation/exports/:export_id/download`, response),
      );
      await openJob();
      fireEvent.click(screen.getByRole('button', { name: text(`scr14.${kind}`) }));
      expect(
        (await screen.findByText(label('export_state', 'expired'), {}, WAIT)).textContent,
      ).toBe('Caducada para descarga');
      expect(screen.queryByRole('link', { name: text('scr14.download_document') })).toBeNull();
      expect(screen.queryByRole('alert')).toBeNull();
    },
  );

  it('emite URLs al pulsar, presenta hashes distintos, puntos de control y caducidad de 15 minutos; axe', async () => {
    const { clock, view } = await fixture();
    await openJob();
    fireEvent.click(screen.getByRole('button', { name: text('scr14.document') }));
    const document = await screen.findByRole(
      'link',
      { name: text('scr14.download_document') },
      WAIT,
    );
    expect(document.getAttribute('href')).toBe('https://downloads.example.test/document.pdf');
    fireEvent.click(screen.getByRole('button', { name: text('scr14.volumes') }));
    const volume = await screen.findByRole(
      'link',
      { name: `${text('scr14.download_volume')} 2` },
      WAIT,
    );
    expect(volume.getAttribute('href')).toBe('https://downloads.example.test/vol-02.zip');
    const verifier = screen.getByRole('region', { name: text('scr14.verifier') });
    fireEvent.click(
      within(verifier).getByRole('button', { name: text('components_status.refresh.button') }),
    );
    await within(verifier).findByText('d'.repeat(64), {}, WAIT);
    const checkpoints = screen.getByRole('region', { name: text('scr14.checkpoints') });
    fireEvent.click(
      within(checkpoints).getByRole('button', { name: text('components_status.refresh.button') }),
    );
    await within(checkpoints).findByRole('table', { name: text('scr14.checkpoints') }, WAIT);
    fireEvent.click(screen.getByRole('button', { name: text('scr14.manifest_hash') }));
    const proof = await screen.findByText(MANIFEST_HASH, {}, WAIT);
    expect(proof.textContent).toBe(MANIFEST_HASH);
    const manifest = screen.getByRole('region', { name: text('scr14.manifest') });
    expect(within(manifest).getByText(DOCUMENT_HASH).textContent).toBe(DOCUMENT_HASH);
    expect(within(manifest).getByText(HASH).textContent).toBe(HASH);
    expect(await axe(view.container)).toHaveNoViolations();
    act(() => {
      clock.advance(900_000);
    });
    expect(screen.queryByRole('link', { name: `${text('scr14.download_volume')} 2` })).toBeNull();
    expect(view.container.querySelectorAll('[data-download-expired="true"]')).toHaveLength(2);
  });

  it('busca el hash por clave de paginación, no usa registros de otra exportación', async () => {
    await fixture();
    const queries: URLSearchParams[] = [];
    server.use(
      http.get(`${API}/ledger/records`, ({ request }) => {
        const query = new URL(request.url).searchParams;
        queries.push(query);
        const page = ledgerPage();
        if (query.get('after_record_id') === null)
          return HttpResponse.json({
            items: [],
            next_cursor: { record_id: EXPORT, received_at: job().requested_at },
          });
        return HttpResponse.json(page);
      }),
    );
    await openJob();
    fireEvent.click(screen.getByRole('button', { name: text('scr14.manifest_hash') }));
    await screen.findByText(MANIFEST_HASH, {}, WAIT);
    expect(queries).toHaveLength(2);
    expect(queries[0]?.get('plant_id')).toBe(PLANT);
    expect(queries[0]?.getAll('record_type')).toEqual(['export_produced']);
    expect(queries[1]?.get('after_record_id')).toBe(EXPORT);
  });

  it('503 storage_unavailable degrada solo la descarga sin destruir el manifiesto', async () => {
    await fixture();
    server.use(
      http.post(`${API}/accreditation/exports/:export_id/download-url`, () =>
        HttpResponse.json(
          {
            code: 'storage_unavailable',
            message_es: 'Almacenamiento sintético no disponible',
            correlation_id: EXPORT,
          },
          { status: 503 },
        ),
      ),
    );
    await openJob();
    fireEvent.click(screen.getByRole('button', { name: text('scr14.document') }));
    await screen.findByText(text('scr14.storage_unavailable'), {}, WAIT);
    expect(screen.getByRole('region', { name: text('scr14.manifest') })).toBeDefined();
    expect(screen.getByRole('button', { name: text('scr14.volumes') })).toBeDefined();
  });

  it('al solicitar no hace una lectura auditada implícita', async () => {
    await fixture();
    let calls = 0;
    server.use(
      http.get(`${API}/accreditation/exports/:export_id`, () => {
        calls++;
        return HttpResponse.json(job());
      }),
    );
    fillRequest();
    fireEvent.click(screen.getByRole('button', { name: text('scr14.request') }));
    await screen.findByRole('region', { name: text('scr14.manifest') }, WAIT);
    expect(calls).toBe(0);
  });

  it('valida forma, límites, esquema de URL y contexto del manifiesto con sondas negativas', () => {
    const request = {
      plant_id: PLANT,
      zone_ids: ['zona'],
      period: job().period,
      package_form: 'without_clips' as const,
    };
    expect(validRequest(request)).toBe(true);
    expect(validRequest({ ...request, plant_id: '' })).toBe(false);
    expect(validRequest({ ...request, period: { from: 'no-fecha', to: 'no-fecha' } })).toBe(false);
    for (const url of [
      ['javascript', ':alert(1)'].join(''),
      'data:text/html,prueba',
      'https://user:pass@example.test/',
      'ruta-relativa',
    ])
      expect(downloadHref(url)).toBeUndefined();
    expect(downloadHref('https://example.test/file')).toBe('https://example.test/file');
    expect(manifestHash(ledgerPage(), job(), ORG)).toBe(MANIFEST_HASH);
    for (const patch of [
      { organization_id: EXPORT },
      { plant_id: EXPORT },
      { record_type: 'finding' },
      { content: { export_id: PLANT, manifest_sha256: MANIFEST_HASH } },
      { content: { export_id: EXPORT, manifest_sha256: 'inválido' } },
    ]) {
      const page = ledgerPage();
      const record = page.items[0];
      if (record === undefined) throw new Error('Falta el registro sintético.');
      page.items[0] = { ...record, ...patch };
      expect(manifestHash(page, job(), ORG)).toBeNull();
    }
  });
});
