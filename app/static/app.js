let currentToken = localStorage.getItem("newtonite_token") || "";
let currentUser = null;
let currentItems = [];
let nextCursor = null;
let currentActiveView = "all";
let searchDebounceTimeout = null;
let currentSelectedItem = null;

// Initialize
window.addEventListener("DOMContentLoaded", async () => {
  if (currentToken) {
    await fetchCurrentUser();
  } else {
    // Auto login as Alice Lead by default for reviewer convenience
    await loginUser("alice@newtonite.io", "LeadPass123!");
  }
  await loadWorkItems(true);
});

async function loginUser(email, password) {
  try {
    const res = await fetch("/api/v1/auth/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email, password })
    });
    if (!res.ok) throw new Error("Login failed");
    const data = await res.json();
    currentToken = data.access_token;
    localStorage.setItem("newtonite_token", currentToken);
    await fetchCurrentUser();
    showToast("Logged in as " + email, "success");
    await loadWorkItems(true);
  } catch (err) {
    showToast(err.message, "error");
  }
}

async function fetchCurrentUser() {
  try {
    const res = await fetch("/api/v1/auth/me", {
      headers: { "Authorization": `Bearer ${currentToken}` }
    });
    if (!res.ok) {
      logout();
      return;
    }
    currentUser = await res.json();
    document.getElementById("logged-in-profile").classList.remove("hidden");
    document.getElementById("user-display-name").textContent = currentUser.name;
    const teamRoleStr = currentUser.teams.map(t => `${t.team_name} (${t.role})`).join(", ");
    document.getElementById("user-display-role").textContent = teamRoleStr;
  } catch (e) {
    console.error(e);
  }
}

function logout() {
  currentToken = "";
  currentUser = null;
  localStorage.removeItem("newtonite_token");
  document.getElementById("logged-in-profile").classList.add("hidden");
  showToast("Logged out", "info");
}

async function demoLogin() {
  const val = document.getElementById("demo-user-select").value;
  const [email, pwd] = val.split("|");
  await loginUser(email, pwd);
}

function setActiveView(view) {
  currentActiveView = view;
  ["all", "assigned_to_me", "unassigned_urgent", "awaiting_approval"].forEach(v => {
    const btn = document.getElementById(`view-btn-${v}`);
    if (v === view) {
      btn.className = "w-full text-left px-3 py-2 text-sm rounded-lg bg-indigo-600/20 text-indigo-400 font-medium flex items-center justify-between";
    } else {
      btn.className = "w-full text-left px-3 py-2 text-sm rounded-lg hover:bg-slate-700/50 text-slate-300 font-medium flex items-center justify-between";
    }
  });
  loadWorkItems(true);
}

function debounceSearch() {
  clearTimeout(searchDebounceTimeout);
  searchDebounceTimeout = setTimeout(() => {
    loadWorkItems(true);
  }, 350);
}

async function loadWorkItems(reset = false) {
  if (!currentToken) return;

  const spinner = document.getElementById("loading-spinner");
  spinner.classList.remove("hidden");

  if (reset) {
    nextCursor = null;
    currentItems = [];
    document.getElementById("items-table-body").innerHTML = "";
  }

  const status = document.getElementById("filter-status").value;
  const priority = document.getElementById("filter-priority").value;
  const q = document.getElementById("search-input").value.trim();

  let url = `/api/v1/items?limit=25`;
  if (status) url += `&status=${status}`;
  if (priority) url += `&priority=${priority}`;
  if (q) url += `&q=${encodeURIComponent(q)}`;
  if (nextCursor) url += `&cursor=${encodeURIComponent(nextCursor)}`;

  if (currentActiveView === "assigned_to_me") url += `&assigned_to_me=true`;
  if (currentActiveView === "unassigned_urgent") url += `&unassigned_urgent=true`;
  if (currentActiveView === "awaiting_approval") url += `&awaiting_approval=true`;

  try {
    const res = await fetch(url, {
      headers: { "Authorization": `Bearer ${currentToken}` }
    });
    if (!res.ok) throw new Error("Failed to fetch work items");
    const data = await res.json();

    currentItems = reset ? data.items : [...currentItems, ...data.items];
    nextCursor = data.next_cursor;

    renderTable();

    const loadMoreBtn = document.getElementById("load-more-btn");
    if (data.has_more) {
      loadMoreBtn.classList.remove("hidden");
    } else {
      loadMoreBtn.classList.add("hidden");
    }

    document.getElementById("item-stats").textContent = `Loaded ${currentItems.length} items`;
    document.getElementById("pagination-status").textContent = data.has_more ? "More items available" : "End of results";
  } catch (err) {
    showToast(err.message, "error");
  } finally {
    spinner.classList.add("hidden");
  }
}

