// people-sheet.js — Unified Searchable Tiered All-People Sheet + Create, Edit & Delete Person.
// Fixes P0-07 (full Edit/Delete person), P0-08 (real POST /persons/{id}/photos/avatar),
// P1-14 (dynamic header sync), P1-15 (clean wrapping filter chips & memo fallback), and stacked picker mode.
import { h, clear, svg } from '../dom.js';
import { api } from '../api/endpoints.js';
import { store, upsertPerson, removePerson, applyTierChange } from '../store.js';
import { monogramOf, sealStyle } from '../decor.js';
import { formatDateCn, TIER_LABEL, clip } from '../format.js';
import { SEARCH_SVG, CAMERA_SVG, PEN_SVG } from '../icons.js';
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
  const hay = [p.nameCn, p.roleTag, p.pinyin, p.shortMemo, ...(p.aliases || [])].filter(Boolean).join(' ').toLowerCase();
  return hay.includes(q.toLowerCase());
}

function avatarNode(p) {
  const tier = p.tier || 'crystal';
  const silver = tier === 'silver';
  const usePhoto = !silver && Boolean(p.avatarUrl);
  const el = h('span', { class: ['people-row-avatar', silver && 'is-cloaked'], dataset: { tier }, style: usePhoto || silver ? {} : sealStyle(p) });
  if (usePhoto) el.appendChild(h('img', { src: p.avatarUrl, alt: '', loading: 'lazy', decoding: 'async' }));
  else el.textContent = monogramOf(p);
  return el;
}

