// Split old full-video graphs with SaveVideo branches into real scheduler nodes.
// All filenames, formats and codecs remain properties of SaveVideo nodes.
export function migrateVideoStageGraph(graph) {
  if (!Array.isArray(graph?.nodes) || !Array.isArray(graph.links)) return false;
  let nodeId = Math.max(Number(graph.last_node_id) || 0, ...graph.nodes.map(n => Number(n.id) || 0));
  let linkId = Math.max(Number(graph.last_link_id) || 0, ...graph.links.map(l => Number(l[0]) || 0));
  let changed = false;
  for (const node of [...graph.nodes]) {
    if (node.type !== "KKGO_FullVideoAgent") continue;
    const videoLinks = graph.links.filter(l => String(l[1]) === String(node.id) && l[2] >= 3 && l[2] <= 7);
    const savers = videoLinks.map(l => graph.nodes.find(n => String(n.id) === String(l[3])))
      .filter(n => n?.type === "SaveVideo");
    if (!savers.length) continue;
    const reverseId = ++nodeId;
    const contextLink = ++linkId;
    const pos = Array.isArray(node.pos) ? node.pos : [0, 0];
    const width = Number(node.size?.[0]) || 540;
    const reverse = {
      id: reverseId, type: "KKGO_VideoReverse", title: "KK 独立反推（与SaveVideo并行） GO",
      pos: [Number(pos[0]) + width + 40, Number(pos[1]) - 180], size: [340, 150],
      flags: {}, order: (node.order || 0) + 1, mode: 0,
      inputs: [{ name: "context", type: "KKGO_VIDEO_CONTEXT", link: contextLink }],
      outputs: ["提示词", "分析证据", "状态", "分镜参考图列表", "抽帧信息",
        ...Array.from({length:5}, (_,i)=>`第${i+1}段参考图`)].map((name, slot_index) =>
        ({ name, type: slot_index === 3 || slot_index >= 5 ? "IMAGE" : "STRING", links: [], slot_index })),
      properties: { "Node name for S&R": "KKGO_VideoReverse" }, widgets_values: [],
    };
    // Existing text viewers/savers now wait on reverse. VIDEO branches wait
    // only on segmentation, so the executor can save during reverse inference.
    for (const link of graph.links) {
      if (String(link[1]) !== String(node.id)) continue;
      const targetSlot = link[2] <= 2 ? link[2] : link[2] >= 8 && link[2] <= 14 ? link[2] - 5 : -1;
      if (targetSlot < 0) continue;
      link[1] = reverseId;
      link[2] = targetSlot;
      reverse.outputs[targetSlot].links.push(link[0]);
    }
    node.type = "KKGO_VideoSegments";
    node.properties ??= {};
    node.properties["Node name for S&R"] = node.type;
    node.title = "KK agent runninghub";
    node.outputs ??= [];
    for (let index = 0; index < 3; index++) {
      if (node.outputs[index]) node.outputs[index].links = [];
    }
    while (node.outputs.length < 8) {
      const index = node.outputs.length;
      node.outputs.push({ name: `video${index - 2}`, type: "VIDEO", links: [], slot_index: index });
    }
    node.outputs = node.outputs.slice(0, 8);
    node.outputs[8] = { name: "反推资料", type: "KKGO_VIDEO_CONTEXT", links: [contextLink], slot_index: 8 };
    graph.links.push([contextLink, node.id, 8, reverseId, 0, "KKGO_VIDEO_CONTEXT"]);
    graph.nodes.push(reverse);
    // Old graphs often wired only three SaveVideo nodes. Give every optional
    // VIDEO output a saver, retaining the user's existing saver settings.
    for (let slot = 3; slot <= 7; slot++) {
      if (graph.links.some(l => String(l[1]) === String(node.id) && l[2] === slot &&
          graph.nodes.some(n => String(n.id) === String(l[3]) && n.type === "SaveVideo"))) continue;
      const saver = structuredClone(savers[0]);
      saver.id = ++nodeId;
      saver.title = `保存 video${slot - 2}`;
      saver.pos = [Number(pos[0]) + width + 440, Number(pos[1]) + (slot - 3) * 320];
      saver.order = (node.order || 0) + slot;
      const inputSlot = saver.inputs.findIndex(input => input.name === "video");
      if (inputSlot < 0) continue;
      for (const input of saver.inputs) input.link = null;
      for (const output of saver.outputs || []) output.links = [];
      const id = ++linkId;
      saver.inputs[inputSlot].link = id;
      (node.outputs[slot].links ??= []).push(id);
      graph.links.push([id, node.id, slot, saver.id, inputSlot, "VIDEO"]);
      graph.nodes.push(saver);
    }
    changed = true;
  }
  if (changed) {
    graph.last_node_id = nodeId;
    graph.last_link_id = linkId;
  }
  return changed;
}

// Existing image savers should wait on frame selection, not final prompt review.
export function migrateEarlyFrameLinks(graph) {
  if (!Array.isArray(graph?.nodes) || !Array.isArray(graph.links)) return false;
  let changed = false;
  for (const reverse of graph.nodes.filter(n=>n.type === "KKGO_VideoReverse")) {
    const context = graph.links.find(l=>String(l[3])===String(reverse.id) && l[4]===0 && l[5]==="KKGO_VIDEO_CONTEXT");
    const entry = context && graph.nodes.find(n=>String(n.id)===String(context[1]) && n.type==="KKGO_VideoSegments");
    if (!entry) continue;
    entry.outputs ??= [];
    for (const link of graph.links) {
      if (String(link[1])!==String(reverse.id) || link[2]<3 || link[2]>9) continue;
      const oldSlot=link[2], newSlot=oldSlot+6;
      const name=oldSlot===3 ? "分镜参考图列表" : oldSlot===4 ? "抽帧信息" : `第${oldSlot-4}段参考图`;
      entry.outputs[newSlot] ??= {name,type:oldSlot===4 ? "STRING":"IMAGE",links:[],slot_index:newSlot};
      const old=reverse.outputs?.[oldSlot];
      if (Array.isArray(old?.links)) old.links=old.links.filter(id=>String(id)!==String(link[0]));
      const links=(entry.outputs[newSlot].links ??= []);
      if (!links.includes(link[0])) links.push(link[0]);
      link[1]=entry.id;
      link[2]=newSlot;
      changed=true;
    }
  }
  return changed;
}
