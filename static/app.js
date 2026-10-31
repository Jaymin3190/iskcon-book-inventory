const state = { token: localStorage.getItem('tk') || '', user: null, activeTab: 'dashboard', books: [], scCountId: null, cart: [], distributorSales: [] };

function esc(s) { return String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c])); }

async function api(path, opts = {}) {
  const headers = { 'Content-Type': 'application/json' };
  if (state.token) headers['Authorization'] = 'Bearer ' + state.token;
  const res = await fetch(path, { headers, ...opts, body: opts.body ? JSON.stringify(opts.body) : undefined });
  if (res.status === 401 && !opts.public) { logout(); throw new Error('Session expired'); }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const msg = Array.isArray(data.detail)
      ? data.detail.map(d => `${Array.isArray(d.loc) ? d.loc.join('.') + ': ' : ''}${d.msg}`).join('; ')
      : (data.detail || res.statusText);
    throw new Error(msg);
  }
  return data;
}

const TABS = [
  { id: 'dashboard', label: 'Dashboard', roles: ['super_admin', 'zone_admin', 'temple'] },
  { id: 'books', label: 'Books', roles: ['super_admin', 'zone_admin', 'temple'] },
  { id: 'stock', label: 'Stock', roles: ['super_admin', 'zone_admin', 'temple'] },
  { id: 'transfers', label: 'Transfers', roles: ['super_admin', 'zone_admin', 'temple'] },
  { id: 'returns', label: 'Returns', roles: ['super_admin', 'zone_admin', 'temple'] },
  { id: 'pos', label: 'POS Billing', roles: ['super_admin', 'zone_admin', 'distributor', 'temple'] },
  { id: 'reports', label: 'Reports', roles: ['super_admin', 'zone_admin', 'temple'] },
  { id: 'users', label: 'Users', roles: ['super_admin', 'zone_admin'] },
  { id: 'locations', label: 'Locations', roles: ['super_admin'] },
];

const TAB_GROUPS = [
  { label: 'Overview', tabs: ['dashboard'] },
  { label: 'Catalogue', tabs: ['books', 'stock'] },
  { label: 'Operations', tabs: ['transfers', 'returns', 'pos'] },
  { label: 'Insights', tabs: ['reports'] },
  { label: 'Administration', tabs: ['users', 'locations'] },
];

const NAV_ICONS = {
  dashboard: '<rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/>',
  books: '<path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20"/><path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z"/>',
  stock: '<path d="M21 8V5a2 2 0 0 0-2-2H5a2 2 0 0 0-2 2v3"/><path d="M3 8h18v11a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8z"/><path d="M10 12h4"/>',
  transfers: '<path d="M4 7h13l-3-3"/><path d="M20 17H7l3 3"/>',
  returns: '<path d="M3 12a9 9 0 1 0 3-6.7"/><path d="M3 4v5h5"/>',
  pos: '<circle cx="9" cy="20" r="1"/><circle cx="18" cy="20" r="1"/><path d="M2 3h3l2.4 11.2a2 2 0 0 0 2 1.6h7.8a2 2 0 0 0 2-1.6L21 7H6"/>',
  reports: '<path d="M4 20V10"/><path d="M10 20V4"/><path d="M16 20v-7"/><path d="M22 20H2"/>',
  users: '<path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 0 0-3-3.9"/><path d="M16 3.1a4 4 0 0 1 0 7.8"/>',
  locations: '<path d="M21 10c0 7-9 13-9 13S3 17 3 10a9 9 0 0 1 18 0z"/><circle cx="12" cy="10" r="3"/>',
};

function navButton(tab) {
  return `<button type="button" data-tab="${tab.id}">
    <span class="nav-icon" aria-hidden="true"><svg viewBox="0 0 24 24" focusable="false">${NAV_ICONS[tab.id] || ''}</svg></span>
    <span class="nav-label">${tab.label}</span>
  </button>`;
}

const RENDER = { dashboard, books, stock, transfers: renderTransfers, returns: renderReturns, pos, reports, users, locations };

function renderCurrentTab() {
  return RENDER[state.activeTab]().then(() => {
    document.querySelectorAll('#content table').forEach(table => {
      if (table.parentElement.classList.contains('table-scroll')) return;
      const wrapper = document.createElement('div');
      wrapper.className = 'table-scroll';
      table.parentElement.insertBefore(wrapper, table);
      wrapper.appendChild(table);
    });
  });
}

async function login() {
  const u = document.getElementById('loginUser').value.trim();
  const p = document.getElementById('loginPass').value;
  try {
    const res = await api('/api/auth/login', { method: 'POST', body: { username: u, password: p }, public: true });
    state.token = res.token;
    localStorage.setItem('tk', res.token);
    state.user = res.user;
    showApp();
  } catch (e) { document.getElementById('loginError').textContent = e.message; }
}

function initials(name) {
  return String(name || '').trim().split(/\s+/).filter(Boolean).slice(0, 2)
    .map(part => part[0].toUpperCase()).join('') || 'U';
}

function setSidebarOpen(open) {
  const appView = document.getElementById('appView');
  appView.classList.toggle('sidebar-open', open);
  document.getElementById('sidebarOverlay').hidden = !open;
  document.getElementById('menuToggle').setAttribute('aria-expanded', String(open));
  document.body.classList.toggle('nav-open', open);
}

function setPageTitle(label) {
  document.getElementById('pageTitle').textContent = label;
  document.title = `${label} · ISKCON Book Inventory`;
}

function logout() {
  stopCam();
  state.token = ''; state.user = null; localStorage.removeItem('tk');
  setSidebarOpen(false);
  document.getElementById('appView').hidden = true;
  document.getElementById('loginView').hidden = false;
}

function showApp() {
  document.getElementById('loginView').hidden = true;
  document.getElementById('appView').hidden = false;
  const u = state.user;
  const loc = u.location_name || u.zone_name || 'Global';
  const fullInfo = `${u.name} (${u.role}) — ${loc}`;
  const av = initials(u.name);
  document.getElementById('userInfo').textContent = `${u.name} · ${loc}`;
  document.getElementById('userInfo').title = fullInfo;
  const roleLabel = String(u.role || '').replace(/_/g, ' ');
  document.getElementById('sidebarUserInfo').innerHTML =
    `<strong>${esc(u.name)}</strong><span>${esc(roleLabel)} · ${esc(loc)}</span>`;
  document.getElementById('userAvatar').textContent = av;
  document.getElementById('sidebarAvatar').textContent = av;
  const nav = document.getElementById('tabs');
  setSidebarOpen(false);
  const allowedTabs = TABS.filter(t => t.roles.includes(u.role));
  nav.innerHTML = TAB_GROUPS.map(group => {
    const items = group.tabs.map(id => allowedTabs.find(t => t.id === id)).filter(Boolean);
    if (!items.length) return '';
    return `<div class="nav-group"><span class="nav-group-label">${group.label}</span>${items.map(navButton).join('')}</div>`;
  }).join('');
  const availableTabs = nav.querySelectorAll('button');
  document.getElementById('appView').classList.toggle('single-tab-nav', availableTabs.length === 1);
  nav.querySelectorAll('button').forEach(b => b.addEventListener('click', () => {
    nav.querySelectorAll('button').forEach(x => x.classList.remove('active'));
    b.classList.add('active');
    state.activeTab = b.dataset.tab;
    localStorage.setItem('activeTab', state.activeTab);
    setPageTitle(b.textContent.trim());
    stopCam();
    setSidebarOpen(false);
    renderCurrentTab().catch(e => toast(e.message, 'error'));
  }));
  const savedTab = localStorage.getItem('activeTab');
  const firstTab = (savedTab && nav.querySelector(`button[data-tab="${savedTab}"]`)) || nav.querySelector('button');
  firstTab.classList.add('active');
  state.activeTab = firstTab.dataset.tab;
  setPageTitle(firstTab.textContent.trim());
  renderCurrentTab().catch(e => toast(e.message, 'error'));
}

function refreshCurrent() { if (state.user) renderCurrentTab().catch(e => toast(e.message, 'error')); }

document.getElementById('menuToggle').addEventListener('click', () => {
  setSidebarOpen(!document.getElementById('appView').classList.contains('sidebar-open'));
});
document.getElementById('sidebarOverlay').addEventListener('click', () => setSidebarOpen(false));
document.getElementById('sidebarLogout').addEventListener('click', logout);
// Any click on the page itself (not the menu) closes the open panel.
document.addEventListener('click', e => {
  const appView = document.getElementById('appView');
  if (!appView.classList.contains('sidebar-open')) return;
  if (e.target.closest('#sidebar') || e.target.closest('#menuToggle')) return;
  setSidebarOpen(false);
});
document.addEventListener('keydown', e => {
  if (e.key !== 'Escape') return;
  setSidebarOpen(false);
  closeModal();
});
window.addEventListener('resize', () => { if (window.innerWidth > 960) setSidebarOpen(false); });

function openModal(html) {
  stopCam();
  document.getElementById('modalBox').innerHTML = `<button class="modal-close" type="button" aria-label="Close window" title="Close" onclick="closeModal()">&times;</button>${html}`;
  document.getElementById('modalBackdrop').hidden = false;
}
function closeModal() {
  stopCam();
  document.getElementById('modalBackdrop').hidden = true;
}
document.getElementById('modalBackdrop').addEventListener('click', e => { if (e.target.id === 'modalBackdrop') closeModal(); });

function toast(msg, type = '') {
  const t = document.getElementById('toast');
  t.textContent = msg; t.className = type; t.hidden = false;
  setTimeout(() => t.hidden = true, 3200);
}

const scanDb = new Map();
let scanCameraHandler = null;

function scanBoxHTML(placeholder = 'Scan barcode / ISBN, press Enter', camera = false) {
  return `<div class="cart-item barcode-scan-row" style="margin-bottom:10px">
    <span style="font-size:12px;color:#8a5a1f;white-space:nowrap">Barcode scan</span>
    <input id="scanStripInput" placeholder="${esc(placeholder)}" autocomplete="off" style="flex:1;font-family:Consolas,monospace">
    ${camera ? '<button id="scanBoxCameraToggle" class="action" type="button" onclick="openScannerHere()">Scan with camera</button>' : ''}
  </div>`;
}

