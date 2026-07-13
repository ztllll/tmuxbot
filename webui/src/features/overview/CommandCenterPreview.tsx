import type { SystemStatus, TmuxSession } from "../../app/api";

type PreviewProps = {
  status: SystemStatus;
  sessions: TmuxSession[];
  theme: "light" | "dark" | "system";
  onThemeChange: (theme: "light" | "dark" | "system") => void;
};

function bridgeInfo(bridge: SystemStatus["bridge"]): { status: string; reason: string } {
  if (typeof bridge === "string") return { status: bridge, reason: "" };
  return {
    status: bridge?.status || "unknown",
    reason: bridge?.reason || "服务尚未提供 bridge 说明",
  };
}

function statusLabel(status: string): string {
  const labels: Record<string, string> = {
    running: "运行中",
    ok: "可用",
    found: "已发现",
    unconfigured: "尚未配置",
    degraded: "需要检查",
    unknown: "状态未知",
  };
  return labels[status] || status;
}

export default function CommandCenterPreview({ status, sessions, theme, onThemeChange }: PreviewProps) {
  const bridge = bridgeInfo(status.bridge);
  const providers = status.providers || [];
  const host = typeof status.host === "string" ? { hostname: status.host } : (status.host || {});
  const tmux = typeof status.tmux === "string" ? { status: status.tmux } : (status.tmux || {});
  const pathEntries = Object.entries(status.paths || {});
  const providerReady = providers.some((provider) => provider.status === "found" || provider.status === "ok");
  const bridgeReady = bridge.status === "running";

  const spine = [
    { label: "安装", state: "complete" },
    { label: "认证", state: "complete" },
    { label: "Provider", state: providerReady ? "complete" : "current" },
    { label: "通道", state: bridgeReady ? "complete" : providerReady ? "current" : "pending" },
    { label: "会话", state: sessions.length > 0 ? "complete" : "pending" },
  ];

  return (
    <main className="command-center">
      <header className="topbar">
        <div>
          <p className="eyebrow">TMUXBOT · LOCAL COMMAND PLANE</p>
          <h1>本机运行总览</h1>
        </div>
        <div className="host-stamp">
          <span>HOST</span>
          <strong>{host.hostname || "未报告"}</strong>
          <small>{[host.platform, host.python_version && `Python ${host.python_version}`].filter(Boolean).join(" · ") || "主机详情未报告"}</small>
          <label className="theme-picker"><span>界面</span><select value={theme} onChange={(event) => onThemeChange(event.target.value as "light" | "dark" | "system")}><option value="system">跟随系统</option><option value="dark">深色</option><option value="light">浅色</option></select></label>
        </div>
      </header>

      <nav className="run-spine" aria-label="首次启用进度">
        {spine.map((step, index) => (
          <div className={`spine-node is-${step.state}`} key={step.label}>
            <span className="node-index">{String(index + 1).padStart(2, "0")}</span>
            <strong>{step.label}</strong>
            <span className="node-state">{step.state === "complete" ? "完成" : step.state === "current" ? "下一步" : "等待"}</span>
          </div>
        ))}
      </nav>

      <section className="status-grid" aria-label="本机服务状态">
        <article className={`status-plate bridge-plate is-${bridge.status}`}>
          <div className="plate-heading">
            <span>BRIDGE</span>
            <strong>{statusLabel(bridge.status)}</strong>
          </div>
          <p>{bridge.reason}</p>
          {bridge.status === "unconfigured" && (
            <div className="next-action">
              <span>下一步</span>
              <strong>配置 Provider 与消息通道后，bridge 会独立启动。</strong>
              <small>当前 Web 控制台保持可用，不会创建或终止 tmux 会话。</small>
            </div>
          )}
        </article>

        <article className="status-plate">
          <div className="plate-heading">
            <span>TMUX</span>
            <strong>{statusLabel(tmux.status || "unknown")}</strong>
          </div>
          <p>{tmux.version || "服务未报告 tmux 版本"}</p>
          <div className="large-count"><strong>{sessions.length}</strong><span>当前 pane</span></div>
        </article>
      </section>

      <section className="inventory-grid">
        <article className="inventory-panel">
          <header><h2>Provider 候选</h2><span>{providers.length} 项</span></header>
          {providers.length ? (
            <ul className="machine-list">
              {providers.map((provider, index) => (
                <li key={`${provider.name || "provider"}-${index}`}>
                  <span className={`status-mark is-${provider.status || "unknown"}`} />
                  <div><strong>{provider.name || "未命名 Provider"}</strong><small>{provider.path || "路径未报告"}</small></div>
                  <div className="machine-meta"><span>{statusLabel(provider.status || "unknown")}</span><small>{provider.version || "版本未知"}</small></div>
                </li>
              ))}
            </ul>
          ) : <p className="empty-guidance">尚未发现 Provider 候选。此页面不会主动调用模型；可在本机运行 tmuxbot doctor 查看被动检查结果。</p>}
        </article>

        <article className="inventory-panel">
          <header><h2>tmux 会话</h2><span>{sessions.length} 项</span></header>
          {sessions.length ? (
            <ul className="machine-list">
              {sessions.map((session) => (
                <li key={session.target}>
                  <span className="status-mark is-found" />
                  <div><strong>{session.session_name}</strong><small>{session.cwd}</small></div>
                  <div className="machine-meta"><span>{session.command}</span><small>{session.classification}</small></div>
                </li>
              ))}
            </ul>
          ) : <p className="empty-guidance">没有 tmux 会话。Preview A 只显示现状，不会替你创建或接管会话。</p>}
        </article>
      </section>

      {pathEntries.length > 0 && (
        <section className="path-strip" aria-label="数据路径">
          {pathEntries.map(([name, value]) => {
            const path = typeof value === "string" ? value : value.path || "未报告";
            return <div key={name}><span>{name.toUpperCase()}</span><code>{path}</code></div>;
          })}
        </section>
      )}
    </main>
  );
}
