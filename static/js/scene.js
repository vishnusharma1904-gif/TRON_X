/**
 * TRON-X Three.js Scene
 * Central holographic orb + orbital rings + particle field
 * State-reactive: colors + scale change based on system state
 */

const Scene = (() => {
  const STATE_COLORS = {
    idle:      { orb: 0x00f5ff, ring: 0x0066ff, emissive: 0x003344 },
    thinking:  { orb: 0xffaa00, ring: 0xff6600, emissive: 0x332200 },
    speaking:  { orb: 0xb44fff, ring: 0x8800ff, emissive: 0x220033 },
    listening: { orb: 0x00ff88, ring: 0x00cc66, emissive: 0x002211 },
    error:     { orb: 0xff4444, ring: 0xff0000, emissive: 0x330000 },
  };

  let renderer, camera, scene;
  let orb, innerOrb, rings = [], particles;
  let orbTargetScale = 1, orbScale = 1;
  let currentColors = STATE_COLORS.idle;
  let targetColors  = STATE_COLORS.idle;
  let voiceAmplitude = 0;
  let time = 0;

  function init(canvasEl) {
    const W = canvasEl.parentElement.clientWidth;
    const H = canvasEl.parentElement.clientHeight;

    renderer = new THREE.WebGLRenderer({ canvas: canvasEl, antialias: true, alpha: true });
    renderer.setSize(W, H);
    renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    renderer.setClearColor(0x000008, 1);

    camera = new THREE.PerspectiveCamera(60, W / H, 0.1, 100);
    camera.position.set(0, 0, 6);

    scene = new THREE.Scene();

    _buildOrb();
    _buildRings();
    _buildParticles();
    _buildGrid();

    window.addEventListener('resize', _onResize);
    _animate();
  }

  function _buildOrb() {
    // Outer wireframe orb
    const geo = new THREE.IcosahedronGeometry(1.6, 2);
    const mat = new THREE.MeshBasicMaterial({
      color: 0x00f5ff, wireframe: true, transparent: true, opacity: 0.55,
    });
    orb = new THREE.Mesh(geo, mat);
    scene.add(orb);

    // Inner solid core (darker tint)
    const igeo = new THREE.IcosahedronGeometry(1.1, 1);
    const imat = new THREE.MeshBasicMaterial({
      color: 0x003344, transparent: true, opacity: 0.5,
    });
    innerOrb = new THREE.Mesh(igeo, imat);
    scene.add(innerOrb);

    // Glow sprite
    const spriteMat = new THREE.SpriteMaterial({
      color: 0x00f5ff, transparent: true, opacity: 0.08,
    });
    const sprite = new THREE.Sprite(spriteMat);
    sprite.scale.set(8, 8, 1);
    sprite.name = 'glow';
    scene.add(sprite);
  }

  function _buildRings() {
    const defs = [
      { r: 2.4, tiltX: Math.PI / 2,    tiltZ: 0,              speed: 0.004 },
      { r: 2.8, tiltX: Math.PI / 4,    tiltZ: Math.PI / 5,    speed: -0.003 },
      { r: 3.2, tiltX: Math.PI / 6,    tiltZ: -Math.PI / 3,   speed: 0.002 },
    ];
    defs.forEach(d => {
      const pts = [];
      for (let i = 0; i <= 128; i++) {
        const a = (i / 128) * Math.PI * 2;
        pts.push(new THREE.Vector3(Math.cos(a) * d.r, 0, Math.sin(a) * d.r));
      }
      const geo  = new THREE.BufferGeometry().setFromPoints(pts);
      const mat  = new THREE.LineBasicMaterial({ color: 0x0066ff, transparent: true, opacity: 0.5 });
      const ring = new THREE.Line(geo, mat);
      ring.rotation.x = d.tiltX;
      ring.rotation.z = d.tiltZ;
      ring.userData.speed = d.speed;
      rings.push(ring);
      scene.add(ring);
    });
  }

  function _buildParticles() {
    const count = 2200;
    const positions = new Float32Array(count * 3);
    for (let i = 0; i < count * 3; i++) {
      positions[i] = (Math.random() - 0.5) * 24;
    }
    const geo = new THREE.BufferGeometry();
    geo.setAttribute('position', new THREE.BufferAttribute(positions, 3));
    const mat = new THREE.PointsMaterial({
      color: 0x00f5ff, size: 0.025, transparent: true, opacity: 0.45,
    });
    particles = new THREE.Points(geo, mat);
    scene.add(particles);
  }

  function _buildGrid() {
    const helper = new THREE.GridHelper(30, 30, 0x001122, 0x001122);
    helper.position.y = -4;
    helper.material.transparent = true;
    helper.material.opacity = 0.4;
    scene.add(helper);
  }

  function _lerpColor(a, b, t) {
    const ca = new THREE.Color(a), cb = new THREE.Color(b);
    return ca.lerp(cb, t).getHex();
  }

  function _animate() {
    requestAnimationFrame(_animate);
    time += 0.01;

    // Smooth state color transitions
    const t = 0.03;
    orb.material.color.lerp(new THREE.Color(targetColors.orb), t);
    innerOrb.material.color.lerp(new THREE.Color(targetColors.emissive), t);
    rings.forEach(r => r.material.color.lerp(new THREE.Color(targetColors.ring), t));

    // Orb rotation + breathing
    orb.rotation.y      += 0.003;
    orb.rotation.x      += 0.001;
    innerOrb.rotation.y -= 0.005;
    innerOrb.rotation.z += 0.002;

    // Voice-reactive scale pulse
    const breathe = 1 + Math.sin(time * 1.2) * 0.015;
    const voicePulse = 1 + voiceAmplitude * 0.3;
    orbScale += (orbTargetScale * breathe * voicePulse - orbScale) * 0.08;
    orb.scale.setScalar(orbScale);
    innerOrb.scale.setScalar(orbScale * 0.95);

    // Update glow
    const glow = scene.getObjectByName('glow');
    if (glow) {
      glow.material.opacity = 0.06 + voiceAmplitude * 0.04 + Math.sin(time * 1.5) * 0.01;
    }

    // Ring orbits
    rings.forEach(r => { r.rotation.z += r.userData.speed; });

    // Slow particle drift
    particles.rotation.y += 0.0003;
    particles.rotation.x += 0.0001;

    renderer.render(scene, camera);
    voiceAmplitude *= 0.85; // decay
  }

  function _onResize() {
    if (!renderer) return;
    const wrap = document.getElementById('canvas-wrap');
    const W = wrap.clientWidth, H = wrap.clientHeight;
    camera.aspect = W / H;
    camera.updateProjectionMatrix();
    renderer.setSize(W, H);
  }

  // ── Public API ────────────────────────────────────────────────────────────

  function setState(state) {
    targetColors  = STATE_COLORS[state] || STATE_COLORS.idle;
    orbTargetScale = state === 'thinking' ? 1.05 : state === 'speaking' ? 1.1 : 1.0;
    const label = { idle:'STANDBY', thinking:'PROCESSING', speaking:'RESPONDING', listening:'LISTENING', error:'ERROR' };
    const el = document.getElementById('ring-text');
    if (el) { el.textContent = label[state] || 'STANDBY'; }
  }

  function setVoiceAmplitude(amp) {
    voiceAmplitude = Math.min(amp, 1);
  }

  return { init, setState, setVoiceAmplitude };
})();
