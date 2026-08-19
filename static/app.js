const state = { token: localStorage.getItem('tk') || '', user: null, activeTab: 'dashboard', books: [], scCountId: null, cart: [] };

function esc(s) { return String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c])); }

async function api(path, opts = {}) {
  const headers = { 'Content-Type': 'application/json' };
  if (state.token) headers['Authorization'] = 'Bearer ' + state.token;
  const res = await fetch(path, { headers, ...opts, body: opts.body ? JSON.stringify(opts.body) : undefined });
  if (res.status === 401 && !opts.public) { logout(); throw new Error('Session expired'); }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const msg = Array.isArray(data.detail) ? data.detail.map(d => d.msg).join('; ') : (data.detail || res.statusText);
    throw new Error(msg);
  }
  return data;
}

const TABS = [
  { id: 'dashboard', label: 'Dashboard', roles: ['super_admin', 'zone_admin', 'distributor', 'temple'] },
  { id: 'books', label: 'Books', roles: ['super_admin', 'zone_admin', 'distributor', 'temple'] },
  { id: 'stock', label: 'Stock', roles: ['super_admin', 'zone_admin', 'distributor', 'temple'] },
  { id: 'transfers', label: 'Transfers', roles: ['super_admin', 'zone_admin', 'distributor', 'temple'] },
  { id: 'returns', label: 'Returns', roles: ['super_admin', 'zone_admin', 'distributor', 'temple'] },
  { id: 'pos', label: 'POS Billing', roles: ['super_admin', 'zone_admin', 'distributor', 'temple'] },
  { id: 'reports', label: 'Reports', roles: ['super_admin', 'zone_admin', 'distributor', 'temple'] },
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
  nav.innerHTML = TABS.filter(t => t.roles.includes(u.role))
    .map(t => `<button data-tab="${t.id}">${t.label}</button>`).join('');
  nav.querySelectorAll('button').forEach(b => b.addEventListener('click', () => {
    nav.querySelectorAll('button').forEach(x => x.classList.remove('active'));
    b.classList.add('active');
    state.activeTab = b.dataset.tab;
    RENDER[state.activeTab]().catch(e => toast(e.message, 'error'));
  }));
  nav.querySelector('button').classList.add('active');
  RENDER.dashboard().catch(e => toast(e.message, 'error'));
}

function refreshCurrent() { if (state.user) RENDER[state.activeTab]().catch(e => toast(e.message, 'error')); }

function openModal(html) { document.getElementById('modalBox').innerHTML = html; document.getElementById('modalBackdrop').hidden = false; }
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
      <div class="stat"><div class="num">${totalUnits}</div><div class="lbl">Units on hand</div></div>
      <div class="stat"><div class="num">&#8377;${totalValue.toFixed(0)}</div><div class="lbl">Stock value</div></div>
      <div class="stat"><div class="num">&#8377;${revenue.toFixed(0)}</div><div class="lbl">Sales revenue</div></div>
      <div class="stat"><div class="num">${open}</div><div class="lbl">Open transfer requests</div></div>
    </div>
    <div class="card"><h2>Stock summary by location</h2>
      <table><tr><th>Location</th><th>Type</th><th>SKUs</th><th>Units</th><th>Value</th></tr>
        ${stock.map(s => `<tr><td>${esc(s.name)}</td><td>${esc(s.type)}</td><td>${s.skus}</td><td>${s.total_units}</td><td>&#8377;${(s.stock_value || 0).toFixed(0)}</td></tr>`).join('')}
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

