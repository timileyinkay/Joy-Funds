function showMessage(element, text, kind) {
  if (!element) return;
  element.style.display = 'block';
  element.className = kind === 'success' ? 'flash-msg flash-success' : 'flash-msg flash-error';
  element.textContent = text;
}

async function loginUser() {
  const email = document.getElementById('email')?.value.trim();
  const password = document.getElementById('password')?.value.trim();
  const message = document.getElementById('message');

  if (!email || !password) {
    showMessage(message, 'Please enter both email and password.', 'error');
    return;
  }

  try {
    const response = await fetch('/api/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
      body: new URLSearchParams({ email, password })
    });

    const result = await response.json();
    if (!response.ok) {
      showMessage(message, result.message || 'Login failed.', 'error');
      return;
    }

    showMessage(message, result.message || 'Login successful.', 'success');
    window.location.href = result.redirect || 'dashboard.html';
  } catch (error) {
    showMessage(message, 'Unable to connect to the server.', 'error');
  }
}

async function loginAdmin() {
  const email = document.getElementById('adminEmail')?.value.trim();
  const password = document.getElementById('adminPassword')?.value.trim();
  const message = document.getElementById('message');

  if (!email || !password) {
    showMessage(message, 'Please enter the admin email and password.', 'error');
    return;
  }

  try {
    const response = await fetch('/api/admin/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
      body: new URLSearchParams({ email, password })
    });

    const result = await response.json();
    if (!response.ok) {
      showMessage(message, result.message || 'Admin login failed.', 'error');
      return;
    }

    showMessage(message, result.message || 'Admin login successful.', 'success');
    window.location.href = result.redirect || 'admin.html';
  } catch (error) {
    showMessage(message, 'Unable to connect to the server.', 'error');
  }
}

async function signUpUser() {
  const fullName = document.getElementById('fullName')?.value.trim();
  const email = document.getElementById('email')?.value.trim();
  const password = document.getElementById('password')?.value.trim();
  const message = document.getElementById('message');

  if (!fullName || !email || !password) {
    showMessage(message, 'Please fill out all fields.', 'error');
    return;
  }
  if (password.length < 12) {
    showMessage(message, 'Use a password with at least 12 characters.', 'error');
    return;
  }

  try {
    const response = await fetch('/api/signup', {
      method: 'POST',
      headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
      body: new URLSearchParams({ full_name: fullName, email, password })
    });

    const result = await response.json();
    if (!response.ok) {
      showMessage(message, result.message || 'Sign up failed.', 'error');
      return;
    }

    showMessage(message, result.message || 'Account created.', 'success');
    window.location.href = result.redirect || 'dashboard.html';
  } catch (error) {
    showMessage(message, 'Unable to connect to the server.', 'error');
  }
}

async function logoutUser() {
  try {
    await fetch('/api/logout', { method: 'POST' });
  } finally {
    window.location.href = 'login.html';
  }
}

function formatNaira(amount) {
  return new Intl.NumberFormat('en-NG', {
    style: 'currency', currency: 'NGN', maximumFractionDigits: 0
  }).format(Number(amount) || 0);
}

const INVESTMENT_LEVELS = [500, 2000, 5000, 20000, 90000, 450000, 2000000, 10000000];

function getNextInvestmentLevel(lastAmount) {
  const parsed = Number(lastAmount) || 0;
  const nextLevel = INVESTMENT_LEVELS.find((value) => value > parsed);
  return nextLevel ?? INVESTMENT_LEVELS[INVESTMENT_LEVELS.length - 1];
}

function getRequiredInvestmentLevel() {
  return Number(window.lastAcceptedInvestmentLevel || 500);
}

async function hydrateInvestmentLevelHint() {
  const amountInput = document.getElementById('investmentAmount');
  const hint = document.getElementById('investmentLevelHint');
  if (!amountInput || !hint) return;
  const continueButton = document.getElementById('investmentContinue');
  if (continueButton) continueButton.disabled = true;

  try {
    const response = await fetch('/api/me');
    if (!response.ok) {
      hint.textContent = 'Sign in to check when your next deposit is available.';
      return;
    }
    const profile = await response.json();
    const latestAcceptedDeposit = (profile.transactions || [])
      .find((item) => item.type === 'Deposit accepted');
    const lastAccepted = Number(latestAcceptedDeposit?.amount_ngn || 0);
    window.lastAcceptedInvestmentLevel = lastAccepted || 500;
    const suggested = getNextInvestmentLevel(lastAccepted);
    amountInput.value = suggested;
    amountInput.min = String(window.lastAcceptedInvestmentLevel);
    hint.textContent = lastAccepted > 0
      ? `Your last accepted deposit was ₦${lastAccepted.toLocaleString()}. Enter the same amount or more; ₦${suggested.toLocaleString()} is the suggested next level.`
      : 'First deposit minimum is ₦500. After that, enter the same amount as your last accepted deposit or more.';
    window.nextDepositAllowedAt = profile.next_deposit_at || '';
    const updateCooldown = () => {
      const nextAllowed = Date.parse(window.nextDepositAllowedAt || '');
      const remaining = nextAllowed - Date.now();
      if (remaining > 0) {
        if (continueButton) continueButton.disabled = true;
        const hours = Math.ceil(remaining / (60 * 60 * 1000));
        hint.textContent = `You can make another deposit after ${new Date(nextAllowed).toLocaleString('en-NG', { dateStyle: 'medium', timeStyle: 'short' })}. Please wait about ${hours} hour${hours === 1 ? '' : 's'} (5 days after your last deposit).`;
      } else {
        if (continueButton) continueButton.disabled = false;
        window.nextDepositAllowedAt = '';
      }
    };
    updateCooldown();
    if (window.investmentCooldownTimer) clearInterval(window.investmentCooldownTimer);
    window.investmentCooldownTimer = setInterval(updateCooldown, 60 * 1000);
  } catch (error) {
    hint.textContent = 'Could not verify the deposit wait. Refresh the page to try again.';
  }
}

