const { JSDOM, VirtualConsole } = require('jsdom');
const vc = new VirtualConsole();
vc.on('jsdomError', (e) => console.error('JSDOM_ERR:', e.message));
const html = `<!DOCTYPE html><html><head></head><body>
<div id="cap-holder"></div>
<button id="cap-btn" type="button"></button>
<script src="https://o.alicdn.com/captcha-frontend/aliyunCaptcha/AliyunCaptcha.js"></script>
</body></html>`;
const dom = new JSDOM(html, {
  url: 'https://zcode.z.ai/', runScripts: 'dangerously', resources: 'usable',
  pretendToBeVisual: true, virtualConsole: vc,
  beforeParse(window) {
    window.matchMedia = () => ({ matches:false, media:'', onchange:null, addListener(){}, removeListener(){}, addEventListener(){}, removeEventListener(){}, dispatchEvent(){return false;} });
    const proto = window.HTMLCanvasElement.prototype;
    proto.getContext = function (type) {
      if (/webgl/i.test(type)) { return { canvas:this, getParameter:()=>'Intel', getExtension:()=>null, getSupportedExtensions:()=>['WEBGL_debug_renderer_info'], getContextAttributes:()=>({}), getShaderPrecisionFormat:()=>({precision:23,rangeMin:127,rangeMax:127}) }; }
      return { canvas:this, fillRect(){}, clearRect(){}, getImageData:(x,y,w=1,h=1)=>({data:new Uint8ClampedArray(w*h*4)}), putImageData(){}, createImageData:(w=1,h=1)=>({data:new Uint8ClampedArray(w*h*4)}), setTransform(){}, transform(){}, drawImage(){}, save(){}, restore(){}, beginPath(){}, moveTo(){}, lineTo(){}, bezierCurveTo(){}, quadraticCurveTo(){}, closePath(){}, clip(){}, stroke(){}, fill(){}, arc(){}, rect(){}, ellipse(){}, translate(){}, scale(){}, rotate(){}, fillText(){}, strokeText(){}, measureText:(t)=>({width:(''+t).length*8}), createLinearGradient:()=>({addColorStop(){}}), createRadialGradient:()=>({addColorStop(){}}), createPattern:()=>({}), isPointInPath:()=>false, font:'10px sans-serif', textBaseline:'alphabetic', textAlign:'start', fillStyle:'#000', strokeStyle:'#000', globalAlpha:1, lineWidth:1, shadowBlur:0, shadowColor:'' };
    };
    proto.toDataURL = () => 'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==';
    proto.toBlob = (cb) => cb && cb(null);
    window.Worker = class { postMessage(){} terminate(){} addEventListener(){} removeEventListener(){} onmessage=null; onerror=null; };
    window.OffscreenCanvas = window.OffscreenCanvas || class { constructor(w,h){this.width=w;this.height=h;} getContext(){return proto.getContext.call(this);} };
  },
});
const { window } = dom;
function waitFor(cond, t = 12000) { return new Promise((res, rej) => { const s = Date.now(); const i = setInterval(() => { let ok=false; try{ok=cond();}catch{} if(ok){clearInterval(i);res();} else if(Date.now()-s>t){clearInterval(i);rej(new Error('timeout'));} }, 80); }); }
(async () => {
  try { await waitFor(() => typeof window.initAliyunCaptcha === 'function'); console.log('SDK LOADED'); } catch { console.log('SDK NEVER LOADED'); process.exit(3); }
  window.AliyunCaptchaConfig = { region: 'cn', prefix: 'no8xfe' };
  let instanceRef = null;
  window.initAliyunCaptcha({
    SceneId: '11xygtvd', mode: 'popup', language: 'zh-CN', showErrorTip: false,
    element: '#cap-holder', button: '#cap-btn',
    getInstance: (inst) => {
      instanceRef = inst;
      console.log('INSTANCE keys:', Object.keys(inst));
      try { if (inst.startTracelessVerification) inst.startTracelessVerification(); } catch (e) { console.error('start err', e.message); }
    },
    success: (param) => { console.log('SUCCESS:', param); process.exit(0); },
    fail: (p) => { console.log('FAIL:', JSON.stringify(p)); },
    onError: (p) => { console.log('ONERROR:', JSON.stringify(p)); },
  });
  setTimeout(() => {
    console.log('TIMEOUT. instance methods:', instanceRef ? Object.getOwnPropertyNames(Object.getPrototypeOf(instanceRef)) : null);
    // Check if there's a captcha object with show()
    if (instanceRef && instanceRef.captcha) {
      console.log('captcha obj keys:', Object.keys(instanceRef.captcha));
    }
    console.log('window.AliyunCaptcha?', typeof window.AliyunCaptcha);
    process.exit(2);
  }, 25000);
})().catch((e) => { console.log('TOP ERR', e); process.exit(3); });
