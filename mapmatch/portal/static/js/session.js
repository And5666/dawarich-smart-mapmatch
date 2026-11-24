// Session and background-job helpers shared by the shell and the views.
import { api, store, toast, refreshData } from "./lib.js";

export async function loadMe() {
  try { store.set({ me: await api("/me") }); }
  catch (e) { store.set({ me: e.status === 401 ? null : store.get().me ?? null }); if (e.status !== 401) toast(e.message, "error"); }
}

let pollTimer = null;
export async function watchJobs() {
  clearTimeout(pollTimer);
  try {
    const { jobs } = await api("/jobs");
    const active = jobs.find((j) => j.status === "running" || j.status === "queued") || null;
    const prev = store.get().job;
    store.set({ job: active, lastJob: jobs[0] || null });
    if (prev && !active) {
      const done = jobs.find((j) => j.id === prev.id);
      if (done?.status === "done") toast(done.message ? `Done: ${done.message}` : "Done");
      else if (done?.status === "failed") toast(`Failed: ${done.error}`, "error");
      refreshData();
      loadMe();
    }
    pollTimer = setTimeout(watchJobs, active ? 1500 : 30000);
  } catch (e) {
    if (e.status !== 401) pollTimer = setTimeout(watchJobs, 10000);
  }
}
export async function startJob(kind, params = {}) {
  try {
    await api("/jobs", { method: "POST", body: { kind, params } });
    watchJobs();
  } catch (e) { toast(e.message, "error"); }
}

