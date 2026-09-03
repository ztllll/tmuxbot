import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { TmuxWindow } from "../../app/api";
import TerminalCard from "./TerminalCard";
import { loadLayout, saveLayout, type TerminalLayout, type WorkspaceLayout } from "./layout";

type Props = { windows: TmuxWindow[]; onRefresh: () => void };
const MARGIN = 12;
const GAP = 8;

export default function TerminalWall({ windows, onRefresh }: Props) {
  const [layout, setLayout] = useState<WorkspaceLayout>(loadLayout);
  const [fullscreen, setFullscreen] = useState(false);
  const [sidebarHidden, setSidebarHidden] = useState(() => localStorage.getItem("tmuxbot-wall-sidebar-hidden") === "true");
  const canvasRef = useRef<HTMLDivElement>(null);
  const known = useMemo(() => new Set(windows.map((item) => item.target)), [windows]);
  useEffect(() => { localStorage.setItem("tmuxbot-wall-sidebar-hidden", String(sidebarHidden)); }, [sidebarHidden]);
  useEffect(() => { const id = setTimeout(() => saveLayout(layout), 100); return () => clearTimeout(id); }, [layout]);

  const arrange = useCallback((kind: "row" | "column" | "grid" = "grid") => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    setLayout((current) => {
      const count = current.cards.length;
      if (!count) return current;
      const columns = kind === "column" ? 1 : kind === "row" ? count : Math.ceil(Math.sqrt(count));
      const rows = Math.ceil(count / columns);
      const width = Math.max(340, Math.floor((canvas.clientWidth - MARGIN * 2 - GAP * (columns - 1)) / columns));
      const height = Math.max(240, Math.floor((canvas.clientHeight - MARGIN * 2 - GAP * (rows - 1)) / rows));
      return { ...current, cards: current.cards.map((card, index) => ({ ...card, x: MARGIN + (index % columns) * (width + GAP), y: MARGIN + Math.floor(index / columns) * (height + GAP), width, height, z: index + 1 })) };
    });
  }, []);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    let timer = 0;
    const reflow = () => { window.clearTimeout(timer); timer = window.setTimeout(() => arrange(), 180); };
    const observer = new ResizeObserver(reflow);
    observer.observe(canvas);
    return () => { observer.disconnect(); window.clearTimeout(timer); };
  }, [arrange]);
  useEffect(() => { const timer = window.setTimeout(() => arrange(), 180); return () => window.clearTimeout(timer); }, [sidebarHidden, fullscreen, arrange]);

  const raise = (target: string) => setLayout((current) => { const z = Math.max(0, ...current.cards.map((item) => item.z)) + 1; return { ...current, cards: current.cards.map((item) => item.target === target ? { ...item, z } : item) }; });
  const open = (target: string) => {
    if (layout.cards.some((card) => card.target === target)) { raise(target); return; }
    setLayout((current) => ({ ...current, cards: [...current.cards, { target, x: MARGIN, y: MARGIN, width: 720, height: 480, z: Math.max(0, ...current.cards.map((item) => item.z)) + 1 }] }));
    window.setTimeout(() => arrange(), 0);
  };
  const update = (next: TerminalLayout) => setLayout((current) => ({ ...current, cards: current.cards.map((card) => card.target === next.target ? next : card) }));
  const close = (target: string) => setLayout((current) => ({ ...current, cards: current.cards.filter((card) => card.target !== target) }));
  const groups = windows.reduce<Record<string, TmuxWindow[]>>((all, item) => { (all[item.session_name] ||= []).push(item); return all; }, {});
  return <main className={`terminal-wall ${fullscreen ? "is-fullscreen" : ""} ${sidebarHidden ? "sidebar-hidden" : ""}`}><aside className="wall-sidebar"><header><div><span>TMUX TERMINAL WALL</span><h1>本机终端墙</h1></div><button onClick={onRefresh}>刷新</button></header><p>Control Mode 直连真实 tmux pane；当前 Web 卡片就是终端，缩放会像 SSH/Tabby 一样重排真实 TUI。</p><nav>{Object.entries(groups).map(([session, items]) => <section key={session}><h2>{session}</h2>{items.map((item) => <button className="wall-entry" key={item.target} onClick={() => open(item.target)}><strong>{item.target}</strong><small>{item.pane_count} pane · {item.commands.join(" / ") || "shell"}</small><small>{item.cwd_summary || "路径未知"}</small></button>)}</section>)}</nav></aside><section className="wall-main"><header className="wall-toolbar"><div><button className="sidebar-toggle" onClick={() => setSidebarHidden((value) => !value)}>{sidebarHidden ? "显示侧栏" : "隐藏侧栏"}</button><strong>{layout.cards.length} 个 window</strong><span>拖标题移动 · 拖边缩放</span></div><div><button onClick={() => arrange("row")}>横向排列</button><button onClick={() => arrange("column")}>纵向排列</button><button onClick={() => arrange("grid")}>网格排列</button><button onClick={() => setLayout({ version: 1, cards: [] })}>清空</button><button onClick={() => setFullscreen((value) => !value)}>{fullscreen ? "退出全屏" : "全屏"}</button></div></header><div className="wall-canvas" ref={canvasRef}>{layout.cards.length === 0 && <p className="wall-empty">从左侧选择 tmux window，开始组成终端墙。</p>}{layout.cards.map((card) => <TerminalCard key={card.target} card={card} unavailable={!known.has(card.target)} onFocus={() => raise(card.target)} onClose={() => close(card.target)} onChange={update} />)}</div></section></main>;
}
