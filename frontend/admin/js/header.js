/* 后台公共头部渲染 */
async function renderAdminHeader(){
  const mount=document.getElementById('admin-header');
  if(!mount)return;
  let version='';
  try{const r=await fetch((window.ZCODE_API_BASE || '') + '/meta');if(r.ok)version='v'+(await r.json()).version;}catch{}
  // 相对文件名导航：同源 pretty URL 与任意静态托管（GitHub Pages 等）通吃
  const here=location.pathname.split("/").pop()||"accounts.html";
  const nav=[
    ["accounts.html","账号池"],
    ["monitoring.html","请求监控"],
    ["settings.html","设置"],
  ].map(([href,label])=>
    `<a href="${href}" class="admin-nav-link${href===here?' active':''}">${label}</a>`
  ).join('');
  mount.innerHTML=`
    <header class="admin-header">
      <div class="admin-header-inner">
        <div class="admin-brand-wrap">
          <span class="admin-brand">Z<span class="tick">·</span>HUB</span>
        </div>
        <nav class="admin-nav">${nav}</nav>
        <div class="admin-header-right">
          ${version?`<span class="admin-header-version">${version}</span>`:''}
          <button onclick="adminLogout()" class="admin-header-icon-btn" title="退出登录" aria-label="退出登录">
            <svg viewBox="0 0 24 24"><path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/><path d="M16 17l5-5-5-5"/><path d="M21 12H9"/></svg>
          </button>
        </div>
      </div>
    </header>`;
}
