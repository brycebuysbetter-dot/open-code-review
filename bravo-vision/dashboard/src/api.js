// The token lives in sessionStorage only: it is gone when the tab closes.
const KEY = "jarvis_token";

export const getToken = () => {
  try { return sessionStorage.getItem(KEY) || ""; } catch { return ""; }
};
export const setToken = (t) => {
  try { t ? sessionStorage.setItem(KEY, t) : sessionStorage.removeItem(KEY); } catch { /* storage unavailable */ }
};

export async function api(path, { method = "GET", body, form } = {}) {
  const headers = { Authorization: `Bearer ${getToken()}` };
  if (body) headers["Content-Type"] = "application/json";
  const res = await fetch(`/api${path}`, { method, headers, body: form || (body ? JSON.stringify(body) : undefined) });
  const data = res.headers.get("content-type")?.includes("json") ? await res.json() : null;
  if (!res.ok) {
    const d = data?.detail;
    const msg = typeof d === "string" ? d : d?.violations ? d.violations.join("; ") : JSON.stringify(d || res.statusText);
    const err = new Error(msg);
    err.status = res.status;
    throw err;
  }
  return data;
}
