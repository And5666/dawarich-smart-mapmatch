// Linking an account to a Dawarich user. The key is pasted once and kept on the
// account; signing in is a password, never a key.
import { html, useState, useEffect, api, toast, Confirm } from "../lib.js";
import { loadMe, watchJobs } from "../session.js";

export function LinkDawarich({ onDone, intro = true }) {
  const [url, setUrl] = useState("");
  const [key, setKey] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");

  useEffect(() => {
    api("/me").then((m) => setUrl(m.settings?.dawarich_url || "")).catch(() => {});
  }, []);

  async function submit(e) {
    e.preventDefault();
    setBusy(true); setErr("");
    try {
      if (url.trim()) await api("/connect/base", { method: "POST", body: { dawarich_url: url.trim() } });
      const r = await api("/connect", { method: "POST", body: { api_key: key.trim() } });
      await loadMe(); watchJobs();
      toast(r.dawarich?.email ? `Connected as ${r.dawarich.email}` : "Connected to Dawarich");
      onDone?.();
    } catch (x) { setErr(x.message); }
    finally { setBusy(false); }
  }

  return html`<form onSubmit=${submit} class="link-form">
    ${intro && html`<p class="help">Paste the API key from your Dawarich account page. It is stored with this account and only used to read and write your own points.</p>`}
    <label class="field"><span>Dawarich address</span>
      <input value=${url} onInput=${(e) => setUrl(e.target.value)} autocomplete="off" spellcheck="false"
        placeholder="https://dawarich.example.com" /></label>
    <label class="field"><span>API key</span>
      <input value=${key} onInput=${(e) => setKey(e.target.value)} autocomplete="off" spellcheck="false" required
        placeholder="Paste your key" style="font-family: ui-monospace, SFMono-Regular, Menlo, monospace" /></label>
    ${err && html`<p class="err" role="alert">${err}</p>`}
    <div class="actions">
      <button class="go" type="submit" disabled=${busy || !key.trim()}>${busy ? "Checking…" : "Connect"}</button>
      <span class="help" style="align-self:center">You only have to do this once; the key is kept with your account.</span>
    </div>
  </form>`;
}

export function LinkedAccount({ settings }) {
  return html`<div class="row" style="flex-wrap:wrap;gap:10px">
    <span class="note good">Linked to ${settings.dawarich_url ? settings.dawarich_url.replace(/^https?:\/\//, "") : "Dawarich"}
      with key <b>${settings.api_key_hint}</b>.</span>
    <span class="grow"></span>
    <${Confirm} cls="sm danger" armedLabel="Remove the key?" onConfirm=${async () => {
      try { await api("/unlink", { method: "POST" }); await loadMe(); toast("Dawarich key removed"); }
      catch (e) { toast(e.message, "bad"); }
    }}>Unlink<//>
  </div>`;
}
