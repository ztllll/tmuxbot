export type TmuxWindow = { target: string; session_name: string; window_index: number; pane_count: number; commands: string[]; cwd_summary: string };
export async function getTmuxWindows(): Promise<TmuxWindow[]> { const response = await fetch("/api/tmux/windows"); if (!response.ok) throw new Error("tmux inventory unavailable"); return response.json() as Promise<TmuxWindow[]>; }
