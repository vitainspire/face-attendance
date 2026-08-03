// ===================== State & helpers =====================
// Empty string when this frontend is served from the same origin as the backend (the
// default — main.py's own /static mount). Set to the backend's own URL only when this
// file is deployed somewhere else entirely (e.g. a Vercel-hosted static frontend).
const API_BASE = 'https://16-192-137-111.sslip.io';

const state = {
    token: localStorage.getItem('token') || null,
    role: localStorage.getItem('role') || null,
};

// A backend error's `detail` is USUALLY a plain string, but FastAPI's own request
// validation errors (missing/malformed query param, bad JSON body, etc.) return it as
// an ARRAY of {loc, msg, type} objects instead — passing that straight into an alert()
// or innerText renders as an unhelpful "[object Object]". This normalizes either shape
// into one readable line.
function errorMessage(err, fallback) {
    const detail = err && err.detail;
    if (typeof detail === 'string' && detail) return detail;
    if (Array.isArray(detail) && detail.length) {
        return detail.map(d => (d && d.msg) ? d.msg : JSON.stringify(d)).join('; ');
    }
    return fallback;
}

function authHeaders(extra = {}) {
    return { 'Authorization': `Bearer ${state.token}`, ...extra };
}

// Escapes free-text values before they go into innerHTML — anything a user typed (a
// leave request reason, a name, etc.) must go through this before interpolation, since
// it can render in ANOTHER user's session (e.g. a parent's leave reason is shown to
// their child's teacher) and raw innerHTML would let injected HTML/script execute there.
// Also escapes quote characters so the same helper is safe inside a quoted HTML
// attribute (e.g. alt="${escapeHtml(name)}"), not just between tags — a bare
// textContent/innerHTML round trip only escapes & < > and leaves ' and " untouched,
// which is enough to break out of an attribute value.
function escapeHtml(value) {
    const div = document.createElement('div');
    div.textContent = value == null ? '' : String(value);
    return div.innerHTML.replaceAll('"', '&quot;').replaceAll("'", '&#39;');
}

async function api(path, { method = 'GET', json, form } = {}) {
    const opts = { method, headers: authHeaders() };
    if (json) {
        opts.headers['Content-Type'] = 'application/json';
        opts.body = JSON.stringify(json);
    } else if (form) {
        opts.body = form; // browser sets multipart boundary
    }
    const res = await fetch(API_BASE + path, opts);
    let data = null;
    try { data = await res.json(); } catch (_) {}
    if (!res.ok) {
        const detail = data && data.detail ? data.detail : `Request failed (${res.status})`;
        throw { status: res.status, detail };
    }
    return data;
}

function show(id) { document.getElementById(id).classList.remove('hidden'); }
function hide(id) { document.getElementById(id).classList.add('hidden'); }
function setText(id, txt) { document.getElementById(id).innerText = txt; }

const ALL_VIEWS = ['login-view', 'admin-view', 'parent-capture-view', 'parent-dashboard-view', 'teacher-view'];
function showView(id) {
    ALL_VIEWS.forEach(v => hide(v));
    show(id);
}

// Fills a flat <select> with every class/section as one option each ("Class 3 - Section A"),
// value=section_id — used by every dropdown that just needs "pick one section" (not the
// cascading class-then-section pattern used in Manage Students/Embeddings/Reports/Classes).
// Always fetched fresh from /admin/classes so newly-added classes/sections show up
// immediately, instead of the old hardcoded <option> lists that never updated.
async function populateFlatSectionDropdown(selectId, endpoint = '/admin/classes') {
    const sel = document.getElementById(selectId);
    if (!sel) return;
    const prev = sel.value;
    try {
        const data = await api(endpoint);
        sel.innerHTML = '';
        data.classes.forEach(c => {
            c.sections.forEach(s => {
                const opt = document.createElement('option');
                opt.value = s.id;
                opt.dataset.classId = c.id;
                opt.textContent = `${c.name} - ${s.name}`;
                sel.appendChild(opt);
            });
        });
        if (prev && sel.querySelector(`option[value="${prev}"]`)) {
            sel.value = prev;
        }
    } catch (err) {
        sel.innerHTML = `<option value="">${escapeHtml(errorMessage(err, 'Failed to load sections'))}</option>`;
    }
}

async function populateAdminFlatDropdowns() {
    // NOT .map(populateFlatSectionDropdown) directly — Array.map calls its callback
    // with (element, index, array), and since this function now takes an optional
    // second `endpoint` argument, that would pass the array INDEX (0, 1) as endpoint
    // instead of letting it default, producing a malformed request URL.
    await Promise.all(['reg_section', 'edit_section'].map(id => populateFlatSectionDropdown(id)));
}

async function populateTeacherFlatDropdowns() {
    // Scoped to this teacher's own assigned sections — /teacher/my_classes, not
    // /admin/classes, so a teacher never sees a section they aren't assigned to.
    await Promise.all(['att_section_id', 'analytics_section', 'leave_section'].map(
        id => populateFlatSectionDropdown(id, '/teacher/my_classes')
    ));
    loadSubjectsForSection('att_section_id', 'attendance_subject', false);
    loadSubjectsForSection('analytics_section', 'analytics_subject', true);
}

// Populates a subject <select> with only the subjects THIS TEACHER may act on for the
// currently-selected section (via /teacher/my_subjects — the exact same check the
// backend uses to accept/reject recognize, submit_attendance, and leave approval, so
// this dropdown can never offer a subject the server would then reject). A teacher
// who's never been assigned to anything still sees every subject in the section
// (unchanged legacy behavior). keepAllOption preserves a leading "All Subjects" entry
// (used by the analytics filter) instead of requiring one to be picked.
async function loadSubjectsForSection(sectionSelectId, subjectSelectId, keepAllOption) {
    const sectionSel = document.getElementById(sectionSelectId);
    const subjectSel = document.getElementById(subjectSelectId);
    if (!sectionSel || !subjectSel) return;
    const sectionId = sectionSel.value;
    const allOptionHtml = keepAllOption ? '<option value="All">All Subjects</option>' : '';
    if (!sectionId) {
        subjectSel.innerHTML = allOptionHtml || '<option value="" disabled selected>Select a section first...</option>';
        return;
    }
    try {
        const data = await api(`/teacher/my_subjects?section_id=${sectionId}`);
        if (!data.subjects.length) {
            subjectSel.innerHTML = allOptionHtml ||
                '<option value="" disabled selected>No subjects assigned to you for this section — ask your admin</option>';
            return;
        }
        subjectSel.innerHTML = allOptionHtml;
        data.subjects.forEach(name => {
            const opt = document.createElement('option');
            opt.value = name;
            opt.textContent = name;
            subjectSel.appendChild(opt);
        });
        if (!keepAllOption) subjectSel.selectedIndex = 0;
    } catch (err) {
        subjectSel.innerHTML = `<option value="" disabled selected>${escapeHtml(errorMessage(err, 'Failed to load subjects'))}</option>`;
    }
}

document.getElementById('att_section_id').addEventListener('change', () => {
    loadSubjectsForSection('att_section_id', 'attendance_subject', false);
});
document.getElementById('analytics_section').addEventListener('change', () => {
    loadSubjectsForSection('analytics_section', 'analytics_subject', true);
});

// ===================== Routing =====================
// JWTs aren't encrypted, just signed — the payload (including the username in "sub")
// can be read client-side without any new backend endpoint.
function parseJwt(token) {
    try {
        const payload = token.split('.')[1];
        return JSON.parse(atob(payload.replace(/-/g, '+').replace(/_/g, '/')));
    } catch (e) {
        return {};
    }
}

function updateProfileNames() {
    const username = parseJwt(state.token).sub;
    if (!username) return;
    const adminEl = document.getElementById('admin-profile-name');
    const teacherEl = document.getElementById('teacher-profile-name');
    if (adminEl) adminEl.textContent = username;
    if (teacherEl) teacherEl.textContent = username;
}

function routeAfterLogin(loginData) {
    state.token = loginData.access_token;
    state.role = loginData.role;
    localStorage.setItem('token', state.token);
    localStorage.setItem('role', state.role);

    show('user-bar');
    setText('user-label', `Signed in as ${state.role}`);
    updateProfileNames();
    if (state.role === 'teacher') resetIdleTimer();
    document.getElementById('btn-notifications').classList.toggle('hidden', state.role !== 'parent');
    document.getElementById('btn-teacher-notifications').classList.toggle('hidden', state.role !== 'teacher');

    if (state.role === 'admin') {
        showView('admin-view');
        populateAdminFlatDropdowns();
    } else if (state.role === 'teacher') {
        showView('teacher-view');
        populateTeacherFlatDropdowns();
        refreshPendingLeaveBadge();
    } else if (state.role === 'parent') {
        if (loginData.needs_capture) {
            showView('parent-capture-view');
        } else {
            showView('parent-dashboard-view');
            loadParentChildren().then(loadParentDashboard);
            refreshNotificationBadge();
        }
    }
}

function logout() {
    state.token = null; state.role = null;
    localStorage.removeItem('token'); localStorage.removeItem('role');
    hide('user-bar');
    closeDrawer('notif-overlay', 'notifications-panel');
    closeDrawer('teacher-notif-overlay', 'teacher-notifications-panel');
    showView('login-view');
    stopCamera();
    stopGpCamera();
    stopIdleTimer();
}
document.getElementById('btn-logout').addEventListener('click', logout);

// ===================== Idle auto-logout (teacher accounts only) =====================
const IDLE_LOGOUT_MS = 5 * 60 * 1000; // 5 minutes
let idleTimer = null;

function resetIdleTimer() {
    if (state.role !== 'teacher') return;
    if (idleTimer) clearTimeout(idleTimer);
    idleTimer = setTimeout(() => {
        logout();
        const box = document.getElementById('login-error');
        box.innerText = 'You were logged out after 5 minutes of inactivity.';
        show('login-error');
    }, IDLE_LOGOUT_MS);
}

function stopIdleTimer() {
    if (idleTimer) clearTimeout(idleTimer);
    idleTimer = null;
}

['click', 'keydown', 'mousemove', 'touchstart'].forEach(evt =>
    document.addEventListener(evt, () => { if (state.role === 'teacher') resetIdleTimer(); }, { passive: true })
);

// ===================== Change Password (admin, teacher, parent) =====================
document.getElementById('btn-change-password').addEventListener('click', () => {
    document.getElementById('cp_current').value = '';
    document.getElementById('cp_new').value = '';
    document.getElementById('cp_confirm').value = '';
    hide('change-password-error');
    hide('change-password-success');
    show('change-password-modal');
});
document.getElementById('btn-cancel-change-password').addEventListener('click', () => {
    hide('change-password-modal');
});
document.getElementById('btn-submit-change-password').addEventListener('click', async () => {
    hide('change-password-error');
    hide('change-password-success');
    const current_password = document.getElementById('cp_current').value;
    const new_password = document.getElementById('cp_new').value;
    const confirm_password = document.getElementById('cp_confirm').value;

    const errBox = document.getElementById('change-password-error');
    if (new_password.length < 6) {
        errBox.innerText = 'New password must be at least 6 characters';
        show(errBox.id);
        return;
    }
    if (new_password !== confirm_password) {
        errBox.innerText = 'New password and confirmation do not match';
        show(errBox.id);
        return;
    }
    try {
        await api('/me/change_password', { method: 'POST', json: { current_password, new_password } });
        const okBox = document.getElementById('change-password-success');
        okBox.innerText = 'Password updated successfully.';
        show(okBox.id);
        setTimeout(() => hide('change-password-modal'), 1200);
    } catch (err) {
        errBox.innerText = errorMessage(err, 'Could not update password');
        show(errBox.id);
    }
});

// ===================== Parent: sidebar tabs =====================
function parentTab(which) {
    const tabs = { dashboard: 'parent-p-dashboard', attendance: 'parent-p-attendance',
                   leaves: 'parent-p-leaves', settings: 'parent-p-settings', support: 'parent-p-support' };
    Object.keys(tabs).forEach(k => {
        document.getElementById('tab-p-' + k).classList.toggle('active', which === k);
        document.getElementById(tabs[k]).classList.toggle('hidden', which !== k);
    });
}
document.getElementById('tab-p-dashboard').addEventListener('click', () => parentTab('dashboard'));
document.getElementById('tab-p-attendance').addEventListener('click', () => parentTab('attendance'));
document.getElementById('tab-p-leaves').addEventListener('click', () => parentTab('leaves'));
document.getElementById('tab-p-settings').addEventListener('click', () => parentTab('settings'));
document.getElementById('tab-p-support').addEventListener('click', () => parentTab('support'));
document.getElementById('tab-p-logout').addEventListener('click', logout);
document.getElementById('btn-p-goto-leaves').addEventListener('click', () => parentTab('leaves'));
document.getElementById('p-leave-more-notice').addEventListener('click', (e) => { e.preventDefault(); parentTab('leaves'); });
document.getElementById('btn-p-settings-change-password').addEventListener('click', () => {
    document.getElementById('btn-change-password').click();
});

// ===================== Admin / Teacher: sidebar Settings + Logout =====================
// These reuse the exact same (now visually hidden) header buttons and their existing
// click handlers, instead of duplicating the change-password/logout logic.
document.getElementById('tab-admin-settings').addEventListener('click', () => {
    document.getElementById('btn-change-password').click();
});
document.getElementById('tab-admin-logout').addEventListener('click', logout);
document.getElementById('tab-teacher-settings').addEventListener('click', () => {
    document.getElementById('btn-change-password').click();
});
document.getElementById('tab-teacher-logout').addEventListener('click', logout);

// ===================== Login =====================
// The server already waits and retries internally when the CPU is busy hashing other
// logins (see main.py's login concurrency throttle) — a 503 here means it gave up
// after its own wait, or the server is swamped at the connection level. A few quick
// automatic retries smooth over that rare case instead of showing an error right away.
async function loginWithRetry(url, form, maxAttempts = 4) {
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

document.getElementById('login-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    hide('login-error');
    const form = new URLSearchParams();
    form.append('username', document.getElementById('login_username').value);
    form.append('password', document.getElementById('login_password').value);
    try {
        const data = await loginWithRetry('/token', form);
        routeAfterLogin(data);
    } catch (err) {
        const box = document.getElementById('login-error');
        box.innerText = errorMessage(err, 'Login failed');
        show('login-error');
    }
});

