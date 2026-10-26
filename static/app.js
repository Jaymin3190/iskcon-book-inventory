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

const RENDER = { dashboard, books, stock, transfers: renderTransfers, returns: renderReturns, pos, reports, users, locations };

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

function logout() {
  state.token = ''; state.user = null; localStorage.removeItem('tk');
  document.getElementById('appView').hidden = true;
  document.getElementById('loginView').hidden = false;
}

function showApp() {
  document.getElementById('loginView').hidden = true;
  document.getElementById('appView').hidden = false;
  const u = state.user;
  const loc = u.location_name || u.zone_name || 'Global';
  document.getElementById('userInfo').textContent = `${u.name} (${u.role}) — ${loc}`;
  const nav = document.getElementById('tabs');
  const menuToggle = document.getElementById('mobileMenuToggle');
  nav.classList.remove('mobile-menu-open');
  menuToggle.setAttribute('aria-expanded', 'false');
  menuToggle.textContent = 'Menu';
  nav.innerHTML = TABS.filter(t => t.roles.includes(u.role))
    .map(t => `<button data-tab="${t.id}">${t.label}</button>`).join('');
  const availableTabs = nav.querySelectorAll('button');
  document.getElementById('appView').classList.toggle('single-tab-nav', availableTabs.length === 1);
  nav.querySelectorAll('button').forEach(b => b.addEventListener('click', () => {
    nav.querySelectorAll('button').forEach(x => x.classList.remove('active'));
    b.classList.add('active');
    state.activeTab = b.dataset.tab;
    nav.classList.remove('mobile-menu-open');
    menuToggle.setAttribute('aria-expanded', 'false');
    menuToggle.textContent = 'Menu';
    RENDER[state.activeTab]().catch(e => toast(e.message, 'error'));
  }));
  const firstTab = nav.querySelector('button');
  firstTab.classList.add('active');
  state.activeTab = firstTab.dataset.tab;
  RENDER[state.activeTab]().catch(e => toast(e.message, 'error'));
}

function refreshCurrent() { if (state.user) RENDER[state.activeTab]().catch(e => toast(e.message, 'error')); }

document.getElementById('mobileMenuToggle').addEventListener('click', () => {
  const nav = document.getElementById('tabs');
  const isOpen = nav.classList.toggle('mobile-menu-open');
  const menuToggle = document.getElementById('mobileMenuToggle');
  menuToggle.setAttribute('aria-expanded', String(isOpen));
  menuToggle.textContent = isOpen ? 'Close menu' : 'Menu';
});

function openModal(html) {
  document.getElementById('modalBox').innerHTML = `<button class="modal-close" type="button" aria-label="Close window" title="Close" onclick="closeModal()">&times;</button>${html}`;
  document.getElementById('modalBackdrop').hidden = false;
}
function closeModal() { document.getElementById('modalBackdrop').hidden = true; }
document.getElementById('modalBackdrop').addEventListener('click', e => { if (e.target.id === 'modalBackdrop') closeModal(); });

function toast(msg, type = '') {
  const t = document.getElementById('toast');
  t.textContent = msg; t.className = type; t.hidden = false;
  setTimeout(() => t.hidden = true, 3200);
}

const scanDb = new Map();

function scanBoxHTML(placeholder = 'Scan barcode / ISBN, press Enter') {
  return `<div class="cart-item" style="margin-bottom:10px">
    <span style="font-size:12px;color:#8a5a1f;white-space:nowrap">Barcode scan</span>
    <input id="scanStripInput" placeholder="${esc(placeholder)}" autocomplete="off" style="flex:1;font-family:Consolas,monospace">
  </div>`;
}

function bindScan(onScan, onInput, keepFocus = true) {
  const input = document.getElementById('scanStripInput');
  if (!input) return;
  input.focus();
  input.addEventListener('keydown', async (e) => {
    if (e.key !== 'Enter') return;
    const code = input.value.trim();
    input.value = '';
    if (!code) return;
    try {
      const b = await api('/api/books/lookup/' + encodeURIComponent(code));
      scanDb.set('last', b);
      onScan(b);
      toast(b.title + ' matched', 'success');
    } catch (err) { toast('No book for barcode ' + code, 'error'); }
    if (keepFocus) input.focus();
  });
  if (onInput) input.addEventListener('input', () => onInput(input.value));
  return input;
}

async function pageScanStrip(onScan, placeholder = 'Scan barcode / ISBN, press Enter') {
  const holder = document.createElement('div');
  holder.id = 'pageScanStrip';
  holder.innerHTML = scanBoxHTML(placeholder);
  document.getElementById('content').prepend(holder);
  return bindScan(onScan, null);
}

