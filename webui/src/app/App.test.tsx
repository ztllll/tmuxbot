import { StrictMode } from "react";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import App from "./App";

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

test("未配置时显示中文首次设置与短期授权状态", async () => {
  window.history.replaceState({}, "", "/setup#grant=local-one-time-grant");
  vi.spyOn(globalThis, "fetch").mockResolvedValue(
    jsonResponse({
      configured: false,
      setup_available: true,
      setup_expires_at: 2_000_000_600,
      csrf_token: "bootstrap-csrf",
    }),
  );

  render(<App />);

  expect(await screen.findByRole("heading", { name: "设置本机访问密码" })).toBeVisible();
  expect(screen.getByText("本机短期授权已就绪")).toBeVisible();
  expect(window.location.hash).toBe("");
});

test("React 严格模式不会在开发启动时丢失一次性授权", async () => {
  window.history.replaceState({}, "", "/setup#grant=local-one-time-grant");
  vi.spyOn(globalThis, "fetch").mockImplementation(async () => jsonResponse({
    configured: false,
    setup_available: true,
    csrf_token: "bootstrap-csrf",
  }));

  render(<StrictMode><App /></StrictMode>);

  expect(await screen.findByText("本机短期授权已就绪")).toBeVisible();
});

test("首次设置提交密码后进入真实系统概览", async () => {
  const user = userEvent.setup();
  window.history.replaceState({}, "", "/setup#grant=local-one-time-grant");
  const fetchMock = vi.spyOn(globalThis, "fetch")
    .mockResolvedValueOnce(jsonResponse({
      configured: false,
      setup_available: true,
      setup_expires_at: 2_000_000_600,
      csrf_token: "bootstrap-csrf",
    }))
    .mockResolvedValueOnce(jsonResponse({ csrf_token: "session-csrf" }, 201))
    .mockResolvedValueOnce(jsonResponse({
      host: { hostname: "forge-01", platform: "Linux", python_version: "3.12.4" },
      bridge: { status: "unconfigured", reason: "尚未配置通道和 binding" },
      tmux: { status: "ok", version: "tmux 3.4" },
      paths: { config: "/home/test/.config/tmuxbot", data: "/home/test/.local/share/tmuxbot" },
      providers: [{ name: "Claude", status: "found", version: "1.0", path: "/usr/bin/claude" }],
    }))
    .mockResolvedValueOnce(jsonResponse([]));

  render(<App />);
  await user.type(await screen.findByLabelText("新密码"), "correct horse battery staple");
  await user.type(screen.getByLabelText("确认密码"), "correct horse battery staple");
  await user.click(screen.getByRole("button", { name: "设置密码并进入控制台" }));

  expect(await screen.findByRole("heading", { name: "本机运行总览" })).toBeVisible();
  expect(screen.getByText("尚未配置通道和 binding")).toBeVisible();
  expect(screen.getByText("Claude")).toBeVisible();
  expect(fetchMock).toHaveBeenNthCalledWith(2, "/api/auth/setup", expect.objectContaining({
    headers: expect.objectContaining({
      "X-CSRF-Token": "bootstrap-csrf",
      "X-Setup-Token": "local-one-time-grant",
    }),
  }));
});

test("已配置但未登录时显示登录而不是虚构状态", async () => {
  vi.spyOn(globalThis, "fetch")
    .mockResolvedValueOnce(jsonResponse({
      configured: true,
      setup_available: false,
      csrf_token: "bootstrap-csrf",
    }))
    .mockResolvedValueOnce(jsonResponse({ detail: "authentication required" }, 401))
    .mockResolvedValueOnce(jsonResponse({ detail: "authentication required" }, 401));

  render(<App />);

  expect(await screen.findByRole("heading", { name: "登录本机控制台" })).toBeVisible();
  expect(screen.queryByText("本机运行总览")).not.toBeInTheDocument();
});

test("系统读取失败时说明原因并提供恢复动作", async () => {
  vi.spyOn(globalThis, "fetch")
    .mockResolvedValueOnce(jsonResponse({
      configured: true,
      setup_available: false,
      csrf_token: "bootstrap-csrf",
    }))
    .mockResolvedValueOnce(jsonResponse({ detail: "tmux inventory unavailable" }, 503));

  render(<App />);

  expect(await screen.findByRole("alert")).toHaveTextContent("无法读取本机运行状态");
  expect(screen.getByRole("button", { name: "重新读取" })).toBeVisible();
});

test("认证状态读取失败后可以原地重新读取", async () => {
  const user = userEvent.setup();
  vi.spyOn(globalThis, "fetch")
    .mockRejectedValueOnce(new TypeError("connection refused"))
    .mockResolvedValueOnce(jsonResponse({
      configured: false,
      setup_available: false,
      csrf_token: "bootstrap-csrf",
    }));

  render(<App />);
  await user.click(await screen.findByRole("button", { name: "重新读取" }));

  expect(await screen.findByRole("heading", { name: "设置本机访问密码" })).toBeVisible();
});