function buildPersonForm({ existing = null, initialName = '', onSaved, onDeleted, onCancel }) {
  const isEdit = Boolean(existing?.id);
  let tier = existing?.tier || 'gold';
  let pickedPhoto = null;
  let previewUrl = !existing || tier === 'silver' ? null : (existing.avatarUrl || null);
  let ownBlobUrl = null;
  let saving = false;

  const nameInput = h('input', {
    type: 'text',
    class: 'people-form-input',
    maxlength: '24',
    value: existing?.nameCn || initialName,
    placeholder: '称呼，比如：妈妈、小周、林医生',
    'aria-label': '称呼',
  });
  const roleInput = h('input', {
    type: 'text',
    class: 'people-form-input',
    maxlength: '24',
    value: existing?.roleTag || '',
    placeholder: '关系标签（选填），比如：家人、同组同事',
    'aria-label': '关系标签',
  });
  const photoWrap = h('div', { class: 'people-photo-row' });
  const submitBtn = h('button', { type: 'button', class: 'sheet-btn', text: isEdit ? '保存资料' : '新建手帐页', disabled: !(existing?.nameCn || initialName).trim() });

  const revokePreview = () => {
    if (ownBlobUrl) { URL.revokeObjectURL(ownBlobUrl); ownBlobUrl = null; }
  };

  const renderPhotoRow = () => {
    clear(photoWrap);
    if (tier === 'silver') {
      revokePreview();
      pickedPhoto = null;
      previewUrl = null;
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
              if (file) { ownBlobUrl = URL.createObjectURL(file); previewUrl = ownBlobUrl; }
              else previewUrl = first?.url || null;
              renderPhotoRow();
            } catch {
              whisper('这次没能选到照片', 2400);
            }
          },
        },
        svg(CAMERA_SVG, 'people-cam-icon'),
        h('span', { text: previewUrl ? '换一张头像' : '选一张头像（选填）' }),
      ),
    );
  };

  const tierCards = [];
  const tierGroup = h(
    'div',
    { class: 'people-tier-grid', role: 'radiogroup', 'aria-label': '选择关系分类' },
    ...TIER_OPTIONS.map((opt) => {
      const btn = h(
        'button',
        {
          type: 'button',
          class: ['people-tier-choice', tier === opt.id && 'is-selected'],
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
    const roleTag = roleInput.value.trim();
    if (!nameCn || saving) return;
    saving = true;
    submitBtn.disabled = true;
    try {
      let person = existing;
      if (isEdit) {
        const updated = await api.patchPerson(existing.id, { nameCn, roleTag });
        person = { ...existing, ...(updated || {}), nameCn, roleTag };
        upsertPerson(person);
        if (tier !== existing.tier) {
          const tierRes = await api.setTier(existing.id, tier);
          applyTierChange({ personId: existing.id, tier: tierRes?.tier || tier, coreIds: tierRes?.coreIds, casualIds: tierRes?.casualIds });
          person = { ...person, tier: tierRes?.tier || tier };
        }
      } else {
        person = await api.createPerson({ nameCn, roleTag: roleTag || undefined, tier });
        if (person?.id) {
          upsertPerson(person);
          applyTierChange({ personId: person.id, tier: person.tier || tier });
        }
      }
      if (person?.id && pickedPhoto?.file && tier !== 'silver' && typeof api.uploadPersonAvatar === 'function') {
        const withAvatar = await api.uploadPersonAvatar(person.id, pickedPhoto.file).catch(() => null);
        if (withAvatar?.id) { person = withAvatar; upsertPerson(withAvatar); }
      }
      actions.refreshBootstrap?.();
      haptic('memorySeal', { tier });
      whisper(isEdit ? `已更新「${nameCn}」的资料` : `已为「${nameCn}」新建手帐页`, 2200);
      revokePreview();
      onSaved?.(person);
    } catch (err) {
      saving = false;
      submitBtn.disabled = false;
      haptic('warning');
      whisper(err?.message || (isEdit ? '资料没保存成功' : '暂时没能建好这一页'), 2800);
    }
  });

  const deleteZone = isEdit
    ? h(
        'div',
        { class: 'people-danger-zone' },
        h('button', {
          type: 'button',
          class: 'sheet-danger-btn mw-btn-full',
          text: `删除「${existing.nameCn}」及其全部手帐`,
          onClick: async () => {
            const choice = await mwNativeUI?.showSheet?.({
              style: 'actionSheet',
              title: `删除「${existing.nameCn}」？`,
              message: '这位人物下的手帐、提醒与对话记录将一并移除。',
              items: [
                { id: 'delete', label: '确认删除人物', role: 'destructive', destructive: true },
                { id: 'cancel', label: '取消', role: 'cancel', cancel: true },
              ],
            });
            if (choice && choice.selectedId !== 'delete') return;
            try {
              await api.deletePerson(existing.id);
              removePerson(existing.id);
              actions.refreshBootstrap?.();
              haptic('warning');
              revokePreview();
              whisper(`已移除「${existing.nameCn}」`, 2200);
              onDeleted?.(existing);
            } catch (err) {
              haptic('warning');
              whisper(err?.message || '暂时没能删除这位人物', 2600);
            }
          },
        }),
      )
    : null;

  renderPhotoRow();
  const form = h(
    'div',
    { class: 'people-create-form' },
    photoWrap,
    h('label', { class: 'people-field' }, h('span', { class: 'people-field-label', text: '怎么称呼' }), nameInput),
    h('label', { class: 'people-field' }, h('span', { class: 'people-field-label', text: '关系标签' }), roleInput),
    h('div', { class: 'people-field' }, h('span', { class: 'people-field-label', text: '关系分类' }), tierGroup),
    h('div', { class: 'sheet-actions' }, h('button', { type: 'button', class: 'sheet-btn quiet', text: '返回', onClick: () => { revokePreview(); onCancel?.(); } }), submitBtn),
    deleteZone,
  );
  return { el: form, focusEl: nameInput, cleanup: revokePreview };
}

/**
 * Open the unified searchable All-People Sheet (`mode: 'list' | 'create' | 'edit'`).
 */
export function openPeopleSheet({ mode = 'list', person = null, query = '', onSelect = null, title = '', stacked = false } = {}) {
  let currentMode = mode;
  let editingPerson = person;
  let filterTier = 'all';
  let searchQuery = String(query || '').trim();
  let formCleanup = null;
  let sheetHandle = null;

  const host = h('div', { class: 'people-sheet-root' });

  const syncHeader = () => {
    if (!sheetHandle?.setHeader) return;
    const count = Object.keys(store.get().people || {}).length;
    if (currentMode === 'create') sheetHandle.setHeader({ title: '新建一位人物', sub: '写下称呼与关系分类，立即展开第一页' });
    else if (currentMode === 'edit') sheetHandle.setHeader({ title: `编辑「${editingPerson?.nameCn || ''}」`, sub: '修改称呼、关系标签、分类或头像' });
    else sheetHandle.setHeader({ title: title || '全部人物', sub: onSelect ? `共 ${count} 位，轻点选择` : `共 ${count} 位，轻点翻开手帐页` });
  };

  const renderContent = () => {
    formCleanup?.();
    formCleanup = null;
    clear(host);
    syncHeader();

    if (currentMode === 'create' || currentMode === 'edit') {
      const form = buildPersonForm({
        existing: currentMode === 'edit' ? editingPerson : null,
        initialName: searchQuery,
        onCancel: () => {
          if (mode === 'edit' || (mode === 'create' && stacked)) { sheetHandle?.close?.(); return; }
          currentMode = 'list';
          editingPerson = null;
          renderContent();
        },
        onSaved: (saved) => {
          sheetHandle?.close?.({ silentHaptic: true });
          if (mode === 'edit' && stacked) closeSheet({ silentHaptic: true });
          if (onSelect) onSelect(saved);
          else if (saved?.id) actions.openPerson(saved.id);
        },
        onDeleted: () => {
          closeSheet({ silentHaptic: true, all: true });
          const nextId = store.get().activePersonId;
          if (nextId) actions.openPerson(nextId);
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
        const memo = p.shortMemo || clip((s.materials[p.id]?.items || [])[0]?.body || '', 28) || (p.roleTag ? `${p.roleTag} · 轻点翻开手帐` : '轻点翻开手帐');
        const lastDate = formatDateCn(p.lastMaterialAt || (s.materials[p.id]?.items || [])[0]?.happenedOn);
        const tierShort = TIER_LABEL?.[tier]?.short || '';

        const mainBtn = h(
          'button',
          {
            type: 'button',
            class: 'people-row',
            dataset: { personId: p.id, tier },
            onClick: () => {
              haptic('selection');
              sheetHandle?.close?.({ silentHaptic: true });
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
            h(
              'span',
              { class: 'people-row-sub' },
              h('span', { class: 'people-row-memo', text: memo }),
              lastDate ? h('span', { class: 'people-row-date', text: lastDate }) : null,
            ),
          ),
        );

        const editBtn = !onSelect
          ? h('button', {
              type: 'button',
              class: 'people-row-edit-btn',
              'aria-label': `编辑「${p.nameCn}」`,
              onClick: (e) => {
                e.stopPropagation();
                haptic('light');
                editingPerson = p;
                currentMode = 'edit';
                renderContent();
              },
            }, svg(PEN_SVG))
          : h('span', { class: 'people-row-chevron', 'aria-hidden': 'true', text: '›' });

        listHost.appendChild(h('div', { role: 'listitem', class: ['people-row-wrap', p.id === activeId && 'is-active'] }, mainBtn, editBtn));
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

  sheetHandle = openSheet({
    title: title || (mode === 'create' ? '新建一位人物' : mode === 'edit' ? `编辑「${person?.nameCn || ''}」` : '全部人物'),
    sub: mode === 'create' ? '写下称呼与关系分类，立即展开第一页' : `共 ${Object.keys(store.get().people || {}).length} 位，轻点翻开手帐页`,
    body: host,
    className: 'is-people-sheet',
    stacked,
    onClose: () => { formCleanup?.(); },
  });
  renderContent();
  return sheetHandle;
}

export function openDirectory() {
  return openPeopleSheet({ mode: 'list' });
}
