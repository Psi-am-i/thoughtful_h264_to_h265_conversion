// Going back to change an answer must not strand you: the question you were on
// has to stay clickable, without re-confirming everything in between.
const fs=require('fs'); const {JSDOM}=require('jsdom');
const dom=new JSDOM(fs.readFileSync(require('path').join(__dirname,'..','..','vtc','vtc_app_v3.html'),'utf8'),
 {runScripts:'dangerously',pretendToBeVisual:true,url:'http://127.0.0.1/x.html',
  beforeParse(w){w.matchMedia=()=>({matches:false,addListener(){},removeListener(){},addEventListener(){},removeEventListener(){}});
   w.HTMLMediaElement.prototype.play=()=>Promise.resolve();w.HTMLMediaElement.prototype.pause=()=>{};w.HTMLMediaElement.prototype.load=()=>{};}});
const w=dom.window, out=[];
const ck=(n,g,e)=>out.push({ok:JSON.stringify(g)===JSON.stringify(e),n,g,e});
setTimeout(()=>{
  const d=w.document, $=s=>d.querySelector(s);
  const teeth=()=>[...d.querySelectorAll('#comb-steps .tooth')];
  const M=w.eval('M'), answers=w.eval('answers');  // page-scope consts, not window props
  // answer the first four; sit on DESTINATION (the fifth), unanswered
  M.slice(0,4).forEach((q,i)=>{ answers[q.id]=0; });
  w.eval('step=4; armed=null;'); w.render();
  ck('the unanswered question you are on is clickable', teeth()[4].disabled, false);

  // go back to QUALITY (index 1) the way a user does — click its block
  teeth()[1].dispatchEvent(new w.MouseEvent('click',{bubbles:true}));
  ck('clicking an answered step goes back to it', w.eval('step'), 1);

  // ...and the question we came from must still be reachable
  ck('the question you came from is STILL clickable', teeth()[4].disabled, false);
  ck('...as are the answered ones between', [teeth()[2].disabled, teeth()[3].disabled], [false,false]);

  teeth()[4].dispatchEvent(new w.MouseEvent('click',{bubbles:true}));
  ck('and you can jump straight back to it', w.eval('step'), 4);

  // a question further on than the next unanswered one stays out of reach
  M.forEach(q=>delete answers[q.id]); w.eval('step=0; armed=null;'); w.render();
  ck('with nothing answered, step 3 is not reachable', teeth()[3].disabled, true);
  ck('...but the one you are on is', teeth()[0].disabled, false);
  process.stdout.write(JSON.stringify(out)); process.exit(0);
}, 1200);
