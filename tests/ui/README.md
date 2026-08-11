# UI harness

Loads `vtc/vtc_app_v3.html` in jsdom and operates the Advanced-settings controls
the way a person would, then reads back the state the engine is handed.

    npm install jsdom
    node drive.js | python3 -m json.tool

`tests/test_ui.py` runs this under pytest and **skips** when node or jsdom is
missing, so the Python suite stands alone.

Two jsdom gaps are stubbed in `beforeParse` — `matchMedia` and the media
element methods. They are not page bugs: every real browser and WKWebView has
them, but without `matchMedia` the page script dies on its reduced-motion query
and every later `const` stays in the temporal dead zone.