function bindScan(onScan, onInput, keepFocus = true) {
  const input = document.getElementById('scanStripInput');
  if (!input) return;
  const handleCode = async code => {
    const b = await api('/api/books/lookup/' + encodeURIComponent(code));
    scanDb.set('last', b);
    onScan(b);
    toast(b.title + ' matched', 'success');
    return b.title + ' matched';
  };
  scanCameraHandler = handleCode;
  input.focus();
  input.addEventListener('keydown', async (e) => {
    if (e.key !== 'Enter') return;
    const code = input.value.trim();
    input.value = '';
    if (!code) return;
    try { await handleCode(code); } catch (err) { toast(err.message, 'error'); }
    if (keepFocus) input.focus();
  });
  if (onInput) input.addEventListener('input', () => onInput(input.value));
  return input;
}

async function pageScanStrip(onScan, placeholder = 'Scan barcode / ISBN, press Enter', camera = false) {
  const holder = document.createElement('div');
  holder.id = 'pageScanStrip';
  holder.innerHTML = scanBoxHTML(placeholder, camera);
  document.getElementById('content').prepend(holder);
  return bindScan(onScan, null);
}

async function dashboard() {
  const [stock, sales, tf] = await Promise.all([
    api('/api/reports/stock-summary'), api('/api/reports/sales'), api('/api/reports/transfers'),
  ]);
  const totalUnits = stock.reduce((a, s) => a + (s.total_units || 0), 0);
  const warehouseUnits = stock.filter(s => s.type === 'warehouse' && s.parent_id == null)
    .reduce((a, s) => a + (s.total_units || 0), 0);
  const totalValue = stock.reduce((a, s) => a + (s.stock_value || 0), 0);
  const revenue = sales.reduce((a, s) => a + (s.revenue || 0), 0);
  const open = tf.filter(t => t.status === 'requested').reduce((a, t) => a + t.n, 0);
  document.getElementById('content').innerHTML = `
    <div class="stats">
      <div class="stat stock-units-stat"><div class="num">${totalUnits}</div><div class="lbl">Total stock (all locations)</div></div>
      <div class="stat warehouse-units-stat"><div class="num">${warehouseUnits}</div><div class="lbl">Warehouse stock available to issue</div></div>
      <div class="stat stock-value-stat"><div class="num">&#8377;${totalValue.toFixed(0)}</div><div class="lbl">Stock value</div></div>
      <div class="stat"><div class="num">&#8377;${revenue.toFixed(0)}</div><div class="lbl">Sales revenue</div></div>
      <div class="stat"><div class="num">${open}</div><div class="lbl">Open transfer requests</div></div>
    </div>
    <div class="card stock-summary-card"><h2>Stock summary by location</h2>
      <table class="stock-summary-table"><tr><th>Location</th><th>Type</th><th>SKUs</th><th>Units</th><th>Value</th></tr>
        ${stock.map(s => `<tr><td>${esc(s.name)}</td><td><span class="stock-type ${esc(s.type)}">${esc(s.type)}</span></td><td>${s.skus}</td><td><span class="stock-quantity">${s.total_units}</span></td><td><span class="stock-value-text">&#8377;${(s.stock_value || 0).toFixed(0)}</span></td></tr>`).join('')}
      </table>
    </div>
    <div class="card"><h2>Sales by location</h2>
      <table><tr><th>Location</th><th>Invoices</th><th>Subtotal</th><th>Discount</th><th>Revenue</th></tr>
        ${sales.map(s => `<tr><td>${esc(s.location_name)}</td><td>${s.invoice_count}</td><td>&#8377;${s.subtotal.toFixed(2)}</td><td>&#8377;${s.discount.toFixed(2)}</td><td>&#8377;${s.revenue.toFixed(2)}</td></tr>`).join('')}
      </table>
    </div>
    <div class="card"><h2>Transfers by status</h2>
      <table><tr><th>Status</th><th>Count</th></tr>
        ${tf.map(t => `<tr><td><span class="badge ${esc(t.status)}">${esc(t.status)}</span></td><td>${t.n}</td></tr>`).join('')}
      </table>
    </div>`;
}

let bookQuery = '';
let bookSearchTimer = null;

function canManageBooks() { return ['super_admin', 'zone_admin'].includes(state.user.role); }

async function books() {
  const canEdit = canManageBooks();
  document.getElementById('content').innerHTML = `
    <div class="card">
      <div class="toolbar">
        <input id="bookSearch" placeholder="Search title / ISBN / barcode" style="flex:1" value="${esc(bookQuery)}">
        ${canEdit ? '<button class="primary" onclick="bookForm()">+ New book</button>' : ''}
      </div>
      ${canEdit ? '' : '<p class="hint">Only Super Admin and Zone Admin accounts can add or edit books.</p>'}
      <table>
        <tr><th>Title</th><th>Barcode</th><th>ISBN</th><th>Language</th><th>Publication</th><th>Price</th>${canEdit ? '<th>Actions</th>' : ''}</tr>
        <tbody id="bookRows"></tbody>
      </table>
    </div>`;
  const input = document.getElementById('bookSearch');
  input.addEventListener('input', () => {
    bookQuery = input.value;
    clearTimeout(bookSearchTimer);
    bookSearchTimer = setTimeout(loadBookRows, 250);
  });
  await loadBookRows();
  if (['super_admin', 'zone_admin', 'distributor', 'temple'].includes(state.user.role)) {
    pageScanStrip(book => {
      bookQuery = book.barcode;
      refreshCurrent();
    }, 'Scan a book barcode to highlight it in the list', true);
  }
}

async function loadBookRows() {
  const rows = await api('/api/books' + (bookQuery ? '?q=' + encodeURIComponent(bookQuery) : ''));
  state.books = rows;
  const canEdit = canManageBooks();
  const body = document.getElementById('bookRows');
  if (!body) return;
  body.innerHTML = rows.map(b => `<tr ${b.barcode === (scanDb.get('last')?.barcode) ? 'class="hl"' : ''}>
    <td>${esc(b.title)}</td><td>${esc(b.barcode)}</td><td>${esc(b.isbn || '')}</td><td>${esc(b.language || '')}</td><td>${esc(b.publication || '')}</td><td>&#8377;${b.price}</td>
    ${canEdit ? `<td><button class="action" onclick="bookForm(${b.id})">Edit</button><button class="danger" onclick="bookDelete(${b.id})">Delete</button></td>` : ''}
  </tr>`).join('') || `<tr><td colspan="${canEdit ? 7 : 6}" class="hint">No books found.</td></tr>`;
}

function bookForm(bookId = null) {
  const b = bookId ? (state.books || []).find(x => x.id === bookId) : null;
  openModal(`<h3>${b ? 'Edit book' : 'New book'}</h3>
    <div class="form-grid">
      <div class="form-row"><label>Title *</label><input id="bkTitle" value="${esc(b?.title || '')}"></div>
      <div class="form-row"><label>Barcode *</label><input id="bkBarcode" placeholder="e.g. 9780892131343" value="${esc(b?.barcode || '')}"></div>
      <div class="form-row"><label>ISBN</label><input id="bkIsbn" placeholder="e.g. 9780892131343" value="${esc(b?.isbn || '')}"></div>
      <div class="form-row"><label>Language</label><input id="bkLang" value="${esc(b?.language || '')}"></div>
      <div class="form-row"><label>Publication</label><input id="bkPub" placeholder="e.g. Bhaktivedanta Book Trust (BBT)" value="${esc(b?.publication || '')}"></div>
      <div class="form-row"><label>Price (&#8377;)</label><input id="bkPrice" type="number" min="0" value="${b?.price ?? 0}"></div>
    </div>
    <button class="primary" onclick="bookSave(${b ? b.id : 'null'})">${b ? 'Save changes' : 'Create book'}</button>`);
}

async function bookSave(bookId) {
  const body = {
    title: document.getElementById('bkTitle').value.trim(),
    barcode: document.getElementById('bkBarcode').value.trim(),
    isbn: document.getElementById('bkIsbn').value.trim() || null,
    language: document.getElementById('bkLang').value.trim() || null,
    publication: document.getElementById('bkPub').value.trim() || null,
    price: Number(document.getElementById('bkPrice').value) || 0,
  };
  if (!body.title || !body.barcode) { toast('Title and barcode are required', 'error'); return; }
  try {
    if (bookId) await api('/api/books/' + bookId, { method: 'PATCH', body });
    else await api('/api/books', { method: 'POST', body });
    closeModal(); toast(bookId ? 'Book updated' : 'Book added', 'success'); refreshCurrent();
  } catch (e) { toast(e.message, 'error'); }
}

async function bookDelete(bookId) {
  const b = (state.books || []).find(x => x.id === bookId);
  if (!confirm(`Delete book "${b ? b.title : bookId}"? This cannot be undone.`)) return;
  try {
    await api('/api/books/' + bookId, { method: 'DELETE' });
    toast('Book deleted', 'success'); refreshCurrent();
  } catch (e) { toast(e.message, 'error'); }
}

let stockLocFilter = '';
async function stock() {
  const locs = await api('/api/locations');
  const rows = await api('/api/stock' + (stockLocFilter ? '?location_id=' + stockLocFilter : ''));
  const role = state.user.role;
  const adjustmentWarehouse = locs.find(l => l.type === 'warehouse' &&
    (role === 'super_admin' || (role === 'temple' && l.id === state.user.location_id)));
  document.getElementById('content').innerHTML = `
    <div class="card">
      <div class="toolbar">
        <select id="stockLoc" onchange="stockLocFilter=this.value; refreshCurrent()">
          <option value="">All locations</option>
          ${locs.map(l => `<option value="${l.id}" ${String(l.id) === stockLocFilter ? 'selected' : ''}>${esc(l.name)}</option>`).join('')}
        </select>
        ${adjustmentWarehouse ? '<button class="primary" onclick="stockAdjustModal()">+ Receive stock at warehouse</button>' : ''}
        <button class="action" onclick="stockCountModal()">Start stock count</button>
        <button class="action" onclick="showLedger()">Stock ledger</button>
      </div>
      <table class="stock-inventory-table">
        <tr><th>Location</th><th>Title</th><th>Barcode</th><th>Publication</th><th>Qty on hand</th><th>Price</th><th>Value</th><th>Actions</th></tr>
        ${rows.map(r => `<tr ${r.barcode === (scanDb.get('last')?.barcode) ? 'class="hl"' : ''}><td>${esc(r.location_name)}</td><td>${esc(r.title)}</td><td>${esc(r.barcode)}</td><td>${esc(r.publication || '')}</td><td><span class="stock-quantity ${r.qty > 0 ? 'available' : 'empty'}">${r.qty}</span></td><td>&#8377;${r.price}</td><td><span class="stock-value-text">&#8377;${(r.qty * r.price).toFixed(2)}</span></td><td>${adjustmentWarehouse && r.location_id === adjustmentWarehouse.id ? `<button class="action" onclick="stockAdjustModal(${r.book_id}, ${r.location_id})">Adjust</button>` : ''}<button class="action" onclick="showLedger(${r.location_id}, ${r.book_id})">Ledger</button></td></tr>`).join('') || '<tr><td colspan="8" class="hint">No stock rows found.</td></tr>'}
      </table>
    </div>`;
  pageScanStrip(() => refreshCurrent(), 'Scan a barcode to highlight its stock row', true);
}

async function stockAdjustModal(bookId = null, locationId = null) {
  const canAdjust = ['super_admin', 'temple'].includes(state.user.role);
  if (!canAdjust) { toast('Only warehouse staff can adjust stock', 'error'); return; }
  const [locs, books] = await Promise.all([api('/api/locations?type=warehouse'), api('/api/books')]);
  const allowedLocations = locs.filter(l => l.type === 'warehouse' && l.parent_id == null &&
    (state.user.role === 'super_admin' || l.id === state.user.location_id));
  if (!allowedLocations.length) { toast('No assigned warehouse is available for stock entry', 'error'); return; }
  state.books = books;
  openModal(`<h3>Receive / adjust warehouse stock</h3>
    <div class="form-grid">
      <div class="form-row"><label>Warehouse</label><select id="saLoc">${allowedLocations.map(l => `<option value="${l.id}">${esc(l.name)}</option>`).join('')}</select></div>
      <div class="form-row"><label>Book</label><select id="saBook"><option value="">Select book</option>${books.map(b => `<option value="${b.id}">${esc(b.title)} (${esc(b.barcode)})</option>`).join('')}</select></div>
      <div class="form-row"><label>Direction</label><select id="saDir"><option value="in">Stock in (+)</option><option value="out">Stock out (-)</option></select></div>
      <div class="form-row"><label>Quantity</label><input id="saQty" type="number" min="1" value="1"></div>
    </div>
    <div class="form-row"><label>Note / reason</label><input id="saNote" placeholder="e.g. opening stock, purchase, correction"></div>
    <button class="primary" onclick="stockAdjustSubmit()">Save</button>`);
  if (locationId) document.getElementById('saLoc').value = String(locationId);
  if (bookId) document.getElementById('saBook').value = String(bookId);
}

async function stockAdjustSubmit() {
  const bookId = Number(document.getElementById('saBook').value);
  if (!bookId) { toast('Select a book', 'error'); return; }
  const qty = Number(document.getElementById('saQty').value) || 0;
  if (qty <= 0) { toast('Quantity must be positive', 'error'); return; }
  const direction = document.getElementById('saDir').value;
  try {
    const res = await api('/api/stock/adjust', { method: 'POST', body: {
      book_id: bookId,
      location_id: Number(document.getElementById('saLoc').value),
      qty: direction === 'in' ? qty : -qty,
      note: document.getElementById('saNote').value.trim() || null,
    }});
    closeModal(); toast('Stock updated. New quantity: ' + res.qty, 'success'); refreshCurrent();
  } catch (e) { toast(e.message, 'error'); }
}

async function showLedger(locationId = null, bookId = null) {
  const params = new URLSearchParams({ limit: '200' });
  const loc = locationId ?? stockLocFilter;
  if (loc) params.set('location_id', loc);
  if (bookId) params.set('book_id', bookId);
  const rows = await api('/api/stock/ledger?' + params.toString());
  const book = bookId ? (state.books || []).find(b => b.id === bookId) : null;
  openModal(`<h3>Stock ledger (movements)${book ? ' — ' + esc(book.title) : ''}</h3>
    <table>
      <tr><th>Time</th><th>Location</th><th>Title</th><th>Type</th><th>Qty</th><th>Ref</th><th>Note</th><th>User</th></tr>
      ${rows.map(m => `<tr><td>${esc(m.created_at)}</td><td>${esc(m.location_code)}</td><td>${esc(m.title)}</td><td>${esc(m.change_type)}</td><td>${m.qty}</td><td>${esc(m.ref_type || '')}${m.ref_id ? '#' + m.ref_id : ''}</td><td>${esc(m.note || '')}</td><td>${esc(m.user_name || '')}</td></tr>`).join('') || '<tr><td colspan="8" class="hint">No movements yet.</td></tr>'}
    </table>`);
}

async function stockCountModal() {
  const locs = await api('/api/locations');
  openModal(`<h3>Start stock count</h3>
    <div class="form-row"><label>Location</label><select id="scLoc">${locs.map(l => `<option value="${l.id}">${esc(l.name)}</option>`).join('')}</select></div>
    <button class="primary" onclick="scCreate()">Create count</button>`);
}

async function scCreate() {
  const loc = document.getElementById('scLoc').value;
  const res = await api('/api/stock/counts', { method: 'POST', body: { location_id: Number(loc) } });
  const rows = await api('/api/stock?location_id=' + loc);
  state.scCountId = res.count_id;
  openModal(`<h3>Stock count #${res.count_id} — enter counted quantities</h3>
    ${scanBoxHTML('Scan a barcode to jump to that book row', true)}
    <table id="scTable"><tr><th>Title</th><th>System qty</th><th>Counted</th></tr>
      ${rows.map(r => `<tr data-book="${r.book_id}" data-barcode="${esc(r.barcode)}"><td>${esc(r.title)}</td><td>${r.qty}</td><td><input type="number" min="0" value="${r.qty}" data-book="${r.book_id}"></td></tr>`).join('')}
    </table>
    <br><button class="primary" onclick="scComplete()">Complete count</button>`);
  bindScan(b => {
    const row = document.querySelector(`#scTable tr[data-barcode="${b.barcode}"]`);
    if (row) { row.scrollIntoView({ block: 'center' }); row.style.background = '#fff3cd'; row.querySelector('input').focus(); }
    else toast('No count row for that book', 'error');
  });
}

