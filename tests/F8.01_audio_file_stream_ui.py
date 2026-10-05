"""FB5: actual managed/linked EN/FR forms, responsive geometry and preserved drafts."""
from pathlib import Path
import sys
ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]
from playwright.sync_api import sync_playwright, expect
from block_test_artifacts import artifact_path
from block_test_packages import install_test_package, surface_payload
from ui_smoke_common import isolated_server, graph_payload, create_project_api, project_editor_url
from blocs.audio_file_stream.block import AudioFileStreamBlock


def geometry(modal):
    state = modal.evaluate("""el => {
      const r=el.getBoundingClientRect(),b=el.querySelector('.owned-body');
      return {inside:r.left>=-1&&r.right<=innerWidth+1&&r.top>=-1&&r.bottom<=innerHeight+1,
        overflow:el.scrollWidth>el.clientWidth+2||b.scrollWidth>b.clientWidth+2,
        buttons:[...el.querySelectorAll('[data-close-block-modal],[data-owned-apply]')].every(a=>{
          const q=a.getBoundingClientRect();return q.top>=0&&q.bottom<=innerHeight+1}),
        background:getComputedStyle(el).backgroundColor,
        unlabeled:[...el.querySelectorAll('input,select,textarea')].filter(c=>!c.labels?.length&&!c.getAttribute('aria-label')).length};
    }""")
    assert state["inside"] and state["buttons"] and not state["overflow"] and not state["unlabeled"], state
    assert state["background"] not in {"transparent", "rgba(0, 0, 0, 0)"}, state


def main():
    for origin in ("managed", "linked"):
        with isolated_server() as server, sync_playwright() as playwright:
            model = install_test_package(server, "audio_file_stream", origin=origin)
            node = AudioFileStreamBlock().build_node_payload(node_id="ui-case", position={"x":160,"y":160})
            node["block_version"] = model["version"]
            for surface in ("modal", "inspector_panel", "node_card"):
                surface_payload(server, model, node, surface)
            project = create_project_api(server, document=graph_payload("Audio file settings", [node], []))["project"]
            url = project_editor_url(server.base_url, project["project_id"], workspace_project_id=project["workspace_project_id"])
            browser = playwright.chromium.launch(headless=True)
            try:
                page = browser.new_page(viewport={"width":1440,"height":900})
                page.add_init_script("window.localStorage.setItem('bloxsmith.inspectorPinned','true')")
                errors=[]; page.on("pageerror", lambda error:errors.append(str(error)))
                page.goto(url)
                card=page.locator('.canvas-node[data-node-id="ui-case"]'); card.locator('h3').dblclick()
                modal=page.locator('.owned-modal')
                field=modal.locator('[data-block-config-field="allowed_root"]')
                expect(modal.locator('[data-owned-apply]')).to_be_disabled()
                field.fill('cancelled-draft'); modal.locator('[data-close-block-modal]').last.click()
                card.locator('h3').dblclick(); expect(field).to_have_value('')
                field.fill('audio-files')
                with page.expect_response(lambda r:r.url.endswith('/ui-action') and r.request.method=='POST') as response:
                    modal.locator('[data-owned-apply]').click()
                assert not response.value.json().get('error'), response.value.json()
                if modal.is_visible(): modal.locator('[data-close-block-modal]').first.click()
                page.reload(); card.locator('h3').dblclick(); expect(field).to_have_value('audio-files')
                advanced=modal.locator('.owned-advanced').first; advanced.locator('summary').click()
                limit=modal.locator('[data-block-config-field="prepare_timeout_sec"]'); limit.fill('0')
                advanced.locator('summary').click(); modal.locator('[data-owned-apply]').click()
                expect(advanced).to_have_attribute('open',''); expect(limit).to_be_focused()
                limit.fill('30'); advanced.locator('summary').click()
                for width,height in ((1440,900),(800,700),(390,740),(320,568)):
                    page.set_viewport_size({"width":width,"height":height}); geometry(modal)
                    page.screenshot(path=artifact_path('audio-file-'+origin+'-'+str(width)+'.png'))
                page.set_viewport_size({"width":1440,"height":900})
                modal.locator('[data-close-block-modal]').first.click(); card.click(position={"x":20,"y":20})
                inspector=page.locator('[data-properties-surface="inspector"][data-node-id="ui-case"]:visible')
                expect(inspector).to_be_visible()
                value=inspector.locator('[data-block-config-field="allowed_root"]'); value.fill('inspector-draft')
                inspector.locator('[data-owned-inspector-tab="general"]').focus()
                inspector.locator('[data-owned-inspector-tab="general"]').press('ArrowRight')
                expect(inspector.locator('[data-inspector-panel-tab="ports"]')).to_be_visible()
                inspector.locator('[data-owned-inspector-tab="ports"]').press('ArrowLeft')
                expect(value).to_have_value('inspector-draft')
                assert inspector.evaluate('el=>el.scrollWidth<=el.clientWidth+2')
                page.screenshot(path=artifact_path('audio-file-'+origin+'-inspector.png'))
                page.goto(server.base_url+'/'); page.locator('#homeApplicationSettingsButton').click()
                page.locator('#applicationLanguageSelect').select_option('fr')
                page.wait_for_function("window.CWMessages.getLanguage() === 'fr'")
                page.goto(url); card.locator('h3').dblclick()
                expect(modal.locator('[data-i18n="block.audio_file_stream.label_allowed_root"]')).to_have_text('Répertoire source autorisé')
                expect(advanced.locator('summary')).to_have_text('Réglages avancés')
                for width,height in ((390,740),(320,568)):
                    page.set_viewport_size({"width":width,"height":height}); geometry(modal)
                    page.screenshot(path=artifact_path('audio-file-'+origin+'-fr-'+str(width)+'.png'))
                assert not errors, errors
                assert page.evaluate('!window.CWBlockUiBlocks?.audio_file_stream')
            finally:
                browser.close()
        print('[ok] '+origin+' bilingual responsive modal/inspector and draft actions', flush=True)


if __name__ == '__main__': main()
