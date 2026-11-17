import { useEffect, useState } from 'react';
import { Box, Check, ChevronDown, Copy, Lightbulb, Link2, Power, SlidersHorizontal, Snowflake, Trash2 } from 'lucide-react';
import { defaultParameters, KIND_NAMES, parametersFromState } from './types';
import type { DeviceSpec, DeviceState, Parameters, SceneObject, Vec3 } from './types';

function NumberField({ value, onChange, label, unit, min, disabled }: { value: number; onChange: (value: number) => void; label: string; unit?: string; min?: number; disabled?: boolean }) {
  const [draft, setDraft] = useState(String(Number(value.toFixed(3))));
  useEffect(() => setDraft(String(Number(value.toFixed(3)))), [value]);
  const commit = () => {
    const next = Number(draft);
    if (draft.trim() && Number.isFinite(next) && (min === undefined || next >= min)) onChange(next);
    else setDraft(String(Number(value.toFixed(3))));
  };
  return <label className="number-field"><span>{label}</span><input aria-label={label} type="number" disabled={disabled} step="0.1" min={min} value={draft} onChange={event => setDraft(event.target.value)} onBlur={commit} onKeyDown={event => { if (event.key === 'Enter') { commit(); event.currentTarget.blur(); } }} />{unit && <small>{unit}</small>}</label>;
}

interface Props {
  object: SceneObject | undefined;
  devices: DeviceSpec[];
  editing: boolean;
  initial?: Parameters;
  state?: DeviceState;
  busy: boolean;
  onUpdate: (patch: Partial<SceneObject>) => void;
  onParameters: (parameters: Parameters) => void;
  onDelete: () => void;
  onDuplicate: () => void;
}

