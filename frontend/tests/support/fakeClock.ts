/**
 * Reloj simulado para las pruebas (PAT-APP-RES-06): el tiempo solo avanza con `advance`.
 *
 * Los temporizadores vencidos se ejecutan en orden de vencimiento y, a igual vencimiento, en el
 * orden en que se programaron. Un temporizador programado durante `advance` se ejecuta en esa
 * misma llamada si vence dentro de la ventana; `now()` vale su vencimiento mientras se ejecuta.
 */
import type { Clock, TimerHandle } from '@/state/clock';

interface PendingTimer {
  readonly id: number;
  readonly dueMs: number;
  readonly callback: () => void;
}

export class FakeClock implements Clock {
  #nowMs: number;
  #nextId = 1;
  readonly #pending = new Map<number, PendingTimer>();

  constructor(startMs = 0) {
    if (!Number.isSafeInteger(startMs)) throw new RangeError('startMs debe ser un entero seguro.');
    this.#nowMs = startMs;
  }

  now(): number {
    return this.#nowMs;
  }

  setTimeout(callback: () => void, delayMs: number): TimerHandle {
    const id = this.#nextId++;
    const delay = Number.isFinite(delayMs) && delayMs > 0 ? Math.floor(delayMs) : 0;
    this.#pending.set(id, { id, dueMs: this.#nowMs + delay, callback });
    return id as unknown as TimerHandle;
  }

  clearTimeout(handle: TimerHandle): void {
    this.#pending.delete(handle as unknown as number);
  }

  /** Temporizadores aún pendientes. */
  get pendingCount(): number {
    return this.#pending.size;
  }

  /** Avanza `ms` milisegundos y ejecuta en orden lo que vence dentro de la ventana. */
  advance(ms: number): void {
    if (!Number.isSafeInteger(ms) || ms < 0) {
      throw new RangeError('ms debe ser un entero no negativo.');
    }
    const targetMs = this.#nowMs + ms;
    for (let next = this.#nextDue(targetMs); next !== undefined; next = this.#nextDue(targetMs)) {
      this.#pending.delete(next.id);
      this.#nowMs = next.dueMs;
      next.callback();
    }
    this.#nowMs = targetMs;
  }

  #nextDue(targetMs: number): PendingTimer | undefined {
    let best: PendingTimer | undefined;
    for (const timer of this.#pending.values()) {
      if (timer.dueMs > targetMs) continue;
      if (best === undefined || timer.dueMs < best.dueMs) best = timer;
    }
    return best;
  }
}
