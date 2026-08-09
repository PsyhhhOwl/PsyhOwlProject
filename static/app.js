const tg = window.Telegram?.WebApp;
const state = {
  token: localStorage.getItem('psyhowl_token') || '',
  me: null,
  paymentUrl: '',
  supportUsername: '',
  page: 'home',
  recorder: null,
  stream: null,
  chunks: [],
  recordingStarted: 0,
  timer: null,
  speakReplies: localStorage.getItem('psyhowl_speak') !== '0',
  activeAudio: null,
};

const $ = (id) => document.getElementById(id);
const pages = ['home', 'chat', 'practices', 'journal', 'profile', 'admin'];

function haptic(type = 'light') {
  try { tg?.HapticFeedback?.impactOccurred(type); } catch (_) {}
}

function notify(message, isError = false) {
  const el = $('toast');
  el.textContent = message;
  el.classList.toggle('error', isError);
  el.classList.add('show');
  clearTimeout(notify.timer);
  notify.timer = setTimeout(() => el.classList.remove('show'), 2600);
}

async function api(path, options = {}) {
  const headers = new Headers(options.headers || {});
  if (state.token) headers.set('Authorization', `Bearer ${state.token}`);
  if (options.body && !(options.body instanceof FormData) && typeof options.body !== 'string') {
    headers.set('Content-Type', 'application/json');
    options.body = JSON.stringify(options.body);
  }
  const res = await fetch(path, { ...options, headers });
  if (!res.ok) {
    let detail = `Ошибка ${res.status}`;
    try { detail = (await res.json()).detail || detail; } catch (_) {}
    const error = new Error(detail);
    error.status = res.status;
    throw error;
  }
  const type = res.headers.get('content-type') || '';
  if (type.includes('application/json')) return res.json();
  return res.blob();
}

async function authenticate() {
  if (!tg?.initData) {
    throw new Error('Открой мини‑приложение через Telegram-бота — так мы безопасно определим твой аккаунт.');
  }
  tg.ready();
  tg.expand();
  try { tg.setHeaderColor('#0b1121'); tg.setBackgroundColor('#080d1a'); } catch (_) {}

  try {
    const data = await api('/api/auth/telegram', { method: 'POST', body: { init_data: tg.initData } });
    state.token = data.token;
    localStorage.setItem('psyhowl_token', state.token);
  } catch (e) {
    localStorage.removeItem('psyhowl_token');
    throw e;
  }
}

async function loadMe() {
  const data = await api('/api/me');
  state.me = data.user;
  state.paymentUrl = data.payment_url || '';
  state.supportUsername = data.support_username || '';
  $('priceText').textContent = `${Number(data.subscription_price_rub || 12990).toLocaleString('ru-RU')} ₽`;
  renderIdentity();
  return data;
}

function renderIdentity() {
  const u = state.me;
  if (!u) return;
  const name = u.first_name || u.username || 'Друг';
  const initial = name.trim().slice(0, 1).toUpperCase() || 'С';
  $('avatarInitial').textContent = initial;
  $('profileAvatar').textContent = initial;
  $('profileName').textContent = name;
  $('profileUsername').textContent = u.username ? `@${u.username}` : `Telegram ID ${u.telegram_id}`;
  $('welcomeTitle').textContent = `${greeting()}, ${name}?`;
  $('accessDot').classList.toggle('off', !u.has_access);
  $('adminOpenButton').classList.toggle('hidden', !u.is_admin);

  const badge = $('subscriptionBadge');
  const subText = $('subscriptionText');
  if (u.is_admin) {
    badge.textContent = u.role === 'owner' ? 'Owner · Free' : 'Admin · Free';
    subText.textContent = 'бесплатный доступ администратора';
  } else if (u.is_free) {
    badge.textContent = 'Free access';
    subText.textContent = 'бесплатный доступ';
  } else if (u.has_access) {
    badge.textContent = 'Premium';
    subText.textContent = u.subscription_expires_at ? `до ${formatDate(u.subscription_expires_at)}` : 'активна';
  } else {
    badge.textContent = 'Доступ не активен';
    subText.textContent = 'нужно оформить подписку';
  }
}

function greeting() {
  const h = new Date().getHours();
  if (h < 6) return 'Не спится';
  if (h < 12) return 'Как твоё утро';
  if (h < 18) return 'Как ты сегодня';
  return 'Как прошёл твой день';
}

