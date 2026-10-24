/**
 * Complemento de construcción `vigia-bootstrap` (LC-APP-01, PAT-APP-REN-01; NFR-APP-31).
 *
 * Al construir, dos cosas que `index.html` necesita y no puede llevar escritas a mano:
 *
 * - **Versión mínima de la política de navegadores**: resuelve la lista `browserslist` de
 *   `package.json` y escribe `src/bootstrap/minVersions.generated.ts`, el archivo que
 *   `app.bootstrap` compara con el navegador en marcha (`config/minVersions.ts`). La comparación
 *   por versión cubre `Sec-Fetch-Site`, del que depende la barrera anti-falsificación de U-02.
 * - **Precarga del paquete inicial**: el guion de comprobación (`src/bootstrap/check.ts`) importa
 *   `src/main.tsx` de forma dinámica —React no puede cargarse antes de saber que el navegador
 *   sirve—, así que la porción del paquete inicial no aparece sola en `dist/index.html`; el
 *   complemento añade su `modulepreload` para que el navegador la descargue en paralelo con la
 *   comprobación. Si la porción no está, la construcción falla.
 */
import { readFileSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';

import type { Plugin } from 'vite';

import {
  browserPolicyQueries,
  formatMinVersions,
  MIN_VERSIONS_PATH,
  minVersionsFrom,
  renderMinVersions,
  resolveBrowserPolicy,
} from './minVersions.ts';

/** Módulo del paquete inicial, relativo a `frontend/`. */
export const APP_ENTRY = 'src/main.tsx';

/** Lo que el complemento usa de una porción del paquete de salida. */
export interface BundleChunkLike {
  readonly type: string;
  readonly fileName: string;
  readonly isDynamicEntry?: boolean;
  readonly facadeModuleId?: string | null;
  readonly moduleIds?: readonly string[];
}

function moduleIdsOf(chunk: BundleChunkLike): readonly string[] {
  const facade = chunk.facadeModuleId ?? null;
  const ids = chunk.moduleIds ?? [];
  return facade === null ? ids : [facade, ...ids];
}

function isModule(moduleId: string, appEntry: string): boolean {
  const normalized = moduleId.replaceAll('\\', '/');
  return normalized === appEntry || normalized.endsWith(`/${appEntry}`);
}

/**
 * Archivo de salida de la porción del paquete inicial: el trozo dinámico que contiene `appEntry`
 * (y, si la construcción lo dejara en otro, el primer trozo que lo contenga). `null` si no está.
 */
function appBundleFile(
  bundle: Readonly<Record<string, BundleChunkLike>>,
  appEntry: string = APP_ENTRY,
): string | null {
  const chunks = Object.values(bundle).filter((chunk) => chunk.type === 'chunk');
  const dynamic = chunks.find(
    (chunk) =>
      chunk.isDynamicEntry === true && moduleIdsOf(chunk).some((id) => isModule(id, appEntry)),
  );
  if (dynamic !== undefined) return dynamic.fileName;
  return (
    chunks.find((chunk) => moduleIdsOf(chunk).some((id) => isModule(id, appEntry)))?.fileName ??
    null
  );
}

/** Etiquetas de `dist/index.html` que inyecta el complemento (`HtmlTagDescriptor` de Vite). */
export interface HtmlTag {
  readonly tag: string;
  readonly attrs: Readonly<Record<string, string>>;
  readonly injectTo: 'head-prepend';
}

/** La etiqueta de precarga del paquete inicial, o `null` si el trozo no está. */
export function preloadTag(
  bundle: Readonly<Record<string, BundleChunkLike>>,
  appEntry: string = APP_ENTRY,
): HtmlTag | null {
  const file = appBundleFile(bundle, appEntry);
  if (file === null) return null;
  return {
    tag: 'link',
    attrs: { rel: 'modulepreload', href: `/${file}`, crossorigin: '' },
    injectTo: 'head-prepend',
  };
}

export interface BootstrapPluginOptions {
  /** Raíz de `frontend/` (donde están `package.json` y `src/`). */
  readonly root: string;
  /** Módulo del paquete inicial, relativo a `root`. */
  readonly appEntry?: string;
}

function readIfAny(file: string): string | null {
  try {
    return readFileSync(file, 'utf8');
  } catch {
    return null;
  }
}

/** El documento de `minVersions.generated.ts` tal como debe estar en `root`. */
export async function minVersionsDocument(root: string): Promise<string> {
  const queries = browserPolicyQueries(readFileSync(join(root, 'package.json'), 'utf8'));
  const resolved = resolveBrowserPolicy(queries, root);
  return formatMinVersions(
    renderMinVersions(minVersionsFrom(resolved), queries),
    join(root, '.prettierrc.json'),
  );
}

export function vigiaBootstrap({ root, appEntry = APP_ENTRY }: BootstrapPluginOptions): Plugin {
  return {
    name: 'vigia-bootstrap',
    apply: 'build',
    async buildStart() {
      const document = await minVersionsDocument(root);
      const target = join(root, MIN_VERSIONS_PATH);
      // Solo se escribe si cambió: una construcción reproducible no toca el árbol de trabajo.
      if (readIfAny(target) !== document) writeFileSync(target, document);
    },
    transformIndexHtml: {
      order: 'post',
      handler(_html, ctx) {
        const bundle = ctx.bundle;
        if (bundle === undefined) return undefined;
        const tag = preloadTag(bundle, appEntry);
        if (tag === null) {
          throw new Error(
            `vigia-bootstrap: no encontré la porción del paquete inicial (${appEntry}) en la salida; ` +
              'sin ella no se puede precargar (PAT-APP-REN-01).',
          );
        }
        return [tag];
      },
    },
  };
}
