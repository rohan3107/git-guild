const form = document.getElementById('analyze-form');
const loadingEl = document.getElementById('loading');
const errorEl = document.getElementById('error');
const partyEl = document.getElementById('party');
const timelineEl = document.getElementById('timeline');
const bossesEl = document.getElementById('bosses');
const storyEl = document.getElementById('story');
const storyImagesEl = document.getElementById('story-images');
const oracleEl = document.getElementById('oracle');
const teamSummaryEl = document.getElementById('team-summary');
const guildPrevBtn = document.getElementById('guild-prev');
const guildNextBtn = document.getElementById('guild-next');
const guildPageEl = document.getElementById('guild-page');
const partyNameEl = document.getElementById('party-name');
const partyBannerAvatarEl = document.getElementById('party-banner-avatar');
const partyBannerFallbackEl = document.getElementById('party-banner-fallback');
const partyBannerLabelEl = document.getElementById('party-banner-label');

const GUILD_PAGE_SIZE = 3;
const CACHE_KEY = 'gitguild:last-analysis:v1';
let guildParty = [];
let guildPage = 0;

function normalizeRepoInput(input) {
  const trimmed = input.trim();
  if (trimmed.startsWith('http://') || trimmed.startsWith('https://')) {
    const m = trimmed.match(/github\.com\/([^/]+)\/([^/]+)/i);
    if (!m) return trimmed;
    return `${m[1]}/${m[2].replace(/\.git$/, '')}`;
  }
  return trimmed.replace(/\.git$/, '');
}

function parseRepoOwner(repoInput = '') {
  const normalized = normalizeRepoInput(repoInput || '');
  const [owner] = normalized.split('/');
  return owner || '';
}

function updatePartyIdentity(repoInput = '') {
  const owner = parseRepoOwner(repoInput);
  if (!owner) return;

  const prettyOwner = owner.replace(/[-_]/g, ' ');
  partyNameEl.textContent = `⚔️ ${prettyOwner} GUILD`;
  partyBannerLabelEl.textContent = `${prettyOwner} Avatar`;

  const avatarUrl = `https://github.com/${encodeURIComponent(owner)}.png?size=192`;
  partyBannerAvatarEl.onload = () => {
    partyBannerAvatarEl.style.display = 'block';
    partyBannerFallbackEl.style.display = 'none';
  };
  partyBannerAvatarEl.onerror = () => {
    partyBannerAvatarEl.style.display = 'none';
    partyBannerFallbackEl.style.display = 'block';
  };
  partyBannerAvatarEl.src = avatarUrl;
}

function statValue(stats, key) {
  const raw = Number(stats?.[key]);
  if (!Number.isFinite(raw)) return 0;
  return Math.max(0, Math.min(100, raw));
}

function buildSpiderGraphSvg(stats = {}) {
  const axes = [
    { key: 'str', label: 'STR' },
    { key: 'dex', label: 'DEX' },
    { key: 'int', label: 'INT' },
    { key: 'con', label: 'CON' },
    { key: 'wis', label: 'WIS' },
    { key: 'cha', label: 'CHA' }
  ];
  const values = axes.map((a) => statValue(stats, a.key));
  const center = 54;
  const radius = 38;
  const steps = 4;

  const ringPolygons = Array.from({ length: steps }, (_, i) => {
    const r = (radius * (i + 1)) / steps;
    const pts = axes.map((_, idx) => {
      const angle = -Math.PI / 2 + (idx * Math.PI * 2) / axes.length;
      const x = center + r * Math.cos(angle);
      const y = center + r * Math.sin(angle);
      return `${x.toFixed(2)},${y.toFixed(2)}`;
    });
    return `<polygon points="${pts.join(' ')}" class="spider-ring" />`;
  }).join('');

  const axisLines = axes.map((_, idx) => {
    const angle = -Math.PI / 2 + (idx * Math.PI * 2) / axes.length;
    const x = center + radius * Math.cos(angle);
    const y = center + radius * Math.sin(angle);
    return `<line x1="${center}" y1="${center}" x2="${x.toFixed(2)}" y2="${y.toFixed(2)}" class="spider-axis" />`;
  }).join('');

  const valuePoints = axes.map((a, idx) => {
    const angle = -Math.PI / 2 + (idx * Math.PI * 2) / axes.length;
    const r = radius * (values[idx] / 100);
    const x = center + r * Math.cos(angle);
    const y = center + r * Math.sin(angle);
    return `${x.toFixed(2)},${y.toFixed(2)}`;
  });

  const labels = axes.map((a, idx) => {
    const angle = -Math.PI / 2 + (idx * Math.PI * 2) / axes.length;
    const x = center + (radius + 12) * Math.cos(angle);
    const y = center + (radius + 12) * Math.sin(angle);
    return `<text x="${x.toFixed(2)}" y="${(y + 3).toFixed(2)}" class="spider-label">${a.label}</text>`;
  }).join('');

  return `
    <svg class="spider-chart" viewBox="0 0 108 108" role="img" aria-label="Member stats spider graph">
      ${ringPolygons}
      ${axisLines}
      <polygon points="${valuePoints.join(' ')}" class="spider-shape" />
      ${labels}
    </svg>
  `;
}

