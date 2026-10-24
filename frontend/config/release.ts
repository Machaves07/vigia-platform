/**
 * Complemento de construcción `vigia-release` (VIG-288, LC-APP-29; NFR-APP-14, 24; R-APP-7).
 *
 * Al final de `vite build`, sobre la salida ya generada:
 *
 * - **Marcador**: falla si `index.html` no contiene `vigia-app` (la comprobación de Route 53
 *   `vigia-app-index` lo busca en `/`).
 * - **Mapas de fuente fuera de `dist/`**: cada `.map` se escribe en `sourcemapDir` y se quita de la
 *   salida; `vigia-api` nunca los cataloga, y el release los adjunta para diagnóstico (VIG-337).
 * - **Precompresión**: `.br` (calidad 11) y `.gz` (nivel 9) junto a cada recurso de `assets/`, que
 *   `vigia-api` sirve según `Accept-Encoding` (`shared/api/static.py`).
 * - **`version.json`** `{app_version, contract_tag, built_at}` en la raíz de `dist/`.
 *
 * Las reglas puras (patrones, marcador, qué se precomprime) están en `config/build.ts`.
 */
import { mkdirSync, rmSync, writeFileSync } from 'node:fs';
import { dirname, isAbsolute, join } from 'node:path';
import { brotliCompressSync, constants, gzipSync } from 'node:zlib';

import type { Plugin } from 'vite';

import {
  type AppRelease,
  assertAppMarker,
  isPrecompressed,
  isSourceMap,
  versionDocument,
} from './build.ts';

export interface ReleaseOptions {
  readonly release: AppRelease;
  /** Carpeta de los mapas de fuente, fuera de `dist/`; se vacía en cada construcción. */
  readonly sourcemapDir: string;
}

function brotli(source: Uint8Array): Uint8Array {
  return brotliCompressSync(source, {
    params: {
      [constants.BROTLI_PARAM_QUALITY]: constants.BROTLI_MAX_QUALITY,
      [constants.BROTLI_PARAM_SIZE_HINT]: source.byteLength,
    },
  });
}

function gzip(source: Uint8Array): Uint8Array {
  return gzipSync(source, { level: constants.Z_BEST_COMPRESSION });
}

function bytes(source: string | Uint8Array): Uint8Array {
  return typeof source === 'string' ? Buffer.from(source, 'utf8') : source;
}

function message(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

export function vigiaRelease({ release, sourcemapDir }: ReleaseOptions): Plugin {
  // Antes de llegar a la salida: un valor inválido detiene la construcción al configurarla.
  const document = versionDocument(release);
  // La carpeta se vacía en cada construcción: nunca una ruta relativa (vacía sería el cwd).
  if (!isAbsolute(sourcemapDir)) {
    throw new Error(`La carpeta de mapas de fuente debe ser absoluta, no «${sourcemapDir}».`);
  }
  return {
    name: 'vigia-release',
    apply: 'build',
    generateBundle: {
      // Después de los complementos de Vite, que emiten `index.html` en esta misma fase.
      order: 'post',
      handler(_options, bundle) {
        const index = bundle['index.html'];
        if (index?.type !== 'asset') this.error('la construcción no produjo index.html.');
        try {
          assertAppMarker(Buffer.from(bytes(index.source)).toString('utf8'));
        } catch (error) {
          this.error(message(error));
        }
        rmSync(sourcemapDir, { recursive: true, force: true });
        for (const [fileName, output] of Object.entries(bundle)) {
          const source = output.type === 'chunk' ? output.code : output.source;
          if (isSourceMap(fileName)) {
            const target = join(sourcemapDir, fileName);
            mkdirSync(dirname(target), { recursive: true });
            writeFileSync(target, source);
            // eslint-disable-next-line @typescript-eslint/no-dynamic-delete -- quitar de la salida
            delete bundle[fileName];
            continue;
          }
          if (!isPrecompressed(fileName)) continue;
          const raw = bytes(source);
          this.emitFile({ type: 'asset', fileName: `${fileName}.br`, source: brotli(raw) });
          this.emitFile({ type: 'asset', fileName: `${fileName}.gz`, source: gzip(raw) });
        }
        this.emitFile({ type: 'asset', fileName: 'version.json', source: document });
      },
    },
  };
}
