/**
 * Inventario del hito «U-05 terminada» (VIG-339, TASK-459; `unit-of-work-dependency.md` §4).
 *
 * - Las dieciséis pantallas con su `ScreenDescriptor` y **ningún grupo de rutas en página de
 *   relevo**: cada `src/pages/scrNN/<grupo>/index.tsx` que dejó VIG-290 está sustituido.
 * - Al menos un puerto por pantalla (`src/ports/scrNN*.ts`) y ninguna operación `assumed` en
 *   ningún puerto de `src/ports/`.
 * - PR-APP-01 a 30 en `tests/properties/` (igualdad de conjuntos con la lista del diseño:
 *   `business-logic-model.md` §11, 01 a 22, y `nfr-design-patterns.md`, 23 a 30); FS-APP-01 a 10
 *   en `tests/resilience/` y A-1 a A-10 en `tests/abuse/` (`business-rules.md` §10).
 * - Las operaciones de `backend/openapi/app.yaml` (el número se lee del archivo) frente a las que
 *   nombran los puertos: las que ninguna pantalla consume son exactamente las de `NOT_CONSUMED`,
 *   cada una con su motivo.
 * - El README cita el trabajo y los artefactos reales del `nightly.yml` de VIG-337.
 *
 * Las listas del diseño se copian aquí porque `aidlc-docs` no está en el repositorio.
 */