// ===================== Admin: tabs =====================
function adminTab(which) {
    const tabs = { register: 'admin-register', students: 'admin-students', embeddings: 'admin-embeddings',
                   teachers: 'admin-teachers', reports: 'admin-reports', classes: 'admin-classes',
                   storage: 'admin-storage', recognition: 'admin-recognition',
                   'school-profile': 'admin-school-profile', events: 'admin-events' };
    Object.keys(tabs).forEach(k => {
        document.getElementById('tab-' + k).classList.toggle('active', which === k);
        document.getElementById(tabs[k]).classList.toggle('hidden', which !== k);
    });
    if (which === 'embeddings') initEmbedTab();
    if (which === 'students') { loadClasses(); loadLinkChildOptions(); }
    if (which === 'teachers') loadTeachers();
    if (which === 'reports') loadReportClasses();
    if (which === 'classes') loadClassesTab();
    if (which === 'storage') loadS3Settings();
    if (which === 'recognition') loadRecognitionSettings();
    if (which === 'school-profile') loadSchoolProfile();
    if (which === 'events') loadEvents();
}
document.getElementById('tab-register').addEventListener('click', () => adminTab('register'));
document.getElementById('tab-students').addEventListener('click', () => adminTab('students'));
document.getElementById('tab-embeddings').addEventListener('click', () => adminTab('embeddings'));
document.getElementById('tab-teachers').addEventListener('click', () => adminTab('teachers'));
document.getElementById('tab-reports').addEventListener('click', () => adminTab('reports'));
document.getElementById('tab-classes').addEventListener('click', () => adminTab('classes'));
document.getElementById('tab-storage').addEventListener('click', () => adminTab('storage'));
document.getElementById('tab-recognition').addEventListener('click', () => adminTab('recognition'));
document.getElementById('tab-school-profile').addEventListener('click', () => adminTab('school-profile'));
document.getElementById('tab-events').addEventListener('click', () => adminTab('events'));

// ===================== Admin: events / announcements =====================
async function loadEvents() {
    const list = document.getElementById('events-list');
    list.innerHTML = '<li><span class="hint">Loading...</span></li>';
    try {
        const data = await api('/admin/events');
        if (!data.events.length) {
            list.innerHTML = '<li><span class="hint">No events added yet.</span></li>';
            return;
        }
        list.innerHTML = '';
        data.events.forEach(e => {
            const li = document.createElement('li');
            li.innerHTML = `
                <div style="flex:1">
                    <strong>${escapeHtml(e.title)}</strong> — ${e.event_date}<br>
                    ${e.description ? `<span style="font-size:0.9em; color:#666">${escapeHtml(e.description)}</span>` : ''}
                </div>
                <button class="mini-btn danger" data-del-event="${e.id}" data-title="${escapeHtml(e.title)}">Remove</button>`;
            list.appendChild(li);
        });
        list.querySelectorAll('[data-del-event]').forEach(btn => {
            btn.addEventListener('click', () => deleteEvent(btn.dataset.delEvent, btn.dataset.title));
        });
    } catch (err) {
        list.innerHTML = `<li><span class="hint">${escapeHtml(errorMessage(err, 'Failed to load events'))}</span></li>`;
    }
}

async function deleteEvent(id, title) {
    if (!confirm(`Remove event "${title}"?`)) return;
    try {
        await api(`/admin/events/${id}`, { method: 'DELETE' });
        await loadEvents();
    } catch (err) {
        alert(errorMessage(err, 'Failed to remove event'));
    }
}

document.getElementById('event-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    hide('event-error');
    const submitBtn = e.target.querySelector('button[type="submit"]');
    if (submitBtn) submitBtn.disabled = true;
    const title = document.getElementById('event_title').value.trim();
    const event_date = document.getElementById('event_date').value;
    const description = document.getElementById('event_description').value.trim();
    try {
        await api('/admin/events', { method: 'POST', json: { title, event_date, description: description || null } });
        document.getElementById('event-form').reset();
        await loadEvents();
    } catch (err) {
        document.getElementById('event-error').innerText = errorMessage(err, 'Failed to add event');
        show('event-error');
    } finally {
        if (submitBtn) submitBtn.disabled = false;
    }
});

// Admin: manage students (database view/edit/delete)
let CLASSES_CACHE = [];
const SECTION_LABELS = {}; // section_id -> "Class X - Section Y", built from /admin/classes

// Load classes and populate the cascading filter.
async function loadClasses() {
    const classSel = document.getElementById('students_class');
    try {
        const data = await api('/admin/classes');
        CLASSES_CACHE = data.classes;
        classSel.innerHTML = '<option value="" disabled selected>Select a class...</option>';
        Object.keys(SECTION_LABELS).forEach(k => delete SECTION_LABELS[k]);
        data.classes.forEach(c => {
            const opt = document.createElement('option');
            opt.value = c.id;
            opt.textContent = c.name;
            classSel.appendChild(opt);
            c.sections.forEach(s => { SECTION_LABELS[s.id] = `${c.name} - ${s.name}`; });
        });
    } catch (err) {
        classSel.innerHTML = `<option>${escapeHtml(errorMessage(err, 'Failed to load classes'))}</option>`;
    }
}

// When a class is chosen, populate its sections.
document.getElementById('students_class').addEventListener('change', (e) => {
    const cls = CLASSES_CACHE.find(c => String(c.id) === e.target.value);
    const secSel = document.getElementById('students_section');
    secSel.innerHTML = '<option value="" disabled selected>Select a section...</option>';
    if (cls) {
        cls.sections.forEach(s => {
            const opt = document.createElement('option');
            opt.value = s.id;
            opt.textContent = s.name;
            secSel.appendChild(opt);
        });
        secSel.disabled = false;
    }
});

document.getElementById('btn-find-students').addEventListener('click', () => {
    const sectionId = document.getElementById('students_section').value;
    if (!sectionId) { alert('Please select a class and section first.'); return; }
    loadStudents(sectionId);
});

let currentStudentsSection = null;
async function loadStudents(sectionId) {
    currentStudentsSection = sectionId;
    show('students-table-wrap');
    const tbody = document.getElementById('students-tbody');
    tbody.innerHTML = '<tr><td colspan="8">Loading...</td></tr>';
    try {
        const data = await api(`/admin/students?section_id=${sectionId}`);
        tbody.innerHTML = '';
        if (!data.students.length) {
            tbody.innerHTML = '<tr><td colspan="8">No students registered yet.</td></tr>';
            return;
        }
        data.students.forEach(s => {
            const tr = document.createElement('tr');
            tr.innerHTML = `
                <td>${s.id}</td>
                <td>${escapeHtml(s.name)}</td>
                <td>${escapeHtml(s.roll_no)}</td>
                <td>${escapeHtml(SECTION_LABELS[s.section_id] || s.section_id)}</td>
                <td>${s.has_image ? '✅' : '❌'}</td>
                <td>${s.has_embedding ? '✅' : '❌'}</td>
                <td>${s.percentage}%</td>
                <td class="row-actions">
                    <button class="mini-btn" data-edit='${escapeHtml(JSON.stringify(s))}'>Edit</button>
                    <button class="mini-btn warn" data-reset="${s.id}" data-name="${escapeHtml(s.name)}">Reset Login</button>
                    <button class="mini-btn danger" data-del="${s.id}" data-name="${escapeHtml(s.name)}">Del</button>
                </td>`;
            tbody.appendChild(tr);
        });
        // wire edit/delete/reset buttons
        tbody.querySelectorAll('[data-edit]').forEach(btn => {
            btn.addEventListener('click', () => openEditModal(JSON.parse(btn.dataset.edit)));
        });
        tbody.querySelectorAll('[data-del]').forEach(btn => {
            btn.addEventListener('click', () => deleteStudent(btn.dataset.del, btn.dataset.name));
        });
        tbody.querySelectorAll('[data-reset]').forEach(btn => {
            btn.addEventListener('click', () => resetParent(btn.dataset.reset, btn.dataset.name));
        });
    } catch (err) {
        tbody.innerHTML = `<tr><td colspan="8">${escapeHtml(errorMessage(err, 'Failed to load'))}</td></tr>`;
    }
}

function refreshStudents() {
    if (currentStudentsSection) loadStudents(currentStudentsSection);
}

function openEditModal(s) {
    document.getElementById('edit_id').value = s.id;
    document.getElementById('edit_name').value = s.name;
    document.getElementById('edit_roll').value = s.roll_no;
    document.getElementById('edit_section').value = s.section_id;
    hide('edit-error');
    show('edit-modal');
}
document.getElementById('btn-cancel-edit').addEventListener('click', () => hide('edit-modal'));

document.getElementById('btn-save-edit').addEventListener('click', async () => {
    const id = document.getElementById('edit_id').value;
    try {
        await api(`/admin/students/${id}`, {
            method: 'PUT',
            json: {
                name: document.getElementById('edit_name').value,
                roll_no: document.getElementById('edit_roll').value,
                section_id: parseInt(document.getElementById('edit_section').value),
            },
        });
        hide('edit-modal');
        refreshStudents();
    } catch (err) {
        const box = document.getElementById('edit-error');
        box.innerText = errorMessage(err, 'Update failed');
        show('edit-error');
    }
});

async function deleteStudent(id, name) {
    if (!confirm(`Delete ${name}? Their attendance history is kept and they can be restored later ` +
                 `from "Deleted Students" — only their parent login is removed.`)) return;
    try {
        await api(`/admin/students/${id}`, { method: 'DELETE' });
        refreshStudents();
    } catch (err) {
        alert(errorMessage(err, 'Delete failed'));
    }
}

document.getElementById('btn-load-deleted').addEventListener('click', loadDeletedStudents);

async function loadDeletedStudents() {
    show('deleted-students-wrap');
    const tbody = document.getElementById('deleted-students-tbody');
    tbody.innerHTML = '<tr><td colspan="5">Loading...</td></tr>';
    try {
        const data = await api('/admin/students/deleted');
        if (!data.students.length) {
            tbody.innerHTML = '<tr><td colspan="5">No deleted students.</td></tr>';
            return;
        }
        tbody.innerHTML = '';
        data.students.forEach(s => {
            const tr = document.createElement('tr');
            const when = new Date(s.deleted_at).toLocaleString();
            tr.innerHTML = `
                <td>${s.id}</td><td>${escapeHtml(s.name)}</td><td>${escapeHtml(s.roll_no)}</td><td>${when}</td>
                <td><button class="mini-btn" data-restore="${s.id}" data-name="${escapeHtml(s.name)}">Restore</button></td>`;
            tbody.appendChild(tr);
        });
        tbody.querySelectorAll('[data-restore]').forEach(btn => {
            btn.addEventListener('click', () => restoreStudent(btn.dataset.restore, btn.dataset.name));
        });
    } catch (err) {
        tbody.innerHTML = `<tr><td colspan="5">${escapeHtml(errorMessage(err, 'Failed to load'))}</td></tr>`;
    }
}

async function restoreStudent(id, name) {
    if (!confirm(`Restore ${name}? They'll reappear in their section's roster. ` +
                 `You'll need to generate a new parent login for them afterward.`)) return;
    try {
        await api(`/admin/students/${id}/restore`, { method: 'POST' });
        loadDeletedStudents();
        refreshStudents();
    } catch (err) {
        alert(errorMessage(err, 'Restore failed'));
    }
}

async function resetParent(id, name) {
    if (!confirm(`Generate a NEW username & password for ${name}'s parent?\n` +
                 `The student's photo, embedding and attendance records are kept.`)) return;
    const box = document.getElementById('students-msg');
    try {
        const data = await api(`/admin/students/${id}/reset_parent`, { method: 'POST' });
        const c = data.parent_credentials;
        box.className = 'result-box success-box';
        box.innerHTML = `<b>New login for ${escapeHtml(name)} (give to parent):</b><br>
            Username: <b>${escapeHtml(c.username)}</b><br>
            Password: <b>${escapeHtml(c.password)}</b><br>
            <span class="hint">Shown once.</span>`;
        show('students-msg');
    } catch (err) {
        box.className = 'result-box error-text';
        box.innerText = errorMessage(err, 'Reset failed');
        show('students-msg');
    }
}

// Admin: link an additional child to an existing parent account
async function loadLinkChildOptions() {
    const parentSelect = document.getElementById('link_parent_username');
    const studentSelect = document.getElementById('link_student_id');
    parentSelect.innerHTML = '<option value="" disabled selected>Loading parent accounts...</option>';
    studentSelect.innerHTML = '<option value="" disabled selected>Loading students...</option>';
    try {
        const data = await api('/admin/students');
        const students = data.students;

        studentSelect.innerHTML = '<option value="" disabled selected>Select a student...</option>';
        students.forEach(s => {
            const opt = document.createElement('option');
            opt.value = s.id;
            opt.innerText = `${s.name} (Roll ${s.roll_no}) — ${SECTION_LABELS[s.section_id] || s.section_id}`;
            studentSelect.appendChild(opt);
        });

        // One entry per distinct parent login, labeled with whichever child(ren) it
        // currently belongs to — so an admin can never mistake "which jaysurya" a
        // username actually points to, unlike free-typing a raw username string.
        const byUsername = new Map();
        students.forEach(s => {
            if (!s.parent_username) return;
            if (!byUsername.has(s.parent_username)) byUsername.set(s.parent_username, []);
            byUsername.get(s.parent_username).push(s);
        });
        parentSelect.innerHTML = '<option value="" disabled selected>Select a parent account...</option>';
        byUsername.forEach((kids, username) => {
            const opt = document.createElement('option');
            opt.value = username;
            const kidLabel = kids.map(k => `${k.name} (Roll ${k.roll_no})`).join(' + ');
            opt.innerText = `${username} — currently: ${kidLabel}`;
            parentSelect.appendChild(opt);
        });
    } catch (err) {
        parentSelect.innerHTML = '<option value="" disabled selected>Failed to load</option>';
        studentSelect.innerHTML = '<option value="" disabled selected>Failed to load</option>';
    }
}

document.getElementById('link-child-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    hide('link-child-error'); hide('link-child-success');
    const parent_username = document.getElementById('link_parent_username').value;
    const student_id = parseInt(document.getElementById('link_student_id').value);
    try {
        const data = await api('/admin/link_child', { method: 'POST', json: { parent_username, student_id } });
        document.getElementById('link-child-success').innerText = data.message;
        show('link-child-success');
        document.getElementById('link-child-form').reset();
        loadLinkChildOptions();
    } catch (err) {
        document.getElementById('link-child-error').innerText = errorMessage(err, 'Failed to link child');
        show('link-child-error');
    }
});

