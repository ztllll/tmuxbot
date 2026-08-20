import "@testing-library/jest-dom/vitest";

Object.defineProperty(window, "matchMedia", {
  writable: true,
  value: (query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    addListener: () => undefined,
    removeListener: () => undefined,
    addEventListener: () => undefined,
    removeEventListener: () => undefined,
    dispatchEvent: () => false,
  }),
});

class ResizeObserverStub {
  observe() { return undefined; }
  disconnect() { return undefined; }
}
Object.defineProperty(window, "ResizeObserver", { writable: true, value: ResizeObserverStub });
Object.defineProperty(globalThis, "ResizeObserver", { writable: true, value: ResizeObserverStub });

afterEach(() => {
  vi.restoreAllMocks();
  window.history.replaceState({}, "", "/");
});
