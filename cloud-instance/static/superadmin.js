// Deliberately NOT persisted in localStorage — this panel can create schools and see
// every school's onboarding data, so it always requires a fresh login on page load
// rather than staying signed in indefinitely like the regular teacher/parent/admin app.
let saToken = null;

// See app.js for why this exists — empty when served from the same origin as the
// backend (the default), set only when this file is deployed somewhere else entirely.
const API_BASE = 'https://16-192-137-111.sslip.io';

function hide(id) { document.getElementById(id).classList.add('hidden'); }
function show(id) { document.getElementById(id).classList.remove('hidden'); }

// Escapes free-text before it goes into innerHTML. Critical here specifically: elastic_ip
// is submitted by the SCHOOL during onboarding (an untrusted party at that point — that's
// the whole reason there's a review/approve step) and rendered straight into this table.
// Without escaping, a malicious submission would run as script in the superadmin's own
// session the moment this page loads, before any review happens.
// Also escapes quote characters so this is safe inside a quoted HTML attribute too,
// not just between tags — a bare textContent/innerHTML round trip only escapes
// & < > and leaves ' and " untouched, which is enough to break out of an attribute.
function escapeHtml(value) {
    const div = document.createElement('div');
    div.textContent = value == null ? '' : String(value);
    return div.innerHTML.replaceAll('"', '&quot;').replaceAll("'", '&#39;');
}

async function saApi(path, { method = 'GET', json } = {}) {
    const opts = { method, headers: { 'Authorization': `Bearer ${saToken}` } };
    if (json) {
        opts.headers['Content-Type'] = 'application/json';
        opts.body = JSON.stringify(json);
    }
    const res = await fetch(API_BASE + path, opts);
    let data = null;
    try { data = await res.json(); } catch (_) {}
    if (!res.ok) {
        throw { status: res.status, detail: data && data.detail ? data.detail : `Request failed (${res.status})` };
    }
    return data;
}

function routeIn() {
    if (saToken) {
        hide('sa-login-view');
        show('sa-dashboard-view');
        loadRequests();
    } else {
        show('sa-login-view');
        hide('sa-dashboard-view');
    }
}

document.getElementById('btn-sa-logout').addEventListener('click', () => {
    saToken = null;
    document.getElementById('sa_username').value = '';
    document.getElementById('sa_password').value = '';
    routeIn();
});

// A 503 here means the server's own internal login wait (see main.py) gave up, or the
// connection-level limit was hit — a few quick automatic retries smooth over that rare
// case instead of showing an error right away.
async function saLoginWithRetry(url, form, maxAttempts = 4) {
    for (let attempt = 1; attempt <= maxAttempts; attempt++) {
        const res = await fetch(API_BASE + url, {
            method: 'POST',
            headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
            body: form,
        });
        const data = await res.json().catch(() => null);
        if (res.ok) return data;
        if (res.status !== 503 || attempt === maxAttempts) throw data || { detail: 'Login failed' };
        await new Promise(resolve => setTimeout(resolve, 1000 * attempt));
    }
}

document.getElementById('sa-login-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    hide('sa-login-error');
    const form = new URLSearchParams();
    form.append('username', document.getElementById('sa_username').value);
    form.append('password', document.getElementById('sa_password').value);
    try {
        const data = await saLoginWithRetry('/superadmin/token', form);
        saToken = data.access_token;
        routeIn();
    } catch (err) {
        document.getElementById('sa-login-error').innerText = (err && err.detail) ? err.detail : 'Login failed';
        show('sa-login-error');
    }
});

document.getElementById('sa-create-link-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    hide('sa-create-error'); hide('sa-created-link-box');
    try {
        const data = await saApi('/superadmin/onboarding_links', {
            method: 'POST',
            json: {
                school_name: document.getElementById('sa_school_name').value.trim(),
                contact_email: document.getElementById('sa_contact_email').value.trim() || null,
            },
        });
        const fullUrl = new URL(data.onboarding_url, window.location.origin).href;
        document.getElementById('sa-created-link').value = fullUrl;
        show('sa-created-link-box');
        document.getElementById('sa-create-link-form').reset();
        loadRequests();
    } catch (err) {
        document.getElementById('sa-create-error').innerText = err.detail || 'Failed to create link';
        show('sa-create-error');
    }
});

