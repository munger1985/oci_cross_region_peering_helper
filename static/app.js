const state = {
  profile: "DEFAULT",
  compartments: [],
  regions: [],
  subscribed: [],
  sides: {
    left: emptySide(),
    right: emptySide(),
  },
  link: null,
  drgRouteView: { left: "", right: "" },
  drag: null,
  refreshing: false,
  nodeTops: {},
};

function emptySide() {
  return {
    region: "",
    drgCompartment: "",
    vcnCompartment: "",
    drgId: "",
    vcnId: "",
    routeType: "SUBNET_CIDRS",
    drgs: [],
    vcns: [],
    subnets: [],
    attachments: [],
    rpcs: [],
    drgRouteTables: [],
    routeTables: [],
  };
}

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

function sideOf(el) {
  return el.closest("[data-side]").dataset.side;
}

function toast(message, error = false) {
  const el = $("#toast");
  el.textContent = message;
  el.classList.toggle("error", error);
  el.classList.remove("hidden");
  clearTimeout(toast._t);
  toast._t = setTimeout(() => el.classList.add("hidden"), 4500);
}

function banner(message) {
  const el = $("#banner");
  if (!message) {
    el.classList.add("hidden");
    return;
  }
  el.textContent = message;
  el.classList.remove("hidden");
}

let busyCount = 0;

function setBusy(message) {
  busyCount += 1;
  const panel = $("#working");
  $("#working-text").textContent = message || "Working, please wait…";
  panel.classList.remove("hidden");
  return () => {
    busyCount = Math.max(0, busyCount - 1);
    if (!busyCount) panel.classList.add("hidden");
  };
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  const data = await response.json().catch(() => ({ ok: false, error: response.statusText }));
  if (!response.ok || data.ok === false) {
    throw new Error(data.error || response.statusText);
  }
  return data;
}

function qs(params) {
  const search = new URLSearchParams();
  Object.entries(params).forEach(([key, value]) => {
    if (value !== undefined && value !== null && value !== "") search.set(key, value);
  });
  return search.toString();
}

function option(value, label, selected = false) {
  const el = document.createElement("option");
  el.value = value;
  el.textContent = label;
  el.selected = selected;
  return el;
}

function fillSelect(combo, items, placeholder, selected) {
  if (!combo) return;
  combo.dataset.placeholder = placeholder || "Type to filter";
  combo._items = items;
  if (!combo.querySelector("input")) mountCombo(combo);
  const keep = selected !== undefined ? selected : combo.dataset.value || "";
  setComboValue(combo, keep, { silent: true });
}

function mountCombo(combo) {
  const input = document.createElement("input");
  input.placeholder = combo.dataset.placeholder || "Type to filter";
  input.autocomplete = "off";
  const list = document.createElement("div");
  list.className = "combo-list hidden";
  combo.append(input, list);
  input.addEventListener("focus", () => renderComboList(combo, input.value));
  input.addEventListener("input", () => renderComboList(combo, input.value));
  input.addEventListener("keydown", (event) => {
    const buttons = [...list.querySelectorAll("button")];
    const current = buttons.findIndex((button) => button.classList.contains("active"));
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      const next = event.key === "ArrowDown" ? current + 1 : current - 1;
      const target = buttons[Math.max(0, Math.min(buttons.length - 1, next))];
      buttons.forEach((button) => button.classList.remove("active"));
      if (target) target.classList.add("active");
    } else if (event.key === "Enter") {
      event.preventDefault();
      const target = buttons.find((button) => button.classList.contains("active")) || buttons[0];
      if (target) {
        input.blur();
        target.click();
      }
    } else if (event.key === "Escape") {
      list.classList.add("hidden");
    }
  });
  document.addEventListener("click", (event) => {
    if (!combo.contains(event.target)) list.classList.add("hidden");
  });
}

function renderComboList(combo, query) {
  const list = combo.querySelector(".combo-list");
  const needle = query.trim().toLowerCase();
  const items = (combo._items || []).filter((item) => !needle || item.label.toLowerCase().includes(needle) || String(item.id).toLowerCase().includes(needle));
  list.replaceChildren();
  list.classList.remove("hidden");
  if (!items.length) {
    list.append(Object.assign(document.createElement("div"), { className: "empty", textContent: "No match" }));
    return;
  }
  items.slice(0, 80).forEach((item, index) => {
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = item.label;
    if (index === 0) button.classList.add("active");
    button.addEventListener("mousedown", (event) => event.preventDefault());
    button.addEventListener("click", () => setComboValue(combo, item.id, { force: true }));
    list.append(button);
  });
}

function setComboValue(combo, value, options = {}) {
  if (!combo) return;
  const item = (combo._items || []).find((candidate) => candidate.id === value);
  combo.dataset.value = item ? item.id : "";
  const input = combo.querySelector("input");
  if (input && (document.activeElement !== input || options.force)) input.value = item ? item.label : "";
  combo.querySelector(".combo-list")?.classList.add("hidden");
  if (!options.silent) combo.dispatchEvent(new Event("change"));
}

function comboValue(combo) {
  return combo?.dataset.value || "";
}

function compartmentLabel(id) {
  const found = state.compartments.find((item) => item.id === id);
  return found ? found.name : id.slice(0, 18);
}

function regionOptions() {
  const preferred = new Set(state.subscribed);
  return state.regions
    .slice()
    .sort((a, b) => Number(preferred.has(b)) - Number(preferred.has(a)) || a.localeCompare(b))
    .map((region) => ({
      id: region,
      label: preferred.has(region) ? `${region}  · subscribed` : region,
    }));
}

async function boot() {
  const profile = $("#profile");
  const requested = profile.value || localStorage.getItem("oci-drg-profile") || "";
  const data = await api(`/api/bootstrap?${qs({ profile: requested })}`);
  state.profile = data.profile || requested || "DEFAULT";
  state.regions = data.regions || [];
  state.subscribed = data.subscribed_regions || [];
  const names = data.profiles && data.profiles.length ? data.profiles : ["DEFAULT"];
  const current = names.includes(state.profile) ? state.profile : names[0];
  profile.replaceChildren(...names.map((name) => option(name, name, name === current)));
  profile.value = current;
  state.profile = current;
  localStorage.setItem("oci-drg-profile", current);
  $("#tenancy").textContent = data.tenancy ? `tenancy ${data.tenancy}` : "";
  banner(data.config_error);
  const compartments = await api(`/api/compartments?${qs({ profile: current })}`);
  state.compartments = compartments.compartments || [];
  $$(".side").forEach((panel) => initSide(panel));
}

