/**
 * UI-24 `CopyOnceSecret` (BR-APP-12, 50): el valor se ve una vez, se copia, se confirma «lo
 * guardé» y, tras desmontar, no queda en el DOM, `localStorage`, `sessionStorage`,
 * `document.title`, `history.state`, la URL ni la caché de consultas y mutaciones. Cada prueba
 * busca el valor exacto generado en esa corrida.
 */
import {
  QueryClient,
  QueryClientProvider,
  useMutation,
  type MutationKey,
  type QueryKey,
} from '@tanstack/react-query';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { useEffect } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { axe } from 'vitest-axe';

import { CopyOnceSecret, type SecretKind } from '@/components/CopyOnceSecret/CopyOnceSecret';
import { text } from '@/texts';

let writeText: ReturnType<typeof vi.fn<(value: string) => Promise<void>>>;

beforeEach(() => {
  writeText = vi.fn<(value: string) => Promise<void>>().mockResolvedValue(undefined);
  Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true });
  localStorage.clear();
  sessionStorage.clear();
});

afterEach(() => {
  Reflect.deleteProperty(navigator, 'clipboard');
});

function secretFor(kind: SecretKind): string | readonly string[] {
  const token = crypto.randomUUID();
  switch (kind) {
    case 'recovery_codes':
      return Array.from({ length: 10 }, () => crypto.randomUUID().slice(0, 10));
    case 'invitation_link':
      return `https://app.vigia.example/invitations/${token}#accept`;
    case 'enrollment_code':
      return `ENR-${token}`;
  }
}

function needles(value: string | readonly string[]): readonly string[] {
  return typeof value === 'string' ? [value] : value;
}

function storageDump(storage: Storage): string {
  const entries: string[] = [];
  for (let index = 0; index < storage.length; index += 1) {
    const key = storage.key(index);
    if (key !== null) entries.push(key, storage.getItem(key) ?? '');
  }
  return entries.join('\n');
}

function cacheDump(client: QueryClient): string {
  return JSON.stringify([
    client
      .getQueryCache()
      .getAll()
      .map((query) => [query.queryKey, query.state.data]),
    client
      .getMutationCache()
      .getAll()
      .map((mutation) => [mutation.state.data, mutation.state.variables]),
  ]);
}

/** Todos los lugares donde el valor no puede quedar. */
function leaks(client: QueryClient, value: string | readonly string[]): string[] {
  const places: Record<string, string> = {
    dom: document.documentElement.outerHTML,
    localStorage: storageDump(localStorage),
    sessionStorage: storageDump(sessionStorage),
    title: document.title,
    history: JSON.stringify(window.history.state),
    location: window.location.href,
    cache: cacheDump(client),
  };
  return Object.entries(places)
    .filter(([, content]) => needles(value).some((needle) => content.includes(needle)))
    .map(([place]) => place);
}

const MUTATION_KEY: MutationKey = ['users', 'invite'];
const QUERY_KEY: QueryKey = ['nodes', 'enrollment_code'];

/** Pantalla mínima: la escritura devuelve el secreto y el componente lo muestra una vez. */
function Screen({
  kind,
  value,
  onAcknowledged,
}: {
  readonly kind: SecretKind;
  readonly value: string | readonly string[];
  readonly onAcknowledged: () => void;
}) {
  const mutation = useMutation({
    mutationKey: MUTATION_KEY,
    mutationFn: () => Promise.resolve(value),
  });
  const { mutate } = mutation;
  useEffect(() => {
    mutate();
  }, [mutate]);
  if (mutation.data === undefined) return null;
  return (
    <CopyOnceSecret
      kind={kind}
      value={mutation.data}
      nodeId={kind === 'enrollment_code' ? '0190a3b2-7c4d-7e8f-9a0b-1c2d3e4f5a6b' : undefined}
      fingerprints={kind === 'enrollment_code' ? ['a'.repeat(64), 'b'.repeat(64)] : undefined}
      discard={{ mutationKey: MUTATION_KEY, queryKey: QUERY_KEY }}
      onAcknowledged={onAcknowledged}
    />
  );
}

describe.each(['recovery_codes', 'invitation_link', 'enrollment_code'] as const)('%s', (kind) => {
  it('se ve, se copia, se confirma y tras desmontar no queda en ningún sitio', async () => {
    const value = secretFor(kind);
    const client = new QueryClient();
    // Otra vía por la que la pantalla podría tenerlo: una consulta con el valor.
    client.setQueryData(QUERY_KEY, { code: needles(value)[0] });
    const onAcknowledged = vi.fn();
    const { unmount } = render(
      <QueryClientProvider client={client}>
        <main>
          <Screen kind={kind} value={value} onAcknowledged={onAcknowledged} />
        </main>
      </QueryClientProvider>,
    );
    const region = await screen.findByRole('region', {
      name: text(`components_forms.secret.${kind}`),
    });
    const shown = region.querySelector('[data-part="value"]');
    expect(
      typeof value === 'string'
        ? shown?.textContent
        : [...(shown?.querySelectorAll('code') ?? [])].map((code) => code.textContent),
    ).toEqual(value);
    expect(region.querySelector('p')?.textContent).toBe(text('components_forms.secret.once'));

    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: text('components_forms.secret.copy') }));
      await Promise.resolve();
    });
    expect(writeText).toHaveBeenCalledExactlyOnceWith(
      typeof value === 'string' ? value : value.join('\n'),
    );
    fireEvent.click(
      screen.getByRole('button', { name: text('components_forms.secret.acknowledge') }),
    );
    expect(onAcknowledged).toHaveBeenCalledOnce();

    expect(leaks(client, value)).toEqual(expect.arrayContaining(['dom', 'cache']));
    unmount();
    expect(leaks(client, value)).toEqual([]);
    client.clear();
  });
});

describe('código de alta', () => {
  it('muestra el node_id y todas las huellas de la raíz', async () => {
    const client = new QueryClient();
    render(
      <QueryClientProvider client={client}>
        <Screen kind="enrollment_code" value="ENR-x" onAcknowledged={() => undefined} />
      </QueryClientProvider>,
    );
    await waitFor(() => {
      expect(document.querySelector('[data-part="node_id"]')?.textContent).toBe(
        '0190a3b2-7c4d-7e8f-9a0b-1c2d3e4f5a6b',
      );
    });
    expect(
      [...document.querySelectorAll('[data-part="fingerprints"] code')].map(
        (code) => code.textContent,
      ),
    ).toEqual(['a'.repeat(64), 'b'.repeat(64)]);
  });

  it('funciona sin proveedor de consultas (no hay caché que limpiar)', () => {
    const value = `https://x.example/i/${crypto.randomUUID()}`;
    const { unmount } = render(
      <CopyOnceSecret kind="invitation_link" value={value} onAcknowledged={() => undefined} />,
    );
    expect(leaks(new QueryClient(), value)).toEqual(['dom']);
    unmount();
    expect(leaks(new QueryClient(), value)).toEqual([]);
  });
});

describe('axe', () => {
  it.each(['recovery_codes', 'invitation_link', 'enrollment_code'] as const)('%s', async (kind) => {
    const { container } = render(
      <main>
        <CopyOnceSecret
          kind={kind}
          value={secretFor(kind)}
          nodeId="0190a3b2-7c4d-7e8f-9a0b-1c2d3e4f5a6b"
          fingerprints={['c'.repeat(64)]}
          onAcknowledged={() => undefined}
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
