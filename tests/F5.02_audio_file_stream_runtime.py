"""FB1/FB3/FB4: real runtimes, all package origins, recorder and browser Speaker."""
import json
from pathlib import Path
import runpy
import subprocess
import sys
import time
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]
from playwright.sync_api import sync_playwright
from block_test_packages import install_test_package, prepare_release_run, release_key
from ui_smoke_common import (isolated_server, graph_payload, text_node, data_edge, display_node,
    create_project_api, project_editor_url, create_run_api, wait_for_run_terminal,
    wait_for_run_predicate, stop_run_api, http_json, graph_storage_dir)
from blocs.audio_file_stream.block import AudioFileStreamBlock
from blocs.save_audio.block import SaveAudioBlock
from blocs.audio_play_stream.block import AudioPlayStreamBlock

wav = runpy.run_path(str(Path(__file__).with_name("F5.01_audio_file_stream.py")))["wav"]


def output(data, port):
    raw = data.get("output_values", {}).get("bridge:" + str(port), {}).get("value")
    return json.loads(raw) if raw else {}


def main():
    for origin in (None, "managed", "linked"):
        with isolated_server() as server, sync_playwright() as playwright:
            source = wav(server.root_dir / "voice.wav", duration=.65)
            long_source = wav(server.root_dir / "long.wav", duration=4)
            browser_test = origin == "managed"
            player_model = install_test_package(server, "audio_play_stream") if browser_test else None
            model = install_test_package(server, "audio_file_stream", origin=origin) if origin else None
            bridge = AudioFileStreamBlock().build_node_payload(node_id="bridge", config_overrides={"allowed_root":str(server.root_dir)})
            bridge["inputs"].reverse(); bridge["outputs"].reverse()
            if model: bridge["block_version"] = model["version"]
            directory = server.root_dir / "recordings"
            recorder = SaveAudioBlock().build_node_payload(node_id="save", config_overrides={"output_dir":str(directory)})
            player = AudioPlayStreamBlock().build_node_payload(node_id="player")
            if player_model: player["block_version"] = player_model["version"]
            nodes = [text_node("file", "File", str(source), 0, 100), text_node("command", "Interrupt", "", 0, 300),
                     bridge, recorder, player, display_node("status", "Status", 600, 100)]
            nodes[1]["outputs"][0]["emits"] = ["application/json"]
            graph = graph_payload("Completed file audio integration", nodes,
                [data_edge("file", "file", 1, "bridge", 1), data_edge("interrupt", "command", 1, "bridge", 2),
                 data_edge("speaker-interrupt", "command", 1, "player", 2),
                 data_edge("audio-save", "bridge", 1, "save", 1), data_edge("audio-speaker", "bridge", 1, "player", 1),
                 data_edge("cycle", "bridge", 2, "save", 2), data_edge("status", "bridge", 3, "status", 1)])
            project = create_project_api(server, document=graph)["project"]
            # A blank command is not a business interrupt. Keep it unwired in simulation;
            # no audio block opens/converts a source or fabricates a successful stream.
            simulation = {**graph, "edges":[edge for edge in graph["edges"] if not edge["id"].endswith("interrupt")]}
            run = create_run_api(server, simulation, project_id=project["graph_id"], runtime_mode="centralized")
            finished = wait_for_run_terminal(server, run["run_id"], timeout_sec=20)
            assert finished["status"] == "success", finished.get("logs")
            assert not directory.exists() or not list(directory.iterdir())
            rid = prepare_release_run(server, project["graph_id"], graph)["run_id"]
            browser = None
            scope_arg = None
            try:
                if browser_test:
                    catalog = next(item for item in http_json(server.base_url, "/api/blocks")["blocks"] if item["kind"] == release_key(player_model))
                    asset = next(item["path"] for item in catalog["browser_runtime_assets"] if item["path"].endswith("/assets/js/common.js"))
                    common_url = server.base_url + "/api/blocks/" + quote(release_key(player_model), safe="") + "/assets/" + asset
                    browser = playwright.chromium.launch(headless=True)
                    page = browser.new_page()
                    page.goto(project_editor_url(server.base_url, project["graph_id"], workspace_project_id=project["workspace_project_id"]))
                    page.wait_for_selector('.canvas-node[data-node-id="player"]')
                    unlock=page.locator('#browserAudioUnlockButton')
                    unlock.wait_for(state="visible", timeout=10000); unlock.click()
                    scope_arg = {"url":common_url, "scope":{"workspaceProjectId":project["workspace_project_id"],
                        "graphId":project["graph_id"], "instanceId":"1", "runId":rid, "nodeId":"player"}}
                    page.wait_for_function("async data=>{const ui=await import(data.url);return ui.get(ui.key(data.scope))?.player?.snapshot().active}", arg=scope_arg)

                def send(node, value):
                    response=http_json(server.base_url, f"/api/runs/{rid}/active/control", method="POST", payload={
                        "action":"publish_output", "node_id":node, "port_id":1,
                        "value":json.dumps(value), "content_type":"application/json"})
                    assert not response.get("error"), response

                def wait(predicate):
                    state = wait_for_run_predicate(server, rid, lambda data:data.get("status")=="failed" or predicate(data),
                        "File audio integration did not progress", timeout_sec=15)
                    assert state.get("status") != "failed", state.get("logs")
                    return state

                def status(ident, state):
                    return wait(lambda data:output(data,3).get("request_id")==ident and output(data,3).get("state")==state)

                send("file", {"path":str(source), "request_id":"first"})
                status("first", "completed")
                wait(lambda data:len(data.get("results",{}).get("save",{}).get("save_audio",{}).get("saved_files",[]))==1)
                recordings=list(directory.glob("*.ogg")); assert len(recordings)==1
                probe=subprocess.run(["ffprobe","-v","error","-show_entries","format=duration","-of","json",str(recordings[0])], capture_output=True, check=True)
                assert abs(float(json.loads(probe.stdout)["format"]["duration"])-.65)<.03
                if browser_test:
                    page.wait_for_function("async data=>{const ui=await import(data.url);return ui.get(ui.key(data.scope))?.player?.snapshot().playedSamples===31200}", arg=scope_arg)
                    snapshot=page.evaluate("async data=>{const ui=await import(data.url);return ui.get(ui.key(data.scope)).player.snapshot()}",scope_arg)
                    assert not snapshot.get("error") and snapshot["highWaterSeconds"]<1, snapshot
                send("file", {"path":"missing.wav", "request_id":"missing"}); status("missing","error")
                send("file", {"path":str(long_source), "request_id":"interrupt-me"})
                status("interrupt-me", "streaming")
                send("command", {"action":"interrupt"}); status("interrupt-me","cancelled")
                assert len(list(directory.glob("*.ogg")))==1, "Cancelled stream was finalized"
                send("file", {"path":str(source), "request_id":"recovered"}); status("recovered","completed")
                state=wait(lambda data:len(data.get("results",{}).get("save",{}).get("save_audio",{}).get("saved_files",[]))==2)
                assert len(list(directory.glob("*.ogg")))==2
                assert not any("saturat" in line.lower() for line in state.get("logs",[])), state.get("logs")
                send("file", {"path":str(long_source), "request_id":"stop-me"}); status("stop-me","streaming")
                before=time.monotonic(); ended=stop_run_api(server,rid)
                assert time.monotonic()-before<8 and "shutdown_timeout" not in str(ended), ended
                if origin is None:
                    storage=graph_storage_dir(server,project)/"instances"/"1"/"blocs"/"bridge"
                    assert not list(storage.glob("file-stream-*"))
            finally:
                if browser: browser.close()
                stop_run_api(server,rid)
            print('[ok] File stream '+str(origin)+' simulation/active → Save Audio, recovery/interrupt/Stop'+(' + actual browser Speaker' if browser_test else ''),flush=True)


if __name__ == "__main__": main()
