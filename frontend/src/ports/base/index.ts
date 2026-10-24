/**
 * `ports.base` (LC-APP-13 y 14): el transporte único (`http.ts`), su registro de desarrollo
 * (`devLog.ts`) y el marco de los puertos de pantalla (`defineScreenPort.ts`). Lo importan los
 * puertos (`src/ports/scrNN*`, `src/ports/shared/`) y el arranque; nunca una página (BR-APP-62).
 * Cada tarea que conecta un proveedor (VIG-300, 301 y 302) exporta aquí lo que use.
 */
export {
  API_VERSION_HEADER,
  CONCESSION_HEADER,
  createPortConnection,
  createTransport,
  installTransport,
  isPortError,
  PortError,
  READ_RETRY_DELAYS_MS,
  TIMEOUT_MS,
  type OperationId,
  type RequestKind,
  type TransportConnection,
  type TransportOptions,
} from './http';
export { createDevLog, type DevLogEntry } from './devLog';
export {
  defineScreenPort,
  SLOW_AFTER_MS,
  type Declaration,
  type ScreenPort,
} from './defineScreenPort';
