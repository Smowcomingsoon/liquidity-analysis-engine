/**
 * The ENGN cube as a state icon: <engn-motion state="loading|scan|...">.
 * From the motion kit in design/engn-motion-kit (PR #383), no dependencies.
 * Only transform and opacity animate (the kit's stroke-width and r keyframes
 * were rewritten), paused off screen and when the tab is hidden, still under
 * prefers-reduced-motion. States must mirror real events: loading while a
 * request is pending, reconnect while retrying, never a fake success.
 */
export const motions = [
 ['assemble','Engine boot','decorative',3000,'Hero entrance'],
 ['idle','Engine alive','decorative',4800,'Quiet brand background'],
 ['scan','Radar scan','functional',2400,'A scan really running'],
 ['loading','Processing','functional',1600,'Wait of unknown length'],
 ['progress','Measured progress','functional',0,'Numeric progress from the backend'],
 ['success','Completed','functional',700,'Confirmed completion'],
 ['warning','Needs attention','functional',900,'Needs attention'],
 ['error','Could not complete','functional',500,'Recoverable error'],
 ['empty','No results','functional',0,'No results for the filters'],
 ['reconnect','Reconnecting','functional',2200,'Real reconnection attempt'],
 ['cta','Activate engine','decorative',850,'Hover or focus on a call to action'],
 ['transition','Module handoff','decorative',900,'Panel or section change']
].map(([id,title,category,duration,use])=>({id,title,category,duration,use}));

export function safeState(value){return motions.some(m=>m.id===value)?value:'idle'}
export function safeProgress(value){const n=Number(value);return Number.isFinite(n)?Math.min(100,Math.max(0,n)):0}
const style=`
svg{--mint:var(--engn-accent,#2de2b0);--dim:var(--engn-muted,#49605d);--alert:var(--engn-warning,#eabb69);--bad:var(--engn-error,#ef827f);overflow:visible}
.top{fill:#252d33;stroke:#35413f}.left{fill:#0b1115;stroke:#293533}.right{fill:#0e191b;stroke:#2b3e39}.facet{stroke-width:.7;stroke-linejoin:round}.outline{fill:none;stroke:var(--mint);stroke-width:1;opacity:.13}.band{fill:none;stroke:var(--mint);stroke-width:2.7;stroke-linecap:round;stroke-linejoin:round}.halo{stroke-width:9;opacity:.12}.ground{fill:none;stroke:var(--dim);opacity:.16;stroke-width:.7}.ring{fill:none;stroke:var(--mint);stroke-width:1;opacity:.2}.orbit{transform-origin:160px 158px}.module{transform-box:fill-box;transform-origin:center;animation:assemble 3s both;animation-delay:0s}.face{transform-origin:160px 155px}.scanline{stroke:var(--mint);stroke-width:2;opacity:0}.signal{fill:var(--mint)}.badge{fill:#0b1516;stroke:var(--mint);stroke-width:1.3}.mark{fill:none;stroke:var(--mint);stroke-width:2.5;stroke-linecap:round;stroke-linejoin:round}.status-mark{transform-origin:225px 215px}.pulse{fill:none;stroke:var(--mint);opacity:0;transform-origin:225px 215px}.arc{fill:none;stroke:var(--mint);stroke-width:3;stroke-linecap:round;transform:rotate(-90deg);transform-origin:160px 158px}.track{fill:none;stroke:var(--dim);stroke-width:2;opacity:.22}.label{fill:var(--mint);font:11px monospace;text-anchor:middle}.s-assemble .solid{animation:reveal 3s both}.s-assemble .modules{animation:hideModules 3s both}.s-assemble .band{animation:ignite 3s both}.s-idle .face{animation:breathe 4.8s ease-in-out infinite}.s-idle .band{animation:glow 4.8s ease-in-out infinite}.s-scan .scanline{animation:scan 2.4s ease-in-out infinite}.s-scan .signal{animation:blip 2.4s infinite}.s-loading .orbit{animation:spin 1.6s linear infinite}.s-loading .band{animation:glow 1.6s ease-in-out infinite}.s-success .status-mark{animation:confirm .7s both}.s-success .pulse{animation:ripple .7s both}.s-warning{--mint:var(--alert)}.s-warning .status-mark{animation:confirm .9s both}.s-error{--mint:var(--bad)}.s-error .face{animation:nudge .5s both}.s-empty .band{stroke:var(--dim);opacity:.45}.s-empty .face{opacity:.55}.s-reconnect .link-dot{animation:connect 2.2s ease-in-out infinite;animation-delay:var(--delay)}.s-cta .face{animation:activate .85s both}.s-cta .band{animation:glow .85s both}.s-transition .face{animation:handoff .9s both}.paused *{animation-play-state:paused!important}
@keyframes assemble{0%,10%{transform:translate(var(--dx),var(--dy));opacity:.85}60%,100%{transform:translate(0,0);opacity:1}}
@keyframes reveal{0%,59%{opacity:0}64%,100%{opacity:1}}@keyframes hideModules{0%,59%{opacity:1}64%,100%{opacity:0}}
@keyframes ignite{0%,58%{opacity:0}68%,100%{opacity:1}}
@keyframes breathe{0%,100%{transform:translateY(0)}50%{transform:translateY(-3px)}}@keyframes glow{0%,100%{opacity:.65}50%{opacity:1}}
@keyframes scan{0%{transform:translateY(-46px);opacity:0}20%{opacity:.8}80%{opacity:.8}100%{transform:translateY(48px);opacity:0}}
@keyframes blip{0%,60%,100%{opacity:.15}70%,80%{opacity:1}}@keyframes spin{to{transform:rotate(360deg)}}
@keyframes confirm{0%{transform:scale(.75);opacity:0}65%,100%{transform:scale(1);opacity:1}}@keyframes ripple{0%{transform:scale(1);opacity:.6}100%{transform:scale(1.77);opacity:0}}
@keyframes nudge{0%,100%{transform:translateX(0)}25%{transform:translateX(-3px)}60%{transform:translateX(3px)}}
@keyframes connect{0%,100%{opacity:.12}50%{opacity:1}}@keyframes activate{0%{transform:translateY(0)}60%,100%{transform:translateY(-5px)}}
@keyframes handoff{0%{transform:translateX(-12px);opacity:0}100%{transform:translateX(0);opacity:1}}
@media(prefers-reduced-motion:reduce){*{animation:none!important}.s-assemble .modules{display:none}.s-assemble .solid{opacity:1}.s-scan .scanline{opacity:.65}.status-mark{opacity:1}.link-dot{opacity:.55}.face{transform:none!important}.pulse{display:none}}
`;

