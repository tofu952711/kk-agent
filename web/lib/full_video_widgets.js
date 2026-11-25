const PROTOCOLS = new Set(["deepseek-messages", "openai-completions", "codez"]);
const EFFORTS = new Set(["high", "medium", "low", "auto"]);

// Give each prompt its own minimum instead of sharing a tiny editor area.
export function resizeFullVideoInputs(node) {
  for (const credential of node.widgets || []) {
    if (credential.name === "review_base_url") credential.label = "数值2";
    if (["api_key", "review_api_key"].includes(credential.name)) credential.label = "数值";
  }
  for (const [name, height] of [["requirement", 120], ["rules_text", 260], ["segment_rules_text", 140], ["chain_b_reverse_rules_text", 260], ["chain_b_review_rules_text", 260], ["chain_a_review_rules_text", 260]]) {
    const widget = node.widgets?.find((item) => item.name === name);
    if (!widget) continue;
    widget.options ??= {};
    widget.options.getMinHeight = () => height;
    widget.options.getHeight = () => height;
    const element = widget.element ?? widget.inputEl;
    element?.style?.setProperty("--comfy-widget-min-height", `${height}px`);
    element?.style?.setProperty("--comfy-widget-height", `${height}px`);
    // Legacy ComfyUI uses computeSize instead of computeLayoutSize.
    if (!widget.computeLayoutSize && !widget.dshExpandedInput) {
      const computeSize = widget.computeSize;
      widget.computeSize = function (width) {
        if (this.type === "hidden" || this.type === "converted-widget") {
          return computeSize?.call(this, width) ?? [0, -4];
        }
        return [width ?? 0, height];
      };
      widget.dshExpandedInput = true;
    }
  }
  if (node.computeSize && node.setSize && node.size) {
    const minimum = node.computeSize();
    node.setSize([Math.max(node.size[0], minimum[0], 520), Math.max(node.size[1], minimum[1])]);
    node.graph?.setDirtyCanvas(true, true);
  }
}

// Repair historical field orders without altering the source saved array.
export function migrateFullVideoWidgets(values) {
  if (!Array.isArray(values)) return values;
  let migrated = values;
  if (values.length >= 8 && typeof values[4] === "number" && typeof values[5] === "number") {
    const rules = values[6];
    if (typeof rules === "string" && (!rules.trim() || rules.includes("\n")) &&
        PROTOCOLS.has(values[7]) && PROTOCOLS.has(values[8])) {
      migrated = [...values];
      migrated[6] = "";
      migrated[7] = values[8];
      migrated[8] = rules;
    }
    if (EFFORTS.has(migrated[11]) && (migrated.length === 12 ||
        (migrated.length === 13 && typeof migrated[12] === "boolean"))) {
      migrated = [...migrated.slice(0, 11), "", ...migrated.slice(11)];
    }
    const retired = new Set([1, 2, 4, 5, 6, 7, 12]);
    migrated = migrated.filter((_, index) => !retired.has(index));
  }
  // 0.2.0/0.2.1 stored the review mode followed by its dynamic widgets.
  if (migrated[8] === "自定义") {
    return [...migrated.slice(0, 8), migrated[9] ?? "", migrated[10] || "https://api.zdjapi.com", ...migrated.slice(13)];
  }
  if (migrated[8] === "沿用当前AI") {
    return [...migrated.slice(0, 8), "", "https://api.zdjapi.com", ...migrated.slice(9)];
  }
  if (migrated.length === 10) {
    return [...migrated.slice(0, 9), "https://api.zdjapi.com", ...migrated.slice(9)];
  }
  return migrated;
}

export function migrateFullVideoImageInputs(node, links = []) {
  if (!Array.isArray(node?.inputs)) return;
  for (const input of node.inputs) {
    if (input.name === "person") input.name = "image1";
    if (input.name === "product") input.name = "image2";
    if (/^image(?:10|[1-9])$/.test(input.name)) {
      input.label = input.name;
      input.localized_name = input.name;
    }
  }
  const existing = new Map(node.inputs.map((input) => [input.name, input]));
  const images = Array.from({ length: 10 }, (_, i) => existing.get(`image${i + 1}`) ||
    { name: `image${i + 1}`, label: `image${i + 1}`, localized_name: `image${i + 1}`, type: "IMAGE", link: null });
  node.inputs = [existing.get("video"), ...images,
    ...node.inputs.filter((input) => input.name !== "video" && !/^image(?:10|[1-9])$/.test(input.name))].filter(Boolean);
  const allLinks = Array.isArray(links) ? links : Object.values(links || {});
  node.inputs.forEach((input, slot) => {
    if (input.link == null) return;
    const link = allLinks.find((item) => String(Array.isArray(item) ? item[0] : item.id) === String(input.link));
    if (!link) return;
    if (Array.isArray(link)) link[4] = slot;
    else link.target_slot = slot;
  });
}

export function migrateFullVideoOutputs(node) {
  if (!Array.isArray(node?.outputs)) return;
  for (let index = 1; index <= 5; index++) {
    const slot = index + 2;
    if (!node.outputs[slot]) node.outputs.push({ name: `video${index}`, type: "VIDEO", links: null, slot_index: slot });
  }
  if (node.type === "KKGO_FullVideoAgent") {
    if (!node.outputs[8]) node.outputs.push({ name: "分镜参考图列表", type: "IMAGE", links: null, slot_index: 8 });
    if (!node.outputs[9]) node.outputs.push({ name: "抽帧信息", type: "STRING", links: null, slot_index: 9 });
    for (let index = 1; index <= 5; index++) {
      const slot = index + 9;
      if (!node.outputs[slot]) node.outputs[slot] = { name: `第${index}段参考图`, type: "IMAGE", links: null, slot_index: slot };
    }
  }
  if (node.type === "KKGO_VideoSegments") {
    node.outputs[8] ??= {name:"反推资料",type:"KKGO_VIDEO_CONTEXT",links:null,slot_index:8};
    for (const [slot,name,type] of [[9,"分镜参考图列表","IMAGE"],[10,"抽帧信息","STRING"],
      ...Array.from({length:5},(_,i)=>[i+11,`第${i+1}段参考图`,"IMAGE"])]) {
      node.outputs[slot] ??= {name,type,links:null,slot_index:slot};
    }
  }
}

// Drop connections to retired settings before the new schema is configured.
export function migrateFixedReverseInputs(node) {
  const retired = new Set(["base_url", "model", "timeout_s", "max_tokens", "asr_model_dir", "api_protocol", "reasoning_effort",
    "review_ai", "review_ai.model", "review_ai.api_protocol"]);
  for (const input of node.inputs || []) {
    if (input.name === "review_ai.base_url") {
      input.name = "review_base_url";
      input.label = input.localized_name = "数值2";
      if (input.widget) input.widget = { ...input.widget, name: "review_base_url" };
    }
    if (input.name === "review_ai.api_key") {
      input.name = "review_api_key";
      input.label = "数值";
      input.localized_name = "数值";
      if (input.widget) input.widget = { ...input.widget, name: "review_api_key" };
    }
  }
  const removedLinks = new Set((node.inputs || []).filter(input => retired.has(input.name))
    .map(input => input.link).filter(id => id != null).map(String));
  if (Array.isArray(node.inputs)) node.inputs = node.inputs.filter(input => !retired.has(input.name));
  return removedLinks;
}
