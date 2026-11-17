/**
 * ThemeToggle - Vanilla Web Component (Auto / Light / Dark)
 * Usage: <theme-toggle storage-key="mango-theme"></theme-toggle>
 */
class ThemeToggle extends HTMLElement {
  static get observedAttributes() {
    return ["storage-key"];
  }

  connectedCallback() {
    this.storageKey = this.getAttribute("storage-key") || "mango-theme";
    this.mode = localStorage.getItem(this.storageKey) || "auto";
    this.render();
    this.applyTheme(this.mode);
    this.bindEvents();
  }

  resolveTheme(m) {
    if (m === "light" || m === "dark") return m;
    const hour = new Date().getHours();
    const isNight = window.matchMedia("(prefers-color-scheme: dark)").matches || hour >= 19 || hour < 7;
    return isNight ? "dark" : "light";
  }

  applyTheme(nextMode) {
    this.mode = nextMode;
    const resolved = this.resolveTheme(nextMode);
    document.documentElement.dataset.theme = nextMode;
    document.documentElement.dataset.resolvedTheme = resolved;
    document.documentElement.style.colorScheme = resolved;
    if (resolved === "dark") {
      document.documentElement.classList.add("dark");
    } else {
      document.documentElement.classList.remove("dark");
    }
    localStorage.setItem(this.storageKey, nextMode);
    this.updateActiveButtons();
    this.dispatchEvent(new CustomEvent("change", { detail: { mode: nextMode, resolved } }));
  }

  updateActiveButtons() {
    this.querySelectorAll("[data-mode]").forEach((btn) => {
      const isCurrent = btn.dataset.mode === this.mode;
      btn.className = `flex items-center gap-1.5 px-2.5 py-1 rounded-lg transition-all ${
        isCurrent
          ? "bg-white dark:bg-zinc-900 text-zinc-900 dark:text-zinc-100 shadow-xs font-semibold"
          : "hover:text-zinc-900 dark:hover:text-zinc-100"
      }`;
    });
  }

  render() {
    this.innerHTML = `
      <div class="inline-flex items-center p-1 rounded-xl bg-zinc-100 dark:bg-zinc-800/80 border border-zinc-200/80 dark:border-zinc-700/80 text-xs font-medium text-zinc-500 dark:text-zinc-400 select-none" role="group" aria-label="Theme toggle">
        <button type="button" data-mode="auto" title="Follow System/Time">
          <span>💻</span>
          <span>Auto</span>
        </button>
        <button type="button" data-mode="light" title="Light Mode">
          <span>☀️</span>
          <span>Light</span>
        </button>
        <button type="button" data-mode="dark" title="Dark Mode">
          <span>🌙</span>
          <span>Dark</span>
        </button>
      </div>
    `;
  }

  bindEvents() {
    this.addEventListener("click", (e) => {
      const btn = e.target.closest("[data-mode]");
      if (btn) {
        this.applyTheme(btn.dataset.mode);
      }
    });
  }
}

if (!customElements.get("theme-toggle")) {
  customElements.define("theme-toggle", ThemeToggle);
}
export default ThemeToggle;