export default function Inspector({ object, devices, editing, initial, state, busy, onUpdate, onParameters, onDelete, onDuplicate }: Props) {
  const type = object?.kind ?? '';
  const base = editing ? { ...defaultParameters(type), ...initial } : state ? parametersFromState(state) : defaultParameters(type);
  const baseSignature = JSON.stringify(base);
  const [draft, setDraft] = useState<Parameters>(base);
  useEffect(() => setDraft(JSON.parse(baseSignature)), [baseSignature, object?.id, editing]);
  const updateDraft = (patch: Parameters) => {
    const next = { ...draft, ...patch };
    setDraft(next);
    if (editing && object?.device_id) onParameters(next);
  };
  const updateVector = (field: 'position' | 'rotation' | 'scale', axis: number, value: number) => {
    if (!object) return;
    const vector = [...object[field]] as Vec3;
    vector[axis] = field === 'rotation' ? value * Math.PI / 180 : value;
    onUpdate({ [field]: vector });
  };
  const controllable = object?.device_id && ['lighting', 'hvac'].includes(type);
  const matching = devices.filter(device => device.device_type === type);

  return <aside className="inspector panel">
    <div className="panel-heading"><span><SlidersHorizontal size={15} /> 属性</span><small>{editing ? '编辑模式' : '运行状态'}</small></div>
    {!object ? <div className="selection-empty"><div className="selection-cube"><Box size={28} strokeWidth={1.3} /></div><h3>让空间中的物体可交互</h3><p>在场景或左侧列表中选择物体，<br />查看它的位置、绑定与状态。</p><div className="hint-shortcuts"><kbd>拖拽</kbd><span>旋转视角</span><kbd>滚轮</kbd><span>缩放场景</span></div></div> : <>
      <div className="object-identity"><span className={`object-glyph ${type === 'lighting' ? 'warm' : ''}`}>{type === 'lighting' ? <Lightbulb size={22} /> : type === 'hvac' ? <Snowflake size={22} /> : <Box size={22} />}</span><div><strong>{object.name}</strong><span>{KIND_NAMES[object.kind]} <i>·</i> {object.device_id ? '设备已绑定' : '场景物体'}</span></div></div>
      <div className="inspector-scroll">
        <section className="property-section">
          <h3>基本信息</h3>
          <label className="field-label">名称<input value={object.name} disabled={!editing || busy} maxLength={80} onChange={event => onUpdate({ name: event.target.value })} /></label>
          {editing ? <label className="field-label">关联设备<div className="select-wrap"><Link2 size={13} /><select disabled={busy} aria-label="关联设备" value={object.device_id ?? ''} onChange={event => onUpdate({ device_id: event.target.value || null })}><option value="">未绑定 · 仅用于展示</option>{matching.map(device => <option key={device.device_id} value={device.device_id}>{device.device_id}</option>)}</select><ChevronDown size={12} /></div></label> : <div className="key-value"><span>设备 ID</span><code>{object.device_id ?? '无设备绑定'}</code></div>}
          {!editing && state && <div className="device-health"><span className={`status-dot ${state.health === 'online' ? 'green' : 'red'}`} />{state.health === 'online' ? '设备在线' : state.health === 'offline' ? '设备离线' : '设备故障'}<span>{state.rated_power_w} W 额定功率</span></div>}
        </section>

        <section className="property-section">
          <h3>空间变换 <span>米 / 度</span></h3>
          {(['position', 'rotation', 'scale'] as const).map((field, index) => <div className="transform-row" key={field}><span className="property-label">{['位置', '旋转', '缩放'][index]}</span><div className="vector-inputs">{object[field].map((value, axis) => editing ? <NumberField disabled={busy} key={axis} label={`${['X', 'Y', 'Z'][axis]} ${['位置', '旋转', '缩放'][index]}`} value={field === 'rotation' ? value * 180 / Math.PI : value} min={field === 'scale' ? 0.01 : undefined} onChange={next => updateVector(field, axis, next)} /> : <div className="vector-static" key={axis}><small>{['X', 'Y', 'Z'][axis]}</small>{(field === 'rotation' ? value * 180 / Math.PI : value).toFixed(1)}</div>)}</div></div>)}
          <p className="micro-note">Y 轴为高度 · 位置不改变房间级物理计算</p>
        </section>

        {controllable && <section className="property-section device-controls">
          <h3>{editing ? '实验初始状态' : '设备控制'}<span>{type === 'lighting' ? 'LIGHTING' : 'HVAC'}</span></h3>
          <div className="power-row"><div><Power size={16} /><span>{editing ? '初始电源' : '电源'}</span></div><button className={`toggle ${draft.power_on ? 'on' : ''}`} role="switch" aria-checked={Boolean(draft.power_on)} aria-label="设备电源" disabled={busy || (!editing && state?.health !== 'online')} onClick={() => updateDraft({ power_on: !draft.power_on })}><span /></button></div>
          {type === 'lighting' ? <>
            <label className="range-field"><span>亮度 <strong>{Number(draft.brightness_pct)}<small>%</small></strong></span><input aria-label="灯光亮度" type="range" min="1" max="100" value={Number(draft.brightness_pct)} disabled={busy} onChange={event => updateDraft({ brightness_pct: Number(event.target.value) })} /><div className="range-limits"><span>柔和</span><span>明亮</span></div></label>
            <label className="range-field"><span>色温 <strong>{Number(draft.color_temperature_k)}<small>K</small></strong></span><input className="temperature-range" aria-label="灯光色温" type="range" min="2700" max="6500" step="100" value={Number(draft.color_temperature_k)} disabled={busy} onChange={event => updateDraft({ color_temperature_k: Number(event.target.value) })} /><div className="range-limits"><span>暖光</span><span>冷光</span></div></label>
            <label className="field-label">照明场景<select value={String(draft.scene)} disabled={busy} onChange={event => updateDraft({ scene: event.target.value })}><option value="work">日常办公</option><option value="presentation">演示模式</option><option value="meeting">会议模式</option><option value="focus">专注模式</option>{!['work', 'presentation', 'meeting', 'focus'].includes(String(draft.scene)) && <option value={String(draft.scene)}>{String(draft.scene)}</option>}</select></label>
          </> : <>
            <label className="field-label">运行模式<select value={String(draft.mode)} disabled={busy} onChange={event => updateDraft({ mode: event.target.value })}><option value="cooling">制冷</option><option value="heating">制热</option><option value="fan">送风</option></select></label>
            <label className="range-field"><span>设定温度 <strong>{Number(draft.target_temperature_c)}<small>°C</small></strong></span><input aria-label="设定温度" type="range" min="16" max="30" step="0.5" value={Number(draft.target_temperature_c)} disabled={busy} onChange={event => updateDraft({ target_temperature_c: Number(event.target.value) })} /><div className="range-limits"><span>16°C</span><span>30°C</span></div></label>
            <div className="field-label">风速<div className="segmented fan-selector">{[1, 2, 3].map(level => <button key={level} className={draft.fan_level === level ? 'active' : ''} disabled={busy} onClick={() => updateDraft({ fan_level: level })}>{['低风', '中风', '高风'][level - 1]}</button>)}</div></div>
          </>}
          {!editing && <><button className="button primary apply-button" disabled={busy || !state || state.health !== 'online'} onClick={() => onParameters(draft)}><Check size={15} />{busy ? '正在提交…' : '应用设备设置'}</button><p className="micro-note">画面根据服务端确认的状态更新。</p></>}
          {editing && <p className="micro-note">保存场景后，在新建或重置实验时生效。</p>}
        </section>}
        {!controllable && object.device_id && <section className="property-section"><h3>设备状态</h3><p className="micro-note">此类设备目前支持位置编辑与状态查看。</p><div className="key-value"><span>运行模式</span><strong>{state?.mode ?? '运行后可见'}</strong></div><div className="key-value"><span>电源</span><strong>{state ? state.power_on ? '已开启' : '已关闭' : '—'}</strong></div></section>}
        {editing && <section className="property-section asset-property"><h3>模型资产 <span>可选</span></h3><label className="field-label">GLB 路径<input disabled={busy} placeholder="/models/device.glb" value={object.asset_url ?? ''} onChange={event => onUpdate({ asset_url: event.target.value || null })} /></label><p className="micro-note">未指定时使用内置模型。替换外观保留设备绑定。</p></section>}
      </div>
      {editing && <div className="inspector-actions"><button className="button secondary" disabled={busy} onClick={onDuplicate}><Copy size={14} />复制物体</button><button className="button icon-button danger" title="删除视觉物体" aria-label="删除视觉物体" disabled={busy} onClick={onDelete}><Trash2 size={15} /></button></div>}
    </>}
  </aside>;
}
