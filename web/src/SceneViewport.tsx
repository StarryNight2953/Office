import { Component, Suspense, useCallback, useEffect, useMemo, useRef, useState, type ReactNode, type RefObject } from 'react';
import { Canvas, useFrame, useThree } from '@react-three/fiber';
import {
  Clone, ContactShadows, Grid, Html, OrbitControls, OrthographicCamera,
  PerspectiveCamera, TransformControls, useGLTF,
} from '@react-three/drei';
import * as THREE from 'three';
import Furniture, { DeviceEffects } from './Furniture';
import type { DeviceState, Scene, SceneObject, Vec3 } from './types';

type Props = {
  scene: Scene;
  selectedId: string | null;
  onSelect: (id: string | null) => void;
  onTransform: (id: string, patch: Partial<SceneObject>) => void;
  editing: boolean;
  busy: boolean;
  transformMode: 'translate' | 'rotate' | 'scale';
  cameraMode: 'perspective' | 'top';
  states: Record<string, DeviceState>;
  showLabels: boolean;
};

class ModelBoundary extends Component<{ children: ReactNode; fallback: ReactNode }, { failed: boolean }> {
  state = { failed: false };
  static getDerivedStateFromError() { return { failed: true }; }
  render() { return this.state.failed ? this.props.fallback : this.props.children; }
}

function ImportedAsset({ url, kind, state }: { url: string; kind: string; state?: DeviceState }) {
  const { scene } = useGLTF(url);
  return <><Clone object={scene} castShadow receiveShadow /><DeviceEffects kind={kind} state={state} /></>;
}

function Outline({ target }: { target: RefObject<THREE.Group | null> }) {
  const { scene } = useThree();
  const helper = useRef<THREE.BoxHelper | null>(null);
  useEffect(() => {
    if (!target.current) return;
    const box = new THREE.BoxHelper(target.current, '#348b79');
    box.material.depthTest = false;
    box.material.transparent = true;
    box.material.opacity = 0.7;
    box.renderOrder = 10;
    box.raycast = () => {};
    helper.current = box;
    scene.add(box);
    return () => {
      scene.remove(box);
      box.geometry.dispose();
      box.material.dispose();
      helper.current = null;
    };
  }, [scene, target]);
  useFrame(() => helper.current?.update());
  return null;
}

function ObjectNode({ object, selected, state, onSelect, onTransform, editing, transformMode, showLabels }: {
  object: SceneObject; selected: boolean; state?: DeviceState;
  onSelect: Props['onSelect']; onTransform: Props['onTransform'];
  editing: boolean; transformMode: Props['transformMode']; showLabels: boolean;
}) {
  const group = useRef<THREE.Group>(null);
  const [target, setTarget] = useState<THREE.Group | null>(null);
  const attachGroup = useCallback((node: THREE.Group | null) => { group.current = node; setTarget(node); }, []);
  useEffect(() => () => { document.body.style.cursor = 'auto'; }, []);
  const labelHeight = ['hvac', 'lighting', 'sensor', 'projector', 'ventilation', 'access_control'].includes(object.kind) ? 0.3 : object.kind === 'plant' ? 1.3 : 1.15;
  const assetUrl = object.asset_url && /^\/models\/[A-Za-z0-9_/-]+\.glb$/.test(object.asset_url) && !object.asset_url.includes('..') ? object.asset_url : null;
  const body = <group
    ref={attachGroup} position={object.position} rotation={object.rotation} scale={object.scale}
    onClick={event => { event.stopPropagation(); onSelect(object.id); }}
    onPointerOver={event => { event.stopPropagation(); document.body.style.cursor = 'pointer'; }}
    onPointerOut={() => { document.body.style.cursor = 'auto'; }}
  >
    {assetUrl ? <ModelBoundary key={assetUrl} fallback={<Furniture kind={object.kind} state={state} />}>
      <Suspense fallback={<Furniture kind={object.kind} state={state} />}><ImportedAsset url={assetUrl} kind={object.kind} state={state} /></Suspense>
    </ModelBoundary> : <Furniture kind={object.kind} state={state} />}
    {(showLabels && object.device_id || selected) && <Html position={[0, labelHeight, 0]} center zIndexRange={[20, 0]} style={{ pointerEvents: 'none' }}>
      <div className={`scene-object-label${selected ? ' selected' : ''}`}>
        {object.device_id && <span className={`object-state-dot ${state?.power_on ? 'is-on' : ''}`} />}
        {object.name}
      </div>
    </Html>}
  </group>;
  const commit = () => {
    if (!group.current) return;
    const { position, rotation, scale } = group.current;
    onTransform(object.id, {
      position: position.toArray().map(n => Math.round(n * 1000) / 1000) as Vec3,
      rotation: [rotation.x, rotation.y, rotation.z],
      scale: scale.toArray().map(n => Math.max(0.05, Math.round(n * 1000) / 1000)) as Vec3,
    });
  };
  return <>
    {body}
    {editing && selected && target && <TransformControls object={target} mode={transformMode} size={0.8} onMouseUp={commit} />}
    {selected && <Outline target={group} />}
  </>;
}

