const form = document.querySelector('#download-form');
const submit = document.querySelector('#submit');
const statusBox = document.querySelector('#status');
const message = document.querySelector('#message');
const progress = document.querySelector('#progress');
const counts = document.querySelector('#counts');
const link = document.querySelector('#zip-link');
const retry = document.querySelector('#retry');
const cancelDownload = document.querySelector('#cancel-download');
let pollTimer;
let activeId;

function setRunning(running) {
  submit.disabled = running;
  document.querySelector('#find-profile').disabled = running;
  form.querySelectorAll('input').forEach(input => { input.disabled = running; });
  cancelDownload.hidden = !running;
}

// Some browsers block storage even though the application itself is reachable.
function storedJob(value) {
  try {
    if (value === undefined) return sessionStorage.getItem('archiveJob');
    if (value === null) sessionStorage.removeItem('archiveJob');
    else sessionStorage.setItem('archiveJob', value);
  } catch { /* Downloads and login also work without browser storage. */ }
  return null;
}

async function responseData(response) {
  try { return await response.json(); }
  catch { throw new Error('Invalid server response. Try again.'); }
}

async function poll() {
  clearTimeout(pollTimer);
  retry.hidden = true;
  try {
    const response = await fetch(`/api/jobs/${encodeURIComponent(activeId)}`);
    const job = await responseData(response);
    if (response.status === 404 || response.status === 410) {
      storedJob(null);
      activeId = null;
      setRunning(false);
      progress.hidden = true;
      counts.textContent = '';
      statusBox.classList.add('error');
      message.textContent = 'Download expired. Start a new one.';
      return;
    }
    if (!response.ok) throw new Error(job.error);
    statusBox.classList.remove('error');
    if (job.media_type) {
      form.querySelectorAll('input[name="media_type"]').forEach(input => { input.checked = input.value === job.media_type; });
    }
    if (job.sources) {
      form.querySelectorAll('input[name="sources"]').forEach(input => { input.checked = job.sources.includes(input.value); });
    }
    if (job.profile) showProfile(job.profile);
    message.textContent = job.message;
    counts.textContent = `${job.images} photos / ${job.videos || 0} videos / ${job.processed} of ${job.total || "..."} items`;
    if (job.total) { progress.max = job.total; progress.value = job.processed; }
    if (job.status === 'running') {
      cancelDownload.disabled = Boolean(job.cancel_requested);
      pollTimer = setTimeout(poll, 1500);
      return;
    }
    setRunning(false);
    progress.hidden = true;
    storedJob(null);
    if (job.status === 'cancelled') return;
    if (job.status === 'error') { statusBox.classList.add('error'); return; }
    link.href = `/api/jobs/${activeId}/download`;
    link.download = '';
    link.hidden = false;
    link.click();
  } catch (error) {
    message.textContent = 'Status unavailable. The download may still be running.';
    retry.hidden = false;
  }
}

cancelDownload.addEventListener('click', async () => {
  cancelDownload.disabled = true;
  try {
    const response = await fetch(`/api/jobs/${encodeURIComponent(activeId)}/cancel`, {
      method: 'POST', headers: {'Content-Type': 'application/json'}, body: '{}'
    });
    const job = await responseData(response);
    if (!response.ok) throw new Error(job.error);
    message.textContent = job.message;
    poll();
  } catch (error) {
    message.textContent = error.message || 'Could not cancel. Try again.';
    cancelDownload.disabled = false;
  }
});

retry.addEventListener('click', poll);
form.addEventListener('submit', async (event) => {
  event.preventDefault();
  const data = new FormData(form);
  const payload = {username: document.querySelector('#username').value, media_type: data.get('media_type'), sources: data.getAll('sources')};
  if (!payload.sources.length) {
    statusBox.hidden = false;
    message.textContent = 'Select at least one content source.';
    return;
  }
  setRunning(true);
  cancelDownload.hidden = true;
  cancelDownload.disabled = false;
  statusBox.hidden = false;
  statusBox.classList.remove('error');
  link.hidden = true;
  retry.hidden = true;
  progress.hidden = false;
  progress.removeAttribute('value');
  counts.textContent = '';
  message.textContent = 'Preparing download...';
  try {
    const response = await fetch('/api/jobs', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(payload)
    });
    const result = await responseData(response);
    if (!response.ok) throw new Error(result.error);
    activeId = result.id;
    storedJob(activeId);
    cancelDownload.hidden = false;
    poll();
  } catch (error) {
    message.textContent = error.message || 'Cannot reach the app.';
    statusBox.classList.add('error');
    setRunning(false);
    progress.hidden = true;
  }
});