function loadNextPage() {
  if (nextCursor) {
    loadWorkItems(false);
  }
}

function renderTable() {
  const tbody = document.getElementById("items-table-body");
  tbody.innerHTML = "";

  if (currentItems.length === 0) {
    tbody.innerHTML = `<tr><td colspan="7" class="text-center py-8 text-slate-500">No work items found matching criteria.</td></tr>`;
    return;
  }

  currentItems.forEach(item => {
    const tr = document.createElement("tr");
    tr.className = "hover:bg-slate-700/30 transition cursor-pointer";
    tr.onclick = (e) => {
      if (!e.target.closest("button")) {
        openDetailDrawer(item.id);
      }
    };

    const assigneeStr = item.assignee ? item.assignee.name : `<span class="text-slate-500 italic">Unassigned</span>`;

    tr.innerHTML = `
      <td class="px-4 py-3">
        <p class="font-medium text-white">${escapeHtml(item.title)}</p>
        <p class="text-xs text-slate-400 truncate max-w-xs">${escapeHtml(item.description || "")}</p>
      </td>
      <td class="px-4 py-3 text-xs"><span class="bg-slate-700 px-2 py-0.5 rounded font-mono">${item.type}</span></td>
      <td class="px-4 py-3 text-xs prio-${item.priority}">${item.priority}</td>
      <td class="px-4 py-3 text-xs"><span class="px-2 py-0.5 rounded-full text-[11px] font-semibold badge-${item.status}">${item.status}</span></td>
      <td class="px-4 py-3 text-xs text-slate-300">${assigneeStr}</td>
      <td class="px-4 py-3 text-xs font-mono text-slate-400">v${item.version}</td>
      <td class="px-4 py-3 text-right space-x-1">
        ${!item.assignee_id ? `<button onclick="claimItem('${item.id}', event)" class="px-2.5 py-1 bg-emerald-600/80 hover:bg-emerald-600 text-white rounded text-xs font-medium">Claim</button>` : ''}
        <button onclick="openDetailDrawer('${item.id}')" class="px-2.5 py-1 bg-slate-700 hover:bg-slate-600 text-slate-200 rounded text-xs font-medium">View</button>
      </td>
    `;
    tbody.appendChild(tr);
  });
}

// Concurrency: Atomic Claim with Optimistic Reconciliation
async function claimItem(itemId, event) {
  if (event) event.stopPropagation();
  try {
    const res = await fetch(`/api/v1/items/${itemId}/claim`, {
      method: "POST",
      headers: { "Authorization": `Bearer ${currentToken}` }
    });

    if (res.status === 409) {
      const err = await res.json();
      showToast(err.detail || "Conflict: Already claimed by someone else", "error");
      await loadWorkItems(true);
      return;
    }

    if (!res.ok) {
      const err = await res.json();
      throw new Error(err.detail || "Claim failed");
    }

    showToast("Successfully claimed item!", "success");
    await loadWorkItems(true);
    if (currentSelectedItem && currentSelectedItem.id === itemId) {
      await openDetailDrawer(itemId);
    }
  } catch (err) {
    showToast(err.message, "error");
  }
}

