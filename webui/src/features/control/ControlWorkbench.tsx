import { useState, type FormEvent } from "react";

import {
  createManagedSession,
  createProject,
  probeProvider,
  scanProviders,
  type ManagedSession,
  type Project,
  type ProviderProfile,
} from "../../app/api";

type Props = {
  csrfToken: string;
  providers: ProviderProfile[];
  projects: Project[];
  managedSessions: ManagedSession[];
  onRefresh: () => Promise<void>;
};

export default function ControlWorkbench({
  csrfToken, providers, projects, managedSessions, onRefresh,
}: Props) {
  const [busy, setBusy] = useState<string | null>(null);
  const [notice, setNotice] = useState("所有探测均在本机执行；版本扫描不会调用模型。 ");
  const [projectName, setProjectName] = useState("");
  const [projectPath, setProjectPath] = useState("");
  const [sessionName, setSessionName] = useState("");
  const [projectId, setProjectId] = useState("");
  const [providerId, setProviderId] = useState("");

  async function scan() {
    setBusy("scan");
    try {
      const items = await scanProviders(csrfToken);
      setNotice(`扫描完成：发现 ${items.length} 个本机 CLI。`);
      await onRefresh();
    } catch { setNotice("扫描失败，请运行 tmuxbot doctor 查看原因。"); }
    finally { setBusy(null); }
  }

  async function probe(id: string) {
    setBusy(id);
    try {
      const result = await probeProvider(id, csrfToken);
      setNotice(result.success ? `版本探测成功：${result.version || "可用"}` : `探测未通过：${result.error_code || "未知原因"}`);
      await onRefresh();
    } catch { setNotice("Provider 身份已变化，请重新扫描。"); }
    finally { setBusy(null); }
  }

  async function addProject(event: FormEvent) {
    event.preventDefault(); setBusy("project");
    try {
      await createProject(projectName, projectPath, csrfToken);
      setProjectName(""); setProjectPath(""); setNotice("项目已登记。tmuxbot 不会修改项目全局指令文件。");
      await onRefresh();
    } catch { setNotice("项目登记失败：请确认路径存在且尚未登记。"); }
    finally { setBusy(null); }
  }

  async function addSession(event: FormEvent) {
    event.preventDefault(); setBusy("session");
    try {
      await createManagedSession(sessionName, projectId, providerId, csrfToken);
      setSessionName(""); setNotice("受管 tmux CLI 已启动，可继续进入终端或 TeamRun。");
      await onRefresh();
    } catch { setNotice("会话启动失败：请先完成 Provider 探测并检查 tmux。"); }
    finally { setBusy(null); }
  }

  const llmProviders = providers.filter((item) => item.binary_name !== "tmux");
  return (
    <section className="workbench" aria-label="本机配置与启动">
      <header className="workbench-head">
        <div><span>CONTROL WORKBENCH</span><h2>配置与启动</h2></div>
        <button className="primary-action compact-action" onClick={() => void scan()} disabled={busy !== null}>
          {busy === "scan" ? "正在扫描…" : "扫描本机 CLI"}
        </button>
      </header>
      <p className="operator-notice" role="status">{notice}</p>
      <div className="workbench-columns">
        <article className="workbench-unit">
          <span className="unit-number">01 / PROVIDER</span><h3>确认可执行文件</h3>
          {providers.length === 0 ? <p>尚未扫描。点击上方按钮发现 tmux、Claude Code 与 Codex。</p> : (
            <ul className="probe-list">{providers.map((provider) => <li key={provider.id}>
              <div><strong>{provider.binary_name}</strong><code>{provider.executable_path}</code><small>{provider.version || "尚未探测版本"}</small></div>
              <button className="secondary-action" onClick={() => void probe(provider.id)} disabled={busy !== null}>{busy === provider.id ? "探测中…" : "测试"}</button>
            </li>)}</ul>
          )}
        </article>
        <form className="workbench-unit control-form" onSubmit={addProject}>
          <span className="unit-number">02 / PROJECT</span><h3>登记项目目录</h3>
          <label><span>项目名称</span><input value={projectName} onChange={(e) => setProjectName(e.target.value)} required /></label>
          <label><span>宿主机绝对路径</span><input value={projectPath} onChange={(e) => setProjectPath(e.target.value)} placeholder="/home/user/projects/demo" required /></label>
          <button className="secondary-action" disabled={busy !== null}>保存项目</button>
          <small>已登记 {projects.length} 个项目。</small>
        </form>
        <form className="workbench-unit control-form" onSubmit={addSession}>
          <span className="unit-number">03 / SESSION</span><h3>启动原生 tmux CLI</h3>
          <label><span>分工名称</span><input value={sessionName} onChange={(e) => setSessionName(e.target.value)} placeholder="Codex 实施" required /></label>
          <label><span>项目</span><select value={projectId} onChange={(e) => setProjectId(e.target.value)} required><option value="">请选择</option>{projects.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}</select></label>
          <label><span>Provider</span><select value={providerId} onChange={(e) => setProviderId(e.target.value)} required><option value="">请选择</option>{llmProviders.map((p) => <option key={p.id} value={p.id}>{p.binary_name}</option>)}</select></label>
          <button className="primary-action" disabled={busy !== null}>启动 CLI 会话</button>
          <small>当前受管会话 {managedSessions.length} 个。启动采用项目目录与已验证 binary，不接受浏览器命令字符串。</small>
        </form>
      </div>
    </section>
  );
}
