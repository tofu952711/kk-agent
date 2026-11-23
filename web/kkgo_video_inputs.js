import { app } from "../../scripts/app.js";
import { migrateFullVideoWidgets, migrateFixedReverseInputs, migrateFullVideoImageInputs, migrateFullVideoOutputs, resizeFullVideoInputs } from "./lib/full_video_widgets.js";
import { migrateVideoStageGraph } from "./lib/video_stage_graph.js";

app.registerExtension({
  rh: { type: "nodes", nodes: "all" },
  name: "KKGO.FullVideo.InputMigration",
  beforeConfigureGraph(graphData) {
    if (migrateVideoStageGraph(graphData)) {
      console.info("[KKGO] 已拆分B分段与反推连线：标准SaveVideo和独立反推节点可同时执行，保存配置保持不变");
    }
    const removedLinks = new Set();
    for (const node of graphData?.nodes || []) {
      if (node.type === "KKGO_VideoSegments" && (!node.title ||
          ["KKGO 视频分析／B分段（并行入口）", "KKGO 视频分析／B分段（SaveVideo与反推分流）"].includes(node.title))) {
        node.title = "KK agent 视频反推";
      }
      if (["KKGO_FullVideoAgent", "KKGO_VideoSegments"].includes(node.type)) {
        node.widgets_values = migrateFullVideoWidgets(node.widgets_values);
        for (const id of migrateFixedReverseInputs(node)) removedLinks.add(id);
        migrateFullVideoImageInputs(node, graphData.links);
        migrateFullVideoOutputs(node);
      }
    }
    if (removedLinks.size && Array.isArray(graphData.links)) {
      graphData.links = graphData.links.filter(link => !removedLinks.has(String(Array.isArray(link) ? link[0] : link.id)));
      for (const node of graphData.nodes || []) {
        for (const output of node.outputs || []) {
          if (Array.isArray(output.links)) output.links = output.links.filter(id => !removedLinks.has(String(id)));
        }
      }
    }
  },
  beforeRegisterNodeDef(nodeType, nodeData) {
    if (!["KKGO_FullVideoAgent", "KKGO_VideoSegments"].includes(nodeData.name)) return;
    const created = nodeType.prototype.onNodeCreated;
    nodeType.prototype.onNodeCreated = function () {
      const result = created?.apply(this, arguments);
      resizeFullVideoInputs(this);
      return result;
    };
    const original = nodeType.prototype.onConfigure;
    nodeType.prototype.onConfigure = function (info) {
      const previous = info?.widgets_values;
      const migrated = migrateFullVideoWidgets(previous);
      if (migrated !== previous) info.widgets_values = migrated;
      const reviewWidget = this.widgets?.find((item) => item.name === "chain_b_review_rules_text");
      const reviewDefault = nodeData.input?.optional?.chain_b_review_rules_text?.[1]?.default ?? reviewWidget?.value;
      let aReviewWidget = this.widgets?.find((item) => item.name === "chain_a_review_rules_text");
      const aReviewDefault = nodeData.input?.optional?.chain_a_review_rules_text?.[1]?.default ?? aReviewWidget?.value;
      const result = original?.apply(this, arguments);
      // Re-find after configure: custom reviewer widgets may have been inserted.
      aReviewWidget = this.widgets?.find((item) => item.name === "chain_a_review_rules_text");
      const aReviewIndex = this.widgets?.indexOf(aReviewWidget) ?? -1;
      const aReviewLinked = this.inputs?.some((input) => input.name === "chain_a_review_rules_text" && input.link != null);
      if (aReviewWidget && !aReviewLinked && (!Array.isArray(migrated) || aReviewIndex >= migrated.length || aReviewWidget.value == null)) {
        aReviewWidget.value = aReviewDefault;
      }
      const reviewLinked = this.inputs?.some((input) => input.name === "chain_b_review_rules_text" && input.link != null);
      if (reviewWidget && !reviewLinked && (!Array.isArray(migrated) || migrated.length <= 7)) {
        reviewWidget.value = reviewDefault;
      }
      if (this.inputs?.some((input) => input.name === "person" || input.name === "product") ||
          (this.inputs && !this.inputs.some((input) => input.name === "image10"))) {
        migrateFullVideoImageInputs(this, this.graph?.links);
      }
      migrateFullVideoOutputs(this);
      if (migrated !== previous) {
        for (const [index, name] of [[0, "api_key"], [1, "requirement"], [2, "rules_text"],
            [3, "keep_audio"], [4, "segment_rules_text"], [5, "chain_b_reverse_rules_text"],
            [6, "stream_usage"], [7, "chain_b_review_rules_text"],
            [8, "review_api_key"], [9, "review_base_url"], [10, "chain_a_review_rules_text"]]) {
          if (index >= migrated.length) continue;
          const widget = this.widgets?.find((item) => item.name === name);
          if (widget) widget.value = migrated[index];
        }
        console.info("[KKGO] 已兼容旧节点字段顺序，保留数值、规则文本及审核配置，反推配置已固定");
      }
      resizeFullVideoInputs(this);
      return result;
    };
  },
});
