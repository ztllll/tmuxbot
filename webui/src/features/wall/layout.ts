export type TerminalLayout = { target: string; x: number; y: number; width: number; height: number; z: number };
export type WorkspaceLayout = { version: 1; cards: TerminalLayout[] };
export const STORAGE_KEY = "tmuxbot-terminal-wall-v1";

export function loadLayout(): WorkspaceLayout {
  try {
    const stored: unknown = JSON.parse(localStorage.getItem(STORAGE_KEY) || "null");
    if (!stored || typeof stored !== "object" || (stored as { version?: unknown }).version !== 1 || !Array.isArray((stored as { cards?: unknown }).cards)) return { version: 1, cards: [] };
    const cards = (stored as { cards: unknown[] }).cards.filter((card): card is TerminalLayout => Boolean(card) && typeof card === "object" && typeof (card as { target?: unknown }).target === "string" && ["x", "y", "width", "height", "z"].every((field) => typeof (card as Record<string, unknown>)[field] === "number"));
    return { version: 1, cards };
  } catch { return { version: 1, cards: [] }; }
}
export function saveLayout(layout: WorkspaceLayout) { localStorage.setItem(STORAGE_KEY, JSON.stringify(layout)); }
