// PeakCut front door for mypeakcut.dpdns.org
// Cloud Shell first (through the tunnel this domain already uses). If it is off, Render.
// A small label at the bottom-right of each page says which server you are on.

const RENDER = "https://peakcut-render.onrender.com";
const DOWN = new Set([502, 503, 504, 520, 521, 522, 523, 524, 525, 526, 527, 530]);
const MAX_BUFFER = 5_000_000;                                // bigger request bodies go to Cloud Shell only

export default {
  async fetch(request) {
    return app(request, new URL(request.url));
  },
};

function page(title, text, code = 503, refresh = 0) {
  const meta = refresh ? `<meta http-equiv="refresh" content="${refresh}">` : "";
  return new Response(
    `<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">${meta}<title>${title}</title>` +
      `<body style="font:16px system-ui;background:#161616;color:#eee;display:grid;place-items:center;min-height:100vh;margin:0;text-align:center">` +
      `<div style="max-width:420px;padding:24px"><h2 style="margin:0 0 12px">${title}</h2><p style="color:#bbb;line-height:1.5">${text}</p></div></body>`,
    { status: code, headers: { "content-type": "text/html; charset=utf-8", "cache-control": "no-store" } }
  );
}

async function app(request, url) {
  const hasBody = !["GET", "HEAD", "OPTIONS"].includes(request.method);
  const size = Number(request.headers.get("content-length") || 0);
  const canBuffer = !hasBody || size <= MAX_BUFFER;
  const body = hasBody && canBuffer ? await request.arrayBuffer() : null;

  // 1. Cloud Shell, through the tunnel
  let first = null;
  try {
    first = await fetch(canBuffer ? new Request(request.url, { method: request.method, headers: request.headers, body, redirect: "manual" })
                                  : new Request(request, { redirect: "manual" }));
  } catch (e) { /* the tunnel is down */ }
  if (first && !DOWN.has(first.status)) return decorate(first, "cloudshell", url);
  if (!canBuffer) return page("Cloud Shell is off", "This upload is too large to hold while Cloud Shell is off. Turn Cloud Shell on and try again.");

  // 2. Render
  let second = null;
  try {
    second = await fetch(new Request(RENDER + url.pathname + url.search, { method: request.method, headers: request.headers, body, redirect: "manual" }));
  } catch (e) { /* Render is not answering either */ }
  if (!second) return page("PeakCut is starting", "Cloud Shell is off and Render is not answering yet. This page refreshes by itself.", 503, 20);
  const type = second.headers.get("content-type") || "";
  if (type.startsWith("text/plain")) {                          // Render's own status line: it is not serving PeakCut yet
    const t = (await second.clone().text()).slice(0, 200);
    if (t.startsWith("renderposter:")) {
      return page("Render is taking over", "Cloud Shell is off. Render needs about a minute to take over, then PeakCut appears here. This page refreshes by itself.", 503, 15);
    }
  }
  return decorate(second, "render", url);
}

function decorate(resp, who, url) {
  const out = new Response(resp.body, resp);
  out.headers.set("X-Served-By", who);
  const loc = out.headers.get("location");
  if (loc && loc.startsWith(RENDER)) out.headers.set("location", url.origin + loc.slice(RENDER.length));
  const type = out.headers.get("content-type") || "";
  if (type.startsWith("text/html") && resp.status === 200 && !url.pathname.startsWith("/api/")) return injectBadge(out, who);
  return out;
}

async function injectBadge(resp, who) {
  let html = await resp.text();
  const on = who === "cloudshell";
  const tag = `<div style="position:fixed;right:10px;bottom:10px;z-index:2147483000;font:12px system-ui;background:#1e1e1ee6;color:#fff;` +
    `padding:5px 10px;border-radius:14px;pointer-events:none;box-shadow:0 1px 6px #0006"><span style="color:${on ? "#4cd964" : "#ff9f0a"}">&#9679;</span> ${on ? "Cloud Shell" : "Render"}</div>`;
  html = html.includes("</body>") ? html.replace("</body>", tag + "</body>") : html + tag;
  const headers = new Headers(resp.headers);
  headers.delete("content-length");
  headers.delete("content-encoding");
  headers.set("cache-control", "no-store");
  return new Response(html, { status: resp.status, statusText: resp.statusText, headers });
}
