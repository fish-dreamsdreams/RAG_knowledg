/**
 * 问答助手的 Three 场景：加载 GLB，用光影和四分之三视角做出体积感。
 * 默认站定不晃；指针靠近时只做轻微转向。
 */
import { useLayoutEffect, useRef } from 'react'
import { Canvas, useFrame } from '@react-three/fiber'
import { ContactShadows, useGLTF } from '@react-three/drei'
import { Box3, MeshStandardMaterial, Vector3 } from 'three'

export const ASSISTANT_MODEL_URL = '/assistant/character.glb'

const REST_YAW = 0.38
const REST_PITCH = 0.06

function litMaterial(mat) {
  if (!mat || mat.isMeshStandardMaterial || mat.isMeshPhysicalMaterial) {
    return mat
  }
  return new MeshStandardMaterial({
    map: mat.map ?? null,
    color: mat.color,
    transparent: Boolean(mat.transparent),
    opacity: mat.opacity,
    side: mat.side,
    alphaTest: mat.alphaTest ?? 0,
    roughness: 0.48,
    metalness: 0.08,
  })
}

function FittedModel({ look }) {
  const { scene } = useGLTF(ASSISTANT_MODEL_URL)
  const pivot = useRef()
  const fitted = useRef()
  const floor = useRef(-0.56)

  useLayoutEffect(() => {
    const root = fitted.current
    if (!root) {
      return undefined
    }
    root.scale.setScalar(1)
    root.position.set(0, 0, 0)
    scene.traverse((node) => {
      if (!node.isMesh) {
        return
      }
      node.castShadow = true
      node.receiveShadow = true
      node.material = Array.isArray(node.material)
        ? node.material.map(litMaterial)
        : litMaterial(node.material)
    })
    const box = new Box3().setFromObject(root)
    const size = box.getSize(new Vector3())
    const maxDim = Math.max(size.x, size.y, size.z, 0.0001)
    const scale = 1.22 / maxDim
    root.scale.setScalar(scale)
    const center = box.getCenter(new Vector3())
    root.position.set(-center.x * scale, -center.y * scale, -center.z * scale)
    floor.current = -size.y * scale * 0.5
    return undefined
  }, [scene])

  useFrame((_, delta) => {
    const group = pivot.current
    if (!group) {
      return
    }
    const ease = 1 - Math.exp(-10 * delta)
    group.rotation.y += (look.current.yaw - group.rotation.y) * ease
    group.rotation.x += (look.current.pitch - group.rotation.x) * ease
  })

  return (
    <>
      <group ref={pivot} rotation={[REST_PITCH, REST_YAW, 0]}>
        <group ref={fitted}>
          <primitive object={scene} />
        </group>
      </group>
      <ContactShadows
        position={[0, -0.56, 0]}
        opacity={0.38}
        scale={2.4}
        blur={2.6}
        far={1.6}
        color="#1f2937"
      />
    </>
  )
}

export default function AssistantScene() {
  const look = useRef({ yaw: REST_YAW, pitch: REST_PITCH })

  const follow = (event) => {
    const rect = event.currentTarget.getBoundingClientRect()
    if (!rect.width || !rect.height) {
      return
    }
    const nx = ((event.clientX - rect.left) / rect.width) * 2 - 1
    const ny = ((event.clientY - rect.top) / rect.height) * 2 - 1
    look.current.yaw = REST_YAW + nx * 0.45
    look.current.pitch = REST_PITCH + ny * 0.14
  }

  return (
    <Canvas
      camera={{ position: [0.72, 0.28, 2.85], fov: 36 }}
      dpr={[1, 1.5]}
      style={{ background: 'transparent' }}
      gl={{ alpha: true, antialias: true, premultipliedAlpha: false, powerPreference: 'high-performance' }}
      onCreated={({ gl, camera }) => {
        gl.setClearColor(0x000000, 0)
        gl.toneMappingExposure = 1.18
        camera.lookAt(0, 0.04, 0)
      }}
      onPointerMove={follow}
      onPointerLeave={() => {
        look.current.yaw = REST_YAW
        look.current.pitch = REST_PITCH
      }}
    >
      <hemisphereLight args={['#fff6ec', '#b7c4dc', 0.72]} />
      <ambientLight intensity={0.52} />
      <directionalLight position={[2.6, 3.4, 2.1]} intensity={1.85} color="#fff7ee" />
      <directionalLight position={[-2.4, 1.1, 1.2]} intensity={0.72} color="#c5d6ff" />
      <directionalLight position={[0.1, 1.8, -2.8]} intensity={0.88} color="#ffdccb" />
      <FittedModel look={look} />
    </Canvas>
  )
}

useGLTF.preload(ASSISTANT_MODEL_URL)
