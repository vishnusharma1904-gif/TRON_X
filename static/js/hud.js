/**
 * TRON-X HUD — Three.js animated background  v2
 * Electrifying particle grid + rotating rings + persona-reactive colors
 * Jarvis = electric cyan | Friday = hot magenta
 */
(function () {
  'use strict';

  const canvas   = document.getElementById('bg-canvas');
  const renderer = new THREE.WebGLRenderer({ canvas, antialias: false, alpha: true });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio, 1.5));
  renderer.setSize(window.innerWidth, window.innerHeight);
  renderer.setClearColor(0x000000, 1);

  const scene  = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(60, window.innerWidth / window.innerHeight, 0.1, 1000);
  camera.position.set(0, 0, 38);

  // ── Fog ──────────────────────────────────────────────────────────────
  scene.fog = new THREE.FogExp2(0x000306, 0.016);

  // ── Particle grid (wave field) ─────────────────────────────────────────
  const COLS = 48, ROWS = 30;
  const particleGeo = new THREE.BufferGeometry();
  const positions   = new Float32Array(COLS * ROWS * 3);
  const baseY       = new Float32Array(COLS * ROWS);

  let idx = 0;
  for (let r = 0; r < ROWS; r++) {
    for (let c = 0; c < COLS; c++) {
      const x = (c / (COLS - 1) - 0.5) * 90;
      const y = (r / (ROWS - 1) - 0.5) * 56;
      positions[idx * 3]     = x;
      positions[idx * 3 + 1] = y;
      positions[idx * 3 + 2] = (Math.random() - 0.5) * 6;
      baseY[idx]              = y;
      idx++;
    }
  }
  particleGeo.setAttribute('position', new THREE.BufferAttribute(positions, 3));

  const particleMat = new THREE.PointsMaterial({
    color: 0x00e5ff, size: 0.22,
    transparent: true, opacity: 0.6, sizeAttenuation: true,
  });
  const particles = new THREE.Points(particleGeo, particleMat);
  scene.add(particles);

  // ── Grid lines ─────────────────────────────────────────────────────────
  const lineMat = new THREE.LineBasicMaterial({ color: 0x001a2e, transparent: true, opacity: 0.32 });

  function makeLine(pts) {
    const g = new THREE.BufferGeometry().setFromPoints(pts);
    return new THREE.Line(g, lineMat);
  }
  for (let r = 0; r < ROWS; r++) {
    const y = (r / (ROWS - 1) - 0.5) * 56;
    scene.add(makeLine([new THREE.Vector3(-45, y, 0), new THREE.Vector3(45, y, 0)]));
  }
  for (let c = 0; c < COLS; c++) {
    const x = (c / (COLS - 1) - 0.5) * 90;
    scene.add(makeLine([new THREE.Vector3(x, -28, 0), new THREE.Vector3(x, 28, 0)]));
  }

  // ── Rotating rings ─────────────────────────────────────────────────────
  const rings = [];
  const ringDefs = [
    { radius: 7,  tube: 0.07, color: 0x00e5ff, speedX: 0.004,  speedY: 0.006,  persona: true  },
    { radius: 12, tube: 0.05, color: 0xff6600, speedX: -0.003, speedY: 0.004,  persona: false },
    { radius: 18, tube: 0.04, color: 0x00e5ff, speedX: 0.002,  speedY: -0.003, persona: true  },
    { radius: 25, tube: 0.03, color: 0x4400cc, speedX: -0.001, speedY: 0.002,  persona: false },
    { radius: 33, tube: 0.02, color: 0x00e5ff, speedX: 0.0005, speedY: -0.001, persona: true  },
  ];
  ringDefs.forEach(def => {
    const geo  = new THREE.TorusGeometry(def.radius, def.tube, 8, 90);
    const mat  = new THREE.MeshBasicMaterial({ color: def.color, transparent: true, opacity: 0.38 });
    const mesh = new THREE.Mesh(geo, mat);
    mesh.rotation.x = Math.PI / 5 + Math.random() * 0.5;
    scene.add(mesh);
    rings.push({ mesh, speedX: def.speedX, speedY: def.speedY, mat, persona: def.persona });
  });

  // ── Central hexagons ──────────────────────────────────────────────────
  const hexMat = new THREE.MeshBasicMaterial({ color: 0x00e5ff, wireframe: true, transparent: true, opacity: 0.65 });
  const hex    = new THREE.Mesh(new THREE.CylinderGeometry(2.8, 2.8, 0.05, 6, 1, true), hexMat);
  hex.rotation.x = Math.PI / 2;
  scene.add(hex);

  const hexInnerMat = new THREE.MeshBasicMaterial({ color: 0xff6600, wireframe: true, transparent: true, opacity: 0.75 });
  const hexInner = new THREE.Mesh(new THREE.CylinderGeometry(1.4, 1.4, 0.05, 6, 1, true), hexInnerMat);
  hexInner.rotation.x = Math.PI / 2;
  scene.add(hexInner);

  const hexOuterMat = new THREE.MeshBasicMaterial({ color: 0x003366, wireframe: true, transparent: true, opacity: 0.28 });
  const hexOuter = new THREE.Mesh(new THREE.CylinderGeometry(5.0, 5.0, 0.05, 6, 1, true), hexOuterMat);
  hexOuter.rotation.x = Math.PI / 2;
  scene.add(hexOuter);

  // ── Ambient floating data dots ─────────────────────────────────────────
  const burstGeo = new THREE.BufferGeometry();
  const bPos = new Float32Array(80 * 3);
  for (let i = 0; i < 80 * 3; i++) bPos[i] = (Math.random() - 0.5) * 100;
  burstGeo.setAttribute('position', new THREE.BufferAttribute(bPos, 3));
  const burstMat = new THREE.PointsMaterial({ color: 0x00ffff, size: 0.45, transparent: true, opacity: 0.75 });
  scene.add(new THREE.Points(burstGeo, burstMat));

  // ── Vertical scan lines ────────────────────────────────────────────────
  for (let i = 0; i < 8; i++) {
    const x = (Math.random() - 0.5) * 80;
    const g = new THREE.BufferGeometry().setFromPoints([
      new THREE.Vector3(x, -35, -3),
      new THREE.Vector3(x,  35, -3),
    ]);
    scene.add(new THREE.Line(g, new THREE.LineBasicMaterial({ color: 0x00e5ff, transparent: true, opacity: 0.08 })));
  }

  // ── Resize handler ─────────────────────────────────────────────────────
  window.addEventListener('resize', () => {
    camera.aspect = window.innerWidth / window.innerHeight;
    camera.updateProjectionMatrix();
    renderer.setSize(window.innerWidth, window.innerHeight);
  });

  // ── Persona color update ───────────────────────────────────────────────
  let lastPersona = 'jarvis';
  function _syncPersonaColors() {
    const p = (document.body && document.body.dataset.persona) || 'jarvis';
    if (p === lastPersona) return;
    lastPersona = p;
    const c = p === 'friday' ? 0xff00cc : 0x00e5ff;
    particleMat.color.setHex(c);
    hexMat.color.setHex(c);
    burstMat.color.setHex(p === 'friday' ? 0xff44ee : 0x00ffff);
    rings.forEach(r => { if (r.persona) r.mat.color.setHex(c); });
  }

  // ── Animation loop ─────────────────────────────────────────────────────
  let t = 0;
  let frameCount = 0;
  function animate() {
    requestAnimationFrame(animate);
    t += 0.007;
    frameCount++;

    // Sync persona colors every 60 frames
    if (frameCount % 60 === 0) _syncPersonaColors();

    // Wave the particle grid
    const pos = particleGeo.attributes.position;
    for (let i = 0; i < COLS * ROWS; i++) {
      const x = pos.getX(i);
      const z = pos.getZ(i);
      pos.setY(i, baseY[i] + Math.sin(x * 0.12 + t) * 0.9 + Math.cos(z * 0.18 + t * 0.7) * 0.45);
    }
    pos.needsUpdate = true;

    // Rotate rings
    rings.forEach(r => {
      r.mesh.rotation.x += r.speedX;
      r.mesh.rotation.y += r.speedY;
    });

    // Pulse central hexagons
    const pulse = 0.82 + 0.18 * Math.sin(t * 2.2);
    hex.scale.set(pulse, pulse, pulse);
    hexInner.rotation.z += 0.013;
    const innerPulse = 0.88 + 0.12 * Math.sin(t * 3.5 + 1);
    hexInner.scale.set(innerPulse, innerPulse, innerPulse);
    hexOuter.rotation.z -= 0.004;

    // Breathe particle opacity
    particleMat.opacity = 0.5 + 0.15 * Math.sin(t * 1.1);

    // Slow camera drift
    camera.position.x = Math.sin(t * 0.09) * 2.5;
    camera.position.y = Math.cos(t * 0.065) * 1.2;
    camera.lookAt(0, 0, 0);

    renderer.render(scene, camera);
  }

  animate();
})();
