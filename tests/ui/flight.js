// Several files are in flight at once whenever jobs>1. This drives the real
// progress modal with three concurrent workers and checks none of them is lost.
const fs=require('fs'); const {JSDOM}=require('jsdom');
const dom=new JSDOM(fs.readFileSync(require('path').join(__dirname,'..','..','vtc','vtc_app_v3.html'),'utf8'),
 {runScripts:'dangerously',pretendToBeVisual:true,url:'http://127.0.0.1/x.html',
  beforeParse(w){w.matchMedia=()=>({matches:false,addListener(){},removeListener(){},addEventListener(){},removeEventListener(){}});
   w.HTMLMediaElement.prototype.play=()=>Promise.resolve();w.HTMLMediaElement.prototype.pause=()=>{};w.HTMLMediaElement.prototype.load=()=>{};}});
const w=dom.window; const out=[];
const ck=(n,g,e)=>out.push({ok:JSON.stringify(g)===JSON.stringify(e),n,g,e});
setTimeout(()=>{
  const d=w.document, $=s=>d.querySelector(s);
  // a run of 4 work files, 3 encoding at once (jobs=3)
  w.pgStart(4, 600, ['a.mkv','b.mkv','c.mkv','d.mkv']);
  w.pgFile('a.mkv', 0.10, {});
  w.pgFile('b.mkv', 0.40, {});
  w.pgFile('c.mkv', 0.75, {});
  const rows=[...d.querySelectorAll('#pg-recent .pg-r')];
  const proc=rows.filter(r=>/processing/.test(r.textContent)).map(r=>r.querySelector('span').textContent);
  ck('all three in-flight files show as processing', proc.sort(), ['a.mkv','b.mkv','c.mkv']);
  ck('each row carries its own percentage',
     rows.filter(r=>/processing · \d+%/.test(r.textContent)).length, 3);
  ck('headline names the count', /Encoding 3 files/.test($('#pg-cur-n').textContent), true);
  ck('per-file bar tracks the SLOWEST (the one being waited on)',
     $('#pg-cur-f').style.width, '10%');
  // one finishes: it leaves the flight, the others stay
  w.pgDone1({f:'b.mkv', t:'ok'});
  const rows2=[...d.querySelectorAll('#pg-recent .pg-r')];
  const proc2=rows2.filter(r=>/processing/.test(r.textContent)).map(r=>r.querySelector('span').textContent);
  ck('a finished file leaves the flight', proc2.sort(), ['a.mkv','c.mkv']);
  ck('...and is marked done', /done/.test(rows2.find(r=>/b\.mkv/.test(r.textContent)).textContent), true);
  ck('progress counts the work cohort', $('#pg-done').textContent+' of '+$('#pg-total').textContent, '1 of 4');

  // Reported from a real run: a `converted/` folder beside the originals puts the
  // SAME filename in the queue twice. Keying in-flight by name lit up every copy,
  // so one encoding file showed as two rows both saying "processing".
  const dup=['a.mkv','dup.mkv','b.mkv','dup.mkv','c.mkv'];
  w.pgStart(dup.length, 600, dup);
  w.pgFile('dup.mkv', 0.2, {});
  const procRows=[...d.querySelectorAll('#pg-recent .pg-r')].filter(r=>/processing/.test(r.textContent));
  ck('a duplicated filename shows ONE processing row', procRows.length, 1);
  process.stdout.write(JSON.stringify(out)); process.exit(0);
}, 1200);
