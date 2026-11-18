'use strict';
// Small HTTP endpoint (design.md conflict #4). Reads the Menagerie extension IPC socket
// at ROO_CODE_IPC_SOCKET_PATH and returns JSON {state, tasks}, or {state:"unknown"}.
// Untested against the real extension: the IPC framing is a best-effort assumption
// (JSON messages separated by \f or newlines).
const http = require('http');
const net = require('net');

const SOCKET = process.env.ROO_CODE_IPC_SOCKET_PATH || '/tmp/menagerie/ipc.sock';
const PORT = parseInt(process.env.MENAGERIE_STATUS_PORT || '8081', 10);
const READ_MS = 1500;

function probe() {
  return new Promise((resolve) => {
    const unknown = { state: 'unknown' };
    let buf = '';
    let done = false;
    const finish = (v) => {
      if (done) return;
      done = true;
      clearTimeout(timer);
      sock.destroy();
      resolve(v);
    };
    const sock = net.createConnection(SOCKET);
    const timer = setTimeout(() => finish(parse(buf)), READ_MS);
    sock.on('data', (d) => { buf += d.toString('utf8'); });
    sock.on('error', () => finish(unknown));
    sock.on('close', () => finish(buf ? parse(buf) : unknown));
  });
}

function parse(buf) {
  const tasks = new Map();
  let state = 'idle';
  for (const part of buf.split(/[\f\n]+/)) {
    if (!part.trim()) continue;
    let msg;
    try { msg = JSON.parse(part); } catch (_) { continue; }
    const data = (msg && msg.data) || msg || {};
    const ev = data.eventName || data.type;
    const id = data.taskId || (data.payload && data.payload[0]) || data.id;
    if (typeof ev === 'string' && id !== undefined) {
      tasks.set(String(id), { id: String(id), status: ev });
      if (/start|resum|running|message/i.test(ev)) state = 'running';
      else if (/complet|abort|finish/i.test(ev)) state = 'idle';
    }
  }
  return { state, tasks: Array.from(tasks.values()) };
}

http.createServer(async (req, res) => {
  const path = (req.url || '').split('?')[0];
  if (req.method !== 'GET' || (path !== '/' && path !== '/status')) {
    res.writeHead(404, { 'content-type': 'application/json' });
    return res.end('{"error":"not_found"}');
  }
  let body;
  try { body = await probe(); } catch (_) { body = { state: 'unknown' }; }
  res.writeHead(200, { 'content-type': 'application/json' });
  res.end(JSON.stringify(body));
}).listen(PORT, '0.0.0.0');
