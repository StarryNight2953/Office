import { Component, useEffect, useMemo, useState, type ReactNode } from 'react';
import { Activity, ArrowUpRight, Armchair, Box, Boxes, Check, ChevronDown, CircleHelp, Cuboid, Grid2X2, Layers, Lightbulb, LoaderCircle, Maximize2, Monitor, Move3D, MousePointer2, PencilRuler, Play, Plus, Rotate3D, RotateCcw, Save, Search, Snowflake, Sprout, Square, Tags, Trash2, Unplug, Users, Wifi, WifiOff, X } from 'lucide-react';
import { KIND_NAMES } from './types';
import type { ObjectKind, SceneObject } from './types';
import { useLab } from './useLab';
import SceneViewport from './SceneViewport';
import Inspector from './Inspector';
import EnvironmentPanel from './EnvironmentPanel';

const categoryIcon = (kind: string, size = 16) => {
  if (kind === 'lighting') return <Lightbulb size={size} />;
  if (kind === 'hvac' || kind === 'ventilation') return <Snowflake size={size} />;
  if (kind === 'plant') return <Sprout size={size} />;
  if (kind === 'chair') return <Armchair size={size} />;
  if (kind === 'desk' || kind === 'table') return <Monitor size={size} />;
  return <Box size={size} />;
};

class ViewportBoundary extends Component<{ children: ReactNode }, { error: boolean }> {
  state = { error: false };
  static getDerivedStateFromError() { return { error: true }; }
  render() {
    if (this.state.error) return <div className="viewport-error"><Cuboid size={38} strokeWidth={1.1} /><h2>三维视图暂时无法加载</h2><p>请确认浏览器支持 WebGL，并开启硬件加速。<br />你仍可以通过列表和属性面板编辑场景。</p><button className="button secondary" onClick={() => this.setState({ error: false })}>重新加载视图</button></div>;
    return this.props.children;
  }
}

