// The web player: plays «Моя волна» or a playlist from the server (/player/…) in the bar at the bottom, which stays
// while the pages change (kurai.js swaps only the page). Each track that gives way to the next is reported, played on or
// skipped; the wave is rebuilt from that on the server, and the player takes the new version's tracks as the next ones
// (asking after each report and every half a minute), without a gap. Buttons with data-play="wave" or a playlist's id
// start it (data-start: from that track of it, as in «История»), or pause and resume what they started. ♥ and 👎
// (data-rate, data-track) rate a track. The phone's lock screen and headphones control it too (Media Session).
(() => {
  const $ = (id) => document.getElementById(id);
  const bar = $("player"), audio = $("player-audio");
  if (!bar || !audio) return;
  const els = {cover: $("player-cover"), title: $("player-title"), artist: $("player-artist"), source: $("player-source"),
    toggle: $("player-toggle"), next: $("player-next"), seek: $("player-seek"), time: $("player-time"),
    duration: $("player-duration")};
  const say = (key, values = {}) => Object.entries(values).reduce((s, [k, v]) => s.replace(`{${k}}`, v), audio.dataset[key]);
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const post = (url, data, keepalive = false) => fetch(url, {method: "POST", keepalive, credentials: "same-origin",
    headers: {"X-CSRF-Token": document.body.dataset.csrf, "Content-Type": "application/json"},
    body: JSON.stringify(data)});
  const clock = (s) => `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, "0")}`;

  let source = null;  // {which: "wave" or a playlist's id, name, version}
  let tracks = [], index = 0;
  let played = 0, lastTime = 0;  // seconds the current track really played (seeking doesn't count)
  let failures = 0;
  const heard = new Set();  // this session's tracks: a new version of the wave doesn't bring them back
  const statuses = {};  // what the player says about each source (shown wherever the page has its place)
  let refreshing = null;
  const current = () => tracks[index];
  const isWave = () => source && source.which === "wave";

  const setStatus = (which, text) => {
    statuses[which] = text;
    document.querySelectorAll(`[data-player-status="${which}"]`).forEach((el) => { el.textContent = text; });
  };

  // the page's play buttons show what plays (the page may have been swapped since); one with data-start is a track
  // of its source («История»)
  const playing = (b) => !audio.paused && source && b.dataset.play === String(source.which)
    && (!b.dataset.start || (current() && current().id === b.dataset.start));
  const syncButtons = () => {
    document.querySelectorAll("[data-play]").forEach((b) => b.classList.toggle("playing", playing(b)));
    showRating();
    els.toggle.querySelector("i").textContent = audio.paused ? "play_arrow" : "pause";
    for (const [which, text] of Object.entries(statuses)) setStatus(which, text);
  };

  async function fetchQueue(which) {
    const response = await fetch(`/player/queue/${which}`, {credentials: "same-origin"});
    if (response.redirected) { location.assign(response.url); return null; }  // logged out
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || response.status);
    return data;
  }

  // a source's tracks; the wave may first have to be made (the server queues it): asked again until it's there
  async function load(which) {
    for (let i = 0; i < 40; i++) {
      const data = await fetchQueue(which);
      if (!data) return null;
      if (data.message) { setStatus(which, data.message); return null; }
      if (!data.pending) return data;
      setStatus(which, say("pending"));
      await sleep(3000);
    }
    return null;
  }

  // a source from its start, or from one of its tracks (`startId`); the one already playing pauses or resumes
  async function start(which, startId) {
    if (source && String(source.which) === which && (!startId || (current() && current().id === startId))) {
      if (audio.paused) audio.play(); else audio.pause();
      return;
    }
    report();
    try {
      const data = await load(which);
      if (!data || !data.tracks.length) return;
      source = {which, name: data.kind === "wave" ? say("wave") : data.name, version: data.version};
      tracks = which === "wave" ? data.tracks.filter((t) => !heard.has(t.id)) : data.tracks;
      if (!tracks.length) tracks = data.tracks;
      index = Math.max(0, startId ? tracks.findIndex((t) => t.id === startId) : 0);
      if (which === "wave") setStatus("wave", data.description || "");
      playCurrent();
    } catch (e) {
      setStatus(which, `${say("error")} ${e.message || e}`);
    }
  }

  function playCurrent() {
    const t = current();
    if (!t) return;
    heard.add(t.id);
    played = 0; lastTime = 0;
    audio.src = `/player/stream/${encodeURIComponent(t.id)}`;
    audio.play().catch(() => {});  // a refused autoplay leaves the bar paused, ready for a tap
    bar.hidden = false;
    els.cover.src = `/player/cover/${encodeURIComponent(t.id)}`;
    els.title.textContent = t.title;
    els.artist.textContent = t.artist;
    els.source.querySelector("span").textContent = source.name;
    els.source.querySelector("i").textContent = isWave() ? "waves" : "queue_music";
    els.duration.textContent = clock(t.duration || 0);
    bar.querySelectorAll("[data-rate]").forEach((b) => { b.dataset.track = t.id; });
    showRating();
    if (isWave()) setStatus("wave", say("now", {track: `${t.artist} — ${t.title}`}));
    if ("mediaSession" in navigator) {
      navigator.mediaSession.metadata = new MediaMetadata({title: t.title, artist: t.artist, album: t.album,
        artwork: [{src: els.cover.src, sizes: "600x600"}]});
    }
  }

  // the current track gave way (next, the end, another source, the page closed): played on or skipped, told to the
  // server, which counts the play and rebuilds the wave when it's due
  // a 👎 is a skip however long the track played
  function report(disliked = false) {
    const t = current();
    if (!t || (played < 1 && !disliked)) return;
    const seconds = Math.round(played);
    played = 0;
    post("/player/event", {track_id: t.id, seconds, disliked}, true)
      .then((r) => (r.ok ? r.json() : {}))
      .then((r) => { if (r.rebuilding && isWave()) followRebuild(); })
      .catch(() => {});
  }

  function next(disliked = false) {
    report(disliked);
    index += 1;
    if (isWave() && index >= tracks.length - 3) refresh();  // the wave never ends: its next version is asked early
    if (index < tracks.length) playCurrent();
    else if (isWave()) refresh().then(() => { if (index < tracks.length) playCurrent(); });
    else { audio.pause(); index = tracks.length - 1; }
  }

  // the wave's newest version: its tracks not yet heard become the ones after the current track
  async function refresh() {
    if (!isWave()) return false;
    if (refreshing) return refreshing;
    refreshing = (async () => {
      try {
        const data = await fetchQueue("wave");
        if (!data || !data.tracks || data.version === source.version) return false;
        const now = current();
        const upcoming = data.tracks.filter((t) => !heard.has(t.id) && (!now || t.id !== now.id));
        tracks = tracks.slice(0, index + 1).concat(upcoming);
        source.version = data.version;
        setStatus("wave", say("updated", {t: new Date().toLocaleTimeString([], {hour: "2-digit", minute: "2-digit"})}));
        return true;
      } catch {
        return false;
      } finally {
        refreshing = null;
      }
    })();
    return refreshing;
  }

  // after a report that queued a rebuild: the new version usually comes within seconds
  async function followRebuild() {
    for (const wait of [3000, 4000, 6000, 10000, 15000]) {
      await sleep(wait);
      if (await refresh()) return;
    }
  }
  setInterval(() => { if (isWave() && !audio.paused) refresh(); }, 30000);

  // ---------- the audio element ----------
  audio.addEventListener("timeupdate", () => {
    const t = audio.currentTime;
    if (!audio.paused && t > lastTime && t - lastTime < 2) played += t - lastTime;
    lastTime = t;
    const d = audio.duration || (current() && current().duration) || 0;
    els.time.textContent = clock(t);
    if (d && document.activeElement !== els.seek) els.seek.value = String(Math.round((t / d) * 100));
  });
  audio.addEventListener("seeked", () => { lastTime = audio.currentTime; });
  audio.addEventListener("playing", () => { failures = 0; });
  audio.addEventListener("ended", () => next());
  audio.addEventListener("play", syncButtons);
  audio.addEventListener("pause", syncButtons);
  audio.addEventListener("error", () => {
    // a file that won't play (a format the browser lacks, a missing file): the next one, but not round and round
    if (!source || !current()) return;
    failures += 1;
    setStatus(String(source.which), `${say("error")} ${current().artist} — ${current().title}`);
    if (failures < 3) setTimeout(() => next(), 1500);
  });

  // ---------- ♥ and 👎: told to the server (Navidrome too), every pair of the same track kept in step; a 👎 on what
  // plays skips it ----------
  const ratings = {};  // track id -> {liked, disliked}, as last known (the queue's, then the answers')
  function showRating() {
    document.querySelectorAll("[data-rate][data-track]").forEach((b) => {
      const known = ratings[b.dataset.track] || (current() && current().id === b.dataset.track ? current() : null);
      if (!known) return;
      const on = b.dataset.rate === "like" ? known.liked : known.disliked;
      b.classList.toggle("on", !!on);
      b.setAttribute("aria-pressed", on ? "true" : "false");
    });
  }
  async function rate(button) {
    const id = button.dataset.track;
    const value = button.classList.contains("on") ? "none" : button.dataset.rate;
    try {
      const response = await post("/player/rate", {track_id: id, value});
      const answer = await response.json();
      if (!response.ok) throw new Error(answer.detail || response.status);
      ratings[id] = answer;
      tracks.forEach((t) => { if (t.id === id) Object.assign(t, answer); });
      showRating();
      if (value === "dislike" && current() && current().id === id) next(true);
    } catch (e) {
      const toast = $("player-error");
      toast.textContent = String(e.message || e);
      if (typeof ui === "function") ui("#player-error");
    }
  }
  document.addEventListener("click", (e) => {
    const button = e.target.closest("[data-rate]");
    if (button && button.dataset.track) rate(button);
  });

  // ---------- the bar's buttons, the page's buttons, the lock screen ----------
  els.toggle.addEventListener("click", () => { if (audio.paused) audio.play(); else audio.pause(); });
  els.next.addEventListener("click", () => next());
  els.seek.addEventListener("change", () => {
    const d = audio.duration || 0;
    if (d) audio.currentTime = (Number(els.seek.value) / 100) * d;
  });
  document.addEventListener("click", (e) => {
    const button = e.target.closest("[data-play]");
    if (button && !button.disabled) start(button.dataset.play, button.dataset.start);
  });
  if ("mediaSession" in navigator) {
    navigator.mediaSession.setActionHandler("play", () => audio.play());
    navigator.mediaSession.setActionHandler("pause", () => audio.pause());
    navigator.mediaSession.setActionHandler("nexttrack", () => next());
  }
  document.addEventListener("kurai:page", syncButtons);
  window.addEventListener("pagehide", report);
})();
