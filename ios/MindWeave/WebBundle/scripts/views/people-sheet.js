// people-sheet.js — Unified searchable tiered All-People Sheet (1..100+ people) + Create/Edit Person.
// Replaces retired casual.js. Uses 16px search input (0 iOS auto-zoom), mwNativeUI.pickPhoto
// (PHPickerViewController on Gold/Crystal, strictly hidden on Silver), and 0 'TA' copy leaks.
import { h, clear, svg, $ } from '../dom.js';
import { api } from '../api/endpoints.js';
import { store, upsertPerson, applyTierChange } from '../store.js';
import { monogramOf, sealStyle } from '../decor.js';
import { formatDateCn, TIER_LABEL, clip } from '../format.js';
import { SEARCH_SVG, CAMERA_SVG } from '../icons.js';
import { openSheet, closeSheet } from './sheet.js';
import { whisper } from './toast.js';
import { actions } from '../actions.js';
import { haptic, mwNativeUI } from '../native-bridge.js';

const TIER_FILTERS = [
  { id: 'all', label: '全部' },
  { id: 'gold', label: '金箭 · 珍重' },
  { id: 'silver', label: '银盾 · 边界' },
  { id: 'crystal', label: '水晶 · 日常' },
];

const TIER_OPTIONS = [
  { id: 'gold', title: '金箭 · 珍重', desc: '最珍视的人，丘比特多替你想一步' },
  { id: 'silver', title: '银盾 · 边界', desc: '需要守好边界的人，照片默认收起' },
  { id: 'crystal', title: '水晶 · 日常', desc: '日常往来的人，礼数周全不耗神' },
];

function allPeopleSorted(s) {
  const coreSet = new Set(s.coreIds || []);
  const coreOrder = new Map((s.coreIds || []).map((id, i) => [id, i]));
  return Object.values(s.people || {}).sort((a, b) => {
    const aCore = coreSet.has(a.id) ? 0 : 1;
    const bCore = coreSet.has(b.id) ? 0 : 1;
    if (aCore !== bCore) return aCore - bCore;
    if (aCore === 0) return (coreOrder.get(a.id) ?? 99) - (coreOrder.get(b.id) ?? 99);
    const ta = String(a.updatedAt || a.createdAt || '');
    const tb = String(b.updatedAt || b.createdAt || '');
    return tb.localeCompare(ta) || String(a.nameCn || '').localeCompare(String(b.nameCn || ''), 'zh-CN');
  });
}

function matchesPerson(p, q) {
  if (!q) return true;
  const hay = [p.nameCn, p.roleTag, p.pinyin, p.shortMemo, ...(p.aliases || [])]
    .filter(Boolean)
    .join(' ')
    .toLowerCase();
  return hay.includes(q.toLowerCase());
}

function avatarNode(p) {
  const tier = p.tier || 'crystal';
  const silver = tier === 'silver';
  const usePhoto = !silver && Boolean(p.avatarUrl);
  const el = h('span', {
    class: ['people-row-avatar', silver && 'is-cloaked'],
    dataset: { tier },
    style: usePhoto || silver ? {} : sealStyle(p),
  });
  if (usePhoto) el.appendChild(h('img', { src: p.avatarUrl, alt: '', loading: 'lazy', decoding: 'async' }));
  else el.textContent = monogramOf(p);
  return el;
}

