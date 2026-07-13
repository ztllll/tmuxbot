import { useState } from "react";

import type { ManagedSession } from "../../app/api";
import TerminalDock from "./TerminalDock";

export type WorkspaceTerminal = {
  key: string;
  session: ManagedSession;
  observedTarget?: string;
};

export default function TerminalWorkspace({ terminals, csrfToken, onClose }: {
  terminals: WorkspaceTerminal[];
  csrfToken: string;
  onClose: (key: string) => void;
}) {
  const [fullscreen, setFullscreen] = useState(false);
  const [layout, setLayout] = useState<"auto" | "one" | "two">("auto");
  if (terminals.length === 0) return null;
  const columns = layout === "one" ? "one" : layout === "two" ? "two" : terminals.length === 1 ? "one" : "two";
  return <section className={`terminal-workspace ${fullscreen ? "is-fullscreen" : ""}`} aria-label="tmux 多窗口工作区">
    <header className="workspace-head"><div><span>TMUX WORKSPACE</span><strong>{terminals.length} 个终端 · 不同 pane 可分别接管</strong></div><div className="workspace-actions"><button className={layout === "one" ? "primary-action" : "secondary-action"} onClick={() => setLayout("one")}>单列</button><button className={layout === "two" ? "primary-action" : "secondary-action"} onClick={() => setLayout("two")}>双列分屏</button><button className={layout === "auto" ? "primary-action" : "secondary-action"} onClick={() => setLayout("auto")}>自动</button><button className="secondary-action" onClick={() => setFullscreen(!fullscreen)}>{fullscreen ? "退出全屏" : "全屏"}</button></div></header>
    <div className={`terminal-grid is-${columns}`}>{terminals.map((terminal) => <TerminalDock key={terminal.key} embedded session={terminal.session} observedTarget={terminal.observedTarget} csrfToken={csrfToken} onClose={() => onClose(terminal.key)} />)}</div>
  </section>;
}
