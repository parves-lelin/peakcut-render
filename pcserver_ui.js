// ---- Settings > Server & cookies (added by patch_peakcut.py) ----
async function pcServerTab(body) {
  const st = S.pc = await api("/api/pc/server");
  const word = {serving: "Showing PeakCut now", "standing by": "Running, standing by", off: "Off", unknown: "Unknown"};
  const pill = v => `<span class="pill ${v === "serving" ? "ok" : v === "off" ? "warn" : ""}">${esc(word[v] || v)}</span>`;
  const pick = (k, l) => `<button class="btn ${st.prefer === k ? "primary" : ""}" onclick="pcPickServer('${k}')">${l}</button>`;
  const c = st.cookies || {};
  const n = Math.round(c.age_days || 0);
  const when = c.age_days == null ? "" : c.age_days < 1 ? "today" : `${n} day${n === 1 ? "" : "s"} ago`;
  const verdict = c.accepted === true ? `<span class="pill ok">${ic("check")}YouTube accepted them</span>`
    : c.accepted === false ? `<span class="pill bad">YouTube did not accept them</span>` : "";
  body.innerHTML = `<section class="set-sec"><h2>Server</h2>
      <p class="help">Cloud Shell and Render keep the same data and share this address. Only one of them shows PeakCut at a time. <b>Auto</b> uses Cloud Shell whenever it is on, and Render when it is off.</p>
      ${st.error ? `<div class="note" style="margin:0 0 14px">${ic("warn")}<span>${esc(st.error)}</span></div>` : ""}
      <div class="appcard"><div class="apptx"><b>Cloud Shell</b><span>Records, transcribes and cuts videos</span></div><div class="appact">${pill(st.cloudshell)}</div></div>
      <div class="appcard" style="margin-top:10px"><div class="apptx"><b>Render</b><span>Always on. Shows PeakCut and posts while Cloud Shell is off</span></div><div class="appact">${pill(st.render)}</div></div>
      <p class="sub" style="margin:16px 0 8px">Show me</p>
      <div class="row">${pick("auto", "Auto")}${pick("cloudshell", "Cloud Shell")}${pick("render", "Render")}</div>
      <p class="sub" id="pc_srv_msg" style="margin-top:10px">${st.prefer === "render" ? "Render is chosen. If Cloud Shell is on, it steps aside and saves everything first." : st.prefer === "cloudshell" ? "Cloud Shell is chosen. It must be on: if it is off, Render keeps showing PeakCut. Type clip in Cloud Shell to start it." : "Auto: Cloud Shell when it is on, otherwise Render."}</p></section>
    <section class="set-sec"><h2>YouTube cookies</h2>
      <p class="help">Downloads from YouTube need your cookies. They are saved in your shared database, so Cloud Shell and Render both use them. A new upload replaces the old one.</p>
      <div class="ai-state">${c.present ? `<span class="pill ok">${ic("check")}Saved</span><span class="sub">${c.count} cookies · ${esc(when)}${c.saved_on ? " · from " + (c.saved_on === "render" ? "Render" : "Cloud Shell") : ""}</span>${verdict}` : `<span class="pill warn">None saved</span>`}</div>
      <div class="ai-form" style="margin-top:14px">
        <label class="f">Cookies file<input id="pc_file" type="file" accept=".txt,text/plain"></label>
        <label class="f">Or paste them<textarea id="pc_text" rows="5" spellcheck="false" placeholder="# Netscape HTTP Cookie File"></textarea></label>
      </div>
      <div class="row" style="margin-top:14px"><button class="btn primary" onclick="pcSaveCookies()">${c.present ? "Replace cookies" : "Save cookies"}</button>${c.present ? `<button class="btn" onclick="pcRemoveCookies()">Remove</button>` : ""}</div>
      <p class="sub" id="pc_msg" style="margin-top:10px"></p></section>`;
}
async function pcPickServer(k) {
  try {
    S.pc = await send("/api/pc/server", {prefer: k});
    toast(k === "render" ? "Switching to Render. This page will go away for about a minute." : k === "cloudshell" ? "Cloud Shell chosen." : "Auto chosen.");
    drawSettings();
  } catch (e) { toast(e.message, "bad"); }
}
async function pcSaveCookies() {
  const msg = $("#pc_msg");
  let text = ($("#pc_text") || {}).value || "";
  const f = ($("#pc_file") || {}).files && $("#pc_file").files[0];
  if (f) text = await f.text();
  if (!text.trim()) { msg.textContent = "Choose a cookies file, or paste the text, first."; return; }
  msg.textContent = "Saving, then testing with YouTube. This can take up to a minute…";
  try {
    const r = await send("/api/pc/cookies", {text});
    toast(r.message, r.accepted === false ? "bad" : "");
    await drawSettings();
    const m = $("#pc_msg"); if (m) m.textContent = r.message;
  } catch (e) { msg.textContent = e.message; toast(e.message, "bad"); }
}
async function pcRemoveCookies() {
  if (!await ask("Remove the cookies?", "Downloads from YouTube will stop using them, on both servers.", "Remove", true)) return;
  try { await send("/api/pc/cookies", {}, "DELETE"); toast("Cookies removed."); drawSettings(); } catch (e) { toast(e.message, "bad"); }
}
// ---- end of Server & cookies ----
