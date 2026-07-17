import { useEffect, useState, type FormEvent } from "react";

import {
  commandTeamRun, completeTeamTask, createTeamRun, getDispatchReceipts, getTeamRun, getTeamRunEvents, reviewTeamTask,
  type DispatchReceipt, type ManagedSession, type TeamRunEvent, type TeamRunSnapshot, type TeamRunSummary, type TeamTaskInput,
} from "../../app/api";

type DraftTask = TeamTaskInput;

function defaultTask(goal = ""): DraftTask {
  return { taskId: "implementation", title: "实现与验证", goal, role: "implementer", dependencies: [], requiresWrite: true };
}

export default function TeamRunPanel({ sessions, csrfToken, runs, onRefresh }: {
  sessions: ManagedSession[];
  csrfToken: string;
  runs: TeamRunSummary[];
  onRefresh: () => Promise<void>;
}) {
  const [goal, setGoal] = useState("");
  const [coordinator, setCoordinator] = useState("");
  const [implementer, setImplementer] = useState("");
  const [reviewer, setReviewer] = useState("");
  const [tasks, setTasks] = useState<DraftTask[]>([defaultTask()]);
  const [snapshot, setSnapshot] = useState<TeamRunSnapshot | null>(null);
  const [notice, setNotice] = useState("创建任务图后，统筹、实施、审查会在各自 tmux 会话中接力。写入任务始终串行执行。");
  const [busy, setBusy] = useState(false);
  const [reviewingTask, setReviewingTask] = useState<string | null>(null);
  const [reviewNotes, setReviewNotes] = useState("");
  const [events, setEvents] = useState<TeamRunEvent[]>([]);
  const [dispatches, setDispatches] = useState<DispatchReceipt[]>([]);

  useEffect(() => {
    if (snapshot) return;
    const active = runs.find((run) => ["draft", "running", "paused", "operator_required"].includes(run.state));
    if (active) void getTeamRun(active.run_id).then(setSnapshot).catch(() => undefined);
  }, [runs, snapshot]);

  function updateTask(index: number, patch: Partial<DraftTask>) {
    setTasks((current) => current.map((task, position) => position === index ? { ...task, ...patch } : task));
  }

  async function create(event: FormEvent) {
    event.preventDefault();
    const normalized = tasks.map((task) => ({ ...task, goal: task.goal.trim() || goal.trim() }));
    if (!goal.trim() || normalized.some((task) => !task.title.trim() || !task.goal.trim())) return;
    setBusy(true);
    const runId = `run-${Date.now()}`;
    try {
      const created = await createTeamRun({ runId, goal: goal.trim(), coordinator, implementer, reviewer, tasks: normalized }, csrfToken);
      const started = await commandTeamRun(created.run.run_id, "start", csrfToken);
      setSnapshot(started); setNotice(`已启动 ${normalized.length} 个任务；依赖满足后会自动进入下一个 tmux CLI。`);
      await onRefresh();
    } catch { setNotice("TeamRun 启动失败：三个角色必须是不同受管 CLI，任务编号也不能重复。"); }
    finally { setBusy(false); }
  }

  async function refresh() {
    if (!snapshot) return;
    const [next, nextEvents, nextDispatches] = await Promise.all([
      getTeamRun(snapshot.run.run_id), getTeamRunEvents(snapshot.run.run_id), getDispatchReceipts(snapshot.run.run_id),
    ]);
    setSnapshot(next); setEvents(nextEvents); setDispatches(nextDispatches);
  }
  useEffect(() => { if (snapshot) void refresh().catch(() => undefined); }, [snapshot?.run.run_id]);
  async function complete(taskId: string, agentId: string | null | undefined) {
    if (!snapshot) return;
    if (!agentId) { setNotice("任务尚未分配给 CLI，无法登记证据。"); return; }
    const artifactUri = window.prompt("输入该任务的证据路径或 commit URI", "git:HEAD");
    if (!artifactUri) return;
    setBusy(true);
    try { await completeTeamTask(snapshot.run.run_id, taskId, agentId, artifactUri, csrfToken); await refresh(); setNotice("证据已登记，Reviewer 已收到独立审查任务。"); }
    catch { setNotice("证据登记失败：任务状态可能已经变化，请刷新后重试。"); }
    finally { setBusy(false); }
  }
  async function review(taskId: string, verdict: "approved" | "rejected") {
    if (!snapshot) return;
    setBusy(true);
    try { await reviewTeamTask(snapshot.run.run_id, taskId, verdict, reviewNotes, csrfToken); setReviewingTask(null); setReviewNotes(""); await refresh(); setNotice(verdict === "approved" ? "已通过审查，后续依赖任务会自动推进。" : "已退回实施者，调度器将按任务重试上限处理。"); }
    catch { setNotice("审查提交失败：任务状态可能已经变化，请刷新后重试。"); }
    finally { setBusy(false); }
  }

  return <section className="teamrun-panel" id="teamrun">
    <header><div><span>MULTI-LLM / 协作调度</span><h2>TeamRun 协作台</h2></div>{snapshot && <strong className={`run-state is-${snapshot.run.state}`}>{snapshot.run.state}</strong>}</header>
    <p className="operator-notice" role="status">{notice}</p>
    {!snapshot ? <form className="teamrun-form" onSubmit={create}>
      <label className="goal-field"><span>统一目标</span><textarea value={goal} onChange={(event) => { setGoal(event.target.value); if (tasks.length === 1 && !tasks[0].goal) updateTask(0, { goal: event.target.value }); }} placeholder="例如：完成 WebUI 调度页，由 Codex 编码，Claude 审查" required /></label>
      {[{ label: "统筹 CLI", value: coordinator, set: setCoordinator }, { label: "实施 CLI（唯一写者）", value: implementer, set: setImplementer }, { label: "审查 CLI", value: reviewer, set: setReviewer }].map((role) => <label key={role.label}><span>{role.label}</span><select value={role.value} onChange={(event) => role.set(event.target.value)} required><option value="">请选择受管 CLI</option>{sessions.map((session) => <option key={session.id} value={session.id}>{session.name} · {session.provider || "CLI"}</option>)}</select></label>)}
      <div className="task-composer"><div className="task-composer-head"><strong>任务图</strong><button className="secondary-action" type="button" onClick={() => setTasks((current) => [...current, { ...defaultTask(goal), taskId: `task-${current.length + 1}`, title: `任务 ${current.length + 1}`, dependencies: current.length ? [current[current.length - 1].taskId] : [] }])}>新增任务</button></div>{tasks.map((task, index) => <article className="task-editor" key={`${task.taskId}-${index}`}><label><span>任务名称</span><input value={task.title} onChange={(event) => updateTask(index, { title: event.target.value })} required /></label><label><span>任务目标</span><input value={task.goal} onChange={(event) => updateTask(index, { goal: event.target.value })} required /></label><label><span>执行角色</span><select value={task.role} onChange={(event) => { const role = event.target.value as DraftTask["role"]; updateTask(index, { role, requiresWrite: role === "implementer" ? task.requiresWrite : false }); }}><option value="coordinator">统筹 / 方案设计</option><option value="implementer">实施 / 唯一写者</option></select></label><fieldset className="task-dependencies"><legend>依赖任务</legend>{index === 0 ? <small>无前序任务</small> : tasks.slice(0, index).map((candidate) => <label key={candidate.taskId}><input type="checkbox" checked={task.dependencies.includes(candidate.taskId)} onChange={(event) => updateTask(index, { dependencies: event.target.checked ? [...task.dependencies, candidate.taskId] : task.dependencies.filter((id) => id !== candidate.taskId) })} />{candidate.title}</label>)}</fieldset><label className="task-write"><input type="checkbox" checked={task.requiresWrite} disabled={task.role !== "implementer"} onChange={(event) => updateTask(index, { requiresWrite: event.target.checked })} />需要写入项目</label>{tasks.length > 1 && <button className="text-danger" type="button" onClick={() => setTasks((current) => current.filter((_, position) => position === index ? null : { ...task, dependencies: task.dependencies.filter((id) => id !== current[index].taskId) }).filter((task): task is DraftTask => task !== null))}>移除</button>}</article>)}</div>
      <button className="primary-action" disabled={busy || sessions.length < 3}>{busy ? "正在调度…" : "创建并启动任务图"}</button>{sessions.length < 3 && <small>请先创建至少 3 个受管 CLI 会话。</small>}
    </form> : <div className="run-console">
      <div className="run-goal"><span>协作目标 / GOAL</span><strong>{snapshot.run.goal}</strong></div>
      <div className="task-board">{snapshot.tasks.map((task) => { const dependencies = task.dependencies || []; return <article className={`task-track is-${task.state}`} key={task.task_id}><span className={`status-mark is-${task.state}`} /><div><strong>{task.title}</strong><small>状态 {task.state} · 尝试 {task.attempt}</small>{(task.role || dependencies.length > 0) && <small>{task.role || "实施"} · {dependencies.length ? `依赖 ${dependencies.join("、")}` : "无依赖"}</small>}</div><div className="task-actions">{task.state === "working" && <button className="primary-action" disabled={busy} onClick={() => void complete(task.task_id, task.assignee_agent_id)}>登记证据</button>}{task.state === "review" && <button className="primary-action" disabled={busy} onClick={() => { setReviewingTask(task.task_id); setReviewNotes(""); }}>审查任务</button>}</div>{reviewingTask === task.task_id && <div className="review-sheet"><label>审查结论<textarea autoFocus value={reviewNotes} onChange={(event) => setReviewNotes(event.target.value)} placeholder="说明通过依据或退回原因" /></label><button className="secondary-action" onClick={() => setReviewingTask(null)}>取消</button><button className="danger-action" disabled={busy} onClick={() => void review(task.task_id, "rejected")}>退回修改</button><button className="primary-action" disabled={busy} onClick={() => void review(task.task_id, "approved")}>通过审查</button></div>}</article>; })}</div>
      <section className="teamrun-audit" aria-label="协作事件审计"><header><div><span>EVENT AUDIT / 可审计协作过程</span><h3>运行事件与 tmux 投递回执</h3></div><button className="secondary-action" onClick={() => void refresh()}>刷新</button></header><div className="dispatch-receipts">{dispatches.map((dispatch) => <div key={dispatch.command_id} className={`dispatch-receipt is-${dispatch.state}`}><strong>{dispatch.task_id} · #{dispatch.attempt}</strong><span>{dispatch.state === "tmux_written" ? "已写入 tmux" : dispatch.state === "pending" ? "等待投递" : "投递不确定，需人工确认"}</span>{dispatch.last_error && <small>{dispatch.last_error}</small>}</div>)}{!dispatches.length && <small>尚未生成 tmux 投递记录。</small>}</div><ol className="event-timeline">{events.slice().reverse().map((item) => <li key={item.event_id}><time>{new Date(item.occurred_at).toLocaleTimeString()}</time><strong>{item.event_type}</strong><span>{item.aggregate_id}</span></li>)}{!events.length && <li>尚无事件；开始 TeamRun 后会显示可核验的协作过程。</li>}</ol></section>
      <div className="run-actions"><button className="secondary-action" onClick={() => snapshot && void commandTeamRun(snapshot.run.run_id, "pause", csrfToken).then(setSnapshot)}>暂停</button><button className="secondary-action" onClick={() => snapshot && void commandTeamRun(snapshot.run.run_id, "resume", csrfToken).then(setSnapshot)}>继续</button></div>
    </div>}
  </section>;
}
