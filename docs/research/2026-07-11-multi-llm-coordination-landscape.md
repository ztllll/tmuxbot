# Multi-LLM Coordination Landscape

Date: 2026-07-11 UTC

Status: research only; no orchestration runtime implementation is authorized by this document.

## Research question

How should tmuxbot evolve from independent Claude and Codex tmux sessions sharing a project directory into a reliable multi-LLM collaboration platform without replacing tmux as the execution plane?

## Systems reviewed

### Native coding-agent teams

- [Claude Code Agent Teams](https://code.claude.com/docs/en/agent-teams): team lead, independent teammates, shared task list, mailbox, direct teammate messaging, and explicit warnings about file conflicts because teammates do not automatically receive isolated worktrees.
- [Codex subagents](https://developers.openai.com/codex/subagents): parent-managed agent threads, specialized custom agents, parallel delegation, steering, and a recommendation to be cautious with parallel write-heavy work.
- [OpenHands delegation](https://docs.openhands.dev/sdk/guides/agent-delegation): independent contexts, shared workspace support, parallel delegation, and consolidated observations returned to the main agent.

### General orchestration frameworks

- [AutoGen](https://microsoft.github.io/autogen/stable/user-guide/core-user-guide/framework/agent-and-agent-runtime.html): event-driven runtime, typed agents, messages, topics, subscriptions, group-chat managers, and handoffs.
- [CrewAI](https://docs.crewai.com/en/concepts/production-architecture): a flow-first architecture where deterministic state/control lives in Flows and autonomous collaboration is delegated to focused Crews.
- [LangGraph Agent Server](https://langchain-ai.github.io/langgraph/tutorials/langgraph-platform/local-server/): durable runs, thread serialization, task queues, checkpoints, streaming events, and separate orchestration/execution processes at larger scale.
- [AgentScope](https://github.com/agentscope-ai/agentscope): event system, permission controls, multi-session service, workspace abstraction, middleware, sandbox backends, and built-in observability.

### Software-company and structured-workflow systems

- [MetaGPT](https://arxiv.org/html/2308.00352): role specialization, SOP-driven stages, structured artifacts, message pool, subscriptions, and executable feedback. Its most relevant lesson is to exchange PRDs, designs, task lists, code, and test results rather than unconstrained dialogue.
- [ChatDev](https://github.com/OpenBMB/ChatDev): configurable roles, workflow graph/canvas, intermediate artifacts, replayable logs, and human-in-the-loop feedback.
- [Magentic-One](https://www.microsoft.com/en-us/research/publication/magentic-one-a-generalist-multi-agent-system-for-solving-complex-tasks/): an orchestrator maintains a task ledger and progress ledger, selects the next agent, detects stalls/loops, replans, and synthesizes a final answer.

### Interoperability protocols

- [A2A Protocol 1.0](https://a2a-protocol.org/v1.0.0/specification/): Agent Cards, stateful Tasks, Messages, Parts, Artifacts, streaming updates, task retrieval/cancellation, and explicit terminal/interrupted task states.
- A2A's immutable terminal-task model and `contextId`/`taskId` separation are useful even for a local-only tmux implementation because they make follow-up tasks and artifact lineage auditable.

### Coordination evidence and failure modes

- [MultiAgentBench](https://arxiv.org/abs/2503.01935) reports that moderate team sizes can improve coordination, while adding more agents can reduce overall performance because coordination complexity grows.
- [CAID](https://arxiv.org/pdf/2603.21489) identifies centralized delegation, asynchronous execution, isolated workspaces, branch-and-merge integration, and executable tests as effective software-engineering coordination primitives.
- A recent [empirical evaluation of code-agent frameworks](https://arxiv.org/html/2511.00872v1) finds that coordination strategy, feedback integration, and reasoning depth matter more than simply increasing agent count; planning/reflection also dominate token overhead in multi-agent workflows.

## Reusable design principles for tmuxbot

1. Keep tmux sessions as provider runtimes. The coordinator sends structured assignments into existing panes and observes normalized provider events.
2. Separate deterministic orchestration from model autonomy. Python owns task state, dependencies, leases, limits, approvals, and artifact gates; LLMs own planning, implementation, review, and domain judgment within assigned tasks.
3. Use structured envelopes instead of free-form agent chat. Minimum fields should include task ID, context ID, sender, recipient, role, objective, constraints, dependencies, expected artifacts, status, attempt, and evidence.
4. Make artifacts first-class. Plans, diffs, commits, test reports, screenshots, and review findings should be referenced by stable IDs and paths rather than pasted repeatedly into every agent context.
5. Start with two agents and centralized scheduling. More agents should be admitted only when evaluation shows a measurable gain.
6. Prevent simultaneous shared-tree writes. The safe modes are sequential shared-worktree ownership or parallel isolated worktrees followed by tested integration.
7. Add progress and stall ledgers. Detect repeated messages, unchanged git state, repeated failing commands, missing artifacts, excessive turns, and inactive panes; replan or request human input after bounded retries.
8. Require executable gates. A task is not completed because an agent says so; required files, commits, tests, linters, or deployment checks must pass.
9. Preserve human control. Plan approval, destructive actions, cross-agent merges, deployment, and conflict resolution need explicit policy and observable state.
10. Trace every transition. Store normalized events, assignments, messages, artifacts, approvals, costs, and final outcomes in an append-only run log.

## Recommended future project phases

No phase below is implemented yet.

1. Coordination contracts: define AgentCard, Task, Message, Artifact, TeamRun, task states, and event log schemas.
2. Claude-Codex sequential pilot: one coordinator, two existing tmux sessions, exclusive workspace lease, explicit planner/implementer/reviewer handoffs.
3. Evaluation harness: compare single-agent and dual-agent runs on fixed repository tasks using success, latency, tokens, interventions, conflicts, and regression rates.
4. Isolated parallel execution: git worktrees, task ownership, commit-based outputs, integration queue, conflict detection, and full-test gates.
5. Dynamic orchestration: capability routing, dependency-aware parallelism, stall detection, replanning, and bounded retries.
6. Channel UI: expose team state, task board, agent status, artifacts, approvals, costs, and stop controls through the explicit `/panel` surface.

## Decision deferred

The project should not select AutoGen, CrewAI, LangGraph, A2A, or another framework as a dependency until the local coordination contracts and evaluation harness exist. Their patterns are valuable, but tmuxbot's differentiator is operating real provider CLIs inside user-owned tmux sessions; adopting a framework prematurely could replace that advantage with an API-centric runtime.
