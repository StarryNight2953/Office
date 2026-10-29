import { useRef } from 'react';
import { useFrame } from '@react-three/fiber';
import { RoundedBox } from '@react-three/drei';
import * as THREE from 'three';
import type { DeviceState } from './types';

const palette = {
  oak: '#d8bd98', ivory: '#f4f2e9', charcoal: '#334942', metal: '#78847e',
  leaf: '#597c58', terracotta: '#b99e87', screen: '#234f49',
};

function Block({ size, position = [0, 0, 0], color = palette.ivory, radius = 0.025, ...props }: {
  size: [number, number, number]; position?: [number, number, number]; color?: string;
  radius?: number; rotation?: [number, number, number];
}) {
  return <RoundedBox args={size} position={position} radius={radius} smoothness={2} castShadow receiveShadow {...props}>
    <meshStandardMaterial color={color} roughness={0.67} />
  </RoundedBox>;
}

function Desk() {
  return <group>
    <Block size={[1.45, 0.075, 0.72]} position={[0, 0.755, 0]} color={palette.oak} />
    {[-0.61, 0.61].map(x => <group key={x}>
      <Block size={[0.055, 0.7, 0.055]} position={[x, 0.36, -0.26]} color={palette.metal} radius={0.012} />
      <Block size={[0.055, 0.7, 0.055]} position={[x, 0.36, 0.26]} color={palette.metal} radius={0.012} />
      <Block size={[0.055, 0.055, 0.58]} position={[x, 0.05, 0]} color={palette.metal} radius={0.012} />
    </group>)}
    <Block size={[0.2, 0.025, 0.15]} position={[0.1, 0.806, -0.12]} color={palette.charcoal} radius={0.012} />
    <Block size={[0.04, 0.18, 0.04]} position={[0.1, 0.9, -0.17]} color={palette.charcoal} radius={0.008} />
    <Block size={[0.52, 0.32, 0.035]} position={[0.1, 1.08, -0.17]} color={palette.charcoal} radius={0.012} />
    <mesh position={[0.1, 1.08, -0.15]}><planeGeometry args={[0.47, 0.27]} /><meshStandardMaterial color={palette.screen} emissive="#39796e" emissiveIntensity={0.12} /></mesh>
    <Block size={[0.34, 0.018, 0.12]} position={[0.1, 0.81, 0.14]} color="#ced3c9" radius={0.006} />
    <Block size={[0.2, 0.022, 0.26]} position={[-0.45, 0.81, 0.08]} color="#e6e5d9" radius={0.005} />
  </group>;
}

function Chair() {
  return <group>
    <mesh position={[0, 0.23, 0]} castShadow><cylinderGeometry args={[0.027, 0.032, 0.35, 10]} /><meshStandardMaterial color={palette.metal} metalness={0.6} roughness={0.4} /></mesh>
    {[0, 1, 2, 3, 4].map(i => <group key={i} rotation={[0, i * Math.PI * 0.4, 0]}>
      <Block size={[0.045, 0.035, 0.3]} position={[0, 0.07, 0.12]} color={palette.charcoal} radius={0.01} />
      <mesh position={[0, 0.045, 0.26]} rotation={[0, 0, Math.PI / 2]} castShadow><cylinderGeometry args={[0.035, 0.035, 0.05, 10]} /><meshStandardMaterial color="#283732" /></mesh>
    </group>)}
    <Block size={[0.48, 0.09, 0.46]} position={[0, 0.45, 0]} color={palette.charcoal} radius={0.04} />
    <Block size={[0.45, 0.44, 0.075]} position={[0, 0.73, -0.2]} color="#698178" radius={0.035} rotation={[-0.1, 0, 0]} />
    {[-0.255, 0.255].map(x => <group key={x}>
      <Block size={[0.035, 0.2, 0.035]} position={[x, 0.55, 0.02]} color={palette.charcoal} radius={0.01} />
      <Block size={[0.055, 0.035, 0.25]} position={[x, 0.65, 0.04]} color={palette.charcoal} radius={0.014} />
    </group>)}
  </group>;
}

function Table() {
  return <group>
    <Block size={[2.8, 0.09, 1.18]} position={[0, 0.77, 0]} color={palette.oak} radius={0.04} />
    {[-0.9, 0.9].map(x => <group key={x}>
      <Block size={[0.13, 0.71, 0.7]} position={[x, 0.37, 0]} color={palette.ivory} />
      <Block size={[0.38, 0.045, 0.82]} position={[x, 0.045, 0]} color={palette.metal} radius={0.02} />
    </group>)}
    <Block size={[0.48, 0.018, 0.16]} position={[0, 0.827, 0]} color={palette.charcoal} radius={0.02} />
    <Block size={[0.23, 0.025, 0.29]} position={[-0.72, 0.833, 0.21]} color="#f5f2e9" radius={0.005} rotation={[0, -0.12, 0]} />
    <Block size={[0.21, 0.023, 0.27]} position={[0.75, 0.833, -0.23]} color="#a9b7a5" radius={0.005} rotation={[0, 0.16, 0]} />
  </group>;
}

