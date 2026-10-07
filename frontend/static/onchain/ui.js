/** Presentation only: no network, routing, wallet or ranking mutations. */
import '@onchain/motion.js';

const shell=document.getElementById('app-shell');
const intro=document.getElementById('engn-intro');
const login=document.getElementById('login-screen');
const views={
  gems:['01 / GEM FINDER','Find the signal.','Leave the noise.','Projects, research and rankings. Explore the top picks or look deeper into the database.'],
  meme:['01 / SIGNALS','Every signal,','as it fires.','The simulated Top 50 wallet: its latest buys and sells, what it holds, what waits for an entry.'],
  top:['02 / TRACK RECORD','Every pick,','checked.','Each gem is frozen with its receipt when picked, then measured against the market after 1, 3, 7, 30 and 90 days.'],
  agent:['03 / ENGINE ROOM','The engine,','at work.','Follow the research and analysis behind the app. Every update stays connected to its source.'],
  profile:['04 / YOUR PROFILE','Your activity.','One clear view.','Wallets, holdings and performance, with your account controls close at hand.'],
  wallet:['05 / YOUR WALLET','Your assets.','Your decisions.','Review balances and history in one workspace.']
};
function paint(){
  if(!shell||!intro)return;
  const signedIn=!shell.hidden;
  document.documentElement.classList.toggle('engn-signed-in',signedIn);
  intro.hidden=!signedIn;
  if(!signedIn)return;
  const data=views[shell.dataset.view]||views.gems;
  intro.querySelector('[data-engn-kicker]').textContent=data[0];
  intro.querySelector('[data-engn-title]').textContent=data[1];
  intro.querySelector('[data-engn-em]').textContent=data[2];
  intro.querySelector('[data-engn-copy]').textContent=data[3];
}
if(shell){new MutationObserver(paint).observe(shell,{attributes:true,attributeFilter:['hidden','data-view']});paint()}
if(login)new MutationObserver(paint).observe(login,{attributes:true,attributeFilter:['hidden']});
// The existing desktop detail pane is preserved; enrich its empty state only.
function decorateEmpty(){
  const empty=document.querySelector('.desk-empty');
  if(!empty||empty.dataset.engnDecorated)return;
  empty.dataset.engnDecorated='true';
  const box=document.createElement('div');box.className='engn-empty-detail';
  const motion=document.createElement('engn-motion');motion.setAttribute('state','idle');motion.setAttribute('decorative','');
  const label=document.createElement('span');label.className='engn-eyebrow';label.textContent='THE RESEARCH DESK';
  const heading=document.createElement('h2');heading.textContent='A closer look starts here.';
  const p=document.createElement('p');p.textContent='Select a token to explore its chart and research.';
  box.append(motion,label,heading,p);empty.replaceChildren(box);
}
decorateEmpty();
// main.js creates the empty pane during module initialization.
if(!document.querySelector('.desk-empty')){
  const pending=new MutationObserver(()=>{if(document.querySelector('.desk-empty')){decorateEmpty();pending.disconnect()}});
  pending.observe(document.body,{childList:true});
}
