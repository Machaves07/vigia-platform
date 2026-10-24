/**
 * Reloj inyectable (PAT-APP-RES-06, herencia de PAT-RES-07 de U-01).
 *
 * Todo lo que depende del tiempo (sesión, cortacircuito, refrescos, cuentas regresivas,
 * vencimiento de URL de evidencia) recibe un `Clock`; las pruebas usan `FakeClock`
 * (`tests/support/fakeClock.ts`). Este es el único módulo que lee la hora del sistema: ESLint
 * prohíbe `Date.now()`, `new Date()` sin argumento y `performance.now()` en cualquier otro.
 */

declare const timerBrand: unique symbol;

/** Identificador opaco de un temporizador; solo sirve para `clearTimeout` del mismo reloj. */
export interface TimerHandle {
  readonly [timerBrand]: 'TimerHandle';
}

export interface Clock {
  /** Milisegundos desde la época Unix (UTC). */
  now(): number;
  /** Ejecuta `callback` una vez, pasados `delayMs` milisegundos. */
  setTimeout(callback: () => void, delayMs: number): TimerHandle;
  /** Cancela un temporizador pendiente; no hace nada si ya se ejecutó o se canceló. */
  clearTimeout(handle: TimerHandle): void;
}

/** Reloj real del navegador. */
export const systemClock: Clock = {
  now: () => Date.now(),
  setTimeout: (callback, delayMs) =>
    globalThis.setTimeout(callback, delayMs) as unknown as TimerHandle,
  clearTimeout: (handle) => {
    globalThis.clearTimeout(handle as unknown as number);
  },
};
