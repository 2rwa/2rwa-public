const canvas = document.querySelector('#gl');
const zoomSlider = document.querySelector('#zoom');
const zoomValue = document.querySelector('#zoomValue');
const statusEl = document.querySelector('#status');
const metricsEl = document.querySelector('#metrics');
const clockEl = document.querySelector('#clock');
const panel = document.querySelector('#panel');
const panelToggle = document.querySelector('#panelToggle');

const gl = canvas.getContext('webgl2', { antialias: true, alpha: false });
if (!gl) throw new Error('WebGL2 is required');

const VS = [
  '#version 300 es',
  'precision highp float;',
  'layout(location=0) in vec3 positionWorld;',
  'uniform mat4 modelViewProjection;',
  'uniform mat4 globeRotation;',
  'out vec3 normalView;',
  'void main(){',
  '  vec4 rotated = globeRotation * vec4(positionWorld, 1.0);',
  '  normalView = normalize(rotated.xyz);',
  '  gl_Position = modelViewProjection * vec4(positionWorld, 1.0);',
  '}'
].join('\n');

const FS = [
  '#version 300 es',
  'precision highp float;',
  'in vec3 normalView;',
  'uniform vec3 baseColor;',
  'uniform vec3 sunDirectionView;',
  'uniform float frontOnly;',
  'out vec4 outColor;',
  'void main(){',
  '  vec3 surfaceNormal = normalize(normalView);',
  '  if (frontOnly > 0.5 && surfaceNormal.z <= 0.0) discard;',
  '  float daylight = max(dot(surfaceNormal, normalize(sunDirectionView)), 0.0);',
  '  float lightLevel = 0.12 + 0.88 * daylight;',
  '  outColor = vec4(baseColor * lightLevel, 1.0);',
  '}'
].join('\n');

function compileShader(type, source) {
  const shader = gl.createShader(type);
  gl.shaderSource(shader, source);
  gl.compileShader(shader);
  if (!gl.getShaderParameter(shader, gl.COMPILE_STATUS)) {
    throw new Error(gl.getShaderInfoLog(shader) || 'shader compile failed');
  }
  return shader;
}

const program = gl.createProgram();
gl.attachShader(program, compileShader(gl.VERTEX_SHADER, VS));
gl.attachShader(program, compileShader(gl.FRAGMENT_SHADER, FS));
gl.linkProgram(program);
if (!gl.getProgramParameter(program, gl.LINK_STATUS)) {
  throw new Error(gl.getProgramInfoLog(program) || 'program link failed');
}

const uMVP = gl.getUniformLocation(program, 'modelViewProjection');
const uRotation = gl.getUniformLocation(program, 'globeRotation');
const uColor = gl.getUniformLocation(program, 'baseColor');
const uSun = gl.getUniformLocation(program, 'sunDirectionView');
const uFrontOnly = gl.getUniformLocation(program, 'frontOnly');

function m4mul(a, b) {
  const out = new Float32Array(16);
  for (let column = 0; column < 4; column++) {
    for (let row = 0; row < 4; row++) {
      let value = 0;
      for (let k = 0; k < 4; k++) value += a[k * 4 + row] * b[column * 4 + k];
      out[column * 4 + row] = value;
    }
  }
  return out;
}

function rx(angle) {
  const c = Math.cos(angle);
  const s = Math.sin(angle);
  return new Float32Array([1,0,0,0, 0,c,s,0, 0,-s,c,0, 0,0,0,1]);
}

function ry(angle) {
  const c = Math.cos(angle);
  const s = Math.sin(angle);
  return new Float32Array([c,0,-s,0, 0,1,0,0, s,0,c,0, 0,0,0,1]);
}

function perspective(fovy, aspect, near, far) {
  const q = 1 / Math.tan(fovy / 2);
  const out = new Float32Array(16);
  out[0] = q / aspect;
  out[5] = q;
  out[10] = (far + near) / (near - far);
  out[11] = -1;
  out[14] = 2 * far * near / (near - far);
  return out;
}

function viewTranslateZ(distance) {
  return new Float32Array([1,0,0,0, 0,1,0,0, 0,0,1,0, 0,0,-distance,1]);
}