export default function App() {
  const lab = useLab();
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [transformMode, setTransformMode] = useState<'translate' | 'rotate' | 'scale'>('translate');
  const [cameraMode, setCameraMode] = useState<'perspective' | 'top'>('perspective');
  const [showLabels, setShowLabels] = useState(false);
  const [search, setSearch] = useState('');
  const [sidebarTab, setSidebarTab] = useState<'objects' | 'assets'>('objects');
  const [showHelp, setShowHelp] = useState(false);
  const room = lab.catalog?.rooms.find(candidate => candidate.id === lab.roomId);
  const selected = lab.scene?.objects.find(object => object.id === selectedId);
  const agentRunning = lab.snapshot?.agent.status === 'running';
  const objects = useMemo(() => lab.scene?.objects.filter(object => `${object.name} ${object.device_id ?? ''} ${KIND_NAMES[object.kind]}`.toLowerCase().includes(search.toLowerCase())) ?? [], [lab.scene, search]);
  const boundCount = new Set(lab.scene?.objects.map(object => object.device_id).filter(Boolean)).size;
  const onlineCount = Object.values(lab.snapshot?.devices ?? {}).filter(device => device.health === 'online').length;
  useEffect(() => { setSelectedId(null); setSearch(''); }, [lab.roomId]);

  const add = (kind: ObjectKind, name: string) => {
    if (!lab.scene || !lab.editing) return;
    const index = lab.scene.objects.filter(object => object.kind === kind).length + 1;
    const device = room?.devices.find(candidate => candidate.device_type === kind);
    const object: SceneObject = {
      id: `${kind}-${crypto.randomUUID()}`, name: `${name} ${String(index).padStart(2, '0')}`, kind,
      position: [0, kind === 'lighting' ? lab.scene.dimensions.height - 0.2 : kind === 'hvac' ? lab.scene.dimensions.height - 0.65 : 0, 0],
      rotation: [0, 0, 0], scale: [1, 1, 1], device_id: device?.device_id ?? null, asset_url: null,
    };
    lab.addObject(object); setSelectedId(object.id); setSidebarTab('objects');
  };
  const duplicate = () => {
    if (!selected) return;
    const clone: SceneObject = { ...selected, id: `${selected.kind}-${crypto.randomUUID()}`, name: `${selected.name} 副本`, position: [selected.position[0] + 0.4, selected.position[1], selected.position[2] + 0.4] };
    lab.addObject(clone); setSelectedId(clone.id);
  };
  const remove = () => { if (selected) { lab.removeObject(selected.id); setSelectedId(null); } };
  const iconTools = [
    { id: 'translate' as const, label: '移动', icon: Move3D },
    { id: 'rotate' as const, label: '旋转', icon: Rotate3D },
    { id: 'scale' as const, label: '缩放', icon: Maximize2 },
  ];

  return <div className="app-shell" data-run-id={lab.snapshot?.run_id ?? ''} data-revision={lab.snapshot?.revision ?? ''} data-mode={lab.editing ? 'edit' : 'run'}>
    <header className="app-header">
      <a className="brand" href="/" onClick={event => event.preventDefault()} aria-label="OFFICE 场景实验室"><span className="brand-mark"><Boxes size={21} strokeWidth={1.5} /></span><strong>OFFICE<span>场景实验室</span></strong><span className="version-pill">BETA</span></a>
      <div className="mode-switch segmented"><button className={lab.editing ? 'active' : ''} disabled={Boolean(lab.pending) || agentRunning} onClick={lab.enterEdit}><PencilRuler size={15} />搭建场景</button><button className={!lab.editing ? 'active' : ''} disabled={Boolean(lab.pending) || !lab.scene} onClick={() => { if (lab.editing) void lab.enterRun(); }}><Activity size={15} />运行实验</button></div>
      <div className="header-actions"><button className="icon-button help-button" title="使用说明" aria-label="使用说明" onClick={() => setShowHelp(true)}><CircleHelp size={18} /></button><span className={`save-state ${lab.dirty ? 'dirty' : ''}`}>{lab.dirty ? <span className="status-dot amber" /> : <Check size={13} />}{lab.dirty ? '有未保存修改' : '布局已同步'}</span><button className="button primary" disabled={!lab.scene || Boolean(lab.pending) || !lab.editing} onClick={() => void lab.save()}>{lab.pending === 'save' ? <LoaderCircle size={15} className="spin" /> : <Save size={15} />}保存场景</button></div>
    </header>

    <main className="workspace">
      <aside className="sidebar panel">
        <div className="room-selector"><span className="eyebrow">WORKSPACE / 实验空间</span><div className="room-select"><span className="room-icon"><Cuboid size={22} strokeWidth={1.5} /></span><label><span className="sr-only">选择房间</span><select value={lab.roomId} disabled={Boolean(lab.pending) || agentRunning} onChange={event => lab.changeRoom(event.target.value)}>{lab.catalog?.rooms.map(candidate => <option key={candidate.id} value={candidate.id}>{candidate.id.toUpperCase()} · {candidate.id === 'k1315' ? '会议室' : candidate.id === 'k1316' ? '研讨室' : '办公室'}</option>) ?? <option value="k1315">加载空间…</option>}</select></label><ChevronDown size={14} /></div><div className="room-meta"><span><Grid2X2 size={12} />{room?.area_m2 ?? '—'} m²</span><span><Users size={12} />{room?.capacity ?? '—'} 人</span><span className="schematic-badge">示意布局</span></div></div>
        <div className="sidebar-tabs"><button className={sidebarTab === 'objects' ? 'active' : ''} onClick={() => setSidebarTab('objects')}><Layers size={14} />场景物体<span>{lab.scene?.objects.length ?? 0}</span></button><button className={sidebarTab === 'assets' ? 'active' : ''} onClick={() => setSidebarTab('assets')}><Plus size={14} />添加物体</button></div>
        {sidebarTab === 'objects' ? <>
          <label className="object-search"><Search size={14} /><input aria-label="搜索物体" placeholder="搜索物体或设备…" value={search} onChange={event => setSearch(event.target.value)} /><kbd>⌕</kbd></label>
          <div className="object-list">{objects.map(object => {
            const powered = !lab.editing && object.device_id ? lab.snapshot?.devices[object.device_id]?.power_on : false;
            return <button key={object.id} data-object-id={object.id} data-device-id={object.device_id ?? ''} data-powered={powered ? 'true' : 'false'} className={`object-row ${selectedId === object.id ? 'selected' : ''}`} onClick={() => setSelectedId(object.id)}><span className="object-type-icon">{categoryIcon(object.kind)}</span><span className="object-row-text"><strong>{object.name}</strong><small>{object.device_id ? object.device_id.replace(`${lab.roomId}_`, '') : KIND_NAMES[object.kind]}</small></span>{object.device_id && <span className={`status-dot ${powered ? 'green' : 'muted'}`} />}{selectedId === object.id && <ArrowUpRight size={13} className="selection-arrow" />}</button>;
          })}{objects.length === 0 && <div className="list-empty">{lab.loading ? '正在加载物体…' : search ? '没有找到匹配的物体' : '从物体库开始布置空间'}</div>}</div>
          <div className="sidebar-bottom"><div><span className="status-dot green" /><strong>{boundCount}</strong> 个设备已绑定</div><p>选择一个物体，探索它在空间中的角色。</p><button className="button secondary add-object-button" disabled={!lab.editing || !lab.scene} onClick={() => setSidebarTab('assets')}><Plus size={15} />从物体库添加</button></div>
        </> : <div className="asset-library"><div className="asset-intro">为场景添加家具或设备外观。设备可在属性面板中绑定。</div><div className="asset-grid">{lab.catalog?.assets.map(asset => <button key={asset.id} disabled={!lab.editing || Boolean(lab.pending)} onClick={() => add(asset.kind, asset.name)}><span>{categoryIcon(asset.kind, 25)}</span><strong>{asset.name}</strong><Plus size={12} /></button>)}</div>{!lab.editing && <p className="micro-note">返回搭建模式后可以添加物体。</p>}</div>}
      </aside>

      <div className="center-column">
        <section className="viewport-section">
          <div className="viewport-topbar"><div className="scene-title"><span className="eyebrow">{lab.roomId.toUpperCase()}</span><h1>{lab.roomId === 'k1315' ? '让会议空间，开始响应。' : lab.roomId === 'k1316' ? '为每一次讨论，准备好空间。' : '让办公空间，感知每一次变化。'}</h1></div><span className={`live-badge ${!lab.editing && lab.connection === 'connected' ? 'live' : ''}`}><span className="status-dot" />{lab.editing ? '布局预览' : lab.connection === 'connected' ? '实时同步' : lab.connection === 'connecting' ? '连接中' : '连接中断'}</span></div>
          {lab.scene && !lab.loading ? <ViewportBoundary key={lab.roomId}><SceneViewport scene={lab.scene} selectedId={selectedId} onSelect={setSelectedId} onTransform={lab.updateObject} editing={lab.editing} busy={Boolean(lab.pending)} transformMode={transformMode} cameraMode={cameraMode} states={lab.snapshot?.devices ?? {}} showLabels={showLabels} /></ViewportBoundary> : <div className="viewport-loading">{lab.loading ? <><div className="loading-mark"><Boxes size={34} strokeWidth={1.2} /></div><span>正在构建你的实验空间…</span></> : <><Unplug size={36} strokeWidth={1.2} /><h2>等待连接实验服务</h2><p>启动 Python 服务后，即可加载房间与设备。</p><button className="button secondary" onClick={lab.retry}><RotateCcw size={14} />重新连接</button></>}</div>}
          <div className="viewport-tools"><div className="tool-group"><span className="tool-grip"><MousePointer2 size={15} /></span>{iconTools.map(({ id, label, icon: Icon }) => <button key={id} title={label} aria-label={label} aria-pressed={transformMode === id} className={transformMode === id ? 'active' : ''} disabled={!lab.editing} onClick={() => setTransformMode(id)}><Icon size={17} /></button>)}</div><div className="tool-group"><button title="三维视角" aria-label="三维视角" className={cameraMode === 'perspective' ? 'active' : ''} onClick={() => setCameraMode('perspective')}><Cuboid size={17} /></button><button title="俯视平面" aria-label="俯视平面" className={cameraMode === 'top' ? 'active' : ''} onClick={() => setCameraMode('top')}><Grid2X2 size={17} /></button><button title="设备标签" aria-label="设备标签" aria-pressed={showLabels} className={showLabels ? 'active' : ''} onClick={() => setShowLabels(value => !value)}><Tags size={17} /></button></div></div>
          <div className="viewport-footnote"><span><span className="axis-label x">X</span><span className="axis-label y">Y</span><span className="axis-label z">Z</span>单位：米</span><span>拖拽旋转 · 滚轮缩放</span></div>
          {!lab.editing && lab.snapshot && <div className="run-bar"><span className="simulation-clock"><Activity size={14} /><span>仿真时间<strong>{new Date(lab.snapshot.sim_time).toLocaleTimeString('zh-CN', { hour12: false })}</strong></span></span><div className="run-buttons"><button className="button secondary" disabled={Boolean(lab.pending) || agentRunning} onClick={() => void lab.advance(60)}><Play size={12} />+1 分钟</button><button className="button secondary" disabled={Boolean(lab.pending) || agentRunning} onClick={() => void lab.advance(300)}>+5 分钟</button>{agentRunning ? <button className="icon-button" title="停止流程" aria-label="停止流程" disabled={Boolean(lab.pending)} onClick={() => void lab.stop()}><Square size={14} /></button> : <button className="icon-button" title="重置实验" aria-label="重置实验" disabled={Boolean(lab.pending)} onClick={() => void lab.reset()}><RotateCcw size={15} /></button>}</div></div>}
        </section>
        <EnvironmentPanel snapshot={lab.snapshot} editing={lab.editing} busy={Boolean(lab.pending)} agentConfigured={lab.catalog?.agent_configured ?? false} agentReason={lab.catalog?.agent_readiness?.reason ?? null} onAgent={(mode, task) => void lab.startAgent(mode, task)} onStop={() => void lab.stop()} />
      </div>

      <Inspector object={selected} devices={room?.devices ?? []} editing={lab.editing} initial={selected?.device_id ? lab.scene?.initial_states[selected.device_id] : undefined} state={selected?.device_id ? lab.snapshot?.devices[selected.device_id] : undefined} busy={Boolean(lab.pending)} onUpdate={patch => { if (selected) lab.updateObject(selected.id, patch); }} onParameters={parameters => { if (selected?.device_id) { if (lab.editing) lab.updateInitialState(selected.device_id, parameters); else void lab.command(selected.device_id, parameters); } }} onDelete={remove} onDuplicate={duplicate} />
    </main>

    <footer className="statusbar"><span><span className={`status-dot ${lab.catalog ? 'green' : 'amber'}`} />{lab.catalog ? 'FAIRY 世界模型已连接' : '等待实验服务'}</span><span className="statusbar-middle">{lab.scene ? `${lab.scene.dimensions.width} × ${lab.scene.dimensions.depth} × ${lab.scene.dimensions.height} m` : '—'}<i />{lab.editing ? '布局不会改变环境物理参数' : `${onlineCount} 个在线设备 · 房间级环境仿真`}</span><span>{!lab.editing && (lab.connection === 'connected' ? <Wifi size={12} /> : <WifiOff size={12} />)}{lab.editing ? 'LOCAL WORKSPACE' : lab.connection === 'connected' ? '状态已同步' : '正在恢复连接'}</span></footer>
    {lab.error && <div className="error-toast" role="alert"><Unplug size={18} /><div><strong>操作未完成</strong><p>{lab.error}</p></div><button className="icon-button" aria-label="关闭错误提示" onClick={lab.dismissError}><X size={16} /></button></div>}
    {lab.notice && !lab.error && <div className="success-toast" role="status"><Check size={15} />{lab.notice}</div>}
    {showHelp && <div className="modal-backdrop" onClick={() => setShowHelp(false)}><div className="help-modal" role="dialog" aria-modal="true" aria-label="使用说明" onClick={event => event.stopPropagation()}><button className="icon-button modal-close" aria-label="关闭使用说明" onClick={() => setShowHelp(false)}><X size={18} /></button><span className="eyebrow">GETTING STARTED</span><h2>从一个空间，到一次实验。</h2><ol><li><strong>布置场景</strong><p>选择房间，在物体库添加家具，拖动物体或输入精确坐标。</p></li><li><strong>绑定与设置</strong><p>将灯和空调关联到已有设备，设置初始状态，保存场景。</p></li><li><strong>观察变化</strong><p>进入运行模式，操作设备、推进仿真时间，或在 Agent 面板运行演示流程。</p></li></ol><div className="help-note">一个设备可以对应多个视觉物体。灯光与气流用于表现状态；环境数值来自现有房间级仿真。</div><button className="button primary" onClick={() => setShowHelp(false)}>开始搭建<ArrowUpRight size={15} /></button></div></div>}
    <button className="sr-only" onClick={remove} disabled={!selected || !lab.editing}><Trash2 size={12} />删除选中物体</button>
  </div>;
}
