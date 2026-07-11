import { useEffect, useRef, useState } from "react";
import { Terminal } from "@xterm/xterm";
import "@xterm/xterm/css/xterm.css";

import {
  createTerminalTicket,
  releaseTerminalTakeover,
  startTerminalTakeover,
  type ManagedSession,
} from "../../app/api";

type Props = { session: ManagedSession; csrfToken: string; onClose: () => void };

export default function TerminalDock({ session, csrfToken, onClose }: Props) {
  const host = useRef<HTMLDivElement>(null);
  const socketRef = useRef<WebSocket | null>(null);
  const [mode, setMode] = useState<"observe" | "takeover">("observe");
  const modeRef = useRef<"observe" | "takeover">("observe");
  const [state, setState] = useState("正在签发本机终端票据…");

  useEffect(() => {
    if (!host.current) return;
    const terminal = new Terminal({
      cursorBlink: false, convertEol: true, fontFamily: "IBM Plex Mono, monospace",
      fontSize: 13, theme: { background: "#101820", foreground: "#d9e1e8", cursor: "#d59620" },
    });
    terminal.open(host.current);
    terminal.writeln(`\x1b[33m[observe]\x1b[0m ${session.name} · ${session.tmux_target}`);
    let disposed = false;
    void createTerminalTicket(session.id, csrfToken).then(({ ticket }) => {
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
    }).catch(() => setState("无法创建终端票据，请刷新受管会话。"));
    return () => { disposed = true; socketRef.current?.close(); terminal.dispose(); };
  }, [csrfToken, session.id, session.name, session.tmux_target]);

  async function toggleTakeover() {
    if (mode === "observe") {
      try { await startTerminalTakeover(session.id, csrfToken); modeRef.current = "takeover"; setMode("takeover"); setState("接管模式 · 键盘输入将发送到真实 tmux pane"); }
      catch { setState("接管失败：终端未连接或已被其他控制者占用。"); }
    } else {
      try { await releaseTerminalTakeover(session.id, csrfToken); } catch { /* disconnected is already safe */ }
      modeRef.current = "observe"; setMode("observe"); setState("已返回观察模式");
    }
  }

  return <section className="terminal-dock" aria-label={`${session.name} 终端`}>
    <header><div><span>LIVE TMUX / {mode.toUpperCase()}</span><strong>{session.name}</strong><small>{state}</small></div>
      <div className="terminal-actions"><button className={mode === "takeover" ? "danger-action" : "primary-action"} onClick={() => void toggleTakeover()}>{mode === "takeover" ? "释放接管" : "接管键盘"}</button><button className="secondary-action" onClick={onClose}>关闭视图</button></div>
    </header>
    <div className="terminal-surface" ref={host} />
  </section>;
}