// Admin: register student
document.getElementById('register-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    hide('register-error'); hide('credentials-box');
    const submitBtn = e.target.querySelector('button[type="submit"]');
    if (submitBtn) submitBtn.disabled = true;
    try {
        const data = await api('/admin/register_student', {
            method: 'POST',
            json: {
                name: document.getElementById('reg_name').value,
                roll_no: document.getElementById('reg_roll').value,
                section_id: parseInt(document.getElementById('reg_section').value),
            },
        });
        setText('cred-username', data.parent_credentials.username);
        setText('cred-password', data.parent_credentials.password);
        show('credentials-box');
        document.getElementById('register-form').reset();
    } catch (err) {
        const box = document.getElementById('register-error');
        box.innerText = errorMessage(err, 'Registration failed');
        show('register-error');
    } finally {
        if (submitBtn) submitBtn.disabled = false;
    }
});

// ===================== Admin: manage teachers =====================
document.getElementById('teacher-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    hide('teacher-error'); hide('teacher-credentials-box');
    const submitBtn = e.target.querySelector('button[type="submit"]');
    if (submitBtn) submitBtn.disabled = true;
    try {
        const data = await api('/admin/register_teacher', {
            method: 'POST',
            json: {
                username: document.getElementById('teacher_username').value,
                email: document.getElementById('teacher_email').value || null,
            },
        });
        setText('teacher-cred-username', data.teacher_credentials.username);
        setText('teacher-cred-password', data.teacher_credentials.password);
        show('teacher-credentials-box');
        document.getElementById('teacher-form').reset();
        loadTeachers();
    } catch (err) {
        const box = document.getElementById('teacher-error');
        box.innerText = errorMessage(err, 'Registration failed');
        show('teacher-error');
    } finally {
        if (submitBtn) submitBtn.disabled = false;
    }
});

async function loadTeachers() {
    const tbody = document.getElementById('teachers-tbody');
    tbody.innerHTML = '<tr><td colspan="4">Loading...</td></tr>';
    try {
        const data = await api('/admin/teachers');
        tbody.innerHTML = '';
        if (!data.teachers.length) {
            tbody.innerHTML = '<tr><td colspan="4">No teacher accounts yet</td></tr>';
            return;
        }
        data.teachers.forEach(t => {
            const tr = document.createElement('tr');
            tr.innerHTML = `
                <td>${t.id}</td>
                <td>${escapeHtml(t.username)}</td>
                <td>${escapeHtml(t.email || '—')}</td>
                <td class="row-actions">
                    <button class="mini-btn" data-reset-teacher="${t.id}" data-name="${escapeHtml(t.username)}">Reset Password</button>
                    <button class="mini-btn" style="border-color:var(--error);color:var(--error);" data-del-teacher="${t.id}" data-name="${escapeHtml(t.username)}">Delete</button>
                </td>`;
            tbody.appendChild(tr);
        });
        // data-* + addEventListener instead of inline onclick with an interpolated
        // username — an inline onclick string can't be made safe just by HTML-escaping
        // the value, since the browser HTML-decodes the attribute BEFORE handing it to
        // the JS parser as the handler body, undoing the escaping right before it matters.
        tbody.querySelectorAll('[data-reset-teacher]').forEach(btn => {
            btn.addEventListener('click', () => resetTeacherPassword(btn.dataset.resetTeacher, btn.dataset.name));
        });
        tbody.querySelectorAll('[data-del-teacher]').forEach(btn => {
            btn.addEventListener('click', () => deleteTeacher(btn.dataset.delTeacher, btn.dataset.name));
        });
    } catch (err) {
        tbody.innerHTML = `<tr><td colspan="4">${escapeHtml(errorMessage(err, 'Failed to load teachers'))}</td></tr>`;
    }
}

async function resetTeacherPassword(id, username) {
    if (!confirm(`Reset password for ${username}?`)) return;
    const box = document.getElementById('teacher-reset-box');
    try {
        const data = await api(`/admin/teachers/${id}/reset_password`, { method: 'POST' });
        box.innerHTML = `<h3>New Teacher Password</h3>
            <p>Username: <b>${escapeHtml(data.username)}</b></p>
            <p>Password: <b>${escapeHtml(data.password)}</b></p>
            <p class="hint">Shown once — save it now.</p>`;
        box.className = 'result-box success-box';
        show('teacher-reset-box');
    } catch (err) {
        box.innerText = errorMessage(err, 'Failed to reset password');
        box.className = 'result-box error-text';
        show('teacher-reset-box');
    }
}

async function deleteTeacher(id, username) {
    if (!confirm(`Delete teacher account "${username}"? This cannot be undone.`)) return;
    try {
        await api(`/admin/teachers/${id}`, { method: 'DELETE' });
        loadTeachers();
    } catch (err) {
        alert(errorMessage(err, 'Failed to delete teacher'));
    }
}

// Admin: embeddings — class/section scope selector
function embedSectionId() {
    const v = document.getElementById('embed_section').value;
    return v ? parseInt(v) : null;
}

async function initEmbedTab() {
    await loadEmbedClasses();
    checkEmbeddingStatus();
}

// Populate the embeddings class dropdown (reuses CLASSES_CACHE / SECTION_LABELS).
async function loadEmbedClasses() {
    const classSel = document.getElementById('embed_class');
    try {
        const data = await api('/admin/classes');
        CLASSES_CACHE = data.classes;
        classSel.innerHTML = '<option value="" selected>All sections</option>';
        data.classes.forEach(c => {
            const opt = document.createElement('option');
            opt.value = c.id;
            opt.textContent = c.name;
            classSel.appendChild(opt);
            c.sections.forEach(s => { SECTION_LABELS[s.id] = `${c.name} - ${s.name}`; });
        });
    } catch (err) {
        classSel.innerHTML = `<option>${escapeHtml(errorMessage(err, 'Failed to load classes'))}</option>`;
    }
}

// When a class is chosen, populate its sections (or reset to "All sections").
document.getElementById('embed_class').addEventListener('change', (e) => {
    const cls = CLASSES_CACHE.find(c => String(c.id) === e.target.value);
    const secSel = document.getElementById('embed_section');
    secSel.innerHTML = '<option value="" selected>All sections</option>';
    if (cls) {
        cls.sections.forEach(s => {
            const opt = document.createElement('option');
            opt.value = s.id;
            opt.textContent = s.name;
            secSel.appendChild(opt);
        });
        secSel.disabled = false;
    } else {
        secSel.disabled = true;
    }
    checkEmbeddingStatus();
});
document.getElementById('embed_section').addEventListener('change', checkEmbeddingStatus);

// Admin: embedding status
async function checkEmbeddingStatus() {
    const box = document.getElementById('embed-status');
    show(box.id); box.innerHTML = 'Checking...';
    try {
        const sid = embedSectionId();
        const data = await api('/admin/embedding_status' + (sid ? `?section_id=${sid}` : ''));
        const scopeLabel = sid ? (SECTION_LABELS[sid] || `Section ${sid}`) : 'All sections';
        let html = `<p>Scope: <b>${escapeHtml(scopeLabel)}</b></p><p>Total students: <b>${data.total_students}</b></p>`;
        if (data.missing_image.length) {
            html += `<p class="error-text"><b>Cannot generate yet.</b> These students have info but no image:</p><ul class="styled-list">`;
            data.missing_image.forEach(s => {
                html += `<li><span>${escapeHtml(s.name)}</span><span class="score">Roll ${escapeHtml(s.roll_no)} — no image in DB</span></li>`;
            });
            html += `</ul>`;
            document.getElementById('btn-generate').disabled = true;
        } else {
            html += `<p class="success-text">All students have images. Ready to generate.</p>`;
            document.getElementById('btn-generate').disabled = false;
        }
        box.innerHTML = html;
    } catch (err) {
        box.innerText = errorMessage(err, 'Failed to load status');
    }
}
document.getElementById('btn-check-status').addEventListener('click', checkEmbeddingStatus);

// Admin: generate embeddings
document.getElementById('btn-generate').addEventListener('click', async () => {
    const box = document.getElementById('embed-result');
    show(box.id); box.innerHTML = 'Generating embeddings (this can take a moment)...';
    try {
        const sid = embedSectionId();
        const data = await api('/admin/generate_embeddings' + (sid ? `?section_id=${sid}` : ''), { method: 'POST' });
        box.innerHTML = `<span class="success-text">${data.message} (${data.count} students)</span>`;
    } catch (err) {
        if (err.detail && err.detail.missing_roll_nos) {
            box.innerHTML = `<span class="error-text">${escapeHtml(err.detail.message)}</span><br>Missing: ${err.detail.missing_roll_nos.map(escapeHtml).join(', ')}`;
        } else {
            box.innerText = errorMessage(err, 'Failed to generate embeddings');
        }
    }
});

// ===================== Parent: camera capture (3-second, 9-frame burst) =====================
let cameraStream = null;
let capturedFrames = [];   // Blob[] — up to 9 frames spanning ~3 seconds

const BURST_FRAME_COUNT = 9;
const BURST_DURATION_MS = 3000;

async function startCamera() {
    try {
        cameraStream = await navigator.mediaDevices.getUserMedia({ video: { facingMode: 'user' } });
        const video = document.getElementById('camera');
        video.srcObject = cameraStream;
        show('camera'); show('btn-snap'); hide('btn-start-cam');
    } catch (err) {
        const box = document.getElementById('capture-result');
        box.innerText = 'Could not access camera: ' + err.message;
        show('capture-result');
    }
}
function stopCamera() {
    if (cameraStream) { cameraStream.getTracks().forEach(t => t.stop()); cameraStream = null; }
}
document.getElementById('btn-start-cam').addEventListener('click', startCamera);

function grabFrame(video, canvas) {
    canvas.width = video.videoWidth;
    canvas.height = video.videoHeight;
    canvas.getContext('2d').drawImage(video, 0, 0);
    return new Promise(resolve => canvas.toBlob(resolve, 'image/jpeg', 0.9));
}

document.getElementById('btn-snap').addEventListener('click', async () => {
    const video = document.getElementById('camera');
    const canvas = document.getElementById('capture-canvas');
    const btn = document.getElementById('btn-snap');
    btn.disabled = true;
    capturedFrames = [];

    const progressBar = document.getElementById('burst-progress-bar');
    const progressText = document.getElementById('burst-progress-text');
    show('burst-progress');

    const interval = BURST_DURATION_MS / BURST_FRAME_COUNT;
    for (let i = 0; i < BURST_FRAME_COUNT; i++) {
        const blob = await grabFrame(video, canvas);
        if (blob) capturedFrames.push(blob);
        progressBar.style.width = `${Math.round(((i + 1) / BURST_FRAME_COUNT) * 100)}%`;
        progressText.innerText = `Capturing... ${i + 1} / ${BURST_FRAME_COUNT}`;
        if (i < BURST_FRAME_COUNT - 1) await new Promise(r => setTimeout(r, interval));
    }

    hide('camera'); stopCamera(); hide('burst-progress');
    btn.disabled = false;

    const thumbs = document.getElementById('burst-thumbs');
    thumbs.innerHTML = '';
    capturedFrames.forEach(blob => {
        const img = document.createElement('img');
        img.src = URL.createObjectURL(blob);
        img.style.cssText = 'width:56px;height:56px;object-fit:cover;border-radius:6px;';
        thumbs.appendChild(img);
    });
    show('burst-thumbs');

    hide('btn-snap'); show('btn-retake'); show('btn-upload-photo');
});

document.getElementById('btn-retake').addEventListener('click', () => {
    capturedFrames = [];
    hide('burst-thumbs'); hide('capture-preview'); hide('btn-retake'); hide('btn-upload-photo');
    hide('btn-continue-anyway');
    document.getElementById('capture-result').className = 'result-box hidden';
    startCamera();
});

// Single-photo upload, used only by the file-picker fallback below.
async function uploadParentPhoto(fileOrBlob, filename) {
    const box = document.getElementById('capture-result');
    show(box.id); box.innerText = 'Uploading...';
    const form = new FormData();
    form.append('file', fileOrBlob, filename);
    try {
        await api('/parent/upload_photo', { method: 'POST', form });
        box.innerHTML = '<span class="success-text">Photo saved! Loading your dashboard...</span>';
        stopCamera();
        setTimeout(() => { showView('parent-dashboard-view'); loadParentDashboard(); }, 1200);
        return true;
    } catch (err) {
        box.innerText = (errorMessage(err, 'Upload failed')) + ' — please try a clearer, front-facing photo.';
        return false;
    }
}

// Uploads all captured burst frames together — each becomes its own reference embedding.
document.getElementById('btn-upload-photo').addEventListener('click', async () => {
    if (!capturedFrames.length) return;
    const box = document.getElementById('capture-result');
    show(box.id); box.innerText = `Uploading ${capturedFrames.length} frames...`;
    const form = new FormData();
    capturedFrames.forEach((blob, i) => form.append('files', blob, `frame_${i}.jpg`));
    try {
        const data = await api('/parent/upload_photo_burst', { method: 'POST', form });
        if (data.quality_warning) {
            // Advisory only — the capture succeeded and is usable, but flag it now while the
            // camera is still open so retaking is one click instead of a separate trip later.
            box.className = 'result-box warning-box';
            box.innerHTML = `<span class="success-text">Captured ${data.frames_saved} clear frames.</span><br>` +
                             `<span class="warning-text">⚠️ ${data.quality_warning}</span>`;
            show('btn-continue-anyway');
            show('btn-retake');
        } else {
            box.className = 'result-box';
            box.innerHTML = `<span class="success-text">Captured ${data.frames_saved} clear frames! Loading your dashboard...</span>`;
            setTimeout(() => { showView('parent-dashboard-view'); loadParentDashboard(); }, 1200);
        }
    } catch (err) {
        box.innerText = (errorMessage(err, 'Upload failed')) + ' — please retake, keeping the face centered.';
        document.getElementById('btn-retake').click();
    }
});
document.getElementById('btn-continue-anyway').addEventListener('click', () => {
    hide('btn-continue-anyway');
    showView('parent-dashboard-view');
    loadParentDashboard();
});

