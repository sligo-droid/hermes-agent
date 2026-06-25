import type { CommandCenterSnapshot } from "@/lib/api";

export type CommandCenterLoadingState = "initial" | "switching" | "refreshing" | "ready";

export interface CommandCenterLoadingStateInput {
  loading: boolean;
  snapshot: CommandCenterSnapshot | null;
  projectSwitchPending: boolean;
}

export function commandCenterLoadingState(input: CommandCenterLoadingStateInput): CommandCenterLoadingState {
  const { loading, snapshot, projectSwitchPending } = input;
  const hasSnapshot = Boolean(snapshot);
  if (loading && !hasSnapshot) return "initial";
  if (hasSnapshot && projectSwitchPending) return "switching";
  if (loading && hasSnapshot && !projectSwitchPending) return "refreshing";
  return "ready";
}
