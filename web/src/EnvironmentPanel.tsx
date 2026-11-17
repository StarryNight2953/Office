import { useEffect, useRef, useState } from 'react';
import { Activity, ArrowRight, Bot, Droplets, Leaf, Play, Square, Thermometer, Wind, Zap } from 'lucide-react';
import type { Snapshot } from './types';

const quantities = [
  { key: 'air_temperature_c', label: '室内温度', unit: '°C', icon: Thermometer, color: '#c18b5a', digits: 1 },
  { key: 'relative_humidity_pct', label: '相对湿度', unit: '%', icon: Droplets, color: '#6b98a1', digits: 1 },
  { key: 'co2_ppm', label: 'CO₂ 浓度', unit: 'ppm', icon: Wind, color: '#79977d', digits: 0 },
  { key: 'pm25_ug_m3', label: 'PM2.5', unit: 'μg/m³', icon: Leaf, color: '#9e97b0', digits: 1 },
];

function Sparkline({ points, color }: { points: number[]; color: string }) {
  if (points.length < 2) return <svg viewBox="0 0 120 25" aria-hidden="true"><path d="M0 20 H120" stroke="#e2e6e2" strokeDasharray="3 4" fill="none" /></svg>;
  const min = Math.min(...points), span = Math.max(...points) - min || 1;
  const path = points.map((value, index) => `${index === 0 ? 'M' : 'L'}${index * 120 / (points.length - 1)} ${22 - (value - min) * 18 / span}`).join(' ');
  return <svg viewBox="0 0 120 25" aria-label="本次实验观测趋势"><path d={`${path} L120 25 L0 25 Z`} fill={color} opacity="0.07" /><path d={path} stroke={color} strokeWidth="1.7" fill="none" /></svg>;
}

interface Props {
  snapshot: Snapshot | null;
  editing: boolean;
  busy: boolean;
  agentConfigured: boolean;
  agentReason: string | null;
  onAgent: (mode: 'demo' | 'llm', task?: string) => void;
  onStop: () => void;
}

export default function EnvironmentPanel({ snapshot, editing, busy, agentConfigured, agentReason, onAgent, onStop }: Props) {
  const [tab, setTab] = useState<'environment' | 'events' | 'agent'>('environment');
  const [history, setHistory] = useState<Record<string, number[]>>({});
  const [task, setTask] = useState('将灯光设为 70%、4000K，空调制冷 24°C、高风档；每步最多推进 5 分钟，累计至少 10 分钟。读取传感器并报告实际温度、能耗；若未达到 24°C，请如实说明。');
  const seen = useRef<Record<string, string>>({});
  useEffect(() => { seen.current = {}; setHistory({}); }, [snapshot?.run_id]);
  useEffect(() => {
    if (!snapshot) return;
    const next: Record<string, number> = {};
    for (const reading of snapshot.sensors) {
      if (reading.value === null || !Number.isFinite(reading.value)) continue;
      const key = `${reading.observed_at}:${reading.value}`;
      if (seen.current[reading.quantity] === key) continue;
      seen.current[reading.quantity] = key; next[reading.quantity] = reading.value;
    }
    if (Object.keys(next).length) setHistory(previous => {
      const updated = { ...previous };
      Object.entries(next).forEach(([key, value]) => { updated[key] = [...(previous[key] ?? []), value].slice(-36); });
      return updated;
    });
  }, [snapshot]);
  const running = snapshot?.agent.status === 'running';
  const events = snapshot?.events.slice(-35).reverse() ?? [];

  return <section className="environment-panel">
    <div className="environment-header"><div className="panel-tabs"><button className={tab === 'environment' ? 'active' : ''} onClick={() => setTab('environment')}><Activity size={14} />环境观测</button><button className={tab === 'events' ? 'active' : ''} onClick={() => setTab('events')}>事件记录{events.length > 0 && <span className="count-badge">{snapshot?.events.length}</span>}</button><button className={tab === 'agent' ? 'active' : ''} onClick={() => setTab('agent')}><Bot size={14} />Agent</button></div><span className="energy-label"><Zap size={12} />{!editing && snapshot ? snapshot.metrics.energy_kwh.toFixed(3) : '—'} <span>kWh</span></span></div>
    {tab === 'environment' && <div className="metric-grid">{quantities.map(({ key, label, unit, icon: Icon, color, digits }) => {
      const reading = !editing ? snapshot?.sensors.find(sensor => sensor.quantity === key) : undefined;
      return <div className="metric" data-quantity={key} key={key}><div className="metric-label"><Icon size={14} style={{ color }} />{label}</div><div className="metric-value">{reading?.value != null ? reading.value.toFixed(digits) : '—'}<span>{unit}</span></div><Sparkline points={editing ? [] : history[key] ?? []} color={color} /><div className="metric-source">{reading ? <><span className={`status-dot ${['good', 'valid', 'ok'].includes(reading.quality) ? 'green' : 'amber'}`} />{reading.quality} · {new Date(reading.observed_at).toLocaleTimeString('zh-CN', { hour12: false })}</> : '运行实验后显示观测'}</div></div>;
    })}</div>}
    {tab === 'events' && <div className="event-list">{events.length === 0 || editing ? <div className="panel-empty"><Activity size={21} /><span>运行实验后，设备操作和环境事件会记录在这里。</span></div> : events.map(event => <div className="event-row" key={event.id}><time>{new Date(event.time).toLocaleTimeString('zh-CN', { hour12: false })}</time><span className="event-dot" /><div><strong>{event.message}</strong><span>{event.source} · {event.kind}</span></div></div>)}</div>}
    {tab === 'agent' && <div className="agent-panel">
      <div className="agent-task">
        <label htmlFor="agent-task">实验目标</label>
        <textarea id="agent-task" value={task} maxLength={4000} disabled={editing || running || busy} onChange={event => setTask(event.target.value)} rows={2} />
        <small>{editing ? '进入运行模式，观察自动控制过程。' : snapshot?.agent.error ? <span className="error-text" role="alert">{snapshot.agent.error}</span> : running ? '正在执行 · 人工命令在工具调用边界处理' : agentConfigured ? 'Agent 将通过现有 FAIRY 工具操作设备。' : agentReason ?? '可先运行内置演示；连接模型后可使用 Agent。'}</small>
        {!editing && snapshot?.agent.summary && <details className="agent-result" open>
          <summary>Agent 结果摘要</summary>
          <p>{snapshot.agent.summary}</p>
        </details>}
      </div>
      <div className="agent-actions">
        {running ? <button className="button secondary" disabled={busy} onClick={onStop}><Square size={13} />停止流程</button> : <>
          <button className="button primary" disabled={editing || busy || !agentConfigured} onClick={() => onAgent('llm', task)}><Bot size={14} />运行 Agent<ArrowRight size={13} /></button>
          <button className="button text-button" disabled={editing || busy} onClick={() => onAgent('demo')}><Play size={12} />内置演示流程</button>
        </>}
        <span className="agent-footnote">{snapshot?.agent.status === 'completed' ? '本次流程已完成' : '灯光 · 空调 · 环境反馈'}</span>
      </div>
    </div>}
  </section>;
}
