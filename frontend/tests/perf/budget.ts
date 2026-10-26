/**
 * Presupuestos de rendimiento de la interfaz (NFR-APP-03, PAT-APP-REN-04): cifras `[objetivo
 * propio]` con método de medición, no promesas. En el perfil `ci` el umbral es **tendencia**: la
 * cifra se informa y no falla, porque el ejecutor compartido mide con ruido. En `nightly`
 * (`npm run test:nightly`, `FC_PROFILE=nightly`) el umbral es **bloqueante** (TASK-455).
 */
import { expect } from 'vitest';

export type BudgetMode = 'trend' | 'blocking';

/** `blocking` solo con el perfil `nightly`; cualquier otro perfil informa la tendencia. */
function budgetMode(env: Readonly<Record<string, string | undefined>>): BudgetMode {
  return env.FC_PROFILE === 'nightly' ? 'blocking' : 'trend';
}

export interface BudgetResult {
  readonly name: string;
  readonly measuredMs: number;
  readonly budgetMs: number;
  readonly mode: BudgetMode;
  readonly withinBudget: boolean;
}

/** Línea del informe: una por medición, con prefijo fijo para que el `nightly` la recoja. */
function formatBudget(result: BudgetResult): string {
  const verdict = result.withinBudget ? 'dentro' : 'FUERA';
  return `[perf] ${result.name}: ${String(result.measuredMs)} ms (presupuesto ${String(result.budgetMs)} ms, ${result.mode}, ${verdict})\n`;
}

/**
 * Informa `measuredMs` frente a `budgetMs` y, en modo `blocking`, falla si lo supera. Devuelve el
 * resultado para que la prueba pueda comprobar además que la medición existe.
 */
export function checkBudget(
  name: string,
  measuredMs: number,
  budgetMs: number,
  mode: BudgetMode = budgetMode(process.env),
): BudgetResult {
  const result: BudgetResult = {
    name,
    measuredMs: Math.round(measuredMs * 10) / 10,
    budgetMs,
    mode,
    withinBudget: measuredMs <= budgetMs,
  };
  process.stdout.write(formatBudget(result));
  if (mode === 'blocking') {
    expect(measuredMs, `${name}: presupuesto ${String(budgetMs)} ms`).toBeLessThanOrEqual(budgetMs);
  }
  return result;
}
