import { useMemo, useState, type FormEvent } from "react";

import {
  adoptManagedSession, createManagedSession, createProject, deleteProject, configureChannel,
  inspectProject, launchTeamRun, probeProvider, releaseManagedSession, scanProviders, updateProject,
  type ManagedSession, type Project, type ProviderProfile, type TeamRunSummary, type TmuxSession,
} from "../../app/api";
import TerminalWorkspace, { type WorkspaceTerminal } from "../terminal/TerminalWorkspace";
import TeamRunPanel from "../teamrun/TeamRunPanel";

type Props = {
  csrfToken: string; providers: ProviderProfile[]; projects: Project[];
  managedSessions: ManagedSession[]; sessions: TmuxSession[]; teamRuns: TeamRunSummary[];
  onRefresh: () => Promise<void>;
};

type Recipe = "solo" | "delivery" | "ui" | "orchestrated";
type Role = { id: string; label: string; suffix: string; schedulerRole?: "coordinator" | "implementer" | "reviewer" };
const recipes: Record<Recipe, { title: string; description: string; roles: Role[] }> = {
  solo: { title: "单 CLI", description: "适合一个明确任务，先创建一位执行者。", roles: [{ id: "implementer", label: "执行", suffix: "实施" }] },
  delivery: { title: "标准研发", description: "统筹、实施、审查各自拥有 tmux 上下文，可直接启动确定性协作。", roles: [{ id: "coordinator", label: "统筹", suffix: "协调", schedulerRole: "coordinator" }, { id: "implementer", label: "实施", suffix: "实现", schedulerRole: "implementer" }, { id: "reviewer", label: "审查", suffix: "审核", schedulerRole: "reviewer" }] },
  ui: { title: "界面功能", description: "设计、实现分离；设计者先在独立 CLI 输出验收点，再交给实施者。", roles: [{ id: "coordinator", label: "统筹", suffix: "协调", schedulerRole: "coordinator" }, { id: "ui_designer", label: "界面设计", suffix: "UI 设计" }, { id: "implementer", label: "实施", suffix: "实现", schedulerRole: "implementer" }, { id: "reviewer", label: "审查", suffix: "审核", schedulerRole: "reviewer" }] },
  orchestrated: { title: "调度优先", description: "调度者、执行者、审查者分离，适合持续协作与保留上下文。", roles: [{ id: "coordinator", label: "调度", suffix: "协调", schedulerRole: "coordinator" }, { id: "implementer", label: "执行", suffix: "实现", schedulerRole: "implementer" }, { id: "reviewer", label: "审查", suffix: "审核", schedulerRole: "reviewer" }] },
};

function basename(path: string) {
  return path.replace(/\/+$/, "").split("/").pop() || "新项目";
}