// Upload an existing photo file (phone camera or gallery).
document.getElementById('btn-upload-file').addEventListener('click', async () => {
    const input = document.getElementById('parent_photo_file');
    const file = input.files[0];
    if (!file) {
        const box = document.getElementById('capture-result');
        show(box.id); box.innerText = 'Please choose a photo first.';
        return;
    }
    await uploadParentPhoto(file, file.name || 'upload.jpg');
});

// ===================== Parent: dashboard =====================
let currentCalendarDate = new Date();

// Parent attendance state: all records + which calendar date is currently selected.
let parentRecords = [];
let selectedDateKey = null;

// Color for an attendance status: present=green, absent=red, leave=amber, pending=grey.
function statusColor(status) {
    if (status === 'present') return 'var(--primary)';
    if (status === 'leave') return 'var(--accent-gold)';   // leave (neither present nor absent)
    if (status === 'pending') return '#9ca3af';   // leave requested, subject's teacher hasn't decided yet
    return 'var(--error)';                        // absent
}

// Show the classes (subject + status) for one date in the "Today's Classes" pane.
function showDateClasses(key) {
    selectedDateKey = key;
    const list = document.getElementById('p-today');
    const title = document.getElementById('p-today-title');
    if (!list) return;

    const now = new Date();
    const todayKey = `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, '0')}-${String(now.getDate()).padStart(2, '0')}`;
    if (title) {
        if (key === todayKey) {
            title.textContent = "Today's Classes";
        } else {
            const [y, m, d] = key.split('-');
            title.textContent = `Classes on ${d}/${m}/${y}`;
        }
    }

    const dayRecords = parentRecords.filter(r => r.date.split('T')[0] === key);
    list.innerHTML = '';
    if (!dayRecords.length) {
        list.innerHTML = '<li><span class="hint">No classes on this day</span></li>';
        return;
    }
    dayRecords.forEach(r => {
        const li = document.createElement('li');
        li.innerHTML = `<span>${escapeHtml(r.subject)}</span><span class="badge" style="background:${statusColor(r.status)}">${r.status.toUpperCase()}</span>`;
        list.appendChild(li);
    });
}

// Called when a parent clicks a calendar day.
window.selectCalendarDay = function (key) {
    showDateClasses(key);
    renderCalendar(parentRecords);   // re-render to move the highlight
};

function renderCalendar(records) {
    const container = document.getElementById('parent-calendar');
    if (!container) return;
    
    const recordMap = {};
    records.forEach(r => {
        // Handle both "YYYY-MM-DD" and "YYYY-MM-DDTHH:MM:SS" formats safely
        const key = r.date.split('T')[0];
        recordMap[key] = r.status;
    });

    const year = currentCalendarDate.getFullYear();
    const month = currentCalendarDate.getMonth();
    
    const firstDay = new Date(year, month, 1).getDay();
    const daysInMonth = new Date(year, month + 1, 0).getDate();
    
    const monthNames = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"];
    
    let html = `
        <div class="calendar-container">
            <div class="calendar-header">
                <button id="cal-prev">&lt;</button>
                <span>${monthNames[month]} ${year}</span>
                <button id="cal-next">&gt;</button>
            </div>
            <div class="calendar-grid">
                <div class="calendar-day-header">Su</div>
                <div class="calendar-day-header">Mo</div>
                <div class="calendar-day-header">Tu</div>
                <div class="calendar-day-header">We</div>
                <div class="calendar-day-header">Th</div>
                <div class="calendar-day-header">Fr</div>
                <div class="calendar-day-header">Sa</div>
    `;
    
    for (let i = 0; i < firstDay; i++) {
        html += `<div class="calendar-day empty"></div>`;
    }
    
    for (let day = 1; day <= daysInMonth; day++) {
        const key = `${year}-${String(month + 1).padStart(2, '0')}-${String(day).padStart(2, '0')}`;
        const status = recordMap[key];
        const dotColor = status ? statusColor(status) : '';
        const dot = dotColor ? `<span class="calendar-day-dot" style="background:${dotColor}"></span>` : '';
        const selected = key === selectedDateKey ? ' selected' : '';
        html += `<div class="calendar-day${selected}" style="cursor:pointer;" title="${status || 'no record'} — click to view" onclick="selectCalendarDay('${key}')"><span>${day}</span>${dot}</div>`;
    }
    
    html += `
            </div>
        </div>
    `;
    container.innerHTML = html;
    
    document.getElementById('cal-prev').addEventListener('click', () => {
        currentCalendarDate.setMonth(currentCalendarDate.getMonth() - 1);
        renderCalendar(records);
    });
    
    document.getElementById('cal-next').addEventListener('click', () => {
        currentCalendarDate.setMonth(currentCalendarDate.getMonth() + 1);
        renderCalendar(records);
    });
}

// ===================== Parent: multiple children =====================
let currentChildId = null;  // null -> use the account's default/primary child

function childQS() {
    return currentChildId ? `student_id=${currentChildId}` : '';
}
function withChildQS(url) {
    const qs = childQS();
    if (!qs) return url;
    return url + (url.includes('?') ? '&' : '?') + qs;
}

async function loadParentChildren() {
    try {
        const data = await api('/parent/children');
        const sel = document.getElementById('child-switcher');
        if (data.children.length <= 1) {
            hide('child-switcher-wrap');
            currentChildId = data.children.length ? data.children[0].id : null;
            return;
        }
        show('child-switcher-wrap');
        sel.innerHTML = '';
        data.children.forEach(c => {
            const opt = document.createElement('option');
            opt.value = c.id;
            opt.textContent = `${c.name} (Roll ${c.roll_no})`;
            sel.appendChild(opt);
        });
        currentChildId = data.children[0].id;
        sel.value = currentChildId;
    } catch (err) {
        // silent — falls back to the account's default single child
    }
}
document.getElementById('child-switcher').addEventListener('change', (e) => {
    currentChildId = parseInt(e.target.value);
    loadParentDashboard();
    refreshNotificationBadge();
});

async function loadParentDashboard() {
    // Captured before the request goes out — if the parent switches to a different
    // child while this is in flight, a slower response for the OLD child must not
    // overwrite the newer child's already-rendered dashboard once it lands.
    const requestedChildId = currentChildId;
    try {
        const data = await api(withChildQS('/parent/attendance'));
        if (currentChildId !== requestedChildId) return;
        setText('p-pct', data.percentage + '%');
        setText('p-present', `${data.present}`);
        setText('p-total', `${data.unique_days} Day${data.unique_days !== 1 ? 's' : ''}, ${data.total} Class${data.total !== 1 ? 'es' : ''}`);
        setText('p-student', `${data.student.name} (Roll ${data.student.roll_no})`);

        // Store records and show today's classes by default (calendar can change the date).
        parentRecords = data.records || [];
        const now = new Date();
        selectedDateKey = `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, '0')}-${String(now.getDate()).padStart(2, '0')}`;
        showDateClasses(selectedDateKey);

        // Subject-wise month & year percentages
        const sbody = document.getElementById('p-subjects-body');
        sbody.innerHTML = '';
        const subjects = data.by_subject || [];
        if (!subjects.length) {
            sbody.innerHTML = '<tr><td colspan="4">No attendance yet</td></tr>';
        } else {
            const fmt = (pct, p, t) => {
                if (t === 0) return '<span class="hint">— (no graded class)</span>';
                const color = pct >= 75 ? 'var(--success)' : (pct >= 50 ? '#b8860b' : 'var(--error)');
                return `<span style="color:${color};font-weight:600">${pct}%</span> <span class="hint">(${p}/${t})</span>`;
            };
            const leaveCell = (n) => n > 0
                ? `<span style="color:#d4a017;font-weight:600">${n}</span>`
                : '<span class="hint">0</span>';
            subjects.forEach(s => {
                const tr = document.createElement('tr');
                tr.innerHTML = `<td>${escapeHtml(s.subject)}</td>`
                    + `<td>${fmt(s.month_percentage, s.month_present, s.month_total)}</td>`
                    + `<td>${fmt(s.year_percentage, s.year_present, s.year_total)}</td>`
                    + `<td>${leaveCell(s.year_leave)}</td>`;
                sbody.appendChild(tr);
            });
        }

        // Subject-wise progress bars (Dashboard summary view) — same data as the
        // detailed table above, just rendered as bars instead of numeric columns.
        const progressWrap = document.getElementById('p-subjects-progress');
        progressWrap.innerHTML = '';
        if (!subjects.length) {
            progressWrap.innerHTML = '<p class="hint">No attendance yet</p>';
        } else {
            subjects.forEach(s => {
                const pct = s.month_total === 0 ? 0 : s.month_percentage;
                const row = document.createElement('div');
                row.className = 'subject-progress-row';
                row.innerHTML = `
                    <div class="subject-progress-label">${escapeHtml(s.subject)}</div>
                    <div class="progress-track"><div class="progress-fill" style="width:${pct}%"></div></div>`;
                progressWrap.appendChild(row);
            });
        }

        renderCalendar(data.records);
        loadParentLeaves();
        loadParentEvents();
    } catch (err) {
        setText('p-student', errorMessage(err, 'Failed to load'));
    }
}

function _renderLeaveListInto(list, leaves) {
    list.innerHTML = '';
    if (leaves.length === 0) {
        list.innerHTML = '<li><span class="hint">No leave requests.</span></li>';
        return;
    }
    const badgeColor = { approved: 'var(--primary)', rejected: 'var(--error)', pending: 'var(--accent-gold)' };
    leaves.forEach(l => {
        const li = document.createElement('li');
        // Each subject's teacher decides independently — show the per-subject
        // breakdown, not just one overall status, so a parent can see e.g. Hindi
        // approved while English is still pending for the same leave request.
        const perSubjectBadges = (l.approvals || []).map(a =>
            `<span class="badge" style="background:${badgeColor[a.status] || 'var(--primary)'}; margin-left:4px;"
                   title="${escapeHtml(a.subject)}">${escapeHtml(a.subject)}: ${escapeHtml(a.status)}</span>`
        ).join('');
        li.innerHTML = `
            <div style="flex:1">
                <strong>${l.start_date} to ${l.end_date}</strong><br>
                <span style="font-size:0.9em; color:#666">${escapeHtml(l.reason)}</span><br>
                <span style="display:inline-block; margin-top:4px;">${perSubjectBadges}</span>
            </div>
            <span class="badge" style="background:${badgeColor[l.status] || 'var(--primary)'}">${escapeHtml(l.status)}</span>
        `;
        list.appendChild(li);
    });
}

async function loadParentLeaves() {
    try {
        const leaves = await api(withChildQS('/parent/leave'));
        _renderLeaveListInto(document.getElementById('p-leave-list'), leaves);
        _renderLeaveListInto(document.getElementById('p-leave-list-summary'), leaves.slice(0, 3));
        // The dashboard card is only ever a 3-item preview — Leave History (above)
        // always has the full list. Make that explicit instead of leaving it looking
        // like the preview IS the complete list when there are more than 3.
        const moreNotice = document.getElementById('p-leave-more-notice');
        if (moreNotice) {
            if (leaves.length > 3) {
                moreNotice.textContent = `+${leaves.length - 3} more — view all in Leave History`;
                show('p-leave-more-notice');
            } else {
                hide('p-leave-more-notice');
            }
        }
    } catch (err) {
        console.error(err);
    }
}

async function loadParentEvents() {
    const list = document.getElementById('p-events-list');
    if (!list) return;
    try {
        const data = await api('/parent/events');
        list.innerHTML = '';
        if (!data.events.length) {
            list.innerHTML = '<li><span class="hint">No upcoming events.</span></li>';
            return;
        }
        data.events.forEach(e => {
            const li = document.createElement('li');
            li.innerHTML = `
                <div style="flex:1">
                    <strong>${escapeHtml(e.title)}</strong><br>
                    <span style="font-size:0.9em; color:#666">${e.event_date}${e.description ? ' — ' + escapeHtml(e.description) : ''}</span>
                </div>`;
            list.appendChild(li);
        });
    } catch (err) {
        list.innerHTML = '<li><span class="hint">Could not load events.</span></li>';
    }
}

// ===================== Notification drawers (chat-sidebar style) =====================
// Generic open/close for any {overlay, panel} pair — used by both the parent and
// teacher notification drawers so they behave and animate identically.
function openDrawer(overlayId, panelId) {
    document.getElementById(overlayId).classList.add('open');
    document.getElementById(panelId).classList.add('open');
}
function closeDrawer(overlayId, panelId) {
    document.getElementById(overlayId).classList.remove('open');
    document.getElementById(panelId).classList.remove('open');
}

// ---------- Parent notifications ----------
async function refreshNotificationBadge() {
    if (state.role !== 'parent') return;
    try {
        const data = await api('/parent/notifications');
        const badge = document.getElementById('notif-badge');
        if (data.unread_count > 0) {
            badge.innerText = data.unread_count;
            show('notif-badge');
        } else {
            hide('notif-badge');
        }
    } catch (err) {
        // silent — a failed badge check shouldn't interrupt the dashboard
    }
}

async function loadNotificationsPanel() {
    const list = document.getElementById('notifications-list');
    list.innerHTML = '<p class="hint">Loading...</p>';
    try {
        const data = await api('/parent/notifications');
        list.innerHTML = '';
        if (!data.notifications.length) {
            list.innerHTML = '<p class="hint">No notifications yet.</p>';
            return;
        }
        data.notifications.forEach(n => {
            const bubble = document.createElement('div');
            bubble.className = 'chat-bubble' + (n.is_read ? '' : ' unread');
            const when = new Date(n.created_at).toLocaleString();
            bubble.innerHTML = `
                <span class="chat-bubble-text">${escapeHtml(n.message)}</span>
                <span class="chat-bubble-time">${when}</span>`;
            list.appendChild(bubble);
        });
    } catch (err) {
        list.innerHTML = `<p class="hint">${escapeHtml(errorMessage(err, 'Failed to load notifications'))}</p>`;
    }
}

document.getElementById('btn-notifications').addEventListener('click', () => {
    openDrawer('notif-overlay', 'notifications-panel');
    loadNotificationsPanel();
});
document.getElementById('btn-close-notifs').addEventListener('click', () => closeDrawer('notif-overlay', 'notifications-panel'));
document.getElementById('notif-overlay').addEventListener('click', () => closeDrawer('notif-overlay', 'notifications-panel'));

