import uiIcon from './UiIcon.js';

const escapeAttribute = (value) =>
  String(value).replace(/[&<>'"]/g, (char) => ({
    '&': '&amp;',
    '<': '&lt;',
    '>': '&gt;',
    "'": '&#39;',
    '"': '&quot;',
  }[char]));

const TYPE_META = {
  success: { icon: 'check-circle', color: 'text-emerald-500', bar: 'bg-emerald-500' },
  error: { icon: 'circle-x', color: 'text-rose-500', bar: 'bg-rose-500' },
  warning: { icon: 'alert-triangle', color: 'text-amber-500', bar: 'bg-amber-500' },
  info: { icon: 'sparkles', color: 'text-sky-500', bar: 'bg-sky-500' },
};

/**
 * ToastStack - Vanilla Web Component
 * Usage: <toast-stack duration="4000"></toast-stack>
 * JS API: el.push(message, type); el.dismiss(id); el.toasts = [...]
 * Events: dismiss (detail: { id })
 * Each toast carries a bottom countdown bar (h-0.5) that shrinks to 0 over
 * `duration` ms and pauses while hovered. duration="0" = sticky toasts.
 */
export class ToastStackElement extends HTMLElement {
  static get observedAttributes() {
    return ['duration'];
  }

  constructor() {
    super();
    this._toasts = [];
    this._clocks = new Map(); // id -> { started, startPct, total, timer, ticker }
    this._seq = 0;
    this._appliedLayout = '';
  }

  get toasts() {
    return this._toasts;
  }

  set toasts(val) {
    this._toasts = Array.isArray(val) ? val : [];
    this._syncClocks();
    this.render();
  }

  get duration() {
    const parsed = parseInt(this.getAttribute('duration') || '4000', 10);
    return Number.isNaN(parsed) ? 4000 : parsed;
  }

  push(message, type = 'info') {
    const id = ++this._seq;
    this._toasts = [...this._toasts, { id, message, type }];
    this._syncClocks();
    this.render();
    return id;
  }

  dismiss(id) {
    this._stopClock(id);
    this._toasts = this._toasts.filter((t) => t.id !== id);
    this.dispatchEvent(new CustomEvent('dismiss', { detail: { id } }));
    this.render();
  }

  attributeChangedCallback() {
    this._syncClocks();
    this.render();
  }

  disconnectedCallback() {
    for (const id of [...this._clocks.keys()]) this._stopClock(id);
  }

  _stopClock(id) {
    const clock = this._clocks.get(id);
    if (clock) {
      if (clock.timer) clearTimeout(clock.timer);
      if (clock.ticker) clearInterval(clock.ticker);
      this._clocks.delete(id);
    }
  }

  _startClock(id) {
    const clock = this._clocks.get(id);
    if (!clock || clock.total <= 0 || this.duration <= 0) return;
    const started = Date.now();
    clock.started = started;
    clock.timer = setTimeout(() => {
      clock.pct = 0;
      this._updateBar(id, 0);
      this._stopClock(id);
      this.dismiss(id);
    }, clock.total);
    clock.ticker = setInterval(() => {
      const pct = Math.max(0, clock.startPct * (1 - (Date.now() - started) / clock.total));
      clock.pct = pct;
      this._updateBar(id, pct);
    }, 100);
  }

  _updateBar(id, pct) {
    const bar = this.querySelector(`[data-bar-id="${id}"]`);
    if (bar) bar.style.width = `${pct}%`;
  }

  _pauseClock(id) {
    const clock = this._clocks.get(id);
    if (!clock) return;
    if (clock.timer) clearTimeout(clock.timer);
    if (clock.ticker) clearInterval(clock.ticker);
    const elapsed = Date.now() - clock.started;
    clock.startPct = Math.max(0, clock.startPct * (1 - elapsed / clock.total));
    clock.total = Math.max(0, clock.total - elapsed);
  }

  _syncClocks() {
    if (this.duration <= 0) {
      for (const id of [...this._clocks.keys()]) this._stopClock(id);
      return;
    }
    const alive = new Set(this._toasts.map((t) => t.id));
    for (const id of [...this._clocks.keys()]) {
      if (!alive.has(id)) this._stopClock(id);
    }
    for (const t of this._toasts) {
      if (!this._clocks.has(t.id)) {
        this._clocks.set(t.id, { started: Date.now(), startPct: 100, total: this.duration, pct: 100 });
        this._startClock(t.id);
      }
    }
  }

  render() {
    if (this._toasts.length === 0) {
      this.innerHTML = '';
      return;
    }

    const layoutClass = 'fixed bottom-4 right-4 z-50 flex flex-col gap-2 w-80 max-w-[calc(100vw-2rem)]';
    const prev = this._appliedLayout || '';
    let user = this.className || '';
    if (prev && user.startsWith(prev)) user = user.slice(prev.length).trim();
    this._appliedLayout = layoutClass;
    const next = `${layoutClass}${user ? ` ${user}` : ''}`;
    if (this.className !== next) this.className = next;
    this.setAttribute('aria-live', 'polite');

    this.innerHTML = this._toasts
      .map((t) => {
        const meta = TYPE_META[t.type] || TYPE_META.info;
        const clock = this._clocks.get(t.id);
        const pct = clock ? (clock.pct ?? 100) : 100;
        return `
          <div class="toast-card relative overflow-hidden flex items-center gap-2.5 px-3.5 py-2.5 rounded-xl border border-zinc-200 dark:border-zinc-700 bg-white dark:bg-zinc-900 shadow-lg text-sm text-zinc-800 dark:text-zinc-200" role="alert" data-toast-id="${escapeAttribute(t.id)}">
            ${uiIcon(meta.icon, { size: 16, className: `${meta.color} shrink-0` })}
            <span class="flex-1 min-w-0">${escapeAttribute(t.message)}</span>
            <button type="button" data-dismiss-id="${escapeAttribute(t.id)}" class="toast-dismiss text-zinc-400 hover:text-zinc-600 dark:hover:text-zinc-200 transition-colors shrink-0" aria-label="Dismiss">
              ${uiIcon('x', { size: 14 })}
            </button>
            ${this.duration > 0 ? `<span data-bar-id="${escapeAttribute(t.id)}" aria-hidden="true" class="absolute bottom-0 left-0 h-0.5 ${meta.bar} transition-[width] duration-100 ease-linear" style="width:${pct}%"></span>` : ''}
          </div>
        `;
      })
      .join('');

    this.querySelectorAll('.toast-card').forEach((card) => {
      const id = card.getAttribute('data-toast-id');
      const toast = this._toasts.find((t) => String(t.id) === String(id));
      if (!toast) return;
      card.addEventListener('mouseenter', () => this._pauseClock(toast.id));
      card.addEventListener('mouseleave', () => this._startClock(toast.id));
    });
    this.querySelectorAll('.toast-dismiss').forEach((btn) => {
      btn.addEventListener('click', () => {
        const id = btn.getAttribute('data-dismiss-id');
        const toast = this._toasts.find((t) => String(t.id) === String(id));
        if (toast) this.dismiss(toast.id);
      });
    });
  }
}

if (typeof customElements !== 'undefined' && !customElements.get('toast-stack')) {
  customElements.define('toast-stack', ToastStackElement);
}
export default ToastStackElement;
