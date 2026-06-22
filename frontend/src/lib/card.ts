import {
  subscribe, unsubscribe, likePaper, unlikePaper, getCollections, createCollection,
  getCollectionsForPaper, addToCollection, removeFromCollection, type FeedItem,
} from "./api";
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
  /** Paper ids the current user has liked (mutated in place on toggle). */
  likedPaperIds?: Set<number>;
  /** Paper ids saved in ANY of the user's collections (mutated in place). */
  savedPaperIds?: Set<number>;
  /** Whether a user is signed in. */
  signedIn: () => boolean;
  /** Called when an action needs login (e.g. to show a login prompt). */
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

// ── Like / save icons ────────────────────────────────────────────────────────
// Outline (red border, transparent) when not liked/saved; filled red once it is.

const RED = "var(--color-bad)";

function heartIcon(filled: boolean): string {
  return `<svg viewBox="0 0 512 512" width="20" height="20" aria-hidden="true">
    <path d="M352.92 80C288 80 256 144 256 144s-32-64-96.92-64c-52.76 0-94.54 44.14-95.08 96.81-1.1 109.33 86.73 187.08 183 252.42a16 16 0 0 0 17.95 0c96.26-65.34 184.09-143.09 183-252.42-.54-52.67-42.32-96.81-95.08-96.81z"
      fill="${filled ? RED : "none"}" stroke="${RED}" stroke-width="32" stroke-linejoin="round" stroke-linecap="round"/>
  </svg>`;
}

function bookmarkIcon(filled: boolean): string {
  return `<svg viewBox="0 0 512 512" width="20" height="20" aria-hidden="true">
    <path d="M358 64H154a38 38 0 0 0-38 38v344l139.32-90.91a8 8 0 0 1 9.36 0L404 446V102a38 38 0 0 0-38-38z"
      fill="${filled ? RED : "none"}" stroke="${RED}" stroke-width="32" stroke-linejoin="round" stroke-linecap="round"/>
  </svg>`;
}

/** Heart (like) + bookmark (save-to-collection) buttons, fully wired —
 *  shared between feed/search cards and the paper detail page. The returned
 *  element is `relative`, so the save popover can position against it. */
export function createLikeSaveControls(paperId: number, ctx: CardCtx): HTMLElement {
  const liked = ctx.likedPaperIds?.has(paperId) ?? false;
  const saved = ctx.savedPaperIds?.has(paperId) ?? false;

  const wrap = document.createElement("span");
  wrap.className = "like-save-controls relative inline-flex items-center gap-3";
  wrap.dataset.paperId = String(paperId);
  wrap.innerHTML = `
    <button class="like-btn !border-0 !bg-transparent !p-0 leading-none" aria-label="Like" aria-pressed="${liked}">${heartIcon(liked)}</button>
    <button class="save-btn !border-0 !bg-transparent !p-0 leading-none" aria-label="Save to collection" aria-pressed="${saved}">${bookmarkIcon(saved)}</button>`;

  const likeBtn = wrap.querySelector(".like-btn") as HTMLButtonElement;
  likeBtn.onclick = (e) => { e.stopPropagation(); toggleLike(paperId, likeBtn, ctx); };

  const saveBtn = wrap.querySelector(".save-btn") as HTMLButtonElement;
  saveBtn.onclick = (e) => { e.stopPropagation(); toggleSavePopover(paperId, saveBtn, wrap, ctx); };

  return wrap;
}