function formatDate(value) {
  return new Intl.DateTimeFormat('ru-RU', { day: 'numeric', month: 'long', year: 'numeric' }).format(new Date(value));
}

function showMain() {
  $('loading').classList.add('hidden');
  $('fatal').classList.add('hidden');
  $('app').classList.remove('hidden');
  if (!state.me.has_access) {
    $('paywall').classList.remove('hidden');
    pages.forEach(p => $(p)?.classList.add('hidden'));
    $('bottomNav').classList.add('hidden');
  } else {
    $('paywall').classList.add('hidden');
    $('bottomNav').classList.remove('hidden');
    navigate('home');
  }
}

function showFatal(message) {
  $('loading').classList.add('hidden');
  $('fatal').classList.remove('hidden');
  $('fatalText').textContent = message;
}

function navigate(page) {
  if (!state.me?.has_access && page !== 'paywall') return;
  if (page === 'admin' && !state.me?.is_admin) return;
  state.page = page;
  pages.forEach(p => $(p)?.classList.toggle('hidden', p !== page));
  document.querySelectorAll('.bottom-nav button').forEach(b => b.classList.toggle('active', b.dataset.nav === page));
  window.scrollTo({ top: 0, behavior: 'smooth' });
  if (page === 'chat') loadHistory();
  if (page === 'journal') loadMoodHistory();
  if (page === 'admin') loadAdmin();
  haptic('light');
}

function openPayment() {
  if (!state.paymentUrl) {
    notify('Ссылка Tribute пока не добавлена в настройках сервера', true);
    return;
  }
  haptic('medium');
  try { tg?.openLink(state.paymentUrl); }
  catch (_) { window.open(state.paymentUrl, '_blank', 'noopener'); }
}

async function recheckAccess() {
  try {
    await loadMe();
    if (state.me.has_access) {
      notify('Доступ активирован ✦');
      showMain();
    } else {
      notify('Оплата пока не подтверждена. Если только что оплатил — подожди несколько секунд.');
    }
  } catch (e) { notify(e.message, true); }
}

function escapeHtml(s) {
  return String(s).replace(/[&<>'"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;' }[c]));
}

function addMessage(role, text, animate = true) {
  const box = $('messages');
  const el = document.createElement('div');
  el.className = `message ${role}`;
  const label = role === 'assistant' ? 'Совёнок' : 'Ты';
  el.innerHTML = `${escapeHtml(text)}<div class="message-meta">${label}</div>`;
  if (animate) {
    el.style.opacity = '0';
    el.style.transform = 'translateY(8px)';
    requestAnimationFrame(() => {
      el.style.transition = '.25s'; el.style.opacity = '1'; el.style.transform = 'none';
    });
  }
  box.appendChild(el);
  setTimeout(() => el.scrollIntoView({ behavior: 'smooth', block: 'end' }), 50);
}

async function loadHistory() {
  try {
    const data = await api('/api/history');
    $('messages').innerHTML = '';
    if (!data.messages.length) {
      addMessage('assistant', 'Я здесь. Можешь начать с того, что сейчас больше всего занимает мысли. Не нужно формулировать красиво.');
      return;
    }
    data.messages.forEach(m => addMessage(m.role, m.text, false));
  } catch (e) {
    if (e.status === 402) showMain(); else notify(e.message, true);
  }
}

function setTyping(on) { $('typing').classList.toggle('hidden', !on); }

async function sendText(text = null) {
  const input = $('chatInput');
  const value = (text ?? input.value).trim();
  if (!value) return;
  if (text == null) input.value = '';
  navigate('chat');
  addMessage('user', value);
  setTyping(true);
  try {
    const data = await api('/api/chat', { method: 'POST', body: { text: value } });
    addMessage('assistant', data.text);
    if (data.crisis) haptic('heavy');
    if (state.speakReplies) speak(data.text);
  } catch (e) {
    if (e.status === 402) { await loadMe(); showMain(); }
    else notify(e.message, true);
  } finally { setTyping(false); }
}

