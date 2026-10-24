/** Testing Library: desmonta lo renderizado al terminar cada prueba. */
import { cleanup } from '@testing-library/react';
import { afterEach } from 'vitest';

afterEach(() => {
  cleanup();
});
