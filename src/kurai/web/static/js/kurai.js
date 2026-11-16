// The theme (accent colour, light / dark), the CSRF header for HTMX, the «Проверить» buttons, the apps' frames, the
// colour presets, the «i» tips, installing the app, and web push.
(() => {
  const body = document.body;

  // ---------- theme: Beer CSS builds the whole Material palette (with readable on-colours) from one colour ----------
  const isDark = () => body.dataset.mode === "dark"
    || (body.dataset.mode === "auto" && matchMedia("(prefers-color-scheme: dark)").matches);
  const modeToggle = document.getElementById("mode-toggle");
  const applyTheme = async () => {
    // both libraries are modules: they may run after this script
    if (typeof ui !== "function" || typeof materialDynamicColors !== "function") return setTimeout(applyTheme, 50);
    ui("mode", isDark() ? "dark" : "light");
    if (body.dataset.accent) await ui("theme", body.dataset.accent);
    // the switch shows where it leads
    if (modeToggle) modeToggle.querySelector("i").textContent = isDark() ? "light_mode" : "dark_mode";
  };
  applyTheme();
  matchMedia("(prefers-color-scheme: dark)").addEventListener("change", applyTheme);

  // the band's switch: the other mode at once, remembered for the account («Кабинет» also has «Как в системе»)
  if (modeToggle) modeToggle.addEventListener("click", () => {
    body.dataset.mode = isDark() ? "light" : "dark";
    applyTheme();
    post("/me/mode", {mode: body.dataset.mode});
  });

  // ---------- tabs that don't fit: the wheel scrolls them sideways (never the page), the active one is in view ----------
  document.querySelectorAll(".tabs").forEach((tabs) => {
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

  // ---------- HTMX requests carry the session's form token ----------
  document.addEventListener("htmx:configRequest", (e) => { e.detail.headers["X-CSRF-Token"] = body.dataset.csrf; });

  const post = (url, data) => fetch(url, {
    method: "POST", headers: {"X-CSRF-Token": body.dataset.csrf, "Content-Type": "application/json"},
    body: data ? JSON.stringify(data) : null,
  });

  // ---------- «Проверить»: one real request, its time or its error ----------
  document.querySelectorAll("button.check").forEach((btn) => btn.addEventListener("click", async () => {
    const out = document.querySelector(`.check-result[data-for="${btn.dataset.check}"]`);
    out.className = "check-result small-text"; out.textContent = "…";
    try {
      const r = await (await post(`/admin/check/${btn.dataset.check}`)).json();
      out.classList.add(r.ok ? "ok" : "bad");
      out.textContent = `${r.ok ? "✓" : "✗"} ${r.detail} (${r.seconds} с)`;
    } catch (e) { out.classList.add("bad"); out.textContent = `✗ ${e}`; }
  }));

  // ---------- colour fields: the swatch shows the choice; presets fill the field ----------
  const showColour = (input) => { input.parentElement.style.background = input.value; };
  document.querySelectorAll(".swatch input[type=color]").forEach((i) => i.addEventListener("input", () => showColour(i)));
  document.querySelectorAll(".preset").forEach((b) => b.addEventListener("click", () => {
    const input = document.querySelector('input[type=color][name="ui.default_accent"]');
    if (input) { input.value = b.dataset.color; showColour(input); }
  }));

  // ---------- the apps' admin pages in one frame ----------
  // an app that works in a frame is shown in it; one that doesn't (data-frame="no") gets only the way to its own tab
  const showApp = (a) => {
    document.querySelectorAll(".apps-tabs a").forEach((x) => x.classList.toggle("active", x === a));
    const url = `/admin/apps/${a.dataset.app}/`, framed = a.dataset.frame !== "no";
    const frame = document.getElementById("app-frame");
    frame.src = framed ? url : "about:blank";
    document.getElementById("app-stage").classList.toggle("no-frame", !framed);
    document.getElementById("app-tab").href = document.getElementById("app-tab-big").href = url;
    document.getElementById("app-noframe-name").textContent = a.dataset.label;
    document.getElementById("app-full").classList.toggle("unframed", !framed);
    const info = document.getElementById("app-info");
    info.querySelector(".info-tip-text").textContent = a.dataset.about;
    info.hidden = !a.dataset.about;
    info.open = false;
  };
  document.querySelectorAll(".apps-tabs a").forEach((a) => a.addEventListener("click", () => showApp(a)));
  // the frame with its app on the whole screen, where the browser allows it; the frame stays as it is otherwise
  const stage = document.getElementById("app-stage");
  if (stage && document.fullscreenEnabled && stage.requestFullscreen) {
    const full = document.getElementById("app-full");
    full.hidden = false;
    full.addEventListener("click", () => stage.requestFullscreen().catch(() => { full.hidden = true; }));
    document.getElementById("app-exit").addEventListener("click", () => document.exitFullscreen());
  }

  // ---------- «i» (beside a run-now button, an app): opened on hover (with a mouse) or by a tap; one at a time, kept inside the
  // window, closed by a tap elsewhere (a closed <details> shows nothing whatever the CSS, hence the script) ----------
  const infos = document.querySelectorAll(".info-tip");
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
  if (infos.length) document.addEventListener("click", (e) => {
    infos.forEach((d) => { if (!d.contains(e.target)) d.open = false; });
  });

  // ---------- the service worker (installable app, push, «Поделиться») ----------
  const worker = "serviceWorker" in navigator ? navigator.serviceWorker.register("/sw.js", {scope: "/"}) : null;
  const store = {  // per-device remembering; private windows may refuse it
    get: (k) => { try { return localStorage.getItem(k); } catch { return null; } },
    set: (k, v) => { try { localStorage.setItem(k, v); } catch { /* not remembered */ } },
  };

  // ---------- installing the app: the browser's own prompt where it gives one (Chrome, Edge, Samsung), the steps
  // elsewhere (iPhone: «Поделиться» → «На экран „Домой“»); the band offers it until «Не сейчас» ----------
  const installed = matchMedia("(display-mode: standalone)").matches || navigator.standalone === true;
  const ios = /iphone|ipad|ipod/i.test(navigator.userAgent)
    || (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);
  const phone = matchMedia("(pointer: coarse)").matches;
  const banner = document.getElementById("install-banner");
  let prompt = null;
  const showInstall = () => {
    const state = installed ? "installed" : prompt ? "prompt" : ios ? "ios" : "other";
    document.querySelectorAll(".install-state").forEach((el) => { el.hidden = el.dataset.state !== state; });
    if (!banner) return;
    banner.querySelector(".install-now").hidden = !prompt;
    banner.querySelector(".install-how").hidden = !!prompt;
    banner.hidden = installed || store.get("kurai.install.later") === "1" || location.pathname === "/me"
      || !(prompt || (phone && worker));
  };
  window.addEventListener("beforeinstallprompt", (e) => { e.preventDefault(); prompt = e; showInstall(); });
  window.addEventListener("appinstalled", () => { prompt = null; if (banner) banner.hidden = true; });
  document.querySelectorAll(".install-now").forEach((b) => b.addEventListener("click", async () => {
    if (!prompt) return;
    prompt.prompt();
    await prompt.userChoice;
    prompt = null;  // the browser gives a prompt once
    showInstall();
  }));
  const later = document.getElementById("install-later");
  if (later) later.addEventListener("click", () => { store.set("kurai.install.later", "1"); banner.hidden = true; });
  showInstall();

  // ---------- push on this device: its state, and turning it on (permission → subscription → a test push from the
  // server, whose result is shown); the texts come from the page (data-*), translated like every other text ----------
  const pushStatus = document.getElementById("push-status");
  const pushButton = document.getElementById("push-enable");
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
  if (pushStatus) {
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
})();