document.addEventListener('DOMContentLoaded', () => {
  hydrateInvestmentLevelHint();
});

async function showDepositInstructions() {
  const amount = Number(document.getElementById('investmentAmount')?.value);
  const senderName = document.getElementById('senderName')?.value.trim();
  const panel = document.getElementById('paymentInstructions');
  const message = document.getElementById('investmentMessage');
  const submitButton = panel?.querySelector('button[onclick="submitDepositNotice()"]');
  const minimumAmount = getRequiredInvestmentLevel();
  const nextAllowed = Date.parse(window.nextDepositAllowedAt || '');
  if (Number.isFinite(nextAllowed) && nextAllowed > Date.now()) {
    showMessage(message, `You can make another deposit after ${new Date(nextAllowed).toLocaleString('en-NG', { dateStyle: 'medium', timeStyle: 'short' })}. Deposits must be five days apart.`, 'error');
    return;
  }
  if (!Number.isSafeInteger(amount) || amount < 1 || amount > 100_000_000 || !senderName || senderName.length > 120) {
    showMessage(message, 'Enter a valid amount and the name on the sending account.', 'error');
    panel.hidden = false;
    return;
  }
  if (amount < minimumAmount) {
    showMessage(message, `Enter the same amount as your last accepted deposit or more (minimum ${formatNaira(minimumAmount)}).`, 'error');
    return;
  }

  panel.hidden = false;
  message.style.display = 'none';
  submitButton.disabled = true;
  try {
    const response = await fetch('/api/payment-details');
    const result = await response.json();
    if (!response.ok) throw new Error(result.message || 'Payment details are unavailable.');

    document.getElementById('depositBank').textContent = result.bank;
    document.getElementById('depositAccountName').textContent = result.account_name;
    document.getElementById('depositAccountNumber').textContent = result.account_number;
    document.getElementById('depositAmount').textContent = formatNaira(amount);
    window.pendingDeposit = { amount, senderName };
    submitButton.disabled = false;
  } catch (error) {
    showMessage(message, error.message || 'Payment details are unavailable. Do not send funds.', 'error');
  }
}

