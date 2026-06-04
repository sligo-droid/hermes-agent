export type DashboardSurface = "hermes" | "sligo" | "combined";

const DEFAULT_HERMES_HOST = "hermes.sligolabs.com";
const DEFAULT_SLIGO_HOST = "sligo.sligolabs.com";

declare global {
  interface Window {
    __HERMES_DASHBOARD_SURFACE__?: DashboardSurface;
    __HERMES_SLIGO_DASHBOARD_HOST__?: string;
  }
}

function normalizeHost(host: string | undefined): string {
  if (!host) return "";
  const trimmed = host.trim().toLowerCase();
  if (trimmed.startsWith("[")) {
    const end = trimmed.indexOf("]");
    return end >= 0
      ? trimmed.slice(1, end)
      : trimmed.replaceAll("[", "").replaceAll("]", "");
  }
  return trimmed.split(":", 1)[0];
}

export function dashboardSurfaceForHost(host: string | undefined): DashboardSurface {
  const normalized = normalizeHost(host);
  if (normalized === DEFAULT_HERMES_HOST) return "hermes";
  if (normalized === DEFAULT_SLIGO_HOST) return "sligo";
  return "combined";
}

function injectedSurface(): DashboardSurface | null {
  if (typeof window === "undefined") return null;
  const surface = window.__HERMES_DASHBOARD_SURFACE__;
  return surface === "hermes" || surface === "sligo" || surface === "combined"
    ? surface
    : null;
}

export function currentDashboardSurface(): DashboardSurface {
  if (typeof window === "undefined") return "combined";
  return injectedSurface() ?? dashboardSurfaceForHost(window.location.host);
}

export function sligoHostUrl(path: string): string {
  const suffix = path.startsWith("/") ? path : `/${path}`;
  const host =
    typeof window !== "undefined" && window.__HERMES_SLIGO_DASHBOARD_HOST__
      ? window.__HERMES_SLIGO_DASHBOARD_HOST__
      : DEFAULT_SLIGO_HOST;
  return `https://${host}${suffix}`;
}