async function speak(text) {
  try {
    if (state.activeAudio) { state.activeAudio.pause(); state.activeAudio = null; }
    const blob = await api('/api/speech', { method: 'POST', body: { text } });
    const url = URL.createObjectURL(blob);
    const audio = new Audio(url);
    state.activeAudio = audio;
    audio.onended = () => { URL.revokeObjectURL(url); if (state.activeAudio === audio) state.activeAudio = null; };
    await audio.play();
  } catch (e) {
    console.warn('TTS:', e);
  }
}

function bestMime() {
  const variants = ['audio/mp4', 'audio/webm;codecs=opus', 'audio/webm'];
  return variants.find(x => window.MediaRecorder?.isTypeSupported?.(x)) || '';
}

async function startRecording(source = 'home') {
  if (!navigator.mediaDevices?.getUserMedia || !window.MediaRecorder) {
    notify('На этом устройстве запись голоса недоступна', true); return;
  }
  try {
    state.stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true } });
    const mime = bestMime();
    state.recorder = mime ? new MediaRecorder(state.stream, { mimeType: mime }) : new MediaRecorder(state.stream);
    state.chunks = [];
    state.recordingStarted = Date.now();
    state.recorder.ondataavailable = e => { if (e.data.size) state.chunks.push(e.data); };
    state.recorder.onstop = () => finishRecording(source, mime || state.recorder.mimeType);
    state.recorder.start(250);
    setRecordingUI(true, source);
    haptic('medium');
  } catch (e) {
    notify('Нужен доступ к микрофону для голосового разговора', true);
  }
}

function stopRecording(source = 'home') {
  if (state.recorder?.state === 'recording') state.recorder.stop();
  setRecordingUI(false, source);
  state.stream?.getTracks().forEach(t => t.stop());
  state.stream = null;
}

function setRecordingUI(on, source) {
  const homeButton = $('voiceButton');
  const chatMic = $('chatMic');
  homeButton.classList.toggle('recording', on && source === 'home');
  chatMic.classList.toggle('recording', on && source === 'chat');
  if (source === 'home') {
    $('voiceTitle').textContent = on ? 'Я слушаю…' : 'Нажми и расскажи';
    $('voiceHint').textContent = on ? 'Говори в своём темпе. Нажми ещё раз, когда закончишь.' : 'Можно говорить столько, сколько нужно. После записи Совёнок ответит голосом.';
    $('voiceTimer').classList.toggle('hidden', !on);
  }
  clearInterval(state.timer);
  if (on) {
    state.timer = setInterval(() => {
      const sec = Math.floor((Date.now() - state.recordingStarted) / 1000);
      const mm = String(Math.floor(sec / 60)).padStart(2, '0');
      const ss = String(sec % 60).padStart(2, '0');
      $('voiceTimer').querySelector('b').textContent = `${mm}:${ss}`;
    }, 500);
  }
}

async function finishRecording(source, mime) {
  if (!state.chunks.length) return;
  if (Date.now() - state.recordingStarted < 650) { notify('Запись получилась слишком короткой'); return; }
  const blob = new Blob(state.chunks, { type: mime || 'audio/webm' });
  const ext = (mime || '').includes('mp4') ? 'm4a' : 'webm';
  const form = new FormData();
  form.append('audio', blob, `voice.${ext}`);
  if (source === 'home') navigate('chat');
  setTyping(true);
  notify('Слушаю и разбираю твои слова…');
  try {
    const data = await api('/api/voice', { method: 'POST', body: form });
    addMessage('user', data.transcript);
    addMessage('assistant', data.text);
    if (state.speakReplies) speak(data.text);
  } catch (e) {
    if (e.status === 402) { await loadMe(); showMain(); }
    else notify(e.message, true);
  } finally { setTyping(false); }
}

function toggleRecording(source) {
  if (state.recorder?.state === 'recording') stopRecording(source);
  else startRecording(source);
}

async function saveMood(score, note = '') {
  try {
    await api('/api/mood', { method: 'POST', body: { score: Number(score), note } });
    notify('Сохранил. Спасибо, что заметил своё состояние 🤍');
    haptic('light');
    loadMoodHistory();
  } catch (e) { notify(e.message, true); }
}

