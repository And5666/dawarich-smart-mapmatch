// Match: what gets snapped to the map. Save the choices (used by every match
// from now on, including automatic runs), or try them once on some trips.
import { html, useState, useEffect, api, useStore, store, toast, Icon, MODES, modeVar, ago, plural, Seg } from "../lib.js";
import { loadMe, startJob } from "../session.js";

const MOVING = ["walk", "cycle", "drive", "train"];
const PRESETS = [["Everything", MOVING], ["Bikes only", ["cycle"]], ["Cars only", ["drive"]], ["Walks only", ["walk"]], ["Nothing", []]];
const CHOICE_KEYS = ["match_modes", "detailed", "strength"];
const AUTO_KEYS = ["auto_enabled", "interval_min", "auto_push", "push_review"];
const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);
const differs = (f, s, keys) => keys.some((k) => !same(f[k], s[k]) && String(f[k]) !== String(s[k]));
const toTs = (d, end) => (d ? Math.floor(Date.parse(d) / 1000) + (end ? 86399 : 0) : null);

export function Match() {
  const me = useStore((s) => s.me);
  const job = useStore((s) => s.job);
  const version = useStore((s) => s.dataVersion);
  const s = me?.settings || {};
  const [f, setF] = useState(null);
  const [saving, setSaving] = useState("");
  const [scope, setScope] = useState("new");
  const [range, setRange] = useState({ from: "", to: "" });
  const [pushAfter, setPushAfter] = useState(false);
  const [sys, setSys] = useState(null);
  useEffect(() => { if (me && !f) setF({ ...s }); }, [me]);
  useEffect(() => { api("/system").then(setSys).catch(() => {}); }, [version]);
  if (!f) return html`<div class="doc faint">Loading…</div>`;

  const set = (patch) => setF({ ...f, ...patch });
  const upd = (k) => (e) => set({ [k]: e.target.type === "checkbox" ? e.target.checked : e.target.value });
  const modes = f.match_modes || [];
  const toggleMode = (m) => set({ match_modes: MOVING.filter((x) => (x === m ? !modes.includes(x) : modes.includes(x))) });
  const running = !!job;
  const choicesDirty = differs(f, s, CHOICE_KEYS);

  async function save(section, keys) {
    setSaving(section);
    try {
      const body = {};
      for (const k of keys) body[k] = f[k];
      if ("interval_min" in body) body.interval_min = Number(body.interval_min);
      await api("/settings", { method: "PUT", body });
      await loadMe();
      setF({ ...f, ...Object.fromEntries(keys.map((k) => [k, store.get().me.settings[k]])) });
      toast("Saved");
    } catch (e) { toast(e.message, "bad"); }
    finally { setSaving(""); }
  }

  async function runOnce() {
    const params = { scope, once: { match_modes: modes, detailed: f.detailed, strength: f.strength }, push: pushAfter };
    if (scope === "range") Object.assign(params, { start: toTs(range.from), end: toTs(range.to, true) });
    if (await startJob("rematch", params)) toast("Matching; progress is in the top bar");
  }

  const summary = !modes.length ? "Nothing is snapped to the map: every trip keeps its GPS as recorded."
    : modes.length === MOVING.length ? "Every walk, ride, drive and train journey is snapped to the map."
    : `Only ${modes.map((m) => MODES[m].label.toLowerCase()).join(", ")} is snapped to the map. Everything else keeps its GPS as recorded.`;
  const rangeOk = scope !== "range" || range.from || range.to;

  return html`<div class="doc">
    <h1>Match</h1>
    <p class="lede">Choose what mapmatch snaps to roads, paths and railways, and what it leaves exactly as you recorded it.</p>

    ${sys?.outdated_trips > 0 && html`<div class="callout">
      <${Icon} name="info" />
      <div class="grow"><b>${plural(sys.outdated_trips, "trip")} ${sys.outdated_trips === 1 ? "was" : "were"} matched by an older version of mapmatch.</b>
        <div class="help">Matching them again picks up the improvements. Your choices on each trip are kept.</div></div>
      <button class="go" disabled=${running} onClick=${() => startJob("rematch", { scope: "outdated" }).then((ok) => ok && toast("Matching them again"))}>Match them again</button>
    </div>`}

    <section>
      <header><h2>What to snap to the map</h2><p>${summary}</p></header>
      <div class="body">
        <div class="modepick" role="group" aria-label="Modes to snap to the map">
          ${MOVING.map((m) => html`<label class=${`modechip ${modes.includes(m) ? "on" : ""}`} style=${modeVar(m)}>
            <input type="checkbox" checked=${modes.includes(m)} onChange=${() => toggleMode(m)} />
            <${Icon} name=${m} /><span>${MODES[m].label}</span>
            <span class="state">${modes.includes(m) ? "Snapped" : "GPS kept"}</span></label>`)}
        </div>
        <div class="row wrap" style="gap:6px">
          <span class="small dim">Quick picks</span>
          ${PRESETS.map(([label, ms]) => html`<button class=${`sm ${same(modes, ms) ? "go" : ""}`} onClick=${() => set({ match_modes: ms })}>${label}</button>`)}
        </div>
        <p class="help">A part in a mode you leave off keeps its GPS point for point, and a trip made only of those modes is never changed in Dawarich.
          Set a part's mode by hand on its trip page and it's matched anyway.</p>
      </div>
    </section>

    <section>
      <header><h2>Detailed GPS</h2><p>From a watch or bike computer: a point every second or two, already a cleaner line than any map.</p></header>
      <div class="body">
        <${Seg} value=${f.detailed} onChange=${(v) => set({ detailed: v })}
          options=${[["keep", "Leave it as recorded"], ["snap", "Snap it to the map too"]]} />
        <p class="help">${f.detailed === "keep"
          ? "Detailed stretches keep every point, and a trip recorded entirely in detail is never changed in Dawarich."
          : "Detailed stretches are matched like any other GPS, so they follow the road or path on the map."}${" "}Each trip can choose differently on its own page.</p>
      </div>
    </section>

    <section>
      <header><h2>Gaps in the GPS</h2><p>Tunnels, flat batteries, a phone deep in a bag.</p></header>
      <div class="body">
        <${Seg} value=${f.strength} onChange=${(v) => set({ strength: v })}
          options=${[["fill", "Route across the gap"], ["strict", "Draw a straight line"]]} />
        <p class="help">${f.strength === "fill"
          ? "The missing stretch follows the road or path when the time it took fits; otherwise it stays a straight line."
          : "Gaps are never routed: the line runs straight from the last point before the gap to the first after it."}${" "}Each trip can choose differently in its Gaps tab, gap by gap.</p>
      </div>
    </section>

    <div class="applygrid">
      <div class=${`applycard ${choicesDirty ? "hot" : ""}`}>
        <div class="row"><${Icon} name="check" /><h2>Save as my settings</h2></div>
        <p class="help">Used from now on by automatic runs, the Match buttons on the Trips page, and whenever you match a trip.
          Trips already matched keep their match until they're matched again.</p>
        <div class="actions">
          <button class="go" disabled=${saving === "choices" || !choicesDirty} onClick=${() => save("choices", CHOICE_KEYS)}>${choicesDirty ? "Save" : "Saved"}</button>
          <button disabled=${running || choicesDirty} title=${choicesDirty ? "Save first" : ""}
            onClick=${() => startJob("rematch", { scope: "all" }).then((ok) => ok && toast("Matching every trip again"))}>Match every trip again</button>
        </div>
      </div>
      <div class="applycard">
        <div class="row"><${Icon} name="sync" /><h2>Try them once</h2></div>
        <p class="help">Matches some trips with the choices above without saving them. Locked trips are skipped.</p>
        <div class="row wrap" style="gap:8px">
          <select aria-label="Which trips" value=${scope} onChange=${(e) => setScope(e.target.value)}>
            <option value="new">Trips not matched yet</option>
            <option value="range">Trips between two dates</option>
            <option value="all">Every trip</option>
          </select>
          ${scope === "range" && html`<div class="row" style="gap:6px">
            <input type="date" aria-label="From" value=${range.from} onInput=${(e) => setRange({ ...range, from: e.target.value })} />
            <span class="faint">to</span>
            <input type="date" aria-label="To" value=${range.to} onInput=${(e) => setRange({ ...range, to: e.target.value })} /></div>`}
        </div>
        <label class="check"><input type="checkbox" checked=${pushAfter} disabled=${!me.dawarich_linked} onChange=${(e) => setPushAfter(e.target.checked)} />
          Push them to Dawarich afterwards${me.dry_run ? html` <span class="faint">(dry run)</span>` : ""}</label>
        <div class="actions"><button class="go" disabled=${running || !rangeOk} onClick=${runOnce}>Match now</button></div>
      </div>
    </div>

    <section>
      <header><h2>Automatic runs</h2><p>Off until you turn them on. They use your saved settings above.</p></header>
      <div class="body">
        <label class="check"><input type="checkbox" class="switch" checked=${f.auto_enabled} onChange=${upd("auto_enabled")} />
          Download and match new trips every
          <input type="number" min="15" max="10080" style="width:84px;height:30px" value=${f.interval_min} onInput=${upd("interval_min")}
            disabled=${!f.auto_enabled} aria-label="Minutes between runs" /> minutes</label>
        <label class="check"><input type="checkbox" class="switch" checked=${f.auto_push} onChange=${upd("auto_push")} />Push finished trips to Dawarich too</label>
        ${f.auto_push && html`<label class="check sub"><input type="checkbox" checked=${f.push_review} onChange=${upd("push_review")} />
          Include trips that need a look</label>`}
        <p class="help">${f.auto_push ? `Each finished trip's points in Dawarich are swapped for its matched route${f.push_review ? "" : ", except trips that need a look: those wait for you"}. Trips kept as recorded and locked trips are left alone, and your original GPS stays here, so any trip can be put back.`
          : "Matched trips wait here until you push them."}</p>
        ${s.last_error && html`<div class="note bad">The last run failed: ${s.last_error}</div>`}
        <div class="actions"><button class="go" disabled=${saving === "auto" || !differs(f, s, AUTO_KEYS)} onClick=${() => save("auto", AUTO_KEYS)}>Save</button>
          <span class="help">Last run ${ago(s.last_run)}. Which dates are downloaded is under <a href="#/settings">Settings</a>.</span></div>
      </div>
    </section>
  </div>`;
}