document.getElementById('btn-mark-notifs-read').addEventListener('click', async () => {
    try {
        await api('/parent/notifications/read', { method: 'POST' });
        await loadNotificationsPanel();
        await refreshNotificationBadge();
    } catch (err) {
        alert(errorMessage(err, 'Failed to mark notifications read'));
    }
});

// ---------- Teacher notifications (pending leave requests) ----------
async function loadTeacherNotificationsPanel() {
    const list = document.getElementById('teacher-notifications-list');
    list.innerHTML = '<p class="hint">Loading...</p>';
    try {
        const data = await api('/teacher/leave/pending');
        list.innerHTML = '';
        if (!data.leaves.length) {
            list.innerHTML = '<p class="hint">No pending leave requests.</p>';
            return;
        }
        data.leaves.forEach(l => {
            const bubble = document.createElement('div');
            bubble.className = 'chat-bubble unread';
            bubble.innerHTML = `
                <span class="chat-bubble-text">
                    <b>${escapeHtml(l.student_name)}</b> (${escapeHtml(l.roll_no)}) requested
                    <b>${escapeHtml(l.subject)}</b> leave<br>
                    ${l.start_date} to ${l.end_date} — ${escapeHtml(l.reason)}
                </span>
                <div class="chat-bubble-actions">
                    <button class="primary-btn" data-approve-leave="${l.id}" data-subject="${escapeHtml(l.subject)}">Approve</button>
                    <button class="secondary-btn" style="border-color: var(--error); color: var(--error);" data-reject-leave="${l.id}" data-subject="${escapeHtml(l.subject)}">Reject</button>
                </div>`;
            list.appendChild(bubble);
        });
        // data-* + addEventListener instead of inline onclick — subject is admin-entered
        // free text, so it can't be safely interpolated into an inline onclick string
        // (same reasoning as the teacher-username fix elsewhere in this file).
        list.querySelectorAll('[data-approve-leave]').forEach(btn => {
            btn.addEventListener('click', () => approveTeacherNotifLeave(btn.dataset.approveLeave, btn.dataset.subject));
        });
        list.querySelectorAll('[data-reject-leave]').forEach(btn => {
            btn.addEventListener('click', () => rejectTeacherNotifLeave(btn.dataset.rejectLeave, btn.dataset.subject));
        });
    } catch (err) {
        list.innerHTML = `<p class="hint">${escapeHtml(errorMessage(err, 'Failed to load leave requests'))}</p>`;
    }
}

async function approveTeacherNotifLeave(id, subject) {
    await updateLeave(id, subject, 'approved');
    loadTeacherNotificationsPanel();
}
async function rejectTeacherNotifLeave(id, subject) {
    await updateLeave(id, subject, 'rejected');
    loadTeacherNotificationsPanel();
}

document.getElementById('btn-teacher-notifications').addEventListener('click', () => {
    openDrawer('teacher-notif-overlay', 'teacher-notifications-panel');
    loadTeacherNotificationsPanel();
});
document.getElementById('btn-close-teacher-notifs').addEventListener('click', () => closeDrawer('teacher-notif-overlay', 'teacher-notifications-panel'));
document.getElementById('teacher-notif-overlay').addEventListener('click', () => closeDrawer('teacher-notif-overlay', 'teacher-notifications-panel'));

// ===================== Parent: Download My Attendance =====================
function parentReportQueryString() {
    const startDate = document.getElementById('p_report_start_date').value;
    const endDate = document.getElementById('p_report_end_date').value;
    if (!startDate || !endDate) {
        throw new Error('Select both start and end dates first.');
    }
    let qs = `start_date=${startDate}&end_date=${endDate}`;
    if (currentChildId) qs += `&student_id=${currentChildId}`;
    return qs;
}
document.getElementById('btn-p-report-csv').addEventListener('click', async () => {
    hide('p-report-error');
    try {
        const qs = parentReportQueryString();
        await downloadAuthed(`/parent/attendance_report.csv?${qs}`, 'my_attendance_report.csv', 'p-report-error');
    } catch (err) {
        document.getElementById('p-report-error').innerText = err.message;
        show('p-report-error');
    }
});
document.getElementById('btn-p-report-pdf').addEventListener('click', async () => {
    hide('p-report-error');
    try {
        const qs = parentReportQueryString();
        await downloadAuthed(`/parent/attendance_report.pdf?${qs}`, 'my_attendance_report.pdf', 'p-report-error');
    } catch (err) {
        document.getElementById('p-report-error').innerText = err.message;
        show('p-report-error');
    }
});

document.getElementById('leave-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    const submitBtn = e.target.querySelector('button[type="submit"]');
    if (submitBtn) submitBtn.disabled = true;
    const start = document.getElementById('leave_start').value;
    const end = document.getElementById('leave_end').value;
    const reason = document.getElementById('leave_reason').value;
    try {
        await api('/parent/leave', { method: 'POST', json: { start_date: start, end_date: end, reason: reason, student_id: currentChildId } });
        alert("Leave request submitted successfully");
        document.getElementById('leave-form').reset();
        loadParentLeaves();
    } catch (err) {
        alert(errorMessage(err, 'Failed to submit leave request'));
    } finally {
        if (submitBtn) submitBtn.disabled = false;
    }
});

// ===================== Teacher: tabs =====================
function teacherTab(which) {
    document.getElementById('tab-attendance').classList.toggle('active', which === 'attendance');
    document.getElementById('tab-analytics').classList.toggle('active', which === 'analytics');
    document.getElementById('tab-leaves').classList.toggle('active', which === 'leaves');
    document.getElementById('teacher-attendance').classList.toggle('hidden', which !== 'attendance');
    document.getElementById('teacher-analytics').classList.toggle('hidden', which !== 'analytics');
    document.getElementById('teacher-leaves').classList.toggle('hidden', which !== 'leaves');
    if (which !== 'attendance') stopGpCamera();  // release the camera when leaving this tab
}
document.getElementById('tab-attendance').addEventListener('click', () => teacherTab('attendance'));
document.getElementById('tab-analytics').addEventListener('click', () => {
    teacherTab('analytics');
    // Auto fetch fresh data
    fetchAnalytics();
});
document.getElementById('btn-export-csv').addEventListener('click', async () => {
    const sec = document.getElementById('analytics_section').value;
    const subj = document.getElementById('analytics_subject').value;
    try {
        const token = localStorage.getItem('token');
        let url = `/teacher/analytics/export?section_id=${sec}&period=${analyticsPeriod}`;
        if (subj !== 'All') url += `&subject=${encodeURIComponent(subj)}`;

        const res = await fetch(API_BASE + url, {
            headers: { 'Authorization': `Bearer ${token}` }
        });
        if (!res.ok) throw new Error("Failed to export");
        const blob = await res.blob();
        const objectUrl = window.URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = objectUrl;
        a.download = `attendance_report_section_${sec}.csv`;
        document.body.appendChild(a);
        a.click();
        a.remove();
    } catch(err) {
        alert(err.message);
    }
});
document.getElementById('tab-leaves').addEventListener('click', () => {
    teacherTab('leaves');
    loadTeacherLeaves();
    refreshPendingLeaveBadge();
});

async function loadTeacherLeaves() {
    const sec = parseInt(document.getElementById('leave_section').value);
    if (!sec || Number.isNaN(sec)) {
        // Fires as soon as the "Leave Requests" tab is clicked, which can race the
        // section dropdown's own async load — with nothing selected yet there's no
        // valid request to make, so wait rather than sending a doomed one.
        return;
    }
    try {
        const leaves = await api(`/teacher/leave?section_id=${sec}`);
        const list = document.getElementById('t-leave-list');
        list.innerHTML = '';
        if (leaves.length === 0) {
            list.innerHTML = '<li><span class="hint">No leave requests.</span></li>';
        } else {
            leaves.forEach(l => {
                const li = document.createElement('li');
                let actions;
                if (l.status === 'pending') {
                    actions = `
                        <button class="primary-btn" style="padding: 4px 12px;" data-approve-leave="${l.id}" data-subject="${escapeHtml(l.subject)}">Approve</button>
                        <button class="secondary-btn" style="padding: 4px 12px; border-color: var(--error); color: var(--error);" data-reject-leave="${l.id}" data-subject="${escapeHtml(l.subject)}">Reject</button>
                    `;
                } else {
                    const color = l.status === 'approved' ? '#16a34a' : 'var(--error)';
                    actions = `<span style="padding: 4px 12px; font-weight: 600; text-transform: capitalize; color: ${color};">${escapeHtml(l.status)}</span>`;
                }
                li.innerHTML = `
                    <div style="flex:1">
                        <strong>${escapeHtml(l.student_name)} (${escapeHtml(l.roll_no)})</strong> — <b>${escapeHtml(l.subject)}</b><br>
                        <span style="font-size:0.9em; color:#666">${l.start_date} to ${l.end_date} - ${escapeHtml(l.reason)}</span>
                    </div>
                    <div style="display: flex; gap: 8px; align-items: center;">
                        ${actions}
                    </div>
                `;
                list.appendChild(li);
            });
            list.querySelectorAll('[data-approve-leave]').forEach(btn => {
                btn.addEventListener('click', () => updateLeave(btn.dataset.approveLeave, btn.dataset.subject, 'approved'));
            });
            list.querySelectorAll('[data-reject-leave]').forEach(btn => {
                btn.addEventListener('click', () => updateLeave(btn.dataset.rejectLeave, btn.dataset.subject, 'rejected'));
            });
        }
    } catch (err) {
        alert(errorMessage(err, 'Failed to load leaves'));
    }
}
document.getElementById('btn-load-leaves').addEventListener('click', loadTeacherLeaves);

async function updateLeave(id, subject, status) {
    try {
        await api(`/teacher/leave/${id}/status`, { method: 'POST', json: { subject: subject, status: status } });
        loadTeacherLeaves();
        refreshPendingLeaveBadge();
    } catch (err) {
        alert(errorMessage(err, 'Failed to update leave'));
    }
}

async function refreshPendingLeaveBadge() {
    try {
        const data = await api('/teacher/leave/pending_count');
        [ 'leave-pending-badge', 'teacher-notif-badge' ].forEach(id => {
            const badge = document.getElementById(id);
            if (data.pending_count > 0) {
                badge.innerText = data.pending_count;
                show(id);
            } else {
                hide(id);
            }
        });
    } catch (err) {
        // silent — badge failure shouldn't interrupt the teacher dashboard
    }
}

// ===================== Teacher: classroom photo — upload OR camera, with preview =====================
// Whichever source the teacher used (file picker, drag-drop, or camera capture) ends
// up here as one Blob/File — the submit handler reads this instead of the raw file
// input, since a camera capture never populates group_photo.files itself. Each source
// shows its own preview in place — the dropzone box itself becomes the photo once one
// is picked, and the camera view becomes the captured shot once one is taken — rather
// than a separate preview area appearing below.
let selectedGroupPhotoBlob = null;
let groupPhotoSource = null;  // 'upload' | 'camera' — which pane's preview is the active selection
let gpCameraStream = null;

function resetDropzonePreview() {
    const img = document.getElementById('dropzone-preview');
    if (img) { img.classList.add('hidden'); img.src = ''; }
    show('dropzone-icon');
    show('dropzone-text');
}

function clearGroupPhotoSelection() {
    selectedGroupPhotoBlob = null;
    groupPhotoSource = null;
    resetDropzonePreview();
    const camImg = document.getElementById('gp-camera-preview');
    if (camImg) { camImg.classList.add('hidden'); camImg.src = ''; }
    hide('btn-gp-retake');
    const fileInput = document.getElementById('group_photo');
    if (fileInput) fileInput.value = '';
}

(function () {
    const dropzone = document.getElementById('dropzone');
    const fileInput = document.getElementById('group_photo');
    if (!dropzone || !fileInput) return;

    function useFile() {
        if (fileInput.files && fileInput.files.length) {
            selectedGroupPhotoBlob = fileInput.files[0];
            groupPhotoSource = 'upload';
            const img = document.getElementById('dropzone-preview');
            img.src = URL.createObjectURL(selectedGroupPhotoBlob);
            img.classList.remove('hidden');
            hide('dropzone-icon');
            hide('dropzone-text');
        }
    }
    fileInput.addEventListener('change', useFile);

    ['dragover', 'dragenter'].forEach(evt => {
        dropzone.addEventListener(evt, (e) => {
            e.preventDefault();
            dropzone.classList.add('dragover');
        });
    });
    ['dragleave', 'dragend'].forEach(evt => {
        dropzone.addEventListener(evt, () => dropzone.classList.remove('dragover'));
    });
    dropzone.addEventListener('drop', (e) => {
        e.preventDefault();
        dropzone.classList.remove('dragover');
        if (e.dataTransfer.files && e.dataTransfer.files.length) {
            fileInput.files = e.dataTransfer.files;
            useFile();
        }
    });
})();

// ---- Upload / Camera source tabs ----
function stopGpCamera() {
    if (gpCameraStream) { gpCameraStream.getTracks().forEach(t => t.stop()); gpCameraStream = null; }
}

function showGpUploadTab() {
    stopGpCamera();
    document.getElementById('gp-tab-upload').classList.replace('secondary-btn', 'primary-btn');
    document.getElementById('gp-tab-camera').classList.replace('primary-btn', 'secondary-btn');
    show('gp-upload-pane');
    hide('gp-camera-pane');
}

function showGpCameraTab() {
    document.getElementById('gp-tab-camera').classList.replace('secondary-btn', 'primary-btn');
    document.getElementById('gp-tab-upload').classList.replace('primary-btn', 'secondary-btn');
    hide('gp-upload-pane');
    show('gp-camera-pane');
    if (groupPhotoSource === 'camera' && selectedGroupPhotoBlob) {
        // A capture from earlier in this same session is still the active selection —
        // show it (with Retake available) instead of resetting to a fresh live view.
        hide('gp-camera-live');
        show('gp-camera-preview');
        show('btn-gp-retake');
    } else {
        // Not started yet, or the active selection came from the upload tab instead —
        // leave THAT selection alone and just present a fresh camera view here.
        show('gp-camera-live');
        hide('gp-camera-preview');
        hide('btn-gp-capture');
        show('btn-gp-start-cam');
        hide('btn-gp-retake');
    }
}

