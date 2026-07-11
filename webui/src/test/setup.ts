import "@testing-library/jest-dom/vitest";

afterEach(() => {
  vi.restoreAllMocks();
  window.history.replaceState({}, "", "/");
});
