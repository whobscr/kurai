// The theme (accent colour, light / dark), moving between pages without reloading (so the web player plays on), the
// CSRF header for HTMX, and each page's pieces: the «Проверить» buttons, the apps' frames, the colour presets, the
// «i» tips, the playlists' sheet, installing the app, and web push.
(() => {
  const body = document.body;
  const post = (url, data) => fetch(url, {
    method: "POST", headers: {"X-CSRF-Token": body.dataset.csrf, "Content-Type": "application/json"},
    body: data ? JSON.stringify(data) : null,
  });
  const store = {  // per-device remembering; private windows may refuse it
    get: (k) => { try { return localStorage.getItem(k); } catch { return null; } },
    set: (k, v) => { try { localStorage.setItem(k, v); } catch { /* not remembered */ } },
  };

  // ---------- theme: Beer CSS builds the whole Material palette (with readable on-colours) from one colour ----------
  const isDark = () => body.dataset.mode === "dark"
    || (body.dataset.mode === "auto" && matchMedia("(prefers-color-scheme: dark)").matches);
  const applyTheme = async () => {
    // both libraries are modules: they may run after this script
    if (typeof ui !== "function" || typeof materialDynamicColors !== "function") return setTimeout(applyTheme, 50);
    ui("mode", isDark() ? "dark" : "light");
    if (body.dataset.accent) await ui("theme", body.dataset.accent);
    // the band's switch shows where it leads
    const toggle = document.getElementById("mode-toggle");
    if (toggle) toggle.querySelector("i").textContent = isDark() ? "light_mode" : "dark_mode";
  };
  applyTheme();
  matchMedia("(prefers-color-scheme: dark)").addEventListener("change", applyTheme);

  // ---------- HTMX requests carry the session's form token ----------
  document.addEventListener("htmx:configRequest", (e) => { e.detail.headers["X-CSRF-Token"] = body.dataset.csrf; });

  // ---------- the service worker (installable app, push, «Поделиться») and the browser's install prompt ----------
  const worker = "serviceWorker" in navigator ? navigator.serviceWorker.register("/sw.js", {scope: "/"}) : null;
  const installed = matchMedia("(display-mode: standalone)").matches || navigator.standalone === true;
  const ios = /iphone|ipad|ipod/i.test(navigator.userAgent)
    || (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);
  const phone = matchMedia("(pointer: coarse)").matches;
  let prompt = null;
  window.addEventListener("beforeinstallprompt", (e) => { e.preventDefault(); prompt = e; showInstall(document); });
  window.addEventListener("appinstalled", () => { prompt = null; showInstall(document); });

  // ---------- installing the app: the browser's own prompt where it gives one (Chrome, Edge, Samsung), the steps
  // elsewhere (iPhone: «Поделиться» → «На экран „Домой“»); the band offers it until «Не сейчас» ----------
  function showInstall(root) {
    const state = installed ? "installed" : prompt ? "prompt" : ios ? "ios" : "other";
    root.querySelectorAll(".install-state").forEach((el) => { el.hidden = el.dataset.state !== state; });
    const banner = root.querySelector("#install-banner");
    if (!banner) return;
    banner.querySelector(".install-now").hidden = !prompt;
    banner.querySelector(".install-how").hidden = !!prompt;
    banner.hidden = installed || store.get("kurai.install.later") === "1" || location.pathname === "/me"
      || !(prompt || (phone && worker));
  }

  // ---------- each page's pieces (run at load and after every move to another page) ----------
  function init(root) {
    // the band's light / dark switch: the other mode at once, remembered for the account
    const modeToggle = root.querySelector("#mode-toggle");
    if (modeToggle) modeToggle.addEventListener("click", () => {
      body.dataset.mode = isDark() ? "light" : "dark";
      applyTheme();
      post("/me/mode", {mode: body.dataset.mode});
    });

    // tabs that don't fit: the wheel scrolls them sideways (never the page), the active one is in view
    root.querySelectorAll(".tabs").forEach((tabs) => {
      // only the strip moves (scrollIntoView would also shift the page sideways on a narrow screen)
      const active = tabs.querySelector("a.active");
      if (active) tabs.scrollLeft = active.offsetLeft - tabs.offsetLeft - (tabs.clientWidth - active.offsetWidth) / 2;
      tabs.addEventListener("wheel", (e) => {
        if (tabs.scrollWidth <= tabs.clientWidth) return;
        e.preventDefault();
        const delta = Math.abs(e.deltaX) > Math.abs(e.deltaY) ? e.deltaX : e.deltaY;
        tabs.scrollLeft += e.deltaMode === 1 ? delta * 40 : delta;  // Firefox counts in lines
      }, {passive: false});
    });

    // «Проверить»: one real request, its time or its error
    root.querySelectorAll("button.check").forEach((btn) => btn.addEventListener("click", async () => {
      const out = root.querySelector(`.check-result[data-for="${btn.dataset.check}"]`);
      out.className = "check-result small-text"; out.textContent = "…";
      try {
        const r = await (await post(`/admin/check/${btn.dataset.check}`)).json();
        out.classList.add(r.ok ? "ok" : "bad");
        out.textContent = `${r.ok ? "✓" : "✗"} ${r.detail} (${r.seconds} с)`;
      } catch (e) { out.classList.add("bad"); out.textContent = `✗ ${e}`; }
    }));

    // colour fields: the swatch shows the choice; a ready colour fills its field (and is marked chosen)
    const showColour = (input) => {
      input.parentElement.style.background = input.value;
      root.querySelectorAll(`.preset[data-for="${input.id}"]`).forEach((b) => {
        b.classList.toggle("chosen", b.dataset.color === input.value.toLowerCase());
      });
    };
    root.querySelectorAll(".swatch input[type=color]").forEach((i) => i.addEventListener("input", () => showColour(i)));
    root.querySelectorAll(".preset").forEach((b) => b.addEventListener("click", () => {
      const input = root.querySelector(`#${b.dataset.for}`);
      if (input) { input.value = b.dataset.color; showColour(input); }
    }));

    // the apps' admin pages in one frame: an app that works in a frame is shown in it; one that doesn't
    // (data-frame="no") gets only the way to its own tab
    const showApp = (a) => {
      root.querySelectorAll(".apps-tabs a").forEach((x) => x.classList.toggle("active", x === a));
      const url = `/admin/apps/${a.dataset.app}/`, framed = a.dataset.frame !== "no";
      root.querySelector("#app-frame").src = framed ? url : "about:blank";
      root.querySelector("#app-stage").classList.toggle("no-frame", !framed);
      root.querySelector("#app-tab").href = root.querySelector("#app-tab-big").href = url;
      root.querySelector("#app-noframe-name").textContent = a.dataset.label;
      root.querySelector("#app-full").classList.toggle("unframed", !framed);
      const info = root.querySelector("#app-info");
      info.querySelector(".info-tip-text").textContent = a.dataset.about;
      info.hidden = !a.dataset.about;
      info.open = false;
    };
    root.querySelectorAll(".apps-tabs a").forEach((a) => a.addEventListener("click", () => showApp(a)));
    // the frame with its app on the whole screen, where the browser allows it
    const stage = root.querySelector("#app-stage");
    if (stage && document.fullscreenEnabled && stage.requestFullscreen) {
      const full = root.querySelector("#app-full");
      full.hidden = false;
      full.addEventListener("click", () => stage.requestFullscreen().catch(() => { full.hidden = true; }));
      root.querySelector("#app-exit").addEventListener("click", () => document.exitFullscreen());
    }

    // «i» (beside a run-now button, an app): opened on hover (with a mouse) or by a tap; one at a time, kept inside
    // the window (a closed <details> shows nothing whatever the CSS, hence the script)
    const infos = root.querySelectorAll(".info-tip");
    const keepInside = (d) => {
      const text = d.querySelector(".info-tip-text");
      text.style.insetInlineStart = "0px";
      const over = text.getBoundingClientRect().right - (window.innerWidth - 12);
      if (over > 0) text.style.insetInlineStart = `${-over}px`;
    };
    infos.forEach((d) => {
      d.addEventListener("toggle", () => {
        if (!d.open) return;
        infos.forEach((other) => { if (other !== d) other.open = false; });
        keepInside(d);
      });
      if (matchMedia("(hover: hover)").matches) {
        d.addEventListener("mouseenter", () => { d.open = true; });
        d.addEventListener("mouseleave", () => { d.open = false; });
      }
    });

    // «Плейлисты» on «Для вас»: a sheet from the side; closed by its button, Esc, a tap beside it, or starting a playlist
    const sheet = root.querySelector("#playlists");
    if (sheet) {
      root.querySelector("#playlists-open").addEventListener("click", () => sheet.showModal());
      root.querySelector("#playlists-close").addEventListener("click", () => sheet.close());
      sheet.addEventListener("click", (e) => {
        if (e.target === sheet || e.target.closest("[data-play]")) sheet.close();
      });
    }

    // a question asked as the page opens (what a new listener likes)
    root.querySelectorAll("dialog[data-open-at-start]").forEach((d) => { if (!d.open) d.showModal(); });

    // installing the app
    showInstall(root);
    const later = root.querySelector("#install-later");
    if (later) later.addEventListener("click", () => {
      store.set("kurai.install.later", "1");
      root.querySelector("#install-banner").hidden = true;
    });
    root.querySelectorAll(".install-now").forEach((b) => b.addEventListener("click", async () => {
      if (!prompt) return;
      prompt.prompt();
      await prompt.userChoice;
      prompt = null;  // the browser gives a prompt once
      showInstall(document);
    }));

    initPush(root);
  }

  // a tap outside an open «i» closes it
  document.addEventListener("click", (e) => {
    document.querySelectorAll(".info-tip[open]").forEach((d) => { if (!d.contains(e.target)) d.open = false; });
  });

  // ---------- push on this device: its state, and turning it on (permission → subscription → a test push from the
  // server, whose result is shown); the texts come from the page (data-*), translated like every other text ----------
  function initPush(root) {
    const pushStatus = root.querySelector("#push-status");
    if (!pushStatus) return;
    const pushButton = root.querySelector("#push-enable");
    const b64 = (s) => Uint8Array.from(atob((s + "=".repeat((4 - s.length % 4) % 4)).replace(/-/g, "+").replace(/_/g, "/")),
      (c) => c.charCodeAt(0));
    const say = (what, extra = "") => { pushStatus.textContent = `${pushStatus.dataset[what]} ${extra}`.trim(); };
    const pushState = async () => {
      if (!worker || !("PushManager" in window) || !("Notification" in window)) {
        return say(ios && !installed ? "ios" : "unsupported");
      }
      if (!pushStatus.dataset.key) return say("nokey");
      pushButton.hidden = false;
      if (Notification.permission === "denied") return say("denied");
      const sub = await (await navigator.serviceWorker.ready).pushManager.getSubscription();
      // on only when this device is subscribed and the account takes push (the «Push» box above)
      say(sub && Notification.permission === "granted" && pushStatus.dataset.userOn === "1" ? "on" : "off");
    };
    say("checking");
    pushState().catch((e) => say("failed", String(e)));
    pushButton.addEventListener("click", async () => {
      pushButton.disabled = true;
      say("checking");
      try {
        if (await Notification.requestPermission() !== "granted") return say("denied");
        const reg = await navigator.serviceWorker.ready;
        const sub = await reg.pushManager.getSubscription()
          || await reg.pushManager.subscribe({userVisibleOnly: true, applicationServerKey: b64(pushStatus.dataset.key)});
        const r = await post("/me/push", sub.toJSON());
        const answer = r.ok ? await r.json() : {ok: false, detail: `HTTP ${r.status}`};
        if (answer.ok) say("sent"); else say("failed", answer.detail);
      } catch (e) {
        say("failed", String(e));
      } finally {
        pushButton.disabled = false;
      }
    });
  }

  // ---------- moving between pages: links and forms of the app fetch the next page and swap only <main id="page">
  // (and the menus), so the player outside it plays on. A page without one (the login page, an error) is shown as
  // it came; the apps' pages, downloads and links to other sites are left to the browser ----------
  const ours = (url) => url.origin === location.origin
    && !/^\/(admin\/apps|player|static|rest|logout)(\/|$)/.test(url.pathname);

  async function show(response, remember) {
    const html = await response.text();
    const doc = new DOMParser().parseFromString(html, "text/html");
    const next = doc.getElementById("page");
    if (remember) history.pushState({}, "", response.url);
    if (!next) {  // not a page of the app's shell: shown whole (the player stops with it)
      document.open(); document.write(html); document.close();
      return;
    }
    document.title = doc.title;
    // the menus (the active entry, the admin's) and the body's settings (accent, mode, form token) follow the page
    for (const sel of ["nav.left", "nav.bottom"]) {
      const mine = document.querySelector(sel), theirs = doc.querySelector(sel);
      if (mine && theirs) mine.replaceWith(theirs);
    }
    let themed = false;
    for (const key of ["accent", "mode", "csrf"]) {
      if (doc.body.dataset[key] !== undefined && doc.body.dataset[key] !== body.dataset[key]) {
        body.dataset[key] = doc.body.dataset[key];
        themed ||= key !== "csrf";
      }
    }
    document.getElementById("page").replaceWith(next);
    if (window.htmx) htmx.process(next);
    init(next);
    applyTheme();  // the new elements get the theme's colours (and a changed accent or mode applies)
    const anchor = location.hash && document.getElementById(decodeURIComponent(location.hash.slice(1)));
    if (anchor) anchor.scrollIntoView(); else window.scrollTo(0, 0);
    document.dispatchEvent(new CustomEvent("kurai:page"));
  }

  async function go(url, options = {}, remember = true) {
    try {
      const response = await fetch(url, {credentials: "same-origin", ...options});
      // a GET, or a POST that redirected: the address to remember; a POST answered with a page keeps the address
      await show(response, remember && (!options.method || options.method === "GET" || response.redirected));
    } catch (e) {
      location.assign(url);  // the network failed: let the browser try (and say what's wrong)
    }
  }

  if (document.getElementById("page")) {
    document.addEventListener("click", (e) => {
      const a = e.target.closest("a[href]");
      if (!a || e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
      if ((a.target && a.target !== "_self") || a.hasAttribute("download") || a.hasAttribute("data-reload")) return;
      const url = new URL(a.href, location.href);
      if (!ours(url)) return;
      if (url.pathname === location.pathname && url.search === location.search && url.hash) return;  // same page
      e.preventDefault();
      go(url.href);
    });
    document.addEventListener("submit", (e) => {
      const form = e.target;
      if (e.defaultPrevented || form.hasAttribute("data-reload") || (form.target && form.target !== "_self")) return;
      const url = new URL(form.getAttribute("action") || location.href, location.href);
      if (!ours(url)) return;
      e.preventDefault();
      const data = new FormData(form, e.submitter);
      const method = (form.getAttribute("method") || "GET").toUpperCase();
      if (method === "GET") {
        url.search = new URLSearchParams(data).toString();
        go(url.href);
      } else {
        go(url.href, {method, body: data});
      }
    });
    window.addEventListener("popstate", () => go(location.href, {}, false));
  }

  init(document);
})();