async function scComplete() {
  try {
    const items = [...document.querySelectorAll('#scTable input[data-book]')].map(i => ({ book_id: Number(i.dataset.book), counted_qty: Number(i.value) || 0 }));
    await api(`/api/stock/counts/${state.scCountId}/items`, { method: 'POST', body: items });
    const done = await api(`/api/stock/counts/${state.scCountId}/complete`, { method: 'POST' });
    closeModal(); toast('Count completed. Variances: ' + JSON.stringify(done.variances), 'success'); refreshCurrent();
  } catch (e) { toast(e.message, 'error'); }
}

async function renderTransfers() {
  const rows = await api('/api/stock/transfers');
  const role = state.user.role;
  document.getElementById('content').innerHTML = `
    <div class="card">
      <div class="toolbar"><button class="primary" onclick="transferForm()">+ Request transfer</button></div>
      <table>
        <tr><th>#</th><th>From</th><th>To</th><th>Items</th><th>Status</th><th>Requested by</th><th>Actions</th></tr>
        ${rows.map(t => `<tr>
          <td>${esc(t.request_number)}</td><td>${esc(t.from_name)}</td><td>${esc(t.to_name)}</td>
          <td><button class="action" onclick="transferDetail(${t.id})">view</button></td>
          <td><span class="badge ${esc(t.status)}">${esc(t.status)}</span></td>
          <td>${esc(t.requested_by_name || '')}</td>
          <td>${transferActions(t, role)}</td>
        </tr>`).join('')}
      </table>
    </div>`;
}

function transferActions(t, role) {
  let h = '';
  if (t.status === 'requested' && (role === 'super_admin' || role === 'zone_admin'))
    h += `<button class="action" onclick="tAction(${t.id},'approve')">Approve</button><button class="danger" onclick="tAction(${t.id},'reject')">Reject</button>`;
  if (t.status === 'approved' && (role === 'super_admin' || role === 'zone_admin' || role === 'distributor'))
    h += `<button class="action" onclick="tAction(${t.id},'dispatch')">Dispatch</button>`;
  if (t.status === 'dispatched' && (role === 'super_admin' || role === 'temple' || role === 'zone_admin' || role === 'distributor'))
    h += `<button class="action" onclick="tAction(${t.id},'receive')">Receive</button>`;
  if ((t.status === 'requested' || t.status === 'rejected') && (role === 'super_admin' || role === 'zone_admin'))
    h += `<button class="danger" onclick="tDelete(${t.id})">Delete</button>`;
  return h || '&mdash;';
}

async function tAction(id, action) {
  if (!confirm(`Confirm: ${action} transfer #${id}?`)) return;
  try { await api(`/api/stock/transfers/${id}/${action}`, { method: 'POST' }); toast('Transfer ' + action + 'd', 'success'); refreshCurrent(); }
  catch (e) { toast(e.message, 'error'); }
}

async function tDelete(id) {
  if (!confirm(`Delete transfer request #${id}?`)) return;
  try { await api('/api/stock/transfers/' + id, { method: 'DELETE' }); toast('Transfer deleted', 'success'); refreshCurrent(); }
  catch (e) { toast(e.message, 'error'); }
}