async function books() {
  const q = document.getElementById('bookSearch')?.value || '';
  const rows = await api('/api/books' + (q ? '?q=' + encodeURIComponent(q) : ''));
  const canEdit = ['super_admin', 'zone_admin'].includes(state.user.role);
  document.getElementById('content').innerHTML = `
    <div class="card">
      <div class="toolbar">
        <input id="bookSearch" placeholder="Search title / ISBN / barcode" style="flex:1" oninput="refreshCurrent()">
        ${canEdit ? '<button class="primary" onclick="bookForm()">+ New book</button>' : ''}
      </div>
      <table>
        <tr><th>Title</th><th>Barcode/ISBN</th><th>Language</th><th>Publication</th><th>Price</th></tr>
        ${rows.map(b => `<tr ${b.barcode === (scanDb.get('last')?.barcode) ? 'class="hl"' : ''}><td>${esc(b.title)}</td><td>${esc(b.barcode)}</td><td>${esc(b.language || '')}</td><td>${esc(b.publication || '')}</td><td>&#8377;${b.price}</td></tr>`).join('')}
      </table>
    </div>`;
  if (['super_admin', 'zone_admin', 'distributor', 'temple'].includes(state.user.role)) {
    pageScanStrip(() => refreshCurrent(), 'Scan a book barcode to highlight it in the list');
  }
}

async function bookForm() {
  openModal(`<h3>New book</h3>
    <div class="form-grid">
      <div class="form-row"><label>Title *</label><input id="bkTitle"></div>
      <div class="form-row"><label>Barcode/ISBN *</label><input id="bkBarcode" placeholder="e.g. 9780892131343"></div>
      <div class="form-row"><label>Language</label><input id="bkLang"></div>
      <div class="form-row"><label>Publication</label><input id="bkPub" placeholder="e.g. Bhaktivedanta Book Trust (BBT)"></div>
      <div class="form-row"><label>Price (&#8377;)</label><input id="bkPrice" type="number" value="0"></div>
    </div>
    <button class="primary" onclick="bookCreate()">Save</button>`);
}

async function bookCreate() {
  try {
    await api('/api/books', { method: 'POST', body: {
      title: document.getElementById('bkTitle').value, barcode: document.getElementById('bkBarcode').value.trim(),
      isbn: document.getElementById('bkBarcode').value.trim(), language: document.getElementById('bkLang').value,
      publication: document.getElementById('bkPub').value,
      price: Number(document.getElementById('bkPrice').value) || 0,
    }});
    closeModal(); toast('Book added', 'success'); refreshCurrent();
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
        <button class="primary" onclick="stockCountModal()">Start stock count</button>
        <button class="action" onclick="showLedger()">Stock ledger</button>
      </div>
      <table>
        <tr><th>Location</th><th>Title</th><th>Barcode</th><th>Publication</th><th>Qty on hand</th><th>Price</th><th>Value</th></tr>
        ${rows.map(r => `<tr ${r.barcode === (scanDb.get('last')?.barcode) ? 'class="hl"' : ''}><td>${esc(r.location_name)}</td><td>${esc(r.title)}</td><td>${esc(r.barcode)}</td><td>${esc(r.publication || '')}</td><td>${r.qty}</td><td>&#8377;${r.price}</td><td>&#8377;${(r.qty * r.price).toFixed(2)}</td></tr>`).join('')}
      </table>
    </div>`;
  pageScanStrip(() => refreshCurrent(), 'Scan a barcode to highlight its stock row');
}

async function showLedger() {
  const rows = await api('/api/stock/ledger' + (stockLocFilter ? '?location_id=' + stockLocFilter : '') + '&limit=200');
  openModal(`<h3>Stock ledger (movements)</h3>
    <table>
      <tr><th>Time</th><th>Location</th><th>Title</th><th>Type</th><th>Qty</th><th>Ref</th><th>User</th></tr>
      ${rows.map(m => `<tr><td>${esc(m.created_at)}</td><td>${esc(m.location_code)}</td><td>${esc(m.title)}</td><td>${esc(m.change_type)}</td><td>${m.qty}</td><td>${esc(m.ref_type || '')}${m.ref_id ? '#' + m.ref_id : ''}</td><td>${esc(m.user_name || '')}</td></tr>`).join('')}
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
  return h || '&mdash;';
}

async function tAction(id, action) {
  if (!confirm(`Confirm: ${action} transfer #${id}?`)) return;
  try { await api(`/api/stock/transfers/${id}/${action}`, { method: 'POST' }); toast('Transfer ' + action + 'd', 'success'); refreshCurrent(); }
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
          <td>${(r.status === 'recorded' && canProc) ? `<button class="action" onclick="processReturn(${r.id})">Process</button>` : '&mdash;'}</td>
        </tr>`).join('')}
      </table>
    </div>
    <div class="card"><h2>Damaged write-offs</h2>
      <table><tr><th>Time</th><th>Location</th><th>Title</th><th>Qty</th><th>Reason</th></tr>
        ${damaged.map(d => `<tr><td>${esc(d.created_at)}</td><td>${esc(d.location_code)}</td><td>${esc(d.title)}</td><td>${d.qty}</td><td>${esc(d.reason_code)}</td></tr>`).join('')}
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
          <select id="payMode"><option value="cash">Cash</option><option value="upi">UPI</option><option value="card">Card</option><option value="ledger">Credit / Ledger</option></select>
        </div>
        <div class="form-row"><label>Payment ref (optional)</label><input id="payRef"></div>
        <div class="form-row"><label>Discount (&#8377;)</label><input id="discount" type="number" min="0" value="0" oninput="renderCart()"></div>
      </div>
      <div style="margin-top:14px;display:flex;justify-content:space-between;align-items:center">
        <div><strong>Total:</strong> <span id="cartTotal">&#8377;0.00</span></div>
        <button class="primary" onclick="checkout()">Checkout</button>
      </div>
    </div>
    <video id="scanCam" hidden playsinline></video>`;
  renderCart();
  document.getElementById('scanInput').focus();
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
      <button class="primary" onclick="window.open('/api/pos/invoices/${res.invoice_id}/print','_blank')">Print / download invoice</button>
      <button class="action" onclick="closeModal(); refreshCurrent()">Done</button>`);
    toast('Billed: ' + res.invoice_number, 'success');
  } catch (e) { toast(e.message, 'error'); }
}

async function reports() {
  const [sales, byBook, stockSum, tf] = await Promise.all([
    api('/api/reports/sales'), api('/api/reports/sales-by-book'), api('/api/reports/stock-summary'), api('/api/reports/transfers'),
  ]);
  let auditHtml = '';
  if (['super_admin', 'zone_admin'].includes(state.user.role)) {
    const audit = await api('/api/audit?limit=50');
    auditHtml = `<div class="card"><h2>Audit trail</h2><table>
      <tr><th>Time</th><th>User</th><th>Action</th><th>Entity</th><th>Details</th></tr>
      ${audit.map(a => `<tr><td>${esc(a.created_at)}</td><td>${esc(a.username || '')}</td><td>${esc(a.action)}</td><td>${esc(a.entity || '')}${a.entity_id ? ' #' + a.entity_id : ''}</td><td>${esc(a.details || '')}</td></tr>`).join('')}
    </table></div>`;
  }
  document.getElementById('content').innerHTML = `
    <div class="card"><h2>Sales by location</h2>
      <table><tr><th>Location</th><th>Invoices</th><th>Revenue</th></tr>
        ${sales.map(s => `<tr><td>${esc(s.location_name)}</td><td>${s.invoice_count}</td><td>&#8377;${s.revenue.toFixed(2)}</td></tr>`).join('')}
      </table></div>
    <div class="card"><h2>Best sellers</h2>
      <table><tr><th>Book</th><th>Barcode</th><th>Units sold</th><th>Revenue</th></tr>
        ${byBook.map(b => `<tr><td>${esc(b.title)}</td><td>${esc(b.barcode)}</td><td>${b.qty_sold}</td><td>&#8377;${b.revenue.toFixed(2)}</td></tr>`).join('')}
      </table></div>
    <div class="card"><h2>Stock summary</h2>
      <table><tr><th>Location</th><th>Type</th><th>SKUs</th><th>Units</th><th>Value</th></tr>
        ${stockSum.map(s => `<tr><td>${esc(s.name)}</td><td>${esc(s.type)}</td><td>${s.skus}</td><td>${s.total_units}</td><td>&#8377;${s.stock_value.toFixed(0)}</td></tr>`).join('')}
      </table></div>
    <div class="card"><h2>Transfers by status</h2>
      <table><tr><th>Status</th><th>Count</th></tr>
        ${tf.map(t => `<tr><td>${esc(t.status)}</td><td>${t.n}</td></tr>`).join('')}
      </table></div>
    ${auditHtml}`;
}

async function users() {
  const rows = await api('/api/users');
  const canCreate = state.user.role === 'super_admin';
  document.getElementById('content').innerHTML = `
    <div class="card">
      <div class="toolbar">${canCreate ? '<button class="primary" onclick="userForm()">+ New user</button>' : ''}</div>
      <table>
        <tr><th>Username</th><th>Name</th><th>Role</th><th>Zone</th><th>Location</th><th>Active</th></tr>
        ${rows.map(u => `<tr><td>${esc(u.username)}</td><td>${esc(u.name)}</td><td>${esc(u.role)}</td><td>${esc(u.zone_name || '')}</td><td>${esc(u.location_name || '')}</td><td>${u.is_active ? 'Yes' : 'No'}</td></tr>`).join('')}
      </table>
    </div>`;
}

async function userForm() {
  const locs = await api('/api/locations');
  const zones = locs.filter(l => l.type === 'zone');
  const stores = locs.filter(l => l.type === 'store');
  openModal(`<h3>New user</h3>
    <div class="form-grid">
      <div class="form-row"><label>Username *</label><input id="uUser"></div>
      <div class="form-row"><label>Password *</label><input id="uPass"></div>
      <div class="form-row"><label>Name *</label><input id="uName"></div>
      <div class="form-row"><label>Role</label>
        <select id="uRole"><option value="zone_admin">Zone Admin</option><option value="distributor">Distributor</option><option value="temple">Temple / Store</option></select>
      </div>
      <div class="form-row"><label>Zone (for Zone Admin / Distributor)</label>
        <select id="uZone">${zones.map(z => `<option value="${z.id}">${esc(z.name)}</option>`).join('')}</select>
      </div>
      <div class="form-row"><label>Store (for Temple)</label>
        <select id="uStore"><option value="">— none —</option>${stores.map(s => `<option value="${s.id}">${esc(s.name)}</option>`).join('')}</select>
      </div>
    </div>
    <button class="primary" onclick="userCreate()">Create</button>`);
}

async function userCreate() {
  try {
    const role = document.getElementById('uRole').value;
    await api('/api/users', { method: 'POST', body: {
      username: document.getElementById('uUser').value.trim(),
      password: document.getElementById('uPass').value,
      name: document.getElementById('uName').value.trim(),
      role,
      zone_id: Number(document.getElementById('uZone').value) || null,
      location_id: role === 'temple' ? Number(document.getElementById('uStore').value) || null : null,
    }});
    closeModal(); toast('User created', 'success'); refreshCurrent();
  } catch (e) { toast(e.message, 'error'); }
}

async function locations() {
  const rows = await api('/api/locations');
  document.getElementById('content').innerHTML = `
    <div class="card">
      <div class="toolbar"><button class="primary" onclick="locationForm()">+ New location</button></div>
      <table>
        <tr><th>Code</th><th>Name</th><th>Type</th><th>Parent</th></tr>
        ${rows.map(l => `<tr><td>${esc(l.code)}</td><td>${esc(l.name)}</td><td>${esc(l.type)}</td><td>${esc(l.parent_name || '')}</td></tr>`).join('')}
      </table>
    </div>`;
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