function assetUrl(path = '') {
  return encodeURI(path);
}

function normalizeArchetype(archetype = '') {
  const t = String(archetype || '').toLowerCase();
  if (t.includes('barb')) return 'barb';
  if (t.includes('rogue')) return 'rogue';
  if (t.includes('cleric')) return 'cleric';
  if (t.includes('wizard') || t.includes('mage')) return 'mage';
  if (t.includes('necro')) return 'necro';
  if (t.includes('bard')) return 'bard';
  return 'unknown';
}

function memberPortraitAsset(member = {}) {
  const status = String(member.status || '').toLowerCase();
  const archetype = normalizeArchetype(member.archetype);
  const isFallen = status.includes('dead') || status.includes('fallen') || status.includes('retired');

  const aliveByArchetype = {
    barb: 'assets/DPs/DP Barb.jpeg',
    rogue: 'assets/DPs/DP rogue.jpeg',
    cleric: 'assets/DPs/DP Cleric.jpeg',
    mage: 'assets/DPs/DP Mage.png',
    necro: 'assets/DPs/DP Necro.jpeg',
    bard: 'assets/DPs/DP Bard.png'
  };

  const deadByArchetype = {
    barb: 'assets/dead barb.jpeg',
    rogue: 'assets/dead rogue.jpeg',
    cleric: 'assets/dead cleric.jpeg',
    mage: 'assets/dead mage.png',
    necro: 'assets/dead necro.png',
    bard: 'assets/dead bard.png'
  };

  const attackingByArchetype = {
    barb: 'assets/attacking barb.jpeg',
    cleric: 'assets/attacking cleric.jpeg',
    mage: 'assets/attacking mage.png',
    necro: 'assets/attacking necro.png',
    bard: 'assets/attacking bard.png'
  };

  return {
    portrait: isFallen
      ? (deadByArchetype[archetype] || 'assets/barb - neutral blank.jpeg')
      : (aliveByArchetype[archetype] || 'assets/Barb - neutral.jpeg'),
    attack: attackingByArchetype[archetype] || ''
  };
}

function escapeHtml(value = '') {
  return String(value)
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#39;');
}

function ensureArray(value) {
  if (Array.isArray(value)) return value;
  if (value == null) return [];
  if (typeof value === 'string' || typeof value === 'number' || typeof value === 'boolean') {
    return [value];
  }
  if (typeof value === 'object') {
    if (Array.isArray(value.items)) return value.items;
    return [value];
  }
  return [];
}

function loadCachedAnalysis() {
  try {
    const raw = localStorage.getItem(CACHE_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw);
    if (!parsed || typeof parsed !== 'object') return null;
    return parsed;
  } catch {
    return null;
  }
}

function saveCachedAnalysis(cacheData) {
  try {
    localStorage.setItem(CACHE_KEY, JSON.stringify(cacheData));
  } catch {
    // Ignore storage quota and serialization failures.
  }
}

function renderCards(el, items, mapper, emptyText) {
  const list = ensureArray(items);
  if (!list.length) {
    el.classList.add('empty-state');
    el.innerHTML = emptyText || 'No data.';
    return;
  }
  el.classList.remove('empty-state');
  el.innerHTML = list.map(mapper).join('');
}