function showProfile(profile) {
  document.querySelector('#profile-preview').hidden = false;
  document.querySelector('#profile-name').textContent = `@${profile.username}`;
  const avatar = document.querySelector('#profile-avatar');
  avatar.hidden = !profile.avatar;
  if (profile.avatar) avatar.src = profile.avatar;
  else avatar.removeAttribute('src');
}
document.querySelector('#profile-avatar').addEventListener('error', event => { event.target.hidden = true; });
document.querySelector('#username').addEventListener('input', () => {
  document.querySelector('#profile-preview').hidden = true;
  document.querySelector('#profile-error').hidden = true;
});
document.querySelector('#find-profile').addEventListener('click', async event => {
  const username = document.querySelector('#username').value;
  const errorBox = document.querySelector('#profile-error');
  event.target.disabled = true;
  errorBox.hidden = true;
  document.querySelector('#profile-preview').hidden = true;
  try {
    const response = await fetch('/api/profile', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({username})});
    const profile = await responseData(response);
    if (document.querySelector('#username').value !== username) return;
    if (!response.ok) throw new Error(profile.error);
    showProfile(profile);
  } catch (error) {
    if (document.querySelector('#username').value === username) {
      errorBox.textContent = error.message || 'Profile unavailable.';
      errorBox.hidden = false;
    }
  } finally {
    event.target.disabled = submit.disabled;
  }
});

activeId = storedJob();
if (activeId) { setRunning(true); statusBox.hidden = false; poll(); }

const loginForm = document.querySelector('#login-form');
const codeForm = document.querySelector('#two-factor-form');
const logoutButton = document.querySelector('#logout');
const authState = document.querySelector('#auth-state');
const authError = document.querySelector('#auth-error');
const authRefresh = document.querySelector('#auth-refresh');
const passwordInput = document.querySelector('#login-password');
const codeInput = document.querySelector('#two-factor-code');
const browserForm = document.querySelector('#browser-login-form');
const browserUsername = document.querySelector('#browser-username');
let authTimer;

function showAuth(state) {
  loginForm.hidden = state.authenticated || state.two_factor_required || state.browser_pending;
  browserForm.hidden = Boolean(state.browser_pending);
  if (state.username && !browserUsername.value) browserUsername.value = state.username;
  codeForm.hidden = !state.two_factor_required;
  logoutButton.hidden = !state.authenticated && !state.two_factor_required && !state.browser_pending;
  logoutButton.textContent = state.browser_pending || state.two_factor_required ? 'Cancel login' : 'Sign out';
  authState.textContent = state.browser_pending
    ? 'Sign in using the Instagram window.'
    : state.two_factor_required
    ? 'Enter your verification code within 10 minutes.'
    : state.authenticated ? `Signed in as @${state.username}` : 'Not signed in.';
  authRefresh.hidden = true;
  if (state.browser_message) authState.textContent += ` ${state.browser_message}`;
  if (state.browser_pending) authTimer = setTimeout(() => authRequest(), 1500);
  if (state.two_factor_required) codeInput.focus();
}

async function authRequest(payload) {
  clearTimeout(authTimer);
  let stateReceived = false;
  const controls = document.querySelectorAll('.auth-card button, .auth-card input');
  controls.forEach(control => { control.disabled = true; });
  authError.hidden = true;
  authRefresh.hidden = true;
  if (payload) authState.textContent = 'Processing...';
  try {
    const response = await fetch('/api/auth', payload ? {
      method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload)
    } : {});
    const state = await responseData(response);
    if (!response.ok) {
      if (typeof state.authenticated === 'boolean') {
        showAuth(state);
        stateReceived = true;
      }
      throw new Error(state.error || 'Request failed.');
    }
    showAuth(state);
  } catch (error) {
    authError.textContent = error.message || 'Cannot reach the app.';
    authError.hidden = false;
    if (!stateReceived) authState.textContent = 'Session unavailable. Retry login.';
    authRefresh.hidden = false;
  } finally {
    passwordInput.value = '';
    codeInput.value = '';
    controls.forEach(control => { control.disabled = false; });
    if (!codeForm.hidden) codeInput.focus();
  }
}

loginForm.addEventListener('submit', event => {
  event.preventDefault();
  const password = passwordInput.value;
  passwordInput.value = '';
  authRequest({action: 'login', username: document.querySelector('#login-username').value, password});
});
codeForm.addEventListener('submit', event => {
  event.preventDefault();
  authRequest({action: 'two_factor', code: codeInput.value});
});
logoutButton.addEventListener('click', () => authRequest({action: 'logout'}));
authRefresh.addEventListener('click', () => authRequest());
browserForm.addEventListener('submit', event => {
  event.preventDefault();
  authRequest({action: 'browser_login', username: browserUsername.value});
});
authRequest();
