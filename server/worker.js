// token-tracker ranking server: a Cloudflare Worker backed by D1.
//
// Each tracker posts only nickname, plan names and three numbers per period
// (API-equivalent spend, subscription share, window). No prompts, projects or models.
//
//   POST   /v1/groups                     {group_name, nick}          -> {code, member_id, token}
//   POST   /v1/groups/:code/join          {nick}                      -> {code, name, member_id, token}
//   PUT    /v1/groups/:code/score         {nick, plans, periods}      (Bearer token)
//   GET    /v1/groups/:code/ranking?period=day|week|month&window=...  (Bearer token)
//   DELETE /v1/groups/:code/me                                         (Bearer token)
//
// Public ranking (opt-in, ordered by API-equivalent usage / subscription):
//   POST   /v1/global/join                {nick}                      -> {member_id, token}
//   PUT    /v1/global/score               same body as a group score  (Bearer token)
//   GET    /v1/global/ranking?period=...&window=...  -> top 100, your place, participant count
//   DELETE /v1/global/me                                               (Bearer token)

const PERIODS = ['day', 'week', 'month'];
const CODE_ALPHABET = 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789'; // no 0/O/1/I
const MAX_MEMBERS = 50;
const GLOBAL = 'GLOBAL';   // the public ranking's built-in group (no member limit)
const TOP = 100;

const json = (data, status = 200) =>
  new Response(JSON.stringify(data), {
    status,
    headers: { 'content-type': 'application/json; charset=utf-8', 'cache-control': 'no-store' },
  });
const err = (status, message) => json({ error: message }, status);

function invitePage(url) {
  const value = (url.searchParams.get('invite') || '').toUpperCase();
  const code = /^[A-Z2-9]{4}-[A-Z2-9]{4}$/.test(value) ? value : '';
  return new Response(`<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
    <title>Token Tracker Invitation</title><style>body{font:18px Calibri,Arial,sans-serif;background:#141416;color:#f4f4f5;margin:0;padding:8vh 24px}main{max-width:650px;margin:auto}a{color:#82b6ff}code{font-size:26px}li{margin:18px 0}p{line-height:1.5;color:#bbb}button{padding:10px 18px;cursor:pointer}</style>
    <main><h1>Token Tracker Ranking</h1><p>The shared ranking server is online on Cloudflare. Each person's usage dashboard runs privately on their own PC.</p>
    ${code ? `<h2>Your invite code</h2><code id="code">${code}</code> <button id="copy">Copy code</button>` : '<p>Ask your friend for a group invite code.</p>'}
    <ol><li>Download Token Tracker: <a href="https://github.com/hihikkul/token-tracker/archive/refs/heads/main.zip">Windows ZIP</a> or <a href="https://github.com/hihikkul/token-tracker/releases/latest">Mac DMG</a>. Windows needs Python 3.10 or later; extract the ZIP and run install.bat. Mac: open the DMG and copy the app to Applications.</li>
    <li>Start Token Tracker on <strong>your own PC</strong>, then open its dashboard.</li><li>Open <strong>Ranking → Join with invite code</strong> and enter ${code ? `<code>${code}</code>` : 'your invite code'} and a nickname.</li></ol>
    <p>A localhost or 127.0.0.1 link opens the dashboard on your own computer. It cannot open your friend's dashboard. If that address will not load, start Token Tracker first. If joining fails, update the app and check your internet connection.</p>
    </main><script>const button=document.getElementById('copy');if(button)button.onclick=async()=>{try{await navigator.clipboard.writeText(document.getElementById('code').textContent);button.textContent='Copied'}catch{button.textContent='Select and copy the code above'}};</script></html>`,
    {headers: {'content-type': 'text/html; charset=utf-8', 'cache-control': 'no-store'}});
}

function randomString(n, alphabet) {
  const bytes = crypto.getRandomValues(new Uint8Array(n));
  return Array.from(bytes, (b) => alphabet[b % alphabet.length]).join('');
}
const newCode = () => `${randomString(4, CODE_ALPHABET)}-${randomString(4, CODE_ALPHABET)}`;
const newId = () => randomString(16, 'abcdefghijklmnopqrstuvwxyz0123456789');
const newToken = () => randomString(40, 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789');

async function sha256(text) {
  const buf = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(text));
  return Array.from(new Uint8Array(buf), (b) => b.toString(16).padStart(2, '0')).join('');
}

function cleanText(v, max) {
  if (typeof v !== 'string') return '';
  return v.replace(/[\u0000-\u001f\u007f]/g, '').trim().slice(0, max);
}
const cleanNum = (v) => (typeof v === 'number' && Number.isFinite(v) && v >= 0 && v < 1e6 ? v : null);
const cleanCount = (v) => (typeof v === 'number' && Number.isFinite(v) && v >= 0 && v < 1e15 ? v : null);
const cleanWindow = (v) => (typeof v === 'string' && /^\d{4}-(W\d{2}|\d{2}(-\d{2})?)$/.test(v) ? v : null);