function Plant() {
  return <group>
    <mesh position={[0, 0.2, 0]} castShadow><cylinderGeometry args={[0.21, 0.145, 0.4, 20]} /><meshStandardMaterial color={palette.terracotta} roughness={0.95} /></mesh>
    <mesh position={[0, 0.405, 0]}><cylinderGeometry args={[0.19, 0.19, 0.01, 20]} /><meshStandardMaterial color="#4f4d38" /></mesh>
    {[0, 1, 2, 3, 4, 5].map(i => <group key={i} rotation={[0, i * 2.4, 0]}>
      <mesh position={[0.045, 0.65 + (i % 3) * 0.07, 0]} rotation={[0, 0, -0.12]} castShadow><cylinderGeometry args={[0.009, 0.012, 0.5, 6]} /><meshStandardMaterial color="#607b48" /></mesh>
      <mesh position={[0.14, 0.88 + (i % 3) * 0.09, 0]} rotation={[0.3, 0, -0.55]} scale={[0.1, 0.29, 0.15]} castShadow><sphereGeometry args={[1, 9, 7]} /><meshStandardMaterial color={i % 2 ? '#738a5d' : palette.leaf} roughness={0.85} /></mesh>
    </group>)}
  </group>;
}

function Airflow({ heating }: { heating: boolean }) {
  const ref = useRef<THREE.Group>(null);
  useFrame(({ clock }) => {
    ref.current?.children.forEach((child, i) => {
      const t = (clock.elapsedTime * 0.38 + (i % 4) * 0.25) % 1;
      child.position.set((Math.floor(i / 4) - 1) * 0.22, -0.16 - t * 0.65, 0.13 + t * 0.95);
      child.scale.setScalar(Math.sin(t * Math.PI) * 0.75 + 0.2);
    });
  });
  return <group ref={ref}>
    {Array.from({ length: 12 }, (_, i) => <mesh key={i} rotation={[0.6, 0, 0]}>
      <capsuleGeometry args={[0.009, 0.12, 3, 6]} />
      <meshBasicMaterial color={heating ? '#d79550' : '#6bbfca'} transparent opacity={0.48} depthWrite={false} />
    </mesh>)}
  </group>;
}

function Hvac({ state }: { state?: DeviceState }) {
  const on = Boolean(state?.power_on && state?.health === 'online');
  return <group>
    <Block size={[1.03, 0.3, 0.25]} color="#f7f7f0" radius={0.04} />
    <Block size={[0.83, 0.028, 0.15]} position={[0, -0.13, 0.07]} color="#51605a" radius={0.008} />
    {[-0.055, 0, 0.055].map(z => <Block key={z} size={[0.8, 0.012, 0.008]} position={[0, -0.145, z + 0.075]} color="#bac7bd" radius={0.003} />)}
    <mesh position={[0.36, -0.04, 0.131]}><circleGeometry args={[0.012, 12]} /><meshBasicMaterial color={on ? '#70bd98' : '#bac2b7'} /></mesh>
    {on && <Airflow heating={state?.mode === 'heating'} />}
  </group>;
}

function Lamp({ state }: { state?: DeviceState }) {
  const on = Boolean(state?.power_on && state?.health === 'online');
  const brightness = on ? Math.max(0, Math.min(1, Number(state?.settings.brightness_pct ?? 100) / 100)) : 0;
  const kelvin = Number(state?.settings.color_temperature_k ?? 4000);
  const color = kelvin < 3500 ? '#ffd5a0' : kelvin > 5000 ? '#ebf6ff' : '#fff0d7';
  return <group>
    <Block size={[1.05, 0.065, 0.29]} color="#b3bdb4" radius={0.018} />
    <mesh position={[0, -0.036, 0]} rotation={[-Math.PI / 2, 0, 0]}><planeGeometry args={[0.97, 0.23]} /><meshStandardMaterial color={on ? color : '#e0e4db'} emissive={color} emissiveIntensity={brightness * 1.7} side={THREE.DoubleSide} /></mesh>
    {on && <pointLight position={[0, -0.18, 0]} color={color} intensity={brightness * 9} distance={4.5} decay={2} />}
  </group>;
}