function initSide(panel) {
  const side = panel.dataset.side;
  const bag = state.sides[side];
  fillSelect($(".region", panel), regionOptions(), "Type a region", bag.region);
  const compartments = state.compartments.map((item) => ({ id: item.id, label: item.name }));
  fillSelect($(".drg-compartment", panel), compartments, "Type a compartment", bag.drgCompartment);
  fillSelect($(".vcn-compartment", panel), compartments, "Type a compartment", bag.vcnCompartment);
  fillSelect($(".drg", panel), [], "Select a region and compartment", bag.drgId);
  fillSelect($(".vcn", panel), [], "Select a compartment", bag.vcnId);
  fillSelect($(".rpc", panel), [], "Select a DRG first", "");
  renderRpcBox(side);
  fillSelect($(".vcn-route-table", panel), [], "Select a VCN route table", "");
  if (!bag.region && state.subscribed.length) {
    const pick = side === "left" ? state.subscribed[0] : state.subscribed[1] || state.subscribed[0];
    setComboValue($(".region", panel), pick, { silent: true, force: true });
    bag.region = pick;
  }
}

async function loadDrgs(side, options = {}) {
  const panel = panelOf(side);
  const bag = state.sides[side];
  bag.region = comboValue($(".region", panel));
  bag.drgCompartment = comboValue($(".drg-compartment", panel));
  if (!bag.region || !bag.drgCompartment) return;

  const compartmentName = compartmentLabel(bag.drgCompartment);
  const done = options.announce
    ? setBusy(`Searching DRGs in compartment ${compartmentName}. Please wait…`)
    : null;
  let selected = "";
  try {
    if (options.resetSelection) {
      bag.drgId = "";
      bag.rpcs = [];
      bag.attachments = [];
      fillSelect($(".rpc", panel), [], "Select a DRG first", "");
      setComboValue($(".drg", panel), "", { silent: true, force: true });
      renderRpcBox(side);
    }

    const data = await api(`/api/drgs?${qs({
      profile: state.profile,
      region: bag.region,
      compartment_id: bag.drgCompartment,
    })}`);
    bag.drgs = data.drgs;

    const currentStillExists = data.drgs.some((item) => item.id === bag.drgId);
    selected = options.selectFirst
      ? (data.drgs[0]?.id || "")
      : (currentStillExists ? bag.drgId : (data.drgs[0]?.id || ""));
    bag.drgId = selected;

    fillSelect(
      $(".drg", panel),
      data.drgs.map((item) => ({ id: item.id, label: `${item.name}  (${item.state})` })),
      data.drgs.length ? "Select a DRG" : "No DRG in this compartment",
      selected,
    );
    setComboValue($(".drg", panel), selected, { silent: true, force: true });
    $(".drg-empty", panel).classList.toggle("hidden", data.drgs.length > 0);
    renderRpcBox(side);

    if (!data.drgs.length && !$(".drg-name", panel).value) {
      $(".drg-name", panel).value = `drg-${bag.region}`;
    }
    if (options.announce) {
      toast(data.drgs.length
        ? `Found ${data.drgs.length} DRG${data.drgs.length === 1 ? "" : "s"} in ${compartmentName}. The first one is selected.`
        : `No DRG found in ${compartmentName}. You can create one below.`);
    }
  } finally {
    if (done) done();
  }

  if (selected && options.loadRpc) {
    await refreshContext(side, { announce: true });
  }
}

async function createDrg(side) {
  const panel = panelOf(side);
  const bag = state.sides[side];
  bag.region = comboValue($(".region", panel));
  bag.drgCompartment = comboValue($(".drg-compartment", panel));
  const name = $(".drg-name", panel).value.trim() || `drg-${bag.region}`;
  if (!bag.region || !bag.drgCompartment) throw new Error("Select a region and a DRG compartment first");
  const created = await api("/api/drgs", {
    method: "POST",
    body: JSON.stringify({
      profile: state.profile,
      region: bag.region,
      compartment_id: bag.drgCompartment,
      display_name: name,
    }),
  });
  toast(`Created DRG ${name}`);
  bag.drgId = created.id;
  await loadDrgs(side);
  setComboValue($(".drg", panel), created.id, { silent: true, force: true });
  await refreshContext(side);
}

async function loadVcns(side) {
  const panel = panelOf(side);
  const bag = state.sides[side];
  bag.region = comboValue($(".region", panel));
  bag.vcnCompartment = comboValue($(".vcn-compartment", panel));
  if (!bag.region || !bag.vcnCompartment) return;
  const data = await api(`/api/vcns?${qs({ profile: state.profile, region: bag.region, compartment_id: bag.vcnCompartment })}`);
  bag.vcns = data.vcns;
  fillSelect(
    $(".vcn", panel),
    data.vcns.map((item) => ({ id: item.id, label: `${item.name}  ${item.cidrs.join(", ")}` })),
    "Select a VCN",
    bag.vcnId,
  );
}

async function loadSubnets(side) {
  const panel = panelOf(side);
  const bag = state.sides[side];
  bag.vcnId = comboValue($(".vcn", panel));
  bag.routeType = $(".route-type", panel).value;
  const list = $(".subnet-list", panel);
  list.replaceChildren();
  if (!bag.region || !bag.vcnCompartment || !bag.vcnId) {
    list.append(Object.assign(document.createElement("div"), { className: "empty", textContent: "Select a VCN first" }));
    return;
  }
  const [subnets, tables] = await Promise.all([
    api(`/api/subnets?${qs({ profile: state.profile, region: bag.region, compartment_id: bag.vcnCompartment, vcn_id: bag.vcnId })}`),
    api(`/api/route-table?${qs({ profile: state.profile, region: bag.region, route_table_id: currentVcn(side).default_route_table_id })}`).catch(() => null),
  ]);
  bag.subnets = subnets.subnets;
  const routeTables = await loadVcnRouteTables(side);
  bag.routeTables = routeTables;
  renderRouteType(side);
  if (!bag.subnets.length) {
    list.append(Object.assign(document.createElement("div"), { className: "empty", textContent: "No subnets in this compartment / VCN" }));
    return;
  }
  bag.subnets.forEach((subnet) => {
    subnetBlocks(subnet).forEach((block) => list.append(subnetCard(side, subnet, block.cidr)));
  });
  if (tables) bag.defaultRoutePreview = tables.route_table;
  draw();
}

