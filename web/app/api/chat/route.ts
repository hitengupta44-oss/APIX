/**
 * Chat proxy.
 *
 * Exists so the browser never sees APIX_API_URL or any credential. The client
 * posts here; this runs server-side on Vercel and forwards to the Space.
 * Putting the Space URL in a NEXT_PUBLIC_ var would work and would also hand
 * every visitor a direct line to the backend.
 */
import { NextResponse } from "next/server";

export const runtime = "nodejs";

export async function POST(req: Request) {
  const api = process.env.APIX_API_URL;
  if (!api) {
    return NextResponse.json(
      { error: "Chat is not configured. Set APIX_API_URL in Vercel." },
      { status: 503 },
    );
  }

  let payload: unknown;
  try {
    payload = await req.json();
  } catch {
    return NextResponse.json({ error: "Malformed request." }, { status: 400 });
  }

  const messages = (payload as { messages?: unknown })?.messages;
  if (!Array.isArray(messages) || messages.length === 0) {
    return NextResponse.json({ error: "No question was sent." }, { status: 400 });
  }

  try {
    // A cold cpu-basic Space takes ~30s to wake, so allow for it rather than
    // showing a timeout error on the first question of the day.
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 60_000);

    const r = await fetch(`${api}/v1/chat`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ messages: messages.slice(-8) }),
      signal: controller.signal,
    });
    clearTimeout(timer);

    const body = await r.json().catch(() => null);
    if (!r.ok) {
      return NextResponse.json(
        { error: body?.detail ?? "The index service is unavailable." },
        { status: r.status },
      );
    }
    return NextResponse.json({ reply: body.reply });
  } catch (e) {
    const aborted = e instanceof Error && e.name === "AbortError";
    return NextResponse.json(
      {
        error: aborted
          ? "The index service is waking up. Ask again in a moment."
          : "Could not reach the index service.",
      },
      { status: 504 },
    );
  }
}