document.getElementById('gp-tab-upload').addEventListener('click', showGpUploadTab);
document.getElementById('gp-tab-camera').addEventListener('click', showGpCameraTab);

async function startGpCamera() {
    try {
        gpCameraStream = await navigator.mediaDevices.getUserMedia({ video: { facingMode: 'environment' } });
        document.getElementById('gp-camera').srcObject = gpCameraStream;
        hide('btn-gp-start-cam');
        show('btn-gp-capture');
    } catch (err) {
        alert('Could not access camera: ' + err.message);
    }
}
document.getElementById('btn-gp-start-cam').addEventListener('click', startGpCamera);

document.getElementById('btn-gp-capture').addEventListener('click', async () => {
    const video = document.getElementById('gp-camera');
    const canvas = document.getElementById('gp-capture-canvas');
    canvas.width = video.videoWidth;
    canvas.height = video.videoHeight;
    canvas.getContext('2d').drawImage(video, 0, 0);
    const blob = await new Promise(resolve => canvas.toBlob(resolve, 'image/jpeg', 0.9));
    selectedGroupPhotoBlob = blob;
    groupPhotoSource = 'camera';
    const img = document.getElementById('gp-camera-preview');
    img.src = URL.createObjectURL(blob);
    img.classList.remove('hidden');
    stopGpCamera();
    hide('gp-camera-live');
    show('btn-gp-retake');
});

// Lets the teacher discard a bad shot (blurry, wrong angle, someone missing from frame)
// and try again without leaving the camera tab or losing the section/subject already picked.
document.getElementById('btn-gp-retake').addEventListener('click', () => {
    clearGroupPhotoSelection();
    hide('btn-gp-retake');
    show('gp-camera-live');
    startGpCamera();
});

// Teacher: recognize + manual-correction checklist
// The checklist always lists the WHOLE section roster as checkboxes (pre-checked from
// recognition where confident), so a teacher can uncheck a wrong high-confidence match or
// check in a student the camera missed — not just fill in blanks for low-confidence faces.
let currentSection = 1;
let currentRoster = [];

let HIGH_CONF = 0.90;   // only >= this is auto-checked as present — school-configurable,
                        // refreshed from /recognition_settings each time a photo is scanned

// Live "Present"/"Absent" counts — recomputed from whichever boxes are ACTUALLY
// checked right now, so they always reflect the teacher's corrections, not just the
// scan's original guess. "Detected"/"Unrecognized" stay as the scan's own snapshot.
function updatePresentAbsentStats() {
    const checkedCount = document.querySelectorAll('.att-checkbox:checked').length;
    setText('stat-recognized', checkedCount);
    setText('stat-absent', currentRoster.length - checkedCount);
}

function renderAttendanceChecklist(roster, faceInfo) {
    const container = document.getElementById('attendance-checklist');
    container.innerHTML = '';
    roster.forEach(s => {
        const info = faceInfo[s.id];
        const checked = info ? info.checked : false;
        const cropHtml = info && info.crop
            ? `<img src="${info.crop}" class="face-crop ${checked ? 'ok' : ''}" alt="${escapeHtml(s.name)}">`
            : '<span class="face-crop face-crop-empty">—</span>';
        // A detected face (any confidence, including auto-checked high-confidence ones)
        // might still be the WRONG person — this lets the teacher hand it to whoever it
        // actually is, the same way an unmatched face already can be assigned below.
        const reassignHtml = info && info.crop
            ? `<select class="reassign-select styled-select" data-student-id="${s.id}" style="width:auto; padding:6px 10px; font-size:0.85em;">
                   <option value="">Wrong match? Reassign…</option>
                   ${roster.filter(other => other.id !== s.id)
                           .map(other => `<option value="${other.id}">${escapeHtml(other.name)} (${escapeHtml(other.roll_no)})</option>`)
                           .join('')}
               </select>`
            : '';
        const li = document.createElement('li');
        li.className = 'manual-row';
        li.innerHTML = `
            <input type="checkbox" class="att-checkbox" data-student-id="${s.id}" ${checked ? 'checked' : ''} style="width:20px;height:20px;flex-shrink:0;">
            ${cropHtml}
            <span class="manual-face-meta">
                <b>${escapeHtml(s.name)}</b>
                <span class="score">${escapeHtml(s.roll_no)}</span>
            </span>
            ${reassignHtml}`;
        container.appendChild(li);
    });

    // Live-updates on every checkbox toggle (event delegation — covers boxes re-rendered later too).
    container.addEventListener('change', (e) => {
        if (e.target.classList.contains('att-checkbox')) updatePresentAbsentStats();
    });

    container.querySelectorAll('.reassign-select').forEach(sel => {
        sel.addEventListener('change', () => {
            const targetId = sel.value;
            if (!targetId) return;
            const fromId = sel.dataset.studentId;
            const fromBox = container.querySelector(`.att-checkbox[data-student-id="${fromId}"]`);
            const toBox = container.querySelector(`.att-checkbox[data-student-id="${targetId}"]`);
            if (fromBox) fromBox.checked = false;
            if (toBox) toBox.checked = true;
            sel.value = '';
            updatePresentAbsentStats();  // programmatic .checked changes don't fire 'change'
        });
    });

    updatePresentAbsentStats();
}

function renderUnmatchedFaces(faces) {
    const list = document.getElementById('unmatched-faces-list');
    list.innerHTML = '';
    if (!faces.length) { hide('unmatched-faces-section'); return; }
    show('unmatched-faces-section');
    const options = currentRoster.map(s => `<option value="${s.id}">${escapeHtml(s.name)} (${escapeHtml(s.roll_no)})</option>`).join('');
    faces.forEach(f => {
        const li = document.createElement('li');
        li.className = 'manual-row';
        const cropImg = f.crop
            ? `<img src="${f.crop}" class="face-crop" alt="unmatched face">`
            : '<span class="face-crop face-crop-empty">?</span>';
        li.innerHTML = `
            ${cropImg}
            <span class="manual-face-meta">
                <b class="error-text">Unrecognized face</b>
                <span class="score">confidence ${(f.score * 100).toFixed(0)}%</span>
            </span>
            <select class="unmatched-select styled-select" data-face-idx="${f.idx}">
                <option value="">-- not a listed student --</option>
                ${options}
            </select>`;
        list.appendChild(li);
    });
}

// /teacher/recognize now queues the photo instead of processing it inline (so a burst
// of uploads can't crash the server) and returns a job_id immediately — this submits
// and then polls /teacher/recognize_status until it's done, resolving with the exact
// same {total_detected, results, engine, ...} shape the old single-call response had,
// so none of the code that consumes it below needed to change.
async function submitAndPollRecognize(form) {
    const { job_id } = await api('/teacher/recognize', { method: 'POST', form });
    const maxAttempts = 60;  // ~90s total at 1.5s intervals
    for (let attempt = 0; attempt < maxAttempts; attempt++) {
        await new Promise(resolve => setTimeout(resolve, 1500));
        const status = await api(`/teacher/recognize_status/${job_id}`);
        if (status.status === 'done') return status;
        // status.status === 'queued' -> still waiting, keep polling
    }
    throw { detail: 'Still processing — the server is busy right now. Please try again in a moment.' };
}

document.getElementById('attendance-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    if (!selectedGroupPhotoBlob) {
        alert('Please select or capture a classroom photo first.');
        return;
    }
    const submitBtn = e.target.querySelector('button[type="submit"]');
    if (submitBtn) submitBtn.disabled = true;
    show('loading'); hide('attendance-result'); hide('absent-box');
    currentSection = parseInt(document.getElementById('att_section_id').value);
    const subj = document.getElementById('attendance_subject').value || 'General';
    const form = new FormData();
    form.append('section_id', currentSection);
    form.append('subject', subj);
    form.append('file', selectedGroupPhotoBlob, 'photo.jpg');

    try {
        const [data, rosterData, recognitionSettings] = await Promise.all([
            submitAndPollRecognize(form),
            api(`/teacher/roster?section_id=${currentSection}`),
            api('/recognition_settings').catch(() => null),  // fall back to the existing HIGH_CONF if this fails
        ]);
        if (recognitionSettings) HIGH_CONF = recognitionSettings.auto_check_threshold;
        hide('loading'); show('attendance-result');
        currentRoster = rosterData.students;

        const faceInfo = {};      // student_id -> {checked, crop, score}
        const unmatchedFaces = []; // faces with no confident guess at all
        let unknown = 0;

        data.results.forEach((res, idx) => {
            if (res.status === 'recognized' && res.score >= HIGH_CONF) {
                faceInfo[res.student_id] = { checked: true, crop: res.crop, score: res.score };
            } else {
                unknown++;
                const guessId = (res.student_id != null && res.student_id !== -1) ? res.student_id : null;
                if (guessId && !faceInfo[guessId]) {
                    faceInfo[guessId] = { checked: false, crop: res.crop, score: res.score };
                } else if (!guessId) {
                    unmatchedFaces.push({ idx, crop: res.crop, score: res.score });
                }
            }
        });

        renderAttendanceChecklist(currentRoster, faceInfo);  // sets stat-recognized/stat-absent live
        renderUnmatchedFaces(unmatchedFaces);

        setText('stat-detected', data.total_detected);
        setText('stat-unknown', unknown);
    } catch (err) {
        hide('loading');
        alert(errorMessage(err, 'Recognition failed'));
    } finally {
        if (submitBtn) submitBtn.disabled = false;
    }
});

document.getElementById('btn-mark-all-present').addEventListener('click', async () => {
    const sectionId = parseInt(document.getElementById('att_section_id').value);
    if (!sectionId) { alert('Select a section first.'); return; }
    currentSection = sectionId;
    hide('absent-box');
    try {
        const rosterData = await api(`/teacher/roster?section_id=${sectionId}`);
        currentRoster = rosterData.students;
        renderAttendanceChecklist(currentRoster, {});           // nobody pre-checked...
        document.querySelectorAll('.att-checkbox').forEach(cb => cb.checked = true);  // ...then check everyone
        updatePresentAbsentStats();  // programmatic .checked changes above don't fire 'change'
        renderUnmatchedFaces([]);
        setText('stat-detected', 0);
        setText('stat-unknown', 0);
        show('attendance-result');
    } catch (err) {
        alert(errorMessage(err, 'Failed to load roster'));
    }
});

document.getElementById('btn-select-all').addEventListener('click', () => {
    document.querySelectorAll('.att-checkbox').forEach(cb => cb.checked = true);
    updatePresentAbsentStats();
});
document.getElementById('btn-deselect-all').addEventListener('click', () => {
    document.querySelectorAll('.att-checkbox').forEach(cb => cb.checked = false);
    updatePresentAbsentStats();
});

document.getElementById('btn-submit-final').addEventListener('click', async () => {
    const present = new Set();
    document.querySelectorAll('.att-checkbox:checked').forEach(cb => present.add(parseInt(cb.dataset.studentId)));
    document.querySelectorAll('.unmatched-select').forEach(sel => {
        if (sel.value) present.add(parseInt(sel.value));
    });
    const finalPresent = [...present];
    const subj = document.getElementById('attendance_subject').value;
    if (!subj) {
        alert('No subject selected — ask your admin to add subjects for this class before taking attendance.');
        return;
    }
    try {
        const sec = parseInt(document.getElementById('att_section_id').value) || currentSection;
        const data = await api('/teacher/submit_attendance', { method: 'POST', json: {
            section_id: sec,
            subject: subj,
            present_student_ids: finalPresent
        }});

        let msg = `${data.message}\n\nPresent: ${data.total_present}   Absent: ${data.absent_count}   Leave: ${data.leave_count || 0}`;
        if (data.leave_students && data.leave_students.length) {
            msg += '\n\nOn Leave (not counted absent):\n' +
                data.leave_students.map(s => `• ${s.name} (${s.roll_no})`).join('\n');
        }
        alert(msg);

        // Reset the form to a fresh state as requested by the user
        document.getElementById('attendance-form').reset();
        clearGroupPhotoSelection();
        showGpUploadTab();
        hide('attendance-result');
        hide('absent-box');
    } catch (err) {
        alert(errorMessage(err, 'Submit failed'));
    }
});

// Teacher: analytics
let currentTeacherCalendarDate = new Date();

function renderTeacherCalendar(dailyStats) {
    const container = document.getElementById('teacher-calendar');
    if (!container) return;

    const statsMap = {};
    dailyStats.forEach(d => {
        statsMap[d.date] = d;
    });

    const year = currentTeacherCalendarDate.getFullYear();
    const month = currentTeacherCalendarDate.getMonth();
    const firstDay = new Date(year, month, 1).getDay();
    const daysInMonth = new Date(year, month + 1, 0).getDate();
    const monthNames = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"];
    
    let html = `
        <div class="calendar-container" style="margin-bottom: 24px; padding: 16px;">
            <div class="calendar-header">
                <button id="tcal-prev">&lt;</button>
                <span>${monthNames[month]} ${year} (Class Attendance)</span>
                <button id="tcal-next">&gt;</button>
            </div>
            <div class="calendar-grid">
                <div class="calendar-day-header">Su</div>
                <div class="calendar-day-header">Mo</div>
                <div class="calendar-day-header">Tu</div>
                <div class="calendar-day-header">We</div>
                <div class="calendar-day-header">Th</div>
                <div class="calendar-day-header">Fr</div>
                <div class="calendar-day-header">Sa</div>
    `;
    
    for (let i = 0; i < firstDay; i++) {
        html += `<div class="calendar-day empty"></div>`;
    }
    
    for (let day = 1; day <= daysInMonth; day++) {
        const key = `${year}-${String(month + 1).padStart(2, '0')}-${String(day).padStart(2, '0')}`;
        const stat = statsMap[key];
        
        let cls = "calendar-day";
        let text = day;
        let title = "No records";
        
        if (stat) {
            text = `${stat.percentage}%`;
            title = `${stat.present}/${stat.total} present`;
            if (stat.percentage >= 85) {
                cls += " present";
            } else if (stat.percentage >= 70) {
                cls += " warning";
            } else {
                cls += " absent";
            }
        }
        
        html += `<div class="${cls}" title="${title}" style="flex-direction: column;">
            <span style="font-weight: bold;">${day}</span>
            ${stat ? `<span style="font-size: 0.75em; margin-top: 4px;">${text}</span>` : ''}
        </div>`;
    }
    
    html += `
            </div>
        </div>
    `;
    
    container.innerHTML = html;
    
    document.getElementById('tcal-prev').addEventListener('click', () => {
        currentTeacherCalendarDate.setMonth(currentTeacherCalendarDate.getMonth() - 1);
        renderTeacherCalendar(dailyStats);
    });
    document.getElementById('tcal-next').addEventListener('click', () => {
        currentTeacherCalendarDate.setMonth(currentTeacherCalendarDate.getMonth() + 1);
        renderTeacherCalendar(dailyStats);
    });
}

