"use client";

import { useState } from "react";

type Turn = { role: "user" | "assistant"; content: string };

export default function Chat() {
  const [turns, setTurns] = useState<Turn[]>([]);
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function send() {
    const text = draft.trim();
    if (!text || busy) return;

    const next: Turn[] = [...turns, { role: "user", content: text }];
    setTurns(next);
    setDraft("");
    setBusy(true);
    setError(null);

    try {
      const r = await fetch("/api/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ messages: next }),
      });
      const body = await r.json();
      if (!r.ok) throw new Error(body?.error ?? `Request failed (${r.status})`);
      setTurns([...next, { role: "assistant", content: body.reply }]);
    } catch (e) {
      // State what went wrong and what to do, in the interface's voice.
      setError(
        e instanceof Error
          ? e.message
          : "The assistant did not respond. Try again in a moment.",
      );
    } finally {
      setBusy(false);
    }
  }

  return (
    <div>
      {turns.length > 0 && (
        <div className="chat-log">
          {turns.map((t, i) => (
            <div key={i} className={`chat-turn ${t.role}`}>
              <div className="who">{t.role === "user" ? "You" : "APIx"}</div>
              <div className="body">{t.content}</div>
            </div>
          ))}
        </div>
      )}

      {turns.length === 0 && (
        <p className="empty">
          Ask about index movements, routes, or how the numbers are built. The
          assistant answers only from the published index tables, so it will say
          when something is outside what has been collected.
        </p>
      )}

      <div className="chat-form">
        <input
          type="text"
          value={draft}
          placeholder="Which routes moved most this week?"
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && send()}
          disabled={busy}
          aria-label="Ask about the index"
        />
        <button onClick={send} disabled={busy || !draft.trim()}>
          {busy ? "Asking" : "Ask"}
        </button>
      </div>

      {error && (
        <p className="note" style={{ marginTop: "0.75rem" }}>
          {error}
        </p>
      )}
    </div>
  );
}