/** Preserve device feedback when a GLB replaces the built-in mesh. */
export function DeviceEffects({ kind, state }: { kind: string; state?: DeviceState }) {
  if (!state?.power_on || state.health !== 'online') return null;
  if (kind === 'hvac') return <Airflow heating={state.mode === 'heating'} />;
  if (kind === 'lighting') {
    const kelvin = Number(state.settings.color_temperature_k ?? 4000);
    const color = kelvin < 3500 ? '#ffd5a0' : kelvin > 5000 ? '#ebf6ff' : '#fff0d7';
    const intensity = Math.min(1, Math.max(0, Number(state.settings.brightness_pct ?? 100) / 100));
    return <group><pointLight position={[0, -0.18, 0]} color={color} intensity={intensity * 9} distance={4.5} decay={2} /><mesh position={[0, -0.08, 0]}><sphereGeometry args={[0.04, 12, 8]} /><meshBasicMaterial color={color} transparent opacity={0.75} /></mesh></group>;
  }
  return null;
}

function Cabinet() {
  return <group>
    <Block size={[1.1, 1.05, 0.4]} position={[0, 0.54, 0]} color="#d3d6c8" radius={0.02} />
    <Block size={[1.13, 0.045, 0.43]} position={[0, 1.085, 0]} color={palette.oak} radius={0.015} />
    {[-0.265, 0.265].map(x => <group key={x}>
      <Block size={[0.514, 0.94, 0.02]} position={[x, 0.55, 0.209]} color="#e6e7dc" radius={0.005} />
      <Block size={[0.025, 0.14, 0.025]} position={[x > 0 ? 0.07 : -0.07, 0.62, 0.233]} color={palette.metal} radius={0.008} />
    </group>)}
  </group>;
}

function Appliance({ kind, state }: { kind: string; state?: DeviceState }) {
  if (kind === 'projector') return <group>
    <Block size={[0.48, 0.16, 0.34]} color="#f3f3eb" radius={0.03} />
    <mesh position={[0.13, 0, 0.18]} rotation={[Math.PI / 2, 0, 0]}><cylinderGeometry args={[0.055, 0.055, 0.03, 18]} /><meshStandardMaterial color="#26463f" metalness={0.6} roughness={0.2} /></mesh>
    <Block size={[0.04, 0.23, 0.04]} position={[0, 0.16, 0]} color={palette.metal} radius={0.008} />
  </group>;
  if (kind === 'sensor' || kind === 'access_control') return <group>
    <Block size={[0.17, 0.23, 0.065]} color={palette.ivory} radius={0.025} />
    <Block size={[0.115, 0.07, 0.007]} position={[0, 0.035, 0.035]} color="#738d7c" radius={0.004} />
    <mesh position={[0, -0.06, 0.036]}><circleGeometry args={[0.012, 10]} /><meshBasicMaterial color="#70b99b" /></mesh>
  </group>;
  if (kind === 'printer') return <group>
    <Block size={[0.5, 0.38, 0.44]} position={[0, 0.2, 0]} color="#e4e5da" radius={0.03} />
    <Block size={[0.45, 0.09, 0.4]} position={[0, 0.425, -0.01]} color={palette.charcoal} radius={0.02} />
    <Block size={[0.31, 0.024, 0.22]} position={[0, 0.25, 0.22]} color="#f6f4e9" radius={0.005} />
    <Block size={[0.12, 0.05, 0.008]} position={[0.12, 0.36, 0.224]} color="#8daf9f" radius={0.005} />
  </group>;
  if (kind === 'ventilation') return <group>
    <Block size={[0.64, 0.09, 0.48]} color="#d9ded2" radius={0.02} />
    {[-2, -1, 0, 1, 2].map(i => <Block key={i} size={[0.51, 0.015, 0.025]} position={[0, -0.05, i * 0.065]} color="#7e8c80" radius={0.004} />)}
  </group>;
  const tall = kind === 'audio' ? 0.48 : 0.63;
  return <group>
    <Block size={[0.34, tall, 0.31]} position={[0, tall / 2, 0]} color={kind === 'audio' ? '#43564c' : '#edece3'} radius={0.045} />
    <mesh position={[0, tall + 0.004, 0]} rotation={[-Math.PI / 2, 0, 0]}><circleGeometry args={[0.1, 20]} /><meshStandardMaterial color="#86958a" /></mesh>
    <mesh position={[0, tall * 0.72, 0.159]}><circleGeometry args={[0.018, 12]} /><meshBasicMaterial color={state?.power_on ? '#83c4a0' : '#acb9ac'} /></mesh>
    {kind === 'audio' && <mesh position={[0, tall * 0.37, 0.16]}><circleGeometry args={[0.09, 20]} /><meshStandardMaterial color="#26372f" /></mesh>}
  </group>;
}

export default function Furniture({ kind, state }: { kind: string; state?: DeviceState }) {
  switch (kind) {
    case 'desk': return <Desk />;
    case 'chair': return <Chair />;
    case 'table': return <Table />;
    case 'plant': return <Plant />;
    case 'lighting': return <Lamp state={state} />;
    case 'hvac': return <Hvac state={state} />;
    case 'cabinet': return <Cabinet />;
    default: return <Appliance kind={kind} state={state} />;
  }
}
