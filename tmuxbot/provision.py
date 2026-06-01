"""会话开通 / 拆除的前端无关公共逻辑。

`provision_chat`: 一个新 chat (TG /init 或飞书 /init) → 建项目目录 + 预置信任 +
建 tmux + 注册 binding (内存 + state) + 起 tailer + 持久化 yaml + 起 claude。
返回新 Binding (失败返回 None)。

`deprovision_chat`: 群解散 / bot 被移除 → 注销 binding (内存 + state) + 杀 tmux +
删 yaml 条目。**绝不删项目目录 / jsonl** (保留可恢复)。tailer 自行退出
(jsonl.py 循环顶部检测 binding 已不在 frontend.bindings → return)。

前端 (telegram / feishu) 只负责: ① 取 display_name ② 调本模块 ③ 回确认卡片。
provision 逻辑不重复实现。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from pathlib import Path
from typing import TYPE_CHECKING

import yaml

from tmuxbot.jsonl import jsonl_poll_loop
from tmuxbot.state import Binding
from tmuxbot.tmux import tmux_has_session, tmux_kill_session, tmux_new_session

if TYPE_CHECKING:
    from tmuxbot.state import State

log = logging.getLogger("tmuxbot")


class AsciiDirRequired(Exception):
    """群名含中文且未给英文目录参数 → 项目目录无法定为 ASCII, 拒绝开通。

    tmux session 名 / binding name 仍可用中文 (encode_cwd 能处理), 只有项目目录
    必须 ASCII。调用方 (telegram/feishu /init) 捕获后引导 Boss 用 /init <英文目录名>。
    """


def _safe_name(display_name: str, *, channel: str, chat_id) -> str:
    """display_name → tmux session 名 / binding name / 目录名。
    tmux target 用 ':' 和 '.' 分隔, 含这俩会破坏 target → 替换成 '-'。
    emoji / variation selector / zero-width joiner 对 tmux session 名不友好 → 直接剥掉。
    中文 OK (encode_cwd 已能处理)。空名 → channel + chat_id 后 8 位降级。"""
    # \u53bb emoji + \u7b26\u53f7: \u53ea\u7559 \u5b57\u6bcd\u6570\u5b57(\u542b\u5404\u56fd\u6587\u5b57)/\u4e2d\u6587/\u7a7a\u683c/-/_
    # \w \u914d re.UNICODE \u542b\u4e2d\u6587/\u5b57\u6bcd/\u6570\u5b57/\u4e0b\u5212\u7ebf, \u4f46\u4e0d\u542b emoji (\u9ad8\u4f4d\u5e73\u9762) \u2192 \u88ab\u5254\u9664
    name = re.sub(r"[^\w\u4e00-\u9fff \-]", "", display_name or "", flags=re.UNICODE)
    name = re.sub(r"\s+", " ", name).strip()        # \u6298\u53e0\u7a7a\u767d
    safe_name = re.sub(r"[:.]", "-", name).strip()  # \u4ecd\u9632 tmux target \u5206\u9694\u7b26(\w \u542b _ \u4e0d\u542b : .)
    if not safe_name:
        safe_name = f"{channel}-{str(chat_id)[-8:]}"
    return safe_name


def _preseed_trust_sync(proj_dir: str) -> None:
    """同步预置 ~/.claude.json 信任 (在 asyncio.to_thread 里调)。失败只 log 不中断。"""
    cfg_path = Path.home() / ".claude.json"
    try:
        cfg = json.loads(cfg_path.read_text()) if cfg_path.exists() else {}
    except Exception as e:
        log.warning(f"provision: 读 ~/.claude.json 失败 (跳过预置信任): {e}")
        return
    projects = cfg.setdefault("projects", {})
    projects[proj_dir] = {
        "hasTrustDialogAccepted": True,
        "hasCompletedProjectOnboarding": True,
        "projectOnboardingSeenCount": 1,
    }
    try:
        cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2))
    except Exception as e:
        log.warning(f"provision: 写 ~/.claude.json 失败 (跳过预置信任): {e}")


def _persist_binding_sync(bindings_file: "Path | None", entry: dict) -> None:
    """同步把新 binding append 到 bindings.yaml (在 asyncio.to_thread 里调)。"""
    if bindings_file is None:
        log.warning("provision: bindings_file 未配置, 跳过持久化")
        return
    try:
        raw = yaml.safe_load(bindings_file.read_text()) or {}
    except Exception as e:
        log.warning(f"provision: 读 bindings.yaml 失败 (跳过持久化): {e}")
        return
    raw.setdefault("bindings", []).append(entry)
    bindings_file.write_text(yaml.safe_dump(raw, allow_unicode=True, sort_keys=False))


def _remove_binding_sync(bindings_file: "Path | None", name: str) -> None:
    """同步从 bindings.yaml 删掉 name 匹配的 binding (在 asyncio.to_thread 里调)。"""
    if bindings_file is None:
        log.warning("deprovision: bindings_file 未配置, 跳过持久化删除")
        return
    try:
        raw = yaml.safe_load(bindings_file.read_text()) or {}
    except Exception as e:
        log.warning(f"deprovision: 读 bindings.yaml 失败 (跳过持久化删除): {e}")
        return
    entries = raw.get("bindings", [])
    raw["bindings"] = [e for e in entries if e.get("name") != name]
    bindings_file.write_text(yaml.safe_dump(raw, allow_unicode=True, sort_keys=False))


async def provision_chat(
    frontend,
    state: "State",
    *,
    chat_id,
    thread_id,
    display_name: str,
    offsets_file: "Path | None",
    bindings_file: "Path | None",
    bot_token_env: str,
    project_base: str,
    channel: str,
    target_dir: str | None = None,
) -> "Binding | None":
    """开通一个新 chat 的会话。前端无关。失败返回 None (不留半成品 binding)。

    thread_id: 飞书恒 None; TG forum topic 可能非 None — chat_id + thread_id 都进 binding。

    target_dir: /init 的可选目录参数。决定项目目录 (proj_dir):
      - None        → project_base/safe_name (现状: 目录 = base/群名)
      - 绝对路径    → 直接用作 proj_dir (任意位置)
      - 相对名      → project_base/target_dir (base 下指定子目录)
    无论 target_dir 取值, tmux session 名 / binding name 始终用 safe_name (群名派生),
    只有项目目录受 target_dir 控制。proj_dir 已存在不报错 (ensure_running 处理 resume)。
    """
    # 防重复: 已绑定的 chat 直接 None (并发 /init 也兜住)
    if frontend.find_binding(chat_id, thread_id) is not None:
        log.info(f"provision: chat_id={chat_id} thread_id={thread_id} 已绑定, 忽略")
        return None

    safe_name = _safe_name(display_name, channel=channel, chat_id=chat_id)
    # tmux/binding 名按 backend 加友好后缀区分: claude_code→-claude, codex→-codex。
    # (同一 chat 多 bot 各带后缀, 互不抢同一 tmux 名 + 一眼看出是哪个 CLI。)
    bname = frontend.backend.name
    suffix = {"claude_code": "claude"}.get(bname, bname)
    sess_name = f"{safe_name}-{suffix}"
    # 目录解析: 只有 proj_dir 受 target_dir 影响 (proj_dir 用 safe_name/target_dir, 非 sess_name)。
    # 项目目录名必须 ASCII (英文): 绝对路径直接用; 相对名/safe_name 含非 ASCII → 拒绝。
    # raise 在建目录/注册 binding 之前, 不留半成品。
    target_dir = (target_dir or "").strip() or None
    if target_dir is None:
        if not safe_name.isascii():
            raise AsciiDirRequired()
        proj_dir = f"{project_base}/{safe_name}"
    elif os.path.isabs(target_dir):
        proj_dir = target_dir
    elif target_dir.isascii():
        proj_dir = f"{project_base}/{target_dir}"
    else:
        raise AsciiDirRequired()
    b: "Binding | None" = None

    try:
        # 双保险: 再查一次 (上面到这里之间无 await, 但保持与原 feishu 行为等价)
        if frontend.find_binding(chat_id, thread_id) is not None:
            return None

        # 1. 建目录
        os.makedirs(proj_dir, exist_ok=True)

        # 2. 预置 claude 信任 (失败只 log)
        await asyncio.to_thread(_preseed_trust_sync, proj_dir)

        # 3. 建 tmux session
        if not tmux_has_session(sess_name):
            tmux_new_session(sess_name, proj_dir)

        # 4. 注册 binding (内存 + state)
        b = Binding(
            name=sess_name,
            chat_id=chat_id,
            thread_id=thread_id,
            tmux_session=sess_name,
            tmux_window=0,
            tmux_pane=0,
            cwd=Path(proj_dir),
            backend=frontend.backend.name,
            bot_token_env=bot_token_env,
            channel=channel,
        )
        frontend.bindings.append(b)
        state.bindings.append(b)

        # 5. 起 tailer
        state.fire(jsonl_poll_loop(b, frontend.backend, frontend, state, offsets_file))

        # 6. 持久化 bindings.yaml
        entry = {
            "name": sess_name,
            "channel": channel,
            "bot_token_env": bot_token_env,
            "backend": frontend.backend.name,
            "chat_id": chat_id,
            "thread_id": thread_id,
            "tmux_session": sess_name,
            "tmux_window": 0,
            "tmux_pane": 0,
            "cwd": proj_dir,
        }
        await asyncio.to_thread(_persist_binding_sync, bindings_file, entry)

        # 7. 起 claude
        await frontend.backend.ensure_running(b)

    except Exception:
        log.exception(f"provision failed for chat_id={chat_id} thread_id={thread_id}")
        # 回滚已注册的半成品 binding (tailer 见 binding 不在 frontend.bindings 自退出)
        if b is not None:
            try:
                frontend.bindings.remove(b)
            except ValueError:
                pass
            try:
                state.bindings.remove(b)
            except ValueError:
                pass
        return None

    log.info(f"provision ok: {safe_name} chat_id={chat_id} thread_id={thread_id} cwd={proj_dir}")
    return b


async def deprovision_chat(
    frontend,
    state: "State",
    binding: "Binding",
    *,
    bindings_file: "Path | None",
) -> None:
    """拆除一个 chat 的会话 (群解散 / bot 被移除)。

    只杀 tmux + 注销 binding (内存 + state) + 删 yaml 条目。
    **绝不删项目目录 / jsonl** (保留可恢复)。
    tailer 见 binding 已不在 frontend.bindings → 下一轮自行退出。
    """
    name = binding.name
    # 1. 注销 binding (内存 + state) — tailer 据此自退出
    try:
        frontend.bindings.remove(binding)
    except ValueError:
        pass
    try:
        state.bindings.remove(binding)
    except ValueError:
        pass

    # 2. 杀 tmux session
    try:
        tmux_kill_session(binding.tmux_session)
    except Exception as e:
        log.warning(f"deprovision: kill tmux {binding.tmux_session!r} err: {e}")

    # 3. 删 yaml 条目 (按 name 匹配)
    try:
        await asyncio.to_thread(_remove_binding_sync, bindings_file, name)
    except Exception as e:
        log.warning(f"deprovision: 删 yaml 条目 {name!r} err: {e}")

    # 4. 清理运行时状态残留
    state.last_active.pop(name, None)
    state.tui_fp.pop(name, None)
    state.picker_notified.pop(name, None)
    state.pending_rename.pop(name, None)
    state.tool_aggregator.pop(name, None)

    log.info(
        f"deprovision ok: {name} chat_id={binding.chat_id} thread_id={binding.thread_id} "
        f"(tmux killed, binding 注销, yaml 已删; 项目目录/jsonl 保留)"
    )