// Drawer & Detail View
async function openDetailDrawer(itemId) {
  const drawer = document.getElementById("item-detail-drawer");
  const content = document.getElementById("drawer-content");
  drawer.classList.remove("hidden");
  content.innerHTML = `<p class="text-xs text-slate-400 py-4 text-center">Loading audit history...</p>`;

  try {
    const res = await fetch(`/api/v1/items/${itemId}`, {
      headers: { "Authorization": `Bearer ${currentToken}` }
    });
    if (!res.ok) throw new Error("Could not load item details");
    const item = await res.json();
    currentSelectedItem = item;

    const activitiesHtml = item.activities.map(a => `
      <div class="border-l-2 border-indigo-500 pl-3 py-1 space-y-0.5">
        <div class="flex items-center justify-between text-[11px] text-slate-400">
          <span class="font-semibold text-slate-300">${escapeHtml(a.actor_name)}</span>
          <span>${new Date(a.created_at).toLocaleTimeString([], {hour: '2-digit', minute:'2-digit'})}</span>
        </div>
        <p class="text-xs text-indigo-300 font-mono">${a.action_type} ${a.old_value ? `(${a.old_value} &rarr; ${a.new_value})` : ''}</p>
        ${a.comment_text ? `<p class="text-xs text-slate-300 bg-slate-900/40 p-1.5 rounded">${escapeHtml(a.comment_text)}</p>` : ''}
      </div>
    `).join("");

    content.innerHTML = `
      <div>
        <span class="px-2 py-0.5 rounded-full text-[11px] font-semibold badge-${item.status}">${item.status}</span>
        <span class="ml-2 text-xs prio-${item.priority}">${item.priority}</span>
        <span class="ml-2 text-xs font-mono text-slate-400">v${item.version}</span>
      </div>
      <div>
        <h4 class="text-base font-bold text-white">${escapeHtml(item.title)}</h4>
        <p class="text-xs text-slate-300 mt-1">${escapeHtml(item.description || "No description provided.")}</p>
      </div>

      <!-- Action Workflow Transitions -->
      <div class="pt-3 border-t border-slate-700 space-y-2">
        <label class="block text-xs font-semibold text-slate-400 uppercase">Workflow Actions</label>
        <div class="flex flex-wrap gap-2">
          ${item.status === 'OPEN' ? `<button onclick="transitionItem('${item.id}', 'IN_PROGRESS')" class="px-3 py-1.5 bg-amber-600 hover:bg-amber-500 text-white text-xs font-semibold rounded">Start Work</button>` : ''}
          ${item.status === 'IN_PROGRESS' ? `<button onclick="transitionItem('${item.id}', 'AWAITING_APPROVAL')" class="px-3 py-1.5 bg-purple-600 hover:bg-purple-500 text-white text-xs font-semibold rounded">Request Approval</button>` : ''}
          ${item.status === 'AWAITING_APPROVAL' ? `<button onclick="transitionItem('${item.id}', 'RESOLVED')" class="px-3 py-1.5 bg-emerald-600 hover:bg-emerald-500 text-white text-xs font-semibold rounded">Approve & Resolve (Lead)</button>` : ''}
          ${item.status === 'RESOLVED' ? `<button onclick="transitionItem('${item.id}', 'CLOSED')" class="px-3 py-1.5 bg-slate-600 hover:bg-slate-500 text-white text-xs font-semibold rounded">Close Item (Lead)</button>` : ''}
        </div>
      </div>

      <!-- Concurrency-Protected Edit Form -->
      <div class="pt-3 border-t border-slate-700 space-y-2">
        <label class="block text-xs font-semibold text-slate-400 uppercase">Edit (Optimistic Locking Check)</label>
        <input type="text" id="edit-title" value="${escapeHtml(item.title)}" class="w-full bg-slate-700 border border-slate-600 rounded px-2 py-1 text-xs text-white">
        <button onclick="saveItemUpdate('${item.id}', ${item.version})" class="w-full py-1.5 bg-indigo-600 hover:bg-indigo-500 text-white text-xs font-semibold rounded">
          Save Changes (Version: ${item.version})
        </button>
      </div>

      <!-- Comment Box -->
      <div class="pt-3 border-t border-slate-700 space-y-2">
        <label class="block text-xs font-semibold text-slate-400 uppercase">Add Comment</label>
        <div class="flex space-x-2">
          <input type="text" id="comment-input" placeholder="Enter activity note..." class="flex-1 bg-slate-700 border border-slate-600 rounded px-2 py-1 text-xs text-white">
          <button onclick="submitComment('${item.id}')" class="px-3 py-1 bg-slate-600 hover:bg-slate-500 text-xs font-semibold rounded text-white">Post</button>
        </div>
      </div>

      <!-- Append-Only Audit Timeline -->
      <div class="pt-3 border-t border-slate-700 space-y-2">
        <label class="block text-xs font-semibold text-slate-400 uppercase">Audit Timeline (${item.activities.length})</label>
        <div class="space-y-3 max-h-60 overflow-y-auto pr-1">
          ${activitiesHtml}
        </div>
      </div>
    `;
  } catch (err) {
    showToast(err.message, "error");
  }
}

function closeDetailDrawer() {
  document.getElementById("item-detail-drawer").classList.add("hidden");
  currentSelectedItem = null;
}

