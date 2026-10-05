/** Bind release-owned settings through the public action API. */
import { enhanceProperties } from "./properties.js";
import { bindSettings } from "./settings.js";
export function mount(root, api) {
  const disposeStyle = enhanceProperties(root), disposeForm = bindSettings(root, api);
  const controller = new AbortController();
  // Own these tabs locally: changing a view must not re-render and discard a draft.
  const select = selected => {
    for (const button of root.querySelectorAll('[data-owned-inspector-tab]')) {
      const active = button.dataset.ownedInspectorTab === selected;
      button.classList.toggle('active', active);
      button.setAttribute('aria-selected', String(active));
      button.tabIndex = active ? 0 : -1;
    }
    for (const view of root.querySelectorAll('[data-inspector-panel-tab]')) {
      const hidden = view.dataset.inspectorPanelTab !== selected;
      view.classList.toggle('hidden', hidden); view.hidden = hidden;
    }
  };
  select(root.querySelector('[data-owned-inspector-tab].active')?.dataset.ownedInspectorTab || 'general');
  root.addEventListener('click', event => {
    const button = event.target.closest('[data-owned-inspector-tab]');
    if (button && root.contains(button)) select(button.dataset.ownedInspectorTab);
  }, {signal: controller.signal});
  return {dispose() {controller.abort(); disposeForm(); disposeStyle();}};
}
