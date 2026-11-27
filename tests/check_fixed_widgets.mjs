import assert from "node:assert/strict";
import fs from "node:fs/promises";
import { fileURLToPath, pathToFileURL } from "node:url";
import path from "node:path";
import { migrateFullVideoWidgets, resizeFullVideoInputs, migrateFullVideoOutputs } from "../web/lib/full_video_widgets.js";
import { migrateVideoStageGraph, migrateEarlyFrameLinks } from "../web/lib/video_stage_graph.js";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const old = ["local-placeholder", "https://old.invalid", "old-model", "图1人物、图2商品", 900, 32768, "/old/model", "deepseek-messages",
  "A规则\n逐字保留", false, "分段规则", "反推规则", "low", true, "B审核规则", "自定义",
  "review-placeholder", "https://review.invalid", "review-model", "openai-completions", "A审核规则"];
const expected = [old[0], old[3], old[8], old[9], old[10], old[11], old[13], old[14], old[16], old[17], old[20]];
const placed = (v, url = "https://api.deepseek.com", mode = "32秒模式（原模式）") => [v[0], url, "deepseek-flash", ...v.slice(1,10), "gpt-6.1-sol", v[10], mode];
assert.deepEqual(migrateFullVideoWidgets(old), placed(expected, old[1]));
assert.deepEqual(migrateFullVideoWidgets(expected), placed(expected));
const current = placed(expected);
assert.equal(migrateFullVideoWidgets(current), current, "current workflow must not migrate twice");
assert.equal(old.length, 21, "migration must not mutate the original widget array");
const visibleCredential = { widgets: [{ name: "api_key", value: "" }, { name: "review_api_key", value: "" }] };
resizeFullVideoInputs(visibleCredential);
assert.equal(visibleCredential.widgets[0].label, "数值");
assert.equal(visibleCredential.widgets[0].name, "api_key");
assert.equal(visibleCredential.widgets[1].label, "数值");
const compactPrevious = [old[0], old[3], old[8], old[9], old[10], old[11], old[13], ...old.slice(14)];
assert.deepEqual(migrateFullVideoWidgets(compactPrevious), placed(expected));
const inherited = [...old.slice(0, 15), "沿用当前AI", "A审核规则"];
assert.deepEqual(migrateFullVideoWidgets(inherited), placed([old[0], old[3], old[8], old[9], old[10], old[11], old[13], old[14], "", "https://api.zdjapi.com", "A审核规则"], old[1]));

let script = await fs.readFile(path.join(root, "web/kkgo_video_inputs.js"), "utf8");
script = script.replace('import { app } from "../../scripts/app.js";', 'const app = { registerExtension(extension) { globalThis.fixedReverseExtension = extension; } };');
for (const name of ["full_video_widgets", "video_stage_graph"]) {
  script = script.replace(`"./lib/${name}.js"`, JSON.stringify(pathToFileURL(path.join(root, `web/lib/${name}.js`)).href));
}
await import("data:text/javascript;base64," + Buffer.from(script).toString("base64"));
const graph = {
  nodes: [
    { id: 1, type: "Text", outputs: [{ links: [10, 11, 12, 13] }] },
    { id: 2, type: "KKGO_VideoSegments", widgets_values: [...old], inputs: [
      { name: "video", link: null }, { name: "base_url", link: 10 }, { name: "rules_text", link: 11 },
      { name: "review_ai.api_key", link: 12, widget: { name: "review_ai.api_key" } }, { name: "review_ai.model", link: 13 }
    ], outputs: [] },
    { id: 3, type: "Unrelated", widgets_values: [...old] }
  ],
  links: [[10, 1, 0, 2, 1, "STRING"], [11, 1, 0, 2, 2, "STRING"], [12, 1, 0, 2, 3, "STRING"], [13, 1, 0, 2, 4, "STRING"]]
};
globalThis.fixedReverseExtension.beforeConfigureGraph(graph);
assert.deepEqual(graph.nodes[1].widgets_values, placed(expected, old[1]));
assert.equal(graph.nodes[1].inputs.some(input => input.name === "base_url"), true);
assert.equal(graph.links.length, 3);
assert.equal(graph.links[0][0], 10);
assert.equal(graph.nodes[1].inputs[graph.links[0][4]].name, "base_url");
assert.equal(graph.nodes[1].inputs[graph.links[1][4]].name, "rules_text");
assert.deepEqual(graph.nodes[0].outputs[0].links, [10, 11, 12]);
assert.equal(graph.nodes[1].inputs[graph.links[2][4]].name, "review_api_key");
assert.deepEqual(graph.nodes[2].widgets_values, old);
globalThis.fixedReverseExtension.beforeConfigureGraph(graph);
assert.deepEqual(graph.nodes[1].widgets_values, placed(expected, old[1]));
console.log("Passed legacy/custom reviewer migration, compact reload and connected-rule preservation.");