async function readJson(req) {
  try {
    const text = await req.text();
    if (text.length > 8192) return null;
    return JSON.parse(text || '{}');
  } catch {
    return null;
  }
}

async function auth(req, env, code) {
  const h = req.headers.get('authorization') || '';
  const token = h.startsWith('Bearer ') ? h.slice(7) : '';
  if (!token) return null;
  const hash = await sha256(token);
  return env.DB.prepare('SELECT * FROM members WHERE group_code = ? AND token_hash = ?').bind(code, hash).first();
}

async function join(env, code, nick) {
  const count = await env.DB.prepare('SELECT COUNT(*) AS n FROM members WHERE group_code = ?').bind(code).first('n');
  if (code !== GLOBAL && count >= MAX_MEMBERS) return null;
  const id = newId();
  const token = newToken();
  const now = Date.now();
  await env.DB.prepare('INSERT INTO members (id, group_code, token_hash, nick, joined) VALUES (?, ?, ?, ?, ?)')
    .bind(id, code, await sha256(token), nick, now).run();
  return { member_id: id, token };
}

export default {
  async fetch(req, env) {
    const url = new URL(req.url);
    const parts = url.pathname.split('/').filter(Boolean); // v1 groups :code action
    if (parts[0] !== 'v1') {
      if (url.pathname === '/' && req.method === 'GET' && (req.headers.get('accept') || '').includes('text/html')) return invitePage(url);
      return url.pathname === '/' ? json({ ok: true, service: 'token-tracker-rank' }) : err(404, 'not found');
    }

    // create a group (the creator joins it)
    if (req.method === 'POST' && parts.length === 2 && parts[1] === 'groups') {
      const body = await readJson(req);
      const name = cleanText(body?.group_name, 40) || 'token-tracker';
      const nick = cleanText(body?.nick, 24);
      if (!nick) return err(400, 'nick required');
      let code = newCode();
      for (let i = 0; i < 5; i++) {
        const taken = await env.DB.prepare('SELECT 1 FROM groups WHERE code = ?').bind(code).first();
        if (!taken) break;
        code = newCode();
      }
      await env.DB.prepare('INSERT INTO groups (code, name, created) VALUES (?, ?, ?)').bind(code, name, Date.now()).run();
      const m = await join(env, code, nick);
      return json({ code, name, ...m }, 201);
    }

    const isGlobal = parts[1] === 'global' && parts.length === 3;
    if (!isGlobal && (parts[1] !== 'groups' || parts.length !== 4)) return err(404, 'not found');
    const code = isGlobal ? GLOBAL : cleanText(parts[2], 9).toUpperCase();
    const action = isGlobal ? parts[2] : parts[3];
    if (!isGlobal && code === GLOBAL) return err(404, 'group not found');  // only through /v1/global
    if (isGlobal) await env.DB.prepare("INSERT OR IGNORE INTO groups (code, name, created) VALUES (?, 'Ranking', 0)").bind(GLOBAL).run();
    const group = await env.DB.prepare('SELECT * FROM groups WHERE code = ?').bind(code).first();
    if (!group) return err(404, 'group not found');

    if (req.method === 'POST' && action === 'join') {
      const body = await readJson(req);
      const nick = cleanText(body?.nick, 24);
      if (!nick) return err(400, 'nick required');
      const m = await join(env, code, nick);
      if (!m) return err(409, 'group is full');
      return json({ code, name: group.name, ...m }, 201);
    }

    const me = await auth(req, env, code);
    if (!me) return err(401, 'bad token');

    if (req.method === 'PUT' && action === 'score') {
      const body = await readJson(req);
      if (!body || typeof body.periods !== 'object') return err(400, 'periods required');
      const now = Date.now();
      const nick = cleanText(body.nick, 24) || me.nick;
      const plans = cleanText(body.plans, 80);
      const calculationVersion = body.calculation_version === 2 ? 2 : 1;
      const stmts = [env.DB.prepare('UPDATE members SET nick = ?, plans = ?, updated = ? WHERE id = ?').bind(nick, plans, now, me.id)];
      for (const p of PERIODS) {
        const s = body.periods[p];
        if (!s) continue;
        const api = cleanNum(s.api), sub = cleanNum(s.sub), win = cleanWindow(s.window);
        if (api === null || sub === null || !win) return err(400, `bad ${p}`);
        const usage = s.usage === undefined ? api : cleanNum(s.usage);
        const tokens = s.tokens === undefined ? 0 : cleanCount(s.tokens);
        if (usage === null || tokens === null) return err(400, `bad ${p}`);
        stmts.push(env.DB.prepare(
          'INSERT INTO scores (member_id, period, window, api, sub, updated, calculation_version, usage, tokens) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) ' +
          'ON CONFLICT(member_id, period) DO UPDATE SET window = excluded.window, api = excluded.api, sub = excluded.sub, updated = excluded.updated, ' +
          'calculation_version = excluded.calculation_version, usage = excluded.usage, tokens = excluded.tokens',
        ).bind(me.id, p, win, api, sub, now, calculationVersion, usage, Math.round(tokens)));
      }
      await env.DB.batch(stmts);
      return json({ ok: true });
    }

    if (isGlobal && req.method === 'GET' && action === 'ranking') {
      const period = PERIODS.includes(url.searchParams.get('period')) ? url.searchParams.get('period') : 'week';
      const win = cleanWindow(url.searchParams.get('window'));
      if (!win) return err(400, 'window required');
      // Full-period prices only, just like the group ranking. Unknown/free subscriptions
      // and zero API usage are not participants for this window.
      const eligible = 'FROM scores s JOIN members m ON m.id = s.member_id ' +
        'WHERE m.group_code = ? AND s.period = ? AND s.window = ? ' +
        'AND s.calculation_version = 2 AND s.api > 0 AND s.sub > 0';
      const { results } = await env.DB.prepare(
        'SELECT m.id, m.nick, m.plans, s.api, s.sub, (1.0 * s.api / s.sub) AS ratio, s.usage, s.tokens ' +
        eligible + ' ORDER BY ratio DESC, s.api DESC, m.id ASC LIMIT ?',
      ).bind(GLOBAL, period, win, TOP).all();
      const total = await env.DB.prepare('SELECT COUNT(*) AS n ' + eligible)
        .bind(GLOBAL, period, win).first('n');
      const mine = await env.DB.prepare('SELECT api, sub, usage, tokens, calculation_version FROM scores WHERE member_id = ? AND period = ? AND window = ?')
        .bind(me.id, period, win).first();
      const ratio = mine && mine.sub > 0 ? mine.api / mine.sub : null;
      let place = null;
      if (mine && mine.api > 0 && mine.sub > 0 && mine.calculation_version === 2) {
        place = 1 + await env.DB.prepare(
          'SELECT COUNT(*) AS n ' + eligible + ' AND ((1.0 * s.api / s.sub) > ? ' +
          'OR ((1.0 * s.api / s.sub) = ? AND s.api > ?) ' +
          'OR ((1.0 * s.api / s.sub) = ? AND s.api = ? AND m.id < ?))',
        ).bind(GLOBAL, period, win, ratio, ratio, mine.api, ratio, mine.api, me.id).first('n');
      }
      return json({
        period, window: win, total, place, ratio, api: mine ? mine.api : 0, sub: mine ? mine.sub : 0,
        usage: mine ? mine.usage : 0, tokens: mine ? mine.tokens : 0, plans: me.plans || '',
        no_sub: !!mine && mine.sub <= 0,
        members: results.map((r, i) => ({ rank: i + 1, nick: r.nick, plans: r.plans,
          api: r.api, sub: r.sub, ratio: r.ratio, usage: r.usage, tokens: r.tokens, me: r.id === me.id })),
      });
    }

    if (req.method === 'GET' && action === 'ranking') {
      const period = PERIODS.includes(url.searchParams.get('period')) ? url.searchParams.get('period') : 'week';
      const win = cleanWindow(url.searchParams.get('window'));
      const { results } = await env.DB.prepare(
        'SELECT m.id, m.nick, m.plans, m.updated, s.window, s.api, s.sub, s.calculation_version FROM members m ' +
        'LEFT JOIN scores s ON s.member_id = m.id AND s.period = ? WHERE m.group_code = ?',
      ).bind(period, code).all();
      const members = results.map((r) => {
        const current = r.window && (!win || r.window === win) && r.calculation_version === 2;
        const api = current ? r.api : 0;
        const sub = current ? r.sub : 0;
        return {
          nick: r.nick, plans: r.plans, me: r.id === me.id, updated: r.updated,
          api, sub, ratio: sub > 0 ? api / sub : null, current: !!current,
          needs_update: !!r.window && r.calculation_version !== 2,
        };
      });
      members.sort((a, b) => (b.ratio ?? -1) - (a.ratio ?? -1) || b.api - a.api);
      return json({ code, name: group.name, period, window: win, members });
    }

    if (req.method === 'DELETE' && action === 'me') {
      await env.DB.batch([
        env.DB.prepare('DELETE FROM scores WHERE member_id = ?').bind(me.id),
        env.DB.prepare('DELETE FROM members WHERE id = ?').bind(me.id),
      ]);
      const left = await env.DB.prepare('SELECT COUNT(*) AS n FROM members WHERE group_code = ?').bind(code).first('n');
      if (!left && code !== GLOBAL) await env.DB.prepare('DELETE FROM groups WHERE code = ?').bind(code).run();
      return json({ ok: true });
    }

    return err(405, 'method not allowed');
  },
};
