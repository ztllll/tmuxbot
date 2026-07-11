import { useState, type FormEvent } from "react";

import {
  commandTeamRun, completeTeamTask, createTeamRun, getTeamRun, reviewTeamTask,
  type ManagedSession, type TeamRunSnapshot,
} from "../../app/api";

export default function TeamRunPanel({ sessions, csrfToken }: { sessions: ManagedSession[]; csrfToken: string }) {
  const [goal, setGoal] = useState("");
  const [coordinator, setCoordinator] = useState("");
  const [implementer, setImplementer] = useState("");
  const [reviewer, setReviewer] = useState("");
  const [snapshot, setSnapshot] = useState<TeamRunSnapshot | null>(null);
  const [notice, setNotice] = useState("第一版采用确定性三角色：统筹、唯一写者、独立审核。 ");
  const [busy, setBusy] = useState(false);

  async function create(event: FormEvent) {
    event.preventDefault(); setBusy(true);
    const runId = `run-${Date.now()}`;
    try {
      const created = await createTeamRun({ runId, goal, coordinator, implementer, reviewer }, csrfToken);
      const started = await commandTeamRun(created.run.run_id, "start", csrfToken);
      setSnapshot(started); setNotice("TeamRun 已启动：Implementer 已收到结构化任务。完成后登记证据，Reviewer 会收到独立审查包。");
    } catch { setNotice("TeamRun 启动失败：三个角色必须绑定三个不同的受管会话。"); }
    finally { setBusy(false); }
  }

  async function refresh() { if (snapshot) setSnapshot(await getTeamRun(snapshot.run.run_id)); }
  async function complete() {
    if (!snapshot) return;
    const uri = window.prompt("输入实现证据路径或 commit URI", "git:HEAD"); if (!uri) return;
    await completeTeamTask(snapshot.run.run_id, uri, csrfToken); await refresh();
    setNotice("实现已进入 review；独立 Reviewer 会话已收到审查包。");
  }
  async function review(verdict: "approved" | "rejected") {
    if (!snapshot) return;
    const notes = window.prompt("填写 Reviewer 结论", verdict === "approved" ? "证据与验收通过" : "需要修改") || "";
    await reviewTeamTask(snapshot.run.run_id, verdict, notes, csrfToken); await refresh();
    setNotice(verdict === "approved" ? "独立验收通过，TeamRun 可完成。" : "已退回 Implementer，调度器按上限重试。");
  }

  const task = snapshot?.tasks[0];
  return <section className="teamrun-panel">
    <header><div><span>MULTI-LLM / DETERMINISTIC</span><h2>TeamRun 协作台</h2></div>{snapshot && <strong className={`run-state is-${snapshot.run.state}`}>{snapshot.run.state}</strong>}</header>
    <p className="operator-notice">{notice}</p>
    {!snapshot ? <form className="teamrun-form" onSubmit={create}>
      <label className="goal-field"><span>统一目标</span><textarea value={goal} onChange={(e) => setGoal(e.target.value)} placeholder="例如：实现登录模块，由 Codex 编码，Claude 独立审核" required /></label>
      {[{ label: "Coordinator / 统筹", value: coordinator, set: setCoordinator }, { label: "Implementer / 唯一写者", value: implementer, set: setImplementer }, { label: "Reviewer / 独立审核", value: reviewer, set: setReviewer }].map((role) => <label key={role.label}><span>{role.label}</span><select value={role.value} onChange={(e) => role.set(e.target.value)} required><option value="">请选择受管 CLI</option>{sessions.map((s) => <option key={s.id} value={s.id}>{s.name} · {s.tmux_target}</option>)}</select></label>)}
      <button className="primary-action" disabled={busy || sessions.length < 3}>{busy ? "正在调度…" : "创建并启动 TeamRun"}</button>
      {sessions.length < 3 && <small>请先在上方创建至少 3 个受管 CLI 会话。</small>}
    </form> : <div className="run-console">
      <div className="run-goal"><span>GOAL</span><strong>{snapshot.run.goal}</strong></div>
      <div className="task-track"><span className={`status-mark is-${task?.state || "unknown"}`} /><div><strong>{task?.title}</strong><small>状态 {task?.state} · 尝试 {task?.attempt}</small></div></div>
      <div className="run-actions"><button className="secondary-action" onClick={() => void refresh()}>刷新状态</button>{task?.state === "working" && <button className="primary-action" onClick={() => void complete()}>登记实现证据</button>}{task?.state === "review" && <><button className="primary-action" onClick={() => void review("approved")}>Reviewer 通过</button><button className="danger-action" onClick={() => void review("rejected")}>退回修改</button></>}</div>
    </div>}
  </section>;
}