async function loadRequests() {
    const tbody = document.getElementById('sa-requests-tbody');
    tbody.innerHTML = '<tr><td colspan="6">Loading...</td></tr>';
    try {
        const data = await saApi('/superadmin/onboarding_requests');
        tbody.innerHTML = '';
        if (!data.schools.length) {
            tbody.innerHTML = '<tr><td colspan="6">No onboarding requests yet.</td></tr>';
            return;
        }
        data.schools.forEach(s => {
            const tr = document.createElement('tr');
            let detail = '';
            if (s.status === 'rejected') detail = s.rejection_reason || '';
            if (s.status === 'failed') detail = (s.provisioning_error || '').slice(0, 200);
            let statusLabel = s.status;
            let actions = '';
            if (s.status === 'submitted') {
                actions = `<button class="mini-btn" onclick="acceptRequest(${s.id})">Accept</button>
                           <button class="mini-btn danger" onclick="rejectRequest(${s.id})">Reject</button>`;
            } else if (s.status === 'active') {
                if (s.service_stopped) {
                    statusLabel = 'active (service stopped)';
                    actions = `<button class="mini-btn" onclick="startService(${s.id})">Start Service</button>`;
                } else {
                    actions = `<button class="mini-btn warn" onclick="stopService(${s.id})">Stop Service</button>`;
                }
                actions += ` <button class="mini-btn" onclick="resetAdminPassword(${s.id})">Reset Password</button>`;
            } else {
                actions = `<span class="hint">${s.status}</span>`;
            }
            actions += ` <button class="mini-btn danger" onclick="deleteRequest(${s.id}, '${s.status}')">Delete</button>`;
            tr.innerHTML = `
                <td>${escapeHtml(s.name)}</td>
                <td>${escapeHtml(statusLabel)}</td>
                <td>${escapeHtml(s.elastic_ip) || '—'}</td>
                <td>${escapeHtml(s.admin_username) || '—'}</td>
                <td style="max-width:260px; white-space:normal;">${escapeHtml(detail)}</td>
                <td class="row-actions">${actions}</td>`;
            tbody.appendChild(tr);
        });
    } catch (err) {
        tbody.innerHTML = `<tr><td colspan="6">${escapeHtml(err.detail || 'Failed to load')}</td></tr>`;
    }
}
document.getElementById('btn-refresh-requests').addEventListener('click', loadRequests);

window.acceptRequest = async function (id) {
    if (!confirm('Start provisioning this school? This will SSH into their instance and deploy the model service.')) return;
    try {
        await saApi(`/superadmin/onboarding_requests/${id}/accept`, { method: 'POST' });
        loadRequests();
    } catch (err) {
        alert(err.detail || 'Failed to accept');
    }
};

window.rejectRequest = async function (id) {
    const reason = prompt('Reason for rejecting this request (shown to the school):');
    if (reason === null) return;
    try {
        await saApi(`/superadmin/onboarding_requests/${id}/reject`, { method: 'POST', json: { reason } });
        loadRequests();
    } catch (err) {
        alert(err.detail || 'Failed to reject');
    }
};

window.deleteRequest = async function (id, status) {
    const msg = status === 'active'
        ? 'This school is ACTIVE — deleting it will lock out everyone who logs into it (their data and server are untouched, but they will no longer be able to sign in). Are you sure?'
        : 'Delete this onboarding request permanently?';
    if (!confirm(msg)) return;
    try {
        await saApi(`/superadmin/onboarding_requests/${id}`, { method: 'DELETE' });
        loadRequests();
    } catch (err) {
        alert(err.detail || 'Failed to delete');
    }
};

window.stopService = async function (id) {
    if (!confirm('Stop this school\'s model service? Their teachers won\'t be able to take attendance until it\'s started again.')) return;
    try {
        await saApi(`/superadmin/onboarding_requests/${id}/stop_service`, { method: 'POST' });
        loadRequests();
    } catch (err) {
        alert(err.detail || 'Failed to stop service');
    }
};

window.startService = async function (id) {
    try {
        await saApi(`/superadmin/onboarding_requests/${id}/start_service`, { method: 'POST' });
        loadRequests();
    } catch (err) {
        alert(err.detail || 'Failed to start service');
    }
};

window.resetAdminPassword = async function (id) {
    if (!confirm("Generate a NEW password for this school's admin login? Their old password will stop working immediately.")) return;
    const box = document.getElementById('sa-reset-password-box');
    try {
        const data = await saApi(`/superadmin/onboarding_requests/${id}/reset_admin_password`, { method: 'POST' });
        box.innerHTML = `<b>New login generated:</b><br>
            Username: <b>${escapeHtml(data.admin_username)}</b><br>
            Password: <b>${escapeHtml(data.new_password)}</b><br>
            <span class="hint">Shown once — save this now.</span>`;
        box.className = 'result-box success-box';
        show('sa-reset-password-box');
    } catch (err) {
        box.innerText = err.detail || 'Failed to reset password';
        box.className = 'result-box error-text';
        show('sa-reset-password-box');
    }
};

routeIn();