function buildCreateForm({ initialName = '', onCreated, onCancel }) {
  let tier = 'gold';
  let pickedPhoto = null;
  let previewUrl = null;
  let saving = false;

  const nameInput = h('input', {
    type: 'text',
    class: 'people-form-input',
    maxlength: '24',
    value: initialName,
    placeholder: '称呼，比如：妈妈、小周、林医生',
    'aria-label': '称呼',
  });
  const roleInput = h('input', {
    type: 'text',
    class: 'people-form-input',
    maxlength: '24',
    placeholder: '关系标签（选填），比如：家人、同组同事',
    'aria-label': '关系标签',
  });
  const photoWrap = h('div', { class: 'people-photo-row' });
  const submitBtn = h('button', { type: 'button', class: 'sheet-btn', text: '新建手帐页', disabled: !initialName.trim() });

  const revokePreview = () => {
    if (previewUrl && previewUrl.startsWith('blob:')) URL.revokeObjectURL(previewUrl);
    previewUrl = null;
  };

  const renderPhotoRow = () => {
    clear(photoWrap);
    // Silver tier strictly hides photo avatar picker (03e §2.5).
    if (tier === 'silver') {
      revokePreview();
      pickedPhoto = null;
      photoWrap.hidden = true;
      return;
    }
    photoWrap.hidden = false;
    photoWrap.append(
      previewUrl
        ? h('img', { class: 'people-row-avatar people-photo-preview', src: previewUrl, alt: '头像预览' })
        : h('span', { class: 'people-row-avatar people-photo-placeholder', text: monogramOf({ nameCn: nameInput.value.trim() || '人' }) }),
      h(
        'button',
        {
          type: 'button',
          class: 'sheet-btn quiet',
          onClick: async () => {
            haptic('light');
            try {
              const items = await mwNativeUI.pickPhoto({ maxCount: 1, maxPixelSize: 512, tier });
              const first = Array.isArray(items) ? items[0] : null;
              if (!first) return;
              revokePreview();
              const file = first instanceof Blob ? first : first?.file || null;
              pickedPhoto = file ? { file, url: first?.url } : first;
              previewUrl = first?.url || (file ? URL.createObjectURL(file) : null);
              renderPhotoRow();
            } catch {
              whisper('这次没能选到照片', 2400);
            }
          },
        },
        svg(CAMERA_SVG, 'people-cam-icon'),
        h('span', { text: previewUrl ? '换一张照片' : '选一张头像（选填）' }),
      ),
    );
  };

  const tierCards = [];
  const tierGroup = h(
    'div',
    { class: 'onb-tier-grid people-tier-grid', role: 'radiogroup', 'aria-label': '选择关系分类' },
    ...TIER_OPTIONS.map((opt) => {
      const btn = h(
        'button',
        {
          type: 'button',
          class: ['onb-tier-card', 'people-tier-choice', tier === opt.id && 'is-selected'],
          dataset: { tier: opt.id },
          role: 'radio',
          'aria-checked': String(tier === opt.id),
          onClick: () => {
            haptic('selection');
            tier = opt.id;
            tierCards.forEach((c) => {
              const on = c.dataset.tier === tier;
              c.classList.toggle('is-selected', on);
              c.setAttribute('aria-checked', String(on));
            });
            renderPhotoRow();
          },
        },
        h('strong', { text: opt.title }),
        h('small', { text: opt.desc }),
      );
      tierCards.push(btn);
      return btn;
    }),
  );

  nameInput.addEventListener('input', () => {
    submitBtn.disabled = saving || !nameInput.value.trim();
    if (!previewUrl) renderPhotoRow();
  });

  submitBtn.addEventListener('click', async () => {
    const nameCn = nameInput.value.trim();
    if (!nameCn || saving) return;
    saving = true;
    submitBtn.disabled = true;
    try {
      const created = await api.createPerson({
        nameCn,
        roleTag: roleInput.value.trim() || undefined,
        tier,
      });
      if (created?.id) {
        upsertPerson(created);
        applyTierChange({ personId: created.id, tier: created.tier || tier });
        if (pickedPhoto?.file && tier !== 'silver' && typeof api.uploadPhoto === 'function') {
          api.uploadPhoto(created.id, { file: pickedPhoto.file, caption: `${nameCn}的封面照片`, pinned: false }).catch(() => {});
        }
        actions.refreshBootstrap?.();
        haptic('memorySeal', { tier });
        whisper(`已为「${nameCn}」新建手帐页`, 2400);
        revokePreview();
        onCreated?.(created);
      }
    } catch (err) {
      saving = false;
      submitBtn.disabled = false;
      haptic('warning');
      whisper(err?.message || '暂时没能建好这一页', 3000);
    }
  });

  renderPhotoRow();
  photoWrap.style.display = 'flex';
  photoWrap.style.alignItems = 'center';
  photoWrap.style.gap = '12px';
  const form = h(
    'div',
    { class: 'people-create-form', style: { display: 'flex', 'flex-direction': 'column', gap: '14px' } },
    photoWrap,
    h('label', { class: 'people-field', style: { display: 'flex', 'flex-direction': 'column', gap: '6px' } }, h('span', { class: 'settings-group-title people-field-label', text: '怎么称呼' }), nameInput),
    h('label', { class: 'people-field', style: { display: 'flex', 'flex-direction': 'column', gap: '6px' } }, h('span', { class: 'settings-group-title people-field-label', text: '关系标签' }), roleInput),
    h('div', { class: 'people-field', style: { display: 'flex', 'flex-direction': 'column', gap: '6px' } }, h('span', { class: 'settings-group-title people-field-label', text: '关系分类' }), tierGroup),
    h(
      'div',
      { class: 'sheet-actions' },
      h('button', { type: 'button', class: 'sheet-btn quiet', text: '返回', onClick: () => { revokePreview(); onCancel?.(); } }),
      submitBtn,
    ),
  );
  return { el: form, focusEl: nameInput, cleanup: revokePreview };
}

/**
 * Open the unified searchable All-People Directory Sheet (supports 1..100+ people).
 * @param {{ mode?: 'list'|'create', query?: string, onSelect?: (person) => void, title?: string }} opts
 */