export function svgMarkup(state='idle',progress=0,uid='engn',paused=false,inlineStyle=true){
 state=safeState(state);progress=safeProgress(progress);uid=String(uid).replace(/[^a-zA-Z0-9_-]/g,'');
 const info=motions.find(m=>m.id===state);
 let modules='';
 if(state==='assemble'){
 const coords=[];for(let x=0;x<3;x++)for(let y=0;y<3;y++)for(let z=0;z<3;z++)coords.push([x,y,z]);
 coords.sort((a,b)=>(a[0]+a[2]-b[0]-b[2])||a[1]-b[1]);
 modules=coords.map(([x,y,z])=>{const px=160+(x-z)*23.3,py=134+(x+z)*12.7-y*25.4;const dx=(x-z)*17,dy=(x+z-2)*9-(y-1)*19;return `<g transform="translate(${px},${py})"><g class="module" style="--dx:${dx}px;--dy:${dy}px"><path class="facet top" d="M0 -12.7 23.3 0 0 12.7 -23.3 0Z"/><path class="facet left" d="M-23.3 0 0 12.7 0 38.1 -23.3 25.4Z"/><path class="facet right" d="M0 12.7 23.3 0 23.3 25.4 0 38.1Z"/></g></g>`}).join('');
 }
 const solid=`<g class="solid"><path class="facet top" d="M160 70 230 108 160 146 90 108Z"/><path class="facet left" d="M90 108 160 146 160 223 90 185Z"/><path class="facet right" d="M160 146 230 108 230 185 160 223Z"/></g>`;
 const band=state==='assemble'?'<g class="solid">':'';
 let extra='';
 if(state==='scan')extra=`<g clip-path="url(#${uid}-clip)"><path class="scanline" d="M78 151 160 197 244 151"/></g><circle class="signal" cx="241" cy="105" r="3"/><path class="outline" d="M235 109 257 96H282"/>`;
 if(state==='loading')extra='<g class="orbit"><circle class="ring" cx="160" cy="158" r="108" stroke-dasharray="65 610" style="opacity:.8;stroke-width:2"/><circle class="signal" cx="160" cy="50" r="2.5"/></g>';
 if(state==='progress')extra=`<circle class="track" cx="160" cy="158" r="108"/><circle class="arc" cx="160" cy="158" r="108" pathLength="100" stroke-dasharray="${progress} 100"/><text class="label" x="160" y="287">${Math.round(progress)}%</text>`;
 if(['success','warning','error'].includes(state))extra=`<g class="status-mark"><circle class="badge" cx="225" cy="215" r="22"/><path class="mark" d="${state==='success'?'M215 215 222 222 236 207':state==='error'?'M218 208 232 222 M232 208 218 222':'M225 205V217 M225 224v.1'}"/></g>${state==='success'?'<circle class="pulse" cx="225" cy="215" r="26"/>':''}`;
 if(state==='empty')extra='<path class="outline" style="opacity:.5;stroke-dasharray:4" d="M75 209 160 255 246 209"/><path class="mark" style="stroke:var(--dim)" d="M213 215h20"/>';
 if(state==='reconnect')extra=[0,1,2,3,4].map(i=>`<circle class="signal link-dot" style="--delay:${i*.16}s" cx="${132+i*14}" cy="253" r="2.3"/>`).join('');
 if(state==='cta')extra='<path class="mark" style="stroke-width:1.5" d="M252 145h24m-7-7 7 7-7 7"/>';
 return `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 320 320" class="s-${state}${paused?' paused':''}" role="img" aria-labelledby="${uid}-title"><title id="${uid}-title">${info.title}${state==='progress'?` ${Math.round(progress)}%`:''}</title>${inlineStyle?`<style>${style}</style>`:''}<defs><clipPath id="${uid}-clip"><path d="M160 70 230 108V185L160 223 90 185V108Z"/></clipPath></defs><path class="ground" d="M61 221 160 275 259 221M80 231 179 177M107 246 206 192M134 260 233 207"/><g class="face">${state==='assemble'?`<g class="modules">${modules}</g>`:''}${solid}${band}<path class="band halo" d="M92 150 160 187 228 150"/><path class="band" d="M92 150 160 187 228 150"/>${state==='assemble'?'</g>':''}${extra}</g></svg>`;
}