function RoomShell({ width, depth, height }: Scene['dimensions']) {
  const windowCount = Math.max(2, Math.floor(width / 2));
  const windowWidth = width / windowCount - 0.46;
  return <group>
    <mesh position={[0, -0.11, 0]} receiveShadow castShadow>
      <boxGeometry args={[width + 0.24, 0.22, depth + 0.24]} />
      <meshStandardMaterial color="#d4d5c8" roughness={0.95} />
    </mesh>
    <mesh position={[0, 0.005, 0]} rotation={[-Math.PI / 2, 0, 0]} receiveShadow>
      <planeGeometry args={[width, depth]} /><meshStandardMaterial color="#eceadf" roughness={0.9} />
    </mesh>
    <Grid position={[0, 0.012, 0]} args={[width, depth]} cellSize={1} cellThickness={0.4} cellColor="#cecec2" sectionSize={5} sectionThickness={0.7} sectionColor="#c6c8bc" fadeDistance={60} fadeStrength={0.2} infiniteGrid={false} />
    <mesh position={[0, height / 2, -depth / 2 - 0.07]} receiveShadow castShadow><boxGeometry args={[width + 0.24, height, 0.14]} /><meshStandardMaterial color="#e8e9df" roughness={0.9} /></mesh>
    <mesh position={[-width / 2 - 0.07, height / 2, 0]} receiveShadow castShadow><boxGeometry args={[0.14, height, depth]} /><meshStandardMaterial color="#dedfd3" roughness={0.9} /></mesh>
    <mesh position={[0, 0.07, -depth / 2 + 0.015]}><boxGeometry args={[width, 0.14, 0.035]} /><meshStandardMaterial color="#a7b1a3" /></mesh>
    <mesh position={[-width / 2 + 0.015, 0.07, 0]}><boxGeometry args={[0.035, 0.14, depth]} /><meshStandardMaterial color="#a7b1a3" /></mesh>
    {Array.from({ length: windowCount }, (_, i) => {
      const x = -width / 2 + width / windowCount * (i + 0.5);
      return <group key={i} position={[x, height * 0.59, -depth / 2 + 0.02]}>
        <mesh><boxGeometry args={[windowWidth + 0.11, height * 0.46 + 0.11, 0.04]} /><meshStandardMaterial color="#b0bdb4" /></mesh>
        <mesh position={[0, 0, 0.028]}><planeGeometry args={[windowWidth, height * 0.46]} /><meshStandardMaterial color="#c8dcd7" roughness={0.3} metalness={0.1} emissive="#d4e6dd" emissiveIntensity={0.16} /></mesh>
        <mesh position={[0, 0, 0.04]}><boxGeometry args={[0.035, height * 0.46, 0.025]} /><meshStandardMaterial color="#a8b7ac" /></mesh>
        <mesh position={[0, -height * 0.24, 0.07]}><boxGeometry args={[windowWidth + 0.18, 0.06, 0.18]} /><meshStandardMaterial color="#f4f2e8" /></mesh>
      </group>;
    })}
    <mesh position={[-width / 2 + 0.02, height * 0.54, depth * 0.17]} rotation={[0, Math.PI / 2, 0]}><planeGeometry args={[Math.min(depth * 0.35, 2.5), height * 0.42]} /><meshStandardMaterial color="#f4f4eb" roughness={0.7} /></mesh>
    <mesh position={[-width / 2 + 0.04, height * 0.31, depth * 0.17]}><boxGeometry args={[0.11, 0.045, Math.min(depth * 0.35, 2.5)]} /><meshStandardMaterial color="#9ba99b" /></mesh>
  </group>;
}

