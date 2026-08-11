const fs=require('fs'); const {JSDOM}=require('jsdom');
const dom=new JSDOM(fs.readFileSync(require('path').join(__dirname,'..','..','vtc','vtc_app_v3.html'),'utf8'),
 {runScripts:'dangerously',pretendToBeVisual:true,url:'http://127.0.0.1/x.html',
  beforeParse(w){w.matchMedia=()=>({matches:false,addListener(){},removeListener(){},addEventListener(){},removeEventListener(){}});
   w.HTMLMediaElement.prototype.play=()=>Promise.resolve();w.HTMLMediaElement.prototype.pause=()=>{};w.HTMLMediaElement.prototype.load=()=>{};}});
const w=dom.window;
setTimeout(()=>{
  const d=w.document;
  const q=['Weird Al.mp4','Peter North.avi','1883 S01E03.mkv','Andor S01E01.mkv','Chernobyl S01E03.mkv'];
  w.pgStart(q.length, 600, q);
  // two workers, non-adjacent in the queue — exactly the reported case
  w.pgFile('1883 S01E03.mkv', 0.0, {});
  w.pgFile('Chernobyl S01E03.mkv', 0.0, {});
  const rows=[...d.querySelectorAll('#pg-recent .pg-r')].map(r=>{
    const s=r.querySelector('span');
    return (s?s.textContent:'') + ' | ' + (r.querySelector('.pg-r-t')||{textContent:''}).textContent.trim();
  });
  const names=rows.map(r=>r.split(' | ')[0]);
  const dupes=names.filter((n,i)=>names.indexOf(n)!==i);
  process.stdout.write(JSON.stringify({rows, duplicates:[...new Set(dupes)]}, null, 1));
  process.exit(0);
}, 1200);
