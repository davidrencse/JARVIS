/* Mobile shell uses existing JARVIS endpoints. No fake device status or speculative location/camera controls. */
(() => {
  "use strict";
  const $ = (selector) => document.querySelector(selector);
  const $$ = (selector) => [...document.querySelectorAll(selector)];
  const panels = new Set(["home","remote","location","camera","assistant"]);
  let alive = true;
  const api = async (path, body) => {
    const options = { cache: "no-store", credentials: "same-origin" };
    if (body !== undefined) {
      options.method = "POST";
      options.headers = {"X-JARVIS":"1","Content-Type":"application/json"};
      options.body = JSON.stringify(body);
    }
    const response = await fetch(path, options);
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.error || "JARVIS responded with HTTP " + response.status);
    return data;
  };
  function navigation(name) {
    if (!panels.has(name)) name = "home";
    $$(".mobile-view").forEach((panel) => {panel.hidden = panel.dataset.panel !== name;});
    $$("[data-nav]").forEach((button) => {
      if (button.closest(".mobile-nav")) {
        if (button.dataset.nav === name) button.setAttribute("aria-current","page");
        else button.removeAttribute("aria-current");
      }
    });
    history.replaceState(null, "", "#"+name);
    window.scrollTo(0,0);
  }
  $$("[data-nav]").forEach((button) => button.addEventListener("click",()=>navigation(button.dataset.nav)));
  addEventListener("hashchange",()=>navigation(location.hash.slice(1)));
  navigation(location.hash.slice(1));
  function renderDevice(device) {
    const row = document.createElement("div");
    row.className = "machine-row";
    const glyph = document.createElement("span");
    glyph.className = "machine-glyph";
    glyph.textContent = device.main ? "⌘" : "▣";
    const info = document.createElement("span");
    info.className = "machine-info";
    const title = document.createElement("b");
    title.textContent = device.label || device.id || "Unknown device";
    const details = document.createElement("small");
    details.textContent = device.main ? "Main computer · Control plane" : (device.ready ? "Node · Ready for remote" : (device.why || (device.online ? "Node · Not ready" : "Node · Offline")));
    info.append(title,details);
    const dot = document.createElement("span");
    dot.className = "machine-dot" + (device.online ? " on" : "");
    dot.setAttribute("aria-label",device.online ? "Online" : "Offline");
    row.append(glyph,info,dot);
    return row;
  }
  function listDevices(target, devices) {
    target.replaceChildren();
    if (!devices.length) {
      const empty = document.createElement("div");
      empty.className = "machine-empty";
      empty.textContent = "No enrolled devices found.";
      target.append(empty);
    } else devices.forEach((d)=>target.append(renderDevice(d)));
  }
  async function refresh() {
    try {
      const [remote, overview] = await Promise.all([api("/api/remote"),api("/api/overview")]);
      if (!alive) return;
      const nodes = Array.isArray(remote.devices) ? remote.devices : [];
      const main = {id:"main",label:"JARVIS Main",online:true,main:true};
      const devices = [main, ...nodes];
      listDevices($("#machine-list"),devices);
      listDevices($("#remote-list"),nodes);
      const online = devices.filter((d)=>d.online).length;
      $("#online-count").textContent = String(online);
      $("#machine-count-label").textContent = online===1 ? "CONNECTED MACHINE" : "CONNECTED MACHINES";
      $("#network-label").textContent = "CONNECTED";
      $("#brain-state").textContent = "Online";
      $("#sync-time").textContent = new Date().toLocaleTimeString([], {hour:"2-digit", minute:"2-digit"});
      $("#link-state").className = "mobile-link-state online";
      $("#link-state span").textContent = "Connected";
    } catch (error) {
      if (!alive) return;
      $("#link-state").className = "mobile-link-state offline";
      $("#link-state span").textContent = "Unreachable";
      $("#network-label").textContent = "OFFLINE";
      $("#online-count").textContent = "—";
      $("#brain-state").textContent = "Unavailable";
      const el = $("#machine-list");
      el.replaceChildren();
      const message = document.createElement("div");
      message.className = "machine-empty";
      message.textContent = "Cannot reach the JARVIS console: " + error.message;
      el.append(message);
      listDevices($("#remote-list"),[]);
    }
  }
  $("#refresh-machines").addEventListener("click",refresh);
  const clock = ()=>{$("#mobile-clock").textContent=new Date().toLocaleTimeString([], {hour:"2-digit",minute:"2-digit"});};
  clock(); refresh();
  const interval = setInterval(()=>{if(!document.hidden){clock();refresh();}},12000);
  addEventListener("pagehide",()=>{alive=false;clearInterval(interval);});
  const form = $("#ask-form"), field = $("#ask-input"), status = $("#ask-status");
  form.addEventListener("submit",async(event)=>{
    event.preventDefault();
    const text = field.value.trim();
    if(!text)return;
    field.value="";
    const bubble=document.createElement("div");
    bubble.className="user-bubble";bubble.textContent=text;
    $("#conversation").append(bubble);
    const button=form.querySelector("button");
    button.disabled=true;status.textContent="Submitting request…";
    try {
      const result = await api("/api/ask",{text,speak:false});
      const reply=document.createElement("div");reply.className="assistant-bubble";
      reply.textContent=result.reply || result.message || (result.id ? "Request queued. Job: "+result.id : "Request submitted to JARVIS.");
      $("#conversation").append(reply);
      status.textContent="Request accepted. Check the desktop console for confirmation gates or running jobs.";
    } catch(error) {
      const reply=document.createElement("div");reply.className="assistant-bubble";
      reply.textContent="Request failed: "+error.message;
      $("#conversation").append(reply);
      status.textContent="Could not submit your request.";
    } finally {button.disabled=false;$("#conversation").scrollTop=$("#conversation").scrollHeight;}
  });
})();