export function createCard(item: FeedItem, ctx: CardCtx): HTMLElement {
  const showSub = ctx.showSubscribe !== false;
  const subscribed = ctx.subscribedSlugs.has(item.field.slug);
  const el = document.createElement("article");
  el.className =
    "relative cursor-pointer rounded-xl border border-border bg-card  " +
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
    <div class="card-actions mt-[0.7rem] flex items-center justify-between">
      <span class="text-[0.8rem] text-muted hover:underline">Read the detailed summary →</span>
    </div>`;

  el.querySelector(".card-actions")!.appendChild(createLikeSaveControls(item.paper_id, ctx));

  // Navigate to the post's own page (but not when clicking links/buttons/popovers).
  el.addEventListener("click", (e) => {
    const t = e.target as HTMLElement;
    if (t.closest("a") || t.closest("button") || t.closest(".like-save-controls")) return;
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

async function toggleLike(paperId: number, btn: HTMLButtonElement, ctx: CardCtx) {
  if (!ctx.signedIn()) { ctx.onNeedLogin(); return; }
  const liked = ctx.likedPaperIds ?? (ctx.likedPaperIds = new Set<number>());
  btn.disabled = true;
  try {
    if (liked.has(paperId)) {
      await unlikePaper(paperId);
      liked.delete(paperId);
    } else {
      await likePaper(paperId);
      liked.add(paperId);
      // Liking saves into the default "Liked Papers" collection.
      ctx.savedPaperIds?.add(paperId);
    }
    const on = liked.has(paperId);
    btn.innerHTML = heartIcon(on);
    btn.setAttribute("aria-pressed", String(on));
  } catch (e) { console.error(e); }
  btn.disabled = false;
}

// ── Save-to-collection popover ───────────────────────────────────────────────
// One popover open at a time; closing the previous one before opening a new
// (or the same) button's keeps clicks from stacking duplicate menus.

let openSavePopover: HTMLElement | null = null;
let closeOnDocClick: ((e: MouseEvent) => void) | null = null;

function closeSavePopover() {
  openSavePopover?.remove();
  openSavePopover = null;
  if (closeOnDocClick) {
    document.removeEventListener("click", closeOnDocClick);
    closeOnDocClick = null;
  }
}

function updateSaveButtonIcon(paperId: number, ctx: CardCtx) {
  const saved = ctx.savedPaperIds?.has(paperId) ?? false;
  document.querySelectorAll<HTMLButtonElement>(".save-btn").forEach((btn) => {
    // Only the button that opened the currently-tracked popover is guaranteed
    // to correspond to this paperId on multi-card pages, so scope by wrapper.
    const wrap = btn.closest<HTMLElement>(".like-save-controls");
    if (wrap?.dataset.paperId === String(paperId)) {
      btn.innerHTML = bookmarkIcon(saved);
      btn.setAttribute("aria-pressed", String(saved));
    }
  });
}

async function toggleSavePopover(
  paperId: number, btn: HTMLButtonElement, wrap: HTMLElement, ctx: CardCtx,
) {
  if (!ctx.signedIn()) { ctx.onNeedLogin(); return; }
  const wasOpenForThisButton = openSavePopover?.dataset.forPaper === String(paperId);
  closeSavePopover();
  if (wasOpenForThisButton) return;

  const popover = document.createElement("div");
  popover.dataset.forPaper = String(paperId);
  popover.className =
    "like-save-controls absolute right-0 top-full z-30 mt-1 w-56 rounded-lg border border-accent bg-card p-2 shadow-lg";
  popover.innerHTML = `<p class="m-0 mb-1 px-1 text-[0.78rem] text-muted">Loading collections…</p>`;
  wrap.appendChild(popover);
  openSavePopover = popover;
  closeOnDocClick = (e) => {
    if (!popover.contains(e.target as Node) && e.target !== btn) closeSavePopover();
  };
  document.addEventListener("click", closeOnDocClick);

  try {
    const [{ collections }, { collection_ids }] = await Promise.all([
      getCollections(),
      getCollectionsForPaper(paperId),
    ]);
    renderSaveOptions(popover, paperId, collections.map((c) => ({ id: c.id, name: c.name })),
      new Set(collection_ids), ctx);
  } catch (e) {
    console.error(e);
    popover.innerHTML = `<p class="m-0 px-1 text-[0.78rem] text-muted">Failed to load collections.</p>`;
  }
}

function renderSaveOptions(
  popover: HTMLElement, paperId: number, collections: { id: number; name: string }[],
  inCollections: Set<number>, ctx: CardCtx,
) {
  popover.innerHTML = "";
  if (!collections.length) {
    const p = document.createElement("p");
    p.className = "m-0 mb-2 px-1 text-[0.78rem] text-muted";
    p.textContent = "No collections yet.";
    popover.appendChild(p);
  } else {
    for (const c of collections) {
      const added = inCollections.has(c.id);
      const item = document.createElement("button");
      item.className =
        "flex w-full items-center justify-between rounded-md px-2 py-[0.35rem] text-left text-[0.85rem] text-text hover:bg-bg";
      item.innerHTML = `<span>${c.name}</span>${added ? '<span class="text-accent">✓</span>' : ""}`;
      item.onclick = async () => {
        item.disabled = true;
        try {
          if (added) {
            await removeFromCollection(c.id, paperId);
            inCollections.delete(c.id);
          } else {
            await addToCollection(c.id, paperId);
            inCollections.add(c.id);
          }
          if (inCollections.size > 0) ctx.savedPaperIds?.add(paperId);
          else ctx.savedPaperIds?.delete(paperId);
          updateSaveButtonIcon(paperId, ctx);
          renderSaveOptions(popover, paperId, collections, inCollections, ctx);
        } catch (e) { console.error(e); item.disabled = false; }
      };
      popover.appendChild(item);
    }
  }

  const form = document.createElement("form");
  form.className = "mt-1 flex gap-1 border-t border-border pt-2";
  form.innerHTML = `
    <input type="text" placeholder="New collection…" required
           class="min-w-0 flex-1 rounded-md border border-border bg-bg px-2 py-1 text-[0.8rem] text-text outline-none focus:border-accent" />
    <button type="submit" class="rounded-md px-2 py-1 text-[0.8rem]">Add</button>`;
  const input = form.querySelector("input") as HTMLInputElement;
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const name = input.value.trim();
    if (!name) return;
    const submitBtn = form.querySelector("button")!;
    submitBtn.setAttribute("disabled", "true");
    try {
      const { collection_id } = await createCollection(name);
      await addToCollection(collection_id, paperId);
      inCollections.add(collection_id);
      ctx.savedPaperIds?.add(paperId);
      updateSaveButtonIcon(paperId, ctx);
      renderSaveOptions(popover, paperId, [...collections, { id: collection_id, name }], inCollections, ctx);
    } catch (e) { console.error(e); submitBtn.removeAttribute("disabled"); }
  });
  popover.appendChild(form);
}
