// lib/spring-waapi.js — Closed-form CASpringAnimation -> CSS linear() & WAAPI FLIP morph (<= 140L)

const easingCache = new Map();

export function isMotionReduced() {
  if (typeof document !== 'undefined' && document.documentElement.classList.contains('mw-reduce-motion')) {
    return true;
  }
  if (typeof window !== 'undefined' && window.matchMedia) {
    return window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  }
  return false;
}

/**
 * Generates a 28-sample CSS `linear(...)` easing curve from a closed-form
 * damped harmonic oscillator matching UIKit CASpringAnimation.
 */
export function springLinearEasing({ stiffness = 380, damping = 34, mass = 1, samples = 28 } = {}) {
  const key = `${stiffness}:${damping}:${mass}:${samples}`;
  if (easingCache.has(key)) return easingCache.get(key);

  const w0 = Math.sqrt(Math.max(1, stiffness) / Math.max(0.1, mass));
  const zeta = Math.max(0.05, damping) / (2 * Math.sqrt(Math.max(1, stiffness) * Math.max(0.1, mass)));
  const settleSec = Math.min(1.4, Math.max(0.28, 4.6 / (zeta * w0)));
  const n = Math.max(8, Math.min(48, samples));
  const pts = [];

  for (let i = 0; i <= n; i++) {
    const u = i / n;
    const t = u * settleSec;
    let x;
    if (i === 0) {
      x = 0;
    } else if (i === n) {
      x = 1;
    } else if (zeta < 1) {
      const wd = w0 * Math.sqrt(1 - zeta * zeta);
      const decay = Math.exp(-zeta * w0 * t);
      x = 1 - decay * (Math.cos(wd * t) + ((zeta * w0) / wd) * Math.sin(wd * t));
    } else if (Math.abs(zeta - 1) < 1e-4) {
      const decay = Math.exp(-w0 * t);
      x = 1 - decay * (1 + w0 * t);
    } else {
      const s = Math.sqrt(zeta * zeta - 1);
      const r1 = -w0 * (zeta - s);
      const r2 = -w0 * (zeta + s);
      const c1 = r2 / (r2 - r1);
      const c2 = -r1 / (r2 - r1);
      x = 1 - (c1 * Math.exp(r1 * t) + c2 * Math.exp(r2 * t));
    }
    pts.push(Number(x.toFixed(4)));
  }

  const expr = `linear(${pts.join(', ')})`;
  easingCache.set(key, expr);
  return expr;
}

export function animateSpring(el, keyframes, { stiffness = 380, damping = 34, mass = 1, durationMs = 320, fill = 'none' } = {}) {
  if (!el || !Array.isArray(keyframes) || keyframes.length === 0) {
    return { finished: Promise.resolve(), cancel() {} };
  }
  const reduced = isMotionReduced();
  if (typeof el.animate !== 'function') {
    return { finished: Promise.resolve(), cancel() {} };
  }
  const frames = reduced
    ? [{ opacity: 0.72 }, { opacity: 1 }]
    : keyframes;
  const opts = {
    duration: reduced ? 110 : durationMs,
    easing: reduced ? 'ease-out' : springLinearEasing({ stiffness, damping, mass }),
    fill,
  };
  const anim = el.animate(frames, opts);
  const finished = anim.finished ? anim.finished.catch(() => {}) : Promise.resolve();
  return {
    animation: anim,
    finished,
    cancel() {
      try { anim.cancel(); } catch (_) {}
    },
  };
}

export function flipScaleMorph({ fromEl, toEl, durationMs = 320, stiffness = 360, damping = 34, onDone } = {}) {
  const finish = () => { if (typeof onDone === 'function') onDone(); };
  if (!toEl) {
    finish();
    return { cancel() {} };
  }
  if (isMotionReduced() || !fromEl || typeof fromEl.getBoundingClientRect !== 'function') {
    const fade = animateSpring(toEl, [{ opacity: 0 }, { opacity: 1 }], { durationMs: 140 });
    fade.finished.then(finish);
    return fade;
  }

  const fromRect = fromEl.getBoundingClientRect();
  const toRect = toEl.getBoundingClientRect();
  if (fromRect.width < 4 || fromRect.height < 4 || toRect.width < 4 || toRect.height < 4) {
    const fade = animateSpring(toEl, [{ opacity: 0.6 }, { opacity: 1 }], { durationMs: 180 });
    fade.finished.then(finish);
    return fade;
  }

  const dx = fromRect.left + fromRect.width / 2 - (toRect.left + toRect.width / 2);
  const dy = fromRect.top + fromRect.height / 2 - (toRect.top + toRect.height / 2);
  const sx = Math.max(0.45, Math.min(1.6, fromRect.width / toRect.width));
  const sy = Math.max(0.45, Math.min(1.6, fromRect.height / toRect.height));

  const anim = animateSpring(
    toEl,
    [
      { transform: `translate3d(${dx.toFixed(1)}px, ${dy.toFixed(1)}px, 0) scale(${sx.toFixed(3)}, ${sy.toFixed(3)})`, opacity: 0.55 },
      { transform: 'translate3d(0, 0, 0) scale(1, 1)', opacity: 1 },
    ],
    { stiffness, damping, durationMs },
  );
  anim.finished.then(finish);
  return anim;
}
