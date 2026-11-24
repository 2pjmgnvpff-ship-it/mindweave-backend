// lib/virtual-river.js — Fenwick-Tree Virtual Scroller (MAX_MOUNTED_NODES=32, 0-LoAF DocumentFragment batch, <= 390L)

const MAX_MOUNTED_NODES = 32;
const HEIGHT_EPSILON_PX = 1.5;
const MOMENTUM_SETTLE_MS = 140;

/**
 * High-performance virtual scroller for 1..500+ variable-height journal slips.
 * Uses a Fenwick tree (BIT) for O(log N) prefix offsets, batches mounts via DocumentFragment,
 * caches DOM heights (lastDomH) to eliminate LoAFs, and holds scroll adjustments during
 * pinch/touch/momentum per TanStack Virtual #1307/#1148/#1258.
 */
export function createVirtualRiver({
  scroller,
  stage,
  headerEl = null,
  renderItem,
  estimateHeight = () => 148,
  overscanPx = 420,
} = {}) {
  let items = [];
  let heights = new Float64Array(0);
  let bit = new Float64Array(1);
  let totalHeight = 0;

  const mounted = new Map(); // index -> { node, key, lastDomH }
  const keyHeightCache = new Map(); // item key -> measured height
  let pinchHold = false;
  let pointerDown = false;
  let tailStillMoving = false;
  let momentumTimer = 0;
  let rafId = 0;
  let pendingScrollDelta = 0;
  let destroyed = false;

  const keyOf = (item, idx) => (item && item.id != null ? String(item.id) : `idx:${idx}`);

  const bitAdd = (i, delta) => {
    const n = items.length;
    for (let k = i + 1; k <= n; k += k & -k) bit[k] += delta;
  };

  const bitSum = (count) => {
    let sum = 0;
    for (let k = Math.min(count, items.length); k > 0; k -= k & -k) sum += bit[k];
    return sum;
  };

  const bitLowerBound = (targetY) => {
    const n = items.length;
    if (n === 0 || targetY <= 0) return 0;
    let idx = 0;
    let sum = 0;
    let step = 1;
    while ((step << 1) <= n) step <<= 1;
    for (; step > 0; step >>= 1) {
      const next = idx + step;
      if (next <= n && sum + bit[next] < targetY) {
        idx = next;
        sum += bit[next];
      }
    }
    return Math.min(idx, n - 1);
  };

  const clampStageHeight = (h) => {
    const dpr = (typeof window !== 'undefined' && window.devicePixelRatio) || 1;
    return Math.min(Math.ceil(h), Math.floor(15_000_000 / dpr));
  };

  const rebuildTree = () => {
    const n = items.length;
    heights = new Float64Array(n);
    bit = new Float64Array(n + 1);
    totalHeight = 0;
    for (let i = 0; i < n; i++) {
      const k = keyOf(items[i], i);
      const cached = keyHeightCache.get(k);
      const h = Math.max(36, cached != null ? cached : Number(estimateHeight(items[i], i)) || 148);
      heights[i] = h;
      totalHeight += h;
      bit[i + 1] += h;
      const parent = i + 1 + ((i + 1) & -(i + 1));
      if (parent <= n) bit[parent] += bit[i + 1];
    }
    if (stage) stage.style.height = `${clampStageHeight(totalHeight)}px`;
  };

  const headerOffset = () => {
    if (stage && typeof stage.offsetTop === 'number' && stage.offsetTop > 0) return stage.offsetTop;
    return headerEl ? headerEl.offsetHeight : 0;
  };

  const flushHeldDelta = () => {
    if (!scroller || Math.abs(pendingScrollDelta) < HEIGHT_EPSILON_PX) {
      pendingScrollDelta = 0;
      return;
    }
    if (pinchHold || pointerDown || tailStillMoving) return;
    scroller.scrollTop = Math.max(0, scroller.scrollTop + pendingScrollDelta);
    pendingScrollDelta = 0;
  };

  const ro = typeof ResizeObserver === 'function'
    ? new ResizeObserver((entries) => {
        if (destroyed) return;
        let changed = false;
        const viewportTop = scroller ? scroller.scrollTop - headerOffset() : 0;

        for (const entry of entries) {
          const el = entry.target;
          if (!el.isConnected) continue;
          const idx = Number(el.dataset.vrIndex);
          if (!Number.isInteger(idx) || idx < 0 || idx >= items.length) continue;
          const slot = mounted.get(idx);
          if (!slot || slot.node !== el) continue;

          const boxH = entry.borderBoxSize?.[0]?.blockSize ?? entry.contentRect.height;
          const measured = Math.max(36, Math.round(boxH + 12)); // includes 12px margin rhythm
          if (Math.abs(measured - slot.lastDomH) < HEIGHT_EPSILON_PX) continue;

          const delta = measured - heights[idx];
          slot.lastDomH = measured;
          if (Math.abs(delta) < HEIGHT_EPSILON_PX) continue;

          const itemTop = bitSum(idx);
          heights[idx] = measured;
          keyHeightCache.set(slot.key, measured);
          bitAdd(idx, delta);
          totalHeight += delta;
          changed = true;

          // Keep anchor stable when an above-the-fold item grows/shrinks
          if (itemTop + heights[idx] < viewportTop) {
            if (pinchHold || pointerDown || tailStillMoving) {
              pendingScrollDelta += delta;
            } else if (scroller) {
              scroller.scrollTop = Math.max(0, scroller.scrollTop + delta);
            }
          }
        }

        if (changed) {
          if (stage) stage.style.height = `${clampStageHeight(totalHeight)}px`;
          positionMounted();
        }
      })
    : null;

  const positionMounted = () => {
    for (const [idx, slot] of mounted) {
      const y = bitSum(idx);
      slot.node.style.transform = `translate3d(0, ${Math.round(y)}px, 0)`;
    }
  };

  const unmountAll = () => {
    for (const slot of mounted.values()) {
      if (ro) ro.unobserve(slot.node);
      slot.node.remove();
    }
    mounted.clear();
  };

  const renderWindow = () => {
    if (destroyed || !stage || !scroller) return;
    const n = items.length;
    if (n === 0) {
      unmountAll();
      stage.style.height = '0px';
      return;
    }

    const viewH = scroller.clientHeight || 720;
    const relTop = Math.max(0, scroller.scrollTop - headerOffset());
    let start = bitLowerBound(Math.max(0, relTop - overscanPx));
    let end = bitLowerBound(relTop + viewH + overscanPx);
    end = Math.min(n - 1, end + 1);

    // Strictly enforce MAX_MOUNTED_NODES = 32 cap centered around viewport
    if (end - start + 1 > MAX_MOUNTED_NODES) {
      const mid = bitLowerBound(relTop + viewH * 0.5);
      const half = Math.floor(MAX_MOUNTED_NODES / 2);
      start = Math.max(0, Math.min(n - MAX_MOUNTED_NODES, mid - half));
      end = Math.min(n - 1, start + MAX_MOUNTED_NODES - 1);
    }

    for (const [idx, slot] of mounted) {
      if (idx < start || idx > end) {
        if (ro) ro.unobserve(slot.node);
        slot.node.remove();
        mounted.delete(idx);
      }
    }

    const frag = document.createDocumentFragment();
    let mountedAny = false;

    for (let i = start; i <= end; i++) {
      if (mounted.has(i)) continue;
      const item = items[i];
      const key = keyOf(item, i);
      const inner = renderItem(item, i);
      if (!inner) continue;

      const wrap = document.createElement('div');
      wrap.className = 'vr-row';
      wrap.dataset.vrIndex = String(i);
      wrap.style.position = 'absolute';
      wrap.style.top = '0';
      wrap.style.left = '0';
      wrap.style.right = '0';
      wrap.style.transform = `translate3d(0, ${Math.round(bitSum(i))}px, 0)`;
      wrap.appendChild(inner);

      mounted.set(i, { node: wrap, key, lastDomH: heights[i] });
      frag.appendChild(wrap);
      mountedAny = true;
    }

    if (mountedAny) {
      stage.appendChild(frag);
      if (ro) {
        for (let i = start; i <= end; i++) {
          const slot = mounted.get(i);
          if (slot) ro.observe(slot.node);
        }
      }
    }
    positionMounted();
  };

  const scheduleRender = () => {
    if (destroyed || rafId) return;
    rafId = requestAnimationFrame(() => {
      rafId = 0;
      renderWindow();
    });
  };

  const onScroll = () => {
    tailStillMoving = true;
    clearTimeout(momentumTimer);
    momentumTimer = setTimeout(() => {
      tailStillMoving = false;
      flushHeldDelta();
    }, MOMENTUM_SETTLE_MS);
    scheduleRender();
  };

  const onTouchStart = () => { pointerDown = true; };
  const onTouchEnd = () => {
    pointerDown = false;
    if (!tailStillMoving) flushHeldDelta();
  };

  if (scroller) {
    scroller.addEventListener('scroll', onScroll, { passive: true });
    scroller.addEventListener('touchstart', onTouchStart, { passive: true });
    scroller.addEventListener('touchend', onTouchEnd, { passive: true });
    scroller.addEventListener('touchcancel', onTouchEnd, { passive: true });
  }

  const setItems = (nextItems) => {
    items = Array.isArray(nextItems) ? nextItems.slice() : [];
    unmountAll();
    pendingScrollDelta = 0;
    rebuildTree();
    renderWindow();
  };

  const syncItems = (nextItems) => {
    const anchor = getAnchor();
    setItems(nextItems);
    if (anchor && anchor.id != null) {
      scrollToItem(anchor.id, anchor.offsetPx);
    }
  };

  const setPinchHold = (active) => {
    pinchHold = Boolean(active);
    if (!pinchHold) flushHeldDelta();
  };

  const scrollToItem = (id, offsetPx = 0) => {
    if (!scroller) return false;
    const targetKey = String(id);
    const idx = items.findIndex((it, i) => keyOf(it, i) === targetKey);
    if (idx < 0) return false;
    const y = Math.max(0, headerOffset() + bitSum(idx) + offsetPx);
    scroller.scrollTop = y;
    renderWindow();
    return true;
  };

  const scrollToTop = ({ behavior = 'smooth' } = {}) => {
    if (!scroller) return;
    if (typeof scroller.scrollTo === 'function') {
      scroller.scrollTo({ top: 0, behavior });
    } else {
      scroller.scrollTop = 0;
    }
  };

  const getAnchor = () => {
    if (!scroller || items.length === 0) return null;
    const relTop = Math.max(0, scroller.scrollTop - headerOffset());
    const idx = bitLowerBound(relTop);
    const item = items[idx];
    if (!item) return null;
    return {
      index: idx,
      id: keyOf(item, idx),
      offsetPx: relTop - bitSum(idx),
    };
  };

  const destroy = () => {
    destroyed = true;
    clearTimeout(momentumTimer);
    if (rafId) cancelAnimationFrame(rafId);
    unmountAll();
    if (ro) ro.disconnect();
    if (scroller) {
      scroller.removeEventListener('scroll', onScroll);
      scroller.removeEventListener('touchstart', onTouchStart);
      scroller.removeEventListener('touchend', onTouchEnd);
      scroller.removeEventListener('touchcancel', onTouchEnd);
    }
  };

  return {
    setItems,
    syncItems,
    setPinchHold,
    scrollToItem,
    scrollToTop,
    getAnchor,
    getTotalHeight: () => totalHeight,
    destroy,
  };
}
