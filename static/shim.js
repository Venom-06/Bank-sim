// Replaces the Claude "db"/"user" runtime with the Flask API (polling every 1s).
(() => {
  const api = (m, u, b) => fetch(u, { method: m, headers: { "Content-Type": "application/json" },
    body: b === undefined ? undefined : JSON.stringify(b) }).then(r => r.json());
  const sd = (id, data) => ({ id, exists: true, data: () => data });
  function Query(col, q = { where: [], orderBy: null, limit: 100 }) {
    return {
      where: (f, op, v) => Query(col, { ...q, where: [...q.where, [f, op, v]] }),
      orderBy: (f, d = "asc") => Query(col, { ...q, orderBy: [f, d] }),
      limit: n => Query(col, { ...q, limit: n }),
      get: async () => ({ docs: (await api("POST", "/api/query", { col, ...q })).map(x => sd(x.id, x.data)) }),
      add: async data => ({ id: (await api("POST", "/api/add/" + col, data)).id }),
      onSnapshot(cb, err) {
        let seen = null, stopped = false;
        const tick = async () => {
          if (stopped || document.hidden) return;
          try {
            const r = await api("POST", "/api/query", { col, ...q });
            const first = seen === null, prev = seen || {}, next = {}, changes = [];
            r.forEach(x => {
              const s = JSON.stringify(x.data); next[x.id] = s;
              if (!(x.id in prev)) changes.push({ type: "added", doc: sd(x.id, x.data) });
              else if (prev[x.id] !== s) changes.push({ type: "modified", doc: sd(x.id, x.data) });
            });
            Object.keys(prev).forEach(id => { if (!(id in next)) changes.push({ type: "removed", doc: sd(id, {}) }); });
            seen = next;
            if (first || changes.length) cb({ docs: r.map(x => sd(x.id, x.data)), docChanges: () => changes });
          } catch (e) { if (err) err(e); }
        };
        tick(); const t = setInterval(tick, 1000);
        return () => { stopped = true; clearInterval(t); };
      },
    };
  }
  const db = {
    collection: name => Query(name),
    doc: path => {
      const i = path.indexOf("/"), url = "/api/doc/" + path.slice(0, i) + "/" + path.slice(i + 1);
      return {
        get: async () => { const r = await api("GET", url); return { exists: r.exists, data: () => r.data }; },
        set: d => api("PUT", url, d), update: d => api("PATCH", url, d), delete: () => api("DELETE", url),
      };
    },
  };
  const user = { id: async () => (await api("GET", "/api/me")).id };
  window.claude = { use: async n => (n === "db" ? db : n === "user" ? user : null) };
})();
