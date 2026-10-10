import React, { useCallback, useEffect, useState } from "react";
import { api, getToken, setToken } from "./api.js";

const money = (v) => (v === undefined || v === null ? "" : `$${Number(v).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`);
let TZ = "America/Chicago"; // overwritten from /api/me so every time shown is in the business time zone
const when = (v) => (v ? new Date(v).toLocaleString("en-US", { timeZone: TZ, timeZoneName: "short" }) : "");

function useLoad(path, deps = []) {
  const [data, setData] = useState(null);
  const [error, setError] = useState("");
  const reload = useCallback(() => {
    api(path).then((d) => { setData(d); setError(""); }).catch((e) => setError(e.message));
  }, [path]);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(reload, [reload, ...deps]);
  return [data, error, reload];
}

function Err({ error }) { return error ? <p className="error" role="alert">{error}</p> : null; }

function Login({ onDone }) {
  const [t, setT] = useState("");
  return (
    <form className="card login" onSubmit={(e) => { e.preventDefault(); setToken(t.trim()); onDone(); }}>
      <h1>Jarvis</h1>
      <p className="muted">Paste your API token. It is kept for this browser tab only.</p>
      <input type="password" value={t} onChange={(e) => setT(e.target.value)} placeholder="bv_..." autoFocus />
      <button className="primary">Sign in</button>
    </form>
  );
}

function Summary() {
  const [d, error] = useLoad("/dashboard");
  const tiles = d && [["Pending approvals", d.pending_approvals], ["Hot leads", d.hot_leads], ["Open tasks", d.open_tasks],
    ["Scheduled sends", d.scheduled_messages], ["Active leads", d.total_leads]];
  return (<><Err error={error} /><div className="tiles">{tiles?.map(([k, v]) => <div className="tile" key={k}><b>{v}</b><span>{k}</span></div>)}</div></>);
}

