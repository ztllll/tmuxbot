import { useMemo, useState, type FormEvent } from "react";

import {
  adoptManagedSession, createManagedSession, createProject, deleteProject, configureChannel,
  probeProvider, scanProviders, updateProject,
  type ManagedSession, type Project, type ProviderProfile, type TeamRunSummary, type TmuxSession,
} from "../../app/api";
import TerminalDock from "../terminal/TerminalDock";
import TeamRunPanel from "../teamrun/TeamRunPanel";

type Props = {
  csrfToken: string; providers: ProviderProfile[]; projects: Project[];
  managedSessions: ManagedSession[]; sessions: TmuxSession[]; teamRuns: TeamRunSummary[];
  onRefresh: () => Promise<void>;
};

type Recipe = "solo" | "delivery" | "ui" | "orchestrated";
const recipes: Record<Recipe, { title: string; description: string; roles: Array<[string, string]> }> = {
  solo: { title: "单 CLI", description: "适合一个明确任务，先创建一位执行者。", roles: [["执行", "实施"]] },
  delivery: { title: "标准研发", description: "统筹、实施、审查三条 tmux CLI，后续可在协作台分配任务。", roles: [["统筹", "协调"], ["实施", "实现"], ["审查", "审核"]] },
  ui: { title: "界面功能", description: "增加界面设计职责，让设计与实现有独立上下文。", roles: [["统筹", "协调"], ["界面设计", "UI 设计"], ["实施", "实现"], ["审查", "审核"]] },
  orchestrated: { title: "调度优先", description: "先建立调度者、执行者和审查者，适合持续协作。", roles: [["调度", "协调"], ["执行", "实现"], ["审查", "审核"]] },
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
  const [terminalSession, setTerminalSession] = useState<ManagedSession | null>(null);
  const [editing, setEditing] = useState<Project | null>(null);
  const [channel, setChannel] = useState<"telegram" | "feishu">("telegram"); const [channelSession, setChannelSession] = useState("");
  const [remoteChatId, setRemoteChatId] = useState(""); const [credentialId, setCredentialId] = useState("");
  const [credentialSecret, setCredentialSecret] = useState(""); const [bossId, setBossId] = useState("");
  const llmProviders = providers.filter((item) => item.binary_name !== "tmux");
  const selectedProvider = llmProviders.find((item) => item.id === providerId);
  const currentRecipe = recipes[recipe];
  const unmanaged = useMemo(() => sessions.filter((item) => !managedSessions.some((managed) => managed.tmux_target === item.target)), [sessions, managedSessions]);

  async function scan() { setBusy("scan"); try { const items = await scanProviders(csrfToken); setNotice(`扫描完成：发现 ${items.length} 个本机 CLI。`); await onRefresh(); } catch { setNotice("扫描失败，请运行 tmuxbot doctor 查看原因。"); } finally { setBusy(null); } }
  async function probe(id: string) { setBusy(id); try { const r = await probeProvider(id, csrfToken); setNotice(r.success ? `版本探测成功：${r.version || "可用"}` : `探测未通过：${r.error_code || "未知原因"}`); await onRefresh(); } catch { setNotice("Provider 身份已变化，请重新扫描。"); } finally { setBusy(null); } }

  async function launchRecipe() {
    if (!projectPath || !providerId) return;
    setBusy("launch");
    try {
      const project = await createProject(projectName.trim() || basename(projectPath), projectPath, csrfToken);
      for (const [, suffix] of currentRecipe.roles) await createManagedSession(`${project.name} · ${suffix}`, project.id, providerId, csrfToken);
      setNotice(`已在 tmux 创建 ${currentRecipe.roles.length} 个 ${selectedProvider?.binary_name || "CLI"} 会话。可在下方直接查看或短暂接管。`);
      setProjectName(""); setProjectPath(""); setStep(1); await onRefresh();
    } catch { setNotice("创建失败：确认项目路径存在、CLI 已扫描，并且项目尚未登记。"); } finally { setBusy(null); }
  }

  async function adopt(item: TmuxSession, projectId: string, provider: string) {
    setBusy(`adopt-${item.target}`); try {
      const managed = await adoptManagedSession({ name: `${item.session_name} · 已纳入`, projectId, providerId: provider, target: item.target }, csrfToken);
      setTerminalSession(managed); setNotice("现有 tmux pane 已纳入项目；默认只读，点击“接管输入”才会写入终端。"); await onRefresh();
    } catch { setNotice("无法纳入该 pane：它必须仍存在于所选项目目录内，且不能已被管理。"); } finally { setBusy(null); }
  }

  async function saveProject(event: FormEvent) { event.preventDefault(); if (!editing) return; setBusy(`project-${editing.id}`); try { await updateProject(editing.id, editing.name, editing.root_path, csrfToken); setEditing(null); setNotice("项目已更新。"); await onRefresh(); } catch { setNotice("项目更新失败：请确认路径存在且未被另一个项目使用。"); } finally { setBusy(null); } }
  async function removeProject(project: Project) { if (!window.confirm(`删除项目“${project.name}”？不会关闭 tmux；仍有关联会话时会拒绝删除。`)) return; setBusy(`remove-${project.id}`); try { await deleteProject(project.id, csrfToken); setNotice("项目已删除。"); await onRefresh(); } catch { setNotice("项目仍有关联的受管会话，暂不能删除。"); } finally { setBusy(null); } }
  async function saveChannel(event: FormEvent) { event.preventDefault(); setBusy("channel"); try { const r = await configureChannel({ channel, managed_session_id: channelSession, remote_chat_id: remoteChatId, credential_id: credentialId, credential_secret: credentialSecret || undefined, boss_id: bossId, mention_required: false }, csrfToken); setNotice(r.restart_required ? "通道与 binding 已保存；重启 tmuxbot serve 后 bridge 会载入。" : "通道已生效。"); } catch { setNotice("通道配置失败：请检查凭据、Boss ID、chat ID 与会话。"); } finally { setBusy(null); } }

  return <>
    <section className="workbench" aria-label="项目启动与本机控制">
      <header className="workbench-head"><div><span>LOCAL CONTROL DESK</span><h2>项目与 tmux 调度</h2></div><button className="primary-action compact-action" onClick={() => void scan()} disabled={busy !== null}>{busy === "scan" ? "正在扫描…" : "扫描本机 CLI"}</button></header>
      <p className="operator-notice" role="status">{notice}</p>

      <section className="wizard" aria-label="创建项目向导">
        <div className="wizard-steps"><span className={step === 1 ? "active" : ""}>1 · 项目目录</span><span className={step === 2 ? "active" : ""}>2 · 选择 CLI</span><span className={step === 3 ? "active" : ""}>3 · 协作方式</span></div>
        {step === 1 && <div className="wizard-stage"><h3>从一个目录开始</h3><p>输入已存在的项目绝对路径。名称可留空，系统会使用目录名。</p><label><span>项目名称（可选）</span><input value={projectName} onChange={(e) => setProjectName(e.target.value)} placeholder="例如：官网改版" /></label><label><span>项目绝对路径</span><input value={projectPath} onChange={(e) => setProjectPath(e.target.value)} placeholder="/home/user/projects/demo" /></label><button className="primary-action" disabled={!projectPath} onClick={() => setStep(2)}>下一步：选择 CLI</button></div>}
        {step === 2 && <div className="wizard-stage"><h3>选择已验证的 CLI</h3><p>先扫描并测试，创建时只会使用本机已发现的绝对路径。</p>{llmProviders.length === 0 ? <button className="secondary-action" onClick={() => void scan()} disabled={busy !== null}>扫描 CLI</button> : <div className="provider-picks">{llmProviders.map((provider) => <button key={provider.id} className={providerId === provider.id ? "provider-pick selected" : "provider-pick"} onClick={() => setProviderId(provider.id)}><strong>{provider.binary_name}</strong><small>{provider.version || "待测试"}</small><code>{provider.executable_path}</code><em onClick={(e) => { e.stopPropagation(); void probe(provider.id); }}>测试</em></button>)}</div>}<div className="wizard-actions"><button className="secondary-action" onClick={() => setStep(1)}>返回</button><button className="primary-action" disabled={!providerId} onClick={() => setStep(3)}>下一步：协作方式</button></div></div>}
        {step === 3 && <div className="wizard-stage"><h3>选择协作配方</h3><p>每个职责拥有自己的 tmux CLI 和上下文；之后可在协作台启动任务。</p><div className="recipe-grid">{(Object.keys(recipes) as Recipe[]).map((key) => <button key={key} className={recipe === key ? "recipe-card selected" : "recipe-card"} onClick={() => setRecipe(key)}><strong>{recipes[key].title}</strong><small>{recipes[key].description}</small><code>{recipes[key].roles.map(([role]) => role).join(" · ")}</code></button>)}</div><div className="wizard-actions"><button className="secondary-action" onClick={() => setStep(2)}>返回</button><button className="primary-action" disabled={busy !== null} onClick={() => void launchRecipe()}>{busy === "launch" ? "正在创建 tmux 会话…" : `创建 ${currentRecipe.roles.length} 个 tmux CLI`}</button></div></div>}
      </section>

      <div className="workbench-columns">
        <article className="workbench-unit"><span className="unit-number">PROJECTS</span><h3>已登记项目</h3>{projects.length === 0 ? <p>尚未登记项目。使用上方三步向导开始。</p> : <ul className="project-list">{projects.map((project) => <li key={project.id}><div><strong>{project.name}</strong><code>{project.root_path}</code></div><div><button className="secondary-action" onClick={() => setEditing({ ...project })}>编辑</button><button className="text-danger" onClick={() => void removeProject(project)} disabled={busy !== null}>删除</button></div></li>)}</ul>}{editing && <form className="inline-project-edit" onSubmit={saveProject}><label>名称<input value={editing.name} onChange={(e) => setEditing({ ...editing, name: e.target.value })} required /></label><label>路径<input value={editing.root_path} onChange={(e) => setEditing({ ...editing, root_path: e.target.value })} required /></label><div><button className="secondary-action" type="button" onClick={() => setEditing(null)}>取消</button><button className="primary-action" disabled={busy !== null}>保存项目</button></div></form>}</article>
        <article className="workbench-unit"><span className="unit-number">TMUX WINDOWS</span><h3>查看与操作终端</h3><p>所有受管会话默认以只读方式打开；只有手动接管后才可发送输入。</p><div className="terminal-list">{managedSessions.map((item) => <button key={item.id} type="button" className="session-row" onClick={() => setTerminalSession(item)}><span>{item.name}</span><code>{item.tmux_target}</code><strong>查看终端</strong></button>)}</div>{unmanaged.length > 0 && <div className="orphan-list"><h4>现有 tmux pane</h4>{unmanaged.map((item) => <div key={item.target} className="orphan-row"><div><strong>{item.session_name}</strong><code>{item.target} · {item.cwd}</code></div><select defaultValue="" aria-label={`${item.target} 所属项目`} onChange={(e) => { const [projectId, provider] = e.target.value.split(":"); if (projectId && provider) void adopt(item, projectId, provider); }} disabled={busy !== null}><option value="">纳入项目并打开…</option>{projects.flatMap((project) => llmProviders.map((provider) => <option key={`${project.id}-${provider.id}`} value={`${project.id}:${provider.id}`}>{project.name} · {provider.binary_name}</option>))}</select></div>)}</div>}</article>
        <form className="workbench-unit control-form" onSubmit={saveChannel}><span className="unit-number">CHANNEL</span><h3>接入消息通道</h3><label><span>通道</span><select value={channel} onChange={(e) => setChannel(e.target.value as "telegram" | "feishu")}><option value="telegram">Telegram</option><option value="feishu">飞书</option></select></label><label><span>受管会话</span><select value={channelSession} onChange={(e) => setChannelSession(e.target.value)} required><option value="">请选择</option>{managedSessions.map((s) => <option value={s.id} key={s.id}>{s.name}</option>)}</select></label><label><span>{channel === "telegram" ? "Bot Token" : "App ID"}</span><input type="password" value={credentialId} onChange={(e) => setCredentialId(e.target.value)} required /></label>{channel === "feishu" && <label><span>App Secret</span><input type="password" value={credentialSecret} onChange={(e) => setCredentialSecret(e.target.value)} required /></label>}<label><span>{channel === "telegram" ? "Boss User ID" : "Boss Open ID"}</span><input value={bossId} onChange={(e) => setBossId(e.target.value)} required /></label><label><span>{channel === "telegram" ? "Chat ID" : "Chat ID（oc_…）"}</span><input value={remoteChatId} onChange={(e) => setRemoteChatId(e.target.value)} required /></label><button className="primary-action" disabled={busy !== null}>保存通道配置</button></form>
      </div>
    </section>
    {terminalSession && <TerminalDock session={terminalSession} csrfToken={csrfToken} onClose={() => setTerminalSession(null)} />}
    <TeamRunPanel sessions={managedSessions} csrfToken={csrfToken} runs={teamRuns} onRefresh={onRefresh} />
  </>;
}
