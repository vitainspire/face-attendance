const TOKEN = new URLSearchParams(window.location.search).get('token');

// See app.js for why this exists — empty when served from the same origin as the
// backend (the default), set only when this file is deployed somewhere else entirely.
const API_BASE = 'https://16-192-137-111.sslip.io';

const VIEWS = ['ob-loading', 'ob-form-view', 'ob-submitted-view', 'ob-provisioning-view',
               'ob-rejected-view', 'ob-failed-view', 'ob-active-view'];
function showView(id) {
    VIEWS.forEach(v => document.getElementById(v).classList.add('hidden'));
    document.getElementById(id).classList.remove('hidden');
}

let pollTimer = null;

async function fetchStatus() {
    if (!TOKEN) {
        showView('ob-failed-view');
        document.getElementById('ob-failed-message').innerText = 'This link is missing its token — please use the exact link you were sent.';
        return;
    }
    try {
        const res = await fetch(`${API_BASE}/public/onboarding/${TOKEN}`);
        const data = await res.json();
        if (!res.ok) {
            showView('ob-failed-view');
            document.getElementById('ob-failed-message').innerText = data.detail || 'Invalid onboarding link.';
            return;
        }
        render(data);
    } catch (err) {
        showView('ob-failed-view');
        document.getElementById('ob-failed-message').innerText = 'Could not reach the server. Please try again shortly.';
    }
}

// Shared by the initial 'invited' view and the resubmit-after-rejected/failed flow.
function populateKeyUI(data) {
    document.getElementById('ob-school-name').innerText = `Setting up: ${data.school_name}`;
    document.getElementById('btn-download-pubkey').href = `${API_BASE}/public/onboarding/${TOKEN}/public_key.pub`;
    document.getElementById('ob-pubkey-command').value =
        `echo "${data.public_key}" >> ~/.ssh/authorized_keys`;
}

function render(data) {
    if (pollTimer) { clearTimeout(pollTimer); pollTimer = null; }

    if (data.status === 'invited') {
        populateKeyUI(data);
        showView('ob-form-view');
    } else if (data.status === 'submitted') {
        showView('ob-submitted-view');
        pollTimer = setTimeout(fetchStatus, 5000);
    } else if (data.status === 'provisioning') {
        showView('ob-provisioning-view');
        pollTimer = setTimeout(fetchStatus, 5000);
    } else if (data.status === 'rejected') {
        document.getElementById('ob-reject-reason').innerText = data.reason || 'No reason given.';
        showView('ob-rejected-view');
    } else if (data.status === 'failed') {
        document.getElementById('ob-failed-message').innerText = data.error || 'Setup failed — please contact support.';
        showView('ob-failed-view');
    } else if (data.status === 'active') {
        const link = document.getElementById('ob-login-link');
        link.href = data.login_url;
        link.innerText = data.login_url;
        document.getElementById('ob-admin-username').innerText = data.admin_username;
        if (data.admin_password) {
            document.getElementById('ob-admin-password').innerText = data.admin_password;
        } else {
            document.getElementById('ob-credentials-block').innerHTML =
                '<span class="hint">Credentials were already shown once. Contact support if you lost them.</span>';
        }
        showView('ob-active-view');
    }
}

document.getElementById('btn-copy-pubkey').addEventListener('click', () => {
    const ta = document.getElementById('ob-pubkey-command');
    ta.select();
    navigator.clipboard.writeText(ta.value).catch(() => document.execCommand('copy'));
});

document.getElementById('ob-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    hide('ob-form-error');
    const errBox = document.getElementById('ob-form-error');
    try {
        const res = await fetch(`${API_BASE}/public/onboarding/${TOKEN}/submit`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                supabase_db_url: document.getElementById('ob_supabase_url').value.trim(),
                elastic_ip: document.getElementById('ob_elastic_ip').value.trim(),
                pubkey_confirmed: document.getElementById('ob_pubkey_confirmed').checked,
            }),
        });
        const data = await res.json();
        if (!res.ok) throw data;
        fetchStatus();
    } catch (err) {
        errBox.innerText = (err && err.detail) ? err.detail : 'Submission failed.';
        show('ob-form-error');
    }
});

// render() only shows ob-form-view for status 'invited' — a rejected or failed request
// needs the form re-opened explicitly, since its status stays 'rejected'/'failed' until
// a fresh submission changes it.
function reopenFormForResubmit() {
    fetch(`${API_BASE}/public/onboarding/${TOKEN}`).then(r => r.json()).then(data => {
        populateKeyUI(data);
        showView('ob-form-view');
    });
}
document.getElementById('btn-fix-resubmit').addEventListener('click', reopenFormForResubmit);
document.getElementById('btn-failed-resubmit').addEventListener('click', reopenFormForResubmit);

function hide(id) { document.getElementById(id).classList.add('hidden'); }
function show(id) { document.getElementById(id).classList.remove('hidden'); }

fetchStatus();
