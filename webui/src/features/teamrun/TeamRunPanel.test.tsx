import { render, screen } from "@testing-library/react";

import TeamRunPanel from "./TeamRunPanel";

function response(body: unknown): Response {
  return new Response(JSON.stringify(body), {
    headers: { "Content-Type": "application/json" },
  });
}

test("重新打开页面时恢复进行中的 TeamRun", async () => {
  vi.spyOn(globalThis, "fetch").mockImplementation((input) => {
    if (String(input).includes("/events") || String(input).includes("/dispatches") || String(input).includes("/worktrees")) {
      return Promise.resolve(response([]));
    }
    return Promise.resolve(response({
    run: { run_id: "run-active", goal: "恢复发布任务", state: "running" },
    agents: [],
    tasks: [{
      task_id: "implementation", title: "实现与验证", goal: "恢复发布任务",
      state: "working", attempt: 1,
    }],
    }));
  });

  render(
    <TeamRunPanel
      csrfToken="csrf"
      sessions={[]}
      runs={[{ run_id: "run-active", goal: "恢复发布任务", state: "running" }]}
      onRefresh={async () => undefined}
    />,
  );

  expect(await screen.findByText("恢复发布任务")).toBeVisible();
  expect(screen.getByText("状态 工作中 · working · 第 1 次尝试")).toBeVisible();
});