async function transferDetail(id) {
  const t = await api('/api/stock/transfers/' + id);
  openModal(`<h3>Transfer ${esc(t.request_number)} — <span class="badge ${esc(t.status)}">${esc(t.status)}</span></h3>
    <p style="font-size:13px">From: ${esc(t.from_name)} &rarr; To: ${esc(t.to_name)}<br>${esc(t.note || '')}</p>
    <table><tr><th>Title</th><th>Barcode</th><th>Qty</th><th>Price</th></tr>
      ${t.items.map(i => `<tr><td>${esc(i.title)}</td><td>${esc(i.barcode)}</td><td>${i.qty}</td><td>&#8377;${i.price}</td></tr>`).join('')}
    </table>`);
}

let trItems = [];
async function transferForm() {
  trItems = [{ book_id: '', qty: 1 }];
  const [locs, books] = await Promise.all([api('/api/locations'), api('/api/books')]);
  const sources = locs.filter(l => l.type === 'warehouse' && l.parent_id == null);
  const destinations = locs.filter(l => l.type !== 'warehouse');
  if (!sources.length || !destinations.length) {
    toast('A root warehouse and at least one zone or store are required for stock issue', 'error');
    return;
  }
  state.books = books;
  openModal(`<h3>Request stock transfer</h3>
    <div class="form-grid">
      <div class="form-row"><label>From warehouse</label><select id="tfFrom">${sources.map(l => `<option value="${l.id}">${esc(l.name)}</option>`).join('')}</select></div>
      <div class="form-row"><label>To zone or store</label><select id="tfTo">${destinations.map(l => `<option value="${l.id}">${esc(l.name)}</option>`).join('')}</select></div>
    </div>
    <div class="form-row"><label>Note</label><input id="tfNote"></div>
    ${scanBoxHTML('Scan barcode to add book to transfer', true)}
    <div id="trItems"></div>
    <button class="action" onclick="trAddRow()">+ Add item</button>
    <br><br><button class="primary" onclick="trSubmit()">Submit request</button>`);
  trRenderRows();
  bindScan(b => {
    trItems = trItems.filter(i => i.book_id);
    const ex = trItems.find(i => i.book_id === b.id);
    if (ex) ex.qty++;
    else trItems.push({ book_id: b.id, qty: 1 });
    trRenderRows();
  });
}

function trRenderRows() {
  const books = state.books || [];
  document.getElementById('trItems').innerHTML = trItems.map((it, i) => `
    <div class="cart-item" style="margin-bottom:8px">
      <select style="flex:1" onchange="trSet(${i},'book_id',this.value)">
        <option value="">Select book</option>
        ${books.map(b => `<option value="${b.id}" ${String(b.id) === String(it.book_id) ? 'selected' : ''}>${esc(b.title)} (${esc(b.barcode)})</option>`).join('')}
      </select>
      <input type="number" min="1" value="${it.qty}" style="width:70px" onchange="trSet(${i},'qty',this.value)">
      <button class="danger" onclick="trDel(${i})">x</button>
    </div>`).join('');
}

function trSet(i, key, val) { trItems[i][key] = Number(val); }
function trAddRow() { trItems.push({ book_id: '', qty: 1 }); trRenderRows(); }
function trDel(i) { trItems.splice(i, 1); trRenderRows(); }

async function trSubmit() {
  const items = trItems.filter(i => i.book_id).map(i => ({ book_id: i.book_id, qty: i.qty }));
  if (!items.length) { toast('Add at least one item', 'error'); return; }
  try {
    await api('/api/stock/transfers', { method: 'POST', body: {
      from_location_id: Number(document.getElementById('tfFrom').value),
      to_location_id: Number(document.getElementById('tfTo').value),
      note: document.getElementById('tfNote').value, items,
    }});
    closeModal(); toast('Transfer requested', 'success'); refreshCurrent();
  } catch (e) { toast(e.message, 'error'); }
}

async function renderReturns() {
  const [rows, damaged] = await Promise.all([api('/api/stock/returns'), api('/api/stock/damaged')]);
  const canProc = ['super_admin', 'zone_admin'].includes(state.user.role);
  document.getElementById('content').innerHTML = `
    <div class="card">
      <div class="toolbar">
        <button class="primary" onclick="returnForm()">+ Record return</button>
        <button class="primary" onclick="damagedForm()">Write off damaged</button>
      </div>
      <h2>Returns</h2>
      <table>
        <tr><th>Return #</th><th>Location</th><th>Reason</th><th>Units</th><th>Status</th><th>Actions</th></tr>
        ${rows.map(r => `<tr>
          <td>${esc(r.return_number)}</td><td>${esc(r.location_name)}</td><td>${esc(r.reason || '')}</td><td>${r.units}</td>
          <td><span class="badge ${esc(r.status)}">${esc(r.status)}</span></td>
          <td>${(r.status === 'recorded' && canProc) ? `<button class="action" onclick="processReturn(${r.id})">Process</button><button class="danger" onclick="rDelete(${r.id})">Delete</button>` : '&mdash;'}</td>
        </tr>`).join('') || '<tr><td colspan="6" class="hint">No returns recorded.</td></tr>'}
      </table>
    </div>
    <div class="card"><h2>Damaged write-offs</h2>
      <table><tr><th>Time</th><th>Location</th><th>Title</th><th>Qty</th><th>Reason</th><th>Actions</th></tr>
        ${damaged.map(d => `<tr><td>${esc(d.created_at)}</td><td>${esc(d.location_code)}</td><td>${esc(d.title)}</td><td>${d.qty}</td><td>${esc(d.reason_code)}</td><td>${canProc ? `<button class="danger" onclick="dDelete(${d.id})">Undo</button>` : '&mdash;'}</td></tr>`).join('') || '<tr><td colspan="6" class="hint">No damaged write-offs.</td></tr>'}
      </table>
    </div>`;
}

let rtItems = [];
async function returnForm() {
  rtItems = [{ book_id: '', qty: 1 }];
  const [locs, books] = await Promise.all([api('/api/locations'), api('/api/books')]);
  state.books = books;
  openModal(`<h3>Record return</h3>
    <div class="form-row"><label>Location</label><select id="rtLoc">${locs.map(l => `<option value="${l.id}">${esc(l.name)}</option>`).join('')}</select></div>
    <div class="form-row"><label>Reason</label><input id="rtReason"></div>
    ${scanBoxHTML('Scan barcode to add book to return', true)}
    <div id="rtItems"></div>
    <button class="action" onclick="rtAddRow()">+ Add item</button>
    <br><br><button class="primary" onclick="rtSubmit()">Record return</button>`);
  rtRenderRows();
  bindScan(b => {
    rtItems = rtItems.filter(i => i.book_id);
    const ex = rtItems.find(i => i.book_id === b.id);
    if (ex) ex.qty++;
    else rtItems.push({ book_id: b.id, qty: 1 });
    rtRenderRows();
  });
}

function rtRenderRows() {
  const books = state.books || [];
  document.getElementById('rtItems').innerHTML = rtItems.map((it, i) => `
    <div class="cart-item" style="margin-bottom:8px">
      <select style="flex:1" onchange="rtSet(${i},'book_id',this.value)">
        <option value="">Select book</option>
        ${books.map(b => `<option value="${b.id}" ${String(b.id) === String(it.book_id) ? 'selected' : ''}>${esc(b.title)} (${esc(b.barcode)})</option>`).join('')}
      </select>
      <input type="number" min="1" value="${it.qty}" style="width:70px" onchange="rtSet(${i},'qty',this.value)">
      <button class="danger" onclick="rtDel(${i})">x</button>
    </div>`).join('');
}

function rtSet(i, key, val) { rtItems[i][key] = Number(val); }
function rtAddRow() { rtItems.push({ book_id: '', qty: 1 }); rtRenderRows(); }
function rtDel(i) { rtItems.splice(i, 1); rtRenderRows(); }

async function rtSubmit() {
  const items = rtItems.filter(i => i.book_id).map(i => ({ book_id: i.book_id, qty: i.qty }));
  if (!items.length) { toast('Add at least one item', 'error'); return; }
  try {
    await api('/api/stock/returns', { method: 'POST', body: {
      location_id: Number(document.getElementById('rtLoc').value),
      reason: document.getElementById('rtReason').value, items,
    }});
    closeModal(); toast('Return recorded', 'success'); refreshCurrent();
  } catch (e) { toast(e.message, 'error'); }
}

async function processReturn(id) {
  const locs = await api('/api/locations');
  openModal(`<h3>Process return #${id}</h3>
    <div class="form-row"><label>Credit stock to location</label>
      <select id="prLoc">${locs.map(l => `<option value="${l.id}">${esc(l.name)}</option>`).join('')}</select>
    </div>
    <button class="primary" onclick="prSubmit(${id})">Process return</button>`);
}

async function prSubmit(id) {
  try {
    await api(`/api/stock/returns/${id}/process?credit_location_id=` + document.getElementById('prLoc').value, { method: 'PATCH' });
    closeModal(); toast('Return processed and credited', 'success'); refreshCurrent();
  } catch (e) { toast(e.message, 'error'); }
}

async function rDelete(id) {
  if (!confirm(`Delete return #${id}?`)) return;
  try { await api('/api/stock/returns/' + id, { method: 'DELETE' }); toast('Return deleted', 'success'); refreshCurrent(); }
  catch (e) { toast(e.message, 'error'); }
}

async function dDelete(id) {
  if (!confirm(`Undo damaged write-off #${id}? Stock will be restored.`)) return;
  try { await api('/api/stock/damaged/' + id, { method: 'DELETE' }); toast('Write-off reversed', 'success'); refreshCurrent(); }
  catch (e) { toast(e.message, 'error'); }
}

async function damagedForm() {
  const [locs, books] = await Promise.all([api('/api/locations'), api('/api/books')]);
  state.books = books;
  openModal(`<h3>Write off damaged stock</h3>
    <div class="form-row"><label>Location</label><select id="dmLoc">${locs.map(l => `<option value="${l.id}">${esc(l.name)}</option>`).join('')}</select></div>
    ${scanBoxHTML('Scan barcode to select book', true)}
    <div class="form-row"><label>Book</label>
      <select id="dmBook"><option value="">Select book</option>
        ${books.map(b => `<option value="${b.id}">${esc(b.title)} (${esc(b.barcode)})</option>`).join('')}
      </select>
    </div>
    <div class="form-grid">
      <div class="form-row"><label>Qty</label><input id="dmQty" type="number" min="1" value="1"></div>
      <div class="form-row"><label>Reason code</label>
        <select id="dmReason">
          <option value="DAMAGED">Damaged</option><option value="STAINED">Stained</option>
          <option value="MISSING_PAGES">Missing pages</option><option value="PRINT_ERROR">Print error</option>
          <option value="OTHER">Other</option>
        </select>
      </div>
    </div>
    <div class="form-row"><label>Note</label><input id="dmNote"></div>
    <button class="primary" onclick="dmSubmit()">Write off</button>`);
  bindScan(b => {
    const sel = document.getElementById('dmBook');
    const opt = [...sel.options].find(o => String(o.value) === String(b.id));
    if (opt) { sel.value = opt.value; }
    else toast('Book not in list', 'error');
  });
}

async function dmSubmit() {
  try {
    await api('/api/stock/damaged', { method: 'POST', body: {
      location_id: Number(document.getElementById('dmLoc').value),
      book_id: Number(document.getElementById('dmBook').value),
      qty: Number(document.getElementById('dmQty').value) || 1,
      reason_code: document.getElementById('dmReason').value,
      note: document.getElementById('dmNote').value,
    }});
    closeModal(); toast('Damaged stock written off', 'success'); refreshCurrent();
  } catch (e) { toast(e.message, 'error'); }
}

async function pos() {
  const [books, locs] = await Promise.all([api('/api/books'), api('/api/locations')]);
  state.books = books;
  state.cart = [];
  const isDistributor = state.user.role === 'distributor';
  const defaultWarehouse = locs.find(l => l.type === 'warehouse' && l.parent_id == null);
  const defLoc = isDistributor
    ? state.user.zone_id
    : state.user.location_id || state.user.zone_id ||
      (state.user.role === 'super_admin' ? defaultWarehouse?.id || locs[0]?.id : null);
  const billingLocation = locs.find(l => l.id === (isDistributor ? state.user.zone_id : defLoc));
  if (!billingLocation && state.user.role !== 'super_admin') {
    toast('No assigned billing location is available', 'error'); return;
  }
  document.getElementById('content').innerHTML = `
    <div class="card">
      <h2>Quick-scan billing</h2>
      <div class="toolbar pos-scan-toolbar" id="scanBox">
        ${isDistributor ? `<span class="billing-location">Billing zone: <strong>${esc(billingLocation.name)}</strong></span>` : `<select id="billLoc" style="max-width:220px">${locs.map(l => `<option value="${l.id}" ${String(l.id) === String(defLoc) ? 'selected' : ''}>${esc(l.name)}</option>`).join('')}</select>`}
        <input id="scanInput" placeholder="Scan / type barcode or ISBN, press Enter" style="flex:1" onkeydown="if(event.key==='Enter')doScan(this.value)">
        <button id="cameraToggle" class="primary" onclick="openScannerPOS()">Scan with camera</button>
        <button class="action" onclick="invoiceList()">Invoice history</button>
        <select id="manualBook" onchange="posAddBySelect(this.value)" style="max-width:260px">
          <option value="">&mdash; manual search &mdash;</option>
          ${books.map(b => `<option value="${b.barcode}">${esc(b.title)}</option>`).join('')}
        </select>
      </div>
      <table id="cartTable">
        <tr><th>Book</th><th>Price</th><th>Qty</th><th>Total</th><th></th></tr>
      </table>
      <div id="cartEmpty" style="color:#8a6d3b;font-size:13px;margin-top:8px">Cart is empty. Scan a book to begin.</div>
      <div class="form-grid" style="margin-top:14px">
        <div class="form-row"><label>Customer name</label><input id="custName"></div>
        <div class="form-row"><label>Phone</label><input id="custPhone"></div>
        <div class="form-row"><label>Email</label><input id="custEmail"></div>
        <div class="form-row"><label>Payment mode</label>
          <select id="payMode" onchange="toggleUpiQr()"><option value="cash">Cash</option><option value="upi">UPI</option><option value="card">Card</option><option value="ledger">Credit / Ledger</option></select>
        </div>
        <div class="form-row"><label>Payment ref (optional)</label><input id="payRef"></div>
        <div class="form-row"><label>Discount (&#8377;)</label><input id="discount" type="number" min="0" value="0" oninput="renderCart()"></div>
      </div>
      <div id="upiQrPanel" class="upi-qr-panel" hidden>
        <p>Scan this QR code to pay with UPI.</p>
        <img src="/upi-qr.jpeg" alt="ISKCON Jamnagar Books and Gifts UPI payment QR code">
      </div>
      <div style="margin-top:14px;display:flex;justify-content:space-between;align-items:center">
        <div><strong>Total:</strong> <span id="cartTotal">&#8377;0.00</span></div>
        <button class="primary" onclick="checkout()">Checkout</button>
      </div>
    </div>`;
  renderCart();
  toggleUpiQr();
  document.getElementById('scanInput').focus();
}

function toggleUpiQr() {
  const panel = document.getElementById('upiQrPanel');
  if (panel) panel.hidden = document.getElementById('payMode').value !== 'upi';
}

function addToCart(b) {
  const ex = state.cart.find(i => i.barcode === b.barcode);
  if (ex) ex.qty++;
  else state.cart.push({ barcode: b.barcode, title: b.title, price: b.price, qty: 1 });
  renderCart();
}

function posAddBySelect(val) {
  document.getElementById('manualBook').value = '';
  if (!val) return;
  (async () => {
    try { const b = await api('/api/books/lookup/' + encodeURIComponent(val)); addToCart(b); toast(b.title + ' added', 'success'); }
    catch (e) { toast(e.message, 'error'); }
  })();
}

async function doScan(code, opts = {}) {
  const c = document.getElementById('scanInput');
  if (c) c.value = '';
  if (!code) return;
  try {
    const b = await api('/api/books/lookup/' + encodeURIComponent(code.trim()));
    addToCart(b);
    toast(b.title + ' added', 'success');
    if (c) c.focus();
    return b.title + ' added';
  } catch (e) {
    if (!opts.rethrow) toast(e.message, 'error');
    if (c) c.focus();
    if (opts.rethrow) throw e;
  }
}

function setCartQty(i, v) { state.cart[i].qty = Math.max(1, Number(v) || 1); renderCart(); }
function removeCart(i) { state.cart.splice(i, 1); renderCart(); }

function renderCart() {
  const table = document.getElementById('cartTable');
  if (!table) return;
  const subtotal = state.cart.reduce((a, i) => a + i.price * i.qty, 0);
  const disc = Number(document.getElementById('discount')?.value) || 0;
  const total = Math.max(0, subtotal - disc);
  document.getElementById('cartEmpty').hidden = state.cart.length > 0;
  table.innerHTML = `<tr><th>Book</th><th>Price</th><th>Qty</th><th>Total</th><th></th></tr>` +
    state.cart.map((i, idx) => `<tr>
      <td><span class="cart-book-title">${esc(i.title)}</span><span class="cart-book-barcode">${esc(i.barcode)}</span></td>
      <td>&#8377;${i.price.toFixed(2)}</td>
      <td><input type="number" min="1" value="${i.qty}" style="width:60px" onchange="setCartQty(${idx},this.value)"></td>
      <td>&#8377;${(i.price * i.qty).toFixed(2)}</td>
      <td><button class="danger" onclick="removeCart(${idx})">x</button></td>
    </tr>`).join('');
  document.getElementById('cartTotal').textContent = `&#8377;${total.toFixed(2)}`;
  document.getElementById('cartTotal').innerHTML = `&#8377;${total.toFixed(2)}`;
}

let zxingLoadPromise = null;

const SCAN_FORMATS = [
  'qr_code', 'ean_13', 'ean_8', 'upc_a', 'upc_e', 'code_128', 'code_39', 'code_93',
  'itf', 'codabar', 'data_matrix', 'pdf417', 'aztec',
];

const scanner = {
  session: 0, stream: null, detector: null, reader: null, canvas: null, timer: null,
  open: false, facing: 'environment', torch: false, lastCode: '', lastAt: 0,
  onResult: null, continuous: false, hits: 0, nativeMissing: false, playing: false,
};

function scannerEl(id) { return document.getElementById(id); }

function scannerStatus(text, kind = '') {
  const el = scannerEl('scanStatus');
  if (!el) return;
  el.textContent = text;
  el.className = 'scanner-status' + (kind ? ' ' + kind : '');
}

let scanAudioCtx = null;

function scanBeep() {
  try {
    const Ctx = window.AudioContext || window.webkitAudioContext;
    if (Ctx) {
      if (!scanAudioCtx) scanAudioCtx = new Ctx();
      if (scanAudioCtx.state === 'suspended') scanAudioCtx.resume().catch(() => {});
      const osc = scanAudioCtx.createOscillator();
      const gain = scanAudioCtx.createGain();
      const t = scanAudioCtx.currentTime;
      osc.type = 'sine';
      osc.frequency.value = 1150;
      gain.gain.setValueAtTime(0.0001, t);
      gain.gain.exponentialRampToValueAtTime(0.2, t + 0.01);
      gain.gain.exponentialRampToValueAtTime(0.0001, t + 0.12);
      osc.connect(gain).connect(scanAudioCtx.destination);
      osc.start(t);
      osc.stop(t + 0.14);
    }
  } catch (e) { /* sound is a bonus, never a requirement */ }
  if (navigator.vibrate) navigator.vibrate(45);
}

function scannerVideoEl() { return scannerEl('scanVideo'); }

function scannerTrack() {
  return scanner.stream ? (scanner.stream.getVideoTracks()[0] || null) : null;
}

async function scannerBuildDetector() {
  if (scanner.nativeMissing || !('BarcodeDetector' in window)) { scanner.nativeMissing = true; return null; }
  try {
    const supported = await BarcodeDetector.getSupportedFormats();
    const formats = SCAN_FORMATS.filter(f => supported.includes(f));
    if (!formats.length) { scanner.nativeMissing = true; return null; }
    return new BarcodeDetector({ formats });
  } catch (e) {
    scanner.nativeMissing = true;
    return null;
  }
}

function scannerBuildReader() {
  if (scanner.reader || !window.ZXingBrowser?.BrowserMultiFormatReader) return scanner.reader;
  let hints;
  const Z = window.ZXing;
  if (Z?.DecodeHintType) {
    hints = new Map();
    hints.set(Z.DecodeHintType.TRY_HARDER, true);
    if (Z.BarcodeFormat) {
      const picks = ['QR_CODE', 'EAN_13', 'EAN_8', 'UPC_A', 'UPC_E', 'CODE_128', 'CODE_39', 'ITF',
        'DATA_MATRIX', 'PDF_417', 'AZTEC', 'CODABAR']
        .map(name => Z.BarcodeFormat[name]).filter(Boolean);
      if (picks.length) hints.set(Z.DecodeHintType.POSSIBLE_FORMATS, picks);
    }
  }
  scanner.reader = new ZXingBrowser.BrowserMultiFormatReader(hints);
  return scanner.reader;
}

async function scannerDecodeFrameZxing(video) {
  const reader = scannerBuildReader();
  if (!reader || !video.videoWidth || !video.videoHeight) return null;
  if (!scanner.canvas) scanner.canvas = document.createElement('canvas');
  const canvas = scanner.canvas;
  const scale = Math.min(1, 1280 / video.videoWidth);
  canvas.width = Math.round(video.videoWidth * scale);
  canvas.height = Math.round(video.videoHeight * scale);
  canvas.getContext('2d', { willReadFrequently: true })
    .drawImage(video, 0, 0, canvas.width, canvas.height);
  const result = await reader.decodeFromCanvas(canvas);
  return result?.getText?.() || null;
}

function scannerLoop(session) {
  clearTimeout(scanner.timer);
  const step = async () => {
    if (!scanner.open || session !== scanner.session) return;
    const video = scannerVideoEl();
    if (video && video.readyState >= 2) {
      let code = null;
      try {
        if (scanner.detector) {
          const found = await scanner.detector.detect(video);
          if (found.length) code = found[0].rawValue;
        } else {
          code = await scannerDecodeFrameZxing(video);
        }
      } catch (e) { /* an unreadable frame is the normal case */ }
      if (code && scanner.open && session === scanner.session) scannerHit(code);
    }
    if (!scanner.open || session !== scanner.session) return;
    scanner.timer = setTimeout(step, scanner.detector ? 120 : 240);
  };
  step();
}

async function scannerHit(code) {
  const value = String(code || '').trim();
  if (!value) return;
  const now = Date.now();
  // The camera keeps seeing the same code while it is held up: staying on a
  // repeat should not re-add the book. Only accept the code again once it has
  // been out of view for a moment (or a different code was scanned in between).
  if (value === scanner.lastCode && now - scanner.lastAt < 1200) {
    scanner.lastAt = now;
    return;
  }
  scanner.lastCode = value;
  scanner.lastAt = now;
  scanBeep();
  const cb = scanner.onResult;
  if (scanner.continuous) {
    scanner.hits += 1;
    const tag = scannerEl('scanCount');
    if (tag) { tag.hidden = false; tag.textContent = `${scanner.hits} scanned`; }
    scannerStatus(`${value}`, 'ok');
    if (cb) {
      try {
        const message = await cb(value);
        if (message) scannerStatus(`\u2713 ${message}`, 'ok');
      } catch (e) {
        scannerStatus(e.message || 'Not found', 'error');
      }
    }
    return;
  }
  scannerStop();
  closeModal();
  if (cb) {
    try { await cb(value); } catch (e) { toast(e.message || 'Scan failed', 'error'); }
  }
}

function scannerCameraError(err) {
  const name = err?.name || '';
  if (name === 'NotAllowedError') return 'Camera permission blocked. Allow it from the address bar, or type the code instead.';
  if (name === 'NotFoundError' || name === 'OverconstrainedError') return 'No camera found on this device. Type the code instead.';
  if (name === 'NotReadableError') return 'The camera is busy in another app. Close it and try again.';
  return 'Camera unavailable: ' + (err?.message || err);
}

async function scannerRefreshControls() {
  const torchBtn = scannerEl('scanTorch');
  if (torchBtn) {
    const caps = scannerTrack()?.getCapabilities?.() || {};
    torchBtn.hidden = !caps.torch;
    torchBtn.textContent = scanner.torch ? 'Torch off' : 'Torch on';
  }
  const flipBtn = scannerEl('scanFlip');
  if (flipBtn) {
    try {
      const cams = (await navigator.mediaDevices.enumerateDevices()).filter(d => d.kind === 'videoinput');
      flipBtn.hidden = cams.length < 2;
    } catch (e) {
      flipBtn.hidden = true;
    }
  }
}

async function scannerStartCamera() {
  const session = ++scanner.session;
  const video = scannerVideoEl();
  if (!video) return;
  scannerStatus('Starting camera\u2026');
  try {
    const stream = await navigator.mediaDevices.getUserMedia({
      audio: false,
      video: {
        facingMode: { ideal: scanner.facing },
        width: { ideal: 1920 },
        height: { ideal: 1080 },
        frameRate: { ideal: 30 },
        advanced: [{ focusMode: 'continuous' }],
      },
    });
    if (!scanner.open || session !== scanner.session) { stream.getTracks().forEach(t => t.stop()); return; }
    scanner.stream = stream;
    video.srcObject = stream;
    await video.play().catch(() => {});
    scanner.playing = true;
    scannerRefreshControls();
    scannerStatus(scanner.continuous
      ? 'Camera stays on \u2014 scan each item in turn.'
      : 'Point the camera at the barcode or QR code.');
    scannerLoop(session);
  } catch (err) {
    scannerStatus(scannerCameraError(err), 'error');
  }
}

async function scannerToggleTorch() {
  const track = scannerTrack();
  if (!track) return;
  try {
    scanner.torch = !scanner.torch;
    await track.applyConstraints({ advanced: [{ torch: scanner.torch }] });
    scannerRefreshControls();
  } catch (e) {
    scanner.torch = false;
    toast('Torch is not available on this camera', 'error');
  }
}

async function scannerFlip() {
  if (!scanner.open) return;
  scanner.facing = scanner.facing === 'environment' ? 'user' : 'environment';
  scanner.session++;
  clearTimeout(scanner.timer);
  scanner.timer = null;
  if (scanner.stream) { scanner.stream.getTracks().forEach(t => t.stop()); scanner.stream = null; }
  scanner.torch = false;
  const video = scannerVideoEl();
  if (video) video.srcObject = null;
  await scannerStartCamera();
}

async function scannerDecodeFile(file) {
  if (!file) return;
  scannerStatus('Reading image\u2026');
  let code = null;
  const url = URL.createObjectURL(file);
  try {
    const det = scanner.detector || await scannerBuildDetector();
    if (det && window.createImageBitmap) {
      try {
        const bitmap = await createImageBitmap(file);
        const found = await det.detect(bitmap);
        if (found.length) code = found[0].rawValue;
        if (bitmap.close) bitmap.close();
      } catch (e) { /* fall back to ZXing below */ }
    }
    if (!code) {
      await loadZXing();
      const result = await scannerBuildReader().decodeFromImageUrl(url);
      code = result?.getText?.() || null;
    }
  } catch (e) {
    code = null;
  } finally {
    URL.revokeObjectURL(url);
    const input = scannerEl('scanFile');
    if (input) input.value = '';
  }
  if (!scanner.open) return;
  if (code) scannerHit(code);
  else scannerStatus('No barcode found in that image. Try a sharper, closer photo.', 'error');
}

async function openScanner({ onResult = null, continuous = false } = {}) {
  if (!window.isSecureContext || !navigator.mediaDevices?.getUserMedia) {
    toast('Camera scanning needs HTTPS and camera permission. Type the code instead.', 'error');
    return;
  }
  openModal(`
    <h3>Scan barcode or QR</h3>
    <div class="scanner">
      <div class="scanner-stage">
        <video id="scanVideo" playsinline muted autoplay></video>
        <div class="scanner-frame" aria-hidden="true"><i></i><i></i><i></i><i></i></div>
        <div class="scanner-laser" aria-hidden="true"></div>
        ${continuous ? '<span class="scanner-count" id="scanCount" hidden></span>' : ''}
      </div>
      <p class="scanner-status" id="scanStatus">Starting camera\u2026</p>
      <div class="scanner-actions">
        <button id="scanTorch" class="action" type="button" hidden>Torch on</button>
        <button id="scanFlip" class="action" type="button" hidden>Switch camera</button>
        <label class="action scanner-file">Upload image<input id="scanFile" type="file" accept="image/*" hidden></label>
        <button id="scanClose" class="primary" type="button">Done</button>
      </div>
      <p class="hint">No camera? Close this and type the code in the scan field.</p>
    </div>`);
  scanner.open = true;
  scanner.session++;
  scanner.continuous = continuous;
  scanner.onResult = onResult;
  scanner.hits = 0;
  scanner.torch = false;
  scanner.lastCode = '';
  scanner.lastAt = 0;
  scanner.detector = null;
  scanner.reader = null;
  scannerEl('scanTorch').addEventListener('click', scannerToggleTorch);
  scannerEl('scanFlip').addEventListener('click', scannerFlip);
  scannerEl('scanClose').addEventListener('click', () => closeModal());
  scannerEl('scanFile').addEventListener('change', e => scannerDecodeFile(e.target.files?.[0]));
  scanner.detector = await scannerBuildDetector();
  if (!scanner.open) return;
  if (!scanner.detector) loadZXing().catch(() => {});
  await scannerStartCamera();
}

function openScannerHere() {
  const handler = scanCameraHandler;
  openScanner({ onResult: handler ? code => handler(code) : null });
}

function openScannerPOS() {
  openScanner({ continuous: true, onResult: code => doScan(code, { rethrow: true }) });
}

async function loadZXing() {
  if (window.ZXingBrowser?.BrowserMultiFormatReader) return;
  if (!zxingLoadPromise) {
    zxingLoadPromise = (async () => {
      for (const file of ['zxing-library.min.js', 'zxing-browser.min.js']) {
        await new Promise((resolve, reject) => {
          const script = document.createElement('script');
          script.src = new URL('/vendor/' + file, document.baseURI).href;
          script.onload = resolve;
          script.onerror = () => reject(new Error(`Failed to load camera scanner (${file})`));
          document.head.append(script);
        });
      }
    })().catch(e => {
      zxingLoadPromise = null;
      throw e;
    });
  }
  await zxingLoadPromise;
}

function scannerPause() {
  clearTimeout(scanner.timer);
  scanner.timer = null;
  if (scanner.stream) {
    scanner.stream.getTracks().forEach(t => t.stop());
    scanner.stream = null;
  }
  const video = scannerVideoEl();
  if (video) video.srcObject = null;
}

function scannerStop() {
  scanner.open = false;
  scanner.session++;
  scannerPause();
  scanner.torch = false;
  scanner.playing = false;
}

function stopCam() { scannerStop(); }

// Phones and laptops can hold the camera open when the tab goes away; release
// it while the page is hidden and resume the preview when it comes back.
document.addEventListener('visibilitychange', () => {
  if (!scanner.open) return;
  if (document.hidden) {
    scanner.session++;
    scannerPause();
  } else if (scanner.playing) {
    scannerStartCamera();
  }
});

async function checkout() {
  if (!state.cart.length) { toast('Cart is empty', 'error'); return; }
  const custName = document.getElementById('custName').value.trim();
  const payMode = document.getElementById('payMode').value;
  const payRef = document.getElementById('payRef').value.trim();
  if (payMode === 'ledger' && !custName) return toast('Customer name required for ledger (credit) billing', 'error');
  try {
    const res = await api('/api/pos/invoices', { method: 'POST', body: {
      location_id: state.user.role === 'distributor' ? state.user.zone_id : Number(document.getElementById('billLoc').value),
      customer_name: custName, customer_phone: document.getElementById('custPhone').value,
      customer_email: document.getElementById('custEmail').value,
      payment_mode: payMode, payment_ref: payRef,
      discount: Number(document.getElementById('discount').value) || 0,
      items: state.cart.map(i => ({ barcode: i.barcode, qty: i.qty })),
    }});
    state.cart = [];
    openModal(`<h3>Invoice ${esc(res.invoice_number)}</h3>
      <p>Total: &#8377;${res.total.toFixed(2)} &nbsp;(subtotal &#8377;${res.subtotal.toFixed(2)}, discount &#8377;${res.discount.toFixed(2)})</p>
      <p style="font-size:13px;color:#6b5a3a">Payment: ${esc(payMode.toUpperCase())}${payRef ? ' (' + esc(payRef) + ')' : ''}</p>
      <button class="primary" onclick="printInvoice(${res.invoice_id})">Print / download invoice</button>
      <button class="action" onclick="closeModal(); refreshCurrent()">Done</button>`);
    toast('Billed: ' + res.invoice_number, 'success');
  } catch (e) { toast(e.message, 'error'); }
}

async function printInvoice(invoiceId) {
  const w = window.open('', '_blank');
  if (!w) { toast('Allow pop-ups to print the invoice', 'error'); return; }
  try {
    const res = await fetch('/api/pos/invoices/' + invoiceId + '/print', { headers: { Authorization: 'Bearer ' + state.token } });
    if (!res.ok) throw new Error('Could not load invoice');
    const html = await res.text();
    w.document.write(html);
    w.document.close();
  } catch (e) {
    try { w.close(); } catch (_) {}
    toast(e.message, 'error');
  }
}

async function invoiceList() {
  const rows = await api('/api/pos/invoices');
  const canDeleteAny = ['super_admin', 'zone_admin'].includes(state.user.role);
  openModal(`<h3>Invoice history</h3>
    <table>
      <tr><th>Invoice</th><th>Date</th><th>Location</th><th>Customer</th><th>Payment</th><th>Total</th><th>Actions</th></tr>
      ${rows.map(i => `<tr>
        <td>${esc(i.invoice_number)}</td><td>${esc(i.created_at)}</td><td>${esc(i.location_name)}</td>
        <td>${esc(i.customer_name || '')}</td><td>${esc(i.payment_mode.toUpperCase())}</td><td>&#8377;${Number(i.total).toFixed(2)}</td>
        <td><button class="action" onclick="printInvoice(${i.id})">Print</button>${canDeleteAny || i.cashier_id === state.user.id ? `<button class="danger" onclick="invoiceDelete(${i.id})">Delete</button>` : ''}</td>
      </tr>`).join('') || '<tr><td colspan="7" class="hint">No invoices yet.</td></tr>'}
    </table>`);
}

async function invoiceDelete(invoiceId) {
  if (!confirm(`Delete invoice #${invoiceId}? Stock will be returned to the location.`)) return;
  try {
    await api('/api/pos/invoices/' + invoiceId, { method: 'DELETE' });
    toast('Invoice deleted and stock restored', 'success');
    invoiceList();
  } catch (e) { toast(e.message, 'error'); }
}

async function reports() {
  const canViewDistributorSales = ['super_admin', 'zone_admin'].includes(state.user.role);
  const [sales, byBook, stockSum, tf, distributorSales] = await Promise.all([
    api('/api/reports/sales'), api('/api/reports/sales-by-book'), api('/api/reports/stock-summary'), api('/api/reports/transfers'),
    canViewDistributorSales ? api('/api/reports/distributor-sales') : Promise.resolve([]),
  ]);
  state.distributorSales = distributorSales;
  let auditHtml = '';
  let distributorSalesHtml = '';
  if (canViewDistributorSales) {
    const audit = await api('/api/audit?limit=50');
    auditHtml = `<div class="card"><h2>Audit trail</h2><table>
      <tr><th>Time</th><th>User</th><th>Action</th><th>Entity</th><th>Details</th></tr>
      ${audit.map(a => `<tr><td>${esc(a.created_at)}</td><td>${esc(a.username || '')}</td><td>${esc(a.action)}</td><td>${esc(a.entity || '')}${a.entity_id ? ' #' + a.entity_id : ''}</td><td>${esc(a.details || '')}</td></tr>`).join('')}
    </table></div>`;
    distributorSalesHtml = `<div class="card">
      <h2>Distributor sales by bill</h2>
      <div class="toolbar">
        <label>From <input id="distSalesFrom" type="date"></label>
        <label>To <input id="distSalesTo" type="date"></label>
        <button class="primary" onclick="loadDistributorSales()">Load report</button>
        <button class="action" onclick="exportDistributorSales()">Export CSV</button>
      </div>
      <div id="distributorSalesTable">${renderDistributorSalesRows(distributorSales)}</div>
    </div>`;
  }
  document.getElementById('content').innerHTML = `
    ${distributorSalesHtml}
    <div class="card"><h2>Sales by location</h2>
      <table><tr><th>Location</th><th>Invoices</th><th>Revenue</th></tr>
        ${sales.map(s => `<tr><td>${esc(s.location_name)}</td><td>${s.invoice_count}</td><td>&#8377;${s.revenue.toFixed(2)}</td></tr>`).join('')}
      </table></div>
    <div class="card"><h2>Best sellers</h2>
      <table><tr><th>Book</th><th>Barcode</th><th>Units sold</th><th>Revenue</th></tr>
        ${byBook.map(b => `<tr><td>${esc(b.title)}</td><td>${esc(b.barcode)}</td><td>${b.qty_sold}</td><td>&#8377;${b.revenue.toFixed(2)}</td></tr>`).join('')}
      </table></div>
    <div class="card stock-summary-card"><h2>Stock summary</h2>
      <table class="stock-summary-table"><tr><th>Location</th><th>Type</th><th>SKUs</th><th>Units</th><th>Value</th></tr>
        ${stockSum.map(s => `<tr><td>${esc(s.name)}</td><td><span class="stock-type ${esc(s.type)}">${esc(s.type)}</span></td><td>${s.skus}</td><td><span class="stock-quantity">${s.total_units}</span></td><td><span class="stock-value-text">&#8377;${s.stock_value.toFixed(0)}</span></td></tr>`).join('')}
      </table></div>
    <div class="card"><h2>Transfers by status</h2>
      <table><tr><th>Status</th><th>Count</th></tr>
        ${tf.map(t => `<tr><td>${esc(t.status)}</td><td>${t.n}</td></tr>`).join('')}
      </table></div>
    ${auditHtml}`;
}

function renderDistributorSalesRows(rows) {
  if (!rows.length) return '<p class="hint">No distributor sales found for this period.</p>';
  return `<table><tr><th>Date</th><th>Bill</th><th>Distributor</th><th>Location</th><th>Book</th><th>Qty</th><th>Payment mode</th><th>Amount</th></tr>
      ${rows.map(s => `<tr>
        <td>${esc(s.sold_at)}</td><td>${esc(s.invoice_number)}</td>
        <td>${esc(s.distributor_name)} (${esc(s.distributor_username)})</td>
        <td>${esc(s.location_name)}</td><td>${esc(s.book_title)}</td><td>${s.qty}</td>
        <td>${esc(s.payment_mode.toUpperCase())}</td><td>&#8377;${Number(s.amount).toFixed(2)}</td>
      </tr>`).join('')}</table>`;
}

async function loadDistributorSales() {
  const from = document.getElementById('distSalesFrom').value;
  const to = document.getElementById('distSalesTo').value;
  if (from && to && from > to) { toast('From date must not be after To date', 'error'); return; }
  const params = new URLSearchParams();
  if (from) params.set('frm', from);
  if (to) params.set('to', to);
  const query = params.toString();
  state.distributorSales = await api('/api/reports/distributor-sales' + (query ? '?' + query : ''));
  document.getElementById('distributorSalesTable').innerHTML = renderDistributorSalesRows(state.distributorSales);
}

function exportDistributorSales() {
  if (!state.distributorSales.length) { toast('No distributor sales to export', 'error'); return; }
  const columns = [
      ['sold_at', 'Date'], ['invoice_number', 'Bill'], ['distributor_name', 'Distributor'],
      ['distributor_username', 'Username'], ['location_name', 'Location'], ['book_title', 'Book'],
      ['barcode', 'Barcode'], ['qty', 'Quantity'], ['payment_mode', 'Payment mode'], ['amount', 'Amount'],
  ];
  const csv = [columns.map(([, label]) => label), ...state.distributorSales.map(row => columns.map(([key]) => row[key] ?? ''))]
      .map(row => row.map(value => `"${String(value).replace(/"/g, '""')}"`).join(','))
      .join('\r\n');
  const url = URL.createObjectURL(new Blob(['\uFEFF', csv], { type: 'text/csv;charset=utf-8' }));
  const link = document.createElement('a');
  link.href = url;
  link.download = 'distributor-sales.csv';
  document.body.append(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 0);
}