// 'all' | 'today' | 'week' | 'month' — which slice of attendance history the stats
// tables show. The calendar pane always shows full history regardless (see backend).
let analyticsPeriod = 'all';

function setAnalyticsPeriod(period) {
    analyticsPeriod = period;
    document.querySelectorAll('.period-btn').forEach(b => {
        const active = b.dataset.period === period;
        b.classList.toggle('primary-btn', active);
        b.classList.toggle('secondary-btn', !active);
    });
    fetchAnalytics();
}
document.querySelectorAll('.period-btn').forEach(btn => {
    btn.addEventListener('click', () => setAnalyticsPeriod(btn.dataset.period));
});

async function fetchAnalytics() {
    const sec = parseInt(document.getElementById('analytics_section').value);
    const subj = document.getElementById('analytics_subject').value;
    try {
        let url = `/teacher/analytics?section_id=${sec}&period=${analyticsPeriod}`;
        if (subj !== 'All') url += `&subject=${encodeURIComponent(subj)}`;
        const data = await api(url);
        
        const low = document.getElementById('low-list');
        const all = document.getElementById('all-list');
        low.innerHTML = ''; all.innerHTML = '';
        if (!data.low_attendance.length) {
            low.innerHTML = '<tr><td colspan="5"><span class="success-text">None below 75%</span></td></tr>';
        } else {
            data.low_attendance.forEach(s => {
                const tr = document.createElement('tr');
                tr.innerHTML = `<td class="error-text">${escapeHtml(s.name)} (${escapeHtml(s.roll_no)})</td>`
                    + `<td>${s.percentage}%</td><td>${s.present}</td><td>${s.absent}</td><td>${s.leave}</td>`;
                low.appendChild(tr);
            });
        }
        data.students.forEach(s => {
            const tr = document.createElement('tr');
            tr.innerHTML = `<td>${escapeHtml(s.name)} (${escapeHtml(s.roll_no)})</td>`
                + `<td>${s.percentage}%</td><td>${s.present}</td><td>${s.absent}</td><td>${s.leave}</td>`;
            all.appendChild(tr);
        });
        renderTeacherCalendar(data.daily_stats || []);
    } catch (err) {
        alert(errorMessage(err, 'Failed to load analytics'));
    }
}

document.getElementById('btn-load-analytics').addEventListener('click', fetchAnalytics);

// ===================== Shared: authed file download =====================
async function downloadAuthed(url, filename, errBoxId) {
    try {
        const res = await fetch(API_BASE + url, { headers: authHeaders() });
        if (!res.ok) {
            let body = null;
            try { body = await res.json(); } catch (_) {}
            throw new Error(errorMessage(body, `Request failed (${res.status})`));
        }
        const blob = await res.blob();
        const objectUrl = window.URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = objectUrl;
        a.download = filename;
        document.body.appendChild(a);
        a.click();
        a.remove();
        window.URL.revokeObjectURL(objectUrl);
    } catch (err) {
        const box = document.getElementById(errBoxId);
        box.innerText = err.message || 'Download failed';
        show(errBoxId);
    }
}

// ===================== Admin: Attendance Reports =====================
let REPORT_CLASSES_CACHE = [];

async function loadReportClasses() {
    const classSel = document.getElementById('report_class');
    try {
        const data = await api('/admin/classes');
        REPORT_CLASSES_CACHE = data.classes;
        classSel.innerHTML = '<option value="" disabled selected>Select a class...</option>';
        data.classes.forEach(c => {
            const opt = document.createElement('option');
            opt.value = c.id;
            opt.textContent = c.name;
            classSel.appendChild(opt);
        });
    } catch (err) {
        classSel.innerHTML = `<option>${escapeHtml(errorMessage(err, 'Failed to load classes'))}</option>`;
    }
}
document.getElementById('report_class').addEventListener('change', async (e) => {
    const cls = REPORT_CLASSES_CACHE.find(c => String(c.id) === e.target.value);
    const secSel = document.getElementById('report_section');
    secSel.innerHTML = '<option value="" disabled selected>Select a section...</option>';
    // Reset directly (rebuilding secSel's options programmatically doesn't fire a
    // native 'change' event, so report_section's own listener won't do this for us).
    document.getElementById('report_student').innerHTML = '<option value="All" selected>All Students</option>';
    if (cls) {
        cls.sections.forEach(s => {
            const opt = document.createElement('option');
            opt.value = s.id;
            opt.textContent = s.name;
            secSel.appendChild(opt);
        });
        secSel.disabled = false;
    }

    const subjSel = document.getElementById('report_subject');
    subjSel.innerHTML = '<option value="All" selected>All Subjects</option>';
    if (cls) {
        try {
            const data = await api(`/admin/subjects?class_id=${cls.id}`);
            data.subjects.forEach(s => {
                const opt = document.createElement('option');
                opt.value = s.name;
                opt.textContent = s.name;
                subjSel.appendChild(opt);
            });
        } catch (_) {
            // Leave just "All Subjects" if this fails — reports still work, just
            // without the per-subject narrowing until the page is retried.
        }
    }
});

// 'today' | 'week' | 'month' | 'year' (academic year) — which button was last clicked.
// null until the admin picks one; the preview/download actions all require this to be set.
let reportPeriod = null;
const REPORT_PERIOD_LABELS = { today: 'Today', week: 'Weekly', month: 'Monthly', year: 'Yearly' };

function reportQueryString() {
    const sectionId = document.getElementById('report_section').value;
    const subject = document.getElementById('report_subject').value || 'All';
    const studentId = document.getElementById('report_student').value;
    if (!sectionId) throw new Error('Select a class and section first.');
    if (!reportPeriod) throw new Error('Pick a time range (Today / Weekly / Monthly / Yearly) first.');
    let qs = `section_id=${sectionId}&period=${reportPeriod}&subject=${encodeURIComponent(subject)}`;
    if (studentId && studentId !== 'All') qs += `&student_id=${studentId}`;
    return qs;
}

async function loadReportPreview() {
    hide('report-error');
    hide('report-preview');
    try {
        const qs = reportQueryString();
        const data = await api(`/reports/attendance/preview?${qs}`);
        const titleParts = [];
        if (data.school_name) titleParts.push(data.school_name);
        document.getElementById('report-preview-title').innerText =
            titleParts.length ? titleParts.join(' — ') : 'Attendance Report';
        document.getElementById('report-preview-meta').innerText =
            `${data.period_label} (${data.start_date} to ${data.end_date}) — ` +
            `${data.rows.length} record${data.rows.length === 1 ? '' : 's'} — ` +
            `Present: ${data.counts.present}, Absent: ${data.counts.absent}, Leave: ${data.counts.leave}`;
        const studentSel = document.getElementById('report_student');
        const studentLabel = studentSel.value !== 'All' ? studentSel.options[studentSel.selectedIndex].textContent : null;
        if (studentLabel) {
            document.getElementById('report-preview-meta').innerText += `  —  Student: ${studentLabel}`;
        }
        const tbody = document.getElementById('report-preview-rows');
        tbody.innerHTML = '';
        if (!data.rows.length) {
            tbody.innerHTML = '<tr><td colspan="5"><span class="hint">No attendance records in this range.</span></td></tr>';
        } else {
            data.rows.forEach(r => {
                const tr = document.createElement('tr');
                tr.innerHTML = `<td>${escapeHtml(r.date)}</td><td>${escapeHtml(r.roll_no)}</td>`
                    + `<td>${escapeHtml(r.name)}</td><td>${escapeHtml(r.subject)}</td><td>${escapeHtml(r.status)}</td>`;
                tbody.appendChild(tr);
            });
        }
        show('report-preview');
    } catch (err) {
        const box = document.getElementById('report-error');
        box.innerText = err.message || errorMessage(err, 'Failed to load report');
        show('report-error');
    }
}

document.querySelectorAll('.report-period-btn').forEach(btn => {
    btn.addEventListener('click', () => {
        reportPeriod = btn.dataset.period;
        document.querySelectorAll('.report-period-btn').forEach(b => {
            const active = b.dataset.period === reportPeriod;
            b.classList.toggle('primary-btn', active);
            b.classList.toggle('secondary-btn', !active);
        });
        loadReportPreview();
    });
});
// Populates the Student dropdown for the newly-selected section, then (like
// subject) re-runs the currently selected period if one's already picked.
document.getElementById('report_section').addEventListener('change', async (e) => {
    const sectionId = e.target.value;
    const studentSel = document.getElementById('report_student');
    studentSel.innerHTML = '<option value="All" selected>All Students</option>';
    if (sectionId) {
        try {
            const data = await api(`/admin/students?section_id=${sectionId}`);
            data.students.forEach(s => {
                const opt = document.createElement('option');
                opt.value = s.id;
                opt.textContent = `${s.name} (${s.roll_no})`;
                studentSel.appendChild(opt);
            });
        } catch (_) {
            // Leave just "All Students" if this fails — reports still work for the
            // whole section, just without the per-student narrowing until retried.
        }
    }
    if (reportPeriod) loadReportPreview();
});
// Re-run the currently selected period whenever subject/student changes underneath it.
document.getElementById('report_subject').addEventListener('change', () => { if (reportPeriod) loadReportPreview(); });
document.getElementById('report_student').addEventListener('change', () => { if (reportPeriod) loadReportPreview(); });

document.getElementById('btn-report-csv').addEventListener('click', async () => {
    hide('report-error');
    try {
        const qs = reportQueryString();
        await downloadAuthed(`/reports/attendance.csv?${qs}`, `attendance_report_${REPORT_PERIOD_LABELS[reportPeriod]}.csv`, 'report-error');
    } catch (err) {
        const box = document.getElementById('report-error');
        box.innerText = err.message;
        show('report-error');
    }
});
document.getElementById('btn-report-pdf').addEventListener('click', async () => {
    hide('report-error');
    try {
        const qs = reportQueryString();
        await downloadAuthed(`/reports/attendance.pdf?${qs}`, `attendance_report_${REPORT_PERIOD_LABELS[reportPeriod]}.pdf`, 'report-error');
    } catch (err) {
        const box = document.getElementById('report-error');
        box.innerText = err.message;
        show('report-error');
    }
});

// ===================== Admin: Manage Classes & Sections =====================
async function loadClassesTab() {
    await populateSectionClassDropdown();
    await renderClassesTable();
    await populateSubjectsClassDropdown();
    await populateAssignmentClassDropdown();
    await populateAssignmentTeacherDropdown();
}

async function populateSubjectsClassDropdown() {
    const sel = document.getElementById('subjects_class');
    const prev = sel.value;
    try {
        const data = await api('/admin/classes');
        sel.innerHTML = '<option value="" disabled selected>Select a class...</option>';
        data.classes.forEach(c => {
            const opt = document.createElement('option');
            opt.value = c.id;
            opt.textContent = c.name;
            sel.appendChild(opt);
        });
        if (prev && sel.querySelector(`option[value="${prev}"]`)) {
            sel.value = prev;
            renderSubjectsList();
        }
    } catch (err) {
        sel.innerHTML = `<option>${escapeHtml(errorMessage(err, 'Failed to load classes'))}</option>`;
    }
}

async function renderSubjectsList() {
    const classId = document.getElementById('subjects_class').value;
    const list = document.getElementById('subjects-list');
    if (!classId) { list.innerHTML = ''; return; }
    list.innerHTML = '<li><span class="hint">Loading...</span></li>';
    try {
        const data = await api(`/admin/subjects?class_id=${classId}`);
        if (!data.subjects.length) {
            list.innerHTML = '<li><span class="hint">No subjects added yet for this class.</span></li>';
            return;
        }
        list.innerHTML = '';
        data.subjects.forEach(s => {
            const li = document.createElement('li');
            li.innerHTML = `<span style="flex:1">${escapeHtml(s.name)}</span>
                <button class="mini-btn danger" data-del-subject="${s.id}" data-name="${escapeHtml(s.name)}">Remove</button>`;
            list.appendChild(li);
        });
        list.querySelectorAll('[data-del-subject]').forEach(btn => {
            btn.addEventListener('click', () => deleteSubject(btn.dataset.delSubject, btn.dataset.name));
        });
    } catch (err) {
        list.innerHTML = `<li><span class="hint">${escapeHtml(errorMessage(err, 'Failed to load subjects'))}</span></li>`;
    }
}

document.getElementById('subjects_class').addEventListener('change', renderSubjectsList);

document.getElementById('subject-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    hide('subject-error');
    const class_id = parseInt(document.getElementById('subjects_class').value);
    const name = document.getElementById('new_subject_name').value.trim();
    if (!class_id) {
        document.getElementById('subject-error').innerText = 'Select a class first.';
        show('subject-error');
        return;
    }
    const submitBtn = e.target.querySelector('button[type="submit"]');
    if (submitBtn) submitBtn.disabled = true;
    try {
        await api('/admin/subjects', { method: 'POST', json: { class_id, name } });
        document.getElementById('new_subject_name').value = '';
        await renderSubjectsList();
    } catch (err) {
        document.getElementById('subject-error').innerText = errorMessage(err, 'Failed to add subject');
        show('subject-error');
    } finally {
        if (submitBtn) submitBtn.disabled = false;
    }
});

async function deleteSubject(id, name) {
    if (!confirm(`Remove subject "${name}"? Past attendance records already using it are unaffected.`)) return;
    try {
        await api(`/admin/subjects/${id}`, { method: 'DELETE' });
        await renderSubjectsList();
    } catch (err) {
        alert(errorMessage(err, 'Failed to remove subject'));
    }
}