async function loadVcnRouteTables(side) {
  const bag = state.sides[side];
  const vcn = currentVcn(side);
  if (!vcn) return [];
  const data = await api(`/api/route-tables?${qs({
    profile: state.profile,
    region: bag.region,
    compartment_id: bag.vcnCompartment,
    vcn_id: vcn.id,
  })}`);
  const tables = data.route_tables.map((item) => ({
    ...item,
    label: item.id === vcn.default_route_table_id ? `${item.name} (VCN default)` : item.name,
  }));
  const select = $(".vcn-route-table", panelOf(side));
  fillSelect(select, tables.map((item) => ({ id: item.id, label: item.label })), "Select a VCN route table", vcn.default_route_table_id);
  if (vcn.default_route_table_id) setComboValue(select, vcn.default_route_table_id, { silent: true, force: true });
  return tables;
}

function renderRouteType(side) {
  const panel = panelOf(side);
  const type = $(".route-type", panel).value;
  $(".vcn-rt-wrap", panel).classList.toggle("hidden", type !== "VCN_CIDRS");
}

function subnetBlocks(subnet) {
  const cidrs = (subnet.cidrs && subnet.cidrs.length) ? subnet.cidrs : [subnet.cidr].filter(Boolean);
  return cidrs.map((cidr) => ({ subnet, cidr }));
}

function blockByCidr(side, subnetId, cidr) {
  const subnet = subnetById(side, subnetId);
  if (!subnet) return null;
  return subnetBlocks(subnet).find((block) => block.cidr === cidr) || null;
}

function subnetCard(side, subnet, cidr) {
  const card = document.createElement("div");
  card.className = "subnet";
  card.draggable = true;
  card.dataset.side = side;
  card.dataset.id = subnet.id;
  card.dataset.cidr = cidr;
  card.innerHTML = `<strong>${escapeHtml(cidr)}</strong><span>${escapeHtml(subnet.name)} · ${escapeHtml(subnet.ad || "regional")}</span>`;
  card.addEventListener("dragstart", (event) => {
    state.drag = { side, subnetId: subnet.id, cidr };
    event.dataTransfer.setData("text/plain", `${side}:${subnet.id}:${cidr}`);
    event.dataTransfer.effectAllowed = "link";
    card.classList.add("dragging");
  });
  card.addEventListener("dragend", () => {
    card.classList.remove("dragging");
    state.drag = null;
  });
  card.addEventListener("dragover", (event) => {
    if (state.drag && state.drag.side !== side) event.preventDefault();
  });
  card.addEventListener("drop", (event) => {
    event.preventDefault();
    if (!state.drag || state.drag.side === side) return;
    createLink(state.drag, { side, subnetId: subnet.id, cidr });
  });
  card.addEventListener("click", () => card.classList.toggle("selected"));
  return card;
}

function panelOf(side) {
  return $(`.side[data-side="${side}"]`);
}

function currentVcn(side) {
  const bag = state.sides[side];
  return bag.vcns.find((item) => item.id === bag.vcnId);
}

function currentDrg(side) {
  const bag = state.sides[side];
  return bag.drgs.find((item) => item.id === bag.drgId);
}

function subnetById(side, id) {
  return state.sides[side].subnets.find((item) => item.id === id);
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (ch) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch]));
}

async function refreshContext(side, options = {}) {
  const bag = state.sides[side];
  const panel = panelOf(side);
  bag.drgId = comboValue($(".drg", panel));
  renderRpcBox(side);
  if (!bag.region || !bag.drgCompartment || !bag.drgId) return;
  const done = options.announce ? setBusy(`Searching RPCs in the ${bag.region} DRG. Please wait…`) : null;
  try {
    const data = await api(`/api/drg-context?${qs({
      profile: state.profile,
      region: bag.region,
      compartment_id: bag.drgCompartment,
      vcn_compartment_id: bag.vcnCompartment,
      drg_id: bag.drgId,
    })}`);
    bag.attachments = data.attachments;
    bag.rpcs = data.rpcs;
    bag.drgRouteTables = data.drg_route_tables;
    const current = comboValue($(".rpc", panel));
    const stillThere = bag.rpcs.some((item) => item.id === current);
    const pick = stillThere ? current : (bag.rpcs[0]?.id || "");
    fillSelect(
      $(".rpc", panel),
      bag.rpcs.map((item) => ({ id: item.id, label: `${item.name}  ${item.peering_status || item.state}` })),
      bag.rpcs.length ? "Select an RPC on this DRG" : "No RPC on this DRG yet",
      pick,
    );
    if (pick) setComboValue($(".rpc", panel), pick, { silent: true, force: true });
    renderRpcBox(side);
    renderRpcStatus(side);
    renderAttachmentStatus(side);
    draw();
    if (options.announce) {
      toast(bag.rpcs.length ? `Found ${bag.rpcs.length} RPC${bag.rpcs.length === 1 ? "" : "s"} on this DRG. The first one is selected.` : "No RPC on this DRG yet. Create one below.");
    }
  } finally {
    if (done) done();
  }
}

function renderRpcBox(side) {
  const panel = panelOf(side);
  const visible = Boolean(comboValue($(".drg", panel)));
  $(".rpc-box", panel).classList.toggle("hidden", !visible);
}

function selectedRpc(side) {
  const bag = state.sides[side];
  const id = comboValue($(".rpc", panelOf(side)));
  return bag.rpcs.find((item) => item.id === id);
}

function vcnAttachment(side) {
  const bag = state.sides[side];
  return bag.attachments.find((item) => item.type === "VCN" && item.network_id === bag.vcnId);
}

function rpcAttachment(side) {
  const rpc = selectedRpc(side);
  if (!rpc) return null;
  return state.sides[side].attachments.find((item) => item.type === "REMOTE_PEERING_CONNECTION" && (item.network_id === rpc.id || item.id === rpc.id));
}

function rpcStatusLabel(rpc) {
  if (!rpc) return "NONE";
  if (rpc.lifecycle_state && rpc.lifecycle_state !== "AVAILABLE") return rpc.lifecycle_state;
  return rpc.peering_status || "NEW";
}

