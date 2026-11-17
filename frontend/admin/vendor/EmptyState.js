import uiIcon from './UiIcon.js';

const escapeAttribute = (value) =>
  String(value).replace(/[&<>'"]/g, (char) => ({
    '&': '&amp;',
    '<': '&lt;',
    '>': '&gt;',
    "'": '&#39;',
    '"': '&quot;',
  }[char]));

export class EmptyStateElement extends HTMLElement {
  static get observedAttributes() {
    return ['icon', 'title', 'description', 'action-label'];
  }

  connectedCallback() {
    this.render();
  }

  attributeChangedCallback() {
    this.render();
  }

  _applyHostClass(layoutClass) {
    const prev = this._appliedLayout || '';
    let user = this.className || '';
    if (prev && user.startsWith(prev)) user = user.slice(prev.length).trim();
    this._appliedLayout = layoutClass;
    const next = `${layoutClass}${user ? ` ${user}` : ''}`;
    if (this.className !== next) this.className = next;
  }

  render() {
    const icon = this.getAttribute('icon') || 'sparkles';
    const title = this.getAttribute('title') || '';
    const description = this.getAttribute('description') || '';
    const actionLabel = this.getAttribute('action-label') || '';

    this._applyHostClass('flex flex-col items-center justify-center text-center px-6 py-12');
    this.innerHTML = `
      <div class="p-3 rounded-2xl bg-zinc-100 dark:bg-zinc-800 text-zinc-400 dark:text-zinc-500 mb-4">
        ${uiIcon(icon, { size: 24 })}
      </div>
      <div class="text-sm font-semibold text-zinc-800 dark:text-zinc-200">${escapeAttribute(title)}</div>
      ${description ? `<div class="mt-1 text-xs text-zinc-400 dark:text-zinc-500 max-w-xs">${escapeAttribute(description)}</div>` : ''}
      ${
        actionLabel
          ? `<button type="button" class="empty-action mt-4 px-3.5 py-1.5 rounded-lg bg-indigo-600 hover:bg-indigo-500 text-white text-xs font-medium transition-colors">${escapeAttribute(actionLabel)}</button>`
          : ''
      }`;

    const btn = this.querySelector('.empty-action');
    if (btn) btn.addEventListener('click', () => this.dispatchEvent(new CustomEvent('action')));
  }
}

if (typeof customElements !== 'undefined' && !customElements.get('empty-state')) {
  customElements.define('empty-state', EmptyStateElement);
}
export default EmptyStateElement;
