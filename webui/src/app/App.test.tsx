import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import App from "./App";

function response(body: unknown, status = 200) { return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } }); }

test("展示 tmux window 并可加入终端墙", async () => {
  const user = userEvent.setup();
  vi.spyOn(globalThis, "fetch").mockResolvedValue(response([{ target: "alpha:0", session_name: "alpha", window_index: 0, pane_count: 2, commands: ["bash", "claude"], cwd_summary: "/repo" }]));
  render(<App />);
  expect(await screen.findByText("alpha:0")).toBeVisible();
  await user.click(screen.getByText("alpha:0"));
  expect(await screen.findByText("1 个 window")).toBeVisible();
});

test("inventory 失败提供重试", async () => {
  const user = userEvent.setup();
  vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(response({}, 503)).mockResolvedValueOnce(response([]));
  render(<App />);
  await user.click(await screen.findByRole("button", { name: "重新读取" }));
  expect(await screen.findByText("本机终端墙")).toBeVisible();
});
