import { subscribe, unsubscribe, type FeedItem } from "./api";
import { observeSeen } from "./seen";

/** Reddit-style URL for a field: /r/<field>. */
export function fieldHref(slug: string) {
  return `/r/${encodeURIComponent(slug)}`;
}

/** Reddit-style URL for a post: /r/<field>/<post-id>. */
export function paperHref(fieldSlug: string, s2PaperId: string) {
  return `/r/${encodeURIComponent(fieldSlug)}/${encodeURIComponent(s2PaperId)}`;
}

export interface CardCtx {
  /** Slugs the current user is subscribed to (mutated in place on toggle). */
  subscribedSlugs: Set<string>;
  /** Whether a user is signed in. */
  signedIn: () => boolean;
  /** Called when an action needs login (e.g. to show a hint). */
  onNeedLogin: () => void;
  /** Show the subscribe button (hidden on single-field pages). Default true. */
  showSubscribe?: boolean;
}

function escapeHtml(s: string) {
  return (s ?? "").replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]!
  ));
}

function formatAuthors(a: string[]) {
  if (!a?.length) return "";
  return a.length <= 3 ? a.join(", ") : a.slice(0, 3).join(", ") + " et al.";
}

export function createCard(item: FeedItem, ctx: CardCtx): HTMLElement {
  const showSub = ctx.showSubscribe !== false;
  const subscribed = ctx.subscribedSlugs.has(item.field.slug);
  const el = document.createElement("article");
  el.className =
    "cursor-pointer rounded-xl border border-border bg-card  " +
    "transition-colors duration-150 hover:border-accent p-4";
  const joinPill = showSub
    ? `<button class="sub-btn rounded-full px-[0.7rem] py-[0.1rem] text-[0.72rem] font-semibold ${subscribed ? "" : "primary"}" data-slug="${item.field.slug}">${subscribed ? "Joined" : "Join"}</button>`
    : "";
  el.innerHTML = `
    <div class="flex items-center justify-between text-[0.8rem] text-muted">
      <span class="flex items-center gap-2">
        <span class="font-semibold text-accent"><a href="/r/${item.field.slug}" class="hover:underline">r/${item.field.slug}</a></span>
        ${joinPill}
      </span>
      <span>${item.citation_count} citations · ${item.year ?? ""}</span>
    </div>
    <h2 class="mt-2 mb-[0.4rem] text-[1.2rem] leading-[1.3] hover:underline">${escapeHtml(item.summary_title)}</h2>
    <p class="mb-[0.8rem] leading-normal text-text">${escapeHtml(item.summary_body)}</p>
    <div class="border-t border-border pt-[0.6rem] text-[0.9rem]">
      <a class="font-semibold hover:underline" href="${item.url}" target="_blank" rel="noopener">${escapeHtml(item.paper_title)}</a>
      <div class="mt-[0.2rem] text-[0.82rem] text-muted">${escapeHtml(formatAuthors(item.authors))} · ${escapeHtml(item.venue)}</div>
    </div>
    <div class="mt-[0.7rem] text-[0.8rem] text-muted hover:underline">Read the detailed summary →</div>`;

  // Navigate to the post's own page (but not when clicking links/buttons).
  el.addEventListener("click", (e) => {
    const t = e.target as HTMLElement;
    if (t.closest("a") || t.closest("button")) return;
    location.href = paperHref(item.field.slug, item.s2_paper_id);
  });

  if (showSub) {
    const btn = el.querySelector(".sub-btn") as HTMLButtonElement;
    btn.onclick = () => toggleSub(item.field.slug, ctx);
  }
  // Mark as seen once it scrolls into view (signed-in users only).
  observeSeen(el, item.paper_id);
  return el;
}

async function toggleSub(slug: string, ctx: CardCtx) {
  if (!ctx.signedIn()) { ctx.onNeedLogin(); return; }
  const buttons = document.querySelectorAll<HTMLButtonElement>(`.sub-btn[data-slug="${slug}"]`);
  buttons.forEach((b) => (b.disabled = true));
  try {
    if (ctx.subscribedSlugs.has(slug)) {
      await unsubscribe(slug);
      ctx.subscribedSlugs.delete(slug);
    } else {
      await subscribe(slug);
      ctx.subscribedSlugs.add(slug);
    }
    const on = ctx.subscribedSlugs.has(slug);
    buttons.forEach((b) => {
      b.textContent = on ? "Joined" : "Join";
      b.classList.toggle("primary", !on);
    });
  } catch (e) { console.error(e); }
  buttons.forEach((b) => (b.disabled = false));
}
