import { useEffect, useState } from "react";
import { getTmuxWindows, type TmuxWindow } from "./api";
import TerminalWall from "../features/wall/TerminalWall";

export default function App() {
  const [windows, setWindows] = useState<TmuxWindow[]>([]);
  const [failed, setFailed] = useState(false);
  async function refresh() { try { setWindows(await getTmuxWindows()); setFailed(false); } catch { setFailed(true); } }
  useEffect(() => { void refresh(); }, []);
  if (failed) return <main className="wall-error"><strong>无法读取本机 tmux inventory。</strong><button onClick={() => void refresh()}>重新读取</button></main>;
  return <TerminalWall windows={windows} onRefresh={() => void refresh()} />;
}
