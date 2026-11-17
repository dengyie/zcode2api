/**
 * StatusIndicator - Vanilla Web Component
 * Usage: <status-indicator status="online" label="VPS Connected" ping-ms="24"></status-indicator>
 */
const escapeAttribute = (value) =>
  String(value ?? '')
    .replace(/&/g, '&amp;')
    .replace(/"/g, '&quot;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;');

class StatusIndicator extends HTMLElement {
  static get observedAttributes() {
    return ["status", "label", "ping-ms", "show-dot"];
  }

  connectedCallback() {
    this.render();
  }

  attributeChangedCallback() {
    this.render();
  }

  render() {
    const status = this.getAttribute("status") || "online";
    const label = escapeAttribute(this.getAttribute("label"));
    const pingMs = escapeAttribute(this.getAttribute("ping-ms"));
    const showDot = this.getAttribute("show-dot") !== "false";

    let dotBg = "bg-emerald-500", pingBg = "bg-emerald-400", text = "text-emerald-700 dark:text-emerald-400", badgeBg = "bg-emerald-50 dark:bg-emerald-950/40 border-emerald-200/80 dark:border-emerald-800/60", defaultLabel = "Online";
    if (status === "connecting" || status === "busy") {
      dotBg = "bg-amber-500";
      pingBg = "bg-amber-400";
      text = "text-amber-700 dark:text-amber-400";
      badgeBg = "bg-amber-50 dark:bg-amber-950/40 border-amber-200/80 dark:border-amber-800/60";
      defaultLabel = status === "busy" ? "Busy" : "Connecting";
    } else if (status === "error" || status === "offline") {
      dotBg = "bg-rose-500";
      pingBg = "bg-rose-400";
      text = "text-rose-700 dark:text-rose-400";
      badgeBg = "bg-rose-50 dark:bg-rose-950/40 border-rose-200/80 dark:border-rose-800/60";
      defaultLabel = status === "error" ? "Error" : "Offline";
    }

    this.innerHTML = `
      <div class="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full border text-xs font-medium ${badgeBg} ${text}">
        ${showDot ? `
          <span class="relative flex h-2 w-2">
            ${status === 'online' ? `<span class="animate-ping absolute inline-flex h-full w-full rounded-full opacity-75 ${pingBg}"></span>` : ''}
            <span class="relative inline-flex rounded-full h-2 w-2 ${dotBg}"></span>
          </span>
        ` : ''}
        <span class="font-semibold">${label || defaultLabel}</span>
        ${pingMs !== null && pingMs !== undefined ? `<span class="font-mono text-[10px] opacity-75 ml-0.5">(${pingMs}ms)</span>` : ''}
      </div>
    `;
  }
}

if (!customElements.get("status-indicator")) {
  customElements.define("status-indicator", StatusIndicator);
}
export default StatusIndicator;
