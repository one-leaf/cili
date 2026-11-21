/*!
 * iso-kit.js — 等轴测结构图工具（普通脚本，全局 IsoKit）
 *
 * 依赖 three.min.js（r149，UMD，与 template.html 同目录）。
 * 设计要点：
 *  - 正交相机 + 方位角/仰角，默认 45°/30°，得到等轴测观感
 *  - 四档面朝向色调：顶面最亮，两个侧面各一档，背光面最暗（按法线量化，不跑光照模型）
 *  - 每个零件自动描边（EdgesGeometry），面材质带 polygonOffset，避免描边被 z-fighting 吃掉
 *  - 主题（light/dark）运行时切换：只换基色，不重建几何
 *
 * 世界坐标：x 向右、y 向上、z 朝向观察者左侧；单位约等于成品图里 1 个绘图像素。
 */
(function (global) {
'use strict';

const T = global.THREE;
if (!T) {
  throw new Error('iso-kit: 未找到 THREE，请先加载 three.min.js');
}

// ── 主题调色 ─────────────────────────────────────────────
// 零件基色四档：lo（底座/次要）、mid（结构）、hi（主体）、lit（唯一强调件）。
const THEMES = {
  light: {
    bg: '#efece3', line: '#6d675b',
    lo: '#d9d3c6', mid: '#cdc5b5', hi: '#b8ae9a', lit: '#22221f',
  },
  dark: {
    bg: '#141619', line: '#05070a',
    lo: '#2c3138', mid: '#3a4047', hi: '#4a525b', lit: '#eef1f3',
  },
};

let themeName = 'light';
const parts = [];          // 已登记零件（切换主题时重设颜色）
const movers = [];         // 有平缓位移目标的对象

function theme() { return THEMES[themeName]; }

function resolveColor(key) {
  const t = theme();
  return t[key] !== undefined ? t[key] : key;
}

// ── 材质 ─────────────────────────────────────────────────
// 面朝向 → 亮度档位；顶面 1.0，亮侧 0.88，次暗侧 0.74，背光 0.62。
function shadedMaterial(colorKey) {
  return new T.ShaderMaterial({
    uniforms: {
      uColor: { value: new T.Color(resolveColor(colorKey)) },
      uLight: { value: new T.Vector3(-0.45, 0.85, 0.30).normalize() },
    },
    vertexShader: `
      varying vec3 vN;
      void main() {
        vN = normalize(mat3(modelMatrix) * normal);
        gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0);
      }`,
    fragmentShader: `
      uniform vec3 uColor;
      uniform vec3 uLight;
      varying vec3 vN;
      void main() {
        float d = max(dot(normalize(vN), uLight), 0.0);
        float level = d > 0.80 ? 1.0 : d > 0.50 ? 0.88 : d > 0.22 ? 0.74 : 0.62;
        gl_FragColor = vec4(uColor * level, 1.0);
      }`,
    polygonOffset: true,
    polygonOffsetFactor: 1,
    polygonOffsetUnits: 1,
  });
}

// ── 零件构造 ─────────────────────────────────────────────
// opts: { color: lo|mid|hi|lit|css 颜色, pos:[x,y,z], rot:[rx,ry,rz],
//         edges:false 关闭描边, edgeAngle: 描边阈值角度（默认 25°） }
function part(geometry, opts) {
  opts = opts || {};
  const key = opts.color || 'mid';
  const mesh = new T.Mesh(geometry, shadedMaterial(key));
  if (opts.pos) mesh.position.fromArray(opts.pos);
  if (opts.rot) mesh.rotation.set(opts.rot[0] || 0, opts.rot[1] || 0, opts.rot[2] || 0);
  if (opts.scale) {
    if (Array.isArray(opts.scale)) mesh.scale.fromArray(opts.scale);
    else mesh.scale.setScalar(opts.scale);
  }
  if (opts.edges !== false) {
    const edges = new T.LineSegments(
      new T.EdgesGeometry(geometry, opts.edgeAngle !== undefined ? opts.edgeAngle : 25),
      new T.LineBasicMaterial({ color: theme().line })
    );
    edges.userData.isEdge = true;
    mesh.add(edges);
  }
  mesh.userData.baseKey = key;
  parts.push(mesh);
  return mesh;
}

function box(w, h, d, opts) {
  return part(new T.BoxGeometry(w, h, d), opts);
}

function cylinder(rTop, rBottom, h, opts) {
  opts = opts || {};
  return part(new T.CylinderGeometry(rTop, rBottom, h, opts.segments || 24), opts);
}

// profile: [[半径, 高度], ...]，用 LatheGeometry 车出回转体（法兰、钟形罩等）
function lathe(profile, opts) {
  opts = opts || {};
  const points = profile.map(p => new T.Vector2(p[0], p[1]));
  return part(new T.LatheGeometry(points, opts.segments || 32), opts);
}

// points: [[x,y,z], ...]；管线/线缆
function tube(points, radius, opts) {
  opts = opts || {};
  const curve = new T.CatmullRomCurve3(points.map(p => new T.Vector3().fromArray(p)));
  return part(new T.TubeGeometry(curve, opts.tubularSegments || 48, radius,
    opts.radialSegments || 8, false), Object.assign({}, opts, { edges: false }));
}

function sphere(r, opts) {
  opts = opts || {};
  const seg = opts.segments || 20;
  return part(new T.SphereGeometry(r, seg, Math.max(8, seg >> 1)), opts);
}

// ── 运动工具 ─────────────────────────────────────────────
// 指数阻尼：halfLife 秒内走完一半距离（与帧率无关）
function damp(current, target, halfLife, dt) {
  const a = 1 - Math.pow(0.5, dt / Math.max(halfLife, 1e-4));
  return current + (target - current) * a;
}

// ── 场景 ─────────────────────────────────────────────────
function figure(opts) {
  opts = opts || {};
  const canvas = opts.canvas;
  if (!canvas) throw new Error('IsoKit.figure 需要 canvas');

  const renderer = new T.WebGLRenderer({ canvas, antialias: true, preserveDrawingBuffer: true });
  renderer.setPixelRatio(Math.min(global.devicePixelRatio || 1, 2.5));

  const scene = new T.Scene();
  scene.background = new T.Color(theme().bg);

  const camera = new T.OrthographicCamera(-100, 100, 100, -100, 0.1, 5000);
  camera.up.set(0, 1, 0);

  const view = {
    azimuth: opts.azimuth !== undefined ? opts.azimuth : 45,
    elevation: opts.elevation !== undefined ? opts.elevation : 30,
    target: new T.Vector3(),
    fit: null,               // { w, h } 相机空间内的取景尺寸
  };
  const callbacks = [];
  let lastMs = 0;

  function direction() {
    const az = T.MathUtils.degToRad(view.azimuth);
    const el = T.MathUtils.degToRad(view.elevation);
    return new T.Vector3(Math.cos(el) * Math.cos(az), Math.sin(el), Math.cos(el) * Math.sin(az));
  }

  function place() {
    camera.position.copy(view.target).addScaledVector(direction(), 1200);
    camera.lookAt(view.target);
    camera.updateMatrixWorld();
  }

  function applyFrustum() {
    if (!view.fit) return;
    const cw = canvas.clientWidth || 800;
    const ch = canvas.clientHeight || 600;
    const aspect = cw / ch;
    let halfW = view.fit.w / 2;
    let halfH = view.fit.h / 2;
    if (halfW / halfH < aspect) halfW = halfH * aspect;
    else halfH = halfW / aspect;
    camera.left = -halfW; camera.right = halfW;
    camera.top = halfH; camera.bottom = -halfH;
    camera.updateProjectionMatrix();
  }

  function resize() {
    const cw = canvas.clientWidth || 800;
    const ch = canvas.clientHeight || 600;
    renderer.setSize(cw, ch, false);
    applyFrustum();
    renderer.render(scene, camera);
  }

  // 把场景内容整体装进取景框（调用前确保所有零件已 add 进 scene）
  function fit(margin) {
    margin = margin || 1.18;
    place();
    const bounds = new T.Box3().setFromObject(scene);
    if (bounds.isEmpty()) return;
    view.target.copy(bounds.getCenter(new T.Vector3()));
    place();
    const inv = camera.matrixWorldInverse;
    let minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity;
    const v = new T.Vector3();
    for (let i = 0; i < 8; i++) {
      v.set(
        i & 1 ? bounds.max.x : bounds.min.x,
        i & 2 ? bounds.max.y : bounds.min.y,
        i & 4 ? bounds.max.z : bounds.min.z
      ).applyMatrix4(inv);
      minX = Math.min(minX, v.x); maxX = Math.max(maxX, v.x);
      minY = Math.min(minY, v.y); maxY = Math.max(maxY, v.y);
    }
    view.fit = { w: Math.max(maxX - minX, 1) * margin, h: Math.max(maxY - minY, 1) * margin };
    resize();
  }

  function tick(nowMs) {
    const dt = lastMs ? Math.min((nowMs - lastMs) / 1000, 0.1) : 1 / 60;
    lastMs = nowMs;
    for (const m of movers) {
      const u = m.userData.move;
      u.pos.x = damp(u.pos.x, u.to.x, u.halfLife, dt);
      u.pos.y = damp(u.pos.y, u.to.y, u.halfLife, dt);
      u.pos.z = damp(u.pos.z, u.to.z, u.halfLife, dt);
      m.position.copy(u.pos);
      if (u.pos.distanceToSquared(u.to) < 0.01) {
        m.position.copy(u.to);
        delete m.userData.move;
        movers.splice(movers.indexOf(m), 1);
        break;
      }
    }
    for (const cb of callbacks) cb(dt, nowMs / 1000);
    renderer.render(scene, camera);
  }

  function start() { lastMs = 0; renderer.setAnimationLoop(tick); }
  function stop() { renderer.setAnimationLoop(null); }

  // 平缓移到目标位置（offset 为相对当前位置的增量；连续调用会改目标，不叠加）
  function moveBy(obj, offset, halfLife) {
    const u = obj.userData.move || { pos: obj.position.clone(), to: obj.position.clone() };
    u.to = obj.position.clone().add(new T.Vector3().fromArray(offset));
    u.halfLife = halfLife || 0.18;
    obj.userData.move = u;
    if (movers.indexOf(obj) === -1) movers.push(obj);
  }

  // 拖动旋转 + 区分点击（moved 阈值，避免拖动结束误触发）
  function enableOrbit(tapHandler) {
    let dragging = false, px = 0, py = 0, moved = 0;
    canvas.style.touchAction = 'none';
    canvas.addEventListener('pointerdown', (e) => {
      dragging = true; moved = 0; px = e.clientX; py = e.clientY;
      canvas.setPointerCapture(e.pointerId);
    });
    canvas.addEventListener('pointermove', (e) => {
      if (!dragging) return;
      const dx = e.clientX - px, dy = e.clientY - py;
      px = e.clientX; py = e.clientY;
      moved += Math.abs(dx) + Math.abs(dy);
      view.azimuth += dx * 0.4;
      view.elevation = T.MathUtils.clamp(view.elevation + dy * 0.3, 12, 78);
      place();
      renderer.render(scene, camera);
    });
    canvas.addEventListener('pointerup', (e) => {
      dragging = false;
      if (canvas.releasePointerCapture) canvas.releasePointerCapture(e.pointerId);
    });
    if (tapHandler) {
      canvas.addEventListener('click', (e) => { if (moved < 6) tapHandler(e); });
    }
  }

  // 指针所在的最外层零件（挂在 scene 下、带 baseKey 的对象）
  const raycaster = new T.Raycaster();
  const pointer = new T.Vector2();
  function pick(event) {
    const rect = canvas.getBoundingClientRect();
    pointer.set(
      ((event.clientX - rect.left) / rect.width) * 2 - 1,
      -((event.clientY - rect.top) / rect.height) * 2 + 1
    );
    raycaster.setFromCamera(pointer, camera);
    const hits = raycaster.intersectObjects(scene.children, true);
    for (const hit of hits) {
      let o = hit.object;
      while (o && o.parent !== scene) o = o.parent;
      if (o && o.userData.baseKey) return o;
    }
    return null;
  }

  function setTheme(name) {
    themeName = name;
    scene.background.set(theme().bg);
    for (const mesh of parts) {
      mesh.material.uniforms.uColor.value.set(resolveColor(mesh.userData.baseKey));
      for (const child of mesh.children) {
        if (child.userData.isEdge) child.material.color.set(theme().line);
      }
    }
    if (global.document) global.document.documentElement.dataset.theme = name;
    renderer.render(scene, camera);
  }

  // 以 scale 倍像素密度渲染一帧并导出 PNG dataURL（用完恢复原密度）
  function capture(scale) {
    const prev = renderer.getPixelRatio();
    renderer.setPixelRatio(scale || 2);
    renderer.render(scene, camera);
    const url = canvas.toDataURL('image/png');
    renderer.setPixelRatio(prev);
    renderer.render(scene, camera);
    return url;
  }

  function setReadout(text) {
    const el = global.document && global.document.getElementById('iso-readout');
    if (el) el.textContent = text;
  }

  function setHint(text) {
    const el = global.document && global.document.getElementById('iso-hint');
    if (el) el.textContent = text;
  }

  const prefersReducedMotion = !!(global.matchMedia &&
    global.matchMedia('(prefers-reduced-motion: reduce)').matches);

  const fig = {
    scene, camera, renderer, canvas,
    fit, resize, start, stop, place,
    onFrame(cb) { callbacks.push(cb); return fig; },
    moveBy, enableOrbit, pick, setTheme, capture, setReadout, setHint,
    prefersReducedMotion,
    get azimuth() { return view.azimuth; },
    get elevation() { return view.elevation; },
  };

  global.addEventListener && global.addEventListener('resize', resize);
  start();
  return fig;
}

global.IsoKit = {
  THEMES,
  figure,
  box, cylinder, lathe, tube, sphere, part,
  damp,
  get theme() { return theme(); },
  get themeName() { return themeName; },
  // 初始主题（要在 figure() 之前调用；运行时切换用 fig.setTheme）
  setTheme(name) { themeName = name; },
};

})(window);