function renderRpcStatus(side) {
  const rpc = selectedRpc(side);
  const panel = panelOf(side);
  const el = $(".rpc-status", panel);
  const badge = $(".rpc-badge", panel);
  const label = rpcStatusLabel(rpc);
  badge.textContent = label;
  badge.className = "badge rpc-badge";
  if (label === "PEERED") badge.classList.add("ok");
  else if (label === "PENDING" || label === "PROVISIONING" || label === "REVOKED") badge.classList.add("wait");
  else if (label === "NONE" || label === "TERMINATING" || label === "TERMINATED") badge.classList.add("bad");
  if (!rpc) {
    el.textContent = "No RPC selected";
    return;
  }
  const peer = rpc.peer_region_name ? `peer ${rpc.peer_region_name}` : "no peer yet";
  el.textContent = `${label} · ${peer} · ${rpc.state || ""} · ${rpc.id.slice(-10)}`;
}

function renderAttachmentStatus(side) {
  const attachment = vcnAttachment(side);
  const el = $(".attach-status", panelOf(side));
  if (!attachment) {
    el.textContent = "This VCN is not attached to the selected DRG";
    return;
  }
  el.textContent = `${attachment.state} · ${attachment.vcn_route_type || "VCN"} · ${attachment.name || attachment.id}`;
}

async function createRpc(side) {
  const bag = state.sides[side];
  const panel = panelOf(side);
  const name = $(".rpc-name", panel).value.trim() || `rpc-${bag.region}`;
  if (!bag.drgId) throw new Error("Select a DRG first");
  const created = await api("/api/rpc", {
    method: "POST",
    body: JSON.stringify({
      profile: state.profile,
      region: bag.region,
      compartment_id: bag.drgCompartment,
      drg_id: bag.drgId,
      display_name: name,
    }),
  });
  toast(`Created RPC ${name}`);
  await refreshContext(side);
  setComboValue($(".rpc", panel), created.id, { silent: true, force: true });
  renderRpcStatus(side);
}

async function ensureVcnAttachment(side) {
  const done = setBusy(`Checking the VCN attachment in ${state.sides[side].region}…`);
  try {
    await refreshContext(side);
  const existing = vcnAttachment(side);
  if (existing) return existing;
    if (!state.sides[side].vcnId) throw new Error(`Select a VCN in ${state.sides[side].region} before linking CIDRs`);
    $("#working-text").textContent = `Creating the VCN attachment in ${state.sides[side].region}. Please wait…`;
    await attachVcn(side, { quiet: true });
    return vcnAttachment(side);
  } finally {
    done();
  }
}

async function attachVcn(side, options = {}) {
  const bag = state.sides[side];
  const panel = panelOf(side);
  const routeType = $(".route-type", panel).value;
  if (vcnAttachment(side) && !options.force) {
    renderAttachmentStatus(side);
    return vcnAttachment(side);
  }
  if (!bag.drgId || !bag.vcnId) throw new Error("Select a DRG and a VCN first");
  const body = {
    profile: state.profile,
    region: bag.region,
    drg_id: bag.drgId,
    vcn_id: bag.vcnId,
    vcn_route_type: routeType,
    display_name: `${currentVcn(side)?.name || "vcn"}-to-drg`,
  };
  if (routeType === "VCN_CIDRS") body.route_table_id = comboValue($(".vcn-route-table", panel));
  await api("/api/attachments", { method: "POST", body: JSON.stringify(body) });
  if (!options.quiet) toast(`Attached VCN in ${bag.region}, route type = ${routeType}`);
  await refreshContext(side);
  return vcnAttachment(side);
}

async function detachVcn(side) {
  const attachment = vcnAttachment(side);
  if (!attachment) throw new Error("This VCN has no attachment to detach");
  if (!confirm(`Detach the VCN attachment in ${state.sides[side].region}?\n${attachment.id}`)) return;
  await api("/api/attachments", {
    method: "DELETE",
    body: JSON.stringify({ profile: state.profile, region: state.sides[side].region, attachment_id: attachment.id }),
  });
  toast("VCN attachment detached");
  await refreshContext(side);
}

async function createLink(from, to) {
  const left = from.side === "left" ? from : to;
  const right = from.side === "right" ? from : to;
  if (left.side === right.side) return;
  await ensureVcnAttachment("left");
  await ensureVcnAttachment("right");
  const key = `${left.subnetId}|${left.cidr}|${right.subnetId}|${right.cidr}`;
  state.links = state.links || [];
  if (!state.links.some((link) => link.key === key)) {
    state.links.push({ key, left, right });
  }
  state.link = state.links[state.links.length - 1];
  $("#workbench").classList.remove("hidden");
  renderPair();
  draw();
  const results = await Promise.allSettled([
    ensureImports("left", { wait: true }),
    ensureImports("right", { wait: true }),
  ]);
  const failed = results.filter((item) => item.status === "rejected").map((item) => item.reason?.message || String(item.reason));
  if (failed.length) toast(failed.join(" | "), true);
}

function other(side) {
  return side === "left" ? "right" : "left";
}

function linkEnds() {
  return (state.links || []).map((link) => ({
    left: blockByCidr("left", link.left.subnetId, link.left.cidr),
    right: blockByCidr("right", link.right.subnetId, link.right.cidr),
    link,
  })).filter((item) => item.left && item.right);
}

function renderPair() {
  const pairs = linkEnds();
  if (!pairs.length) return;
  const leftBag = state.sides.left;
  const rightBag = state.sides.right;
  $("#pair-title").textContent = pairs.map((pair) => `${leftBag.region} ${pair.left.cidr} ↔ ${rightBag.region} ${pair.right.cidr}`).join("   ·   ");
  $("#cidr-pair").innerHTML = pairs.map((pair) => `<span class="chip">${escapeHtml(pair.left.cidr)} ↔ ${escapeHtml(pair.right.cidr)}</span>`).join("");
  renderRouteEditors();
  renderSecurityEditors();
  renderDrgTables();
}

function renderDrgTables() {
  if (state.drgRouteView?.left) $("#rt-left").textContent = state.drgRouteView.left;
  else $("#rt-left").textContent = describeDrgTable("left");
  if (state.drgRouteView?.right) $("#rt-right").textContent = state.drgRouteView.right;
  else $("#rt-right").textContent = describeDrgTable("right");
}

function describeDrgTable(side) {
  const attachment = vcnAttachment(side);
  const tables = state.sides[side].drgRouteTables.filter((item) => !item.is_default_name);
  const bound = tables.find((item) => item.id === attachment?.drg_route_table_id);
  if (!attachment) return "The VCN is not attached yet.";
  const rpc = rpcAttachment(side);
  const rpcLine = rpc ? `RPC attachment ${rpc.id}` : "RPC attachment not found yet. Connect the RPC first.";
  return `VCN attachment ${attachment.id}\nroute table ${attachment.drg_route_table_id || "none"}\n${rpcLine}`;
}