function transformDirection(matrix, vector) {
  return [
    matrix[0] * vector[0] + matrix[4] * vector[1] + matrix[8] * vector[2],
    matrix[1] * vector[0] + matrix[5] * vector[1] + matrix[9] * vector[2],
    matrix[2] * vector[0] + matrix[6] * vector[1] + matrix[10] * vector[2]
  ];
}

function sphereMesh(latSteps = 48, lonSteps = 96, radius = 1) {
  const positions = [];
  const indices = [];
  for (let y = 0; y <= latSteps; y++) {
    const lat = -Math.PI / 2 + Math.PI * y / latSteps;
    const cosLat = Math.cos(lat);
    const sinLat = Math.sin(lat);
    for (let x = 0; x <= lonSteps; x++) {
      const lon = -Math.PI + 2 * Math.PI * x / lonSteps;
      positions.push(
        radius * cosLat * Math.sin(lon),
        radius * sinLat,
        radius * cosLat * Math.cos(lon)
      );
    }
  }
  for (let y = 0; y < latSteps; y++) {
    for (let x = 0; x < lonSteps; x++) {
      const a = y * (lonSteps + 1) + x;
      const b = a + lonSteps + 1;
      indices.push(a, a + 1, b, a + 1, b + 1, b);
    }
  }
  return {
    positions: new Float32Array(positions),
    indices: new Uint32Array(indices)
  };
}

function gpuMesh(mesh) {
  const vertexCount = mesh.positions.length / 3;
  let indexData = mesh.indices;
  let indexType = gl.UNSIGNED_INT;
  if (vertexCount <= 65535) {
    indexData = new Uint16Array(mesh.indices);
    indexType = gl.UNSIGNED_SHORT;
  }

  const vao = gl.createVertexArray();
  const vertexBuffer = gl.createBuffer();
  const indexBuffer = gl.createBuffer();
  gl.bindVertexArray(vao);
  gl.bindBuffer(gl.ARRAY_BUFFER, vertexBuffer);
  gl.bufferData(gl.ARRAY_BUFFER, mesh.positions, gl.STATIC_DRAW);
  gl.enableVertexAttribArray(0);
  gl.vertexAttribPointer(0, 3, gl.FLOAT, false, 0, 0);
  gl.bindBuffer(gl.ELEMENT_ARRAY_BUFFER, indexBuffer);
  gl.bufferData(gl.ELEMENT_ARRAY_BUFFER, indexData, gl.STATIC_DRAW);
  gl.bindVertexArray(null);

  return {
    vao,
    vertexBuffer,
    indexBuffer,
    count: indexData.length,
    indexType,
    triangles: indexData.length / 3,
    gpuBytes: mesh.positions.byteLength + indexData.byteLength
  };
}

function destroyGpuMesh(mesh) {
  if (!mesh) return;
  gl.deleteBuffer(mesh.vertexBuffer);
  gl.deleteBuffer(mesh.indexBuffer);
  gl.deleteVertexArray(mesh.vao);
}

const ocean = gpuMesh(sphereMesh(180, 360));

let manifest = null;
let centerLon = 139.7;
let centerLat = 35.7;
let zoom = 1;
let currentLod = 0;
let reloadToken = 0;
let refreshTimer = 0;
let qaLighting = false;

const tileCache = new Map();
let cacheGpuBytes = 0;
let accessSequence = 0;
let desiredPaths = new Set();
let activePaths = new Set();
let activeMeshes = [];

const loadQueue = [];
let activeLoads = 0;

const coarsePointer = matchMedia('(pointer: coarse)').matches;
const reportedDeviceMemory = Number(navigator.deviceMemory || 8);
const LOAD_CONCURRENCY = reportedDeviceMemory <= 4 ? 3 : (coarsePointer ? 4 : 8);
let cacheBudgetMiB = coarsePointer ? 64 : 192;
if (reportedDeviceMemory <= 4) cacheBudgetMiB = Math.min(cacheBudgetMiB, 48);
if (!coarsePointer && reportedDeviceMemory >= 16) cacheBudgetMiB = 256;
const CACHE_BUDGET_BYTES = cacheBudgetMiB * 1024 * 1024;

let statusMessage = 'manifest loading…';
let fps = 0;
let fpsFrames = 0;
let fpsWindowStart = performance.now();

function formatMiB(bytes) {
  return (bytes / (1024 * 1024)).toFixed(bytes < 10 * 1024 * 1024 ? 2 : 1);
}

