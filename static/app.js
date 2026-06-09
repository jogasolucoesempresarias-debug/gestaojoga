/* Gestão JOGA — helpers compartilhados (vanilla JS, sem build) */

// ── Fetch com sessão + tratamento de 401/403 ──
async function api(url, opts = {}) {
  const cfg = {
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json' },
    ...opts,
  };
  if (cfg.body && typeof cfg.body !== 'string') cfg.body = JSON.stringify(cfg.body);
  const r = await fetch(url, cfg);
  if (r.status === 401) { window.location.href = '/login'; throw new Error('Não autenticado'); }
  if (r.status === 403) {
    const j = await r.json().catch(() => ({}));
    if (j.redirect) { window.location.href = j.redirect; throw new Error(j.error || 'Acesso negado'); }
  }
  const j = await r.json().catch(() => ({ ok: false, error: 'Resposta inválida' }));
  if (!r.ok || j.ok === false) throw new Error(j.error || `Erro ${r.status}`);
  return j;
}

// ── Formatação ──
const BRL = new Intl.NumberFormat('pt-BR', { style: 'currency', currency: 'BRL' });
function money(v) { return BRL.format(Number(v || 0)); }
function moneyShort(v) {
  const n = Number(v || 0);
  if (Math.abs(n) >= 1000) return 'R$ ' + (n / 1000).toLocaleString('pt-BR', { maximumFractionDigits: 1 }) + 'k';
  return money(n);
}
function fmtDate(iso) {
  if (!iso) return '—';
  const d = new Date(iso + (iso.length === 10 ? 'T00:00:00' : ''));
  return d.toLocaleDateString('pt-BR');
}
function fmtMesAno(ym) {
  const [a, m] = ym.split('-');
  const meses = ['jan', 'fev', 'mar', 'abr', 'mai', 'jun', 'jul', 'ago', 'set', 'out', 'nov', 'dez'];
  return `${meses[parseInt(m, 10) - 1]}/${a.slice(2)}`;
}
function hoje() { return new Date().toISOString().slice(0, 10); }
function escapeHtml(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, c =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

// ── Toast ──
function toast(msg, tipo = 'ok') {
  let t = document.getElementById('toast');
  if (!t) { t = document.createElement('div'); t.id = 'toast'; document.body.appendChild(t); }
  t.textContent = msg;
  t.className = 'show ' + tipo;
  clearTimeout(t._tmr);
  t._tmr = setTimeout(() => t.classList.remove('show'), 3200);
}

// ── Sidebar / navbar ──
const NAV_ITEMS = [
  { href: '/',            ico: '◧', label: 'Dashboard' },
  { href: '/lancamentos', ico: '✎', label: 'Lançamentos' },
  { href: '/contas',      ico: '↹', label: 'A pagar / receber' },
  { href: '/recorrencias', ico: '↻', label: 'Recorrências' },
  { href: '/impostos',    ico: '%', label: 'Impostos' },
  { href: '/cadastros',   ico: '⚙', label: 'Cadastros' },
];

function renderShell(active) {
  const links = NAV_ITEMS.map(i =>
    `<a href="${i.href}" class="${i.href === active ? 'active' : ''}">
       <span class="ico">${i.ico}</span><span>${i.label}</span></a>`).join('');
  const sb = document.createElement('aside');
  sb.className = 'sidebar';
  sb.innerHTML = `
    <div class="brand">
      <div class="logo">J</div>
      <div><div class="name">Gestão JOGA</div><div class="sub">financeiro</div></div>
    </div>
    <nav class="nav">${links}</nav>
    <div class="foot"><span id="nav-user">—</span> · <a href="/logout">sair</a></div>`;
  document.body.prepend(sb);
  api('/api/me').then(j => {
    const el = document.getElementById('nav-user');
    if (el) el.textContent = (j.nome || '').split(' ')[0] || 'usuário';
  }).catch(() => {});
}

// ── Modal helpers ──
function openModal(id) { document.getElementById(id).classList.add('open'); }
function closeModal(id) { document.getElementById(id).classList.remove('open'); }

// ── Cadastro inline de cliente (resolve "cadastrar na hora de fechar") ──
// Mini-modal no tema dark, reutilizável em qualquer página que tenha um <select> de cliente.
let _clienteTargetSelect = null;

function _ensureClienteModal() {
  if (document.getElementById('modalNovoCliente')) return;
  const div = document.createElement('div');
  div.className = 'modal-bg';
  div.id = 'modalNovoCliente';
  div.innerHTML = `
    <div class="modal" style="max-width:480px">
      <h2>Novo cliente</h2>
      <form id="formNovoCliente">
        <div class="row2">
          <div class="field"><label>Nome *</label><input type="text" id="nc-nome" required></div>
          <div class="field"><label>Tipo</label>
            <select id="nc-tipo">
              <option value="cliente">Cliente</option>
              <option value="fornecedor">Fornecedor</option>
              <option value="ambos">Ambos</option>
            </select></div>
        </div>
        <div class="field"><label>CNPJ / CPF</label><input type="text" id="nc-doc"></div>
        <div class="row2">
          <div class="field"><label>E-mail</label><input type="email" id="nc-email"></div>
          <div class="field"><label>Telefone</label><input type="text" id="nc-tel"></div>
        </div>
        <div class="modal-actions">
          <button type="button" class="btn btn-ghost" onclick="closeModal('modalNovoCliente')">Cancelar</button>
          <button type="submit" class="btn btn-primary" id="nc-salvar">Salvar cliente</button>
        </div>
      </form>
    </div>`;
  document.body.appendChild(div);

  document.getElementById('formNovoCliente').addEventListener('submit', async (e) => {
    e.preventDefault();
    const nome = document.getElementById('nc-nome').value.trim();
    if (!nome) { toast('Informe o nome.', 'err'); return; }
    const btn = document.getElementById('nc-salvar');
    btn.disabled = true; btn.textContent = 'Salvando…';
    try {
      const j = await api('/api/contatos', { method: 'POST', body: {
        nome,
        tipo: document.getElementById('nc-tipo').value,
        documento: document.getElementById('nc-doc').value || null,
        email: document.getElementById('nc-email').value || null,
        telefone: document.getElementById('nc-tel').value || null,
      }});
      if (_clienteTargetSelect) {
        const o = document.createElement('option');
        o.value = j.row.id; o.textContent = j.row.nome;
        _clienteTargetSelect.appendChild(o);
        _clienteTargetSelect.value = j.row.id;
      }
      closeModal('modalNovoCliente');
      toast('Cliente cadastrado!', 'ok');
    } catch (err) { toast(err.message, 'err'); }
    btn.disabled = false; btn.textContent = 'Salvar cliente';
  });
}

function novoContatoInline(selectEl) {
  _ensureClienteModal();
  _clienteTargetSelect = selectEl;
  document.getElementById('formNovoCliente').reset();
  openModal('modalNovoCliente');
  setTimeout(() => document.getElementById('nc-nome').focus(), 50);
}
