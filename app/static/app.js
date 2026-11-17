(function () {
  "use strict";

  function $(id) { return document.getElementById(id); }

  function el(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  var statusEl = $("status");
  var searchEl = $("bus-search");
  var suggestEl = $("bus-suggest");
  var tagsRow = $("tags-row");
  var tagsEl = $("tags");
  var listEl = $("list");
  var listSummary = $("list-summary");
  var listItems = $("list-items");
  var infoEl = $("info");
  var shareEl = $("share");
  var shareOpen = $("share-open");
  var routePick = $("route-pick");
  var routeOther = $("route-other");
  var shareSetup = $("share-setup");
  var shareLive = $("share-live");
  var shareStatus = $("share-status");
  var shareError = $("share-error");
  var nearEl = $("near");
  var nearSummary = $("near-summary");
  var nearList = $("near-list");
  var nearError = $("near-error");
  var nearRadius = $("near-radius");
  var nearClear = $("near-clear");
  var hintEl = $("stops-hint");
  var pickEl = $("system-pick");
  var systemOpen = $("system-open");

  var STOPS_MIN_ZOOM = 14;
  var TAGS_KEY = "busTrackerTags";
  var LAYERS_KEY = "busTrackerLayers";
  var SYSTEM_KEY = "busTrackerSystem";
  var MAX_TAGS = 20;

  var useMph = /^en-US/i.test(navigator.language || "");
  var config = null;
  var map = null;
  var markers = {};
  var buses = [];
  var routes = [];
  var routeNames = {};
  var selectedId = null;
  var tags = []; // buses the person chose to see: {type: "route" | "bus", value}
  var fitPending = false;
  var systems = [];
  var system = null; // the transit system being shown: {id, name, center, zoom, ...}
  var layers = { buses: true, stops: false };
  var near = null; // where the person is: {lat, lon, radius}
  var youMarker = null;
  var picking = false;
  var last = { serverTime: null, updated: null, mode: "demo", agency: "", notes: [], feedOk: true };

  // ---------------------------------------------------------------- wording

  function ago(seconds) {
    if (seconds === null || seconds === undefined) return "a while ago";
    if (seconds < 5) return "just now";
    if (seconds < 90) return Math.round(seconds) + " seconds ago";
    if (seconds < 5400) return Math.round(seconds / 60) + " minutes ago";
    return Math.round(seconds / 3600) + " hours ago";
  }

  function speedText(metersPerSecond) {
    if (metersPerSecond === null || metersPerSecond === undefined) return "";
    var value = useMph ? metersPerSecond * 2.23694 : metersPerSecond * 3.6;
    var unit = useMph ? "mph" : "km/h";
    return value < 1 ? "Stopped." : "Moving at " + Math.round(value) + " " + unit + ".";
  }

  function distText(meters) {
    if (useMph) {
      var miles = meters / 1609.34;
      return miles < 0.1 ? Math.round((meters * 3.281) / 10) * 10 + " ft" : miles.toFixed(1) + " mi";
    }
    return meters < 1000 ? Math.round(meters / 10) * 10 + " m" : (meters / 1000).toFixed(1) + " km";
  }

  function distanceM(lat1, lon1, lat2, lon2) {
    var rad = Math.PI / 180;
    var dLat = (lat2 - lat1) * rad;
    var dLon = (lon2 - lon1) * rad;
    var a = Math.sin(dLat / 2) * Math.sin(dLat / 2) +
      Math.cos(lat1 * rad) * Math.cos(lat2 * rad) * Math.sin(dLon / 2) * Math.sin(dLon / 2);
    return 6371000 * 2 * Math.atan2(Math.sqrt(a), Math.sqrt(1 - a));
  }

  function busName(b) {
    if (!b.route) return "Bus " + (b.label || "");
    var extra = b.label && b.label !== b.route ? " (bus " + b.label + ")" : "";
    var name = b.routeName ? " " + b.routeName : "";
    return "Route " + b.route + name + extra;
  }

  function sourceText(b) {
    if (b.source === "riders") {
      return b.riders === 1 ? "Reported by 1 rider on this bus." : "Reported by " + b.riders + " riders on this bus.";
    }
    if (last.mode === "demo") return "Simulated bus.";
    return b.riders > 0
      ? "Position from the agency's feed, confirmed by " + b.riders + (b.riders === 1 ? " rider." : " riders.")
      : "Position from the agency's feed.";
  }

  function systemQuery() { return system && system.id ? "system=" + encodeURIComponent(system.id) : ""; }

  // ---------------------------------------------------------------- header status

  function renderStatus() {
    var line;
    if (last.mode === "demo") {
      line = "Demo mode: simulated buses";
    } else if (!last.feedOk) {
      line = "The agency's feed is not responding";
    } else {
      var age = last.updated === null ? null : Math.max(0, last.serverTime - last.updated);
      line = (last.agency ? "Live from " + last.agency : "Live bus positions") + ", updated " + ago(age);
    }
    if (!layers.buses) {
      statusEl.textContent = line + ". Buses are turned off.";
      return;
    }
    var visible = buses.filter(isShown);
    line += ". " + (visible.length === 1 ? "1 bus" : visible.length + " buses") + " on the map.";
    if (tags.length && !visible.length) line += " None of your chosen buses are reporting right now.";
    statusEl.textContent = line;
  }

  // ---------------------------------------------------------------- markers

  function makeMarker(id) {
    var button = el("button", "bus");
    button.type = "button";
    button.appendChild(el("span", "arrow"));
    button.appendChild(el("span", "name"));
    button.addEventListener("click", function () { select(id, true); });
    return button;
  }

  function updateMarker(b) {
    var m = markers[b.id];
    var fresh = !m;
    if (fresh) {
      var button = makeMarker(b.id);
      m = { el: button, handle: map.addMarker(button, b.lat, b.lon), timer: null };
      markers[b.id] = m;
    } else {
      m.el.classList.add("glide");
      clearTimeout(m.timer);
      m.timer = setTimeout(function () { m.el.classList.remove("glide"); }, 1200);
      m.handle.setLatLng(b.lat, b.lon);
    }
    var node = m.el;
    node.classList.toggle("riders", b.source === "riders");
    node.classList.toggle("stale", b.age > 60);
    node.classList.toggle("selected", b.id === selectedId);
    var label = (b.route || b.label || "?").slice(0, 4);
    node.querySelector(".name").textContent = label;
    node.classList.toggle("long", label.length > 2);
    var arrow = node.querySelector(".arrow");
    arrow.hidden = b.bearing === null;
    if (b.bearing !== null) arrow.style.transform = "rotate(" + b.bearing + "deg)";
    node.setAttribute("aria-label", busName(b) + ". " + sourceText(b) + " Updated " + ago(b.age) + ".");
    m.handle.setHidden(!isShown(b));
  }

  function clearBuses() {
    Object.keys(markers).forEach(function (id) { markers[id].handle.remove(); });
    markers = {};
    buses = [];
    routes = [];
    routeNames = {};
    selectedId = null;
    infoEl.hidden = true;
    renderRoutes();
    renderList();
    renderNear();
  }

  // ---------------------------------------------------------------- details and list

  function clearSelection() {
    selectedId = null;
    Object.keys(markers).forEach(function (key) { markers[key].el.classList.remove("selected"); });
    Object.keys(stopMarkers).forEach(function (key) { stopMarkers[key].el.classList.remove("selected"); });
  }

  function select(id, center) {
    var b = buses.filter(function (x) { return x.id === id; })[0];
    if (!b) { infoEl.hidden = true; return; }
    clearSelection();
    selectedId = id;
    if (markers[id]) markers[id].el.classList.add("selected");
    shareEl.hidden = true;
    nearEl.hidden = true;
    $("info-title").textContent = busName(b);
    $("info-source").textContent = sourceText(b);
    $("info-detail").textContent = ("Updated " + ago(b.age) + ". " + speedText(b.speed)).trim();
    infoEl.hidden = false;
    if (center) map.setView(b.lat, b.lon);
  }

  function selectStop(stop) {
    clearSelection();
    if (stopMarkers[stop.id]) stopMarkers[stop.id].el.classList.add("selected");
    shareEl.hidden = true;
    nearEl.hidden = true;
    $("info-title").textContent = stop.name;
    $("info-source").textContent = "Bus stop";
    $("info-detail").textContent = stop.code ? "Stop number " + stop.code + "." : "";
    infoEl.hidden = false;
  }

  // One row in a list of buses: a small route sign, the name, and a quiet second line.
  function busRow(b, detail, onPick) {
    var item = el("li");
    var button = el("button", "bus-row");
    button.type = "button";
    var sign = el("span", "sign", (b.route || b.label || "?").slice(0, 4));
    sign.classList.toggle("riders", b.source === "riders");
    var text = el("span", "what");
    text.appendChild(el("span", "title", busName(b)));
    text.appendChild(el("small", "", detail));
    button.appendChild(sign);
    button.appendChild(text);
    button.addEventListener("click", onPick);
    item.appendChild(button);
    return item;
  }

  function renderList() {
    var shown = buses.filter(isShown);
    listSummary.textContent = "Bus list (" + shown.length + ")";
    listItems.textContent = "";
    if (!shown.length) {
      listItems.appendChild(el("li", "empty", layers.buses ? "No buses reported right now." : "Buses are turned off."));
      return;
    }
    shown.slice(0, 200).forEach(function (b) {
      var detail = (b.source === "riders" ? "Reported by riders" : "Agency feed") + " \u00B7 " + ago(b.age);
      listItems.appendChild(busRow(b, detail, function () { select(b.id, true); }));
    });
    if (shown.length > 200) listItems.appendChild(el("li", "empty", "And " + (shown.length - 200) + " more. Search for a route to narrow it down."));
  }

  var routeKey = "";

  function renderRoutes() {
    var pick = routePick.dataset.touched ? routePick.value : "";
    routePick.textContent = "";
    routes.forEach(function (r) {
      var o = el("option", "", "Route " + r + (routeNames[r] ? " " + routeNames[r] : ""));
      o.value = r;
      routePick.appendChild(o);
    });
    var other = el("option", "", routes.length ? "Another route" : "Type the route");
    other.value = "__other";
    routePick.appendChild(other);
    var known = routes.indexOf(pick) >= 0 || pick === "__other";
    routePick.value = known ? pick : (routes[0] || "__other");
    routeOther.hidden = routePick.value !== "__other";
  }

  function applyData(data) {
    last = {
      serverTime: data.serverTime,
      updated: data.updated,
      mode: data.mode,
      agency: data.agency,
      notes: data.notes || [],
      feedOk: data.feedOk,
    };
    buses = data.buses || [];
    routeNames = data.routeNames || {};
    var key = (data.routes || []).join("\u0000") + "|" + JSON.stringify(routeNames);
    if (key !== routeKey) { routeKey = key; routes = data.routes || []; renderRoutes(); }
    var seen = {};
    buses.forEach(function (b) { seen[b.id] = true; updateMarker(b); });
    Object.keys(markers).forEach(function (id) {
      if (!seen[id]) {
        markers[id].handle.remove();
        delete markers[id];
        if (selectedId === id) { selectedId = null; infoEl.hidden = true; }
      }
    });
    renderStatus();
    renderList();
    renderNear();
    if (fitPending && buses.length) { fitPending = false; fitToShown(); }
    if (selectedId) select(selectedId, false);
    if (last.notes.length && last.mode !== "demo") statusEl.textContent += " " + last.notes[0];
  }

  // ---------------------------------------------------------------- loading

  function poll() {
    if (!system) return;
    var wanted = system.id;
    fetch("/api/buses" + (systemQuery() ? "?" + systemQuery() : ""), { cache: "no-store" })
      .then(function (response) {
        if (response.status === 429) throw new Error("The map is refreshing too often. Retrying shortly.");
        if (!response.ok) throw new Error("Could not load buses. Retrying shortly.");
        return response.json();
      })
      .then(function (data) {
        if (!system || system.id !== wanted) return; // the person switched systems meanwhile
        applyData(data);
      })
      .catch(function (err) { statusEl.textContent = err.message || "Could not load buses. Retrying shortly."; });
  }

  // ---------------------------------------------------------------- layers: buses and stops

  function loadLayers() {
    try {
      var saved = JSON.parse(localStorage.getItem(LAYERS_KEY) || "{}");
      return { buses: saved.buses !== false, stops: saved.stops === true };
    } catch (err) {
      return { buses: true, stops: false };
    }
  }

  function saveLayers() {
    try { localStorage.setItem(LAYERS_KEY, JSON.stringify(layers)); } catch (err) { /* storage can be off */ }
  }

  function renderLayers() {
    $("layer-buses").setAttribute("aria-pressed", layers.buses ? "true" : "false");
    $("layer-stops").setAttribute("aria-pressed", layers.stops ? "true" : "false");
  }

  var stopMarkers = {};
  var stopToken = 0;
  var stopTimer = null;
  var stopRetry = null;

  function setHint(text) {
    hintEl.textContent = text || "";
    hintEl.hidden = !text;
  }

  function removeStopMarkers(keepIds) {
    Object.keys(stopMarkers).forEach(function (id) {
      if (keepIds && keepIds[id]) return;
      stopMarkers[id].handle.remove();
      delete stopMarkers[id];
    });
  }

  function addStop(stop) {
    var button = el("button", "stop");
    button.type = "button";
    button.setAttribute("aria-label", "Bus stop: " + stop.name);
    button.addEventListener("click", function () { selectStop(stop); });
    stopMarkers[stop.id] = { el: button, handle: map.addMarker(button, stop.lat, stop.lon), stop: stop };
  }

  function drawStops(data) {
    if (data.state === "loading") {
      setHint("Loading the stops...");
      stopRetry = setTimeout(refreshStops, 4000);
      return;
    }
    if (data.state === "off") {
      removeStopMarkers();
      setHint("Stops are not available for " + (system ? system.name : "this system") + " yet.");
      return;
    }
    if (data.state === "failed") {
      removeStopMarkers();
      setHint("The stops could not be loaded right now. Trying again soon.");
      stopRetry = setTimeout(refreshStops, 60000);
      return;
    }
    var keep = {};
    data.stops.forEach(function (stop) {
      keep[stop.id] = true;
      if (!stopMarkers[stop.id]) addStop(stop);
    });
    removeStopMarkers(keep);
    if (data.truncated) setHint("Showing the stops nearest the middle. Zoom in to see more.");
    else setHint(data.stops.length ? "" : "No stops in this view.");
  }

  function refreshStops() {
    clearTimeout(stopTimer);
    clearTimeout(stopRetry);
    if (!map || !system) return;
    if (!layers.stops) { stopToken += 1; removeStopMarkers(); setHint(""); return; }
    if (map.zoom < STOPS_MIN_ZOOM) {
      stopToken += 1;
      removeStopMarkers();
      setHint("Zoom in to see stops.");
      return;
    }
    var b = map.getBounds();
    var mine = ++stopToken;
    var query = ["south", "west", "north", "east"].map(function (k) { return k + "=" + b[k].toFixed(5); }).join("&");
    fetch("/api/stops?" + systemQuery() + "&" + query, { cache: "no-store" })
      .then(function (response) {
        if (response.status === 429) { stopRetry = setTimeout(refreshStops, 15000); return null; }
        if (!response.ok) throw new Error("stops");
        return response.json();
      })
      .then(function (data) { if (data && mine === stopToken) drawStops(data); })
      .catch(function () { if (mine === stopToken) setHint("The stops could not be loaded right now."); });
  }

  function scheduleStops() {
    clearTimeout(stopTimer);
    stopTimer = setTimeout(refreshStops, 600);
  }

  $("layer-buses").addEventListener("click", function () {
    layers.buses = !layers.buses;
    saveLayers();
    renderLayers();
    applyFilter(false);
    renderNear();
  });

  $("layer-stops").addEventListener("click", function () {
    layers.stops = !layers.stops;
    saveLayers();
    renderLayers();
    refreshStops();
  });

  // ---------------------------------------------------------------- choosing a transit system

  function savedSystem() {
    try { return localStorage.getItem(SYSTEM_KEY) || ""; } catch (err) { return ""; }
  }

  function systemById(id) {
    return systems.filter(function (s) { return s.id === id; })[0] || null;
  }

  function hidePicker() {
    pickEl.hidden = true;
    document.body.classList.remove("has-overlay");
  }

  function showPicker() {
    var list = $("system-list");
    list.textContent = "";
    var firstAvailable = null;
    systems.forEach(function (s) {
      var item = el("li");
      var button = el("button", "system-choice");
      button.type = "button";
      button.appendChild(el("strong", "", s.name));
      if (s.city) button.appendChild(el("span", "city", s.city));
      if (!s.available) {
        button.disabled = true;
        button.appendChild(el("span", "note", s.note || "Not available yet."));
      } else {
        if (system && system.id === s.id) button.appendChild(el("span", "note", "Showing now"));
        if (!firstAvailable) firstAvailable = button;
        button.addEventListener("click", function () { chooseSystem(s, true); });
      }
      item.appendChild(button);
      list.appendChild(item);
    });
    $("system-close").hidden = !system;
    pickEl.hidden = false;
    document.body.classList.add("has-overlay");
    if (firstAvailable) firstAvailable.focus();
  }

  function chooseSystem(s, remember) {
    if (share.session) stopSharing("");
    system = s;
    if (remember) {
      try { localStorage.setItem(SYSTEM_KEY, s.id); } catch (err) { /* storage can be off */ }
    }
    clearBuses();
    tags = loadTags();
    fitPending = tags.length > 0;
    renderTags();
    shareEl.hidden = true;
    stopToken += 1;
    removeStopMarkers();
    setHint("");
    systemOpen.textContent = s.name + " ▾";
    systemOpen.setAttribute("aria-label", "Transit system: " + s.name + ". Change it.");
    systemOpen.hidden = systems.length < 2;
    statusEl.textContent = "Loading " + s.name + " buses...";
    if (map && s.center) map.setView(s.center[0], s.center[1], s.zoom);
    hidePicker();
    poll();
    refreshStops();
  }

  systemOpen.addEventListener("click", showPicker);
  $("system-close").addEventListener("click", function () { hidePicker(); systemOpen.focus(); });

  document.addEventListener("keydown", function (e) {
    if (e.key !== "Escape") return;
    if (!pickEl.hidden && system) { hidePicker(); systemOpen.focus(); }
    else if (picking) stopPicking();
  });

  // ---------------------------------------------------------------- near me

  function nearRadii() { return [500, 1000, 2000]; }

  function setUpRadius() {
    nearRadius.textContent = "";
    nearRadii().forEach(function (m) {
      var o = el("option", "", distText(m));
      o.value = String(m);
      nearRadius.appendChild(o);
    });
    nearRadius.value = "1000";
  }

  function showNearError(text) {
    nearError.textContent = text || "";
    nearError.hidden = !text;
  }

  function stopPicking() {
    picking = false;
    if (map) map.onTap = null;
    $("map").classList.remove("picking");
    $("near-pick").textContent = "Pick a spot on the map";
  }

  function fitNearArea() {
    var dLat = (near.radius / 111320) * 1.15;
    var dLon = dLat / Math.max(0.2, Math.cos((near.lat * Math.PI) / 180));
    map.fitBounds(near.lat - dLat, near.lon - dLon, near.lat + dLat, near.lon + dLon);
  }

  function setNear(lat, lon) {
    near = { lat: lat, lon: lon, radius: Number(nearRadius.value) || 1000 };
    if (!youMarker) {
      var dot = el("div", "you");
      dot.setAttribute("role", "img");
      dot.setAttribute("aria-label", "Your chosen location");
      youMarker = map.addMarker(dot, lat, lon);
    } else {
      youMarker.setLatLng(lat, lon);
    }
    showNearError("");
    fitNearArea();
    applyFilter(false);
    renderNear();
  }

  function clearNear() {
    near = null;
    if (youMarker) { youMarker.remove(); youMarker = null; }
    stopPicking();
    applyFilter(false);
    renderNear();
  }

  function renderNear() {
    nearList.textContent = "";
    nearClear.hidden = !near;
    if (!near) {
      nearSummary.textContent = "Choose where you are to see the buses around you.";
      return;
    }
    if (!layers.buses) {
      nearSummary.textContent = "Buses are turned off. Turn on Buses at the top to see them.";
      return;
    }
    var around = buses.filter(isShown).map(function (b) {
      return { bus: b, meters: distanceM(b.lat, b.lon, near.lat, near.lon) };
    }).sort(function (a, b) { return a.meters - b.meters; });
    var within = "within " + distText(near.radius);
    nearSummary.textContent = around.length
      ? (around.length === 1 ? "1 bus " : around.length + " buses ") + within + ". The closest first:"
      : "No buses " + within + " right now. Try a bigger distance.";
    around.slice(0, 8).forEach(function (entry) {
      nearList.appendChild(busRow(entry.bus, distText(entry.meters) + " away", function () { select(entry.bus.id, true); }));
    });
  }

  $("near-open").addEventListener("click", function () {
    infoEl.hidden = true;
    shareEl.hidden = true;
    nearEl.hidden = !nearEl.hidden;
    if (!nearEl.hidden) renderNear();
  });

  $("near-close").addEventListener("click", function () { nearEl.hidden = true; stopPicking(); });
  nearClear.addEventListener("click", clearNear);

  nearRadius.addEventListener("change", function () {
    if (!near) return;
    near.radius = Number(nearRadius.value) || 1000;
    fitNearArea();
    applyFilter(false);
    renderNear();
  });

  $("near-locate").addEventListener("click", function () {
    stopPicking();
    showNearError("");
    if (!("geolocation" in navigator)) {
      showNearError("This browser cannot find your location. Use Pick a spot on the map instead.");
      return;
    }
    nearSummary.textContent = "Finding your location...";
    navigator.geolocation.getCurrentPosition(
      function (position) { setNear(position.coords.latitude, position.coords.longitude); },
      function (error) {
        renderNear();
        showNearError(
          error && error.code === 1
            ? "Location is blocked for this site. Allow it in your browser settings, or pick a spot on the map."
            : "Your location could not be found. Try again, or pick a spot on the map."
        );
      },
      { enableHighAccuracy: false, timeout: 15000, maximumAge: 60000 }
    );
  });

  $("near-pick").addEventListener("click", function () {
    if (picking) { stopPicking(); return; }
    picking = true;
    showNearError("");
    $("map").classList.add("picking");
    $("near-pick").textContent = "Cancel picking";
    nearSummary.textContent = "Tap the map where you are.";
    nearEl.hidden = false;
    map.onTap = function (lat, lon) { stopPicking(); setNear(lat, lon); };
  });

  // ---------------------------------------------------------------- search and tags

  function isShown(b) {
    if (!layers.buses) return false;
    if (near && distanceM(b.lat, b.lon, near.lat, near.lon) > near.radius) return false;
    if (!tags.length) return true;
    return tags.some(function (t) {
      return t.type === "route" ? b.route === t.value : !!b.label && b.label === t.value;
    });
  }

  function hasTag(type, value) {
    return tags.some(function (t) { return t.type === type && t.value === value; });
  }

  function tagName(t) { return (t.type === "route" ? "Route " : "Bus ") + t.value; }

  function validTag(t) {
    return t && (t.type === "route" || t.type === "bus") && typeof t.value === "string" &&
      t.value.length > 0 && t.value.length <= 40;
  }

  function tagsKey() { return TAGS_KEY + (system && system.id ? ":" + system.id : ""); }

  function loadTags() {
    try {
      var list = JSON.parse(localStorage.getItem(tagsKey()) || "[]");
      return Array.isArray(list) ? list.filter(validTag).slice(0, MAX_TAGS) : [];
    } catch (err) {
      return [];
    }
  }

  function saveTags() {
    try { localStorage.setItem(tagsKey(), JSON.stringify(tags)); } catch (err) { /* storage can be off */ }
  }

  function renderTags() {
    tagsEl.textContent = "";
    tags.forEach(function (t, index) {
      var item = el("li");
      item.appendChild(el("span", "", tagName(t)));
      var remove = el("button", "", "×");
      remove.type = "button";
      remove.setAttribute("aria-label", "Remove " + tagName(t));
      remove.addEventListener("click", function () { removeTag(index); });
      item.appendChild(remove);
      tagsEl.appendChild(item);
    });
    tagsRow.hidden = !tags.length;
  }

  function applyFilter(fit) {
    buses.forEach(function (b) {
      var m = markers[b.id];
      if (m) m.handle.setHidden(!isShown(b));
    });
    var selected = buses.filter(function (b) { return b.id === selectedId; })[0];
    if (selected && !isShown(selected)) {
      clearSelection();
      infoEl.hidden = true;
    }
    renderTags();
    renderStatus();
    renderList();
    if (fit) fitToShown();
  }

  // After choosing a bus, show where the chosen buses are.
  function fitToShown() {
    var shown = buses.filter(isShown);
    if (!tags.length || !shown.length || !map) return;
    var south = 90, north = -90, west = 180, east = -180;
    shown.forEach(function (b) {
      south = Math.min(south, b.lat); north = Math.max(north, b.lat);
      west = Math.min(west, b.lon); east = Math.max(east, b.lon);
    });
    map.fitBounds(south, west, north, east);
  }

  function addTag(tag) {
    if (hasTag(tag.type, tag.value) || tags.length >= MAX_TAGS) return;
    tags.push({ type: tag.type, value: tag.value });
    saveTags();
    searchEl.value = "";
    closeSuggestions();
    applyFilter(true);
  }

  function removeTag(index) {
    tags.splice(index, 1);
    saveTags();
    applyFilter(false);
    searchEl.focus();
  }

  var options = [];
  var active = -1;

  function countText(n) { return n === 1 ? "1 bus" : n + " buses"; }

  function findOptions(raw) {
    var q = raw.trim().toLowerCase();
    var found = [];
    routes.forEach(function (r) {
      if (hasTag("route", r)) return;
      var name = routeNames[r] || "";
      var byCode = r.toLowerCase().indexOf(q);
      var byName = name.toLowerCase().indexOf(q);
      if (q && byCode < 0 && byName < 0) return;
      var n = buses.filter(function (b) { return b.route === r; }).length;
      found.push({
        type: "route",
        value: r,
        note: (name ? name + ", " : "") + countText(n),
        rank: byCode === 0 ? 0 : byCode > 0 ? 1 : 2,
      });
    });
    if (q) {
      buses.forEach(function (b) {
        if (!b.label || b.label === b.route || hasTag("bus", b.label)) return;
        if (b.label.toLowerCase().indexOf(q) < 0) return;
        if (found.some(function (f) { return f.type === "bus" && f.value === b.label; })) return;
        found.push({ type: "bus", value: b.label, note: b.route ? "route " + b.route : "", rank: 3 });
      });
    }
    found.sort(function (a, b) { return a.rank - b.rank; });
    return found.slice(0, 8);
  }

  function setActive(index) {
    active = index;
    Array.prototype.forEach.call(suggestEl.children, function (li, i) {
      li.setAttribute("aria-selected", i === index ? "true" : "false");
    });
    if (index >= 0 && suggestEl.children[index]) {
      searchEl.setAttribute("aria-activedescendant", suggestEl.children[index].id);
      suggestEl.children[index].scrollIntoView({ block: "nearest" });
    } else {
      searchEl.removeAttribute("aria-activedescendant");
    }
  }

  function closeSuggestions() {
    suggestEl.hidden = true;
    suggestEl.textContent = "";
    options = [];
    searchEl.setAttribute("aria-expanded", "false");
    setActive(-1);
  }

  function openSuggestions() {
    options = findOptions(searchEl.value);
    suggestEl.textContent = "";
    if (!options.length) {
      if (!searchEl.value.trim()) { closeSuggestions(); return; }
      suggestEl.appendChild(el("li", "none", "No route or bus matches that."));
    }
    options.forEach(function (o, i) {
      var li = el("li");
      li.id = "bus-option-" + i;
      li.setAttribute("role", "option");
      li.appendChild(document.createTextNode(tagName(o)));
      if (o.note) {
        li.appendChild(document.createTextNode(" "));
        li.appendChild(el("small", "", o.note));
      }
      li.addEventListener("mousedown", function (e) { e.preventDefault(); addTag(o); });
      suggestEl.appendChild(li);
    });
    suggestEl.hidden = false;
    searchEl.setAttribute("aria-expanded", "true");
    setActive(options.length ? 0 : -1);
  }

  searchEl.addEventListener("input", openSuggestions);
  searchEl.addEventListener("focus", openSuggestions);
  searchEl.addEventListener("blur", closeSuggestions);
  searchEl.addEventListener("keydown", function (e) {
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      if (suggestEl.hidden) openSuggestions();
      if (!options.length) return;
      e.preventDefault();
      var step = e.key === "ArrowDown" ? 1 : -1;
      setActive((active + step + options.length) % options.length);
    } else if (e.key === "Enter") {
      e.preventDefault();
      if (options[active]) addTag(options[active]);
    } else if (e.key === "Escape") {
      if (!suggestEl.hidden) closeSuggestions(); else searchEl.value = "";
    } else if (e.key === "Backspace" && !searchEl.value && tags.length) {
      removeTag(tags.length - 1);
    }
  });

  $("tags-clear").addEventListener("click", function () {
    tags = [];
    saveTags();
    applyFilter(false);
    searchEl.focus();
  });

  $("info-close").addEventListener("click", function () {
    infoEl.hidden = true;
    clearSelection();
  });

  // ---------------------------------------------------------------- sharing a ride

  var share = { session: null, watch: null, sent: 0, lastSent: 0, route: "", centered: false, wake: null };

  function showShareError(text) {
    shareError.textContent = text || "";
    shareError.hidden = !text;
  }

  function post(url, body, keepalive) {
    return fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      keepalive: !!keepalive,
    }).then(function (response) {
      return response.json().catch(function () { return {}; }).then(function (data) {
        return { ok: response.ok, status: response.status, data: data };
      });
    });
  }

  function renderShare() {
    var sharing = !!share.session;
    shareSetup.hidden = sharing;
    shareLive.hidden = !sharing;
    shareOpen.textContent = sharing ? "Sharing route " + share.route : "Share my bus";
    shareOpen.classList.toggle("live", sharing);
    if (sharing) {
      shareStatus.textContent = share.sent
        ? "Sharing route " + share.route + ". " + share.sent + " position" + (share.sent === 1 ? "" : "s") + " sent."
        : "Sharing route " + share.route + ". Waiting for your phone's location.";
    }
  }

  function releaseWake() {
    if (share.wake && share.wake.release) { share.wake.release().catch(function () {}); }
    share.wake = null;
  }

  function keepScreenOn() {
    if (!("wakeLock" in navigator)) return;
    navigator.wakeLock.request("screen").then(function (lock) { share.wake = lock; }).catch(function () {});
  }

  function stopSharing(message) {
    if (share.watch !== null) navigator.geolocation.clearWatch(share.watch);
    if (share.session) post("/api/riders/stop", { session: share.session }, true).catch(function () {});
    share = { session: null, watch: null, sent: 0, lastSent: 0, route: "", centered: false, wake: share.wake };
    releaseWake();
    renderShare();
    showShareError(message || "");
  }

  function onPosition(position) {
    if (!share.session) return;
    var now = Date.now();
    if (now - share.lastSent < 5000) return;
    share.lastSent = now;
    var c = position.coords;
    if (!share.centered) { share.centered = true; map.setView(c.latitude, c.longitude); }
    post("/api/riders/report", {
      session: share.session,
      lat: c.latitude,
      lon: c.longitude,
      accuracy: c.accuracy,
    }).then(function (result) {
      if (!share.session) return;
      if (result.ok) {
        if (result.data.result === "ok") share.sent += 1;
        showShareError(result.data.result === "ignored" ? "Waiting for a stronger location signal." : "");
        renderShare();
      } else if (result.status === 404) {
        stopSharing(result.data.error || "Sharing has ended.");
      } else if (result.status !== 429) {
        showShareError(result.data.error || "Your position could not be sent.");
      }
    }).catch(function () { showShareError("Could not reach the server. Still trying."); });
  }

  function onPositionError(error) {
    if (error && error.code === 1) {
      stopSharing("Location is blocked for this site. Allow it in your browser settings, then try again.");
    } else {
      showShareError("Your phone could not find its location. Still trying.");
    }
  }

  routePick.addEventListener("change", function () {
    routePick.dataset.touched = "yes";
    routeOther.hidden = routePick.value !== "__other";
  });

  $("share-start").addEventListener("click", function () {
    showShareError("");
    var route = routePick.value === "__other" ? routeOther.value.trim() : routePick.value;
    if (!route) { showShareError("Choose or type the route you are on."); return; }
    if (!("geolocation" in navigator)) {
      showShareError("This browser cannot share location. Try again on your phone's own browser.");
      return;
    }
    var body = { route: route };
    if (system && system.id) body.system = system.id;
    post("/api/riders/start", body).then(function (result) {
      if (!result.ok) { showShareError(result.data.error || "Could not start sharing."); return; }
      share.session = result.data.session;
      share.route = route;
      share.watch = navigator.geolocation.watchPosition(onPosition, onPositionError, {
        enableHighAccuracy: true,
        maximumAge: 5000,
        timeout: 30000,
      });
      keepScreenOn();
      renderShare();
    }).catch(function () { showShareError("Could not reach the server. Try again."); });
  });

  $("share-stop").addEventListener("click", function () { stopSharing(""); shareEl.hidden = true; });
  $("share-cancel").addEventListener("click", function () { shareEl.hidden = true; showShareError(""); });

  shareOpen.addEventListener("click", function () {
    infoEl.hidden = true;
    nearEl.hidden = true;
    shareEl.hidden = !shareEl.hidden;
    renderShare();
  });

  window.addEventListener("pagehide", function () {
    if (share.session) post("/api/riders/stop", { session: share.session }, true).catch(function () {});
  });

  document.addEventListener("visibilitychange", function () {
    if (document.visibilityState === "visible") {
      poll();
      if (share.session && !share.wake) keepScreenOn();
    }
  });

  // ---------------------------------------------------------------- start

  layers = loadLayers();
  renderLayers();
  setUpRadius();
  renderTags();

  function loadSystems() {
    return fetch("/api/systems", { cache: "no-store" })
      .then(function (response) { return response.ok ? response.json() : null; })
      .catch(function () { return null; });
  }

  fetch("/api/config", { cache: "no-store" })
    .then(function (response) { return response.json(); })
    .then(function (data) {
      config = data;
      map = new window.SimpleMap($("map"), {
        center: data.center,
        zoom: data.zoom,
        tileUrl: data.tileUrl,
        attribution: data.attribution,
      });
      map.onChange = scheduleStops;
      renderRoutes();
      setInterval(function () {
        if (document.visibilityState === "visible") poll();
      }, Math.max(5, data.refreshSeconds) * 1000);
      return loadSystems().then(function (result) {
        systems = result && result.systems ? result.systems : [];
        if (systems.length > 1) {
          var saved = systemById(savedSystem());
          if (saved && saved.available) chooseSystem(saved, false);
          else showPicker();
        } else {
          chooseSystem(systems[0] || { id: "", name: data.agency, center: data.center, zoom: data.zoom }, false);
        }
      });
    })
    .catch(function () { statusEl.textContent = "Could not load the map. Check your connection and reload."; });
})();