function peerCidrsFor(side) {
  const cidrs = new Set();
  groupedLinks(side).forEach((group) => group.peerCidrs.forEach((cidr) => cidrs.add(cidr)));
  return [...cidrs];
}

function localCidrsFor(side) {
  const cidrs = new Set();
  groupedLinks(side).forEach((group) => group.localCidrs.forEach((cidr) => cidrs.add(cidr)));
  return [...cidrs];
}

function formatDrgRoutes(tables, imports = []) {
  const importByType = Object.fromEntries((imports || []).map((item) => [item.attachment_type, item]));
  return (tables || []).map((table) => {
    const routes = (table.routes || []).map((route) => {
      const flags = [route.route_type, route.route_provenance, route.is_conflict ? "CONFLICT" : "", route.is_blackhole ? "BLACKHOLE" : ""].filter(Boolean).join(" · ");
      return `${route.destination || "*"}  →  ${shortId(route.next_hop_drg_attachment_id)}  ${flags}`;
    });
    const expectedLabel = table.route_role === "REMOTE_DESTINATIONS" ? "peer CIDRs" : "local CIDRs";
    const expected = `${expectedLabel}: ${(table.expected_cidrs || []).join(", ") || "none"}`;
    const learned = `${expectedLabel} learned: ${(table.learned || []).join(", ") || "none"}`;
    const missing = (table.missing || []).length ? `missing: ${table.missing.join(", ")}` : "all expected CIDRs are present";
    const match = importByType[table.attachment_type];
    const matchLine = match
      ? `import distribution: ${match.import_drg_route_distribution_id}\n${match.message}`
      : `import distribution: ${table.import_drg_route_distribution_id || "none"}\nMATCH_ALL: ${table.match_all ? "yes" : "no"}`;
    return `${table.attachment_type} attachment route table\n${table.drg_route_table_name || table.drg_route_table_id}\n${table.drg_route_table_id}\n${expected}\n${learned}\n${missing}\n${matchLine}\n\nall routes (${routes.length}):\n${routes.join("\n") || "no routes in this DRG route table"}`;
  }).join("\n\n--------------------------------\n\n");
}

function shortId(ocid) {
  if (!ocid) return "none";
  return ocid.length > 22 ? `${ocid.slice(0, 18)}...${ocid.slice(-6)}` : ocid;
}

async function readAttachmentRouteTables(side, options = {}) {
  const bag = state.sides[side];
  const attachment = vcnAttachment(side);
  const rpc = selectedRpc(side);
  const rpcAttach = rpcAttachment(side);
  const localCidrs = localCidrsFor(side);
  const peerCidrs = peerCidrsFor(side);
  if (!attachment || !rpc || !localCidrs.length || !peerCidrs.length) return null;
  const result = await api("/api/drg-attachment-routes", {
    method: "POST",
    body: JSON.stringify({
      profile: state.profile,
      region: bag.region,
      compartment_id: bag.drgCompartment,
      drg_id: bag.drgId,
      vcn_attachment_id: attachment.id,
      rpc_attachment_id: rpcAttach?.id,
      rpc_id: rpc.id,
      attachment_wait_seconds: options.wait ? 60 : 0,
      wait_seconds: options.wait ? 45 : 0,
      ensure_match_all: Boolean(options.ensureMatchAll),
      local_cidrs: localCidrs,
      peer_cidrs: peerCidrs,
    }),
  });
  state.drgRouteView = state.drgRouteView || {};
  state.drgRouteView[side] = formatDrgRoutes(result.tables, result.import_results || []);
  if ((result.import_errors || []).length) {
    state.drgRouteView[side] += `\n\nMATCH_ALL errors:\n${result.import_errors.map((item) => `${item.attachment_type}: ${item.error}`).join("\n")}`;
  }
  $(`#rt-${side}`).textContent = state.drgRouteView[side];
  return result;
}

async function ensureImports(side, options = {}) {
  const bag = state.sides[side];
  const done = options.quiet ? null : setBusy(`Ensuring MATCH_ALL and reading attachment routes in ${bag.region}. Please wait…`);
  try {
    await refreshContext(side);
    const attachment = vcnAttachment(side);
    const rpc = selectedRpc(side);
    if (!attachment) throw new Error(`Attach the VCN in ${bag.region} first`);
    if (!rpc) throw new Error(`Select an RPC on the ${bag.region} DRG first`);
    const result = await readAttachmentRouteTables(side, {
      wait: Boolean(options.wait),
      ensureMatchAll: true,
    });
    const notes = [];
    for (const item of result?.import_results || []) {
      const kind = item.attachment_type === "VCN" ? "VCN" : "RPC";
      notes.push(item.changed ? `${kind} MATCH_ALL added` : `${kind} MATCH_ALL already present`);
    }
    for (const item of result?.import_errors || []) {
      notes.push(`${item.attachment_type} MATCH_ALL error: ${item.error}`);
    }
    for (const table of result?.tables || []) {
      const kind = table.attachment_type === "VCN" ? "VCN" : "RPC";
      const cidrKind = table.route_role === "REMOTE_DESTINATIONS" ? "peer" : "local";
      notes.push(table.missing.length ? `${kind} missing ${cidrKind} CIDRs ${table.missing.join(", ")}` : `${kind} learned all ${cidrKind} CIDRs`);
    }
    if (!options.quiet && notes.length) toast(`${bag.region}: ${notes.join("; ")}`);
    await refreshContext(side);
    return result;
  } finally {
    if (done) done();
  }
}

function groupedLinks(side) {
  const groups = new Map();
  linkEnds().forEach((pair) => {
    const local = pair[side];
    const peer = pair[other(side)];
    const key = local.subnet.id;
    if (!groups.has(key)) groups.set(key, { subnet: local.subnet, localCidrs: new Set(), peerCidrs: new Set(), peerNames: new Set() });
    const group = groups.get(key);
    group.localCidrs.add(local.cidr);
    group.peerCidrs.add(peer.cidr);
    group.peerNames.add(`${state.sides[other(side)].region} ${peer.cidr}`);
  });
  return [...groups.values()];
}

function renderRouteEditors() {
  const root = $("#route-editors");
  root.replaceChildren();
  ["left", "right"].forEach((side) => {
    groupedLinks(side).forEach((group) => root.append(routeEditor(side, group)));
  });
}

