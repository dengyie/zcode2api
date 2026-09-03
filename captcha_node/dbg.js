const { JSDOM, VirtualConsole } = require('jsdom');
const vc = new VirtualConsole();
vc.on('jsdomError', (e) => console.error('JSDOM_ERR:', e.message, (e.detail && e.detail.message) || ''));
vc.on('error', (...a) => console.error('CONSOLE_ERR:', ...a));
const html = `<!DOCTYPE html><html><head></head><body>
<div id="cap"></div><button id="btn"></button>
<script src="https://o.alicdn.com/captcha-frontend/aliyunCaptcha/AliyunCaptcha.js"></script>
</body></html>`;
const dom = new JSDOM(html, {
  url: 'https://zcode.z.ai/', runScripts: 'dangerously', resources: 'usable',
  pretendToBeVisual: true, virtualConsole: vc,
  beforeParse(window) {
    window.matchMedia = () => ({ matches:false, media:'', onchange:null, addListener(){}, removeListener(){}, addEventListener(){}, removeEventListener(){}, dispatchEvent(){return false;} });
    const proto = window.HTMLCanvasElement.prototype;
    proto.getContext = function () { return { canvas:this, getParameter:()=>'Intel', getExtension:()=>null, getSupportedExtensions:()=>['WEBGL_debug_renderer_info'], getContextAttributes:()=>({}), getShaderPrecisionFormat:()=>({precision:23,rangeMin:127,rangeMax:127}) }; };
    proto.toDataURL = () => 'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==';
    window.Worker = class { postMessage(){} terminate(){} addEventListener(){} removeEventListener(){} onmessage=null; onerror=null; };
  },
});
const { window } = dom;
function waitFor(cond, t = 12000) {
  return new Promise((res, rej) => {
    const s = Date.now();
    const i = setInterval(() => { let ok=false; try{ok=cond();}catch{} if(ok){clearInterval(i);res();} else if(Date.now()-s>t){clearInterval(i);rej(new Error('timeout'));} }, 80);
  });
}
(async () => {
  try { await waitFor(() => typeof window.initAliyunCaptcha === 'function'); console.log('SDK LOADED'); } catch { console.log('SDK NEVER LOADED'); process.exit(3); }
  window.initAliyunCaptcha({
    SceneId: '11xygtvd', mode: 'popup', region: 'cn', prefix: 'no8xfe',
    element: '#cap', button: '#btn', captchaLogoImg: '', showErrorTip: true,
    getInstance: (inst) => { console.log('GOT INSTANCE, keys:', Object.keys(inst)); try { (inst.startTracelessVerification || inst.show).call(inst); } catch (e) { console.error('start', e.message); } },
    success: (param) => { console.log('VERIFY_PARAM=' + param); process.exit(0); },
    fail: (e) => { console.log('FAIL:', JSON.stringify(e)); process.exit(4); },
    onError: (e) => { console.log('ONERROR:', JSON.stringify(e)); process.exit(5); },
  });
  setTimeout(() => process.exit(2), 25000);
})().catch((e) => { console.log('TOP ERR', e); process.exit(3); });