const previousFixed = [...expected.slice(0, 9), expected[10]];
assert.deepEqual(migrateFullVideoWidgets(previousFixed), placed([...expected.slice(0, 9), "https://api.zdjapi.com", expected[10]]));
const urlWidget = { widgets: [{ name: "review_base_url" }] };
resizeFullVideoInputs(urlWidget);
assert.equal(urlWidget.widgets[0].label, "审核 AI Base URL");

const legacyReverse = { nodes: [{id:1,type:"KKGO_VideoReverse",outputs:[0,1,2].map(i=>({name:`text${i}`,type:"STRING",links:[]}))}],links:[] };
globalThis.fixedReverseExtension.beforeConfigureGraph(legacyReverse);
assert.equal(legacyReverse.nodes[0].outputs[3].type,"IMAGE");
assert.equal(legacyReverse.nodes[0].outputs[4].type,"STRING");
const full = {id:1,type:"KKGO_FullVideoAgent",outputs:Array.from({length:8},(_,slot_index)=>({name:`out${slot_index}`,slot_index,links:[]}))};
migrateFullVideoOutputs(full);
assert.equal(full.outputs.length,15);
assert.equal(full.outputs[8].type,"IMAGE");
const migrating = {last_node_id:4,last_link_id:3,nodes:[full,
  {id:2,type:"SaveVideo",inputs:[{name:"video",link:1}],outputs:[],pos:[0,0]},
  {id:3,type:"SaveImage",inputs:[{name:"images",link:2}],outputs:[]},
  {id:4,type:"Text",inputs:[{name:"text",link:3}],outputs:[]}],
  links:[[1,1,3,2,0,"VIDEO"],[2,1,8,3,0,"IMAGE"],[3,1,9,4,0,"STRING"]]};
assert.equal(migrateVideoStageGraph(migrating),true);
const newReverse = migrating.nodes.find(n=>n.type==="KKGO_VideoReverse");
assert.deepEqual(migrating.links.find(l=>l[0]===2).slice(1,3),[newReverse.id,3]);
assert.deepEqual(migrating.links.find(l=>l[0]===3).slice(1,3),[newReverse.id,4]);
assert.equal(full.outputs[8].type,"KKGO_VIDEO_CONTEXT");
assert.equal(full.outputs.length,9);
assert.equal(migrateVideoStageGraph(migrating),false);
assert.equal(legacyReverse.nodes[0].outputs.length,10);
assert.equal(legacyReverse.nodes[0].outputs[5].name,"第1段参考图");
assert.equal(legacyReverse.nodes[0].outputs[9].name,"第5段参考图");
assert.equal(newReverse.outputs.length,10);
const fifthGroupGraph = {last_node_id:3,last_link_id:2,nodes:[
  {id:1,type:"KKGO_FullVideoAgent",outputs:structuredClone(full.outputs)},
  {id:2,type:"SaveVideo",inputs:[{name:"video",link:1}],outputs:[],pos:[0,0]},
  {id:3,type:"SaveImage",inputs:[{name:"images",link:2}],outputs:[]}],
  links:[[1,1,3,2,0,"VIDEO"],[2,1,14,3,0,"IMAGE"]]};