async function dashboard() {
  const [stock, sales, tf] = await Promise.all([
    api('/api/reports/stock-summary'), api('/api/reports/sales'), api('/api/reports/transfers'),
  ]);
  const totalUnits = stock.reduce((a, s) => a + (s.total_units || 0), 0);
  const totalValue = stock.reduce((a, s) => a + (s.stock_value || 0), 0);
  const revenue = sales.reduce((a, s) => a + (s.revenue || 0), 0);
  const open = tf.filter(t => t.status === 'requested').reduce((a, t) => a + t.n, 0);
  document.getElementById('content').innerHTML = `
    <div class="stats">
      <div class="stat stock-units-stat"><div class="num">${totalUnits}</div><div class="lbl">Units on hand</div></div>
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
    pageScanStrip(() => refreshCurrent(), 'Scan a book barcode to highlight it in the list');
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
  document.getElementById('content').innerHTML = `
    <div class="card">
      <div class="toolbar">
        <select id="stockLoc" onchange="stockLocFilter=this.value; refreshCurrent()">
          <option value="">All locations</option>
          ${locs.map(l => `<option value="${l.id}" ${String(l.id) === stockLocFilter ? 'selected' : ''}>${esc(l.name)}</option>`).join('')}
        </select>
        <button class="primary" onclick="stockAdjustModal()">+ Add / adjust stock</button>
        <button class="action" onclick="stockCountModal()">Start stock count</button>
        <button class="action" onclick="showLedger()">Stock ledger</button>
      </div>
      <table class="stock-inventory-table">
        <tr><th>Location</th><th>Title</th><th>Barcode</th><th>Publication</th><th>Qty on hand</th><th>Price</th><th>Value</th><th>Actions</th></tr>
        ${rows.map(r => `<tr ${r.barcode === (scanDb.get('last')?.barcode) ? 'class="hl"' : ''}><td>${esc(r.location_name)}</td><td>${esc(r.title)}</td><td>${esc(r.barcode)}</td><td>${esc(r.publication || '')}</td><td><span class="stock-quantity ${r.qty > 0 ? 'available' : 'empty'}">${r.qty}</span></td><td>&#8377;${r.price}</td><td><span class="stock-value-text">&#8377;${(r.qty * r.price).toFixed(2)}</span></td><td><button class="action" onclick="stockAdjustModal(${r.book_id}, ${r.location_id})">Adjust</button><button class="action" onclick="showLedger(${r.location_id}, ${r.book_id})">Ledger</button></td></tr>`).join('') || '<tr><td colspan="8" class="hint">No stock rows found.</td></tr>'}
      </table>
    </div>`;
  pageScanStrip(() => refreshCurrent(), 'Scan a barcode to highlight its stock row');
}

async function stockAdjustModal(bookId = null, locationId = null) {
  const [locs, books] = await Promise.all([api('/api/locations'), api('/api/books')]);
  state.books = books;
  openModal(`<h3>Add / adjust stock</h3>
    <div class="form-grid">
      <div class="form-row"><label>Location</label><select id="saLoc">${locs.map(l => `<option value="${l.id}">${esc(l.name)}</option>`).join('')}</select></div>
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
    ${scanBoxHTML('Scan a barcode to jump to that book row')}
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
  state.books = books;
  openModal(`<h3>Request stock transfer</h3>
    <div class="form-grid">
      <div class="form-row"><label>From location</label><select id="tfFrom">${locs.map(l => `<option value="${l.id}">${esc(l.name)}</option>`).join('')}</select></div>
      <div class="form-row"><label>To location</label><select id="tfTo">${locs.map(l => `<option value="${l.id}">${esc(l.name)}</option>`).join('')}</select></div>
    </div>
    <div class="form-row"><label>Note</label><input id="tfNote"></div>
    ${scanBoxHTML('Scan barcode to add book to transfer')}
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
    ${scanBoxHTML('Scan barcode to add book to return')}
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
    ${scanBoxHTML('Scan barcode to select book')}
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
  const defLoc = state.user.location_id || state.user.zone_id;
  document.getElementById('content').innerHTML = `
    <div class="card">
      <h2>Quick-scan billing</h2>
      <div class="toolbar" id="scanBox">
        <select id="billLoc" style="max-width:220px">
          ${locs.map(l => `<option value="${l.id}" ${String(l.id) === String(defLoc) ? 'selected' : ''}>${esc(l.name)}</option>`).join('')}
        </select>
        <input id="scanInput" placeholder="Scan / type barcode or ISBN, press Enter" style="flex:1" onkeydown="if(event.key==='Enter')doScan(this.value)">
        <button class="primary" onclick="cameraScan()">Camera scan</button>
        <button class="action" onclick="invoiceList()">Invoice history</button>
        <select id="manualBook" onchange="posAddBySelect(this.value)" style="max-width:260px">
          <option value="">&mdash; manual search &mdash;</option>
          ${books.map(b => `<option value="${b.barcode}">${esc(b.title)}</option>`).join('')}
        </select>
      </div>
      <table id="cartTable">
        <tr><th>Title</th><th>Barcode</th><th>Price</th><th>Qty</th><th>Total</th><th></th></tr>
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
    </div>
    <video id="scanCam" hidden playsinline></video>`;
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

async function doScan(code) {
  const c = document.getElementById('scanInput');
  c.value = '';
  if (!code) return;
  try {
    const b = await api('/api/books/lookup/' + encodeURIComponent(code.trim()));
    addToCart(b);
    toast(b.title + ' added', 'success');
  } catch (e) { toast(e.message, 'error'); }
  c.focus();
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
  table.innerHTML = `<tr><th>Title</th><th>Barcode</th><th>Price</th><th>Qty</th><th>Total</th><th></th></tr>` +
    state.cart.map((i, idx) => `<tr>
      <td>${esc(i.title)}</td><td>${esc(i.barcode)}</td><td>&#8377;${i.price.toFixed(2)}</td>
      <td><input type="number" min="1" value="${i.qty}" style="width:60px" onchange="setCartQty(${idx},this.value)"></td>
      <td>&#8377;${(i.price * i.qty).toFixed(2)}</td>
      <td><button class="danger" onclick="removeCart(${idx})">x</button></td>
    </tr>`).join('');
  document.getElementById('cartTotal').textContent = `&#8377;${total.toFixed(2)}`;
  document.getElementById('cartTotal').innerHTML = `&#8377;${total.toFixed(2)}`;
}

let scanStream = null, detectLoop = null;
async function cameraScan() {
  const vid = document.getElementById('scanCam');
  if (!vid) return;
  if (scanStream) { stopCam(); return; }
  if (window.BarcodeDetector === undefined) { toast('Camera scanning not supported in this browser. Use the scan box or a USB scanner.', 'error'); return; }
  try {
    const det = new BarcodeDetector({ formats: ['ean_13', 'ean_8', 'code_128', 'upc_a', 'isbn_13', 'qr_code'] });
    scanStream = await navigator.mediaDevices.getUserMedia({ video: { facingMode: 'environment' } });
    vid.hidden = false; vid.srcObject = scanStream; await vid.play();
    document.querySelector('#scanBox .primary').textContent = 'Stop camera';
    detectLoop = setInterval(async () => {
      try {
        const codes = await det.detect(vid);
        if (codes.length && codes[0].rawValue) { doScan(codes[0].rawValue); stopCam(); }
      } catch (e) {}
    }, 400);
  } catch (e) { toast('Camera unavailable: ' + e.message, 'error'); }
}

function stopCam() {
  clearInterval(detectLoop); detectLoop = null;
  if (scanStream) { scanStream.getTracks().forEach(t => t.stop()); scanStream = null; }
  const vid = document.getElementById('scanCam'); if (vid) vid.hidden = true;
  const btn = document.querySelector('#scanBox .primary'); if (btn) btn.textContent = 'Camera scan';
}

async function checkout() {
  if (!state.cart.length) { toast('Cart is empty', 'error'); return; }
  const custName = document.getElementById('custName').value.trim();
  const payMode = document.getElementById('payMode').value;
  const payRef = document.getElementById('payRef').value.trim();
  if (payMode === 'ledger' && !custName) return toast('Customer name required for ledger (credit) billing', 'error');
  try {
    const res = await api('/api/pos/invoices', { method: 'POST', body: {
      location_id: Number(document.getElementById('billLoc').value),
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
  openModal(`<h3>Invoice history</h3>
    <table>
      <tr><th>Invoice</th><th>Date</th><th>Location</th><th>Customer</th><th>Payment</th><th>Total</th><th>Actions</th></tr>
      ${rows.map(i => `<tr>
        <td>${esc(i.invoice_number)}</td><td>${esc(i.created_at)}</td><td>${esc(i.location_name)}</td>
        <td>${esc(i.customer_name || '')}</td><td>${esc(i.payment_mode.toUpperCase())}</td><td>&#8377;${Number(i.total).toFixed(2)}</td>
        <td><button class="action" onclick="printInvoice(${i.id})">Print</button><button class="danger" onclick="invoiceDelete(${i.id})">Delete</button></td>
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
