'use strict';
const $ = id => document.getElementById(id);
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const money = n => new Intl.NumberFormat('en-US', {style:'currency', currency:'USD', maximumFractionDigits:2}).format(Number(n));
const date = ts => new Date(ts * 1000).toLocaleString('uk-UA', {day:'2-digit',month:'2-digit',hour:'2-digit',minute:'2-digit'});
const time = ts => new Date(ts * 1000).toLocaleTimeString('uk-UA', {hour:'2-digit',minute:'2-digit'});
let state, token, toastTimer, refreshing = false;
function toast(message) { $('toast').textContent = message; $('toast').hidden = false; clearTimeout(toastTimer); toastTimer = setTimeout(() => $('toast').hidden = true, 7000); }
async function api(path, body) {
  const response = await fetch(path, body === undefined ? {} : {method:'POST', headers:{'Content-Type':'application/json','X-Orbit-Token':token}, body:JSON.stringify(body)});
  const data = await response.json();
  if (!response.ok) throw Error(data.error || 'Помилка запиту');
  return data;
}
async function command(kind, payload = {}, button) {
  if (!token) { toast('Дочекайся підключення до сервера'); return; }
  if (button) button.disabled = true;
  try {
    const queued = await api('/api/commands', {kind,payload,request_id:crypto.randomUUID()});
    let complete = false;
    for (let i = 0; i < 80; i++) {
      const result = await api('/api/commands/' + queued.id);
      if (result.status !== 'queued') { toast(result.result); complete = true; break; }
      await new Promise(resolve => setTimeout(resolve, 500));
    }
    if (!complete) toast('Команда залишається в черзі. Перевір стан ядра перед повторенням.');
    await refresh();
  } catch (e) { toast(e.message); }
  finally { if (button) button.disabled = false; }
}
function renderChart(data) {
  if (data.length < 2) { $('chart').innerHTML = '<div class="empty-state">Траєкторія з’явиться після двох циклів ядра.<br>Перші дані — приблизно за 30 секунд.</div>'; return; }
  const values = data.map(x => Number(x.value)), low = Math.min(...values), high = Math.max(...values);
  const padding = Math.max((high-low)*.3, high*.0005, 1), min = low-padding, max = high+padding;
  const x = i => 10 + i / (values.length - 1) * 620, y = v => 174 - (v-min)/(max-min)*155;
  const points = values.map((v,i) => `${x(i).toFixed(2)},${y(v).toFixed(2)}`).join(' ');
  const grid = Array.from({length:4}, (_,i) => { const v = min+(max-min)*i/3, py = y(v); return `<line x1="10" x2="635" y1="${py}" y2="${py}" stroke="#252b3c" stroke-dasharray="3 6"/><text x="650" y="${py+3}">${Math.round(v).toLocaleString('en-US')}</text>`; }).join('');
  $('chart').innerHTML = `<svg viewBox="0 0 720 190" preserveAspectRatio="none" aria-hidden="true"><defs><linearGradient id="fill" x1="0" y1="0" x2="0" y2="1"><stop stop-color="#fc925e" stop-opacity=".17"/><stop offset="1" stop-color="#fc925e" stop-opacity="0"/></linearGradient><linearGradient id="line"><stop stop-color="#ed629e"/><stop offset="1" stop-color="#ffae69"/></linearGradient></defs>${grid}<polygon points="10,190 ${points} 630,190" fill="url(#fill)"/><polyline points="${points}" stroke="url(#line)" stroke-width="2" fill="none" vector-effect="non-scaling-stroke"/><circle cx="630" cy="${y(values.at(-1))}" r="3" fill="#ffb981"/></svg>`;
  $('chart-start').textContent = time(data[0].ts); $('chart-end').textContent = time(data.at(-1).ts);
}
function render(s) {
  const p = s.portfolio, active = s.orders.filter(o => ['open','pending'].includes(o.status));
  const healthy = s.now - s.meta.heartbeat < 65;
  $('equity').textContent = money(p.equity); $('chart-value').textContent = money(p.equity);
  const pnl = Number(p.realized) + Number(p.unrealized);
  $('pnl').textContent = (pnl >= 0 ? '+' : '−') + money(Math.abs(pnl)); $('pnl').className = pnl >= 0 ? 'positive' : 'negative';
  $('pnl-detail').textContent = `${p.closed} закритих · ${p.closed ? Math.round(p.wins / p.closed * 100) : 0}% прибуткових`;
  $('active-count').textContent = String(active.length).padStart(2,'0');
  $('active-detail').textContent = `${active.filter(o => o.status === 'open').length} відкритих / ${active.filter(o => o.status === 'pending').length} очікують`;
  $('agent-status').textContent = !healthy ? 'Ядро недоступне' : s.meta.killed ? 'KILL активний' : s.meta.daily_tripped ? 'Ліміт втрати' : s.meta.auto ? 'На орбіті' : 'На паузі';
  $('heartbeat').textContent = healthy ? `PID ${s.meta.worker_pid} · ядро на зв’язку` : 'Перевір процес та системні події';
  $('agent-status').className = 'status-text ' + (healthy && !s.meta.killed ? 'positive' : 'negative');
  $('feed-badge').textContent = s.meta.feed === 'simulation' ? 'SIMULATION / СИНТЕТИЧНІ ЦІНИ' : 'COINBASE / РИНКОВІ ДАНІ';
  const stale = s.quotes.length < 3 || s.quotes.some(q => s.now - q.ts > s.quote_max_age || q.ts > s.now);
  $('feed-warning').hidden = !stale && s.meta.feed !== 'simulation';
  $('feed-warning').textContent = s.meta.feed === 'simulation' ? 'SIMULATION: синтетичні котировки для перевірки програми. Це не поточні ринкові ціни.' : 'Очікування свіжих котирувань. Без доступу до Coinbase виконання ордерів заблоковане.';
  const names = {'BTC-USD':['₿','Bitcoin','btc'],'ETH-USD':['Ξ','Ethereum','eth'],'SOL-USD':['≋','Solana','sol']};
  $('markets').innerHTML = Object.entries(names).map(([symbol,[icon,name,css]]) => {
    const q = s.quotes.find(q => q.symbol === symbol), age = q ? Math.max(0,Math.round(s.now-q.ts)) : null;
    return `<div class="market-row"><span class="coin ${css}">${icon}</span><div class="market-name">${name}<small>${symbol}</small></div><div class="market-price">${q ? money(q.price) : '—'}</div><div class="market-source">${esc(q?.source || 'Немає даних')}<b class="${!q || age > 90 ? 'stale' : ''}">${q ? age+' с тому' : 'OFFLINE'}</b></div></div>`;
  }).join('');
  $('autopilot-state').textContent = s.meta.auto ? 'RUNNING' : 'PAUSED';
  $('toggle-auto').textContent = s.meta.auto ? 'Ⅱ Пауза автопілота' : '▷ Запустити автопілот';
  $('analyst-provider').textContent = s.llm_configured ? 'Ollama · локальна мовна модель' : 'Шаблонний огляд · без мовної моделі';
  $('delivery-status').textContent = s.telegram_configured ? 'SQLite + Telegram outbox' : 'SQLite · Telegram не налаштовано';
  $('funds').innerHTML = [['Вільно',money(p.available)],['Зарезервовано',money(p.reserved)],['Готівковий залишок',money(p.cash)],['Закритий P&L',money(p.realized)],['Плаваючий P&L',money(p.unrealized)]].map(([a,b]) => `<div class="fund-row"><span>${a}</span><b>${b}</b></div>`).join('');
  $('orders-table').innerHTML = s.orders.length ? s.orders.map(o => `<tr><td>#${o.id}<small>${date(o.created)}</small></td><td>${esc(o.symbol)}<small>${esc(o.reason)}</small></td><td><span class="state ${esc(o.status)}">${esc(o.status.toUpperCase())}</span></td><td>${money(o.budget)}</td><td>${o.entry ? money(o.entry) : money(o.limit_price)}<small>${esc(o.kind)}</small></td><td>${o.stop ? money(o.stop) + ' / ' + money(o.target) : 'Після виконання'}</td><td class="${Number(o.pnl) >= 0 ? 'positive' : 'negative'}">${o.pnl === null ? '—' : money(o.pnl)}</td><td>${o.status === 'open' ? `<button class="button" data-action="close" data-id="${o.id}">Закрити</button>` : o.status === 'pending' ? `<button class="button" data-action="cancel" data-id="${o.id}">Скасувати</button>` : '—'}</td></tr>`).join('') : '<tr><td colspan="8"><div class="empty-state">Журнал порожній. Створи перший паперовий ордер або запусти автопілот.</div></td></tr>';
  $('reports').innerHTML = s.reports.length ? s.reports.map(r => `<article class="report-card"><div><span>#${r.id} · ${date(r.ts)}</span><span class="pill">${r.delivery === 'sent' ? 'TELEGRAM: SENT' : s.telegram_configured ? `PENDING · ${r.attempts} RETRIES` : 'LOCAL ONLY'}</span></div><pre>${esc(r.body)}</pre></article>`).join('') : '<div class="empty-state">Перший звіт створить торговий процес.</div>';
  $('events').innerHTML = s.events.map(e => `<div class="event-row ${esc(e.level)}"><time>${date(e.ts)}</time><span>${esc(e.message)}</span></div>`).join('') || '<div class="empty-state">Подій ще немає.</div>';
  $('system-info').innerHTML = [['Торговий процес',s.meta.worker_pid || 'Запуск…'],['Heartbeat',s.meta.heartbeat ? date(s.meta.heartbeat) : '—'],['База даних','SQLite / WAL / FULL'],['Режим виконання','PAPER · Decimal'],['Планувальник','monotonic / UTC reports'],['Добова база капіталу',money(s.meta.daily_equity)],['Добовий захист',s.meta.daily_tripped ? 'BLOCKED' : 'ARMED']].map(([a,b]) => `<div class="fund-row"><span>${a}</span><b>${esc(b)}</b></div>`).join('');
  $('kill-button').disabled = s.meta.killed; $('arm-button').hidden = !s.meta.killed;
  $('updated').textContent = 'SYNC ' + new Date().toLocaleTimeString('uk-UA');
  renderChart(s.equity);
}
async function refresh() {
  if (refreshing) return;
  refreshing = true;
  try { state = await api('/api/state'); token = state.csrf; render(state); $('connection-warning').hidden = true; }
  catch (e) { $('connection-warning').hidden = false; }
  finally { refreshing = false; }
}
document.querySelectorAll('[data-page]').forEach(button => button.addEventListener('click', () => {
  document.querySelectorAll('.page').forEach(page => page.hidden = page.id !== 'page-'+button.dataset.page);
  document.querySelectorAll('.nav').forEach(nav => nav.classList.toggle('active', nav === button));
  $('page-label').textContent = button.textContent.trim().replace(/^[^А-ЯІЇЄҐ]+/, '');
}));
$('kind').addEventListener('change', () => { const limit = $('kind').value === 'limit'; $('limit-label').hidden = !limit; $('limit-price').required = limit; });
$('order-form').addEventListener('submit', event => { event.preventDefault(); command('buy', {symbol:$('symbol').value,kind:$('kind').value,budget:$('budget').value,limit_price:$('limit-price').value}, event.submitter); });
$('orders-table').addEventListener('click', event => { const button = event.target.closest('[data-action]'); if (button) command(button.dataset.action,{id:Number(button.dataset.id)},button); });
$('toggle-auto').addEventListener('click', event => { if (state) command('auto',{enabled:!state.meta.auto},event.currentTarget); });
$('report-now').addEventListener('click', event => command('report',{},event.currentTarget));
$('kill-button').addEventListener('click', event => command('kill',{},event.currentTarget));
$('arm-button').addEventListener('click', event => command('arm',{},event.currentTarget));
function bubble(text, user=false, label='') { const item = document.createElement('div'); item.className = 'chat-bubble' + (user ? ' user' : ''); if (label) { const small = document.createElement('small'); small.textContent = label; item.append(small); } item.append(document.createTextNode(text)); $('chat-messages').append(item); $('chat-messages').scrollTop = $('chat-messages').scrollHeight; }
$('chat-form').addEventListener('submit', async event => {
  event.preventDefault(); const question = $('question').value.trim(); if (!question) return;
  const button = event.submitter; button.disabled = true; bubble(question,true); $('question').value = '';
  try { const result = await api('/api/chat',{question}); bubble(result.answer,false,result.provider); }
  catch (e) { bubble('Не вдалося отримати відповідь: '+e.message); }
  finally { button.disabled = false; }
});
setInterval(() => $('clock').textContent = new Date().toLocaleTimeString('uk-UA')+' LOCAL',1000);
refresh(); setInterval(refresh,4000);
