// Athena web app: static files plus a same-origin proxy to the backend API.
//
// The backend (`make serve`) sends no CORS headers, so the browser talks only to this
// server, which forwards /v1/* to ATHENA_API. Node built-ins only: no npm install.
//
//   node frontend/web/server.mjs                     http://127.0.0.1:5173
//   ATHENA_API=http://127.0.0.1:8765 PORT=5173 HOST=127.0.0.1 node frontend/web/server.mjs

import { createServer, request as httpRequest } from "node:http";
import { readFile, stat } from "node:fs/promises";
import { extname, join, normalize, sep } from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = join(fileURLToPath(new URL(".", import.meta.url)), "public");
const API = new URL(process.env.ATHENA_API ?? process.env.VIXY_API ?? "http://127.0.0.1:8765");
const PORT = Number(process.env.PORT ?? 5173);
const HOST = process.env.HOST ?? "127.0.0.1";
const TYPES = {
  ".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
  ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml", ".json": "application/json",
  ".png": "image/png", ".ico": "image/x-icon",
};

function proxy(req, res) {
  const upstream = httpRequest({
    hostname: API.hostname, port: API.port || 80, path: req.url, method: req.method,
    headers: { "content-type": "application/json", accept: "application/json",
               ...(req.headers["content-length"] ? { "content-length": req.headers["content-length"] } : {}) },
  }, (up) => {
    res.writeHead(up.statusCode ?? 502, { "content-type": up.headers["content-type"] ?? "application/json",
                                          "cache-control": "no-store" });
    up.pipe(res);
  });
  upstream.on("error", () => {
    res.writeHead(502, { "content-type": "application/json" });
    res.end(JSON.stringify({ schema: "vixy.error.v1",
      error: { code: "backend_unreachable", message: `no backend at ${API.origin}` } }));
  });
  req.pipe(upstream);
}

async function serveStatic(req, res) {
  const path = decodeURIComponent(new URL(req.url, "http://x").pathname);
  let file = normalize(join(ROOT, path));
  if (!file.startsWith(ROOT + sep) && file !== ROOT) {
    res.writeHead(403).end();
    return;
  }
  try {
    if ((await stat(file)).isDirectory()) file = join(file, "index.html");
  } catch {
    file = join(ROOT, "index.html");               // hash routing: unknown paths get the app
  }
  try {
    const body = await readFile(file);
    res.writeHead(200, { "content-type": TYPES[extname(file)] ?? "application/octet-stream",
                         "cache-control": "no-store" });
    res.end(body);
  } catch {
    res.writeHead(404).end("not found");
  }
}

createServer((req, res) => {
  if (req.url.startsWith("/v1/") || req.url === "/v1") proxy(req, res);
  else if (req.method === "GET" || req.method === "HEAD") serveStatic(req, res);
  else res.writeHead(405).end();
}).listen(PORT, HOST, () => {
  console.log(`Athena web app on http://${HOST}:${PORT}  (API proxied to ${API.origin}/v1)`);
});