function setStatus(message) {
  statusMessage = message;
  updateMetrics();
}

function updateMetrics() {
  if (statusEl) statusEl.textContent = statusMessage;
  if (!metricsEl) return;
  let activeTriangles = 0;
  let activeGpuBytes = 0;
  for (const mesh of activeMeshes) {
    activeTriangles += mesh.triangles;
    activeGpuBytes += mesh.gpuBytes;
  }
  metricsEl.textContent =
    'FPS ' + fps.toFixed(0) +
    ' · active ' + activeMeshes.length + ' tile / ' + activeTriangles.toLocaleString() + ' tri' +
    ' · GPU ' + formatMiB(activeGpuBytes) + ' MiB' +
    ' · cache ' + formatMiB(cacheGpuBytes) + '/' + cacheBudgetMiB + ' MiB' +
    ' · fetch ' + activeLoads + '+' + loadQueue.length;
}

function lonLatToXYZ(lon, lat, radius = 1.0005) {
  const lonRad = lon * Math.PI / 180;
  const latRad = lat * Math.PI / 180;
  const c = Math.cos(latRad);
  return [
    radius * c * Math.sin(lonRad),
    radius * Math.sin(latRad),
    radius * c * Math.cos(lonRad)
  ];
}

function angularDistanceDeg(lonA, latA, lonB, latB) {
  const a = latA * Math.PI / 180;
  const b = latB * Math.PI / 180;
  const deltaLon = (lonB - lonA) * Math.PI / 180;
  const cosine =
    Math.sin(a) * Math.sin(b) +
    Math.cos(a) * Math.cos(b) * Math.cos(deltaLon);
  return Math.acos(Math.max(-1, Math.min(1, cosine))) * 180 / Math.PI;
}

function prepareTileMetadata(tile) {
  if (tile.alwaysVisible) {
    tile._centerLon = 0;
    tile._centerLat = 0;
    tile._angularRadius = 180;
    return;
  }
  const bounds = tile.bounds;
  tile._centerLon = (bounds[0] + bounds[2]) / 2;
  tile._centerLat = (bounds[1] + bounds[3]) / 2;
  tile._angularRadius = Math.max(
    angularDistanceDeg(tile._centerLon, tile._centerLat, bounds[0], bounds[1]),
    angularDistanceDeg(tile._centerLon, tile._centerLat, bounds[0], bounds[3]),
    angularDistanceDeg(tile._centerLon, tile._centerLat, bounds[2], bounds[1]),
    angularDistanceDeg(tile._centerLon, tile._centerLat, bounds[2], bounds[3])
  );
}

function deliveryTilesForLod(data, lod) {
  const deliveryLods = data.delivery && data.delivery.lods;
  const bundled = deliveryLods && deliveryLods[String(lod)];
  if (Array.isArray(bundled) && bundled.length) return bundled;
  return data.tiles.filter(tile => tile.lod === lod);
}

function isDeliveryBundleMode(lod = currentLod) {
  if (!manifest || !manifest.delivery || !manifest.delivery.lods) return false;
  const bundled = manifest.delivery.lods[String(lod)];
  return Array.isArray(bundled) && bundled.length > 0;
}

function prepareManifest(data) {
  for (const tile of data.tiles) prepareTileMetadata(tile);
  const deliveryLods = data.delivery && data.delivery.lods;
  if (deliveryLods) {
    for (const descriptors of Object.values(deliveryLods)) {
      for (const tile of descriptors) prepareTileMetadata(tile);
    }
  }
  return data;
}

function viewSurfaceRadiusDeg() {
  const distance = 1 + 2.15 / zoom;
  const fovy = 45 * Math.PI / 180;
  const width = Math.max(1, canvas.width || innerWidth);
  const height = Math.max(1, canvas.height || innerHeight);
  const aspect = width / height;
  const halfVertical = fovy / 2;
  const halfHorizontal = Math.atan(Math.tan(halfVertical) * aspect);
  const cornerRay = Math.atan(Math.hypot(Math.tan(halfVertical), Math.tan(halfHorizontal)));
  const tangentRay = Math.asin(Math.min(1, 1 / distance));

  if (cornerRay >= tangentRay) {
    return Math.acos(1 / distance) * 180 / Math.PI;
  }

  const surfaceAngle =
    Math.asin(Math.min(1, distance * Math.sin(cornerRay))) - cornerRay;
  return surfaceAngle * 180 / Math.PI;
}

