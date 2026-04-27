// Knowledge graph force-directed layout worker
// Receives {nodes, edges, w, h} and posts back {positions}
self.addEventListener('message', ({ data }) => {
  const { nodes, edges, w, h, iters = 200 } = data;

  const pos = {};
  nodes.forEach(n => {
    pos[n.id] = {
      x: w / 2 + (Math.random() - 0.5) * w * 0.6,
      y: h / 2 + (Math.random() - 0.5) * h * 0.6,
    };
  });

  for (let iter = 0; iter < iters; iter++) {
    const alpha = 0.12 * (1 - iter / iters);
    for (let i = 0; i < nodes.length; i++) {
      for (let j = i + 1; j < nodes.length; j++) {
        const a = pos[nodes[i].id], b = pos[nodes[j].id];
        let dx = b.x - a.x, dy = b.y - a.y;
        const dist = Math.sqrt(dx * dx + dy * dy) || 1;
        const force = 6000 / (dist * dist);
        dx /= dist; dy /= dist;
        a.x -= dx * force * alpha; a.y -= dy * force * alpha;
        b.x += dx * force * alpha; b.y += dy * force * alpha;
      }
    }
    edges.forEach(e => {
      const a = pos[e.from], b = pos[e.to];
      if (!a || !b) return;
      let dx = b.x - a.x, dy = b.y - a.y;
      const dist = Math.sqrt(dx * dx + dy * dy) || 1;
      const force = (dist - 120) * 0.01;
      dx /= dist; dy /= dist;
      a.x += dx * force * alpha; a.y += dy * force * alpha;
      b.x -= dx * force * alpha; b.y -= dy * force * alpha;
    });
    nodes.forEach(n => {
      const p = pos[n.id];
      p.x = Math.max(50, Math.min(w - 50, p.x));
      p.y = Math.max(50, Math.min(h - 50, p.y));
    });
  }

  self.postMessage({ positions: pos });
});