async function loadMoodHistory() {
  try {
    const data = await api('/api/mood');
    const box = $('moodHistory');
    if (!data.entries.length) { box.innerHTML = '<p class="muted">Здесь появится твоя динамика.</p>'; return; }
    box.innerHTML = data.entries.slice(-10).reverse().map(x => `
      <div class="mood-history-row">
        <span>${new Date(x.created_at).toLocaleDateString('ru-RU', {day:'2-digit',month:'short'})}</span>
        <div class="mood-history-bar"><i style="width:${x.score * 10}%"></i></div>
        <b>${x.score}/10</b>
      </div>`).join('');
  } catch (_) {}
}

const practices = {
  grounding: { title: 'Вернуться в настоящий момент', intro: 'Эта практика не должна убрать эмоцию. Её задача — помочь нервной системе заметить: прямо сейчас ты здесь.', steps: ['Оглянись и спокойно назови 5 вещей, которые видишь.', 'Заметь 4 ощущения тела: опора стоп, одежда на коже, температура воздуха.', 'Отметь 3 звука — даже самые тихие.', 'Назови 2 запаха или вкуса, которые можешь заметить.', 'Сделай один медленный выдох и спроси себя: «Что мне нужно в следующие 10 минут?»'] },
  sleep: { title: 'Мягкое завершение дня', intro: 'Не заставляем себя заснуть. Сначала снижаем внутреннюю борьбу.', steps: ['Устрой тело настолько удобно, насколько возможно.', 'Назови про себя три вещи, которые сегодня уже закончились.', 'Заметь напряжение в челюсти и плечах и позволь им стать хотя бы на 5% мягче.', 'Скажи себе: «Мне не нужно решить всё сегодня».', 'Переведи внимание на обычное естественное дыхание.'] },
  thought: { title: 'Разговор с автоматической мыслью', intro: 'Мысль может быть очень убедительной и при этом не быть полным описанием реальности.', steps: ['Запиши мысль одним предложением.', 'Что в этой мысли является наблюдаемым фактом?', 'Что здесь является предположением или прогнозом?', 'Какие факты делают картину чуть сложнее?', 'Как звучала бы более точная и менее жестокая версия этой мысли?'] },
  compassion: { title: 'Поддержать себя', intro: 'Самосострадание — не жалость и не разрешение ничего не делать. Это способ перестать добивать себя в момент, когда и так трудно.', steps: ['Назови то, что сейчас тяжело, без оценки.', 'Представь, что близкий человек переживает то же самое. Что бы ты сказал ему?', 'Попробуй адресовать эти же слова себе.', 'Выбери один маленький заботливый шаг на ближайший час.'] },
  breath: { title: 'Дыхание 4–6', intro: 'Если от дыхательных практик становится некомфортно или кружится голова — вернись к обычному дыханию.', breath: true, steps: ['Спокойный вдох примерно на 4 счёта.', 'Мягкий выдох примерно на 6 счётов.', 'Не делай вдох глубже, чем хочется. Повтори 6–10 циклов.'] },
};

function openPractice(key) {
  const p = practices[key]; if (!p) return;
  $('practiceBody').innerHTML = `<div class="eyebrow">ПРАКТИКА</div><h2>${p.title}</h2><p>${p.intro}</p>${p.breath ? '<div class="breath-circle">вдох · выдох</div>' : ''}<ol>${p.steps.map(x => `<li>${x}</li>`).join('')}</ol><p class="muted">Если упражнение усиливает дискомфорт, его можно остановить. Тебе не нужно выполнять практику «правильно».</p>`;
  $('practiceModal').classList.remove('hidden');
  haptic('light');
}

function closePractice() { $('practiceModal').classList.add('hidden'); }

async function loadAdmin() {
  if (!state.me?.is_admin) return;
  try {
    const [stats, users] = await Promise.all([api('/api/admin/stats'), api('/api/admin/users')]);
    $('adminStats').innerHTML = `
      <div class="stat-card"><strong>${stats.users}</strong><small>пользователей</small></div>
      <div class="stat-card"><strong>${stats.active_access}</strong><small>с доступом</small></div>
      <div class="stat-card"><strong>${stats.messages}</strong><small>сообщений</small></div>`;
    $('adminUsers').innerHTML = users.users.map(u => `
      <div class="admin-user">
        <div><b>${escapeHtml(u.first_name || u.username || `ID ${u.telegram_id}`)}</b><small>${u.username ? '@'+escapeHtml(u.username)+' · ' : ''}${u.telegram_id}${u.is_blocked ? ' · BLOCKED' : ''}</small></div>
        <span>${u.role}${u.has_access ? ' · access' : ''}${u.is_free ? ' · free' : ''}</span>
      </div>`).join('');
  } catch (e) { notify(e.message, true); }
}