function buildStoryText(storyData) {
  if (!storyData) return 'No story generated.';
  if (typeof storyData === 'string') return storyData;

  const parts = [];
  if (storyData.prologue) parts.push(`Prologue\n${storyData.prologue}`);

  (storyData.chapters || []).forEach((c, idx) => {
    parts.push(`\nChapter ${idx + 1}: ${c.title || 'Untitled'}\n${c.text || ''}`);
  });

  if (storyData.epilogue) parts.push(`\nEpilogue\n${storyData.epilogue}`);
  return parts.join('\n');
}

function renderResult(data) {
  const party = ensureArray(data.party);
  const milestones = ensureArray(data.milestones);
  const bosses = ensureArray(data.bosses);
  const story = data.story;
  const oracle = data.oracle || {};
  const teamSummary = data.team_summary || {};

  guildParty = party;
  guildPage = 0;
  renderGuildParty();

  if (milestones.length) {
    timelineEl.classList.remove('empty-state');
    timelineEl.innerHTML = milestones
      .map((m) => {
        if (typeof m !== 'object') return `<li>${escapeHtml(m)}</li>`;
        return `<li><strong>${escapeHtml(m.title || 'Event')}:</strong> ${escapeHtml(m.summary || '')}${m.impact ? ` <em>(${escapeHtml(m.impact)})</em>` : ''}</li>`;
      })
      .join('');
  } else {
    timelineEl.classList.add('empty-state');
    timelineEl.innerHTML = '<li>No milestones returned.</li>';
  }

  renderCards(
    bossesEl,
    bosses,
    (b) => {
      const outcome = String(b.outcome || '').toLowerCase();
      const defeated = outcome.includes('defeat') || outcome.includes('slain') || outcome.includes('won');
      const bossArt = defeated ? 'assets/red dragon dead.png' : 'assets/red dragon enemy.png';
      return `
      <div class="card">
        <img class="boss-art" src="${assetUrl(bossArt)}" alt="${escapeHtml(b.name || 'Boss')} art" loading="lazy" />
        <h4>${escapeHtml(b.name || 'Unknown Boss')}</h4>
        <p>Difficulty: ${escapeHtml(b.difficulty || 'n/a')}</p>
        <p>${escapeHtml(b.context || '')}</p>
        ${b.outcome ? `<p><strong>Outcome:</strong> ${escapeHtml(b.outcome)}</p>` : ''}
      </div>
    `;
    },
    'No boss encounters yet.'
  );

  const chapters = typeof story === 'object' ? ensureArray(story.chapters) : [];
  renderCards(
    storyImagesEl,
    chapters,
    (c, idx) => `
      <div class="card scene-card">
        <div class="scene-image-placeholder">Scene ${idx + 1}</div>
        <p>${escapeHtml(c.image_prompt || c.title || 'Illustration placeholder')}</p>
      </div>
    `,
    'Scene placeholders will appear here.'
  );

  storyEl.classList.remove('empty-state');
  storyEl.textContent = buildStoryText(story);

  const summaryText = [
    ['Archetype', teamSummary.archetype],
    ['History', teamSummary.history],
    ['Technical debt / risk', teamSummary.technical_debt_risk],
    ['Future plans', teamSummary.future_plans]
  ]
    .filter(([, v]) => v)
    .map(([k, v]) => `<p><strong>${k}:</strong> ${escapeHtml(v)}</p>`)
    .join('');

  if (summaryText) {
    teamSummaryEl.classList.remove('empty-state');
    teamSummaryEl.innerHTML = summaryText;
  } else {
    teamSummaryEl.classList.add('empty-state');
    teamSummaryEl.innerHTML = 'Team summary was not returned.';
  }

  const oracleSections = [
    { title: 'Next Quests', items: ensureArray(oracle.next_quests) },
    { title: 'Risk Forecast', items: ensureArray(oracle.risk_forecast) },
    { title: 'Recommended Actions', items: ensureArray(oracle.recommended_actions) }
  ];

  const hasOracle = oracleSections.some((s) => ensureArray(s.items).length);
  if (!hasOracle) {
    oracleEl.classList.add('empty-state');
    oracleEl.textContent = 'No oracle insights returned.';
  } else {
    oracleEl.classList.remove('empty-state');
    oracleEl.innerHTML = oracleSections
      .map(
        (section) => `
      <div class="card">
        <h4>${escapeHtml(section.title)}</h4>
        <ul>
          ${ensureArray(section.items).map((i) => `<li>${escapeHtml(typeof i === 'object' ? JSON.stringify(i) : i)}</li>`).join('') || '<li>None</li>'}
        </ul>
      </div>`
      )
      .join('');
  }
}

