'use strict';
const emailForm = document.getElementById('emailForm');
const codeForm = document.getElementById('codeForm');
const statusEl = document.getElementById('status');
let loginEmail = '';
let resendAt = 0;
let sending = false;
function statusMessage(text, error = false) { statusEl.textContent = text; statusEl.className = error ? 'error' : ''; }
async function request(path, data) {
  const response = await fetch(path, {method: 'POST', credentials: 'same-origin', headers: {'Content-Type': 'application/json', 'X-SGO-Request': '1'}, body: JSON.stringify(data)});
  const body = await response.json();
  if (!response.ok) {
    if (response.status === 429) {
      const seconds = Math.max(1, Number(response.headers.get('Retry-After')) || 60);
      const error = new Error(`请求次数已达上限，请等待 ${Math.ceil(seconds / 60)} 分钟后重试。`);
      error.retryAfter = seconds;
      throw error;
    }
    if (response.status === 503) throw new Error('邮件或登录服务尚不可用，请联系管理员。');
    throw new Error(path.endsWith('verify') ? '验证码不正确或已过期，请检查后重试。' : '暂时无法发送，请稍后重试。');
  }
  return body;
}
emailForm.addEventListener('submit', async event => {
  event.preventDefault();
  const button = document.getElementById('sendCode');
  if (sending || Date.now() < resendAt) return;
  sending = true;
  button.disabled = true;
  const requestedEmail = document.getElementById('email').value.trim();
  document.getElementById('email').readOnly = true;
  document.getElementById('changeEmail').disabled = true;
  try {
    const body = await request('/auth/request-code', {email: requestedEmail});
    loginEmail = requestedEmail;
    document.getElementById('code').value = '';
    statusMessage(body.message + ' 请使用最新一封邮件的 8 位验证码；旧验证码已失效，10 分钟内有效。');
    codeForm.hidden = false;
    document.getElementById('email').readOnly = true;
    document.getElementById('code').focus();
    resendAt = Date.now() + 60000;
  } catch (error) {
    if (error.retryAfter) resendAt = Date.now() + error.retryAfter * 1000;
    statusMessage(error.message, true);
    if (codeForm.hidden) document.getElementById('email').readOnly = false;
  } finally {
    sending = false;
    button.disabled = Date.now() < resendAt;
    document.getElementById('changeEmail').disabled = false;
  }
});
setInterval(() => {
  const seconds = Math.max(0, Math.ceil((resendAt - Date.now()) / 1000));
  const button = document.getElementById('sendCode');
  if (resendAt) { button.disabled = sending || seconds > 0; button.textContent = sending ? '正在发送…' : seconds > 60 ? `${Math.ceil(seconds / 60)} 分钟后可重新发送` : seconds ? `${seconds} 秒后可重新发送` : '重新发送验证码'; }
}, 1000);
codeForm.addEventListener('submit', async event => {
  event.preventDefault();
  const button = document.getElementById('verify');
  button.disabled = true;
  try {
    await request('/auth/verify', {email: loginEmail, code: document.getElementById('code').value.trim()});
    window.location.replace('/');
  } catch (error) { statusMessage(error.message, true); button.disabled = false; }
});
document.getElementById('changeEmail').addEventListener('click', () => {
  if (sending) return;
  codeForm.hidden = true; document.getElementById('email').readOnly = false;
  document.getElementById('email').focus(); document.getElementById('code').value = '';
  statusMessage('');
});