const ROLE_LABELS = { super_admin: 'Super Admin', zone_admin: 'Zone Admin', distributor: 'Distributor', temple: 'Temple / Store' };

async function users() {
  const rows = await api('/api/users');
  state.users = rows;
  const canEdit = state.user.role === 'super_admin';
  document.getElementById('content').innerHTML = `
    <div class="card">
      <div class="toolbar">${canEdit ? '<button class="primary" onclick="userForm()">+ New user</button>' : ''}</div>
      <table>
        <tr><th>Username</th><th>Name</th><th>Role</th><th>Zone</th><th>Location</th><th>Active</th>${canEdit ? '<th>Actions</th>' : ''}</tr>
        ${rows.map(u => `<tr>
          <td>${esc(u.username)}</td><td>${esc(u.name)}</td><td>${esc(ROLE_LABELS[u.role] || u.role)}</td>
          <td>${esc(u.zone_name || '')}</td><td>${esc(u.location_name || '')}</td><td>${u.is_active ? 'Yes' : 'No'}</td>
          ${canEdit ? `<td>
            <button class="action" onclick="userForm(${u.id})">Edit</button>
            <button class="action" onclick="userToggle(${u.id})">${u.is_active ? 'Disable' : 'Enable'}</button>
            <button class="danger" onclick="userDelete(${u.id})">Delete</button>
          </td>` : ''}
        </tr>`).join('') || `<tr><td colspan="${canEdit ? 7 : 6}" class="hint">No users found.</td></tr>`}
      </table>
    </div>`;
}

