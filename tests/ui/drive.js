// Load the REAL app page in a DOM and drive the Advanced-settings controls the
// way a person would (type in the box, click the toggle), then read back the
// state the engine is actually handed. This is the closest thing to clicking it
// that runs on this machine — the Chrome extension lives on the other box.
const fs = require('fs');
const { JSDOM, VirtualConsole } = require('jsdom');

const HTML = require('path').join(__dirname, '..', '..', 'vtc', 'vtc_app_v3.html');
const vc = new VirtualConsole();            // swallow page noise; we assert, not read logs
const errors = [];
vc.on('jsdomError', e => errors.push(String(e.message).slice(0, 140)));

const dom = new JSDOM(fs.readFileSync(HTML, 'utf8'), {
  runScripts: 'dangerously', pretendToBeVisual: true, virtualConsole: vc,
  url: 'http://127.0.0.1/vtc_app_v3.html',
  // These are jsdom gaps, not page bugs — every real browser and WKWebView has
  // them. They must exist BEFORE the page script parses, or it dies on the
  // reduced-motion query and every later `const` stays in the dead zone.
  beforeParse(w) {
    w.matchMedia = () => ({ matches: false, media: '', onchange: null,
      addListener() {}, removeListener() {},
      addEventListener() {}, removeEventListener() {}, dispatchEvent() { return false; } });
    w.HTMLMediaElement.prototype.play = () => Promise.resolve();
    w.HTMLMediaElement.prototype.pause = () => {};
    w.HTMLMediaElement.prototype.load = () => {};
    w.scrollTo = () => {};
    class Obs { observe() {} unobserve() {} disconnect() {} }
    w.IntersectionObserver = w.IntersectionObserver || Obs;
    w.ResizeObserver = w.ResizeObserver || Obs;
  },
});
const { window } = dom;

const results = [];
const check = (name, got, want) => {
  const ok = JSON.stringify(got) === JSON.stringify(want);
  results.push({ ok, name, got, want });
};