function Approvals({ perms }) {
  const [rows, error, reload] = useLoad("/approvals?status=pending");
  const [msg, setMsg] = useState("");
  const decide = async (id, approve) => {
    const note = approve ? undefined : window.prompt("Reason for rejecting (optional)") || undefined;
    try { await api(`/approvals/${id}/decision`, { method: "POST", body: { approve, note } }); setMsg(""); } catch (e) { setMsg(e.message); }
    reload();
  };
  return (
    <section>
      <h2>Needs your approval</h2>
      <p className="muted">Nothing below happens until you approve it. You are approving exactly the text and figures shown.</p>
      <Err error={error || msg} />
      {rows?.length === 0 && <p className="muted">Nothing waiting.</p>}
      {rows?.map((a) => (
        <article className="card" key={a.id}>
          <header><span className={`badge ${a.kind}`}>{a.kind}</span> <strong>{a.summary}</strong><small>by {a.requested_by} · {when(a.created_at)}</small></header>
          {a.kind === "call" && <><p><b>Goal:</b> {a.payload.goal}</p><p className="muted">The AI will open the call with exactly this:</p><pre>{a.payload.opening}</pre></>}
          {a.payload.body && <pre>{a.payload.subject ? `Subject: ${a.payload.subject}\n\n` : ""}{a.payload.body}</pre>}
          {a.payload.items && a.payload.items.map((i) => <details key={i.message_id}><summary>To {i.to}</summary><pre>{i.body}</pre></details>)}
          {a.kind === "offer" && <p>Offer amount: <b>{money(a.payload.amount)}</b> (analysis #{a.payload.analysis_id})</p>}
          {["contract", "payment", "destructive"].includes(a.kind) && <pre>{JSON.stringify(a.payload, null, 2)}</pre>}
          {perms.includes(`approve:${a.kind}`)
            ? <div className="row"><button className="primary" onClick={() => decide(a.id, true)}>Approve</button><button onClick={() => decide(a.id, false)}>Reject</button></div>
            : <p className="muted">Your role cannot decide {a.kind} approvals.</p>}
        </article>
      ))}
    </section>
  );
}

function Leads() {
  const [rows, error, reload] = useLoad("/leads?limit=200");
  const [open, setOpen] = useState(null);
  const [msgs, setMsgs] = useState([]);
  const [info, setInfo] = useState("");
  const show = async (l) => { setOpen(l); setInfo(""); setMsgs(await api(`/leads/${l.id}/messages`).catch(() => [])); };
  const run = async (path, label) => {
    try { const r = await api(path, { method: "POST" }); setInfo(`${label}: ${r.draft_message_id ? "draft queued for approval" : r.note || "done"}`); } catch (e) { setInfo(e.message); }
  };
  const upload = async (e) => {
    const f = e.target.files[0]; if (!f) return;
    const form = new FormData(); form.append("file", f);
    try { const r = await api("/leads/import-csv", { method: "POST", form }); setInfo(`Imported ${r.created}, duplicates ${r.duplicates}, rejected ${r.rejected}`); reload(); } catch (er) { setInfo(er.message); }
  };
  return (
    <section>
      <h2>Leads</h2>
      <label className="file">Import CSV <input type="file" accept=".csv" onChange={upload} /></label>
      <Err error={error} />{info && <p className="info">{info}</p>}
      <div className="table-wrap"><table>
        <thead><tr><th>Score</th><th>Pri</th><th>Status</th><th>Property</th><th>Seller</th><th>Next action</th></tr></thead>
        <tbody>{rows?.map((l) => (
          <tr key={l.id} onClick={() => show(l)} className={open?.id === l.id ? "sel" : ""}>
            <td><span className={`score s${l.motivation_score >= 70 ? "hot" : l.motivation_score >= 40 ? "warm" : "cold"}`}>{l.motivation_score}</span></td>
            <td>P{l.priority}</td><td>{l.status}</td>
            <td>{l.property ? `${l.property.street}, ${l.property.city || ""} ${l.property.state || ""}` : "no address"}</td>
            <td>{l.seller?.name || l.seller?.phone || ""}</td><td>{l.next_action}</td>
          </tr>))}</tbody>
      </table></div>
      {open && (
        <aside className="card">
          <h3>Lead #{open.id}</h3>
          <p><b>Why this score:</b> {open.score_reasons.map((r) => `${r.signal} (${r.points > 0 ? "+" : ""}${r.points})`).join(", ") || "no signals yet"}</p>
          <p><b>Discovery:</b> {Object.entries(open.discovery).map(([k, v]) => `${k}: ${v}`).join(" · ") || "none yet"}</p>
          <div className="row">
            <button onClick={() => run(`/leads/${open.id}/agent/outreach`, "Outreach")}>Draft first message (AI)</button>
            <button onClick={() => run(`/leads/${open.id}/agent/reply`, "Reply")}>Draft reply (AI)</button>
          </div>
          <h4>Conversation</h4>
          {msgs.length === 0 && <p className="muted">No messages.</p>}
          {msgs.map((m) => <div key={m.id} className={`bubble ${m.direction}`}><small>{m.direction} · {m.channel} · {m.status}{m.scheduled_for ? ` · sends ${when(m.scheduled_for)}` : ""}</small><p>{m.body}</p></div>)}
        </aside>
      )}
    </section>
  );
}

function Tasks() {
  const [rows, error, reload] = useLoad("/tasks");
  const done = async (id) => { await api(`/tasks/${id}/complete`, { method: "POST" }).catch(() => {}); reload(); };
  return (
    <section><h2>Tasks</h2><Err error={error} />
      {rows?.length === 0 && <p className="muted">All clear.</p>}
      {rows?.map((t) => (
        <div className="card row between" key={t.id}>
          <div><span className={`badge p${t.priority}`}>P{t.priority}</span> {t.title}<small> lead #{t.lead_id} · due {when(t.due_at)}</small></div>
          <button onClick={() => done(t.id)}>Done</button>
        </div>))}
    </section>
  );
}

const FIELDS = [["address", "Address", "text"], ["arv", "ARV (your estimate)", "number"], ["repair_estimate", "Repair estimate", "number"],
  ["closing_costs", "Closing costs", "number"], ["assignment_fee", "Desired assignment fee", "number"], ["discount_factor", "Discount factor (blank = default)", "number"],
  ["buyer_max_price", "Buyer max price (optional)", "number"]];

function Deals() {
  const [f, setF] = useState({});
  const [res, setRes] = useState(null);
  const [error, setError] = useState("");
  const submit = async (e) => {
    e.preventDefault(); setError("");
    const body = { address: f.address, arv: f.arv, repair_estimate: f.repair_estimate, closing_costs: f.closing_costs, assignment_fee: f.assignment_fee };
    if (f.discount_factor) body.discount_factor = f.discount_factor;
    if (f.buyer_max_price) body.buyer_requirements = { max_price: f.buyer_max_price };
    try { setRes(await api("/deals/analyze", { method: "POST", body })); } catch (er) { setError(er.message); setRes(null); }
  };
  return (
    <section><h2>Deal analysis</h2>
      <p className="muted">MAO = ARV × discount factor − repairs − closing costs − assignment fee. Every input is yours; nothing is looked up or guessed.</p>
      <form className="card grid" onSubmit={submit}>
        {FIELDS.map(([k, label, type]) => <label key={k}>{label}<input type={type} step="any" required={k !== "discount_factor" && k !== "buyer_max_price"} value={f[k] || ""} onChange={(e) => setF({ ...f, [k]: e.target.value })} /></label>)}
        <button className="primary">Calculate</button>
      </form>
      <Err error={error} />
      {res && (<div className="card">
        <table><tbody>{res.result.breakdown.map((b) => <tr key={b.label}><td>{b.label}</td><td className="num">{b.amount}</td></tr>)}</tbody></table>
        {res.result.warnings.map((w) => <p className="error" key={w}>{w}</p>)}
        {res.result.buyer_check && <p>Buyer price {res.result.buyer_check.buyer_price}: {res.result.buyer_check.all_met ? "meets" : "does NOT meet"} buyer requirements.</p>}
        <h4>Assumptions</h4><ul>{res.result.assumptions.map((a) => <li key={a}>{a}</li>)}</ul>
        <h4>If inputs change (arithmetic only)</h4><ul>{res.result.sensitivity.map((s) => <li key={s.scenario}>{s.scenario}: {s.mao}</li>)}</ul>
        <p className="muted">{res.result.disclaimer}</p>
      </div>)}
    </section>
  );
}

function Buyers() {
  const [rows, error, reload] = useLoad("/buyers");
  const [f, setF] = useState({ name: "", email: "", phone: "", states: "", max_price: "" });
  const [msg, setMsg] = useState("");
  const add = async (e) => {
    e.preventDefault();
    const criteria = {};
    if (f.states) criteria.states = f.states.split(",").map((s) => s.trim());
    if (f.max_price) criteria.max_price = f.max_price;
    try { await api("/buyers", { method: "POST", body: { name: f.name, email: f.email || null, phone: f.phone || null, criteria } }); setMsg("Added. Approve and verify proof of funds before they can receive deals."); setF({ name: "", email: "", phone: "", states: "", max_price: "" }); reload(); } catch (er) { setMsg(er.message); }
  };
  const toggle = async (b, key) => {
    const field = key === "approved" ? "approved" : "proof_of_funds_verified";
    try { await api(`/buyers/${b.id}/status`, { method: "PATCH", body: { [field]: !b[field] } }); setMsg(""); } catch (e) { setMsg(e.message); }
    reload();
  };
  return (
    <section><h2>Cash buyers</h2><Err error={error} />
      <form className="card grid" onSubmit={add}>
        {["name", "email", "phone", "states", "max_price"].map((k) => <label key={k}>{k.replace("_", " ")}<input value={f[k]} required={k === "name"} onChange={(e) => setF({ ...f, [k]: e.target.value })} /></label>)}
        <button className="primary">Add buyer</button>
      </form>
      {msg && <p className="info">{msg}</p>}
      <div className="table-wrap"><table><thead><tr><th>Name</th><th>Contact</th><th>Criteria</th><th>Approved</th><th>Funds verified</th></tr></thead>
        <tbody>{rows?.map((b) => <tr key={b.id}><td>{b.name}</td><td>{b.email || b.phone}</td><td>{JSON.stringify(b.criteria)}</td>
          <td><button onClick={() => toggle(b, "approved")}>{b.approved ? "Yes" : "No"}</button></td>
          <td><button onClick={() => toggle(b, "pof")}>{b.proof_of_funds_verified ? "Yes" : "No"}</button></td></tr>)}</tbody></table></div>
    </section>
  );
}

function Activity() {
  const [rows, error] = useLoad("/activity?limit=200");
  return (
    <section><h2>Activity log</h2><p className="muted">Every agent decision, tool call, and approval, newest first.</p><Err error={error} />
      <div className="table-wrap"><table><thead><tr><th>When</th><th>Actor</th><th>Event</th><th>Details</th></tr></thead>
        <tbody>{rows?.map((a) => <tr key={a.id}><td>{when(a.at)}</td><td>{a.actor}</td><td>{a.event}</td><td><code>{JSON.stringify(a.data).slice(0, 200)}</code></td></tr>)}</tbody></table></div>
    </section>
  );
}

const TABS = { Approvals, Leads, Tasks, Deals, Buyers, Activity };

export default function App() {
  const [authed, setAuthed] = useState(Boolean(getToken()));
  const [tab, setTab] = useState("Approvals");
  const [me, setMe] = useState(null);
  useEffect(() => {
    if (!authed) return;
    api("/me").then((m) => { TZ = m.timezone || TZ; setMe(m); }).catch(() => { setToken(""); setAuthed(false); });
  }, [authed]);
  if (!authed) return <Login onDone={() => setAuthed(true)} />;
  const Page = TABS[tab];
  return (
    <div className="app">
      <header className="top"><b>Jarvis</b><span>{me ? `Good to see you, ${me.owner_name}.` : ""}</span><span className="muted">{me?.email} · {me?.role}</span>
        <button onClick={() => { setToken(""); setAuthed(false); }}>Sign out</button></header>
      <Summary />
      <nav>{Object.keys(TABS).map((t) => <button key={t} className={t === tab ? "active" : ""} onClick={() => setTab(t)}>{t}</button>)}</nav>
      <main>{me && <Page perms={me.permissions} />}</main>
    </div>
  );
}
