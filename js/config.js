/* zcode2api — 前后端分离配置（2.7.34）。
 * 前端可部署到任意静态源；跨源部署时在部署产物里改这一行：
 *   window.ZCODE_API_BASE = "https://zcode.mangoqwq.com";
 * 默认空 = 同源相对路径（本机 /static 服务形态）。 */
window.ZCODE_API_BASE = window.ZCODE_API_BASE || "";