export default function ControlWorkbench({ csrfToken, providers, projects, managedSessions, sessions, onRefresh, teamRuns }: Props) {
  const [busy, setBusy] = useState<string | null>(null);
  const [notice, setNotice] = useState("从项目目录开始：选择 CLI，再选择协作方式。所有 CLI 均直接运行在 tmux 内。");
  const [step, setStep] = useState(1);
  const [projectName, setProjectName] = useState(""); const [projectPath, setProjectPath] = useState("");
  const [recipe, setRecipe] = useState<Recipe>("delivery"); const [providerId, setProviderId] = useState("");
  const [roleProviders, setRoleProviders] = useState<Record<string, string>>({});
  const [runGoal, setRunGoal] = useState("");
  const [pathInspection, setPathInspection] = useState<{ root_path: string; git_root?: string | null; branch?: string | null; matching_panes: Array<{ target: string; command: string }> } | null>(null);
  const [terminalSessions, setTerminalSessions] = useState<WorkspaceTerminal[]>([]);
  const [editing, setEditing] = useState<Project | null>(null);
  const [channel, setChannel] = useState<"telegram" | "feishu">("telegram"); const [channelSession, setChannelSession] = useState("");
  const [remoteChatId, setRemoteChatId] = useState(""); const [credentialId, setCredentialId] = useState("");
  const [credentialSecret, setCredentialSecret] = useState(""); const [bossId, setBossId] = useState(""); const [mentionRequired, setMentionRequired] = useState(false);
  const llmProviders = providers.filter((item) => item.binary_name !== "tmux");
  const currentRecipe = recipes[recipe];
  const unmanaged = useMemo(() => sessions.filter((item) => !managedSessions.some((managed) => managed.tmux_target === item.target)), [sessions, managedSessions]);

  function openTerminal(session: ManagedSession, observedTarget?: string) {
    const key = observedTarget ? `observed:${observedTarget}` : session.id;
    setTerminalSessions((current) => current.some((item) => item.key === key) ? current : [...current, { key, session, observedTarget }]);
  }

  async function scan() { setBusy("scan"); try { const items = await scanProviders(csrfToken); setNotice(`扫描完成：发现 ${items.length} 个本机 CLI。`); await onRefresh(); } catch { setNotice("扫描失败，请运行 tmuxbot doctor 查看原因。"); } finally { setBusy(null); } }
  async function probe(id: string) { setBusy(id); try { const r = await probeProvider(id, csrfToken); setNotice(r.success ? `版本探测成功：${r.version || "可用"}` : `探测未通过：${r.error_code || "未知原因"}`); await onRefresh(); } catch { setNotice("Provider 身份已变化，请重新扫描。"); } finally { setBusy(null); } }
  async function inspectPath() { setBusy("inspect-path"); try { const result = await inspectProject(projectPath, csrfToken); setProjectPath(result.root_path); setPathInspection(result); setNotice(result.git_root ? `目录已验证 · Git 分支：${result.branch || "detached HEAD"} · 发现 ${result.matching_panes.length} 个现有 pane。` : `目录已验证 · 非 Git 目录 · 发现 ${result.matching_panes.length} 个现有 pane。`); setStep(2); } catch { setPathInspection(null); setNotice("路径不可用：请输入当前宿主机上的可访问项目目录。"); } finally { setBusy(null); } }

  async function launchRecipe() {
    if (!projectPath || currentRecipe.roles.some((role) => !(roleProviders[role.id] || providerId))) return;
    setBusy("launch");
    try {
      const schedulerRoles = currentRecipe.roles.filter((role) => role.schedulerRole);
      if (runGoal.trim() && schedulerRoles.length === 3) {
        const projectLabel = projectName.trim() || basename(projectPath);
        await launchTeamRun({
          projectName: projectLabel, rootPath: projectPath, runId: `run-${Date.now()}`, goal: runGoal.trim(),
          roles: schedulerRoles.map((role) => ({
            role: role.schedulerRole!, providerId: roleProviders[role.id] || providerId,
            name: `${projectLabel} · ${role.suffix}`,
          })),
        }, csrfToken);
        setNotice("已原子创建三位职责 CLI 并启动 TeamRun：统筹先规划，审查确认后才进入隔离实施。可在下方协作台查看每一步。 ");
        setProjectName(""); setProjectPath(""); setRunGoal(""); setRoleProviders({}); setStep(1); await onRefresh();
        return;
      }
      if (runGoal.trim() && schedulerRoles.length !== 3) {
        setNotice("“界面功能”配方含独立设计者，暂不支持一键自动交接；请使用标准研发或调度优先配方启动 TeamRun。");
        return;
      }
      const project = await createProject(projectName.trim() || basename(projectPath), projectPath, csrfToken);
      const created = new Map<string, ManagedSession>();
      for (const role of currentRecipe.roles) {
        const selected = roleProviders[role.id] || providerId;
        created.set(role.id, await createManagedSession(`${project.name} · ${role.suffix}`, project.id, selected, csrfToken));
      }
      setNotice(`已在 tmux 创建 ${created.size} 个职责会话。模型不写死：在终端点“打开原生模型菜单”选择。`);
      setProjectName(""); setProjectPath(""); setRunGoal(""); setRoleProviders({}); setStep(1); await onRefresh();
    } catch { setNotice("启动失败：尚未进入协作运行的资源已自动清理；若任务已写入 tmux，协作台会保留该运行供恢复和人工确认。"); } finally { setBusy(null); }
  }

  async function adopt(item: TmuxSession, projectId: string, provider: string) {
    setBusy(`adopt-${item.target}`); try {
      const managed = await adoptManagedSession({ name: `${item.session_name} · 已纳入`, projectId, providerId: provider, target: item.target }, csrfToken);
      openTerminal(managed); setNotice("现有 tmux pane 已纳入项目；默认只读，点击“接管输入”才会写入终端。"); await onRefresh();
    } catch { setNotice("无法纳入该 pane：它必须仍存在于所选项目目录内，且不能已被管理。"); } finally { setBusy(null); }
  }

  async function saveProject(event: FormEvent) { event.preventDefault(); if (!editing) return; setBusy(`project-${editing.id}`); try { await updateProject(editing.id, editing.name, editing.root_path, csrfToken); setEditing(null); setNotice("项目已更新。"); await onRefresh(); } catch { setNotice("项目更新失败：请确认路径存在且未被另一个项目使用。"); } finally { setBusy(null); } }
  async function removeProject(project: Project) { if (!window.confirm(`删除项目“${project.name}”？不会关闭 tmux；仍有关联会话时会拒绝删除。`)) return; setBusy(`remove-${project.id}`); try { await deleteProject(project.id, csrfToken); setNotice("项目已删除。"); await onRefresh(); } catch { setNotice("项目仍有关联的受管会话，暂不能删除。"); } finally { setBusy(null); } }
  async function releaseSession(item: ManagedSession) { if (!window.confirm(`释放“${item.name}”的受管记录？不会关闭 tmux，只会让项目不再管理它。`)) return; setBusy(`release-${item.id}`); try { await releaseManagedSession(item.id, csrfToken); setNotice("已释放受管记录；tmux 会话仍在运行，可随时重新纳入。"); await onRefresh(); } catch { setNotice("释放会话记录失败。"); } finally { setBusy(null); } }
  async function saveChannel(event: FormEvent) { event.preventDefault(); setBusy("channel"); try { const r = await configureChannel({ channel, managed_session_id: channelSession, remote_chat_id: remoteChatId, credential_id: credentialId, credential_secret: credentialSecret || undefined, boss_id: bossId, mention_required: mentionRequired }, csrfToken); setNotice(r.restart_required ? "通道与 binding 已保存；重启 tmuxbot serve 后 bridge 会载入。" : "通道已生效。"); } catch { setNotice("通道配置失败：请检查凭据、Boss ID、chat ID 与会话。"); } finally { setBusy(null); } }

  return <>
    <section className="workbench" aria-label="项目启动与本机控制">
      <header className="workbench-head"><div><span>LOCAL CONTROL DESK</span><h2>项目与 tmux 调度</h2></div><button className="primary-action compact-action" onClick={() => void scan()} disabled={busy !== null}>{busy === "scan" ? "正在扫描…" : "扫描本机 CLI"}</button></header>
      <p className="operator-notice" role="status">{notice}</p>
      <nav className="control-nav" aria-label="控制台分区"><a href="#project-wizard">创建项目</a><a href="#terminal-workspace">终端工作区</a><a href="#teamrun">协作调度</a><a href="#channel-config">通道管理</a></nav>

      <section className="wizard" id="project-wizard" aria-label="创建项目向导">
        <div className="wizard-steps"><span className={step === 1 ? "active" : ""}>1 · 项目目录</span><span className={step === 2 ? "active" : ""}>2 · 可用 CLI</span><span className={step === 3 ? "active" : ""}>3 · 职责配置</span><span className={step === 4 ? "active" : ""}>4 · 确认启动</span></div>
        {step === 1 && <div className="wizard-stage"><h3>从一个目录开始</h3><p>输入已存在的项目绝对路径。继续前会验证目录、识别 Git 信息，并发现目录内已有 tmux pane。</p><label><span>项目名称（可选）</span><input value={projectName} onChange={(e) => setProjectName(e.target.value)} placeholder="例如：官网改版" /></label><label><span>项目绝对路径</span><input value={projectPath} onChange={(e) => { setProjectPath(e.target.value); setPathInspection(null); }} placeholder="/home/user/projects/demo" /></label><button className="primary-action" disabled={!projectPath || busy !== null} onClick={() => void inspectPath()}>{busy === "inspect-path" ? "正在验证目录…" : "验证目录并选择 CLI"}</button></div>}
        {step === 2 && <div className="wizard-stage"><h3>选择已验证的 CLI</h3><p>先扫描并测试，创建时只会使用本机已发现的绝对路径。</p>{llmProviders.length === 0 ? <button className="secondary-action" onClick={() => void scan()} disabled={busy !== null}>扫描 CLI</button> : <div className="provider-picks">{llmProviders.map((provider) => <article key={provider.id} className={providerId === provider.id ? "provider-pick selected" : "provider-pick"}><button type="button" className="provider-select" onClick={() => setProviderId(provider.id)}><strong>{provider.capabilities?.display_name || provider.binary_name}</strong><small>{provider.version || "待测试"}</small><code>{provider.executable_path}</code><em>{provider.capabilities?.supports_model_picker ? `支持 ${provider.capabilities.model_command} 模型菜单` : "未声明模型菜单"}</em></button><button type="button" className="secondary-action provider-probe" onClick={() => void probe(provider.id)} disabled={busy !== null}>{busy === provider.id ? "测试中…" : "测试 CLI"}</button></article>)}</div>}<div className="wizard-actions"><button className="secondary-action" onClick={() => setStep(1)}>返回</button><button className="primary-action" disabled={!providerId} onClick={() => setStep(3)}>下一步：协作方式</button></div></div>}
        {step === 3 && <div className="wizard-stage"><h3>选择协作配方与职责 CLI</h3><p>每个职责都有独立 tmux 上下文。模型候选始终由对应 CLI 的原生 <code>/model</code> 实时提供，不在 Web 写死。</p><div className="recipe-grid">{(Object.keys(recipes) as Recipe[]).map((key) => <button key={key} className={recipe === key ? "recipe-card selected" : "recipe-card"} onClick={() => setRecipe(key)}><strong>{recipes[key].title}</strong><small>{recipes[key].description}</small><code>{recipes[key].roles.map((role) => role.label).join(" · ")}</code></button>)}</div><div className="role-config">{currentRecipe.roles.map((role) => <label key={role.id}><span>{role.label}</span><select value={roleProviders[role.id] || providerId} onChange={(event) => setRoleProviders({ ...roleProviders, [role.id]: event.target.value })}>{llmProviders.map((provider) => <option key={provider.id} value={provider.id}>{provider.binary_name} · {provider.version || "未探测版本"}</option>)}</select><small>创建后在终端中使用原生 /model 设置当前 CLI 模型。</small></label>)}</div><div className="wizard-actions"><button className="secondary-action" onClick={() => setStep(2)}>返回</button><button className="primary-action" disabled={busy !== null} onClick={() => setStep(4)}>下一步：确认启动</button></div></div>}
        {step === 4 && <div className="wizard-stage launch-review"><h3>确认 tmux 启动计划</h3><p>将创建的 CLI 都在 <code>{projectPath}</code> 内运行。默认启用 CLI 当前权限模式；模型由每个 CLI 自己的原生 picker 决定。</p>{pathInspection && <div className="inspection-summary"><span>{pathInspection.git_root ? `Git · ${pathInspection.branch || "detached HEAD"}` : "非 Git 目录"}</span><span>已有 pane · {pathInspection.matching_panes.length}</span></div>}<ul>{currentRecipe.roles.map((role) => <li key={role.id}><strong>{role.label}</strong><span>{llmProviders.find((provider) => provider.id === (roleProviders[role.id] || providerId))?.binary_name || "未选择"}</span><small>{role.schedulerRole ? `可加入 TeamRun：${role.schedulerRole}` : "独立设计会话；第一版由统筹手动交接"}</small></li>)}</ul><label><span>协作目标（可选）</span><textarea value={runGoal} onChange={(event) => setRunGoal(event.target.value)} placeholder="填写后将自动启动统筹、实施、审查 TeamRun。" /></label><small>当前 TeamRun 已支持统筹、实施、审查三角色。UI 设计者会作为独立 tmux CLI 保留上下文，自动设计→实施交接将在调度器扩展时加入。</small><div className="wizard-actions"><button className="secondary-action" onClick={() => setStep(3)}>返回</button><button className="primary-action" disabled={busy !== null} onClick={() => void launchRecipe()}>{busy === "launch" ? "正在创建 tmux 会话…" : `确认创建 ${currentRecipe.roles.length} 个 CLI`}</button></div></div>}
      </section>

      <div className="workbench-columns">
        <article className="workbench-unit"><span className="unit-number">PROJECTS</span><h3>已登记项目</h3>{projects.length === 0 ? <p>尚未登记项目。使用上方三步向导开始。</p> : <ul className="project-list">{projects.map((project) => <li key={project.id}><div><strong>{project.name}</strong><code>{project.root_path}</code></div><div><button className="secondary-action" onClick={() => setEditing({ ...project })}>编辑</button><button className="text-danger" onClick={() => void removeProject(project)} disabled={busy !== null}>删除</button></div></li>)}</ul>}{editing && <form className="inline-project-edit" onSubmit={saveProject}><label>名称<input value={editing.name} onChange={(e) => setEditing({ ...editing, name: e.target.value })} required /></label><label>路径<input value={editing.root_path} onChange={(e) => setEditing({ ...editing, root_path: e.target.value })} required /></label><div><button className="secondary-action" type="button" onClick={() => setEditing(null)}>取消</button><button className="primary-action" disabled={busy !== null}>保存项目</button></div></form>}</article>
        <article className="workbench-unit" id="terminal-workspace"><span className="unit-number">TMUX WINDOWS</span><h3>查看与操作终端</h3><p>所有终端默认只读；不同 pane 可同时接管。已有 pane 可直接查看，无需先纳入项目。</p><div className="terminal-list">{managedSessions.map((item) => <div className="managed-row" key={item.id}><button type="button" className="session-row" onClick={() => openTerminal(item)}><span>{item.name}</span><code>{item.tmux_target}</code><strong>{item.runtime_model ? `当前模型 · ${item.runtime_model}` : item.provider_capabilities?.supports_model_picker ? `${item.provider || "CLI"} · 原生模型菜单` : "加入工作区"}</strong></button><button className="text-danger" onClick={() => void releaseSession(item)} disabled={busy !== null}>释放管理</button></div>)}</div>{unmanaged.length > 0 && <div className="orphan-list"><h4>现有 tmux pane</h4>{unmanaged.map((item) => <div key={item.target} className="orphan-row"><div><strong>{item.session_name}</strong><code>{item.target} · {item.cwd}</code></div><div className="orphan-actions"><button className="secondary-action" onClick={() => openTerminal({ id: "observed", name: item.session_name, tmux_target: item.target, project_id: "", provider_id: "", status: "observed" }, item.target)}>直接查看</button><select defaultValue="" aria-label={`${item.target} 所属项目`} onChange={(e) => { const [projectId, provider] = e.target.value.split(":"); if (projectId && provider) void adopt(item, projectId, provider); }} disabled={busy !== null}><option value="">纳入项目…</option>{projects.flatMap((project) => llmProviders.map((provider) => <option key={`${project.id}-${provider.id}`} value={`${project.id}:${provider.id}`}>{project.name} · {provider.binary_name}</option>))}</select></div></div>)}</div>}</article>
        <form className="workbench-unit control-form" id="channel-config" onSubmit={saveChannel}><span className="unit-number">CHANNEL</span><h3>接入消息通道</h3><label><span>通道</span><select value={channel} onChange={(e) => setChannel(e.target.value as "telegram" | "feishu")}><option value="telegram">Telegram</option><option value="feishu">飞书</option></select></label><label><span>受管会话</span><select value={channelSession} onChange={(e) => setChannelSession(e.target.value)} required><option value="">请选择</option>{managedSessions.map((s) => <option value={s.id} key={s.id}>{s.name}</option>)}</select></label><label><span>{channel === "telegram" ? "Bot Token" : "App ID"}</span><input type="password" value={credentialId} onChange={(e) => setCredentialId(e.target.value)} required /></label>{channel === "feishu" && <label><span>App Secret</span><input type="password" value={credentialSecret} onChange={(e) => setCredentialSecret(e.target.value)} required /></label>}<label><span>{channel === "telegram" ? "Boss User ID" : "Boss Open ID"}</span><input value={bossId} onChange={(e) => setBossId(e.target.value)} required /></label><label><span>{channel === "telegram" ? "Chat ID" : "Chat ID（oc_…）"}</span><input value={remoteChatId} onChange={(e) => setRemoteChatId(e.target.value)} required /></label><label className="toggle-field"><input type="checkbox" checked={mentionRequired} onChange={(event) => setMentionRequired(event.target.checked)} /><span>群聊仅在 @Bot 时响应</span><small>关闭后，已授权群聊内可直接发送消息。</small></label><button className="primary-action" disabled={busy !== null}>保存通道配置</button></form>
      </div>
    </section>
    <TerminalWorkspace terminals={terminalSessions} csrfToken={csrfToken} onClose={(key) => setTerminalSessions((current) => current.filter((item) => item.key !== key))} />
    <TeamRunPanel sessions={managedSessions} csrfToken={csrfToken} runs={teamRuns} onRefresh={onRefresh} />
  </>;
}
