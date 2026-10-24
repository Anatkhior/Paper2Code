"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { eventsUrl, fetchSnapshot, fetchTrace } from "./api";
import { appendEvent } from "./session";
import { EVENT_TYPES, type Plan, type RunEvent, type RunSnapshot } from "./types";

/** Own the run identity, snapshot, cursor and stream together. Late responses cannot cross sessions. */
export function useRunSession() {
  const [snapshot, setSnapshot] = useState<RunSnapshot | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const current = useRef<RunSnapshot | null>(null);
  const generation = useRef(0);
  const source = useRef<EventSource | null>(null);
  const request = useRef<AbortController | null>(null);
  const syncing = useRef(false);
  const mounted = useRef(false);

  const commit = useCallback((value: RunSnapshot | null) => {
    current.current = value;
    setSnapshot(value);
  }, []);

  const disconnect = useCallback(() => {
    source.current?.close();
    source.current = null;
  }, []);

  const refresh = useCallback(async (id: string, ticket = generation.current) => {
    if (!mounted.current) return;
    const value = await fetchSnapshot(id);
    if (!mounted.current || generation.current !== ticket || current.current?.run_id !== id) return;
    // An older GET must not roll back already-restored transcripts or operation state.
    // The final run_end/stream_idle always requests a snapshot after durable completion.
    if (value.event_cursor < current.current.event_cursor) return;
    commit(value);
    setError(null);
  }, [commit]);

  const connect = useCallback((id: string) => {
    disconnect();
    if (!mounted.current || current.current?.run_id !== id) return;
    const ticket = generation.current;
    const stream = new EventSource(eventsUrl(id, current.current.event_cursor));
    source.current = stream;
    const valid = () => mounted.current && generation.current === ticket && source.current === stream;
    for (const type of EVENT_TYPES) stream.addEventListener(type, event => {
      if (!valid() || !current.current) return;
      const parsed = JSON.parse((event as MessageEvent).data) as RunEvent;
      commit(appendEvent(current.current, parsed));
      if (parsed.type === "run_end") {
        void refresh(id, ticket).catch(caught => { if (valid()) setError(String(caught)); });
      }
    });
    stream.addEventListener("stream_idle", () => {
      if (!valid()) return;
      disconnect();
      void refresh(id, ticket).catch(caught => { if (generation.current === ticket) setError(String(caught)); });
    });
    stream.onerror = () => {
      if (!valid() || syncing.current) return;
      syncing.current = true;
      // A server restart may end an operation without a final event. GET never retries a POST.
      void refresh(id, ticket).then(() => {
        if (valid() && !current.current?.active) disconnect();
      }).catch(caught => { if (valid()) setError(`连接中断，正在重连：${String(caught)}`); })
        .finally(() => { syncing.current = false; });
    };
  }, [commit, disconnect, refresh]);

  const reset = useCallback(() => {
    generation.current += 1;
    request.current?.abort();
    disconnect();
    commit(null);
    setLoading(false);
    setError(null);
  }, [commit, disconnect]);

  const load = useCallback(async (id: string) => {
    if (!mounted.current) return;
    reset();
    const ticket = generation.current;
    const controller = new AbortController();
    request.current = controller;
    setLoading(true);
    try {
      const value = await fetchSnapshot(id, controller.signal);
      if (!mounted.current || ticket !== generation.current) return;
      commit(value);
      if (value.active) connect(id);
      return value;
    } catch (caught) {
      if (!controller.signal.aborted && ticket === generation.current) setError(String(caught));
    } finally {
      if (ticket === generation.current) setLoading(false);
    }
  }, [commit, connect, reset]);

  const older = useCallback(async () => {
    const before = current.current;
    if (!before?.events.length) return;
    const ticket = generation.current;
    try {
      const value = await fetchTrace(before.run_id, before.events[0].id);
      if (ticket !== generation.current || !current.current) return;
      const seen = new Set(current.current.events.map(event => event.id));
      commit({ ...current.current, events: [...value.events.filter(event => !seen.has(event.id)), ...current.current.events],
        has_older_events: value.has_more, warnings: [...new Set([...current.current.warnings, ...value.warnings])] });
    } catch (caught) { if (ticket === generation.current) setError(String(caught)); }
  }, [commit]);

  const updatePlan = useCallback((id: string, plan: Plan) => {
    if (current.current?.run_id === id) commit({ ...current.current, plan });
  }, [commit]);

  const isCurrent = useCallback((id: string) => mounted.current && current.current?.run_id === id, []);
  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; generation.current += 1; request.current?.abort(); disconnect(); };
  }, [disconnect]);
  return { snapshot, loading, error, load, reset, refresh, connect, older, updatePlan, isCurrent };
}
