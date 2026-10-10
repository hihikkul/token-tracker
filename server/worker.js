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

const PERIODS = ['day', 'week', 'month'];
const CODE_ALPHABET = 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789'; // no 0/O/1/I
const MAX_MEMBERS = 50;

const json = (data, status = 200) =>
  new Response(JSON.stringify(data), {
    status,
    headers: { 'content-type': 'application/json; charset=utf-8', 'cache-control': 'no-store' },
  });
const err = (status, message) => json({ error: message }, status);

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
  if (count >= MAX_MEMBERS) return null;
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

    if (parts[1] !== 'groups' || parts.length !== 4) return err(404, 'not found');
    const code = cleanText(parts[2], 9).toUpperCase();
    const action = parts[3];
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
      const stmts = [env.DB.prepare('UPDATE members SET nick = ?, plans = ?, updated = ? WHERE id = ?').bind(nick, plans, now, me.id)];
      for (const p of PERIODS) {
        const s = body.periods[p];
        if (!s) continue;
        const api = cleanNum(s.api), sub = cleanNum(s.sub), win = cleanWindow(s.window);
        if (api === null || sub === null || !win) return err(400, `bad ${p}`);
        stmts.push(env.DB.prepare(
          'INSERT INTO scores (member_id, period, window, api, sub, updated) VALUES (?, ?, ?, ?, ?, ?) ' +
          'ON CONFLICT(member_id, period) DO UPDATE SET window = excluded.window, api = excluded.api, sub = excluded.sub, updated = excluded.updated',
        ).bind(me.id, p, win, api, sub, now));
      }
      await env.DB.batch(stmts);
      return json({ ok: true });
    }

    if (req.method === 'GET' && action === 'ranking') {
      const period = PERIODS.includes(url.searchParams.get('period')) ? url.searchParams.get('period') : 'week';
      const win = cleanWindow(url.searchParams.get('window'));
      const { results } = await env.DB.prepare(
        'SELECT m.id, m.nick, m.plans, m.updated, s.window, s.api, s.sub FROM members m ' +
        'LEFT JOIN scores s ON s.member_id = m.id AND s.period = ? WHERE m.group_code = ?',
      ).bind(period, code).all();
      const members = results.map((r) => {
        const current = r.window && (!win || r.window === win);
        const api = current ? r.api : 0;
        const sub = current ? r.sub : 0;
        return {
          nick: r.nick, plans: r.plans, me: r.id === me.id, updated: r.updated,
          api, sub, ratio: sub > 0 ? api / sub : null, current: !!current,
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
      if (!left) await env.DB.prepare('DELETE FROM groups WHERE code = ?').bind(code).run();
      return json({ ok: true });
    }

    return err(405, 'method not allowed');
  },
};