function routeEditor(side, group) {
  const box = document.createElement("div");
  box.className = "editor";
  box.innerHTML = `<h4>${escapeHtml(state.sides[side].region)} · ${escapeHtml(group.subnet.name)}</h4><p class="hint">Local ${escapeHtml([...group.localCidrs].join(", "))} · route table ${escapeHtml(group.subnet.route_table_id)}</p><div class="rules">Loading…</div>`;
  loadRouteEditor(box, side, group);
  return box;
}

async function loadRouteEditor(box, side, group) {
  const bag = state.sides[side];
  const subnet = group.subnet;
  const peerCidrList = [...group.peerCidrs];
  const data = await api(`/api/route-table?${qs({ profile: state.profile, region: bag.region, route_table_id: subnet.route_table_id })}`);
  const rules = data.route_table.rules || [];
  const peerCidrs = group.peerCidrs;
  const list = $(".rules", box);
  list.replaceChildren();
  if (!rules.length) list.append(Object.assign(document.createElement("div"), { className: "empty", textContent: "Route table is empty" }));
  rules.forEach((rule) => {
    const row = document.createElement("div");
    row.className = "rule";
    const hit = peerCidrs.has(rule.destination);
    row.innerHTML = `<input type="checkbox" ${hit ? "checked" : ""} disabled /><code>${escapeHtml(rule.destination)}</code><span>${hit ? "dragged peer CIDR" : escapeHtml(rule.network_entity_id || "")}</span>`;
    list.append(row);
  });
  const missing = peerCidrList.filter((cidr) => !rules.some((rule) => rule.destination === cidr && rule.network_entity_id === bag.drgId));
  const actions = document.createElement("div");
  actions.className = "row";
  const add = document.createElement("button");
  add.textContent = missing.length ? `Add peer ${missing.join(", ")}` : "Peer CIDRs already present";
  add.disabled = !missing.length || !bag.drgId;
  add.addEventListener("click", async () => {
    await run(() => api("/api/route-rules", {
      method: "POST",
      body: JSON.stringify({
        profile: state.profile,
        region: bag.region,
        route_table_id: subnet.route_table_id,
        rules: missing.map((destination) => ({
          destination,
          network_entity_id: bag.drgId,
          description: `to ${[...group.peerNames].join(", ")}`,
        })),
      }),
    }));
    await loadRouteEditor(box, side, group);
  });
  const remove = document.createElement("button");
  remove.className = "ghost";
  remove.textContent = "Remove these peer routes";
  remove.addEventListener("click", async () => {
    await run(() => api("/api/route-rules", {
      method: "DELETE",
      body: JSON.stringify({
        profile: state.profile,
        region: bag.region,
        route_table_id: subnet.route_table_id,
        destinations: peerCidrList,
        network_entity_id: bag.drgId,
      }),
    }));
    await loadRouteEditor(box, side, group);
  });
  actions.append(add, remove);
  box.querySelector(".actions")?.remove();
  actions.classList.add("actions");
  box.append(actions);
}

function renderSecurityEditors() {
  const root = $("#sl-editors");
  root.replaceChildren();
  ["left", "right"].forEach((side) => {
    groupedLinks(side).forEach((group) => root.append(securityEditor(side, group)));
  });
}

function securityEditor(side, group) {
  const subnet = group.subnet;
  const box = document.createElement("div");
  box.className = "editor";
  const ids = subnet.security_list_ids || [];
  box.innerHTML = `<h4>${escapeHtml(state.sides[side].region)} · ${escapeHtml([...group.localCidrs].join(", "))}</h4><p class="hint">${escapeHtml(subnet.name)}</p>`;
  if (!ids.length) {
    box.append(Object.assign(document.createElement("div"), { className: "empty", textContent: "This subnet has no security list" }));
    return box;
  }
  ids.forEach((id) => {
    const block = document.createElement("div");
    block.innerHTML = `<p class="hint">${escapeHtml(id)}</p><div class="rules">Loading…</div>`;
    box.append(block);
    loadSecurityBlock(block, side, id, group);
  });
  return box;
}

async function loadSecurityBlock(block, side, securityListId, group) {
  const peerCidrList = [...group.peerCidrs];
  const bag = state.sides[side];
  let data;
  try {
    data = await api(`/api/security-list?${qs({ profile: state.profile, region: bag.region, security_list_id: securityListId })}`);
  } catch (error) {
    const rules = $(".rules", block);
    if (rules) rules.textContent = `Failed to load: ${error.message}`;
    return;
  }
  const list = data.security_list;
  const rules = $(".rules", block);
  rules.replaceChildren();
  const peerCidrs = group.peerCidrs;
  const wantIngress = $("#sl-ingress").checked;
  const wantEgress = $("#sl-egress").checked;
  const ingressCidrs = new Set((list.ingress || []).map((rule) => rule.source));
  const egressCidrs = new Set((list.egress || []).map((rule) => rule.destination));
  const shown = [...(list.ingress || []).map((rule) => ({ ...rule, dir: "in", cidr: rule.source })), ...(list.egress || []).map((rule) => ({ ...rule, dir: "out", cidr: rule.destination }))];
  const relevant = shown.filter((rule) => peerCidrs.has(rule.cidr));
  (relevant.length ? relevant : shown.slice(0, 6)).forEach((rule) => {
    const row = document.createElement("div");
    row.className = "rule";
    row.innerHTML = `<span>${rule.dir}</span><code>${escapeHtml(rule.cidr)}</code><span>${escapeHtml(rule.protocol)}</span>`;
    rules.append(row);
  });
  if (!shown.length) rules.append(Object.assign(document.createElement("div"), { className: "empty", textContent: "No rules yet" }));
  const missing = peerCidrList.filter((cidr) => (wantIngress && !ingressCidrs.has(cidr)) || (wantEgress && !egressCidrs.has(cidr)));
  const button = document.createElement("button");
  button.textContent = missing.length ? `Allow peer ${missing.join(", ")}` : "Peer CIDRs already present";
  button.disabled = !missing.length;
  button.addEventListener("click", async () => {
    const protocol = $("#sl-protocol").value;
    const port = $("#sl-port").value.trim();
    const range = parsePort(port);
    const options = range ? { destination_port_range: range } : {};
    const body = {
      profile: state.profile,
      region: bag.region,
      security_list_id: securityListId,
      ingress: [],
      egress: [],
    };
    missing.forEach((cidr) => {
      if ($("#sl-ingress").checked) body.ingress.push({ source: cidr, protocol, description: `from peer ${cidr}`, ...options });
      if ($("#sl-egress").checked) body.egress.push({ destination: cidr, protocol, description: `to peer ${cidr}`, ...options });
    });
    await run(() => api("/api/security-rules", { method: "POST", body: JSON.stringify(body) }));
    await loadSecurityBlock(block, side, securityListId, group);
  });
  block.append(button);
}