/* eslint-disable no-restricted-imports -- el inventario recorre las operaciones del cliente generado. */
import { existsSync, readdirSync, readFileSync, statSync } from 'node:fs';
import { dirname, join, relative, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

import ts from 'typescript';
import { describe, expect, it } from 'vitest';

import { SCREENS } from '@/app/screens';
import { OPERATIONS } from '@/client';
import type { Declaration, ScreenPort } from '@/ports/base';

import { SCENARIOS } from '../resilience/scenarios';

const FRONTEND = resolve(dirname(fileURLToPath(import.meta.url)), '../..');
const REPO = join(FRONTEND, '..');
const read = (path: string): string => readFileSync(join(FRONTEND, path), 'utf8');

const SCREEN_IDS = Array.from({ length: 16 }, (_, i) => String(i + 1).padStart(2, '0'));

/** Listas cerradas del diseño: PR-APP-01 a 30, FS-APP-01 a 10 y A-1 a A-10. */
const DESIGN_PROPERTIES = Array.from(
  { length: 30 },
  (_, i) => `PR-APP-${String(i + 1).padStart(2, '0')}`,
);
const DESIGN_SCENARIOS = Array.from(
  { length: 10 },
  (_, i) => `FS-APP-${String(i + 1).padStart(2, '0')}`,
);
const DESIGN_ABUSE = Array.from({ length: 10 }, (_, i) => `A-${String(i + 1)}`);

const NODES = 'ruta del nodo (mTLS, contrato de U-01): la consume el nodo de U-06, no la SPA';
const PLATFORM = 'operación `platform.*` del operador de plataforma: ninguna pantalla la expone';

/**
 * Operaciones de `app.yaml` que ninguna pantalla consume, con su motivo. Cualquier cambio (una
 * operación nueva sin puerto o un puerto que empieza a consumir una de estas) pone la prueba en
 * rojo hasta revisar la lista.
 */
const NOT_CONSUMED: Readonly<Record<string, string>> = {
  'GET /health/live': 'sonda del circuito de `state.connection` (PAT-APP-RES-01), no una pantalla',
  'POST /platform/dead-letter/{event_id}/{consumer}/replay': PLATFORM,
  'POST /platform/keys/{purpose}/rotate': PLATFORM,
  'GET /audit/entries':
    'fuera del piloto: sin pantalla (nota (4) de `logical-components.md` §8, `src/app/screens.ts`)',
  'POST /api/nodes/clip-uploads': NODES,
  'POST /api/nodes/clip-uploads/{clip_id}/confirmation': NODES,
  'POST /api/nodes/credential-rotations': NODES,
  'POST /api/nodes/detection-reviews': NODES,
  'POST /api/nodes/enrollment': NODES,
  'POST /api/nodes/findings': NODES,
  'POST /api/nodes/heartbeats': NODES,
  'POST /api/nodes/observability-events': NODES,
  'POST /api/nodes/update-results': NODES,
  'GET /api/nodes/zones/{zone_id}/catalog': NODES,
};

function files(dir: string): string[] {
  const absolute = join(FRONTEND, dir);
  if (!existsSync(absolute)) return [];
  return readdirSync(absolute).flatMap((name) => {
    const path = join(absolute, name);
    const rel = relative(FRONTEND, path).replaceAll('\\', '/');
    return statSync(path).isDirectory() ? files(rel) : [rel];
  });
}

/** Literales de cadena e identificadores del código (no los comentarios) de `src/ports/**`. */
function portTokens(): Set<string> {
  const tokens = new Set<string>();
  for (const file of files('src/ports').filter((path) => /\.tsx?$/.test(path))) {
    const source = ts.createSourceFile(file, read(file), ts.ScriptTarget.Latest, true);
    const visit = (node: ts.Node): void => {
      if (ts.isStringLiteralLike(node) || ts.isIdentifier(node)) tokens.add(node.text);
      ts.forEachChild(node, visit);
    };
    visit(source);
  }
  return tokens;
}

const portModules = import.meta.glob<Record<string, unknown>>('/src/ports/**/*.ts', {
  eager: true,
});

function screenPorts(): { file: string; port: ScreenPort<Record<string, Declaration>> }[] {
  return Object.entries(portModules).flatMap(([file, module]) =>
    Object.values(module)
      .filter(
        (value): value is ScreenPort<Record<string, Declaration>> =>
          typeof value === 'object' && value !== null && 'operations' in value && 'screen' in value,
      )
      .map((port) => ({ file: file.slice(1), port })),
  );
}

/** `operationId` de `app.yaml`, leídos del archivo en su orden. */
function specifiedOperations(): string[] {
  const yaml = readFileSync(join(REPO, 'backend/openapi/app.yaml'), 'utf8');
  return [...yaml.matchAll(/^\s+"?operationId"?:\s*"?(\w+)"?\s*$/gm)].map(
    (match) => match[1] ?? '',
  );
}

const label = (id: keyof typeof OPERATIONS): string =>
  `${OPERATIONS[id].method.toUpperCase()} ${OPERATIONS[id].path}`;

describe('hito U-05: pantallas', () => {
  it('dieciséis descriptores, uno por pantalla y en su archivo', () => {
    expect(SCREENS.map((screen) => screen.screen_id)).toEqual(SCREEN_IDS.map((n) => `SCR-${n}`));
    for (const n of SCREEN_IDS) {
      expect(existsSync(join(FRONTEND, `src/pages/scr${n}/descriptor.ts`)), `scr${n}`).toBe(true);
    }
  });

  it('ningún grupo de rutas sigue en la página de relevo de VIG-290', () => {
    const groups = SCREENS.flatMap((screen) =>
      screen.route_groups.map(
        (group) => `src/pages/scr${screen.screen_id.slice('SCR-'.length)}/${group.id}/index.tsx`,
      ),
    );
    expect(groups).toHaveLength(47);
    const relays = groups.filter((file) => {
      const source = read(file);
      return /\bRelayPage\b/.test(source) || /\brelevo de VIG-290\b/.test(source);
    });
    expect(relays).toEqual([]);
    expect(existsSync(join(FRONTEND, 'src/pages/shared/RelayPage.tsx'))).toBe(false);
    const anyRelay = files('src/pages').filter((file) => /\bRelayPage\b/.test(read(file)));
    expect(anyRelay).toEqual([]);
  });
});

describe('hito U-05: puertos', () => {
  it('al menos un puerto por pantalla en src/ports/scrNN*.ts, de esa misma pantalla', () => {
    const ports = screenPorts();
    for (const n of SCREEN_IDS) {
      const own = ports.filter(({ file }) => new RegExp(`^src/ports/scr${n}[A-Z]`).test(file));
      expect(own.length, `SCR-${n}`).toBeGreaterThan(0);
      for (const { file, port } of own) expect(port.screen, file).toBe(`SCR-${n}`);
    }
  });

  it('ninguna operación assumed en ningún puerto de src/ports/', () => {
    const ports = screenPorts();
    expect(ports.length).toBeGreaterThanOrEqual(23);
    const assumed = ports.flatMap(({ file, port }) =>
      port.operations
        .filter((info) => info.status !== 'fixed' || info.api_operation_id === null)
        .map((info) => `${file}: ${info.name}`),
    );
    expect(assumed).toEqual([]);
  });

  // El recuento va en el título para que la salida de la prueba lo deje como evidencia.
  const total = specifiedOperations().length;
  const free = Object.keys(NOT_CONSUMED).length;
  it(`las ${String(total)} operaciones de app.yaml: ${String(total - free)} consumidas por puertos, ${String(free)} sin pantalla`, () => {
    const specified = specifiedOperations();
    expect(new Set(specified).size).toBe(specified.length);
    expect(specified.length).toBeGreaterThan(0);
    // El cliente generado es la misma lista (`generate:check` lo exige).
    expect(new Set(Object.keys(OPERATIONS))).toEqual(new Set(specified));
    const tokens = portTokens();
    const ids = specified as (keyof typeof OPERATIONS)[];
    const unused = ids.filter((id) => !tokens.has(id)).map(label);
    expect(new Set(unused)).toEqual(new Set(Object.keys(NOT_CONSUMED)));
    // Los motivos se sostienen: rutas de nodos, `platform.*` y la sonda de conexión.
    for (const operation of unused) {
      const path = operation.split(' ')[1] ?? '';
      if (NOT_CONSUMED[operation] === NODES) expect(path.startsWith('/api/nodes/')).toBe(true);
      if (NOT_CONSUMED[operation] === PLATFORM) expect(path.startsWith('/platform/')).toBe(true);
    }
    expect(read('src/state/connection/probe.ts')).toContain("'/health/live'");
    expect(read('src/app/screens.ts')).toContain('`GET /audit/entries` (`audit.read`) no tiene');
  });
});

describe('hito U-05: propiedades, escenarios y abusos', () => {
  it('PR-APP-01 a 30 en tests/properties/: igualdad de conjuntos con el diseño', () => {
    const found = files('tests/properties').flatMap((file) => {
      const match = /\/pr_app_(\d{2})_\w+\.test\.tsx?$/.exec(file);
      if (match === null) return [];
      const id = `PR-APP-${match[1] ?? ''}`;
      const source = read(file);
      // El archivo nombra su propiedad y contiene al menos una prueba.
      expect(source, file).toMatch(new RegExp(`\\b${id}\\b`));
      expect(/\b(?:it|test)(?:\.\w+)?\(/.test(source), file).toBe(true);
      return [id];
    });
    expect(new Set(found).size).toBe(found.length);
    expect(new Set(found)).toEqual(new Set(DESIGN_PROPERTIES));
  });

  it('FS-APP-01 a 10 en tests/resilience/ y en el catálogo de escenarios', () => {
    const found = files('tests/resilience').flatMap((file) => {
      const match = /\/fs_app_(\d{2})\.test\.tsx?$/.exec(file);
      if (match === null) return [];
      const id = `FS-APP-${match[1] ?? ''}`;
      expect(read(file), file).toContain(`* ${id} `);
      return [id];
    });
    expect(new Set(found)).toEqual(new Set(DESIGN_SCENARIOS));
    expect(new Set(Object.keys(SCENARIOS))).toEqual(new Set(DESIGN_SCENARIOS));
  });

  it('A-1 a A-10 en tests/abuse/', () => {
    const found = files('tests/abuse').flatMap((file) => {
      const match = /\/a(\d{2})_\w+\.test\.tsx?$/.exec(file);
      if (match === null) return [];
      const id = `A-${String(Number(match[1]))}`;
      expect(read(file), file).toMatch(new RegExp(`['\`]${id} ·`));
      return [id];
    });
    expect(new Set(found)).toEqual(new Set(DESIGN_ABUSE));
  });
});

describe('hito U-05: el README frente a nightly.yml', () => {
  it('cita el trabajo y los artefactos reales del trabajo frontend-nightly', () => {
    const workflow = readFileSync(join(REPO, '.github/workflows/nightly.yml'), 'utf8');
    const job = /^ {2}frontend-nightly:\n {4}name: (.+)$/m.exec(workflow)?.[1];
    expect(job).toBeDefined();
    const block = workflow.slice(workflow.indexOf('  frontend-nightly:'));
    const jobBlock = block.slice(0, block.search(/\n {2}\w[\w-]*:\n/));
    const artifacts = [...jobBlock.matchAll(/^\s+name: ((?:informe|frontend)-[\w-]+)$/gm)].map(
      (match) => match[1],
    );
    expect(artifacts.length).toBeGreaterThan(0);
    const readme = read('README.md');
    const bullet = /^- \*\*`nightly\.yml`\*\*:[\s\S]*?(?=\n- \*\*)/m.exec(readme)?.[0] ?? '';
    expect(bullet.replace(/\s+/g, ' ')).toContain(`«${job ?? ''}»`);
    const cited = [...bullet.matchAll(/`((?:informe|frontend)-[\w-]+)`/g)].map((match) => match[1]);
    expect(new Set(cited)).toEqual(new Set(artifacts));
    expect(jobBlock).toContain('xvfb-run -a npm run e2e');
    expect(bullet).toContain('tests/e2e/*.spec.ts');
  });
});
