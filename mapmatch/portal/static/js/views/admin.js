// Admin: users, sign-up, Valhalla and engine status.
import { html, useState, useEffect, api, toast, Icon, ago, full } from "../lib.js";
import { startJob } from "../session.js";

export function Admin() {
  const [a, setA] = useState(null);
  const load = () => api("/admin").then(setA).catch((e) => toast(e.message, "error"));
  useEffect(() => { load(); }, []);
  if (!a) return html`<div class="content muted">Loading…</div>`;
  const v = a.valhalla || {};
  async function signup(enabled) {
    try { await api("/admin/signup", { method: "POST", body: { enabled } }); load(); } catch (e) { toast(e.message, "error"); }
  }
  return html`<div class="content">
    <h1>Admin</h1>
    <p class="lead">Server status and accounts.</p>
    <div class="stack" style="gap:20px">
      <div class="grid2">
        <div class="card"><header><h2>Valhalla</h2></header><div class="body">
          ${v.error ? html`<div class="callout danger"><${Icon} name="alert" size="sm" />Unreachable: ${v.error}</div>`
            : html`<div class="row"><span class="badge ok"><${Icon} name="check" size="sm" />Online</span><span class="muted num">v${v.version}</span></div>
              ${v.tileset_last_modified && html`<p class="hint">Map data built ${full(new Date(v.tileset_last_modified * 1000).toISOString())}.</p>`}`}
        </div></div>
        <div class="card"><header><h2>Matcher</h2></header><div class="body">
          <div class="row"><span class="badge accent">engine ${a.engine}</span>${a.dry_run && html`<span class="badge warn">PUSH_DRY_RUN on</span>`}</div>
          <p class="hint">${a.outdated_trips ? `${a.outdated_trips} trips across all users were matched by an older engine.` : "Every trip uses the current engine."}</p>
          ${a.outdated_trips > 0 && html`<div><button class="sm" onClick=${() => startJob("rematch", { scope: "outdated" })}><${Icon} name="recalc" size="sm" />Rematch my outdated trips</button></div>`}
        </div></div>
      </div>
      <div class="card"><header><h2>Accounts</h2></header>
        <div class="body">
          <label class="switch"><input type="checkbox" checked=${a.signup} onChange=${(e) => signup(e.target.checked)} />Allow new people to create accounts</label>
          <div class="table-wrap"><table class="tbl"><thead><tr><th>User</th><th>Points</th><th>Trips</th><th>Automation</th><th>Last run</th></tr></thead><tbody>
            ${a.users.map((u) => html`<tr key=${u.id}><td><b>${u.username}</b>${u.is_admin ? html` <span class="badge">admin</span>` : ""}</td>
              <td class="num">${(u.points || 0).toLocaleString()}</td><td class="num">${u.trips}</td>
              <td>${u.auto_enabled ? html`<span class="badge ok">on</span>` : html`<span class="badge">off</span>`}</td>
              <td class="num">${ago(u.last_run)}${u.last_error ? html` <span class="badge danger" title=${u.last_error}>error</span>` : ""}</td></tr>`)}
          </tbody></table></div>
        </div></div>
    </div>
  </div>`;
}
