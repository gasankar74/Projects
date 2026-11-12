"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { Shell, useApp } from "@/components/Shell";
import { api, bus, useApi } from "@/lib/api";
import type { AiAction, AiChange, AiConversation, AiMessage, AiStatus } from "@/lib/types";

const SUGGESTIONS = [
  "What's on my plate today?",
  "What's at risk of missing its deadline this week?",
  "I'm out tomorrow afternoon. What would move?",
  "Push my low-priority tasks to next week",
];

function fmtWhen(iso: string | null | undefined): string {
  if (!iso) return "not scheduled";
  const d = new Date(iso);
  return d.toLocaleString([], { weekday: "short", month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
}

const VERB: Record<string, string> = { create: "Created", update: "Updated", complete: "Completed", delete: "Deleted" };

function Changes({ changes }: { changes: AiChange[] }) {
  const { openTask } = useApp();
  const real = changes.filter((c) => c.task_id);
  if (!real.length) return null;
  return (
    <div className="mt-2 rounded-lg border border-emerald-200 bg-emerald-50/70 p-2 text-xs dark:border-emerald-500/30 dark:bg-emerald-950/30">
      <div className="mb-1 font-semibold text-emerald-800 dark:text-emerald-200">Saved</div>
      <ul className="space-y-0.5">
        {real.map((c, i) => (
          <li key={i} className="flex flex-wrap gap-x-1.5">
            <span className="text-stone-500">{VERB[c.op] ?? c.op}</span>
            {c.deleted ? (
              <span className="line-through">{c.title}</span>
            ) : (
              <button className="text-left font-medium underline decoration-stone-300 underline-offset-2 hover:decoration-current" onClick={() => c.task_id && openTask(c.task_id)}>
                {c.title}
              </button>
            )}
            {c.op !== "delete" && c.op !== "complete" && <span className="text-stone-500">· planned {fmtWhen(c.start)}</span>}
          </li>
        ))}
      </ul>
    </div>
  );
}

function ActionCard({ action, onDecided }: { action: AiAction; onDecided: () => void }) {
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const decide = async (ok: boolean) => {
    setBusy(true);
    setErr(null);
    try {
      await api(`/assistant/actions/${action.id}/${ok ? "confirm" : "cancel"}`, { method: "POST" });
      bus.emit("tasks");
      bus.emit("schedule");
      onDecided();
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  };
  const moves = action.preview.moves ?? [];
  const tone =
    action.status === "pending"
      ? "border-amber-300 bg-amber-50 dark:border-amber-500/40 dark:bg-amber-950/30"
      : "border-stone-200 bg-stone-50 dark:border-stone-700 dark:bg-stone-800/40";
  return (
    <div className={`mt-2 rounded-lg border p-3 text-sm ${tone}`}>
      <div className="flex items-center gap-2">
        <span className="font-semibold">{action.summary}</span>
        <span className="ml-auto text-xs text-stone-500">
          {action.status === "pending" ? "Waiting for you" : action.status === "confirmed" ? "Confirmed" : action.status === "cancelled" ? "Cancelled" : "Couldn't apply"}
        </span>
      </div>
      {action.preview.items.length > 1 && (
        <ul className="mt-1 list-disc pl-5 text-xs text-stone-600 dark:text-stone-300">
          {action.preview.items.slice(0, 12).map((it, i) => (
            <li key={i}>{it.title || "New task"}</li>
          ))}
          {action.preview.items.length > 12 && <li>and {action.preview.items.length - 12} more</li>}
        </ul>
      )}
      {moves.length > 0 && (
        <div className="mt-2 text-xs">
          <div className="mb-0.5 font-medium text-stone-600 dark:text-stone-300">What would move</div>
          <ul className="space-y-0.5">
            {moves.slice(0, 10).map((m) => (
              <li key={m.task_id}>
                <span className="font-medium">{m.title}</span>: {fmtWhen(m.from)} → <span className={m.day_changed ? "font-semibold" : ""}>{fmtWhen(m.to)}</span>
              </li>
            ))}
            {moves.length > 10 && <li className="text-stone-500">and {moves.length - 10} more</li>}
          </ul>
        </div>
      )}
      {action.preview.new_warnings.length > 0 && (
        <ul className="mt-2 space-y-0.5 text-xs text-orange-800 dark:text-orange-200">
          {action.preview.new_warnings.slice(0, 5).map((w, i) => (
            <li key={i}>⚠ {w}</li>
          ))}
        </ul>
      )}
      {action.result?.error && <div className="mt-2 text-xs text-red-600">{action.result.error}</div>}
      {err && <div className="mt-2 text-xs text-red-600">{err}</div>}
      {action.status === "pending" && (
        <div className="mt-3 flex gap-2">
          <button className="btn-primary" disabled={busy} onClick={() => decide(true)}>
            Confirm
          </button>
          <button className="btn" disabled={busy} onClick={() => decide(false)}>
            Cancel
          </button>
        </div>
      )}
    </div>
  );
}

function Thread({ cid, onTitle }: { cid: string; onTitle: () => void }) {
  const [messages, setMessages] = useState<AiMessage[]>([]);
  const [actions, setActions] = useState<AiAction[]>([]);
  const [text, setText] = useState("");
  const [sending, setSending] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const bottom = useRef<HTMLDivElement>(null);

  const load = useCallback(async () => {
    const d = await api<{ messages: AiMessage[]; actions: AiAction[] }>(`/assistant/conversations/${cid}`);
    setMessages(d.messages);
    setActions(d.actions);
  }, [cid]);
  useEffect(() => {
    load();
  }, [load]);
  useEffect(() => {
    bottom.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [messages.length, actions.length, sending]);

  const send = async (t: string) => {
    const body = t.trim();
    if (!body || sending) return;
    setSending(true);
    setErr(null);
    setText("");
    setMessages((m) => [...m, { id: "pending", seq: 1e9, role: "user", content: body, changes: [], action_id: null, created_at: new Date().toISOString() }]);
    try {
      await api(`/assistant/conversations/${cid}/messages`, { method: "POST", json: { text: body } });
      await load();
      bus.emit("tasks");
      bus.emit("schedule");
      onTitle();
    } catch (e) {
      setErr((e as Error).message);
      setText(body);
      await load();
    } finally {
      setSending(false);
    }
  };

  const byId = new Map(actions.map((a) => [a.id, a]));
  const shownActions = new Set<string>();

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="min-h-0 flex-1 overflow-y-auto px-3 py-4 sm:px-6">
        <div className="mx-auto max-w-2xl space-y-3">
          {messages.length === 0 && !sending && (
            <div className="py-6 text-center text-sm text-stone-500">
              <p className="mb-3">Ask about your plan, or tell me what to change. Big changes wait for your OK.</p>
              <div className="flex flex-wrap justify-center gap-2">
                {SUGGESTIONS.map((s) => (
                  <button key={s} className="btn text-xs" onClick={() => send(s)}>
                    {s}
                  </button>
                ))}
              </div>
            </div>
          )}
          {messages.map((m) => {
            if (m.role === "user")
              return (
                <div key={m.id} className="flex justify-end">
                  <div className="max-w-[85%] rounded-2xl rounded-br-sm bg-brand-600 px-3 py-2 text-sm whitespace-pre-wrap text-white">{m.content}</div>
                </div>
              );
            if (m.role === "tool") {
              const act = m.action_id ? byId.get(m.action_id) : undefined;
              if (act) shownActions.add(act.id);
              return (
                <div key={m.id}>
                  <div className="text-[11px] text-stone-400">· {m.content}</div>
                  {act && <ActionCard action={act} onDecided={load} />}
                </div>
              );
            }
            if (m.role === "note")
              return (
                m.changes.length ? (
                  <div key={m.id} className="max-w-[90%]">
                    <Changes changes={m.changes} />
                  </div>
                ) : (
                  <div key={m.id} className="text-center text-xs text-stone-500">
                    {m.content.replace(/^\(App note[^)]*\)\s*/, "")}
                  </div>
                )
              );
            if (!m.content && m.tools?.length) return null;
            return (
              <div key={m.id} className="flex">
                <div className="max-w-[90%]">
                  <div className="rounded-2xl rounded-bl-sm bg-white px-3 py-2 text-sm whitespace-pre-wrap shadow-sm ring-1 ring-stone-200 dark:bg-stone-900 dark:ring-stone-800">
                    {m.content || <span className="text-stone-400">(no reply)</span>}
                  </div>
                  <Changes changes={m.changes} />
                </div>
              </div>
            );
          })}
          {actions
            .filter((a) => a.status === "pending" && !shownActions.has(a.id))
            .map((a) => (
              <ActionCard key={a.id} action={a} onDecided={load} />
            ))}
          {sending && <div className="animate-pulse text-sm text-stone-400">Thinking…</div>}
          {err && <div className="text-sm text-red-600">{err}</div>}
          <div ref={bottom} />
        </div>
      </div>
      <form
        className="border-t border-stone-200 bg-white p-3 dark:border-stone-800 dark:bg-stone-900"
        onSubmit={(e) => {
          e.preventDefault();
          send(text);
        }}
      >
        <div className="mx-auto flex max-w-2xl gap-2">
          <textarea
            className="input min-h-[42px] flex-1 resize-none"
            rows={1}
            placeholder="Ask or tell the assistant… (Enter to send)"
            value={text}
            autoFocus
            onChange={(e) => setText(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                send(text);
              }
            }}
          />
          <button className="btn-primary" disabled={sending || !text.trim()}>
            Send
          </button>
        </div>
      </form>
    </div>
  );
}

function Assistant() {
  const { data: status } = useApi<AiStatus>("/assistant/status");
  const { data: convs, reload } = useApi<AiConversation[]>("/assistant/conversations");
  const [cid, setCid] = useState<string | null>(null);

  useEffect(() => {
    if (convs && !cid && convs.length) setCid(convs[0].id);
  }, [convs, cid]);

  const newConv = async () => {
    const c = await api<AiConversation>("/assistant/conversations", { method: "POST" });
    await reload();
    setCid(c.id);
  };
  useEffect(() => {
    if (convs && convs.length === 0 && !cid) newConv();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [convs]);

  return (
    <div className="flex h-full min-h-0">
      <aside className="hidden w-60 shrink-0 flex-col border-r border-stone-200 bg-white md:flex dark:border-stone-800 dark:bg-stone-900">
        <div className="p-3">
          <button className="btn w-full justify-center" onClick={newConv}>
            + New conversation
          </button>
        </div>
        <div className="min-h-0 flex-1 overflow-y-auto px-2 pb-3">
          {(convs ?? []).map((c) => (
            <div key={c.id} className={`group flex items-center rounded-md ${c.id === cid ? "bg-brand-50 dark:bg-brand-700/20" : "hover:bg-stone-100 dark:hover:bg-stone-800"}`}>
              <button className="min-w-0 flex-1 truncate px-2 py-1.5 text-left text-sm" onClick={() => setCid(c.id)} title={c.title}>
                {c.title}
              </button>
              <button
                className="px-2 text-xs text-stone-400 opacity-0 group-hover:opacity-100 hover:text-red-600"
                title="Delete conversation"
                onClick={async () => {
                  if (!confirm("Delete this conversation? Changes it made stay.")) return;
                  await api(`/assistant/conversations/${c.id}`, { method: "DELETE" });
                  if (c.id === cid) setCid(null);
                  reload();
                }}
              >
                ✕
              </button>
            </div>
          ))}
        </div>
      </aside>
      <section className="flex min-w-0 flex-1 flex-col">
        <div className="flex items-center gap-2 border-b border-stone-200 bg-white px-3 py-2 text-xs dark:border-stone-800 dark:bg-stone-900">
          <select className="input max-w-[60%] md:hidden" value={cid ?? ""} onChange={(e) => setCid(e.target.value)}>
            {(convs ?? []).map((c) => (
              <option key={c.id} value={c.id}>
                {c.title}
              </option>
            ))}
          </select>
          <button className="btn text-xs md:hidden" onClick={newConv}>
            New
          </button>
          {status && (
            <span className="ml-auto flex items-center gap-1.5 text-stone-500" title={status.error ?? undefined}>
              <span className={`h-2 w-2 rounded-full ${status.ok ? "bg-emerald-500" : "bg-red-500"}`} />
              {status.provider === "none" ? "Assistant off" : `${status.model}${status.local ? " · runs on your server" : " · external provider"}`}
            </span>
          )}
        </div>
        {status && !status.ok && (
          <div className="border-b border-amber-200 bg-amber-50 px-4 py-2 text-xs text-amber-900 dark:border-amber-500/30 dark:bg-amber-950/30 dark:text-amber-100">
            {status.error}. The rest of the planner works without it.
          </div>
        )}
        <div className="min-h-0 flex-1">{cid ? <Thread key={cid} cid={cid} onTitle={reload} /> : null}</div>
      </section>
    </div>
  );
}

export default function AssistantPage() {
  return (
    <Shell>
      <Assistant />
    </Shell>
  );
}