const LOD_UP = [1.55, 2.8, 4.7, 7.2];
const LOD_DOWN = [1.35, 2.5, 4.2, 6.6];

function pickLodWithHysteresis() {
  if (!manifest) return 0;
  let lod = Math.max(0, Math.min(currentLod, manifest.lods.length - 1));
  while (lod < manifest.lods.length - 1 && zoom >= LOD_UP[lod]) lod++;
  while (lod > 0 && zoom < LOD_DOWN[lod - 1]) lod--;
  return lod;
}

function selectVisibleTiles() {
  const radius = viewSurfaceRadiusDeg();
  const marginDeg = 1.5;
  const selected = [];
  const candidates = deliveryTilesForLod(manifest, currentLod);
  for (const tile of candidates) {
    if (tile.alwaysVisible) {
      selected.push({ tile, distance: 0 });
      continue;
    }
    const distance = angularDistanceDeg(centerLon, centerLat, tile._centerLon, tile._centerLat);
    if (distance <= radius + tile._angularRadius + marginDeg) {
      selected.push({ tile, distance });
    }
  }
  selected.sort((a, b) => a.distance - b.distance || a.tile.path.localeCompare(b.tile.path));
  return selected;
}

async function decodeAndUploadTile(tile) {
  const response = await fetch('./data/' + tile.path);
  if (!response.ok) throw new Error(tile.path + ': HTTP ' + response.status);
  const buffer = await response.arrayBuffer();
  if (tile.bytes && buffer.byteLength !== tile.bytes) {
    throw new Error(tile.path + ': size mismatch ' + buffer.byteLength + ' != ' + tile.bytes);
  }

  const magic = String.fromCharCode(...new Uint8Array(buffer, 0, 4));
  if (magic !== 'GVB1') throw new Error('bad GVB1 ' + tile.path);

  const view = new DataView(buffer);
  const vertexCount = view.getUint32(4, true);
  const indexCount = view.getUint32(8, true);
  const expectedBytes = 12 + vertexCount * 4 + indexCount * 4;
  if (expectedBytes !== buffer.byteLength) {
    throw new Error(tile.path + ': malformed GVB1 length');
  }

  const quantized = new Uint16Array(buffer, 12, vertexCount * 2);
  const indices = new Uint32Array(buffer, 12 + vertexCount * 4, indexCount);
  const bounds = tile.bounds;
  const positions = new Float32Array(vertexCount * 3);

  for (let n = 0; n < vertexCount; n++) {
    const lon = bounds[0] + (quantized[n * 2] / 65535) * (bounds[2] - bounds[0]);
    const lat = bounds[1] + (quantized[n * 2 + 1] / 65535) * (bounds[3] - bounds[1]);
    const xyz = lonLatToXYZ(lon, lat);
    positions[n * 3] = xyz[0];
    positions[n * 3 + 1] = xyz[1];
    positions[n * 3 + 2] = xyz[2];
  }

  return gpuMesh({ positions, indices });
}

function pumpLoadQueue() {
  loadQueue.sort((a, b) => a.entry.priority - b.entry.priority);
  while (activeLoads < LOAD_CONCURRENCY && loadQueue.length) {
    const job = loadQueue.shift();
    const entry = job.entry;
    const path = job.tile.path;

    if (!desiredPaths.has(path)) {
      if (tileCache.get(path) === entry) tileCache.delete(path);
      entry.promise = null;
      job.resolve(null);
      continue;
    }

    activeLoads++;
    decodeAndUploadTile(job.tile)
      .then(mesh => {
        if (tileCache.get(path) !== entry) {
          destroyGpuMesh(mesh);
          return null;
        }
        entry.mesh = mesh;
        entry.promise = null;
        entry.gpuBytes = mesh.gpuBytes;
        entry.lastUsed = ++accessSequence;
        cacheGpuBytes += mesh.gpuBytes;
        return mesh;
      })
      .catch(error => {
        if (tileCache.get(path) === entry) tileCache.delete(path);
        entry.promise = null;
        throw error;
      })
      .then(job.resolve, job.reject)
      .finally(() => {
        activeLoads--;
        pruneCache();
        updateMetrics();
        pumpLoadQueue();
      });
  }
  updateMetrics();
}