async function adminAction(action) {
  const telegram_id = Number($('adminTgId').value.trim());
  if (!telegram_id) { notify('Введи Telegram ID', true); return; }
  const map = {
    grant: ['/api/admin/access/grant', { telegram_id, days: 30 }],
    free: ['/api/admin/free/toggle', { telegram_id }],
    addAdmin: ['/api/admin/admins/add', { telegram_id }],
    removeAdmin: ['/api/admin/admins/remove', { telegram_id }],
    revoke: ['/api/admin/access/revoke', { telegram_id }],
    block: ['/api/admin/block/toggle', { telegram_id }],
  };
  try {
    await api(map[action][0], { method: 'POST', body: map[action][1] });
    notify('Готово'); haptic('medium'); loadAdmin();
  } catch (e) { notify(e.message, true); }
}

function bindEvents() {
  document.querySelectorAll('[data-nav]').forEach(el => el.addEventListener('click', () => navigate(el.dataset.nav)));
  document.querySelectorAll('[data-prompt]').forEach(el => el.addEventListener('click', () => sendText(el.dataset.prompt)));
  document.querySelectorAll('.practice-start').forEach(el => el.addEventListener('click', () => openPractice(el.dataset.practice)));
  document.querySelectorAll('[data-close-modal]').forEach(el => el.addEventListener('click', closePractice));
  document.querySelectorAll('[data-admin-action]').forEach(el => el.addEventListener('click', () => adminAction(el.dataset.adminAction)));

  $('payButton').addEventListener('click', openPayment);
  $('checkPaymentButton').addEventListener('click', recheckAccess);
  $('subscriptionRow').addEventListener('click', () => state.me?.has_access ? notify($('subscriptionText').textContent) : openPayment());
  $('voiceButton').addEventListener('click', () => toggleRecording('home'));
  $('chatMic').addEventListener('click', () => toggleRecording('chat'));
  $('sendButton').addEventListener('click', () => sendText());
  $('chatInput').addEventListener('keydown', e => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendText(); } });
  $('chatInput').addEventListener('input', e => { e.target.style.height = 'auto'; e.target.style.height = `${Math.min(e.target.scrollHeight, 110)}px`; });
  $('speakToggle').classList.toggle('active', state.speakReplies);
  $('speakToggle').addEventListener('click', () => {
    state.speakReplies = !state.speakReplies;
    localStorage.setItem('psyhowl_speak', state.speakReplies ? '1' : '0');
    $('speakToggle').classList.toggle('active', state.speakReplies);
    notify(state.speakReplies ? 'Голосовые ответы включены' : 'Голосовые ответы выключены');
  });

  $('moodScale').querySelectorAll('button').forEach(btn => btn.addEventListener('click', () => {
    $('moodScale').querySelectorAll('button').forEach(x => x.classList.remove('selected'));
    btn.classList.add('selected'); saveMood(btn.dataset.score);
  }));
  $('journalRange').addEventListener('input', e => $('rangeValue').textContent = `${e.target.value} / 10`);
  $('saveMoodButton').addEventListener('click', async () => {
    await saveMood($('journalRange').value, $('journalNote').value.trim()); $('journalNote').value = '';
  });
  $('supportButton').addEventListener('click', () => {
    if (state.supportUsername) {
      const url = `https://t.me/${state.supportUsername.replace('@','')}`;
      try { tg?.openTelegramLink(url); } catch (_) { location.href = url; }
    } else notify('Контакт поддержки пока не указан');
  });
  $('adminOpenButton').addEventListener('click', () => navigate('admin'));
  $('refreshAdmin').addEventListener('click', loadAdmin);

  document.addEventListener('visibilitychange', () => {
    if (!document.hidden && !state.me?.has_access) recheckAccess();
  });
}

async function boot() {
  bindEvents();
  try {
    await authenticate();
    await loadMe();
    showMain();
  } catch (e) {
    console.error(e);
    showFatal(e.message || 'Не удалось открыть Совёнка.');
  }
}

boot();