function CameraSetup({ mode, width, depth }: { mode: Props['cameraMode']; width: number; depth: number }) {
  const { size } = useThree();
  const span = Math.max(width, depth);
  const topZoom = Math.min(size.width / (width + 3), size.height / (depth + 3));
  return <>
    {mode === 'top'
      ? <OrthographicCamera makeDefault position={[0, span * 2.5, 0.001]} zoom={topZoom} near={0.1} far={300} />
      : <PerspectiveCamera makeDefault position={[width * 1.05, span * 0.95, depth * 1.25]} fov={38} near={0.05} far={300} />}
    <OrbitControls key={`${mode}-${width}-${depth}`} makeDefault target={[0, mode === 'top' ? 0 : 0.65, 0]} enableRotate={mode !== 'top'} minPolarAngle={0.08} maxPolarAngle={Math.PI / 2.1} minDistance={2} maxDistance={span * 4} minZoom={10} maxZoom={200} enableDamping dampingFactor={0.08} />
  </>;
}

export default function SceneViewport({ scene, selectedId, onSelect, onTransform, editing, busy, transformMode, cameraMode, states, showLabels }: Props) {
  const { width, depth, height } = scene.dimensions;
  const lightSpan = Math.max(width, depth);
  const initialStates = useMemo(() => Object.fromEntries(Object.entries(scene.initial_states).map(([id, parameters]) => [id, {
    device_id: id,
    device_type: 'brightness_pct' in parameters ? 'lighting' : 'hvac',
    room_id: scene.room_id, zone_id: '', power_on: Boolean(parameters.power_on),
    mode: String(parameters.mode ?? parameters.scene ?? 'off'),
    level: Number(parameters.fan_level ?? 0),
    target_temperature_c: parameters.target_temperature_c == null ? null : Number(parameters.target_temperature_c),
    health: 'online', settings: parameters, rated_power_w: 0,
  } as DeviceState])), [scene.initial_states, scene.room_id]);
  const displayedStates = editing ? initialStates : states;
  return <div className="scene-canvas" role="region" aria-label="三维房间场景。使用鼠标旋转视角，点击物体选择；也可通过物体列表和属性面板操作。">
    <ModelBoundary fallback={<div style={{ padding: '48px 24px', textAlign: 'center', color: '#64736a' }}><strong>三维视图暂不可用</strong><p>请启用浏览器硬件加速后刷新。物体列表和属性面板仍可使用。</p></div>}>
    <Canvas shadows dpr={[1, 1.75]} gl={{ antialias: true, alpha: true }} onPointerMissed={() => onSelect(null)}>
      <color attach="background" args={['#f0f1e9']} />
      <fog attach="fog" args={['#f0f1e9', lightSpan * 3.5, lightSpan * 8]} />
      <CameraSetup mode={cameraMode} width={width} depth={depth} />
      <ambientLight intensity={1.15} />
      <hemisphereLight args={['#f7fff4', '#a8ad99', 1.4]} />
      <directionalLight position={[4, 10, 6]} intensity={2.3} color="#fff6e3" castShadow shadow-mapSize={[2048, 2048]} shadow-camera-left={-lightSpan} shadow-camera-right={lightSpan} shadow-camera-top={lightSpan} shadow-camera-bottom={-lightSpan} shadow-normalBias={0.035} />
      <directionalLight position={[-5, 4, -5]} intensity={0.65} color="#e0eee9" />
      <RoomShell width={width} depth={depth} height={height} />
      {scene.objects.map(object => <ObjectNode key={object.id} object={object} selected={selectedId === object.id} state={object.device_id ? displayedStates[object.device_id] : undefined} onSelect={onSelect} onTransform={onTransform} editing={editing && !busy} transformMode={transformMode} showLabels={showLabels} />)}
      <ContactShadows position={[0, -0.23, 0]} opacity={0.28} scale={Math.max(width, depth) * 2} blur={2.5} far={5} resolution={512} color="#798071" />
    </Canvas>
    </ModelBoundary>
  </div>;
}