async function submitDepositNotice() {
  const message = document.getElementById('investmentMessage');
  const reference = document.getElementById('transferReference')?.value.trim();
  const deposit = window.pendingDeposit;
  if (!deposit || !reference) {
    showMessage(message, 'Enter the transfer reference before notifying the administrator.', 'error');
    return;
  }

  const button = document.querySelector('#paymentInstructions button[onclick="submitDepositNotice()"]');
  button.disabled = true;
  try {
    const response = await fetch('/api/invest', {
      method: 'POST',
      headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
      body: new URLSearchParams({
        amount: String(deposit.amount),
        sender_name: deposit.senderName,
        transfer_reference: reference
      })
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.message || 'Unable to submit your notice.');
    showMessage(message, result.message, 'success');
    document.getElementById('transferReference').disabled = true;
    window.pendingDeposit = null;
  } catch (error) {
    showMessage(message, error.message || 'Unable to connect to the server.', 'error');
    button.disabled = false;
  }
}

async function submitWithdrawalRequest() {
  const amount = document.getElementById('withdrawalAmount')?.value.trim();
  const note = document.getElementById('withdrawalNote')?.value.trim() || '';
  const bankName = document.getElementById('withdrawalBank')?.value.trim();
  const accountName = document.getElementById('withdrawalAccountName')?.value.trim();
  const accountNumber = document.getElementById('withdrawalAccountNumber')?.value.trim();
  const message = document.getElementById('withdrawalMessage');
  try {
    const response = await fetch('/api/withdraw', {
      method: 'POST',
      headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
      body: new URLSearchParams({
        amount,
        note,
        bank_name: bankName,
        account_name: accountName,
        account_number: accountNumber
      })
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.message || 'Unable to submit your request.');
    showMessage(message, result.message, 'success');
    document.getElementById('withdrawalAmount').disabled = true;
    document.getElementById('withdrawalNote').disabled = true;
    document.getElementById('withdrawalBank').disabled = true;
    document.getElementById('withdrawalAccountName').disabled = true;
    document.getElementById('withdrawalAccountNumber').disabled = true;
  } catch (error) {
    showMessage(message, error.message || 'Unable to connect to the server.', 'error');
  }
}

function renderAdminUsers(users) {
  const tableBody = document.getElementById('adminTableBody');
  if (!tableBody) return;

  if (!users.length) {
    tableBody.innerHTML = `
      <tr>
        <td colspan="5">No registered accounts yet.</td>
      </tr>
    `;
    return;
  }

  tableBody.innerHTML = users.map((user) => {
    const created = user.created_at ? new Date(user.created_at) : null;
    const createdLabel = created && !Number.isNaN(created.valueOf())
      ? new Intl.DateTimeFormat('en', { dateStyle: 'medium', timeZone: 'UTC' }).format(created)
      : '—';
    const balance = formatNaira(user.balance_ngn);
    const activity = Array.isArray(user.activity) && user.activity.length
      ? user.activity.map(item => `
          <div class="admin-activity-item">
            <strong>${escapeHtml(item.type)}</strong> · ${formatNaira(item.amount_ngn)}
            <span class="admin-activity-status">${escapeHtml(item.status)} · ${escapeHtml(item.date || 'Date unavailable')}</span>
            ${item.sender_name ? `<span class="admin-activity-status">Sender: ${escapeHtml(item.sender_name)}</span>` : ''}
            ${item.transfer_reference ? `<span class="admin-activity-status">Reference: ${escapeHtml(item.transfer_reference)}</span>` : ''}
            ${item.note ? `<span class="admin-activity-status">Note: ${escapeHtml(item.note)}</span>` : ''}
            ${item.audit_reference ? `<span class="admin-activity-status">Admin audit: ${escapeHtml(item.audit_reference)}</span>` : ''}
            ${item.id && item.status === 'Pending payment verification' ? `
              <div class="admin-activity-actions">
                <button class="btn btn-primary" type="button" data-request-id="${escapeHtml(item.id)}" data-action="credit_verified_deposit">Verify &amp; credit</button>
                <button class="btn btn-danger" type="button" data-request-id="${escapeHtml(item.id)}" data-action="reject">Reject</button>
              </div>
            ` : ''}
            ${item.id && item.status === 'Pending admin review — balance and eligibility not verified' ? `
              <div class="admin-activity-actions">
                <button class="btn btn-primary" type="button" data-request-id="${escapeHtml(item.id)}" data-action="mark_withdrawal_paid">Record payout &amp; debit</button>
                <button class="btn btn-danger" type="button" data-request-id="${escapeHtml(item.id)}" data-action="reject">Reject</button>
              </div>
            ` : ''}
          </div>
        `).join('')
      : 'No recorded activity';
    return `
      <tr>
        <td>${escapeHtml(user.full_name)}</td>
        <td>${escapeHtml(user.email)}</td>
        <td>${escapeHtml(createdLabel)}</td>
        <td>${balance}</td>
        <td>${activity}</td>
      </tr>
    `;
  }).join('');
}

async function reviewAdminRequest(requestId, action) {
  let auditReference;
  if (action === 'credit_verified_deposit') {
    const confirmed = window.confirm('Verify that this exact transfer has arrived in your bank account before crediting the user?');
    if (!confirmed) return;
    auditReference = window.prompt('Enter your independent bank statement or reconciliation reference:');
  } else if (action === 'mark_withdrawal_paid') {
    const confirmed = window.confirm('Confirm that you have already sent this payout. The request amount will be debited from the user balance.');
    if (!confirmed) return;
    auditReference = window.prompt('Enter the completed payout transaction reference:');
  } else {
    auditReference = window.prompt('Enter the reason for rejecting this request:');
  }
  if (!auditReference?.trim()) return;

  try {
    const response = await fetch('/api/admin/requests/action', {
      method: 'POST',
      headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
      body: new URLSearchParams({ request_id: requestId, action, audit_reference: auditReference.trim() })
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.message || 'Unable to review request.');
    await loadAdminPortal();
  } catch (error) {
    window.alert(error.message || 'Unable to connect to the server.');
  }
}

function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>"']/g, character => ({
    '&': '&amp;',
    '<': '&lt;',
    '>': '&gt;',
    '"': '&quot;',
    "'": '&#39;'
  })[character]);
}

async function loadAdminPortal() {
  const tableBody = document.getElementById('adminTableBody');
  if (!tableBody) return;

  try {
    const response = await fetch('/api/admin/users');
    if (!response.ok) {
      window.location.href = 'admin-login.html';
      return;
    }

    const result = await response.json();
    renderAdminUsers(result.users || []);
  } catch (error) {
    tableBody.innerHTML = '<tr><td colspan="5">Unable to load account records.</td></tr>';
  }
}

window.addEventListener('DOMContentLoaded', () => {
  loadAdminPortal();
  document.getElementById('adminTableBody')?.addEventListener('click', event => {
    const button = event.target.closest('button[data-request-id][data-action]');
    if (button) reviewAdminRequest(button.dataset.requestId, button.dataset.action);
  });
});
