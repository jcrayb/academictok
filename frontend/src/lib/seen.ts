// Tracks which feed cards a signed-in user has actually scrolled past and reports
// them to the server (batched), so the main feed can prioritize new content.
//
// "Seen" means the card was on screen for at least MIN_VISIBLE_MS (continuously)
// and then scrolled up out of view past the top of the viewport — not merely
// that it appeared. This guards against a fast flick through the whole feed
// marking every card as seen: a card only becomes eligible once it's stayed
// in view past the time threshold, and a brief glimpse below that resets.
// Cards still below the fold, or visible at the bottom when you stop, don't
// count either. The queue flushes on a short debounce (and on page hide).
import { $user } from "../stores/auth";
import { markSeen } from "./api";

/** How long (ms) a card must stay continuously in view before it's eligible
 * to be marked "seen" once scrolled past. Global so it's easy to tune. */
export let MIN_VISIBLE_MS = 800;

const pending = new Set<number>();
let timer: number | undefined;
// Pending "has it stayed visible long enough" timers, keyed by element.
const visibleTimers = new Map<HTMLElement, number>();

const observer =
  typeof IntersectionObserver !== "undefined"
    ? new IntersectionObserver(
        (entries) => {
          for (const e of entries) {
            const el = e.target as HTMLElement;
            if (e.isIntersecting) {
              // Start the dwell timer once; it flips wasVisible only if the
              // card is still in view when it fires.
              if (el.dataset.wasVisible !== "1" && !visibleTimers.has(el)) {
                const t = window.setTimeout(() => {
                  el.dataset.wasVisible = "1";
                  visibleTimers.delete(el);
                }, MIN_VISIBLE_MS);
                visibleTimers.set(el, t);
              }
              continue;
            }
            // Left view before the dwell timer fired: it never counted as
            // "visible long enough", so cancel and don't mark it seen.
            const pendingTimer = visibleTimers.get(el);
            if (pendingTimer != null) {
              clearTimeout(pendingTimer);
              visibleTimers.delete(el);
            }
            // Out of view: only "seen" if it had dwelled long enough and has
            // now scrolled up past the top (fully above the viewport's top
            // edge), not if it's still waiting below the fold.
            if (el.dataset.wasVisible !== "1") continue;
            const top = e.rootBounds ? e.rootBounds.top : 0;
            if (e.boundingClientRect.bottom > top) continue; // exited via bottom
            observer!.unobserve(el);
            const id = Number(el.dataset.paperId);
            if (id) {
              pending.add(id);
              schedule();
            }
          }
        },
        { threshold: 0 },
      )
    : null;

function schedule() {
  if (timer != null) return;
  timer = window.setTimeout(flush, 1500);
}

async function flush() {
  timer = undefined;
  if (!pending.size) return;
  const ids = [...pending];
  pending.clear();
  try {
    await markSeen(ids);
  } catch (e) {
    console.error(e);
  }
}

if (typeof document !== "undefined") {
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "hidden") flush();
  });
}

/** Start tracking a card as "seen" once it scrolls into view. No-op signed out. */
export function observeSeen(el: HTMLElement, paperId: number) {
  if (!observer || !$user.get()) return;
  el.dataset.paperId = String(paperId);
  observer.observe(el);
}
