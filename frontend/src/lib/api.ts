import { $user, getIdToken } from "../stores/auth";

const BASE = import.meta.env.PUBLIC_API_URL ?? "";

/** Base URL of the Flask API; used to resolve served media (figure images). */
export const API_BASE = BASE;

async function apiFetch(path: string, options: RequestInit = {}) {
  const res = await fetch(`${BASE}${path}`, options);
  if (!res.ok && ![400, 401, 403].includes(res.status)) {
    throw new Error(`API ${res.status}: ${path}`);
  }
  return res.json();
}

/** Build an Authorization header + uid + token for the current user. */
async function authParams(): Promise<{ headers: Record<string, string>; uid: string; token: string }> {
  const user = $user.get();
  if (!user) throw new Error("Not authenticated");
  const token = await getIdToken();
  return { headers: { Authorization: `Bearer ${token}` }, uid: user.uid, token };
}

export interface FeedItem {
  paper_id: number;
  s2_paper_id: string;
  paper_title: string;
  summary_title: string;
  summary_body: string;
  authors: string[];
  year: number | null;
  venue: string;
  url: string;
  citation_count: number;
  field: { slug: string; name: string };
}

export interface Field {
  id: number;
  slug: string;
  name: string;
  description: string;
  keywords: string[];
  is_seed: number;
  paper_count: number;
}

export interface PaperImage {
  filename: string;
  page: number | null;
  width: number | null;
  height: number | null;
  caption: string | null;
  ord: number;
  url: string;
}

export interface PaperDetail extends Omit<FeedItem, "field"> {
  fields: { slug: string; name: string; is_primary: boolean }[];
  abstract: string | null;
  long_body: string | null;
  pdf_url: string | null;
  images: PaperImage[];
  /** True while the detailed summary is still being generated in the background. */
  enriching: boolean;
}

/** Fetch a paper. `enrich` (default true) lets the server kick off background
 *  generation of the detailed summary; polls should pass false to just read. */
export async function getPaper(s2PaperId: string, enrich = true): Promise<PaperDetail> {
  const qs = enrich ? "" : "?enrich=0";
  return apiFetch(`/api/papers/${encodeURIComponent(s2PaperId)}${qs}`);
}

export type FieldSort = "random" | "citation" | "year";

export interface FieldFeedOpts {
  offset?: number;
  pageSize?: number;
  sort?: FieldSort;
  seed?: number;
  /** Hide papers the signed-in user has already seen (needs auth). */
  unseen?: boolean;
}

export async function getFieldFeed(slug: string, opts: FieldFeedOpts = {}) {
  const params = new URLSearchParams({ page_size: String(opts.pageSize ?? 20) });
  if (opts.offset != null) params.set("offset", String(opts.offset));
  if (opts.sort) params.set("sort", opts.sort);
  if (opts.seed != null) params.set("seed", String(opts.seed));
  let headers: Record<string, string> = {};
  if (opts.unseen && $user.get()) {
    const a = await authParams();
    headers = a.headers;
    params.set("uid", a.uid);
    params.set("unseen", "1");
  }
  return apiFetch(`/api/fields/${encodeURIComponent(slug)}?${params.toString()}`, { headers });
}

/** Cheaply ingest & return brand-new papers for a field (infinite scroll).
 *  Short summary only — no PDF/figures/detailed summary. Omit `limit` to use the
 *  server's batch size. */
export async function discoverField(slug: string, limit?: number): Promise<{
  items: FeedItem[];
  exhausted: boolean;
}> {
  const qs = limit != null ? `?limit=${limit}` : "";
  return apiFetch(`/api/fields/${encodeURIComponent(slug)}/discover${qs}`);
}

/** Record papers the user scrolled past so the feed prioritizes new content.
 *  No-op when signed out. */
export async function markSeen(paperIds: number[]) {
  if (!$user.get() || !paperIds.length) return;
  const { headers, uid, token } = await authParams();
  return apiFetch("/api/seen", {
    method: "POST",
    headers: { ...headers, "Content-Type": "application/json" },
    body: JSON.stringify({ idToken: token, uid, paper_ids: paperIds }),
  });
}

// ── Auth ────────────────────────────────────────────────────────────────────

export async function verifyUser(): Promise<{ valid: boolean; uid: string }> {
  const user = $user.get();
  if (!user) throw new Error("Not authenticated");
  const token = await getIdToken();
  return apiFetch("/api/verifyUserId", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ idToken: token, uid: user.uid }),
  });
}

// ── Fields ──────────────────────────────────────────────────────────────────

export async function getFields(q?: string): Promise<{ fields: Field[] }> {
  const qs = q ? `?q=${encodeURIComponent(q)}` : "";
  return apiFetch(`/api/fields${qs}`);
}

// ── Subscriptions ─────────────────────────────────────────────────────────────

export async function getSubscriptions(): Promise<{ subscriptions: Field[] }> {
  const { headers, uid } = await authParams();
  return apiFetch(`/api/subscriptions?uid=${uid}`, { headers });
}

export async function subscribe(slug: string) {
  const { headers, uid, token } = await authParams();
  return apiFetch("/api/subscriptions", {
    method: "POST",
    headers: { ...headers, "Content-Type": "application/json" },
    body: JSON.stringify({ idToken: token, uid, slug }),
  });
}

