import { afterEach, describe, expect, it, vi } from "vitest";

import { api } from "./api";

describe("self-improvement approval API", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    Reflect.deleteProperty(globalThis, "window");
  });

  it("defaults proposal approval to the Discord worker-board route", async () => {
    Object.defineProperty(globalThis, "window", {
      configurable: true,
      value: { location: { origin: "http://localhost" } },
    });
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue({
      status: 200,
      ok: true,
      json: async () => ({ card: {}, task: null, worker_url: "" }),
    } as Response);

    await api.approveSelfImprovementProposal("proposal-1");

    expect(fetchMock).toHaveBeenCalledOnce();
    const [, init] = fetchMock.mock.calls[0];
    expect(JSON.parse(String(init?.body))).toEqual({ route: "worker_board" });
  });
});
