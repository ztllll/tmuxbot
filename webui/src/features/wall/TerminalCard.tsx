import { useEffect, useRef, useState } from "react";
import { Terminal } from "@xterm/xterm";
import { FitAddon } from "@xterm/addon-fit";
import "@xterm/xterm/css/xterm.css";
import type { TerminalLayout } from "./layout";

const terminalTheme = { background: "#101820", foreground: "#d9e1e8", cursor: "#efb64d", black: "#17212b", brightBlack: "#657481", red: "#c95a64", brightRed: "#ee7b83", green: "#4f9a76", brightGreen: "#76bd96", yellow: "#cba35d", brightYellow: "#e6c477", blue: "#6f9de6", brightBlue: "#96baff", magenta: "#ad83d1", brightMagenta: "#c8a3ea", cyan: "#5caeba", brightCyan: "#81cbd5", white: "#c6d0d8", brightWhite: "#edf3f6" };

type Props = { card: TerminalLayout; unavailable: boolean; onFocus: () => void; onClose: () => void; onChange: (next: TerminalLayout) => void };

export default function TerminalCard({ card, unavailable, onFocus, onClose, onChange }: Props) {
  const host = useRef<HTMLDivElement>(null);
  const [state, setState] = useState(unavailable ? "目标已失效" : "正在连接");
  const [ownsSize, setOwnsSize] = useState(false);
  const resizeTimer = useRef<number | undefined>(undefined);
  const terminalRef = useRef<Terminal | null>(null);
  const fitRef = useRef<FitAddon | null>(null);
  useEffect(() => {
    if (unavailable || !host.current) return;
    const terminal = new Terminal({ cursorBlink: true, convertEol: true, fontFamily: "IBM Plex Mono, SFMono-Regular, Consolas, monospace", fontSize: 14, theme: terminalTheme });
    const fit = new FitAddon(); terminal.loadAddon(fit); terminal.open(host.current);
    terminalRef.current = terminal; fitRef.current = fit;
    let disposed = false;
    const scheme = location.protocol === "https:" ? "wss:" : "ws:";
    const ws = new WebSocket(`${scheme}//${location.host}/api/wall/ws?target=${encodeURIComponent(card.target)}`);
    ws.binaryType = "arraybuffer";
    const resize = () => {
      fit.fit();
      if (resizeTimer.current !== undefined) window.clearTimeout(resizeTimer.current);
      resizeTimer.current = window.setTimeout(() => {
        if (ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ type: "resize", rows: terminal.rows, cols: terminal.cols, apply_window: ownsSize }));
      }, 80);
    };
    const observer = new ResizeObserver(resize); observer.observe(host.current);
    ws.onopen = () => { if (!disposed) { setState("已连接"); resize(); terminal.focus(); } };
    ws.onmessage = (event) => {
      if (event.data instanceof ArrayBuffer) { terminal.write(new Uint8Array(event.data)); return; }
      if (typeof event.data !== "string") return;
      try {
        const message = JSON.parse(event.data) as { type?: string; cols?: number; rows?: number };
        if (message.type !== "window_size" || !message.cols || !message.rows) return;
        terminal.resize(message.cols, message.rows);
        const surface = host.current;
        if (surface) {
          const cellWidth = surface.clientWidth / message.cols;
          const cellHeight = surface.clientHeight / message.rows;
          terminal.options.fontSize = Math.max(8, Math.min(18, Math.floor(Math.min(cellWidth / 0.61, cellHeight / 1.2) * 10) / 10));
        }
      } catch { /* terminal byte stream is the only expected non-JSON payload */ }
    };
    ws.onclose = (event) => { if (!disposed) setState(event.code === 4404 ? "目标已失效" : "连接已断开"); };
    terminal.onData((data) => { if (ws.readyState === WebSocket.OPEN) ws.send(new TextEncoder().encode(data)); });
    return () => { disposed = true; observer.disconnect(); if (resizeTimer.current !== undefined) window.clearTimeout(resizeTimer.current); ws.close(); terminalRef.current = null; fitRef.current = null; terminal.dispose(); };
  }, [card.target, unavailable, ownsSize]);

  function pointer(event: React.PointerEvent, action: "move" | "resize") {
    event.preventDefault(); onFocus();
    const start = { x: event.clientX, y: event.clientY, card: { ...card }, direction: event.currentTarget.getAttribute("data-direction") || "se" };
    const move = (next: PointerEvent) => {
      const dx = next.clientX - start.x, dy = next.clientY - start.y;
      if (action === "move") return onChange({ ...start.card, x: Math.max(0, start.card.x + dx), y: Math.max(0, start.card.y + dy) });
      const west = start.direction.includes("w"), north = start.direction.includes("n");
      const width = Math.max(340, start.card.width + (west ? -dx : dx)), height = Math.max(240, start.card.height + (north ? -dy : dy));
      onChange({ ...start.card, width, height, x: west ? start.card.x + start.card.width - width : start.card.x, y: north ? start.card.y + start.card.height - height : start.card.y });
    };
    const end = () => { window.removeEventListener("pointermove", move); window.removeEventListener("pointerup", end); };
    window.addEventListener("pointermove", move); window.addEventListener("pointerup", end);
  }
  function toggleSizeOwner() {
    const next = !ownsSize;
    setOwnsSize(next);
    setState(next ? "尺寸接管 · 将影响 SSH/Tabby" : "镜像模式 · 不改变共享 tmux 尺寸");
  }
  return <article className={`wall-card ${unavailable ? "is-unavailable" : ""}`} style={{ left: card.x, top: card.y, width: card.width, height: card.height, zIndex: card.z }} onPointerDown={onFocus}>
    <header onPointerDown={(event) => pointer(event, "move")}><div><strong>{card.target}</strong><small>{state}</small></div><div className="wall-card-actions"><button className={ownsSize ? "is-owner" : ""} title="接管会改变同一 tmux window 的 SSH/Tabby 尺寸" onPointerDown={(event) => event.stopPropagation()} onClick={toggleSizeOwner}>{ownsSize ? "释放尺寸" : "接管尺寸"}</button><button aria-label={`关闭 ${card.target}`} onPointerDown={(event) => event.stopPropagation()} onClick={onClose}>×</button></div></header>
    <div className="wall-terminal" ref={host}>{unavailable && <p>该 tmux window 已不存在。</p>}</div>
    {["n", "e", "s", "w", "ne", "se", "sw", "nw"].map((direction) => <i key={direction} className={`resize-handle ${direction}`} data-direction={direction} onPointerDown={(event) => pointer(event, "resize")} />)}
  </article>;
}
