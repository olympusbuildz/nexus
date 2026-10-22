import '@testing-library/jest-dom/vitest';
import { afterEach, vi } from 'vitest';

Element.prototype.scrollIntoView = function scrollIntoView() {
  /* noop */
};

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  localStorage.clear();
  sessionStorage.clear();
});