let serial=0;
// One parsed stylesheet shared by every cube, instead of re-parsing ~3 KB of
// CSS in each shadow root on every render.
const HOST_CSS=':host{display:inline-block;width:160px;aspect-ratio:1;contain:layout style}svg{width:100%;height:100%;display:block}';
let shared=null;
try{if(globalThis.CSSStyleSheet&&'adoptedStyleSheets' in Document.prototype){shared=new CSSStyleSheet();shared.replaceSync(HOST_CSS+style)}}catch{shared=null}
export class EngnMotion extends (globalThis.HTMLElement||class{}){
 static observedAttributes=['state','progress','label','paused'];
 constructor(){super();this.attachShadow({mode:'open'});if(shared)this.shadowRoot.adoptedStyleSheets=[shared];this.uid=`engn-${++serial}`;this.offscreen=false;this.onVisibility=()=>this.updatePause();}
 connectedCallback(){this.render();this.observer=new IntersectionObserver(([entry])=>{this.offscreen=!entry.isIntersecting;this.updatePause()});this.observer.observe(this);document.addEventListener('visibilitychange',this.onVisibility)}
 disconnectedCallback(){this.observer?.disconnect();document.removeEventListener('visibilitychange',this.onVisibility);clearTimeout(this.timer)}
 attributeChangedCallback(){this.schedule()}
 schedule(){this.dirty=true;if(this.queued)return;this.queued=true;queueMicrotask(()=>{this.queued=false;if(this.dirty&&this.isConnected)this.render()})}
 get state(){return safeState(this.getAttribute('state'))}set state(v){this.setAttribute('state',safeState(v))}
 get progress(){return safeProgress(this.getAttribute('progress'))}set progress(v){this.setAttribute('progress',safeProgress(v))}
 play(state=this.state){if(this.getAttribute('state')===state)this.schedule();else this.setAttribute('state',state)}
 updatePause(){this.shadowRoot.querySelector('svg')?.classList.toggle('paused',this.hasAttribute('paused')||this.offscreen||document.hidden)}
 render(){
 this.dirty=false;
 this.shadowRoot.innerHTML=shared?svgMarkup(this.state,this.progress,this.uid,this.hasAttribute('paused'),false):`<style>${HOST_CSS}</style>${svgMarkup(this.state,this.progress,this.uid,this.hasAttribute('paused'))}`;
 const svg=this.shadowRoot.querySelector('svg');const label=this.getAttribute('label');if(label){svg.removeAttribute('aria-labelledby');svg.setAttribute('aria-label',label)}
 if(this.hasAttribute('decorative')){svg.setAttribute('aria-hidden','true');svg.removeAttribute('role')}
 clearTimeout(this.timer);this.updatePause();
 }
}
if(globalThis.customElements&&!customElements.get('engn-motion'))customElements.define('engn-motion',EngnMotion);

