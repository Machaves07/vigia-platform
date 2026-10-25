import { expect, type BrowserContext } from '@playwright/test';

/** Cubre páginas, marcos, ventanas nuevas y navegación, antes del primer guion de la app. */
export async function watchPolicy(context: BrowserContext): Promise<() => void> {
  const violations: string[] = [];
  await context.exposeBinding('__vigiaPolicyViolation', (_source, directive: string) => {
    violations.push(directive);
  });
  await context.addInitScript(() => {
    document.addEventListener('securitypolicyviolation', (event) => {
      const report = (
        globalThis as unknown as {
          __vigiaPolicyViolation: (directive: string) => Promise<void>;
        }
      ).__vigiaPolicyViolation;
      void report(event.effectiveDirective);
    });
  });
  context.on('page', (page) => {
    page.on('console', (message) => {
      const text = message.text();
      if (
        /(?:content.security.policy|trusted.?types?|trustedhtml|trustedscript)/i.test(text) &&
        /(?:refused|violat|blocked|requires|requirement|sink)/i.test(text)
      ) {
        // No copiar el mensaje: podría contener entradas o URL con credenciales.
        violations.push('console: CSP/Trusted Types');
      }
    });
    page.on('pageerror', (error) => {
      if (/trustedhtml|trustedscript|trusted.?types/i.test(error.message)) {
        violations.push('pageerror: Trusted Types');
      }
    });
  });
  return () => {
    expect(violations, 'Violaciones de CSP o Trusted Types').toEqual([]);
  };
}