function renderGuildParty() {
  const totalPages = Math.max(1, Math.ceil(guildParty.length / GUILD_PAGE_SIZE));
  guildPage = ((guildPage % totalPages) + totalPages) % totalPages;

  guildPageEl.textContent = guildParty.length ? `${guildPage + 1} / ${totalPages}` : '0 / 0';

  if (!guildParty.length) {
    partyEl.classList.add('empty-state');
    partyEl.innerHTML = 'No guild members yet.';
    guildPrevBtn.disabled = true;
    guildNextBtn.disabled = true;
    return;
  }

  guildPrevBtn.disabled = totalPages <= 1;
  guildNextBtn.disabled = totalPages <= 1;

  const start = guildPage * GUILD_PAGE_SIZE;
  const members = guildParty.slice(start, start + GUILD_PAGE_SIZE);

  renderCards(
    partyEl,
    members,
    (p) => {
      const status = (p.status || 'active').toLowerCase();
      const isRetired = status.includes('retired') || status.includes('dead') || status.includes('fallen');
      const artwork = memberPortraitAsset(p);
      return `
      <div class="card guild-card ${isRetired ? 'fallen' : ''}">
        <div class="guild-sprite-wrap">
          <img class="guild-sprite-img" src="${assetUrl(artwork.portrait)}" alt="${escapeHtml(p.name || 'Guild member')} portrait" loading="lazy" />
          ${!isRetired && artwork.attack ? `<img class="guild-attack-badge" src="${assetUrl(artwork.attack)}" alt="" aria-hidden="true" loading="lazy" />` : ''}
        </div>
        <h4>${escapeHtml(p.name || 'Unknown')} · ${escapeHtml(p.archetype || 'Adventurer')}</h4>
        <p class="status">Status: ${escapeHtml(p.status || 'active')}</p>
        <div class="stat-chart-wrap">${buildSpiderGraphSvg(p.stats || {})}</div>
        <p>Commits: ${p.stats?.commits ?? 0} · PRs: ${p.stats?.prs ?? 0}</p>
        <p>${escapeHtml(p.lore_blurb || '')}</p>
      </div>`;
    },
    'No guild members yet.'
  );
}

guildPrevBtn.addEventListener('click', () => {
  guildPage -= 1;
  renderGuildParty();
});

guildNextBtn.addEventListener('click', () => {
  guildPage += 1;
  renderGuildParty();
});

renderGuildParty();

const cached = loadCachedAnalysis();
if (cached?.repo && document.getElementById('repo')) {
  document.getElementById('repo').value = cached.repo;
  updatePartyIdentity(cached.repo);
}
if (cached?.data) {
  renderResult(cached.data);
}

form.addEventListener('submit', async (event) => {
  event.preventDefault();

  errorEl.classList.add('hidden');
  loadingEl.classList.remove('hidden');

  try {
    const repo = normalizeRepoInput(document.getElementById('repo').value);
    const token = document.getElementById('token').value.trim();
    updatePartyIdentity(repo);

    const response = await fetch('/api/analyze', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ repo, token: token || undefined })
    });

    const payload = await response.json();

    if (!response.ok) {
      throw new Error(payload.error || 'Failed to analyze repository');
    }

    updatePartyIdentity(payload.repo || repo);
    renderResult(payload.data || {});
    saveCachedAnalysis({
      repo,
      data: payload.data || {},
      cached_at: new Date().toISOString()
    });
  } catch (err) {
    errorEl.textContent = `Error: ${err.message}`;
    errorEl.classList.remove('hidden');
  } finally {
    loadingEl.classList.add('hidden');
  }
});