function parsePort(text) {
  if (!text) return null;
  const [min, max] = text.split("-").map((part) => part.trim());
  return { min: Number(min), max: Number(max || min) };
}

async function connectRpc() {
  const left = selectedRpc("left");
  const right = selectedRpc("right");
  if (!left || !right) throw new Error("Select an RPC on both sides first");
  if (state.sides.left.region === state.sides.right.region) throw new Error("The two RPCs must be in different regions");
  await api("/api/rpc/connect", {
    method: "POST",
    body: JSON.stringify({
      profile: state.profile,
      region: state.sides.left.region,
      rpc_id: left.id,
      peer_id: right.id,
      peer_region_name: state.sides.right.region,
    }),
  });
  toast("Connect requested from Region A. Refresh to see PEERED");
  await refreshContext("left");
  await refreshContext("right");
}

async function deleteRpc(side) {
  const rpc = selectedRpc(side);
  if (!rpc) throw new Error("No RPC selected");
  if (!confirm(`Delete RPC ${rpc.name} in ${state.sides[side].region}?\nA PEERED connection will drop. Create a new RPC to establish it again.`)) return;
  await api("/api/rpc", {
    method: "DELETE",
    body: JSON.stringify({ profile: state.profile, region: state.sides[side].region, rpc_id: rpc.id }),
  });
  toast(`Deleted RPC in ${state.sides[side].region}`);
  await refreshContext(side);
}

async function disconnectRpc() {
  const left = selectedRpc("left");
  const right = selectedRpc("right");
  const victims = [left, right].filter(Boolean);
  if (!victims.length) throw new Error("No RPC selected");
  if (!confirm("Delete the selected RPCs on both sides? A PEERED connection will be torn down.")) return;
  for (const rpc of victims) {
    const side = rpc === left ? "left" : "right";
    await api("/api/rpc", {
      method: "DELETE",
      body: JSON.stringify({ profile: state.profile, region: state.sides[side].region, rpc_id: rpc.id }),
    });
  }
  toast("RPCs deleted. Create new ones, then Connect RPC to establish again.");
  await refreshContext("left");
  await refreshContext("right");
}

async function reestablishRpc() {
  const left = selectedRpc("left");
  const right = selectedRpc("right");
  if (!left || !right) throw new Error("Select an RPC on both sides first");
  if (left.peering_status === "PEERED" || right.peering_status === "PEERED") {
    if (!confirm("This peering is already PEERED. Delete both RPCs and create a new pair?")) return;
    await disconnectRpc();
    return;
  }
  await connectRpc();
}

function draw(options = {}) {
  const keepPosition = options.keepPosition && $$(".node").length > 0;
  if (keepPosition) {
    $$(".node").forEach((node) => {
      state.nodeTops[`${node.dataset.side}:${node.dataset.index}`] = node.style.top;
    });
  }
  const leftLane = $("#lane-left");
  const rightLane = $("#lane-right");
  leftLane.replaceChildren();
  rightLane.replaceChildren();
  const pairs = linkEnds();
  pairs.forEach((pair, index) => {
    const y = pairs.length === 1 ? 0.42 : 0.18 + index * (0.64 / Math.max(pairs.length - 1, 1));
    placeNode("left", pair.left, y, index);
    placeNode("right", pair.right, y, index);
  });
  const leftRpc = selectedRpc("left");
  const rightRpc = selectedRpc("right");
  const leftAttachment = vcnAttachment("left");
  const rightAttachment = vcnAttachment("right");
  if (leftAttachment) placeAttachmentNode("left", leftAttachment);
  if (rightAttachment) placeAttachmentNode("right", rightAttachment);
  const peered = leftRpc?.peering_status === "PEERED" || rightRpc?.peering_status === "PEERED";
  const pending = leftRpc && rightRpc;
  const hub = $("#hub");
  hub.classList.toggle("peered", peered);
  hub.classList.toggle("pending", pending && !peered);
  $("#hub-state").textContent = peered ? "PEERED" : pending ? "Ready to connect" : "Not connected";
  requestAnimationFrame(drawWires);
  const summary = $("#link-summary");
  if (!pairs.length) {
    summary.textContent = "No CIDR pair yet. Drag one CIDR block onto a CIDR block in the other region.";
    return;
  }
  summary.textContent = pairs.map((pair) => `${state.sides.left.region} ${pair.left.cidr} ↔ ${state.sides.right.region} ${pair.right.cidr}`).join("   ·   ");
}

function placeAttachmentNode(side, attachment) {
  const lane = side === "left" ? $("#lane-left") : $("#lane-right");
  const node = document.createElement("div");
  node.className = `node attachment ${side}`;
  node.style.top = "16px";
  node.style.left = side === "left" ? "18px" : "auto";
  node.style.right = side === "right" ? "18px" : "auto";
  node.innerHTML = `<b>VCN attachment</b><small>${escapeHtml(attachment.state || "")} · ${escapeHtml(attachment.vcn_route_type || "VCN")}</small>`;
  lane.append(node);
}

function placeNode(side, block, yRatio, index) {
  if (!block) return;
  const lane = side === "left" ? $("#lane-left") : $("#lane-right");
  const node = document.createElement("div");
  node.className = `node ${side}`;
  node.dataset.side = side;
  node.dataset.index = String(index);
  const saved = state.nodeTops[`${side}:${index}`];
  node.style.top = saved || `${Math.round(lane.clientHeight * yRatio)}px`;
  node.style.left = side === "left" ? "18px" : "auto";
  node.style.right = side === "right" ? "18px" : "auto";
  node.innerHTML = `<b>${escapeHtml(block.cidr)}</b><small>${escapeHtml(block.subnet.name)}</small>`;
  node.addEventListener("mousedown", (event) => startNodeDrag(event, node));
  lane.append(node);
}

function startNodeDrag(event, node) {
  const lane = node.parentElement;
  const originY = event.clientY - node.offsetTop;
  function move(ev) {
    const next = Math.max(12, Math.min(lane.clientHeight - node.offsetHeight - 12, ev.clientY - originY));
    node.style.top = `${next}px`;
    drawWires();
  }
  function up() {
    window.removeEventListener("mousemove", move);
    window.removeEventListener("mouseup", up);
  }
  window.addEventListener("mousemove", move);
  window.addEventListener("mouseup", up);
}