setTimeout(() => {
  const d = window.document;
  const $ = s => d.querySelector(s);
  const fire = (el, type) => el.dispatchEvent(new window.Event(type, { bubbles: true }));
  const type = (el, v) => { el.value = v; fire(el, 'input'); fire(el, 'blur'); };
  const click = el => el.dispatchEvent(new window.MouseEvent('click', { bubbles: true }));

  if (typeof window.openAdv !== 'function') {
    console.log(JSON.stringify({ fatal: 'openAdv missing — page JS did not initialise', errors }));
    return;
  }
  window.openAdv();                                   // populate the modal from ADV
  const ADV = window.ADV;

  // ── the modal opens showing the CURRENT values, not blanks ────────────────
  check('modal shows current bitrate floor', $('#adv-floor').value, String(ADV.floor));
  check('modal shows current tier bpp (Excellent)', $('#adv-bpp-EXCELLENT').value,
        String(ADV.bpp.EXCELLENT));

  // ── Quality tiers ─────────────────────────────────────────────────────────
  type($('#adv-bpp-EXCELLENT'), '0.15');
  check('typing a tier bpp updates ADV', ADV.bpp.EXCELLENT, 0.15);
  check('...and leaves the other tiers alone', ADV.bpp.OK, 0.0643);
  // The Advanced editor spells out the full implication: this density, for the
  // chosen codec, at 1080p30 AND 4K, and what frame rate does to it.
  const note = d.querySelector('[data-bpp-note="EXCELLENT"]').textContent;
  check('...and the row spells out the bitrate at 1080p and 4K',
        /Mbps at 1080p30/.test(note) && /at 4K30/.test(note) && /60fps/.test(note), true);
  check('...naming the codec it applies to', /H\.26[45]/.test(note), true);
  // The FLOW states density only. A bitrate there is true at exactly one
  // resolution and frame rate, so it is the misleading half of the story —
  // this guards the decision made in 1d01b9c against being undone again.
  const tag = window.eval('M').find(q => q.id === 'quality').opts[2].tag;
  check('...and the QUALITY question advertises the new density', /0\.15 BPP/.test(tag), true);
  check('...as BPP alone, with no bitrate', /Mbps|kbps/i.test(tag), false);
  // out-of-order tiers must be called out, not silently accepted
  type($('#adv-bpp-GOOD'), '0.2');
  check('a tier above the one over it is flagged',
        $('#adv-bpp-summary').classList.contains('warn'), true);
  click($('#adv-bpp-reset'));
  check('Reset tiers restores the anchors', ADV.bpp.EXCELLENT, 0.1093);
  check('...for every tier', [ADV.bpp.OK, ADV.bpp.GOOD, ADV.bpp.STELLAR, ADV.bpp.INSANE],
        [0.0643, 0.0804, 0.1286, 0.1447]);
  check('...and clears the warning', $('#adv-bpp-summary').classList.contains('warn'), false);

  // ── Ignore rules ──────────────────────────────────────────────────────────
  type($('#adv-ign-under'), '250');
  check('"smaller than" reaches ADV', ADV.ignUnderMb, 250);
  type($('#adv-ign-over'), '8000');
  check('"larger than" reaches ADV', ADV.ignOverMb, 8000);
  type($('#adv-ign-exts'), '.AVI, wmv , ');
  check('extensions: dots stripped, lowercased, blanks dropped', ADV.ignExts, ['avi', 'wmv']);
  type($('#adv-ign-names'), 'sample, trailer');
  check('filename fragments split on commas', ADV.ignNames, ['sample', 'trailer']);
  check('the summary says what will happen',
        /under 250 MB/.test($('#adv-ign-summary').textContent) &&
        /\.avi/.test($('#adv-ign-summary').textContent), true);
  // contradictory bounds are a real foot-gun: warn, don't silently ignore nothing
  type($('#adv-ign-under'), '9000');
  check('under > over is flagged as covering everything',
        $('#adv-ign-summary').classList.contains('warn'), true);
  click($('#adv-ign-reset'));
  check('Clear rules empties all four',
        [ADV.ignUnderMb, ADV.ignOverMb, ADV.ignExts, ADV.ignNames], ['', '', [], []]);
  check('...and the boxes are visibly empty', $('#adv-ign-exts').value, '');

  // ── the toggle that moved here ────────────────────────────────────────────
  const ledgerWas = ADV.ledger;
  click($('#adv-ledger'));
  check('history toggle flips', ADV.ledger, !ledgerWas);
  check('...and shows it', $('#adv-ledger').classList.contains('on'), !ledgerWas);
  click($('#adv-ledger'));

  // ── the rest of the modal still works ─────────────────────────────────────
  type($('#adv-floor'), '2500');
  check('bitrate floor', ADV.floor, 2500);
  type($('#adv-tol'), '25');
  check('re-encode tolerance', ADV.tol, 25);
  type($('#adv-hevc-hd'), '0.55');
  check('HEVC factor HD', ADV.hevcHd, 0.55);
  type($('#adv-ab-stereo'), '192');
  check('audio bitrate stereo', ADV.abStereo, 192);
  type($('#adv-jobs'), '3');
  check('parallel jobs', ADV.jobs, 3);
  const mkv = [...$('#adv-container').querySelectorAll('button')].find(b => b.dataset.v === 'mkv');
  click(mkv);
  check('container segmented control', ADV.container, 'mkv');
  check('...and highlights the chosen one', mkv.classList.contains('on'), true);
  $('#adv-audio').value = 'flac'; fire($('#adv-audio'), 'change');
  check('audio policy', ADV.audio, 'flac');
  const leave = [...$('#adv-format').querySelectorAll('button')].find(b => b.dataset.v === 'leave');
  click(leave);
  check('non-MP4 policy', ADV.format, 'leave');
  type($('#adv-subs-langs'), 'eng, fre');
  check('subtitle languages', ADV.subLangs, ['eng', 'fre']);
  const hoh = $('#adv-subs-kinds').querySelector('[data-k="hoh"]');
  click(hoh);
  check('unticking a subtitle kind removes it', ADV.subKinds.includes('hoh'), false);
  // ticking nothing silently discards every subtitle — it must say so
  click($('#adv-subs-kinds').querySelector('[data-k="normal"]'));
  click($('#adv-subs-kinds').querySelector('[data-k="forced"]'));
  check('no subtitle kinds ticked is warned about',
        $('#adv-subs-summary').classList.contains('warn'), true);

  // ── global reset ──────────────────────────────────────────────────────────
  click($('#adv-reset'));
  check('Reset to defaults restores everything',
        [ADV.floor, ADV.container, ADV.audio, ADV.format, ADV.jobs, ADV.bpp.EXCELLENT],
        [1500, 'auto', 'passthrough', 'convert', 1, 0.1093]);
  check('...including the subtitle kinds', ADV.subKinds, ['normal', 'forced', 'hoh']);
  check('...and does not share state with the defaults object',
        (() => { window.ADV.bpp.OK = 9; click($('#adv-reset')); return window.ADV.bpp.OK; })(), 0.0643);

  // ── every section heading must look like a section heading ────────────────
  // Missed by the first version of this harness and caught by eye in a browser:
  // wrapping an <h3> in a header div (to sit a button beside it) broke the
  // `.adv-sec > h3` child selector, so two sections rendered as plain black text
  // while the rest were orange mono caps. State was perfect; only the CSS lied.
  const heads = [...d.querySelectorAll('#adv-sheet h3')];
  const styles = heads.map(h => {
    const c = window.getComputedStyle(h);
    return [c.textTransform, c.fontSize, c.color].join('|');
  });
  check('every Advanced heading is styled the same', new Set(styles).size, 1);
  check('...and there are headings to check', heads.length >= 7, true);

  // ── the software picker ───────────────────────────────────────────────────
  const SW = window.eval('SW');
  SW.rows = [
    { path: '/m/big.mkv',   name: 'big.mkv',   bytes: 9.0e9, res: '1920x1080', dur: 3600, saving: 85 },
    { path: '/m/mid.mkv',   name: 'mid.mkv',   bytes: 4.5e9, res: '1920x1080', dur: 2700, saving: 70 },
    { path: '/m/small.mkv', name: 'small.mkv', bytes: 0.8e9, res: '1280x720',  dur: 1500, saving: 40 },
  ];
  SW.measured = true; SW.picked.clear();
  window.swRender();
  check('picker lists every candidate', d.querySelectorAll('#sw-list .sw-row').length, 3);
  click(d.querySelector('#sw-list .sw-row[data-i="0"]'));
  check('clicking a row ticks it', [...SW.picked], ['/m/big.mkv']);
  check('...and that is what the engine will be sent', window.__softwareFiles, ['/m/big.mkv']);
  check('...and the row shows as ticked',
        d.querySelector('#sw-list .sw-row[data-i="0"]').classList.contains('on'), true);
  click(d.querySelector('#sw-list .sw-row[data-i="0"]'));
  check('clicking again unticks', window.__softwareFiles, []);
  $('#sw-big').value = '4';
  click(d.querySelector('#sw-sheet [data-pick="big"]'));
  check('"everything over 4 GB" ticks exactly those',
        [...SW.picked].sort(), ['/m/big.mkv', '/m/mid.mkv']);
  check('...and the footer counts them', /2<\/b> of 3/.test($('#sw-foot').innerHTML), true);
  check('...and estimates the slow time', /software/.test($('#sw-foot').textContent), true);
  click(d.querySelector('#sw-sheet [data-pick="all"]'));
  check('Tick all', window.__softwareFiles.length, 3);
  click(d.querySelector('#sw-sheet [data-pick="none"]'));
  check('Tick none', window.__softwareFiles.length, 0);

  // A 4K file costs about 4x a 1080p one to encode; a duration-only estimate
  // under-priced it by that factor. Same duration, four times the pixels.
  const hd = { path: '/a', name: 'a', bytes: 1e9, res: '1920x1080', px: 1920*1080, dur: 3600, saving: 50 };
  const uhd = { path: '/b', name: 'b', bytes: 1e9, res: '3840x2160', px: 3840*2160, dur: 3600, saving: 50 };
  const foot = rows => { SW.rows = rows; SW.picked = new Set(rows.map(r => r.path));
                         window.swFoot(); return $('#sw-foot').textContent; };
  check('a 4K file is priced above a 1080p one of the same length',
        foot([hd]) !== foot([uhd]), true);

  process.stdout.write(JSON.stringify({ results, errors }));
  process.exit(0);   // the page keeps timers alive; nothing left to wait for
}, 1500);