export async function unsubscribe(slug: string) {
  const { headers, uid } = await authParams();
  return apiFetch(`/api/subscriptions/${slug}?uid=${uid}`, {
    method: "DELETE",
    headers,
  });
}

// ── Feed ──────────────────────────────────────────────────────────────────────

export interface MixedCursor {
  sub_offset?: number;
  other_offset?: number;
}

export async function getMixedFeed(
  cursor: MixedCursor = {}, sort: FieldSort = "random", seed = 0, pageSize = 20,
) {
  const params = new URLSearchParams({
    mode: "mixed", page_size: String(pageSize), sort, seed: String(seed),
  });
  if (cursor.sub_offset != null) params.set("sub_offset", String(cursor.sub_offset));
  if (cursor.other_offset != null) params.set("other_offset", String(cursor.other_offset));

  // Attach auth when signed in so the mix is weighted toward subscriptions.
  let headers: Record<string, string> = {};
  if ($user.get()) {
    const a = await authParams();
    headers = a.headers;
    params.set("uid", a.uid);
  }
  return apiFetch(`/api/feed?${params.toString()}`, { headers });
}

export async function getSubscribedFeed(
  offset = 0, sort: FieldSort = "random", seed = 0, pageSize = 20,
) {
  const { headers, uid } = await authParams();
  const params = new URLSearchParams({
    mode: "subscribed",
    page_size: String(pageSize),
    offset: String(offset),
    sort,
    seed: String(seed),
    uid,
  });
  return apiFetch(`/api/feed?${params.toString()}`, { headers });
}

// ── Admin ─────────────────────────────────────────────────────────────────────

export interface AdminField {
  slug: string;
  name: string;
  query: string | null;
  is_seed: number;
}

export type JobKind = "ingest" | "secondary_fields" | "keywords";

export interface IngestJob {
  job_id: string;
  kind?: JobKind;
  status: "running" | "done" | "error";
  field: string | null;
  query: string | null;
  limit: number;
  min_citations: number;
  force?: boolean;
  counts: Record<string, number> | null;
  progress: {
    fetched?: number;
    processed: number;
    ingested?: number;
    skipped?: number;
    errors?: number;
    current?: string | null;
  } | null;
  error: string | null;
  started_at: string;
  finished_at: string | null;
  by: string;
}

/** Whether the signed-in user is an allow-listed admin, and whether this
 *  server has LLM features enabled (gates ingestion/recompute/enrichment). */
export async function adminCheck(): Promise<{ admin: boolean; llmEnabled: boolean }> {
  if (!$user.get()) return { admin: false, llmEnabled: true };
  const { headers, uid } = await authParams();
  const res = await apiFetch(`/api/admin/check?uid=${uid}`, { headers });
  return { admin: res?.admin === true, llmEnabled: res?.llm_enabled !== false };
}

export async function adminFields(): Promise<{ fields: AdminField[] }> {
  const { headers, uid } = await authParams();
  return apiFetch(`/api/admin/fields?uid=${uid}`, { headers });
}

export async function startIngest(opts: {
  field?: string;
  query?: string;
  limit?: number;
  min_citations?: number;
}): Promise<{ job_id: string; status: string }> {
  const { headers, uid, token } = await authParams();
  return apiFetch("/api/admin/ingest", {
    method: "POST",
    headers: { ...headers, "Content-Type": "application/json" },
    body: JSON.stringify({ idToken: token, uid, ...opts }),
  });
}

/** Recompute secondary fields. ``force`` recomputes every paper's secondary
 *  fields from scratch instead of only ones that lack them. */
export async function startSecondaryFieldsJob(force = false): Promise<{ job_id: string; status: string }> {
  const { headers, uid, token } = await authParams();
  return apiFetch("/api/admin/utility/secondary-fields", {
    method: "POST",
    headers: { ...headers, "Content-Type": "application/json" },
    body: JSON.stringify({ idToken: token, uid, force }),
  });
}

/** Recompute field search keywords. ``force`` recomputes every field's
 *  keywords from scratch instead of only ones that lack them. */
export async function startKeywordsJob(force = false): Promise<{ job_id: string; status: string }> {
  const { headers, uid, token } = await authParams();
  return apiFetch("/api/admin/utility/keywords", {
    method: "POST",
    headers: { ...headers, "Content-Type": "application/json" },
    body: JSON.stringify({ idToken: token, uid, force }),
  });
}

/** Clear every user's seen-paper history, so the feed treats all papers as
 *  unseen again. Fast: resolves immediately with the number of rows removed. */
export async function resetSeenPapers(): Promise<{ deleted: number }> {
  const { headers, uid, token } = await authParams();
  return apiFetch("/api/admin/utility/reset-seen", {
    method: "POST",
    headers: { ...headers, "Content-Type": "application/json" },
    body: JSON.stringify({ idToken: token, uid }),
  });
}

export async function getJob(jobId: string): Promise<IngestJob> {
  const { headers, uid } = await authParams();
  return apiFetch(`/api/admin/ingest/${jobId}?uid=${uid}`, { headers });
}

export async function getJobs(): Promise<{ jobs: IngestJob[] }> {
  const { headers, uid } = await authParams();
  return apiFetch(`/api/admin/jobs?uid=${uid}`, { headers });
}

export async function getIngestLog(): Promise<{ log: any[] }> {
  const { headers, uid } = await authParams();
  return apiFetch(`/api/admin/log?uid=${uid}`, { headers });
}
