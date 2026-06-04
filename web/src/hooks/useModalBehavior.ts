import { useEffect, useRef } from "react";

const FOCUSABLE_SELECTOR = [
  "a[href]",
  "button:not([disabled])",
  "textarea:not([disabled])",
  "input:not([disabled])",
  "select:not([disabled])",
  "details > summary:first-of-type",
  "[tabindex]:not([tabindex='-1'])",
].join(",");

type ModalEntry = {
  id: symbol;
  onClose: () => void;
  escapeDisabled: boolean;
  restoreTarget: HTMLElement | null;
};

const modalStack: ModalEntry[] = [];
let scrollLockCount = 0;
let previousBodyOverflow = "";

export function isFocusableElement(element: HTMLElement | null): element is HTMLElement {
  if (!element || !element.isConnected) return false;
  if (element.getAttribute("aria-hidden") === "true") return false;
  if (element.hasAttribute("disabled")) return false;
  const tabIndex = element.getAttribute("tabindex");
  if (tabIndex !== null && Number(tabIndex) < 0) return false;
  if (element.tabIndex < 0) return false;
  const style = window.getComputedStyle(element);
  if (style.visibility === "hidden" || style.display === "none") return false;
  return true;
}

function focusableElements(container: HTMLElement): HTMLElement[] {
  return Array.from(container.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR)).filter(isFocusableElement);
}

export function nextTabTarget(container: HTMLElement, active: Element | null, shiftKey: boolean): HTMLElement | null {
  const focusable = focusableElements(container);
  if (!focusable.length) return container;
  const first = focusable[0];
  const last = focusable[focusable.length - 1];
  if (!container.contains(active)) return first;
  if (shiftKey && active === first) return last;
  if (!shiftKey && active === last) return first;
  return null;
}

function initialFocusTarget(container: HTMLElement): HTMLElement {
  return (
    container.querySelector<HTMLElement>("[data-autofocus]") ||
    container.querySelector<HTMLElement>("[autofocus]") ||
    focusableElements(container)[0] ||
    container
  );
}

function topModal(): ModalEntry | undefined {
  return modalStack[modalStack.length - 1];
}

function lockBodyScroll() {
  if (scrollLockCount === 0) {
    previousBodyOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
  }
  scrollLockCount += 1;
}

function unlockBodyScroll() {
  scrollLockCount = Math.max(0, scrollLockCount - 1);
  if (scrollLockCount === 0) {
    document.body.style.overflow = previousBodyOverflow;
  }
}

/**
 * Hook that adds standard modal behaviors when `open` is true:
 * - Focus moves into the modal and stays trapped there
 * - Escape key calls `onClose` for the topmost modal
 * - Body scroll is locked while any modal is open
 * - Focus is restored to the previously focused element on close when possible
 *
 * Returns a ref to attach to the modal container.
 */
export function useModalBehavior({
  disableEscape = false,
  open,
  onClose,
}: {
  disableEscape?: boolean;
  open: boolean;
  onClose: () => void;
}) {
  const containerRef = useRef<HTMLDivElement>(null);
  const closeRef = useRef(onClose);
  closeRef.current = onClose;

  useEffect(() => {
    if (!open) return;

    const id = Symbol("modal");
    const entry: ModalEntry = {
      id,
      onClose: () => closeRef.current(),
      escapeDisabled: disableEscape,
      restoreTarget: document.activeElement instanceof HTMLElement ? document.activeElement : null,
    };
    modalStack.push(entry);
    lockBodyScroll();

    window.requestAnimationFrame(() => {
      if (topModal()?.id !== id) return;
      const container = containerRef.current;
      if (!container) return;
      if (!container.hasAttribute("tabindex")) {
        container.tabIndex = -1;
      }
      initialFocusTarget(container).focus({ preventScroll: true });
    });

    const onKey = (e: KeyboardEvent) => {
      if (topModal()?.id !== id) return;
      if (e.key === "Escape") {
        if (entry.escapeDisabled) return;
        e.preventDefault();
        entry.onClose();
        return;
      }
      if (e.key !== "Tab") return;

      const container = containerRef.current;
      if (!container) return;
      const target = nextTabTarget(container, document.activeElement, e.shiftKey);
      if (target) {
        e.preventDefault();
        target.focus({ preventScroll: true });
      }
    };

    document.addEventListener("keydown", onKey);

    return () => {
      document.removeEventListener("keydown", onKey);
      const stackIndex = modalStack.findIndex((candidate) => candidate.id === id);
      if (stackIndex >= 0) modalStack.splice(stackIndex, 1);
      unlockBodyScroll();
      if (isFocusableElement(entry.restoreTarget)) {
        entry.restoreTarget.focus({ preventScroll: true });
      }
    };
  }, [disableEscape, open]);

  return containerRef;
}
