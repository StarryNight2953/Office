export type Vec3 = [number, number, number];
export type ObjectKind = 'desk' | 'chair' | 'table' | 'plant' | 'lighting' | 'hvac' | 'sensor' | 'projector' | 'audio' | 'printer' | 'humidifier' | 'air_purifier' | 'ventilation' | 'access_control' | 'cabinet';
export type Parameters = Record<string, string | number | boolean | null>;

export interface SceneObject {
  id: string;
  name: string;
  kind: ObjectKind;
  position: Vec3;
  rotation: Vec3;
  scale: Vec3;
  device_id: string | null;
  asset_url: string | null;
}

export interface Dimensions { width: number; depth: number; height: number }
export interface Scene {
  schema_version: 1;
  id: string;
  room_id: string;
  name: string;
  schematic: boolean;
  dimensions: Dimensions;
  objects: SceneObject[];
  initial_states: Record<string, Parameters>;
}
export interface DeviceSpec {
  device_id: string;
  device_type: string;
  room_id: string;
  zone_id: string;
  rated_power_w: number;
  capabilities: string[];
  functional_area_id: string | null;
}
export interface Room {
  id: string;
  name: string;
  capacity: number;
  area_m2: number;
  dimensions: Dimensions;
  devices: DeviceSpec[];
}
export interface Catalog {
  rooms: Room[];
  assets: { id: string; name: string; kind: ObjectKind }[];
  agent_configured: boolean;
  agent_readiness: { configured: boolean; model: string | null; reason: string | null; reason_code: string | null };
}
export interface DeviceState {
  device_id: string;
  device_type: string;
  room_id: string;
  zone_id: string;
  power_on: boolean;
  mode: string;
  level: number;
  target_temperature_c: number | null;
  health: string;
  settings: Record<string, unknown>;
  rated_power_w: number;
}
export interface SensorReading {
  sensor_id: string;
  zone_id: string;
  quantity: string;
  value: number | null;
  unit: string;
  quality: string;
  observed_at: string;
}
export interface RunEvent {
  id: string;
  kind: string;
  time: string;
  message: string;
  source: string;
  device_id?: string;
  details?: Record<string, unknown>;
}
export interface Snapshot {
  run_id: string;
  scene_id: string;
  room_id: string;
  status: 'ready' | 'running' | 'stopped' | 'error';
  sim_time: string;
  revision: number;
  devices: Record<string, DeviceState>;
  sensors: SensorReading[];
  events: RunEvent[];
  agent: { status: string; mode: 'demo' | 'llm' | null; error?: string; error_code?: string; summary?: string };
  metrics: { energy_kwh: number };
}

export const KIND_NAMES: Record<ObjectKind, string> = {
  desk: '办公桌', chair: '座椅', table: '会议桌', plant: '绿植', lighting: '灯具', hvac: '空调', sensor: '传感器', projector: '投影仪', audio: '音响', printer: '打印机', humidifier: '加湿器', air_purifier: '净化器', ventilation: '新风', access_control: '门禁', cabinet: '储物柜',
};

export function defaultParameters(type: string): Parameters {
  return type === 'lighting'
    ? { power_on: false, brightness_pct: 75, color_temperature_k: 4000, scene: 'work' }
    : { power_on: false, mode: 'cooling', target_temperature_c: 24, fan_level: 2 };
}

export function parametersFromState(state: DeviceState): Parameters {
  return state.device_type === 'lighting'
    ? { power_on: state.power_on, brightness_pct: Number(state.settings.brightness_pct) || 75, color_temperature_k: Number(state.settings.color_temperature_k) || 4000, scene: String(state.settings.scene && state.settings.scene !== 'off' ? state.settings.scene : 'work') }
    : { power_on: state.power_on, mode: state.mode === 'off' ? 'cooling' : state.mode, target_temperature_c: state.target_temperature_c ?? 24, fan_level: state.level || 2 };
}
