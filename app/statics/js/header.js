/* 后台公共头部渲染 */
async function renderAdminHeader(){
  const mount=document.getElementById('admin-header');
  if(!mount)return;
  let version='';
  try{const r=await fetch('/meta');if(r.ok)version='v'+(await r.json()).version;}catch{}
  const active=mount.dataset.active||location.pathname;
  const nav=[
    ['/admin/accounts','账号池'],
    ['/admin/settings','设置'],
  ].map(([href,label])=>
    `<a href="${href}" class="admin-nav-link${href===active?' active':''}">${label}</a>`
  ).join('');
  mount.innerHTML=`
    <header class="admin-header">
      <div class="admin-header-inner">
        <div class="admin-brand-wrap">
          <span class="admin-brand">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 2 2 7l10 5 10-5-10-5Z"/><path d="m2 17 10 5 10-5"/><path d="m2 12 10 5 10-5"/></svg>
            zcode2api
          </span>
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