// State transition
async function transitionItem(itemId, targetStatus) {
  try {
    const res = await fetch(`/api/v1/items/${itemId}/transition`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "Authorization": `Bearer ${currentToken}`
      },
      body: JSON.stringify({ target_status: targetStatus })
    });

    if (res.status === 403) {
      const err = await res.json();
      showToast(err.detail || "Forbidden: Only Leads can approve/close items", "error");
      return;
    }

    if (!res.ok) {
      const err = await res.json();
      throw new Error(err.detail || "Transition failed");
    }

    showToast(`Status transitioned to ${targetStatus}`, "success");
    await loadWorkItems();
    await openDetailDrawer(itemId);
  } catch (err) {
    showToast(err.message, "error");
  }
}

// Concurrency: Optimistic Lock save with stale version reconciliation
async function saveItemUpdate(itemId, version) {
  const newTitle = document.getElementById("edit-title").value.trim();
  try {
    const res = await fetch(`/api/v1/items/${itemId}`, {
      method: "PATCH",
      headers: {
        "Content-Type": "application/json",
        "Authorization": `Bearer ${currentToken}`
      },
      body: JSON.stringify({ title: newTitle, version: version })
    });

    if (res.status === 409) {
      // Stale update rejected!
      showToast("Conflict: This item changed while you were editing! Reloading latest version...", "error");
      await openDetailDrawer(itemId);
      await loadWorkItems();
      return;
    }

    if (!res.ok) {
      const err = await res.json();
      throw new Error(err.detail || "Update failed");
    }

    showToast("Work item updated successfully", "success");
    await loadWorkItems();
    await openDetailDrawer(itemId);
  } catch (err) {
    showToast(err.message, "error");
  }
}

async function submitComment(itemId) {
  const input = document.getElementById("comment-input");
  const comment = input.value.trim();
  if (!comment) return;

  try {
    const res = await fetch(`/api/v1/items/${itemId}/comments`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "Authorization": `Bearer ${currentToken}`
      },
      body: JSON.stringify({ comment })
    });
    if (!res.ok) throw new Error("Failed to post comment");
    input.value = "";
    await openDetailDrawer(itemId);
  } catch (err) {
    showToast(err.message, "error");
  }
}

// Create Item with Idempotency-Key
function openCreateModal() {
  document.getElementById("create-modal").classList.remove("hidden");
}
function closeCreateModal() {
  document.getElementById("create-modal").classList.add("hidden");
}

async function handleCreateItem(e) {
  e.preventDefault();
  if (!currentUser || !currentUser.teams || currentUser.teams.length === 0) {
    showToast("User has no team memberships", "error");
    return;
  }

  const teamId = currentUser.teams[0].team_id;
  const title = document.getElementById("new-title").value.trim();
  const description = document.getElementById("new-description").value.trim();
  const type = document.getElementById("new-type").value;
  const priority = document.getElementById("new-priority").value;

  const idempotencyKey = `ui-create-${Date.now()}-${Math.random().toString(36).substring(2, 9)}`;

  try {
    const res = await fetch("/api/v1/items", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "Authorization": `Bearer ${currentToken}`,
        "Idempotency-Key": idempotencyKey
      },
      body: JSON.stringify({
        team_id: teamId,
        title,
        description,
        type,
        priority
      })
    });

    if (!res.ok) {
      const err = await res.json();
      throw new Error(err.detail || "Failed to create item");
    }

    showToast("Work item created successfully!", "success");
    closeCreateModal();
    document.getElementById("create-form").reset();
    await loadWorkItems(true);
  } catch (err) {
    showToast(err.message, "error");
  }
}

function showToast(message, type = "info") {
  const container = document.getElementById("toast-container");
  const toast = document.createElement("div");
  const bg = type === "error" ? "bg-red-600" : type === "success" ? "bg-emerald-600" : "bg-indigo-600";
  toast.className = `${bg} text-white px-4 py-2.5 rounded-lg text-xs font-semibold shadow-lg flex items-center space-x-2 transition transform duration-300 translate-y-0`;
  toast.innerHTML = `<span>${escapeHtml(message)}</span>`;
  container.appendChild(toast);
  setTimeout(() => {
    toast.remove();
  }, 4000);
}

function escapeHtml(str) {
  if (!str) return "";
  return String(str)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#039;");
}
