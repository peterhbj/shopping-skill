const token = document.querySelector('meta[name="app-token"]').content;
const $ = id => document.getElementById(id);
const esc = value => String(value ?? '').replace(/[&<>"']/g, ch => ({
  '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
}[ch]));

let firstLoad = true;
let editing = false;
let latest = null;

async function api(path, data) {
  const response = await fetch(path, {
    method: data === undefined ? 'GET' : 'POST',
    headers: data === undefined ? {} : {'Content-Type': 'application/json', 'X-App-Token': token},
    body: data === undefined ? undefined : JSON.stringify(data)
  });
  const result = await response.json();
  if (!response.ok) throw Error(result.error || `HTTP ${response.status}`);
  return result;
}

function notice(message) { $('notice').textContent = message || ''; }
function fmtMoney(value) {
  return typeof value === 'number'
    ? new Intl.NumberFormat('pt-BR', {style: 'currency', currency: 'BRL'}).format(value)
    : '—';
}

function drawPlan(plan) {
  const rows = plan?.items || [];
  const pending = rows.filter(item => item.status === 'needs_user' ||
    (item.status === 'not_found' && !item.skipped_by_user));
  const decided = rows.filter(item => item.status === 'decided');
  $('plan-summary').textContent = rows.length
    ? `${decided.length} escolhidos · ${pending.length} pendentes · ${rows.length} itens`
    : '';
  $('apply-button').disabled = !rows.length || pending.length > 0 || latest?.job?.running;

  if (!rows.length) {
    $('plan-items').innerHTML = '<div class="empty-state"><span class="empty-icon" aria-hidden="true">◫</span><h3>Seu plano aparece aqui</h3><p>Gere um plano para comparar produtos, preços e dúvidas antes do carrinho.</p></div>';
    return;
  }

  $('plan-items').innerHTML = rows.map((item, index) => {
    const needsReview = item.status === 'needs_user' ||
      (item.status === 'not_found' && !item.skipped_by_user);
    const candidates = needsReview
      ? (item.candidates || []).slice(0, 8).map(candidate => `
          <button type="button" data-index="${index}" data-candidate="${Number(candidate.index)}">
            <strong>${esc(candidate.name)}</strong>
            ${fmtMoney(candidate.price_num)} · ${esc(candidate.brand_name || 'marca não informada')}
          </button>`).join('') +
          `<button type="button" data-index="${index}" data-skip="1" class="secondary">Pular este item</button>`
      : '';
    const jevChoice = item.jev?.candidate;
    const jevProduct = jevChoice
      ? (item.candidates || []).find(candidate => candidate.index === jevChoice.index)
      : null;
    const jevNote = item.jev?.error
      ? `Jev indisponível: ${esc(item.jev.error)}`
      : item.jev
        ? `Jev (observação): ${esc(jevProduct?.name || 'pedir sua decisão')} · confiança ${esc(item.jev.confidence ?? 'não informada')}`
        : '';
    const state = item.skipped_by_user ? 'pulado'
      : item.status === 'decided' ? 'decidido' : 'revisar';
    const explanation = item.chosen_name || item.notes?.join(' · ') || 'Sem resultado';
    const history = item.history_evidence?.length
      ? `Histórico relevante: ${esc(item.history_evidence[0].name)}` : '';
    return `<article class="item">
      <div>
        <h3>${esc(item.raw)}</h3>
        <p class="${item.status === 'decided' ? 'chosen' : ''}">${esc(explanation)} ${item.chosen_name ? '· ' + fmtMoney(item.total_cost) : ''}</p>
        ${item.rule || history ? `<p>${esc(item.rule || '')}${item.rule && history ? ' · ' : ''}${history}</p>` : ''}
        ${jevNote ? `<p class="hint">${jevNote}</p>` : ''}
      </div>
      <span class="state ${esc(item.status)}">${state}</span>
      ${candidates ? `<div class="candidate-list">${candidates}</div>` : ''}
    </article>`;
  }).join('');
}

