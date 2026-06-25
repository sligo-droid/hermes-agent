import { describe, it, expect } from "vitest";
import { commandCenterLoadingState } from "./command-center-loading-state";
import type { CommandCenterSnapshot } from "./api";

const snapshot = { schema_version: 1, generated_at: 0 } as unknown as CommandCenterSnapshot;

describe("commandCenterLoadingState", () => {
  it("returns 'initial' when loading with no snapshot yet", () => {
    expect(commandCenterLoadingState({ loading: true, snapshot: null, projectSwitchPending: false })).toBe("initial");
  });

  it("returns 'initial' when loading with no snapshot even if a switch is pending", () => {
    expect(commandCenterLoadingState({ loading: true, snapshot: null, projectSwitchPending: true })).toBe("initial");
  });

  it("returns 'switching' when loading with a stale snapshot and a project switch pending", () => {
    expect(commandCenterLoadingState({ loading: true, snapshot, projectSwitchPending: true })).toBe("switching");
  });

  it("returns 'refreshing' when loading with a snapshot and no switch pending", () => {
    expect(commandCenterLoadingState({ loading: true, snapshot, projectSwitchPending: false })).toBe("refreshing");
  });

  it("returns 'ready' when not loading with a snapshot", () => {
    expect(commandCenterLoadingState({ loading: false, snapshot, projectSwitchPending: false })).toBe("ready");
  });

  it("returns 'switching' when a stale snapshot is still visible after loading settles", () => {
    expect(commandCenterLoadingState({ loading: false, snapshot, projectSwitchPending: true })).toBe("switching");
  });
});