function loadTile(tile, priority) {
  let entry = tileCache.get(tile.path);
  if (entry) {
    entry.priority = Math.min(entry.priority, priority);
    entry.lastUsed = ++accessSequence;
    if (entry.mesh) return Promise.resolve(entry.mesh);
    if (entry.promise) return entry.promise;
  }

  entry = {
    tile,
    mesh: null,
    promise: null,
    gpuBytes: 0,
    lastUsed: ++accessSequence,
    priority
  };

  entry.promise = new Promise((resolve, reject) => {
    loadQueue.push({ tile, entry, resolve, reject });
  });
  tileCache.set(tile.path, entry);
  pumpLoadQueue();
  return entry.promise;
}

function pruneCache(force = false) {
  const protectedPaths = new Set([...desiredPaths, ...activePaths]);
  const candidates = [];
  for (const [path, entry] of tileCache) {
    if (!entry.mesh || protectedPaths.has(path)) continue;
    candidates.push([path, entry]);
  }
  candidates.sort((a, b) => a[1].lastUsed - b[1].lastUsed);

  for (const [path, entry] of candidates) {
    if (!force && cacheGpuBytes <= CACHE_BUDGET_BYTES) break;
    destroyGpuMesh(entry.mesh);
    cacheGpuBytes -= entry.gpuBytes;
    tileCache.delete(path);
  }
  updateMetrics();
}

async function refreshTiles() {
  if (!manifest) return;

  currentLod = pickLodWithHysteresis();
  const selected = selectVisibleTiles();
  desiredPaths = new Set(selected.map(item => item.tile.path));
  const token = ++reloadToken;
  pumpLoadQueue();

  const lodInfo = manifest.lods[currentLod];
  const resolutionName = lodInfo ? lodInfo.name : 'unknown';
  const deliveryLabel = isDeliveryBundleMode() ? ' · bundle' : ' · canonical';
  setStatus(
    'LOD' + currentLod + ' ' + resolutionName + deliveryLabel +
    ' · ' + selected.length + ' visible tile(s) loading…'
  );

  const loads = selected.map(item =>
    loadTile(item.tile, item.distance)
      .then(mesh => ({ ok: true, mesh, tile: item.tile }))
      .catch(error => ({ ok: false, error, tile: item.tile }))
  );

  const results = await Promise.all(loads);
  if (token !== reloadToken) return;

  const failures = results.filter(result => !result.ok);
  const nextMeshes = [];
  const nextPaths = new Set();
  for (const item of selected) {
    const entry = tileCache.get(item.tile.path);
    if (!entry || !entry.mesh) continue;
    entry.lastUsed = ++accessSequence;
    nextMeshes.push(entry.mesh);
    nextPaths.add(item.tile.path);
  }

  activeMeshes = nextMeshes;
  activePaths = nextPaths;
  pruneCache();

  if (failures.length) {
    setStatus(
      'LOD' + currentLod + ' · ' + nextMeshes.length + '/' + selected.length +
      ' tile(s) · ' + failures.length + ' load error(s)'
    );
  } else {
    setStatus(
      'LOD' + currentLod + ' ' + resolutionName + deliveryLabel +
      ' · ' + selected.length + ' tile(s) · ' + manifest.source.name
    );
  }
}

function scheduleRefresh(delay = 90) {
  clearTimeout(refreshTimer);
  refreshTimer = setTimeout(() => {
    refreshTiles().catch(error => setStatus('tile refresh error: ' + error.message));
  }, delay);
}