async function refresh() {
  try {
    const state = await api('/api/state');
    latest = state;
    if ($('notice').textContent === 'Failed to fetch') notice('');
    $('count-orders').textContent = state.history.orders;
    $('count-lines').textContent = state.history.lines;
    $('history-status').textContent = state.job.message ||
      (state.history.orders
        ? `Histórico com ${state.history.orders} pedidos; cobertura ainda não verificada.`
        : 'Histórico incompleto: nenhum pedido concluído importado.');
    $('progress').textContent = state.job.running
      ? `${state.job.stage}${state.job.done !== undefined ? ' · ' + state.job.done + '/' + state.job.total : ''}`
      : state.job.error || '';
    if (state.job.error) notice(state.job.error);
    if (firstLoad) {
      $('shopping-list').value = state.list;
      firstLoad = false;
    }
    if (!editing && document.activeElement !== $('shopping-list') && state.list !== $('shopping-list').value) {
      $('shopping-list').value = state.list;
    }
    $('luna-review').textContent = state.review || '';
    $('report').textContent = state.job.report || '';
    drawPlan(state.plan);
  } catch (error) {
    notice(error.message);
  }
}

async function act(action) {
  try {
    notice('');
    await action();
    await refresh();
  } catch (error) {
    notice(error.message);
  }
}

$('shopping-list').addEventListener('input', () => { editing = true; });
$('plan-button').addEventListener('click', () => act(async () => {
  editing = false;
  await api('/api/plan', {list: $('shopping-list').value});
}));
$('andorinha-login').addEventListener('click', () => act(async () => {
  const result = await api('/api/andorinha/login', {});
  notice(result.message);
}));
$('andorinha-done').addEventListener('click', () => act(async () => {
  await api('/api/andorinha/login/done', {});
  notice('Sessão de login fechada. Agora tente sincronizar.');
}));
$('history-sync').addEventListener('click', () => act(async () => {
  await api('/api/history/sync', {});
}));
$('history-file').addEventListener('change', event => act(async () => {
  const file = event.target.files[0];
  if (!file) return;
  const result = await api('/api/history/import', {filename: file.name, contents: await file.text()});
  notice(`${result.added} pedidos importados; ${result.skipped} ignorados.`);
  event.target.value = '';
}));
$('jev-save').addEventListener('click', () => act(async () => {
  const key = $('jev-key').value;
  await api('/api/jev/key', {key});
  $('jev-key').value = '';
  notice(key ? 'Jev configurado nesta sessão.' : 'Jev desativado.');
}));
$('luna-login').addEventListener('click', () => act(async () => {
  const login = await api('/api/reviewer/login', {});
  const url = login.verificationUrl || login.authUrl;
  const code = login.userCode || '';
  $('luna-device').innerHTML = url
    ? `Abra <a target="_blank" rel="noopener noreferrer" href="${esc(url)}">${esc(url)}</a> e informe <strong>${esc(code)}</strong>.`
    : 'Verifique a janela de login do Codex.';
}));
$('plan-items').addEventListener('click', event => {
  const button = event.target.closest('button[data-index]');
  if (!button) return;
  act(async () => {
    const data = {index: Number(button.dataset.index)};
    if (button.dataset.skip) data.skip = true;
    else data.candidate_index = Number(button.dataset.candidate);
    await api('/api/resolve', data);
  });
});
$('apply-button').addEventListener('click', () => {
  if (!confirm('Adicionar ao carrinho as escolhas revisadas? O checkout continuará manual.')) return;
  act(async () => { await api('/api/apply', {}); });
});

async function lunaStatus() {
  try {
    const status = await api('/api/reviewer/status');
    $('luna-login').hidden = !status.needs_login;
    $('luna-state').textContent = status.label || (status.ready ? 'Pronto' : 'Indisponível');
  } catch {
    $('luna-login').hidden = true;
    $('luna-state').textContent = 'Assistente indisponível';
  }
}

refresh();
lunaStatus();
setInterval(refresh, 2200);
setInterval(lunaStatus, 30000);
