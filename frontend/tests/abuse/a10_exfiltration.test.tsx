import ts from 'typescript';
import { expect, it } from 'vitest';
import spec from '../msw/openapi.responses.json';
import { nodes, sources, styles } from './source';

// Inventario positivo de salidas: transporte de API, sonda de salud, versión y PUT prefirmado.
// Se fija también la expresión (no solo el archivo) para no permitir otro destino en ese módulo.
const NETWORK = [
  'ports/base/http.ts:globalThis.fetch(input, init)',
  "ports/base/http.ts:fetchImpl(url, { method: operation.method.toUpperCase(), headers: headersFor(request, concession), credentials: 'same-origin', signal: controller.signal, ...(request.body === undefined ? {} : { body: JSON.stringify(request.body) }), })",
  "ports/scr01Admin.ts:globalThis.fetch(new URL('/version.json', globalThis.location.origin), { cache: 'no-store', credentials: 'same-origin', signal, })",
  'ports/upload/upload.ts:new XMLHttpRequest()',
  "state/connection/probe.ts:options .fetch(new URL('/health/live', options.origin(dep)), { method: 'GET', signal: abort.signal, cache: 'no-store', credentials: 'omit', redirect: 'error', })",
];
const compact = (value: string) => value.replace(/\s+/g, ' ').trim();

it('A-10 · BR-APP-79; NFR-APP-22: lista cerrada de destinos, sin telemetría ni recursos externos añadidos', () => {
  const found: string[] = [];
  for (const { path, source } of sources()) {
    for (const node of nodes(source)) {
      if (ts.isCallExpression(node) || ts.isNewExpression(node)) {
        const expression = node.expression;
        const name = ts.isIdentifier(expression)
          ? expression.text
          : ts.isPropertyAccessExpression(expression)
            ? expression.name.text
            : '';
        if (
          [
            'fetch',
            'fetchImpl',
            'XMLHttpRequest',
            'WebSocket',
            'EventSource',
            'sendBeacon',
            'Worker',
            'SharedWorker',
            'importScripts',
            'Image',
            'RTCPeerConnection',
          ].includes(name)
        ) {
          found.push(`${path}:${compact(node.getText(source))}`);
        }
      }
      // Tampoco recursos cargados por importación dinámica, URL literal o script de terceros.
      if (ts.isStringLiteral(node) || ts.isNoSubstitutionTemplateLiteral(node)) {
        expect(
          node.text,
          `${path}:${String(source.getLineAndCharacterOfPosition(node.pos).line + 1)}`,
        ).not.toMatch(/^(?:https?:)?\/\//i);
      }
      if (
        ts.isJsxAttribute(node) &&
        ['src', 'href', 'action', 'srcSet'].includes(node.name.getText(source)) &&
        node.initializer &&
        ts.isStringLiteral(node.initializer)
      ) {
        expect(node.initializer.text, path).not.toMatch(/^(?:https?:)?\/\//i);
      }
    }
  }
  expect(found.sort()).toEqual(NETWORK.map(compact).sort());
  const html = readFileSync(join(import.meta.dirname, '../../index.html'), 'utf8');
  for (const resource of [...styles(), html]) {
    expect(resource).not.toMatch(/(?:https?:)?\/\//i);
  }
  for (const operation of spec.operations) {
    expect(operation.path).toMatch(/^\/(?!\/)/);
    expect(operation.path).not.toContain('://');
  }
}, 30_000);
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
