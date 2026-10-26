import ts from 'typescript';
import { expect, it } from 'vitest';
import spec from '../msw/openapi.responses.json';
import { nodes, sources } from './source';

// Configuración y ciclo de alta de U-03: nunca señales de actuación física. Revocar o retirar
// la identidad administra la flota; no manda detener una máquina ni acciona una salida local.
const NODE_WRITES = new Set([
  'POST /plants/{plant_id}/nodes',
  'POST /nodes/{node_id}/enrollment-codes',
  'POST /nodes/{node_id}/zones',
  'POST /nodes/{node_id}/zones/{zone_id}/unassignment',
  'POST /nodes/{node_id}/revocation',
  'POST /nodes/{node_id}/decommission',
  'POST /fleet/target-versions',
]);

it('BR-APP-32 · P1: todos los puertos carecen de salida a máquinas, balizas o bocinas; nodos solo configuración y alta', () => {
  const ports = sources().filter(
    ({ path }) =>
      path.startsWith('ports/') &&
      !path.startsWith('ports/base/') &&
      !path.startsWith('ports/validation/'),
  );
  const operations = ports
    .flatMap(({ source }) =>
      nodes(source)
        .filter(ts.isPropertyAssignment)
        .filter((node) => node.name.getText(source) === 'operationId')
        .map((node) => {
          if (!ts.isStringLiteral(node.initializer)) {
            // Esquema espejo de configuración del lazo, no una operación de salida a nodos.
            expect(node.initializer.getText(source)).toBe('configurationSchema.operationId');
            return '';
          }
          return node.initializer.text;
        }),
    )
    .filter(Boolean);
  expect(operations.length).toBeGreaterThan(100);
  const declarations = operations.map((id) => {
    const operation = spec.operations.find((operation) => operation.operationId === id);
    if (operation === undefined) throw new Error(`Operación sin especificación: ${id}`);
    return operation;
  });
  // Revisar también operaciones nuevas de la API aunque todavía ningún puerto las importe.
  expect(declarations.filter((operation) => operation.path.startsWith('/api/nodes/'))).toEqual([]);
  for (const operation of [...declarations, ...spec.operations]) {
    // API de ingesta: el nodo inicia la conexión; no es una salida de la aplicación.
    if (operation.path.startsWith('/api/nodes/')) continue;
    expect(operation.path).not.toMatch(
      /(?:^|\/)(?:machines?|plcs?|beacons?|horns?|actuators?|outputs?|commands?)(?:\/|$)/i,
    );
    if (operation.method !== 'get' && /(?:^|\/)(?:nodes?|fleet)(?:\/|$)/.test(operation.path)) {
      expect(
        NODE_WRITES.has(`${operation.method.toUpperCase()} ${operation.path}`),
        operation.operationId,
      ).toBe(true);
    }
  }
  // Las declaraciones provisionales tampoco pueden esconder un canal de actuación.
  for (const { source } of ports) {
    for (const node of nodes(source).filter(ts.isPropertyAssignment)) {
      if (node.name.getText(source) === 'operation') {
        expect(node.initializer.getText(source)).not.toMatch(
          /machine|beacon|horn|actuator|output|command/i,
        );
      }
    }
  }
}, 30_000);
