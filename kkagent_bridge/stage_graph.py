"""Move historical frame links onto the independent selection node."""


def migrate_frame_prompt(payload):
    prompt = payload.get("prompt", {})
    for entry_id, entry in list(prompt.items()):
        if entry.get("class_type") != "KKGO_VideoSegments":
            continue
        context = [entry_id, 8]
        reverse_ids = {node_id for node_id, node in prompt.items()
                       if node.get("class_type") == "KKGO_VideoReverse"
                       and node.get("inputs", {}).get("context") == context}
        links = []
        for node in prompt.values():
            for name, value in node.get("inputs", {}).items():
                if not isinstance(value, list) or len(value) != 2 or not isinstance(value[0], str) or not isinstance(value[1], int):
                    continue
                source, slot = value
                if source == entry_id and 9 <= slot <= 15:
                    links.append((node["inputs"], name, slot - 9))
                elif source in reverse_ids and 3 <= slot <= 9:
                    links.append((node["inputs"], name, slot - 3))
        if not links:
            continue
        frame_id = next((node_id for node_id, node in prompt.items()
                         if node.get("class_type") == "KKGO_VideoFrames"
                         and node.get("inputs", {}).get("context") == context), None)
        if frame_id is None:
            frame_id = f"{entry_id}-frames"
            while frame_id in prompt:
                frame_id += "-1"
            prompt[frame_id] = {"class_type": "KKGO_VideoFrames", "inputs": {"context": context}}
        for inputs, name, slot in links:
            inputs[name] = [frame_id, slot]
    return payload