assert.equal(migrateVideoStageGraph(fifthGroupGraph),true);
const fifthReverse = fifthGroupGraph.nodes.find(n=>n.type==="KKGO_VideoReverse");
assert.deepEqual(fifthGroupGraph.links.find(l=>l[0]===2).slice(1,3),[fifthReverse.id,9]);
assert.equal(migrateEarlyFrameLinks(fifthGroupGraph),true);
const frameNode = fifthGroupGraph.nodes.find(n=>n.type==="KKGO_VideoFrames");
assert.deepEqual(fifthGroupGraph.links.find(l=>l[0]===2).slice(1,3),[frameNode.id,6]);
assert.equal(migrateEarlyFrameLinks(fifthGroupGraph),false);
assert.equal(fifthGroupGraph.nodes[0].outputs[8].type,"KKGO_VIDEO_CONTEXT");
function ReverseLayout() {
  this.size=[340,150];
  this.computeSize=()=>[300,260];
  this.setSize=value=>{this.size=value;};
}
globalThis.fixedReverseExtension.beforeRegisterNodeDef(ReverseLayout,{name:"KKGO_VideoReverse"});
const reverseLayout=new ReverseLayout();
reverseLayout.onConfigure({});
assert.deepEqual(reverseLayout.size,[340,260]);
console.log("Passed IMAGE-list port reload and existing VIDEO/context/image/metadata link migration.");

const savedBranch = {last_node_id:5,last_link_id:4,nodes:[
  {id:1,type:"KKGO_VideoSegments",outputs:Array.from({length:16},(_,slot_index)=>({name:`out${slot_index}`,slot_index,links:[]}))},
  {id:2,type:"KKGO_VideoReverse",inputs:[{name:"context",link:1}],outputs:Array.from({length:10},(_,slot_index)=>({slot_index,links:[]}))},
  {id:3,type:"SaveImage",inputs:[{name:"images",link:2}],widgets_values:["keep-image-path"]},
  {id:4,type:"SaveVideo",inputs:[{name:"video",link:3}],widgets_values:["keep-video-path","mp4","h264"]},
  {id:5,type:"SaveImage",inputs:[{name:"images",link:4}]}],
  links:[[1,1,8,2,0,"KKGO_VIDEO_CONTEXT"],[2,1,11,3,0,"IMAGE"],[3,1,3,4,0,"VIDEO"],[4,2,9,5,0,"IMAGE"]]};
savedBranch.nodes[0].outputs[8].links=[1];
savedBranch.nodes[0].outputs[11].links=[2];
savedBranch.nodes[0].outputs[3].links=[3];
savedBranch.nodes[1].outputs[9].links=[4];
assert.equal(migrateEarlyFrameLinks(savedBranch),true);
const independentFrames=savedBranch.nodes.find(n=>n.type==="KKGO_VideoFrames");
assert.deepEqual(savedBranch.links.find(l=>l[0]===2).slice(1,3),[independentFrames.id,2]);
assert.deepEqual(savedBranch.links.find(l=>l[0]===4).slice(1,3),[independentFrames.id,6]);
assert.deepEqual(savedBranch.links.find(l=>l[0]===3),[3,1,3,4,0,"VIDEO"]);
assert.deepEqual(savedBranch.nodes[3].widgets_values,["keep-video-path","mp4","h264"]);
assert.deepEqual(savedBranch.nodes[2].widgets_values,["keep-image-path"]);
assert.equal(migrateEarlyFrameLinks(savedBranch),false);
migrateFullVideoOutputs(savedBranch.nodes[0]);
assert.equal(savedBranch.nodes[0].outputs.length,9);
console.log("Passed independent frame migration, repeat loading and saver settings preservation.");

const renamedGraph = {nodes:[
  {type:"KKGO_VideoSegments",title:"KK agent 视频反推",widgets_values:[],outputs:[]},
  {type:"KKGO_VideoReverse",title:"KK 独立反推（与SaveVideo并行） GO",outputs:[]},
  {type:"KKGO_VideoFrames",title:"KK 独立抽帧（与视频保存并行）"},
  {type:"KKGO_VideoSegments",title:"我的自定义标题",widgets_values:[],outputs:[]}],links:[]};
globalThis.fixedReverseExtension.beforeConfigureGraph(renamedGraph);
assert.deepEqual(renamedGraph.nodes.map(n=>n.title),["kk agent 2","kk agent 2 独立反推","kk agent 2 独立抽帧","我的自定义标题"]);
console.log("Passed kk agent 2 default title migration and custom title preservation.");
