"""Completed local files → paced Opus, using only public block services."""
from bloxsmith_app.block_api import BlockDefinition, BlockRuntimePreparation
from .configuration import AudioFileError, configuration, validate_ports
from .runtime import FileListener, execute
from . import ui


# FB1 - Explicit file and interruption inputs; separate Opus and lifecycle outputs.
# FB2 - Bounded no-follow file snapshots and complete WAV/Ogg validation.
# FB3 - Paced Opus, exact lifecycle counts, bounded queue and owned interruption.
# FB4 - No IO in prepare/simulation; generic runtime and independent package ownership.
# FB5 - Translated responsive draft settings and stable, reorder-safe ports.
class AudioFileStreamBlock(BlockDefinition):
    kind = "audio_file_stream"

    def ui_assets(self, surface="modal"):
        return list(self.model.get("ui_assets", {}).get(surface, []))

    def prepare_runtime(self, context):
        configuration(context.config)
        validate_ports(context)
        return BlockRuntimePreparation(listen_on_run=context.runtime_mode == "zeromq_active")

    def execute_runtime(self, context):
        return execute(context)

    def listen_runtime(self, context):
        FileListener(context).run()

    def handle_ui_action(self, *, node, action, values, payload=None):
        if action in {"save_settings", "modal_update_fields", "inspector_update_fields"}:
            patch = (values or {}).get("node_patch") or {}
            if "config" in patch:
                try:
                    configuration({**self.default_config(), **(node.get("config") or {}), **patch["config"]})
                except AudioFileError as error:
                    return {"error": self.translate("block.audio_file_stream.error", {"detail": str(error)}, fallback="Invalid settings: {detail}")}
        return ui.replace_config(super().handle_ui_action(node=node,
            action="modal_update_fields" if action == "save_settings" else action, values=values, payload=payload), node)

    def render_modal(self, *, node, payload=None):
        return ui.modal(self, node, payload)

    def render_inspector_panel(self, *, node, payload=None):
        return ui.inspector(self, node, payload)

    def render_node_card(self, *, node, payload=None):
        return ui.card(self, node, self.translate("block.audio_file_stream.preview", fallback="File → paced Opus · 48 kHz"))