async function userForm(userId = null) {
  const u = userId ? (state.users || []).find(x => x.id === userId) : null;
  const locs = await api('/api/locations');
  const zones = locs.filter(l => l.type === 'zone');
  const stores = locs.filter(l => ['store', 'warehouse'].includes(l.type));
  const r = u?.role || 'temple';
  openModal(`<h3>${u ? 'Edit user — ' + esc(u.username) : 'New user'}</h3>
    <div class="form-grid">
      <div class="form-row"><label>Username ${u ? '' : '*'}</label><input id="uUser" value="${esc(u?.username || '')}" ${u ? 'disabled' : ''}></div>
      <div class="form-row"><label>Password ${u ? '(leave blank to keep)' : '*'}</label><input id="uPass" type="password"></div>
      <div class="form-row"><label>Name *</label><input id="uName" value="${esc(u?.name || '')}"></div>
      <div class="form-row"><label>Role</label>
        <select id="uRole">
          <option value="super_admin" ${r === 'super_admin' ? 'selected' : ''}>Super Admin</option>
          <option value="zone_admin" ${r === 'zone_admin' ? 'selected' : ''}>Zone Admin</option>
          <option value="distributor" ${r === 'distributor' ? 'selected' : ''}>Distributor</option>
          <option value="temple" ${r === 'temple' ? 'selected' : ''}>Temple / Store</option>
        </select>
      </div>
      <div class="form-row"><label>Zone (for Zone Admin / Distributor)</label>
        <select id="uZone"><option value="">— none —</option>${zones.map(z => `<option value="${z.id}" ${String(u?.zone_id) === String(z.id) ? 'selected' : ''}>${esc(z.name)}</option>`).join('')}</select>
      </div>
      <div class="form-row"><label>Location (for Temple)</label>
        <select id="uStore"><option value="">— none —</option>${stores.map(s => `<option value="${s.id}" ${String(u?.location_id) === String(s.id) ? 'selected' : ''}>${esc(s.name)}</option>`).join('')}</select>
      </div>
      <div class="form-row"><label>Active</label>
        <select id="uActive"><option value="1" ${u?.is_active !== 0 ? 'selected' : ''}>Yes</option><option value="0" ${u?.is_active === 0 ? 'selected' : ''}>No</option></select>
      </div>
    </div>
    <button class="primary" onclick="userSave(${u ? u.id : 'null'})">${u ? 'Save changes' : 'Create user'}</button>`);
}

