import type { CSSProperties } from "react";

export function Backdrop() {
  return (
    <div
      aria-hidden
      className="pointer-events-none absolute inset-0 z-0 overflow-hidden"
      style={{
        background:
          "var(--component-backdrop-background, var(--theme-asset-bg, radial-gradient(circle at top left, color-mix(in oklab, var(--color-midground) 18%, transparent), transparent 42%), radial-gradient(circle at bottom right, color-mix(in oklab, var(--color-accent) 14%, transparent), transparent 48%)))",
        backgroundPosition:
          "var(--component-backdrop-background-position, center)",
        backgroundRepeat:
          "var(--component-backdrop-background-repeat, no-repeat)",
        backgroundSize: "var(--component-backdrop-background-size, cover)",
        filter: "var(--component-backdrop-filter, none)",
        mixBlendMode: "var(--component-backdrop-mix-blend-mode, normal)" as CSSProperties["mixBlendMode"],
        opacity: "var(--component-backdrop-opacity, 1)",
      }}
    >
      <img
        alt=""
        className="theme-default-filler h-[160dvh] min-w-dvw object-cover opacity-[0.025] mix-blend-difference invert"
        fetchPriority="low"
        src="/ds-assets/filler-bg0.webp"
      />
    </div>
  );
}
