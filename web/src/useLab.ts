import { useCallback, useEffect, useRef, useState } from 'react';
import { errorMessage, request } from './api';
import type { Catalog, Parameters, Scene, SceneObject, Snapshot } from './types';

export function useLab() {
  const [catalog, setCatalog] = useState<Catalog | null>(null);
  const [scene, setScene] = useState<Scene | null>(null);
  const [roomId, setRoomId] = useState(() => {
    try { const saved = localStorage.getItem('office.scene.room'); return saved && ['k1315', 'k1316', 'k1324'].includes(saved) ? saved : 'k1315'; }
    catch { return 'k1315'; }
  });
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null);
  const [editing, setEditing] = useState(true);
  const [dirty, setDirty] = useState(false);
  const [pending, setPending] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [connection, setConnection] = useState<'connected' | 'connecting' | 'offline'>('connecting');
  const [reload, setReload] = useState(0);
  const activeRun = useRef<string | null>(null);
  const inFlight = useRef(false);

  useEffect(() => { try { localStorage.setItem('office.scene.room', roomId); } catch { /* Storage may be disabled. */ } }, [roomId]);

  const adoptSnapshot = useCallback((next: Snapshot | null) => {
    // Old responses can arrive before React cleans up the previous socket.
    activeRun.current = next?.run_id ?? null;
    setSnapshot(next);
  }, []);

  const acceptSnapshot = useCallback((next: Snapshot) => {
    if (activeRun.current !== next.run_id) return;
    setSnapshot(previous => previous?.run_id === next.run_id && previous.revision > next.revision ? previous : next);
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    request<Catalog>('/catalog', undefined, undefined, controller.signal)
      .then(result => { if (!controller.signal.aborted) { setCatalog(result); setError(null); } })
      .catch(reason => { if (!controller.signal.aborted) setError(errorMessage(reason)); })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [reload]);

  useEffect(() => {
    if (!catalog) return;
    const controller = new AbortController();
    setLoading(true); setScene(null); adoptSnapshot(null); setEditing(true);
    Promise.all([
      request<Scene>(`/scenes/${encodeURIComponent(roomId)}`, undefined, undefined, controller.signal),
      request<Snapshot | null>('/runs/current', undefined, undefined, controller.signal),
    ])
      .then(([result, current]) => {
        if (controller.signal.aborted) return;
        setScene(result); setDirty(false); setError(null);
        if (current?.room_id === roomId) { adoptSnapshot(current); setEditing(false); }
      })
      .catch(reason => { if (!controller.signal.aborted) setError(errorMessage(reason)); })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [roomId, catalog, reload, adoptSnapshot]);

  useEffect(() => {
    const warn = (event: BeforeUnloadEvent) => { if (dirty) { event.preventDefault(); event.returnValue = ''; } };
    window.addEventListener('beforeunload', warn);
    return () => window.removeEventListener('beforeunload', warn);
  }, [dirty]);

  useEffect(() => {
    if (!notice) return;
    const timer = setTimeout(() => setNotice(null), 4000);
    return () => clearTimeout(timer);
  }, [notice]);

  const runId = snapshot?.run_id;
  useEffect(() => {
    if (!runId || editing) { setConnection('connecting'); return; }
    let closed = false;
    let socket: WebSocket | null = null;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let retries = 0;
    const connect = () => {
      if (closed) return;
      setConnection('connecting');
      socket = new WebSocket(`${location.protocol === 'https:' ? 'wss:' : 'ws:'}//${location.host}/api/runs/${encodeURIComponent(runId)}/events`);
      socket.onopen = () => { if (!closed) { setConnection('connected'); retries = 0; } };
      socket.onmessage = event => {
        if (closed || activeRun.current !== runId) return;
        try { const message = JSON.parse(event.data); if (message.type === 'snapshot' && message.data.run_id === runId) acceptSnapshot(message.data); }
        catch { setError('收到无法解析的状态消息，请重新连接实验。'); }
      };
      socket.onclose = () => {
        if (closed) return;
        setConnection('offline');
        const retry = () => { if (!closed) timer = setTimeout(connect, Math.min(1000 * 2 ** retries++, 10000)); };
        // A rejected WebSocket handshake is reported as 1006 by browsers,
        // including when the service restarted and no longer knows this run.
        request<Snapshot | null>('/runs/current').then(current => {
          if (closed) return;
          if (activeRun.current !== runId) return;
          if (current && current.room_id === roomId && current.run_id !== runId) adoptSnapshot(current);
          else if (!current || current.room_id !== roomId) {
            adoptSnapshot(null); setEditing(true); setError('当前实验已结束或已被替换，请重新创建实验。');
          } else retry();
        }).catch(retry);
      };
      socket.onerror = () => socket?.close();
    };
    connect();
    return () => { closed = true; clearTimeout(timer); socket?.close(); };
  }, [runId, roomId, editing, acceptSnapshot, adoptSnapshot]);

  const perform = async <T,>(key: string, action: () => Promise<T>): Promise<T | undefined> => {
    if (inFlight.current) return undefined;
    inFlight.current = true;
    setPending(key); setError(null);
    try { return await action(); }
    catch (reason) { setError(errorMessage(reason)); return undefined; }
    finally { inFlight.current = false; setPending(null); }
  };

  const saveScene = async (): Promise<Scene> => {
    if (!scene) throw new Error('请先加载场景。');
    const saved = await request<Scene>(`/scenes/${scene.room_id}`, scene, 'PUT');
    setScene(saved); setDirty(false);
    return saved;
  };

  return {
    catalog, scene, roomId, snapshot, editing, dirty, pending, error, notice, loading, connection,
    dismissError: () => setError(null), retry: () => {
      if (inFlight.current) return;
      if (dirty && !window.confirm('重新加载会放弃当前未保存的修改。')) return;
      setReload(value => value + 1);
    },
    changeRoom: (id: string) => {
      if (inFlight.current) return;
      if (snapshot?.agent.status === 'running') { setError('请先停止当前流程，再切换房间。'); return; }
      if (dirty && !window.confirm('当前布局尚未保存，切换房间会放弃这些修改。')) return;
      setRoomId(id); setDirty(false);
    },
    updateObject: (id: string, patch: Partial<SceneObject>) => {
      if (!editing || inFlight.current) return;
      setScene(current => current && ({ ...current, objects: current.objects.map(object => object.id === id ? { ...object, ...patch } : object) }));
      setDirty(true);
    },
    addObject: (object: SceneObject) => { if (editing && !inFlight.current) { setScene(current => current && ({ ...current, objects: [...current.objects, object] })); setDirty(true); } },
    removeObject: (id: string) => { if (editing && !inFlight.current) { setScene(current => current && ({ ...current, objects: current.objects.filter(object => object.id !== id) })); setDirty(true); } },
    updateInitialState: (deviceId: string, parameters: Parameters) => {
      if (!editing || inFlight.current) return;
      setScene(current => current && ({ ...current, initial_states: { ...current.initial_states, [deviceId]: parameters } })); setDirty(true);
    },
    save: () => perform('save', async () => { await saveScene(); setNotice('布局与初始状态已保存'); }),
    enterRun: () => perform('start', async () => {
      await saveScene();
      const state = await request<Snapshot>('/runs', { scene_id: roomId });
      adoptSnapshot(state); setEditing(false); setNotice('实验已创建，设备状态已同步');
    }),
    enterEdit: () => {
      if (snapshot?.agent.status === 'running') { setError('请先停止流程，再编辑布局。'); return; }
      setEditing(true);
    },
    command: (deviceId: string, parameters: Parameters) => perform('command', async () => {
      if (!snapshot) return;
      const commandRun = snapshot.run_id;
      const result = await request<{ status: string; reason?: string }>(`/runs/${commandRun}/commands`, { device_id: deviceId, parameters });
      if (result.status !== 'accepted') throw new Error(`设备未接受命令：${result.reason ?? result.status}`);
      const confirmed = await request<Snapshot>(`/runs/${commandRun}`);
      if (activeRun.current !== commandRun) return;
      acceptSnapshot(confirmed);
      setNotice('设备状态已更新');
    }),
    advance: (seconds: number) => perform('advance', async () => {
      if (snapshot) acceptSnapshot(await request<Snapshot>(`/runs/${snapshot.run_id}/advance`, { seconds }));
    }),
    startAgent: (mode: 'demo' | 'llm', task?: string) => perform('agent', async () => {
      if (snapshot) acceptSnapshot(await request<Snapshot>(`/runs/${snapshot.run_id}/agent`, { mode, task: task || undefined }));
    }),
    stop: () => perform('stop', async () => { if (snapshot) acceptSnapshot(await request<Snapshot>(`/runs/${snapshot.run_id}/stop`, {})); }),
    reset: () => perform('reset', async () => {
      if (snapshot) { const next = await request<Snapshot>(`/runs/${snapshot.run_id}/reset`, {}); adoptSnapshot(next); setNotice('已从保存的初始配置重新创建实验'); }
    }),
  };
}