async function userSave(userId) {
  const role = document.getElementById('uRole').value;
  const password = document.getElementById('uPass').value;
  const body = {
    name: document.getElementById('uName').value.trim(),
    role,
    zone_id: role === 'super_admin' ? null : (Number(document.getElementById('uZone').value) || null),
    location_id: role === 'temple' ? (Number(document.getElementById('uStore').value) || null) : null,
    is_active: Number(document.getElementById('uActive').value),
  };
  if (password) body.password = password;
  if (!body.name) { toast('Name is required', 'error'); return; }
  try {
    if (userId) {
      await api('/api/users/' + userId, { method: 'PATCH', body });
    } else {
      const username = document.getElementById('uUser').value.trim();
      if (!username || !password) { toast('Username and password are required', 'error'); return; }
      await api('/api/users', { method: 'POST', body: { ...body, username, password } });
    }
    closeModal(); toast(userId ? 'User updated' : 'User created', 'success'); refreshCurrent();
  } catch (e) { toast(e.message, 'error'); }
}

async function userToggle(userId) {
  const u = (state.users || []).find(x => x.id === userId);
  if (!u) return;
  try {
    await api('/api/users/' + userId, { method: 'PATCH', body: { is_active: u.is_active ? 0 : 1 } });
    toast(u.is_active ? 'User disabled' : 'User enabled', 'success'); refreshCurrent();
  } catch (e) { toast(e.message, 'error'); }
}