function drawWires() {
  const svg = $("#wires");
  const canvas = $("#canvas");
  svg.setAttribute("viewBox", `0 0 ${canvas.clientWidth} ${canvas.clientHeight}`);
  svg.replaceChildren();
  const hub = $("#hub").getBoundingClientRect();
  const base = canvas.getBoundingClientRect();
  const hubX = hub.left - base.left + hub.width / 2;
  const hubY = hub.top - base.top + hub.height / 2;
  $$(".node").forEach((node) => {
    if (node.classList.contains("attachment")) return;
    const rect = node.getBoundingClientRect();
    const x = node.classList.contains("left") ? rect.right - base.left : rect.left - base.left;
    const y = rect.top - base.top + rect.height / 2;
    const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
    const mid = (x + hubX) / 2;
    path.setAttribute("d", `M ${x} ${y} C ${mid} ${y}, ${mid} ${hubY}, ${hubX} ${hubY}`);
    path.setAttribute("fill", "none");
    path.setAttribute("stroke", node.classList.contains("left") ? "#0f6e6b" : "#9a4d24");
    path.setAttribute("stroke-width", "2.5");
    svg.append(path);
  });
}

async function run(fn) {
  try {
    await fn();
  } catch (error) {
    toast(error.message, true);
    throw error;
  }
}

function wire() {
  $("#profile").addEventListener("change", () => {
    localStorage.setItem("oci-drg-profile", $("#profile").value);
    run(boot);
  });
  $("#reload").addEventListener("click", () => run(boot));
  $("#connect").addEventListener("click", () => run(connectRpc));
  $("#reestablish").addEventListener("click", () => run(reestablishRpc));
  $("#disconnect").addEventListener("click", () => run(disconnectRpc));
  $("#clear-link").addEventListener("click", () => {
    state.links = [];
    state.link = null;
    state.drgRouteView = { left: "", right: "" };
    state.nodeTops = {};
    $("#workbench").classList.add("hidden");
    draw();
  });
  $("#make-rt-left")?.addEventListener("click", () => run(() => ensureImports("left")));
  $("#make-rt-right")?.addEventListener("click", () => run(() => ensureImports("right")));
  $$(".side").forEach((panel) => {
    const side = panel.dataset.side;
    $(".region", panel).addEventListener("change", () => run(async () => {
      state.sides[side].region = comboValue($(".region", panel));
      await loadDrgs(side, { resetSelection: true });
      await loadVcns(side);
    }));
    $(".drg-compartment", panel).addEventListener("change", () => run(() => loadDrgs(side, {
      announce: true,
      resetSelection: true,
      selectFirst: true,
      loadRpc: true,
    })));
    $(".drg", panel).addEventListener("change", () => run(async () => {
      renderRpcBox(side);
      await refreshContext(side, { announce: true });
    }));
    $(".vcn-compartment", panel).addEventListener("change", () => run(() => loadVcns(side)));
    $(".vcn", panel).addEventListener("change", () => run(() => loadSubnets(side)));
    $(".route-type", panel).addEventListener("change", () => renderRouteType(side));
    $(".create-rpc", panel).addEventListener("click", () => run(() => createRpc(side)));
    $(".create-drg", panel).addEventListener("click", () => run(() => createDrg(side)));
    $(".refresh-side", panel).addEventListener("click", () => run(() => refreshContext(side)));
    $(".delete-rpc", panel).addEventListener("click", () => run(() => deleteRpc(side)));
    $(".attach", panel).addEventListener("click", () => run(() => attachVcn(side)));
    $(".detach", panel).addEventListener("click", () => run(() => detachVcn(side)));
    $(".rpc", panel).addEventListener("change", () => renderRpcStatus(side));
  });
  const canvas = $("#canvas");
  canvas.addEventListener("dragover", (event) => {
    if (state.drag) event.preventDefault();
  });
  canvas.addEventListener("drop", (event) => {
    if (!state.drag) return;
    event.preventDefault();
    toast("Drop the CIDR onto a CIDR card on the other side");
  });
  window.addEventListener("resize", drawWires);
  setInterval(() => {
    liveRefresh().catch((error) => {
      stamp(false);
      console.error(error);
    });
  }, 5000);
}

function stamp(ok) {
  const clock = $("#live-clock");
  const now = new Date().toLocaleTimeString();
  clock.textContent = ok ? `updated ${now}` : `refresh failed ${now}`;
}

async function liveRefresh() {
  if (!$("#live").checked || state.refreshing || state.drag) return;
  if (document.activeElement && ["INPUT", "TEXTAREA"].includes(document.activeElement.tagName)) return;
  state.refreshing = true;
  try {
    await refreshContext("left");
    await refreshContext("right");
    await refreshResourceStates();
    if ((state.links || []).length && !$("#workbench").classList.contains("hidden")) {
      await Promise.allSettled([
        readAttachmentRouteTables("left"),
        readAttachmentRouteTables("right"),
      ]);
      renderDrgTables();
      if ($("#sl-editors .rules")?.textContent?.startsWith("Failed to load")) renderSecurityEditors();
    }
    draw({ keepPosition: true });
    stamp(true);
  } catch (error) {
    stamp(false);
    throw error;
  } finally {
    state.refreshing = false;
  }
}

async function refreshResourceStates() {
  for (const side of ["left", "right"]) {
    const bag = state.sides[side];
    const panel = panelOf(side);
    if (!bag.region || !bag.drgCompartment) continue;
    const drgs = await api(`/api/drgs?${qs({ profile: state.profile, region: bag.region, compartment_id: bag.drgCompartment })}`);
    const selected = comboValue($(".drg", panel));
    const changed = JSON.stringify(drgs.drgs.map((item) => [item.id, item.state])) !== JSON.stringify(bag.drgs.map((item) => [item.id, item.state]));
    if (changed) {
      bag.drgs = drgs.drgs;
      fillSelect($(".drg", panel), drgs.drgs.map((item) => ({ id: item.id, label: `${item.name}  (${item.state})` })), drgs.drgs.length ? "Type a DRG" : "No DRG in this compartment", selected);
      if (selected) setComboValue($(".drg", panel), selected, { silent: true, force: true });
      $(".drg-empty", panel).classList.toggle("hidden", drgs.drgs.length > 0);
    }
  }
}

wire();
run(boot).then(draw).catch((error) => banner(error.message));
