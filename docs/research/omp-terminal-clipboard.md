# OMP 终端选中文本与系统剪贴板

## 1. 问题与约束

问题：在 tmux pane 内用鼠标拖选 OMP 输出时，选中文本是否会被 OMP 设置阻止进入系统剪贴板？本次只查阅官方 `omp://` 文档、已安装 OMP 的只读配置输出及本地事实；不改 OMP/tmux 配置，不检查 tmuxbot 业务代码。

已观察到：当前终端为 tmux 3.4；`omp config list --json` 中与终端/TUI/clipboard/mouse/selection 相关的结果只有 `terminal.showImages`、`terminal.showProgress`、`tui.*`（包括 `tui.scrollbackRebuild`、`tui.hyperlinks` 等），没有 mouse、selection、clipboard transport 或 OSC52 开关。

## 2. 来源支持的发现

1. **OMP TUI 的职责是输入分发与渲染，不是 tmux 选区管理。** `omp://tui-runtime-internals.md`「Runtime layers and ownership」「Input routing and focus model」明确把链路定义为 `stdin -> ProcessTerminal -> StdinBuffer -> TUI -> focusedComponent.handleInput`；同文「Terminal lifecycle and stdin normalization」说明启动时启用 raw mode、bracketed paste，并在停止时恢复终端模式、禁用 keyboard/mouse/appearance protocols。文档没有把鼠标拖选或系统剪贴板声明为 TUI 设置。

2. **备用屏幕与回到 shell 是生命周期行为。** `omp://tui-runtime-internals.md`「Shutdown and terminal handoff」写明 `TUI.stop()` 会离开 resize/fullscreen alternate-screen 状态并将终端交还；这描述 OMP 的屏幕生命周期，不等于 tmux copy-mode 的选择或复制策略。

3. **OMP 暴露的是应用内复制动作，而非 tmux 鼠标复制策略。** `omp://keybindings.md`「Clipboard actions」列出 `app.clipboard.copyLine`（Alt+Shift+L）、`app.clipboard.copyPrompt`（Alt+Shift+C）及粘贴动作；同节还说明 OSC 5522 是终端向 OMP 发送增强粘贴数据的输入协议。它没有提供“禁用鼠标选择写入系统剪贴板”的设置。

4. **OMP 的原生 clipboard API 不是 OSC52 传输。** `omp://natives-media-system-utils.md`「Clipboard」说明 `copyToClipboard(text)` 在 Linux 通过 `arboard::Clipboard::set_text` 持有 X11/Wayland selection；并明确写道当前 `packages/natives` TS wrapper **不会发 OSC52**，也不负责 Termux 或抑制 native clipboard 错误。该 API 属于 OMP 显式复制调用，不是 tmux pane 鼠标选区的 transport。

5. **可配置的 TUI 选项没有命中该问题。** `omp://settings.md`「Appearance and terminal」「Interaction」列出的终端/TUI 设置包括图像、超链接、硬件光标、滚动回放等；`tui.scrollbackRebuild` 只控制预览最终化时是否擦除并重放 scrollback，不能改变鼠标选区或系统剪贴板。

## 3. 结论

**没有证据表明 OMP 提供能导致该症状的 clipboard/mouse 设置；按官方文档，结论应定位在 OMP 之外。** OMP TUI 的 alternate-screen/raw-mode/鼠标协议管理，OMP 应用内复制快捷键，以及 tmux copy-mode 的鼠标选择，是三条不同路径。尤其，tmux pane 内的鼠标拖选通常由 tmux/终端处理；系统剪贴板是否更新还取决于 tmux copy-mode 的复制命令及其 clipboard transport（例如 OSC52），而官方 OMP 文档没有声明自己接管这条路径。这里对 tmux 行为的表述是边界定位，不把未查阅的 tmux 配置当作 OMP 事实。

## 4. 下一步安全诊断

不改任何配置，先在同一 tmux pane 外（直接终端 shell）与 pane 内分别做一次鼠标拖选；再在 pane 内明确进入 tmux copy-mode，完成一次复制并分别观察系统剪贴板是否变化。若仅 tmux pane 失败，下一步只读检查当前 tmux 的 `mouse`、`mode-keys`、`set-clipboard` 及终端 `$TERM`/OSC52 支持；不要先改 OMP 的 `tui.scrollbackRebuild` 或其它 TUI 设置，因为来源显示它们不控制该链路。

## 来源索引

- `omp://tui-runtime-internals.md`：Runtime layers and ownership；Input routing and focus model；Terminal lifecycle and stdin normalization；Shutdown and terminal handoff。
- `omp://keybindings.md`：Clipboard actions（复制/粘贴动作、OSC 5522）。
- `omp://natives-media-system-utils.md`：Clipboard。
- `omp://settings.md`：Appearance and terminal；Interaction。
- 本地只读事实：`omp config list --json`（2026-08-14，当前工作目录 `/home/pyadmin/claude-project/tmuxbot`）。