function solarDirection(date) {
  const millisecondsPerDay = 86400000;
  const start = Date.UTC(date.getUTCFullYear(), 0, 0);
  const day = (date.getTime() - start) / millisecondsPerDay;
  const hour =
    date.getUTCHours() +
    date.getUTCMinutes() / 60 +
    date.getUTCSeconds() / 3600;
  const gamma = 2 * Math.PI / 365 * (day - 1 + (hour - 12) / 24);
  const declination =
    0.006918 -
    0.399912 * Math.cos(gamma) +
    0.070257 * Math.sin(gamma) -
    0.006758 * Math.cos(2 * gamma) +
    0.000907 * Math.sin(2 * gamma) -
    0.002697 * Math.cos(3 * gamma) +
    0.00148 * Math.sin(3 * gamma);
  const equationOfTime =
    229.18 * (
      0.000075 +
      0.001868 * Math.cos(gamma) -
      0.032077 * Math.sin(gamma) -
      0.014615 * Math.cos(2 * gamma) -
      0.040849 * Math.sin(2 * gamma)
    );
  const subsolarLon = -(hour * 60 + equationOfTime - 720) / 4;
  const cosDeclination = Math.cos(declination);
  const lonRad = subsolarLon * Math.PI / 180;
  return [
    cosDeclination * Math.sin(lonRad),
    Math.sin(declination),
    cosDeclination * Math.cos(lonRad)
  ];
}

function resize() {
  const dpr = Math.min(devicePixelRatio || 1, coarsePointer ? 1.5 : 2);
  const width = Math.max(1, Math.floor(innerWidth * dpr));
  const height = Math.max(1, Math.floor(innerHeight * dpr));
  if (canvas.width !== width || canvas.height !== height) {
    canvas.width = width;
    canvas.height = height;
    gl.viewport(0, 0, width, height);
    scheduleRefresh(120);
  }
}

function drawMesh(mesh, color, frontOnly = false) {
  gl.bindVertexArray(mesh.vao);
  gl.uniform3fv(uColor, color);
  gl.uniform1f(uFrontOnly, frontOnly ? 1 : 0);
  gl.drawElements(gl.TRIANGLES, mesh.count, mesh.indexType, 0);
}

function render(timestamp) {
  resize();

  fpsFrames++;
  if (timestamp - fpsWindowStart >= 1000) {
    fps = fpsFrames * 1000 / (timestamp - fpsWindowStart);
    fpsFrames = 0;
    fpsWindowStart = timestamp;
    updateMetrics();
  }

  gl.enable(gl.DEPTH_TEST);
  gl.clearColor(0.015, 0.022, 0.026, 1);
  gl.clear(gl.COLOR_BUFFER_BIT | gl.DEPTH_BUFFER_BIT);

  const rotation = m4mul(
    rx(centerLat * Math.PI / 180),
    ry(-centerLon * Math.PI / 180)
  );
  const distance = 1 + 2.15 / zoom;
  const projection = perspective(
    45 * Math.PI / 180,
    canvas.width / canvas.height,
    Math.max(0.002, distance - 1.05),
    20
  );
  const view = viewTranslateZ(distance);
  const mvp = m4mul(projection, m4mul(view, rotation));
  const sunView = qaLighting
    ? [0, 0, 1]
    : transformDirection(rotation, solarDirection(new Date()));

  gl.useProgram(program);
  gl.uniformMatrix4fv(uMVP, false, mvp);
  gl.uniformMatrix4fv(uRotation, false, rotation);
  gl.uniform3fv(uSun, sunView);

  gl.enable(gl.CULL_FACE);
  gl.cullFace(gl.BACK);
  drawMesh(ocean, [0.055, 0.16, 0.22], false);

  // Land triangles are planar chords between points on the sphere.  Long
  // triangles can dip behind the ocean depth surface.  Render only the
  // front-hemisphere land fragments after the ocean so those chords cannot
  // punch triangular holes through continental interiors.
  gl.disable(gl.CULL_FACE);
  gl.disable(gl.DEPTH_TEST);
  for (const mesh of activeMeshes) drawMesh(mesh, [0.43, 0.48, 0.34], true);
  gl.enable(gl.DEPTH_TEST);

  requestAnimationFrame(render);
}

function setZoom(value, refreshDelay = 80) {
  zoom = Math.max(1, Math.min(10, value));
  zoomSlider.value = String(Math.round(zoom * 100));
  zoomValue.textContent = String(Math.round(zoom * 100)) + '%';
  scheduleRefresh(refreshDelay);
}

const pointers = new Map();
let lastSinglePoint = null;
let pinchStartDistance = 0;
let pinchStartZoom = 1;

function resetGestureBaseline() {
  const points = [...pointers.values()];
  if (points.length === 1) {
    lastSinglePoint = points[0];
    pinchStartDistance = 0;
    return;
  }
  if (points.length >= 2) {
    const dx = points[1].x - points[0].x;
    const dy = points[1].y - points[0].y;
    pinchStartDistance = Math.max(1, Math.hypot(dx, dy));
    pinchStartZoom = zoom;
    lastSinglePoint = null;
  }
}