// ===================== Admin: teacher assignments (teacher x section x subject) =====================
async function populateAssignmentClassDropdown() {
    const sel = document.getElementById('assign_class');
    try {
        const data = await api('/admin/classes');
        sel.innerHTML = '<option value="" disabled selected>Select a class...</option>';
        data.classes.forEach(c => {
            const opt = document.createElement('option');
            opt.value = c.id;
            opt.textContent = c.name;
            opt.dataset.sections = JSON.stringify(c.sections);
            sel.appendChild(opt);
        });
    } catch (err) {
        sel.innerHTML = `<option>${escapeHtml(errorMessage(err, 'Failed to load classes'))}</option>`;
    }
}

async function populateAssignmentTeacherDropdown() {
    const sel = document.getElementById('assign_teacher');
    try {
        const data = await api('/admin/teachers');
        sel.innerHTML = '<option value="" disabled selected>Select a teacher...</option>';
        data.teachers.forEach(t => {
            const opt = document.createElement('option');
            opt.value = t.id;
            opt.textContent = t.username;
            sel.appendChild(opt);
        });
    } catch (err) {
        sel.innerHTML = `<option>${escapeHtml(errorMessage(err, 'Failed to load teachers'))}</option>`;
    }
}

document.getElementById('assign_class').addEventListener('change', (e) => {
    const secSel = document.getElementById('assign_section');
    const subjSel = document.getElementById('assign_subject');
    const sections = JSON.parse(e.target.selectedOptions[0]?.dataset.sections || '[]');
    secSel.innerHTML = '<option value="" disabled selected>Select a section...</option>';
    subjSel.innerHTML = '<option value="" disabled selected>Select a section first...</option>';
    subjSel.disabled = true;
    sections.forEach(s => {
        const opt = document.createElement('option');
        opt.value = s.id;
        opt.textContent = s.name;
        secSel.appendChild(opt);
    });
    secSel.disabled = false;
    renderAssignmentsList();
});

document.getElementById('assign_section').addEventListener('change', async (e) => {
    const classId = document.getElementById('assign_class').value;
    const subjSel = document.getElementById('assign_subject');
    subjSel.innerHTML = '<option value="" disabled selected>Loading subjects...</option>';
    try {
        const data = await api(`/admin/subjects?class_id=${classId}`);
        subjSel.innerHTML = data.subjects.length
            ? '<option value="" disabled selected>Select a subject...</option>'
            : '<option value="General">General</option>';
        data.subjects.forEach(s => {
            const opt = document.createElement('option');
            opt.value = s.name;
            opt.textContent = s.name;
            subjSel.appendChild(opt);
        });
        subjSel.disabled = false;
    } catch (err) {
        subjSel.innerHTML = `<option>${escapeHtml(errorMessage(err, 'Failed to load subjects'))}</option>`;
    }
    renderAssignmentsList();
});

async function renderAssignmentsList() {
    const list = document.getElementById('assignments-list');
    const sectionId = document.getElementById('assign_section').value;
    if (!sectionId) { list.innerHTML = ''; return; }
    list.innerHTML = '<li><span class="hint">Loading...</span></li>';
    try {
        const data = await api(`/admin/teacher_assignments?section_id=${sectionId}`);
        if (!data.assignments.length) {
            list.innerHTML = '<li><span class="hint">Nobody assigned yet for this section — open to any teacher.</span></li>';
            return;
        }
        list.innerHTML = '';
        data.assignments.forEach(a => {
            const li = document.createElement('li');
            li.innerHTML = `<span style="flex:1">${escapeHtml(a.teacher_username)} — ${escapeHtml(a.subject)}</span>
                <button class="mini-btn danger" data-del-assignment="${a.id}">Remove</button>`;
            list.appendChild(li);
        });
        list.querySelectorAll('[data-del-assignment]').forEach(btn => {
            btn.addEventListener('click', () => deleteAssignment(btn.dataset.delAssignment));
        });
    } catch (err) {
        list.innerHTML = `<li><span class="hint">${escapeHtml(errorMessage(err, 'Failed to load assignments'))}</span></li>`;
    }
}

async function deleteAssignment(id) {
    if (!confirm('Remove this teacher assignment? The subject reverts to open-to-any-teacher for this section.')) return;
    try {
        await api(`/admin/teacher_assignments/${id}`, { method: 'DELETE' });
        await renderAssignmentsList();
    } catch (err) {
        alert(errorMessage(err, 'Failed to remove assignment'));
    }
}

document.getElementById('assignment-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    hide('assignment-error');
    const section_id = parseInt(document.getElementById('assign_section').value);
    const subject = document.getElementById('assign_subject').value;
    const teacher_id = parseInt(document.getElementById('assign_teacher').value);
    if (!section_id || !subject || !teacher_id) {
        document.getElementById('assignment-error').innerText = 'Select a class, section, subject, and teacher first.';
        show('assignment-error');
        return;
    }
    const submitBtn = e.target.querySelector('button[type="submit"]');
    if (submitBtn) submitBtn.disabled = true;
    try {
        await api('/admin/teacher_assignments', { method: 'POST', json: { teacher_id, section_id, subject } });
        await renderAssignmentsList();
    } catch (err) {
        document.getElementById('assignment-error').innerText = errorMessage(err, 'Failed to create assignment');
        show('assignment-error');
    } finally {
        if (submitBtn) submitBtn.disabled = false;
    }
});

async function populateSectionClassDropdown() {
    const sel = document.getElementById('new_section_class');
    try {
        const data = await api('/admin/classes');
        sel.innerHTML = '<option value="" disabled selected>Select a class...</option>';
        data.classes.forEach(c => {
            const opt = document.createElement('option');
            opt.value = c.id;
            opt.textContent = c.name;
            sel.appendChild(opt);
        });
    } catch (err) {
        sel.innerHTML = `<option>${escapeHtml(errorMessage(err, 'Failed to load classes'))}</option>`;
    }
}

async function renderClassesTable() {
    const tbody = document.getElementById('classes-tbody');
    tbody.innerHTML = '<tr><td colspan="2">Loading...</td></tr>';
    try {
        const data = await api('/admin/classes');
        tbody.innerHTML = '';
        if (!data.classes.length) {
            tbody.innerHTML = '<tr><td colspan="2">No classes yet.</td></tr>';
            return;
        }
        data.classes.forEach(c => {
            const tr = document.createElement('tr');
            const sections = c.sections.length ? c.sections.map(s => escapeHtml(s.name)).join(', ') : '(none yet)';
            tr.innerHTML = `<td>${escapeHtml(c.name)}</td><td>${sections}</td>`;
            tbody.appendChild(tr);
        });
    } catch (err) {
        tbody.innerHTML = `<tr><td colspan="2">${escapeHtml(errorMessage(err, 'Failed to load classes'))}</td></tr>`;
    }
}

document.getElementById('class-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    hide('class-error'); hide('class-success');
    const name = document.getElementById('new_class_name').value.trim();
    const submitBtn = e.target.querySelector('button[type="submit"]');
    if (submitBtn) submitBtn.disabled = true;
    try {
        await api('/admin/classes', { method: 'POST', json: { name } });
        document.getElementById('class-success').innerText = `Class "${name}" added.`;
        show('class-success');
        document.getElementById('new_class_name').value = '';
        await populateSectionClassDropdown();
        await renderClassesTable();
        await populateAdminFlatDropdowns();
    } catch (err) {
        document.getElementById('class-error').innerText = errorMessage(err, 'Failed to add class');
        show('class-error');
    } finally {
        if (submitBtn) submitBtn.disabled = false;
    }
});

document.getElementById('section-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    hide('section-error'); hide('section-success');
    const class_id = parseInt(document.getElementById('new_section_class').value);
    const name = document.getElementById('new_section_name').value.trim();
    if (!class_id) {
        document.getElementById('section-error').innerText = 'Select a class first.';
        show('section-error');
        return;
    }
    const submitBtn = e.target.querySelector('button[type="submit"]');
    if (submitBtn) submitBtn.disabled = true;
    try {
        await api('/admin/sections', { method: 'POST', json: { class_id, name } });
        document.getElementById('section-success').innerText = `Section "${name}" added.`;
        show('section-success');
        document.getElementById('new_section_name').value = '';
        await renderClassesTable();
        await populateAdminFlatDropdowns();
    } catch (err) {
        document.getElementById('section-error').innerText = errorMessage(err, 'Failed to add section');
        show('section-error');
    } finally {
        if (submitBtn) submitBtn.disabled = false;
    }
});

// ===================== Admin: Photo Storage (per-school S3 bucket) =====================
async function loadS3Settings() {
    const box = document.getElementById('storage-current-status');
    box.innerText = 'Checking...';
    try {
        const data = await api('/admin/s3_settings');
        if (data.configured) {
            box.className = 'result-box success-box';
            box.innerHTML = `<span class="success-text">Storage is set up.</span> Bucket: <b>${escapeHtml(data.bucket_name)}</b> (region: ${escapeHtml(data.region)})<br>
                <span class="hint">Saving new details below will replace this.</span>`;
        } else {
            box.className = 'result-box warning-box';
            box.innerText = 'Not set up yet — photos are currently saved directly in the database. Follow the steps below to switch to your own storage bucket.';
        }
    } catch (err) {
        box.className = 'result-box error-text';
        box.innerText = errorMessage(err, 'Failed to load storage status');
    }
}

document.getElementById('s3-settings-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    hide('s3-settings-error'); hide('s3-settings-success');
    try {
        await api('/admin/s3_settings', {
            method: 'POST',
            json: {
                access_key: document.getElementById('s3_access_key').value.trim(),
                secret_key: document.getElementById('s3_secret_key').value.trim(),
                bucket_name: document.getElementById('s3_bucket_name').value.trim(),
                region: document.getElementById('s3_region').value.trim() || 'us-east-1',
            },
        });
        document.getElementById('s3-settings-success').innerText = 'Saved! New student photos will now be stored in your bucket.';
        show('s3-settings-success');
        document.getElementById('s3-settings-form').reset();
        loadS3Settings();
    } catch (err) {
        document.getElementById('s3-settings-error').innerText = errorMessage(err, 'Failed to save storage settings');
        show('s3-settings-error');
    }
});

// ===================== Admin: Recognition Settings (per-school auto-check threshold) =====================
async function loadRecognitionSettings() {
    const box = document.getElementById('recognition-current-status');
    box.innerText = 'Checking...';
    try {
        const data = await api('/recognition_settings');
        const pct = Math.round(data.auto_check_threshold * 100);
        document.getElementById('recognition_threshold').value = pct;
        document.getElementById('recognition-threshold-display').innerText = pct + '%';
        document.getElementById('school_timezone_offset').value = data.timezone_offset_minutes || 0;
        box.className = data.is_default ? 'result-box' : 'result-box success-box';
        box.innerText = data.is_default
            ? `Using the default threshold (${pct}%) — you haven't customized this yet.`
            : `Custom threshold set: ${pct}%.`;
    } catch (err) {
        box.className = 'result-box error-text';
        box.innerText = errorMessage(err, 'Failed to load recognition settings');
    }
}

document.getElementById('recognition_threshold').addEventListener('input', (e) => {
    document.getElementById('recognition-threshold-display').innerText = e.target.value + '%';
});

document.getElementById('btn-save-recognition').addEventListener('click', async () => {
    hide('recognition-settings-error'); hide('recognition-settings-success');
    try {
        const pct = parseInt(document.getElementById('recognition_threshold').value);
        const tzRaw = document.getElementById('school_timezone_offset').value;
        await api('/admin/recognition_settings', {
            method: 'POST',
            json: { auto_check_threshold: pct / 100, timezone_offset_minutes: tzRaw === '' ? 0 : parseInt(tzRaw) },
        });
        document.getElementById('recognition-settings-success').innerText = 'Saved! This applies the next time a teacher scans an attendance photo.';
        show('recognition-settings-success');
        loadRecognitionSettings();
    } catch (err) {
        document.getElementById('recognition-settings-error').innerText = errorMessage(err, 'Failed to save');
        show('recognition-settings-error');
    }
});

// ===================== Admin: School Profile (report letterhead) =====================
async function loadSchoolProfile() {
    try {
        const data = await api('/admin/school_profile');
        document.getElementById('sp_school_name').value = data.school_name || '';
        document.getElementById('sp_school_place').value = data.school_place || '';
        document.getElementById('sp_academic_year_start_month').value = data.academic_year_start_month || 4;
        document.getElementById('sp_academic_year_end_month').value = data.academic_year_end_month || 3;
    } catch (err) {
        document.getElementById('school-profile-error').innerText = errorMessage(err, 'Failed to load school profile');
        show('school-profile-error');
    }
}

document.getElementById('btn-save-school-profile').addEventListener('click', async () => {
    hide('school-profile-error'); hide('school-profile-success');
    try {
        await api('/admin/school_profile', {
            method: 'POST',
            json: {
                school_name: document.getElementById('sp_school_name').value,
                school_place: document.getElementById('sp_school_place').value,
                academic_year_start_month: parseInt(document.getElementById('sp_academic_year_start_month').value),
                academic_year_end_month: parseInt(document.getElementById('sp_academic_year_end_month').value),
            },
        });
        document.getElementById('school-profile-success').innerText = 'Saved! This now appears on every attendance report.';
        show('school-profile-success');
    } catch (err) {
        document.getElementById('school-profile-error').innerText = errorMessage(err, 'Failed to save');
        show('school-profile-error');
    }
});

// ===================== Boot =====================
(async function boot() {
    if (state.token && state.role) {
        show('user-bar');
        setText('user-label', `Signed in as ${state.role}`);
        updateProfileNames();
        document.getElementById('btn-notifications').classList.toggle('hidden', state.role !== 'parent');
        document.getElementById('btn-teacher-notifications').classList.toggle('hidden', state.role !== 'teacher');
        if (state.role === 'admin') {
            showView('admin-view');
            populateAdminFlatDropdowns();
        } else if (state.role === 'teacher') {
            showView('teacher-view');
            populateTeacherFlatDropdowns();
            refreshPendingLeaveBadge();
        } else {
            // Parent: a saved token doesn't tell us if they actually finished the
            // capture last time — always re-check fresh from the server before routing.
            try {
                const status = await api('/parent/status');
                if (status.needs_capture) {
                    showView('parent-capture-view');
                } else {
                    showView('parent-dashboard-view');
                    loadParentChildren().then(loadParentDashboard);
                    refreshNotificationBadge();
                }
            } catch (err) {
                logout();  // token expired/invalid — back to login
            }
        }
    } else {
        showView('login-view');
    }
})();