async function userDelete(userId) {
  const u = (state.users || []).find(x => x.id === userId);
  if (!confirm(`Delete user "${u ? u.username : userId}"?`)) return;
  try {
    await api('/api/users/' + userId, { method: 'DELETE' });
    toast('User deleted', 'success'); refreshCurrent();
  } catch (e) { toast(e.message, 'error'); }
}

async function locations() {
  const rows = await api('/api/locations');
  document.getElementById('content').innerHTML = `
    <div class="card">
      <div class="toolbar"><button class="primary" onclick="locationForm()">+ New location</button></div>
      <table>
        <tr><th>Code</th><th>Name</th><th>Type</th><th>Parent</th><th>Actions</th></tr>
        ${rows.map(l => `<tr><td>${esc(l.code)}</td><td>${esc(l.name)}</td><td>${esc(l.type)}</td><td>${esc(l.parent_name || '')}</td><td><button class="danger" onclick="locationDelete(${l.id})">Delete</button></td></tr>`).join('')}
      </table>
    </div>`;
}

async function locationDelete(locationId) {
  if (!confirm(`Delete location #${locationId}?`)) return;
  try {
    await api('/api/locations/' + locationId, { method: 'DELETE' });
    toast('Location deleted', 'success'); refreshCurrent();
  } catch (e) { toast(e.message, 'error'); }
}

async function locationForm() {
  const rows = await api('/api/locations');
  openModal(`<h3>New location</h3>
    <div class="form-grid">
      <div class="form-row"><label>Code *</label><input id="lCode" placeholder="e.g. Z3"></div>
      <div class="form-row"><label>Name *</label><input id="lName"></div>
      <div class="form-row"><label>Type</label>
        <select id="lType"><option value="warehouse">Warehouse</option><option value="zone">Zone</option><option value="store">Temple / Store</option></select>
      </div>
      <div class="form-row"><label>Parent (for Zone/Store)</label>
        <select id="lParent"><option value="">— none —</option>${rows.map(l => `<option value="${l.id}">${esc(l.name)}</option>`).join('')}</select>
      </div>
    </div>
    <button class="primary" onclick="locationCreate()">Create</button>`);
}

async function locationCreate() {
  try {
    await api('/api/locations', { method: 'POST', body: {
      code: document.getElementById('lCode').value.trim(),
      name: document.getElementById('lName').value.trim(),
      type: document.getElementById('lType').value,
      parent_id: Number(document.getElementById('lParent').value) || null,
    }});
    closeModal(); toast('Location created', 'success'); refreshCurrent();
  } catch (e) { toast(e.message, 'error'); }
}

document.getElementById('loginBtn').addEventListener('click', login);
document.getElementById('loginPass').addEventListener('keydown', e => { if (e.key === 'Enter') login(); });
document.getElementById('loginUser').addEventListener('keydown', e => { if (e.key === 'Enter') document.getElementById('loginPass').focus(); });
document.getElementById('logoutBtn').addEventListener('click', logout);

(async function init() {
  if (!state.token) { return; }
  try {
    const res = await api('/api/auth/me', { public: true });
    state.user = res.user;
    showApp();
  } catch (e) { logout(); }
})();

// Temporary opt-in layout readout: open the app with ?diag=1 to see the real
// viewport measurements and any element that spills past the window.
if (new URLSearchParams(location.search).has('diag')) {
  const box = document.createElement('div');
  box.id = 'diagBox';
  box.style.cssText = 'position:fixed;left:8px;bottom:8px;z-index:9999;max-width:calc(100vw - 16px);' +
    'background:#1b1208;color:#ffe9c7;font:12px/1.55 Consolas,monospace;padding:10px 12px;' +
    'border-radius:8px;white-space:pre-wrap;box-shadow:0 8px 24px rgba(0,0,0,.45)';
  const update = () => {
    const de = document.documentElement;
    const vv = window.visualViewport;
    const spill = [...document.querySelectorAll('#appView *')]
      .map(el => ({ el, r: el.getBoundingClientRect() }))
      .filter(x => x.r.width > 0 && x.r.right > de.clientWidth + 1)
      .sort((a, b) => b.r.right - a.r.right)
      .slice(0, 3)
      .map(x => `${x.el.tagName.toLowerCase()}.${String(x.el.className).split(' ')[0]} @${Math.round(x.r.right)}`);
    box.textContent = [
      `innerWidth ${window.innerWidth}  clientWidth ${de.clientWidth}`,
      `scrollWidth ${de.scrollWidth}  overflow ${de.scrollWidth - de.clientWidth}px`,
      `devicePixelRatio ${window.devicePixelRatio}`,
      `screen ${window.screen.width}x${window.screen.height}`,
      `visualViewport ${vv ? `${Math.round(vv.width)}px scale ${vv.scale}` : 'n/a'}`,
      `window outer ${window.outerWidth}x${window.outerHeight}`,
      spill.length ? `spill: ${spill.join('  ')}` : 'spill: none',
    ].join('\n');
  };
  update();
  setInterval(update, 800);
  window.addEventListener('resize', update);
  document.body.appendChild(box);
}
