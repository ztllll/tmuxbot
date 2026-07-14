import { useEffect, useRef, useState } from "react";
import { Terminal } from "@xterm/xterm";
import { FitAddon } from "@xterm/addon-fit";
import "@xterm/xterm/css/xterm.css";

import {
  createTerminalTicket,
  createObservedTerminalTicket,
  releaseTerminalTakeover,
  startTerminalTakeover,
  type ManagedSession,
} from "../../app/api";

type TerminalDescriptor = Pick<ManagedSession, "id" | "name" | "tmux_target">;
type Props = { session: TerminalDescriptor; observedTarget?: string; csrfToken: string; onClose: () => void; embedded?: boolean };

function terminalFontSize(width: number): number {
  if (width <= 390) return 12;
  if (width <= 700) return 13;
  return 15;
}

// Keep ANSI colours readable without letting an application's indexed green
// fill turn into the browser's default neon terminal green.
const terminalTheme = {
  background: "#101820", foreground: "#d9e1e8", cursor: "#efb64d",
  black: "#17212b", brightBlack: "#657481",
  red: "#c95a64", brightRed: "#ee7b83",
  green: "#4f9a76", brightGreen: "#76bd96",
  yellow: "#cba35d", brightYellow: "#e6c477",
  blue: "#6f9de6", brightBlue: "#96baff",
  magenta: "#ad83d1", brightMagenta: "#c8a3ea",
  cyan: "#5caeba", brightCyan: "#81cbd5",
  white: "#c6d0d8", brightWhite: "#edf3f6",
  selectionBackground: "#3157c880",
};

export default function TerminalDock({ session, observedTarget, csrfToken, onClose, embedded = false }: Props) {
  const host = useRef<HTMLDivElement>(null);
  const socketRef = useRef<WebSocket | null>(null);
  const [mode, setMode] = useState<"observe" | "takeover">("observe");
  const modeRef = useRef<"observe" | "takeover">("observe");
  const terminalIdRef = useRef(session.id);
  const [state, setState] = useState("正在签发本机终端票据…");

  useEffect(() => {
    if (!host.current) return;
    const terminal = new Terminal({
      cursorBlink: false, convertEol: true, fontFamily: "IBM Plex Mono, monospace",
      fontSize: terminalFontSize(host.current.clientWidth), theme: terminalTheme,
    });
    const fitAddon = new FitAddon();
    terminal.loadAddon(fitAddon);
    terminal.open(host.current);
    terminal.writeln(`\x1b[33m[observe]\x1b[0m ${session.name} · ${session.tmux_target}`);
    let disposed = false;
    const fitTerminal = () => {
      const surface = host.current;
      if (!surface) return;
      const fontSize = terminalFontSize(surface.clientWidth);
      terminal.options.fontSize = fontSize;
      // Font metrics vary across Android, Samsung Internet and desktop fonts.
      // FitAddon measures the rendered glyph instead of guessing a cell size.
      fitAddon.fit();
    };
    const resizeObserver = new ResizeObserver(fitTerminal);
    resizeObserver.observe(host.current);
    fitTerminal();
    async function connectTerminal() {
      const ticket = observedTarget
        ? await createObservedTerminalTicket(observedTarget, csrfToken).then((issued) => {
          terminalIdRef.current = issued.terminal_id;
          return issued.ticket;
        })
        : await createTerminalTicket(session.id, csrfToken).then((issued) => issued.ticket);
      if (disposed) return;
      const scheme = window.location.protocol === "https:" ? "wss:" : "ws:";
      const socket = new WebSocket(`${scheme}//${window.location.host}/api/terminals/ws?ticket=${encodeURIComponent(ticket)}`);
      socket.binaryType = "arraybuffer";
      socketRef.current = socket;
      socket.onopen = () => {
        setState("已连接 · 观察模式");
        socket.send(JSON.stringify({ type: "resize", rows: terminal.rows, cols: terminal.cols }));
      };
      socket.onmessage = (event) => {
        if (event.data instanceof ArrayBuffer) terminal.write(new Uint8Array(event.data));
        else if (typeof event.data === "string") {
          try { const message = JSON.parse(event.data); if (message.reason) setState(`输入未发送：${message.reason}`); } catch { terminal.write(event.data); }
        }
      };
      socket.onclose = () => setState("终端连接已关闭；tmux 会话仍在运行");
      terminal.onData((data) => { if (modeRef.current === "takeover" && socket.readyState === WebSocket.OPEN) socket.send(new TextEncoder().encode(data)); });
      terminal.onResize(({ rows, cols }) => { if (socket.readyState === WebSocket.OPEN) socket.send(JSON.stringify({ type: "resize", rows, cols })); });
    }
    terminalIdRef.current = session.id;
    void connectTerminal().catch(() => setState("无法创建终端票据，请刷新受管会话。"));
    return () => { disposed = true; resizeObserver.disconnect(); socketRef.current?.close(); terminal.dispose(); };
  }, [csrfToken, observedTarget, session.id, session.name, session.tmux_target]);

  async function toggleTakeover() {
    if (mode === "observe") {
      try { await startTerminalTakeover(terminalIdRef.current, csrfToken); modeRef.current = "takeover"; setMode("takeover"); setState("接管模式 · 键盘输入将发送到真实 tmux pane"); }
      catch { setState("接管失败：终端未连接或已被其他控制者占用。"); }
    } else {
      try { await releaseTerminalTakeover(terminalIdRef.current, csrfToken); } catch { /* disconnected is already safe */ }
      modeRef.current = "observe"; setMode("observe"); setState("已返回观察模式");
    }
  }

  async function openNativeModelPicker() {
    if (modeRef.current !== "takeover") {
      try {
        await startTerminalTakeover(terminalIdRef.current, csrfToken);
        modeRef.current = "takeover"; setMode("takeover");
      } catch {
        setState("无法打开模型选择：终端未连接或已被其他控制者占用。");
        return;
      }
    }
    const socket = socketRef.current;
    if (!socket || socket.readyState !== WebSocket.OPEN) {
      setState("终端尚未连接，无法打开原生模型选择。");
      return;
    }
    socket.send(new TextEncoder().encode("/model\r"));
    setState("已发送 /model · 请在真实 CLI picker 中选择模型");
  }

  return <section className={embedded ? "terminal-dock terminal-tile" : "terminal-dock"} aria-label={`${session.name} 终端`}>
    <header><div><span>LIVE TMUX / {mode.toUpperCase()}</span><strong>{session.name}</strong><small>{state}</small></div>
      <div className="terminal-actions"><button className="secondary-action" onClick={() => void openNativeModelPicker()}>原生 /model</button><button className={mode === "takeover" ? "danger-action" : "primary-action"} onClick={() => void toggleTakeover()}>{mode === "takeover" ? "释放接管" : "接管键盘"}</button><button className="secondary-action" onClick={onClose}>关闭</button></div>
    </header>
    <div className="terminal-surface" ref={host} />
  </section>;
}
