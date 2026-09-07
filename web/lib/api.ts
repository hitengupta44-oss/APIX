/**
 * Data access.
 *
 * Two paths on purpose:
 *   - Supabase, read with the ANON key, for published index tables. RLS in
 *     sql/schema.sql restricts anon to exactly those tables, so this is safe
 *     in the browser.
 *   - The FastAPI Space, for derived views (lead-time curve, CPI overlay) and
 *     chat. Chat is proxied through a route handler so the Space URL and any
 *     token stay server-side.
 *
 * The service role key must never appear in this file or any NEXT_PUBLIC_ var.
 */
import { createClient } from "@supabase/supabase-js";

const url = process.env.NEXT_PUBLIC_SUPABASE_URL;
const anon = process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY;

export const supabase = url && anon ? createClient(url, anon) : null;

export type DailyPoint = {
  quote_date: string;
  apix: number;
  dod_pct: number | null;
  mom_pct: number | null;
  yoy_pct: number | null;
  pct_imputed: number | null;
};

export type RoutePoint = {
  route: string;
  index_value: number;
  pct_imputed: number | null;
};

export async function getDaily(limit = 120): Promise<DailyPoint[]> {
  if (!supabase) return [];
  const { data, error } = await supabase
    .from("apix_daily")
    .select("quote_date, apix, dod_pct, mom_pct, yoy_pct, pct_imputed")
    .order("quote_date", { ascending: false })
    .limit(limit);
  if (error) {
    console.error("apix_daily:", error.message);
    return [];
  }
  return (data ?? []).reverse() as DailyPoint[];
}

export async function getRoutes(): Promise<RoutePoint[]> {
  if (!supabase) return [];
  const { data: latest } = await supabase
    .from("apix_route_daily")
    .select("quote_date")
    .order("quote_date", { ascending: false })
    .limit(1);
  const day = latest?.[0]?.quote_date;
  if (!day) return [];

  const { data, error } = await supabase
    .from("apix_route_daily")
    .select("route, index_value, pct_imputed")
    .eq("quote_date", day)
    .order("index_value", { ascending: false });
  if (error) {
    console.error("apix_route_daily:", error.message);
    return [];
  }
  return (data ?? []) as RoutePoint[];
}

export async function getLeadTime(): Promise<{ apw: number; price: number }[]> {
  const { data: cells, error } = (await supabase
    ?.from("elementary_cells")
    .select("apw, price")
    .eq("cabin", "ECONOMY")) ?? { data: null, error: null };
  if (error || !cells) return [];

  // Median per window. Done client-side because the row count is small and it
  // avoids adding a database view for one chart.
  const byWindow = new Map<number, number[]>();
  for (const c of cells as { apw: number; price: number }[]) {
    if (!byWindow.has(c.apw)) byWindow.set(c.apw, []);
    byWindow.get(c.apw)!.push(c.price);
  }
  return [...byWindow.entries()]
    .map(([apw, prices]) => {
      const s = prices.sort((a, b) => a - b);
      const mid = Math.floor(s.length / 2);
      return {
        apw,
        price: s.length % 2 ? s[mid] : (s[mid - 1] + s[mid]) / 2,
      };
    })
    .sort((a, b) => a.apw - b.apw);
}

export function fmt(n: number | null | undefined, dp = 2): string {
  if (n === null || n === undefined || Number.isNaN(n)) return "—";
  return n.toLocaleString("en-IN", {
    minimumFractionDigits: dp,
    maximumFractionDigits: dp,
  });
}

export function signed(n: number | null | undefined, dp = 2): string {
  if (n === null || n === undefined || Number.isNaN(n)) return "—";
  return `${n >= 0 ? "+" : ""}${fmt(n, dp)}%`;
}

export function direction(n: number | null | undefined): string {
  if (n === null || n === undefined || Number.isNaN(n) || Math.abs(n) < 0.005)
    return "flat";
  return n > 0 ? "up" : "down";
}