export function openPeopleSheet({ mode = 'list', query = '', onSelect = null, title = '' } = {}) {
  let currentMode = mode;
  let filterTier = 'all';
  let searchQuery = String(query || '').trim();
  let formCleanup = null;

  const host = h('div', { class: 'people-sheet-root' });

  const renderContent = () => {
    formCleanup?.();
    formCleanup = null;
    clear(host);

    if (currentMode === 'create') {
      const form = buildCreateForm({
        initialName: searchQuery,
        onCancel: () => { currentMode = 'list'; renderContent(); },
        onCreated: (person) => {
          closeSheet({ silentHaptic: true });
          if (onSelect) onSelect(person);
          else actions.openPerson(person.id);
        },
      });
      formCleanup = form.cleanup;
      host.appendChild(form.el);
      requestAnimationFrame(() => form.focusEl?.focus({ preventScroll: true }));
      return;
    }

    const s = store.get();
    const all = allPeopleSorted(s);
    const activeId = s.activePersonId;
    const coreSet = new Set((s.coreIds || []).slice(0, 4));

    const searchInput = h('input', {
      type: 'search',
      class: 'mw-sheet-search',
      value: searchQuery,
      placeholder: `在 ${all.length} 位人物里查找姓名或关系…`,
      autocomplete: 'off',
      'aria-label': '搜索人物',
    });
    const listHost = h('div', { class: 'people-list', role: 'list' });

    const renderList = () => {
      clear(listHost);
      const filtered = all.filter((p) => {
        if (filterTier !== 'all' && (p.tier || 'crystal') !== filterTier) return false;
        return matchesPerson(p, searchQuery);
      });

      if (!filtered.length) {
        listHost.append(
          h('p', { class: 'people-empty', text: searchQuery ? `没找到「${searchQuery}」` : '这一分类里还没有人' }),
          h('button', {
            type: 'button',
            class: 'sheet-btn quiet',
            text: searchQuery ? `+ 新建「${searchQuery}」` : '+ 新建一位人物',
            onClick: () => { currentMode = 'create'; renderContent(); },
          }),
        );
        return;
      }

      for (const p of filtered) {
        const tier = p.tier || 'crystal';
        const isCore = coreSet.has(p.id);
        const memo = p.shortMemo || clip((s.materials[p.id]?.items || [])[0]?.body || '', 30);
        const lastDate = formatDateCn(p.lastMaterialAt || (s.materials[p.id]?.items || [])[0]?.happenedOn);
        const tierShort = TIER_LABEL?.[tier]?.short || '';

        listHost.appendChild(
          h(
            'button',
            {
              type: 'button',
              role: 'listitem',
              class: ['people-row', p.id === activeId && 'is-active'],
              dataset: { personId: p.id, tier },
              onClick: () => {
                haptic('selection');
                closeSheet({ silentHaptic: true });
                if (onSelect) onSelect(p);
                else actions.openPerson(p.id);
              },
            },
            avatarNode(p),
            h(
              'span',
              { class: 'people-row-body' },
              h(
                'span',
                { class: 'people-row-top' },
                h('strong', { class: 'people-row-name', text: p.nameCn }),
                p.roleTag ? h('span', { class: 'people-row-tag', text: p.roleTag }) : null,
                isCore ? h('span', { class: 'people-row-core', text: '常记' }) : null,
                h('span', { class: ['people-row-tier', `is-${tier}`], text: tierShort }),
              ),
              memo || lastDate
                ? h(
                    'span',
                    { class: 'people-row-sub' },
                    h('span', { class: 'people-row-memo', text: memo || '暂无记录' }),
                    lastDate ? h('span', { class: 'people-row-date', text: lastDate }) : null,
                  )
                : null,
            ),
            h('span', { class: 'people-row-chevron', 'aria-hidden': 'true', text: '›' }),
          ),
        );
      }
    };

    searchInput.addEventListener('input', () => {
      searchQuery = searchInput.value.trim();
      renderList();
    });

    const filterChips = [];
    const filterBar = h(
      'div',
      { class: 'people-filter-row', role: 'tablist', 'aria-label': '按关系分类筛选' },
      ...TIER_FILTERS.map((f) => {
        const chip = h('button', {
          type: 'button',
          role: 'tab',
          class: ['people-filter-chip', filterTier === f.id && 'is-selected'],
          'aria-selected': String(filterTier === f.id),
          text: f.label,
          onClick: () => {
            haptic('selection');
            filterTier = f.id;
            filterChips.forEach((c) => {
              const on = c.dataset.filter === filterTier;
              c.classList.toggle('is-selected', on);
              c.setAttribute('aria-selected', String(on));
            });
            renderList();
          },
        });
        chip.dataset.filter = f.id;
        filterChips.push(chip);
        return chip;
      }),
    );

    const searchWrap = h(
      'div',
      { class: 'people-search-bar' },
      svg(SEARCH_SVG, 'people-search-icon'),
      searchInput,
      h('button', {
        type: 'button',
        class: 'people-new-btn',
        text: '+ 新建',
        onClick: () => { haptic('light'); currentMode = 'create'; renderContent(); },
      }),
    );

    renderList();
    host.append(searchWrap, filterBar, listHost);
  };

  renderContent();
  return openSheet({
    title: title || (mode === 'create' ? '新建一位人物' : '全部人物'),
    sub: mode === 'create' ? '慢慢写下在意的人' : `共 ${Object.keys(store.get().people || {}).length} 位，轻点翻开手帐页`,
    body: host,
    className: 'is-people-sheet',
    onClose: () => { formCleanup?.(); },
  });
}

/** Backward-compatible alias so any caller invoking actions.openDirectory() opens the unified All-People Sheet. */
export function openDirectory() {
  return openPeopleSheet({ mode: 'list' });
}
