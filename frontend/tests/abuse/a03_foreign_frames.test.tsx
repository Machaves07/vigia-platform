import { execFileSync } from 'node:child_process';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import ts from 'typescript';
import { expect, it } from 'vitest';
import { nodes, sources } from './source';

it('A-3 · BR-APP-70: ningún marco y frame-ancestors none recibido desde el emisor real de cabeceras', () => {
  const frames = sources().flatMap(({ path, source }) =>
    nodes(source)
      .filter(
        (node) =>
          (ts.isJsxOpeningElement(node) || ts.isJsxSelfClosingElement(node)) &&
          /^(iframe|frame|frameset)$/i.test(node.tagName.getText(source)),
      )
      .map(() => path),
  );
  expect(frames).toEqual([]);
  expect(readFileSync(join(import.meta.dirname, '../../index.html'), 'utf8')).not.toMatch(
    /<(?:iframe|frame|frameset)\b/i,
  );
  // Python estándar: no requiere instalar el backend, Docker ni una segunda suite.
  // El servidor HTTP de la sonda envía las cabeceras del componente de producción, no una copia.
  const headers: unknown = JSON.parse(
    execFileSync(
      'python3',
      [
        '-c',
        `
import http.client, importlib.util, json, threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
path = Path('../backend/src/vigia_platform/shared/api/middleware/headers.py')
spec = importlib.util.spec_from_file_location('vigia_headers', path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        message = {'type': 'http.response.start', 'headers': []}
        module.SecurityHeaders().apply(message)
        self.send_response(200)
        for name, value in message['headers']:
            self.send_header(name.decode(), value.decode())
        self.end_headers()
        self.wfile.write(b'<!doctype html><title>Sonda sintetica</title>')
    def log_message(self, *args):
        pass
with HTTPServer(('127.0.0.1', 0), Handler) as server:
    thread = threading.Thread(target=server.handle_request)
    thread.start()
    client = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=10)
    client.request('GET', '/')
    response = client.getresponse()
    print(json.dumps(dict(response.getheaders())))
    response.read()
    client.close()
    thread.join(timeout=10)
`,
      ],
      { encoding: 'utf8', timeout: 30_000 },
    ),
  );
  expect(headers).toMatchObject({ 'x-frame-options': 'DENY' });
  expect((headers as Record<string, string>)['content-security-policy']?.split('; ')).toContain(
    "frame-ancestors 'none'",
  );
}, 30_000);