canvas.addEventListener('pointerdown', event => {
  pointers.set(event.pointerId, { x: event.clientX, y: event.clientY });
  canvas.setPointerCapture(event.pointerId);
  resetGestureBaseline();
});

canvas.addEventListener('pointermove', event => {
  if (!pointers.has(event.pointerId)) return;
  pointers.set(event.pointerId, { x: event.clientX, y: event.clientY });
  const points = [...pointers.values()];

  if (points.length === 1 && lastSinglePoint) {
    const current = points[0];
    const dx = current.x - lastSinglePoint.x;
    const dy = current.y - lastSinglePoint.y;
    const gain = 0.23 / Math.sqrt(zoom);
    centerLon = (centerLon - dx * gain + 540) % 360 - 180;
    centerLat = Math.max(-89, Math.min(89, centerLat + dy * gain));
    lastSinglePoint = current;
    scheduleRefresh(90);
    return;
  }

  if (points.length >= 2 && pinchStartDistance > 0) {
    const dx = points[1].x - points[0].x;
    const dy = points[1].y - points[0].y;
    const distance = Math.max(1, Math.hypot(dx, dy));
    setZoom(pinchStartZoom * distance / pinchStartDistance, 110);
  }
});

function releasePointer(event) {
  pointers.delete(event.pointerId);
  resetGestureBaseline();
}

canvas.addEventListener('pointerup', releasePointer);
canvas.addEventListener('pointercancel', releasePointer);
canvas.addEventListener('lostpointercapture', releasePointer);

canvas.addEventListener('wheel', event => {
  event.preventDefault();
  setZoom(zoom * Math.exp(-event.deltaY * 0.0012), 80);
}, { passive: false });

zoomSlider.addEventListener('input', () => {
  setZoom(Number(zoomSlider.value) / 100, 70);
});

document.querySelector('#japan').addEventListener('click', () => {
  centerLon = 139.7;
  centerLat = 35.7;
  setZoom(3, 0);
});

document.querySelector('#reset').addEventListener('click', () => {
  centerLon = 0;
  centerLat = 12;
  setZoom(1, 0);
});

if (panelToggle && panel) {
  panelToggle.addEventListener('click', () => {
    const collapsed = panel.classList.toggle('collapsed');
    panelToggle.textContent = collapsed ? '+' : '−';
    panelToggle.setAttribute('aria-expanded', collapsed ? 'false' : 'true');
  });
}

setInterval(() => {
  clockEl.textContent = new Date().toLocaleString();
}, 1000);
clockEl.textContent = new Date().toLocaleString();

window.__vectorGlobe = {
  getState() {
    return {
      centerLon,
      centerLat,
      zoom,
      currentLod,
      deliveryBundleMode: isDeliveryBundleMode(),
      deliveryBundleCount: manifest && manifest.delivery && manifest.delivery.lods &&
        Array.isArray(manifest.delivery.lods[String(currentLod)])
          ? manifest.delivery.lods[String(currentLod)].length
          : 0,
      desiredTiles: desiredPaths.size,
      activeTiles: activeMeshes.length,
      cacheEntries: tileCache.size,
      cacheGpuBytes,
      cacheBudgetBytes: CACHE_BUDGET_BYTES,
      activeLoads,
      queuedLoads: loadQueue.length,
      fps
    };
  },
  clearUnusedGpuCache() {
    pruneCache(true);
    return this.getState();
  },
  setView(lon, lat, nextZoom = zoom) {
    centerLon = ((Number(lon) + 540) % 360) - 180;
    centerLat = Math.max(-89, Math.min(89, Number(lat)));
    setZoom(Number(nextZoom), 0);
    return this.getState();
  },
  setQaLighting(enabled) {
    qaLighting = Boolean(enabled);
    return this.getState();
  }
};

fetch('./data/manifest.json')
  .then(response => {
    if (!response.ok) throw new Error('HTTP ' + response.status);
    return response.json();
  })
  .then(data => {
    manifest = prepareManifest(data);
    return refreshTiles();
  })
  .catch(error => setStatus('vector data error: ' + error.message));

requestAnimationFrame(render);
